"""读一次 UE_EDITOR_BACKEND。选错入口直接拒绝，调用方不得改去调另一边。"""
from __future__ import annotations

import config

_LOCKED: str | None = None
ALLOWED = ("official", "monolith")


def reset_lock() -> None:
    """测试用。一次运行中间不重置。"""
    global _LOCKED
    _LOCKED = None


def lock_backend() -> str:
    """入口读一次。之后本进程不再跟 .env 改口。"""
    global _LOCKED
    if _LOCKED is None:
        _LOCKED = config.editor_backend()
    return _LOCKED


def current_backend() -> str:
    if _LOCKED is not None:
        return _LOCKED
    return config.editor_backend()


def backend_error(name: str) -> str:
    if name in ALLOWED:
        return ""
    return f"UE_EDITOR_BACKEND 只能是 official 或 monolith，当前是 {name!r}"


def official_refusal() -> str:
    """官方入口。monolith 或非法值都拒绝，原文带当前后端或两个允许值。"""
    name = current_backend()
    error = backend_error(name)
    if error:
        return error
    if name != "official":
        return "UE_EDITOR_BACKEND=monolith"
    return ""


def monolith_refusal() -> str:
    """Monolith 入口。official 或非法值都拒绝。"""
    name = current_backend()
    error = backend_error(name)
    if error:
        return error
    if name != "monolith":
        return "UE_EDITOR_BACKEND=official"
    return ""


def bind_run(emit=None) -> str:
    """运行入口锁后端，并写进该 run 的事件。"""
    name = lock_backend()
    if emit is not None:
        emit("editor_backend", backend=name)
    return name
