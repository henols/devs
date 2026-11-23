"""Run short commands with the tunnel CLI over Docker exec."""

import subprocess
import time
from queue import Empty, Queue
from threading import Thread
from typing import Optional
from uuid import uuid4


class TunnelExec:
    """Read command completion even when a Docker proxy keeps the stream open."""

    def __init__(self, container_name: str, command: list) -> None:
        # Docker exec can deliver all output through a proxy without delivering EOF.
        # Let the in-container shell report the exit code before closing the client.
        self._marker = f"devs-exec-{uuid4().hex}:"
        self._lines = Queue()
        self.returncode = None
        self._process = subprocess.Popen(
            [
                "docker",
                "exec",
                container_name,
                "/bin/sh",
                "-c",
                'marker=$1; shift; "$@"; status=$?; '
                'printf "\\n%s%d\\n" "$marker" "$status"; exit "$status"',
                "sh",
                self._marker,
                *command,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        Thread(target=self._read_output, daemon=True).start()

    def _read_output(self) -> None:
        try:
            assert self._process.stdout is not None
            with self._process.stdout as output:
                for line in output:
                    if line.startswith(self._marker):
                        self.returncode = int(line[len(self._marker) :].strip())
                        return
                    self._lines.put(line)
            # EOF without the completion marker indicates an exec transport failure.
            self.returncode = self._process.poll() or 1
        finally:
            self.close()
            self._lines.put(None)

    def read_line(self, deadline: float) -> Optional[str]:
        remaining = max(0, deadline - time.monotonic())
        try:
            return self._lines.get(timeout=remaining)
        except Empty:
            raise subprocess.TimeoutExpired(self._process.args, remaining) from None

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
        self._process.wait()


def run_tunnel_exec(
    container_name: str, command: list, timeout: float = 10
) -> subprocess.CompletedProcess:
    """Return the remote exit code and combined output within the deadline."""
    execution = TunnelExec(container_name, command)
    deadline = time.monotonic() + timeout
    output = []
    try:
        while (line := execution.read_line(deadline)) is not None:
            output.append(line)
        return subprocess.CompletedProcess(
            command, execution.returncode, stdout="".join(output)
        )
    finally:
        execution.close()
