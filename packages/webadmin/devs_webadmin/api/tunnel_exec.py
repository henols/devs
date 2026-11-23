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

# Run the command with its output in a file, and record its PID next to it so the
# command can be stopped from a later exec. After `exec`, $$ is the command's PID.
_WRAPPER = (
    'umask 077; output=$1; shift; echo $$ > "$output.pid"; '
    'exec "$@" > "$output" 2>&1'
)
# Stop the command if it is still running, then remove its files.
_STOP = 'pid=$(cat "$1.pid" 2>/dev/null) && kill "$pid" 2>/dev/null; '
_CLEANUP = 'rm -f -- "$1" "$1.pid"'


def _read_file(client: docker.DockerClient, container_name: str, path: str) -> str:
    try:
        archive, _ = client.api.get_archive(container_name, path)
    except NotFound:
        # The command may not have opened the output file yet.
        return ""
    with tarfile.open(fileobj=io.BytesIO(b"".join(archive))) as stream:
        member = stream.next()
        output = stream.extractfile(member) if member is not None else None
        return output.read().decode("utf-8", errors="replace") if output else ""


class TunnelExec:
    """Read output and exit status through ordinary Docker API requests.

    ``close()`` stops the command inside the container if it is still running and
    removes its output. ``release()`` leaves the command running and its output in
    place, for long-running processes such as the tunnel itself.
    """

    def __init__(
        self, container_name: str, command: list, output_path: Optional[str] = None
    ) -> None:
        # TCP proxies can forward the first exec output frame but lose the remaining
        # output and EOF. Use detached exec, inspect, and an output file.
        self._container = container_name
        self._path = output_path or f"/tmp/.devs-tunnel-{uuid4().hex}"
        self._closed = False
        self._returncode: Optional[int] = None
        self._client = docker.from_env(timeout=5)
        try:
            self._id = self._start(
                ["/bin/sh", "-c", _WRAPPER, "sh", self._path, *command]
            )
        except BaseException:
            self.close()
            raise

    def _start(self, command: list) -> str:
        api = self._client.api
        exec_id: str = api.exec_create(self._container, command)["Id"]
        api.exec_start(exec_id, detach=True)
        return exec_id

    def poll(self) -> Optional[int]:
        if self._returncode is None:
            state = self._client.api.exec_inspect(self._id)
            if not state["Running"]:
                self._returncode = state["ExitCode"]
        return self._returncode

    def output(self) -> str:
        return _read_file(self._client, self._container, self._path)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            with suppress(DockerException, OSError):
                running = getattr(self, "_id", None) is not None and self.poll() is None
                script = (_STOP if running else "") + _CLEANUP
                self._start(["/bin/sh", "-c", script, "sh", self._path])
        finally:
            self._client.close()

    def release(self) -> None:
        if not self._closed:
            self._closed = True
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


def container_workspace_dir(container_name: str) -> Optional[str]:
    """Return the /workspaces/<name> folder a container actually mounts.

    Live-mode containers mount the host folder under its own name, so the path
    can't be derived from the container name.
    """
    with closing(docker.from_env(timeout=5)) as client:
        mounts = client.api.inspect_container(container_name).get("Mounts") or []
    for mount in mounts:
        destination = mount.get("Destination") or ""
        parts = destination.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "workspaces" and parts[1]:
            return destination
    return None
