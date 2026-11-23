"""Regression tests for VS Code tunnels over a Docker proxy."""

import io
import subprocess
import tarfile
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from devs_webadmin.api import routes
from devs_webadmin.api import tunnel_exec
from devs_webadmin.app import app

PROJECT = {"project_name": "org-demo", "dev_name": "work"}
CONTAINER = "dev-org-demo-work"


@pytest.fixture
def fake_docker(monkeypatch):
    docker = Mock()
    docker.api.exec_create.return_value = {"Id": "exec-id"}
    data = b'{"tunnel":null}\n'
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("output")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    docker.api.get_archive.return_value = (iter([buffer.getvalue()]), {})
    monkeypatch.setattr(tunnel_exec.docker, "from_env", Mock(return_value=docker))
    return docker


@pytest.mark.parametrize("returncode", [0, 127])
def test_exec_reads_output_and_status_without_attaching(fake_docker, returncode):
    fake_docker.api.exec_inspect.side_effect = [
        {"Running": True, "ExitCode": None},
        {"Running": False, "ExitCode": returncode},
    ]
    command = ["/usr/local/bin/code", "tunnel", "status"]
    result = tunnel_exec.run_tunnel_exec(CONTAINER, command, timeout=2)

    assert result.returncode == returncode
    assert result.stdout == '{"tunnel":null}\n'
    assert all(
        call.kwargs == {"detach": True}
        for call in fake_docker.api.exec_start.call_args_list
    )
    assert fake_docker.api.exec_create.call_args_list[0].args[1][-3:] == command
    output_path = fake_docker.api.get_archive.call_args.args[1]
    fake_docker.api.exec_create.assert_called_with(
        CONTAINER, ["rm", "-f", "--", output_path]
    )
    fake_docker.close.assert_called_once()


def test_exec_times_out_when_command_never_reports_completion(fake_docker):
    fake_docker.api.exec_inspect.return_value = {"Running": True, "ExitCode": None}
    with pytest.raises(subprocess.TimeoutExpired):
        tunnel_exec.run_tunnel_exec(CONTAINER, ["true"], timeout=0.1)
    fake_docker.api.get_archive.assert_not_called()
    fake_docker.close.assert_called_once()


def test_exec_allows_output_file_to_be_created_after_start(fake_docker):
    fake_docker.api.get_archive.side_effect = tunnel_exec.NotFound("not created yet")
    execution = tunnel_exec.TunnelExec(CONTAINER, ["true"])
    try:
        assert execution.output() == ""
    finally:
        execution.close()


def test_tunnel_status_uses_connected_name(monkeypatch):
    monkeypatch.setattr(
        routes,
        "run_tunnel_exec",
        Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                'diagnostic\n{"tunnel":{"tunnel":"Connected","name":"actual-name"}}\n',
            )
        ),
    )
    response = TestClient(app).get("/api/tunnel/status", params=PROJECT)
    assert response.status_code == 200
    assert response.json()["running"] is True
    assert (
        response.json()["web_url"]
        == "https://vscode.dev/tunnel/actual-name/workspaces/org-demo-work"
    )


def test_start_requests_sign_in_when_credentials_are_missing(monkeypatch):
    exec_command = Mock(
        return_value=subprocess.CompletedProcess([], 1, "not logged in\n")
    )
    monkeypatch.setattr(routes, "run_tunnel_exec", exec_command)
    response = TestClient(app).post("/api/tunnel/start", json=PROJECT)
    assert response.json()["status"] == "auth_required"
    exec_command.assert_called_once_with(
        CONTAINER, [routes._TUNNEL_CLI, "tunnel", "user", "show"]
    )


def test_start_reports_missing_standalone_cli(monkeypatch):
    monkeypatch.setattr(
        routes,
        "run_tunnel_exec",
        Mock(return_value=subprocess.CompletedProcess([], 127, "CLI missing\n")),
    )
    response = TestClient(app).post("/api/tunnel/start", json=PROJECT)
    assert response.status_code == 400
    assert "Install the standalone VS Code CLI" in response.json()["detail"]


def test_start_detaches_with_a_timeout(monkeypatch):
    monkeypatch.setattr(
        routes,
        "run_tunnel_exec",
        Mock(return_value=subprocess.CompletedProcess([], 0, "logged in\n")),
    )
    docker = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(routes.subprocess, "run", docker)
    response = TestClient(app).post("/api/tunnel/start", json=PROJECT)
    assert response.json()["status"] == "starting"
    args = docker.call_args.args[0]
    assert args[:4] == ["docker", "exec", "-d", CONTAINER]
    assert args[-2:] == ["--name", CONTAINER]
    assert docker.call_args.kwargs["timeout"] == 10


def test_device_flow_uses_remote_completion(monkeypatch):
    execution = Mock()
    execution.poll.return_value = None
    execution.output.return_value = (
        "Go to https://github.com/login/device\nand use code ABCD-1234\n"
    )
    monkeypatch.setattr(routes, "TunnelExec", Mock(return_value=execution))
    monkeypatch.setattr(routes, "_auth_processes", {})
    client = TestClient(app)

    auth = client.post("/api/tunnel/auth", json=PROJECT).json()
    assert auth["status"] == "waiting_for_browser"
    assert auth["device_code"] == "ABCD-1234"
    assert (
        client.get("/api/tunnel/auth/status", params=PROJECT).json()["status"]
        == "waiting_for_browser"
    )

    execution.poll.return_value = 0
    assert (
        client.get("/api/tunnel/auth/status", params=PROJECT).json()["status"]
        == "authenticated"
    )
