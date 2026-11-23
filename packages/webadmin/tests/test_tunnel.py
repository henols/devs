"""Regression tests for VS Code tunnels over a Docker proxy."""

import os
import subprocess
import sys
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from devs_webadmin.api import routes
from devs_webadmin.api.tunnel_exec import run_tunnel_exec
from devs_webadmin.app import app

PROJECT = {"project_name": "org-demo", "dev_name": "work"}
CONTAINER = "dev-org-demo-work"


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys, time\n"
        "mode = os.getenv('FAKE_DOCKER_MODE', 'linger')\n"
        "if mode == 'failure':\n"
        "    print('Docker could not execute the command', flush=True)\n"
        "    sys.exit(1)\n"
        "if mode == 'linger':\n"
        "    subprocess.run(sys.argv[3:])\n"
        "# Model a proxy that keeps the client stream open after any output.\n"
        "time.sleep(30)\n"
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return docker


@pytest.mark.parametrize("returncode", [0, 127])
def test_exec_completes_without_stream_eof(fake_docker, returncode):
    result = run_tunnel_exec(
        CONTAINER, ["/bin/sh", "-c", f"printf result; exit {returncode}"], timeout=2
    )

    assert result.returncode == returncode
    assert result.stdout.strip() == "result"


def test_exec_times_out_when_command_never_reports_completion(fake_docker, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_MODE", "no_output")
    with pytest.raises(subprocess.TimeoutExpired):
        run_tunnel_exec(CONTAINER, ["true"], timeout=0.1)


def test_exec_keeps_docker_transport_error(fake_docker, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_MODE", "failure")
    result = run_tunnel_exec(CONTAINER, ["true"], timeout=2)
    assert result.returncode != 0
    assert "Docker could not execute" in result.stdout


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
    execution = Mock(returncode=None)
    execution.read_line.side_effect = [
        "Go to https://github.com/login/device\n",
        "and use code ABCD-1234\n",
    ]
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

    execution.returncode = 0
    assert (
        client.get("/api/tunnel/auth/status", params=PROJECT).json()["status"]
        == "authenticated"
    )
