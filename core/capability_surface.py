# core/capability_surface.py
"""能力面文件的位置与后端指纹（20261029 边界整改）。

为什么放在 `core/`：这两件事原本各写一份，`pipelines/export_capability_surface.py`
有一份 `surface_path()` / `_backend_name()`，`agent/solver.py` 里又内联了一遍。
`agent/` 只能引用 `agent/config/core`（见 `documents/harness/boundaries.md`），
于是 solver 去 import pipelines 时 `eval/check_architecture.py` 就红。搬进 `core/`
是两边都允许的方向：pipelines 和 agent 都能引用 core，core 只引用 config 与标准库。

口径固定为「读 .env，其次进程环境变量，缺省 official」——与 `config.editor_backend()`
同一实现，不再各写一份 `.env` 解析，避免两份判断漂移。
"""
from __future__ import annotations

import os
from pathlib import Path


def backend_fingerprint() -> str:
    """当前后端名（小写，缺省 official）。与 `config.editor_backend()` 同一口径。"""
    from config import editor_backend

    return editor_backend()


def surface_path() -> Path:
    """能力面 JSON 的位置。`UE_CAPABILITY_SURFACE` 可覆盖（评测/fixture 用）。"""
    override = os.environ.get("UE_CAPABILITY_SURFACE", "").strip()
    if override:
        return Path(override)
    from config import DATA_DIR

    return DATA_DIR / "capability_surface.json"
