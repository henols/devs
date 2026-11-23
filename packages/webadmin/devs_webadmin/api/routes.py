"""API routes for webadmin."""

import asyncio
import json
import re
import subprocess
import time
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import structlog

from devs_common.core.container import (
    ContainerManager,
    make_tunnel_name,
    get_container_workspace_dir,
)
from devs_common.core.workspace import WorkspaceManager
from devs_common.core.project import Project
from devs_common.utils.repo_cache import RepoCache
from devs_common.utils.docker_client import DockerClient
from devs_common.devs_config import DevsConfigLoader
from devs_common.exceptions import DevsError

from ..config import config
from .tunnel_exec import TunnelExec, run_tunnel_exec

logger = structlog.get_logger()

router = APIRouter(prefix="/api")

_TUNNEL_CLI = "/usr/local/bin/code"
_TUNNEL_LOG = "/tmp/vscode-tunnel.log"
_auth_processes: Dict[str, TunnelExec] = {}


class StartRequest(BaseModel):
    repo: str  # org/repo format
    dev_name: str


class ContainerActionRequest(BaseModel):
    container_name: str  # Docker container name from list


class TunnelRequest(BaseModel):
    project_name: str  # org-repo format (from container list)
    dev_name: str


def _get_repo_project(repo: str) -> Project:
    """Clone/update a repo via cache and return a Project for it."""
    repo_cache = RepoCache(cache_dir=config.repo_cache_dir)
    repo_path = repo_cache.ensure_repo(repo)
    return Project(project_dir=repo_path)


def _get_container_name(project_name: str, dev_name: str) -> str:
    """Derive container name from project_name + dev_name."""
    project_prefix = config.project_prefix if config else "dev"
    return f"{project_prefix}-{project_name}-{dev_name}"


@router.get("/containers")
async def list_containers(repo: Optional[str] = None) -> dict:
    """List containers, optionally filtered by repo."""
    try:
        if repo:
            project = await asyncio.to_thread(_get_repo_project, repo)
            container_manager = ContainerManager(project, config)
            containers = await asyncio.to_thread(container_manager.list_containers)
        else:
            containers = await asyncio.to_thread(ContainerManager.list_all_containers)

        return {
            "containers": [
                {
                    "name": c.name,
                    "dev_name": c.dev_name,
                    "project_name": c.project_name,
                    "status": c.status,
                    "container_id": c.container_id,
                    "created": c.created.isoformat() if c.created else None,
                    "mode": "live" if c.labels.get("devs.live") == "true" else "copy",
                }
                for c in containers
            ]
        }
    except DevsError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/start")
async def start_container(request: StartRequest) -> dict:
    """Start a named devcontainer for a repo."""
    try:
        project = await asyncio.to_thread(_get_repo_project, request.repo)
        container_manager = ContainerManager(project, config)
        workspace_manager = WorkspaceManager(project, config)

        devs_env = DevsConfigLoader.load_env_vars(request.dev_name, project.info.name)
        extra_env = devs_env if devs_env else None

        workspace_dir = await asyncio.to_thread(
            workspace_manager.create_workspace, request.dev_name
        )

        success = await asyncio.to_thread(
            container_manager.ensure_container_running,
            request.dev_name,
            workspace_dir,
            extra_env=extra_env,
        )

        if success:
            return {
                "status": "started",
                "repo": request.repo,
                "dev_name": request.dev_name,
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to start container")

    except DevsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error starting container", repo=request.repo, dev_name=request.dev_name)
        raise HTTPException(status_code=500, detail=str(e))


def _stop_by_name(container_name: str, remove: bool) -> bool:
    """Stop (and optionally remove) a container by its Docker name."""
    docker = DockerClient()
    try:
        docker.stop_container(container_name)
    except Exception:
        return False
    if remove:
        try:
            docker.remove_container(container_name)
        except Exception:
            pass
    return True


def _clean_workspace(project_name: str, dev_name: str) -> None:
    """Remove workspace for a project/dev combination."""
    # Build workspace path directly from project name + dev name
    workspace_name = f"{project_name}-{dev_name}"
    workspace_dir = config.workspaces_dir / workspace_name
    if workspace_dir.exists():
        import shutil
        shutil.rmtree(workspace_dir)


@router.post("/restart")
async def restart_container(request: ContainerActionRequest) -> dict:
    """Restart a stopped container by Docker name."""
    def _restart(container_name: str) -> bool:
        docker = DockerClient()
        try:
            docker.start_container(container_name)
            return True
        except Exception:
            return False

    success = await asyncio.to_thread(_restart, request.container_name)

    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"Container {request.container_name} not found or cannot be started",
        )

    return {"status": "started", "container_name": request.container_name}


@router.post("/stop")
async def stop_container(request: ContainerActionRequest) -> dict:
    """Stop a container by Docker name (preserves state)."""
    success = await asyncio.to_thread(_stop_by_name, request.container_name, False)

    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"Container {request.container_name} not found or already stopped",
        )

    return {"status": "stopped", "container_name": request.container_name}


@router.post("/clean")
async def clean_container(request: ContainerActionRequest) -> dict:
    """Stop, remove container and clean workspace by Docker name."""
    # Get container labels before removing so we can find the workspace
    docker = DockerClient()
    dev_name = None
    project_name = None
    try:
        containers = docker.find_containers_by_labels({"devs.managed": "true"})
        for c in containers:
            if c["name"] == request.container_name:
                dev_name = c["labels"].get("devs.dev")
                project_name = c["labels"].get("devs.project")
                break
    except Exception:
        pass

    # Stop and remove the container
    await asyncio.to_thread(_stop_by_name, request.container_name, True)

    # Clean workspace if we found the labels
    if dev_name and project_name:
        await asyncio.to_thread(_clean_workspace, project_name, dev_name)

    return {"status": "cleaned", "container_name": request.container_name}


# --- Tunnel helpers ---

def _tunnel_error(result: subprocess.CompletedProcess) -> str:
    if result.returncode == 127:
        return f"Install the standalone VS Code CLI at {_TUNNEL_CLI} in the devcontainer."
    return result.stdout.strip() or f"VS Code CLI exited with status {result.returncode}."


def _tunnel_info(project_name: str, dev_name: str) -> dict:
    """Derive all tunnel-related names/paths from project_name + dev_name."""
    container_name = _get_container_name(project_name, dev_name)
    tunnel_name = make_tunnel_name(container_name)
    workspace_dir = get_container_workspace_dir(container_name)
    return {
        "container_name": container_name,
        "tunnel_name": tunnel_name,
        "workspace_dir": workspace_dir,
        "web_url": f"https://vscode.dev/tunnel/{tunnel_name}{workspace_dir}",
        "vscode_cmd": f"code --remote tunnel+{tunnel_name} {workspace_dir}",
    }


# --- Tunnel endpoints ---

@router.get("/tunnel/status")
async def tunnel_status(project_name: str, dev_name: str) -> dict:
    """Get VS Code tunnel status for a container."""
    info = _tunnel_info(project_name, dev_name)
    try:
        result = await asyncio.to_thread(
            run_tunnel_exec, info["container_name"], [_TUNNEL_CLI, "tunnel", "status"]
        )

        raw_status = {}
        if result.returncode == 0:
            for line in reversed(result.stdout.splitlines()):
                try:
                    status = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(status, dict) and "tunnel" in status:
                    raw_status = status
                    break

        tunnel_data = raw_status.get("tunnel") or {}
        is_connected = tunnel_data.get("tunnel") == "Connected"

        # Use the actual tunnel name from status if available
        actual_tunnel_name = tunnel_data.get("name") or info["tunnel_name"]
        workspace_dir = info["workspace_dir"]
        web_url = f"https://vscode.dev/tunnel/{actual_tunnel_name}{workspace_dir}"
        vscode_cmd = f"code --remote tunnel+{actual_tunnel_name} {workspace_dir}"

        return {
            "running": is_connected,
            "tunnel_name": actual_tunnel_name,
            "web_url": web_url if is_connected else None,
            "vscode_cmd": vscode_cmd if is_connected else None,
        }
    except subprocess.TimeoutExpired:
        return {"running": False, "message": "Status check timed out"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tunnel/start")
async def tunnel_start(request: TunnelRequest) -> dict:
    """Start a VS Code tunnel in a container (background).

    Returns immediately with {"status": "starting"}.  The caller should poll
    GET /api/tunnel/status until running==true.
    """
    info = _tunnel_info(request.project_name, request.dev_name)
    container_name = info["container_name"]
    tunnel_name = info["tunnel_name"]

    def _start() -> dict:
        result = run_tunnel_exec(
            container_name, [_TUNNEL_CLI, "tunnel", "user", "show"]
        )
        if result.returncode == 1 and "not logged in" in result.stdout.lower():
            return {"status": "auth_required", "tunnel_name": tunnel_name}
        if result.returncode != 0:
            raise HTTPException(status_code=400, detail=_tunnel_error(result))

        # The VS Code CLI's singleton handles an already-running tunnel.
        result = subprocess.run(
            [
                "docker", "exec", "-d", container_name, "/bin/sh", "-c",
                'log=$1; shift; exec "$@" > "$log" 2>&1',
                "sh", _TUNNEL_LOG, _TUNNEL_CLI, "tunnel",
                "--accept-server-license-terms", "--name", tunnel_name,
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise HTTPException(
                status_code=400, detail=result.stderr.strip() or _tunnel_error(result)
            )

        return {"status": "starting", "tunnel_name": tunnel_name}

    try:
        return await asyncio.to_thread(_start)
    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Tunnel startup timed out.") from None
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tunnel/kill")
async def tunnel_kill(request: TunnelRequest) -> dict:
    """Kill a running VS Code tunnel."""
    info = _tunnel_info(request.project_name, request.dev_name)
    try:
        result = await asyncio.to_thread(
            run_tunnel_exec, info["container_name"], [_TUNNEL_CLI, "tunnel", "kill"]
        )
        if result.returncode != 0:
            raise HTTPException(status_code=400, detail=_tunnel_error(result))
        return {"killed": True, "message": "Tunnel processes killed"}
    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Tunnel shutdown timed out.") from None
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tunnel/auth")
async def tunnel_auth_start(request: TunnelRequest) -> dict:
    """Start tunnel auth (GitHub device flow).

    Launches the login command, captures the device URL + code,
    and returns them so the user can complete auth in their browser.
    The process keeps running until auth completes or is cancelled.
    """
    info = _tunnel_info(request.project_name, request.dev_name)
    container_name = info["container_name"]

    previous = _auth_processes.pop(container_name, None)
    if previous is not None:
        await asyncio.to_thread(previous.close)

    def _start_auth() -> dict:
        execution = TunnelExec(
            container_name, [_TUNNEL_CLI, "tunnel", "user", "login", "--provider", "github"]
        )
        keep_open = False
        try:
            output_lines = []
            deadline = time.monotonic() + 30
            while (line := execution.read_line(deadline)) is not None:
                output_lines.append(line)
                output = " ".join(output_lines)
                url = re.search(r"https://github\.com/login/device", output)
                code = re.search(r"code\s+([A-Z0-9]{4}-[A-Z0-9]{4})", output, re.IGNORECASE)
                if url and code:
                    device_url, device_code = url.group(), code.group(1)
                    _auth_processes[container_name] = execution
                    keep_open = True
                    return {
                        "status": "waiting_for_browser",
                        "device_url": device_url,
                        "device_code": device_code,
                        "message": f"Open {device_url} and enter code {device_code}",
                    }
            if execution.returncode == 0:
                return {"status": "already_authenticated"}
            return {
                "status": "error",
                "message": "Could not get device code. " + "".join(output_lines[-5:]).strip(),
            }
        finally:
            if not keep_open:
                execution.close()

    try:
        return await asyncio.to_thread(_start_auth)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="GitHub sign-in timed out.") from None
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tunnel/auth/status")
async def tunnel_auth_status(project_name: str, dev_name: str) -> dict:
    """Check if a pending tunnel auth has completed."""
    info = _tunnel_info(project_name, dev_name)
    container_name = info["container_name"]

    execution = _auth_processes.get(container_name)
    if execution is None:
        return {"status": "no_pending_auth"}

    returncode = execution.returncode
    if returncode is None:
        return {"status": "waiting_for_browser"}

    del _auth_processes[container_name]
    if returncode == 0:
        return {"status": "authenticated"}
    else:
        return {"status": "failed", "message": f"Auth process exited with code {returncode}"}
