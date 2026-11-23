"""Run VS Code tunnel commands without an attached Docker exec stream."""

import io
import subprocess
import tarfile
import time
from contextlib import closing, suppress
from typing import Optional
from uuid import uuid4

import docker
from docker.errors import DockerException, NotFound


class TunnelExec:
    """Read output and exit status through ordinary Docker API requests."""

    def __init__(self, container_name: str, command: list) -> None:
        # TCP proxies can forward the first exec output frame but lose the remaining
        # output and EOF. Use detached exec, inspect, and a private output file.
        self._container = container_name
        self._path = f"/tmp/.devs-tunnel-{uuid4().hex}"
        self._client = docker.from_env(timeout=5)
        try:
            self._id = self._start(
                [
                    "/bin/sh",
                    "-c",
                    'umask 077; output=$1; shift; exec "$@" > "$output" 2>&1',
                    "sh",
                    self._path,
                    *command,
                ]
            )
        except BaseException:
            self.close()
            raise

    def _start(self, command: list) -> str:
        api = self._client.api
        exec_id = api.exec_create(self._container, command)["Id"]
        api.exec_start(exec_id, detach=True)
        return exec_id

    def poll(self) -> Optional[int]:
        state = self._client.api.exec_inspect(self._id)
        return None if state["Running"] else state["ExitCode"]

    def output(self) -> str:
        try:
            archive, _ = self._client.api.get_archive(self._container, self._path)
        except NotFound:
            # The command may not have opened the output file yet.
            return ""
        with tarfile.open(fileobj=io.BytesIO(b"".join(archive))) as stream:
            member = stream.next()
            output = stream.extractfile(member) if member is not None else None
            return output.read().decode("utf-8", errors="replace") if output else ""

    def close(self) -> None:
        try:
            with suppress(DockerException, OSError):
                self._start(["rm", "-f", "--", self._path])
        finally:
            self._client.close()


def run_tunnel_exec(
    container_name: str, command: list, timeout: float = 10
) -> subprocess.CompletedProcess:
    """Return the remote exit code and output within the polling deadline."""
    deadline = time.monotonic() + timeout
    with closing(TunnelExec(container_name, command)) as execution:
        while True:
            returncode = execution.poll()
            if returncode is not None:
                return subprocess.CompletedProcess(
                    command, returncode, stdout=execution.output()
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(min(remaining, 0.1))
