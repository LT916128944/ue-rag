"""关闭或启动 UnrealEditor.exe。不 PIE，不保存。"""
import subprocess
import time
from pathlib import Path

import httpx

from config import UE_EDITOR_CMD, UE_MCP_URL, UE_PROJECT

_WAIT_SECONDS = 120.0


def editor_exe() -> Path:
    return UE_EDITOR_CMD.with_name("UnrealEditor.exe")


def editor_running() -> bool:
    proc = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return "UnrealEditor.exe" in (proc.stdout or "")


def mcp_up() -> bool:
    """端口有响应才算连通。连接超时和拒绝连接都是没连通，避免把挂起的端口当成编辑器已开。"""
    try:
        httpx.get(UE_MCP_URL, timeout=2.0)
        return True
    except httpx.ConnectError:
        return False
    except httpx.TimeoutException:
        return False
    except Exception:
        return True


def task_touches_cpp(task: dict | None) -> bool:
    for item in (task or {}).get("verify") or []:
        if isinstance(item, dict) and item.get("kind") == "cpp_build":
            return True
    return False


def _wait_until_stopped(seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not editor_running():
            return True
        time.sleep(1)
    return not editor_running()


def close_editor() -> dict:
    """改 C++ 或编译前关掉编辑器，避免模块 DLL 被占用。未保存的编辑器改动会丢失。"""
    if not editor_running():
        return {"ok": True, "closed": False, "note": "编辑器未开"}
    subprocess.run(
        ["taskkill", "/IM", "UnrealEditor.exe", "/T"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if _wait_until_stopped(15):
        return {"ok": True, "closed": True, "note": "已关闭编辑器"}
    subprocess.run(
        ["taskkill", "/F", "/IM", "UnrealEditor.exe", "/T"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if _wait_until_stopped(15):
        return {"ok": True, "closed": True, "note": "已强制关闭编辑器。未保存的编辑器改动会丢失。"}
    return {"ok": False, "closed": False, "note": "编辑器未能关闭"}


def launch_command() -> list[str]:
    """跳过 Restore Packages。taskkill 不是正常退出，下次启动会模态询问是否恢复自动保存，MCP 因此等不到。"""
    return [str(editor_exe()), str(UE_PROJECT), "-AutoDeclinePackageRecovery"]


def launch_editor() -> dict:
    if editor_running():
        return {
            "ok": True,
            "started": False,
            "note": "编辑器已在运行。新编译的模块要重启编辑器后才加载，本次不另开进程。",
        }
    exe = editor_exe()
    if not exe.is_file():
        return {"ok": False, "started": False, "note": f"找不到 {exe}"}
    subprocess.Popen(
        launch_command(),
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    return {"ok": True, "started": True, "note": "已启动 UnrealEditor"}


def ensure_editor() -> dict:
    """编辑器或 MCP 没起来时启动一次并等待端口。已经在跑就只等。"""
    if mcp_up():
        return {"ok": True, "started": False, "note": "MCP 已连通"}
    launched = launch_editor()
    if not launched.get("ok"):
        return launched
    deadline = time.time() + _WAIT_SECONDS
    while time.time() < deadline:
        if mcp_up():
            note = launched.get("note") or "MCP 已连通"
            return {"ok": True, "started": launched.get("started"), "note": note}
        time.sleep(2)
    return {"ok": False, "started": launched.get("started"), "note": "编辑器已启动，但 MCP 在 120 秒内没有连通"}
