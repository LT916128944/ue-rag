# core/graph_write_policy.py
"""图写拦截的判定：蓝图/材质/UMG/动画/Niagara 写工具是否放行。

Cursor hook、Codex hook 与 DSH MCP 网关共用这一份实现，避免三处漂移。
读不到 .env 时一律按 false（fail-closed）。"""
from __future__ import annotations

import re
from pathlib import Path

from config import ENV_PATH as _ENV
from config import env_value, normalize_backend

BP_WRITES = {
    "write_graph_dsl",
    "create_node",
    "connect_pins",
    "add_event",
    "add_variable",
    "set_pin_value",
}
_READ_PREFIXES = ("get", "list", "find", "read", "search", "discover")
_DENY_BP = "蓝图改图已关闭。把连线步骤写入 Plan/，不要调用写图工具。"
_DOMAIN_KEYS = (
    ("material", "material", "UE_ALLOW_MATERIAL"),
    ("umg", "umg", "UE_ALLOW_UMG"),
    ("niagara", "niagara", "UE_ALLOW_NIAGARA"),
    ("animblueprint", "anim", "UE_ALLOW_ANIM"),
    ("animation", "anim", "UE_ALLOW_ANIM"),
    ("controlrig", "anim", "UE_ALLOW_ANIM"),
)


def editor_backend(env_path: Path | None = None) -> str:
    """钩子用：只读 .env 文件，不看进程环境变量，避免 import 时钉死。"""
    path = env_path if env_path is not None else _ENV
    return normalize_backend(env_value(path, "UE_EDITOR_BACKEND"))


def env_flag(env_path: Path, key: str) -> bool:
    if not env_path.is_file():
        return False
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, value = text.split("=", 1)
        if name.strip() != key:
            continue
        return value.strip().strip('"').strip("'").lower() in {"1", "true", "yes", "on"}
    return False


def blueprint_allowed(env_path: Path | None = None) -> bool:
    return env_flag(env_path if env_path is not None else _ENV, "UE_ALLOW_BLUEPRINT")


def domain_allows(env_path: Path | None = None) -> dict:
    path = env_path if env_path is not None else _ENV
    return {
        "material": env_flag(path, "UE_ALLOW_MATERIAL"),
        "umg": env_flag(path, "UE_ALLOW_UMG"),
        "anim": env_flag(path, "UE_ALLOW_ANIM"),
        "niagara": env_flag(path, "UE_ALLOW_NIAGARA"),
    }


def _flag(command: str, name: str) -> str:
    match = re.search(rf"--{name}(?:\s+|=)(\"[^\"]+\"|'[^']+'|\S+)", command)
    if not match:
        return ""
    return match.group(1).strip("\"'")


def _is_read(tool: str) -> bool:
    return tool.lower().startswith(_READ_PREFIXES)


def _domain_key(toolset: str) -> str:
    for token, _name, key in _DOMAIN_KEYS:
        if token in toolset:
            return key
    return ""


def decide(command: str, allow_bp: bool, allows: dict | None = None) -> dict:
    if "monolith_backend.py" in command and "--exec" in command:
        name = editor_backend()
        if name != "monolith":
            text = (
                f"UE_EDITOR_BACKEND 只能是 official 或 monolith，当前是 {name!r}"
                if name not in {"official", "monolith"}
                else "UE_EDITOR_BACKEND=official"
            )
            return {"permission": "deny", "user_message": text, "agent_message": text}
        from backends.policy import write_block

        blocked = write_block(_flag(command, "namespace"), _flag(command, "action"), {})
        if blocked:
            return {"permission": "deny", "user_message": blocked, "agent_message": blocked}
        return {"permission": "allow"}
    if "call_mcp_tool" not in command:
        return {"permission": "allow"}
    tool = _flag(command, "tool")
    toolset = _flag(command, "toolset").lower()
    allows = allows or {}
    domain_key = _domain_key(toolset)
    if domain_key and tool and not _is_read(tool):
        domain = next(name for token, name, key in _DOMAIN_KEYS if key == domain_key and token in toolset)
        if not allows.get(domain):
            text = f"{domain_key} 不是 true。写工具已关闭，步骤留在对话里。"
            return {"permission": "deny", "user_message": text, "agent_message": text}
    elif tool in BP_WRITES and not allow_bp:
        return {"permission": "deny", "user_message": _DENY_BP, "agent_message": _DENY_BP}
    return {"permission": "allow"}
