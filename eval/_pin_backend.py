# -*- coding: utf-8 -*-
"""离线检查里把编辑器后端钉成指定值。

语义断言（write refused / blueprint edit disabled）钉的是官方路径的拒绝文案，
不该随本机 .env 的 UE_EDITOR_BACKEND 漂移。2026-10-26 切 monolith 后，
凡是直接调 core.editor_exec.exec_tool 的离线检查都要显式钉住，不依赖 .env。
"""
from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def pinned_backend(name: str):
    import config
    from backends import select

    original = config.editor_backend
    select.reset_lock()
    config.editor_backend = lambda: name
    try:
        yield
    finally:
        config.editor_backend = original
        select.reset_lock()
