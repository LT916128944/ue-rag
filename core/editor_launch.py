"""关闭或启动 UnrealEditor.exe。不 PIE，不保存。"""
import re
import subprocess
import time
from pathlib import Path

import httpx

from config import UE_EDITOR_CMD, UE_MCP_URL, UE_PROJECT

_WAIT_SECONDS = 120.0
_OFFICIAL_PLUGIN = "ModelContextProtocol"
_MONOLITH_PLUGIN = "Monolith"
_PLUGIN_ENABLED = re.compile(
    r'("Name"\s*:\s*"(ModelContextProtocol|Monolith)"\s*,\s*"Enabled"\s*:\s*)(true|false)'
)


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


def _probe(url: str) -> bool:
    """端口有响应才算连通。连接超时和拒绝连接都是没连通，避免把挂起的端口当成编辑器已开。"""
    try:
        httpx.get(url, timeout=2.0)
        return True
    except httpx.ConnectError:
        return False
    except httpx.TimeoutException:
        return False
    except Exception:
        return True


def _mcp_urls() -> tuple[str, str]:
    """(本次后端的地址, 另一边的地址)。一次运行只认一边。"""
    from backends.select import current_backend
    from config import monolith_url

    if current_backend() == "monolith":
        return monolith_url(), UE_MCP_URL
    return UE_MCP_URL, monolith_url()


def mcp_up() -> bool:
    active, _other = _mcp_urls()
    return _probe(active)


def _split_note(active_up: bool, foreign_up: bool) -> str:
    """另一边也在听就拒绝。不改去连那一边。"""
    if not foreign_up:
        return ""
    from backends.select import current_backend

    active, other = _mcp_urls()
    if current_backend() == "monolith":
        if active_up:
            return (
                f"官方 MCP {other} 与 Monolith {active} 同时在听。"
                "本次后端是 monolith，请在插件里关掉 Unreal MCP（ModelContextProtocol）后重启编辑器。"
            )
        return (
            f"只听到官方 MCP {other}，Monolith {active} 没有连通。"
            "请确认 Monolith 已启用，并关掉 Unreal MCP（ModelContextProtocol）后重启编辑器。"
        )
    if active_up:
        return (
            f"Monolith {other} 与官方 MCP {active} 同时在听。"
            "本次后端是 official，请在插件里关掉 Monolith 后重启编辑器。"
        )
    return (
        f"只听到 Monolith {other}，官方 MCP {active} 没有连通。"
        "请确认 Unreal MCP 已启用，并关掉 Monolith 后重启编辑器。"
    )


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


def _plugin_enabled(backend: str) -> dict[str, bool] | None:
    """一次只留一个监听插件。另一边在 .uproject 里关掉，启动参数再关一次。"""
    if backend == "monolith":
        return {_OFFICIAL_PLUGIN: False, _MONOLITH_PLUGIN: True}
    if backend == "official":
        return {_OFFICIAL_PLUGIN: True, _MONOLITH_PLUGIN: False}
    return None


def backend_plugin_args(backend: str | None = None) -> list[str]:
    from config import editor_backend

    name = editor_backend() if backend is None else str(backend)
    enabled = _plugin_enabled(name)
    if enabled is None:
        return []
    turn_on = _MONOLITH_PLUGIN if name == "monolith" else _OFFICIAL_PLUGIN
    turn_off = _OFFICIAL_PLUGIN if name == "monolith" else _MONOLITH_PLUGIN
    return [f"-EnablePlugins={turn_on}", f"-DisablePlugins={turn_off}"]


def sync_backend_plugins(backend: str | None = None, project: Path | None = None) -> dict:
    """把 .uproject 里的两个监听插件写成和 UE_EDITOR_BACKEND 一致。不改其他插件。"""
    from config import editor_backend

    name = editor_backend() if backend is None else str(backend)
    enabled = _plugin_enabled(name)
    if enabled is None:
        return {"ok": False, "error": f"UE_EDITOR_BACKEND 只能是 official 或 monolith，当前是 {name!r}"}
    path = Path(project) if project is not None else UE_PROJECT
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
    except OSError as exc:
        return {"ok": False, "error": f"读不到 {path}: {exc}"}
    found: dict[str, bool] = {}

    def repl(match: re.Match) -> str:
        plugin = match.group(2)
        flag = "true" if enabled[plugin] else "false"
        found[plugin] = flag == "true"
        return match.group(1) + flag

    updated, count = _PLUGIN_ENABLED.subn(repl, text)
    missing = [plugin for plugin in enabled if plugin not in found]
    if missing or count != len(enabled):
        return {"ok": False, "error": f"{path.name} 里找不到插件 {', '.join(missing or list(enabled))}"}
    if updated != text:
        try:
            with path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
        except OSError as exc:
            return {"ok": False, "error": f"写不了 {path}: {exc}"}
    return {"ok": True, "backend": name, "enabled": enabled}


def launch_command() -> list[str]:
    """跳过 Restore Packages。taskkill 不是正常退出，下次启动会模态询问是否恢复自动保存，MCP 因此等不到。
    启动参数再关一次另一边的插件，避免 .uproject 还没同步时两个端口一起听。"""
    return [str(editor_exe()), str(UE_PROJECT), *backend_plugin_args(), "-AutoDeclinePackageRecovery"]


def launch_editor() -> dict:
    synced = sync_backend_plugins()
    if not synced.get("ok"):
        return {"ok": False, "started": False, "note": str(synced.get("error") or "插件开关没写上")}
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
    """编辑器或 MCP 没起来时启动一次并等待本次后端的端口。另一边也在听就拒绝，不改连。"""
    active, _other = _mcp_urls()
    active_up = _probe(active)
    foreign_up = _probe(_mcp_urls()[1])
    split = _split_note(active_up, foreign_up)
    if split:
        return {"ok": False, "started": False, "note": split}
    if active_up:
        return {"ok": True, "started": False, "note": "MCP 已连通"}
    launched = launch_editor()
    if not launched.get("ok"):
        return launched
    deadline = time.time() + _WAIT_SECONDS
    while time.time() < deadline:
        active_up = _probe(active)
        foreign_up = _probe(_mcp_urls()[1])
        split = _split_note(active_up, foreign_up)
        if split:
            return {"ok": False, "started": launched.get("started"), "note": split}
        if active_up:
            note = launched.get("note") or "MCP 已连通"
            return {"ok": True, "started": launched.get("started"), "note": note}
        time.sleep(2)
    return {"ok": False, "started": launched.get("started"), "note": "编辑器已启动，但 MCP 在 120 秒内没有连通"}
