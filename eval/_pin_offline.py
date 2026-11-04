# -*- coding: utf-8 -*-
"""离线检查的环境封闭。

断言钉的是「缺省行为」，不该随本机 .env 漂移。20261028 实证：本机把
UE_SOLVER_ENABLED=true 写进 .env 后，没封闭的检查（check_upgrade 的固定窗口
序列）多出一扇 solve 直接红了。凡物化 / 预演 / solve 扇相关的离线断言都该套
closed_env，与 check_solve_gap_fallback 的临时空 .env 同口径，只是收成一个工具。

用法：
    with closed_env():            # 全空 .env + 清掉进程环境变量里的相关键
        ...
    with closed_env(UE_SOLVER_ENABLED="true"):   # 显式盖某个值
        ...
"""
from __future__ import annotations

from contextlib import contextmanager

# 离线断言可能依赖的开关全集。进这里就保证：临时 .env 全空 + 进程环境变量清空，
# 再按调用方给的 values 显式盖。
_WATCHED = (
    "UE_SOLVER_ENABLED", "UE_SOLVER_NAMESPACES", "UE_SOLVER_EDITOR_CALLS",
    "UE_SOLVER_BUILD_ROUNDS", "UE_SOLVER_MODEL_CALLS", "UE_SOLVER_META_CALLS",
    "UE_ALLOW_PYTHON",
    "UE_EDITOR_BACKEND", "UE_ESCALATE_GAPS",
    "UE_ALLOW_BLUEPRINT", "UE_ALLOW_MATERIAL", "UE_ALLOW_UMG",
    "UE_ALLOW_ANIM", "UE_ALLOW_NIAGARA", "UE_ALLOW_AI", "UE_ALLOW_MONOLITH_WRITE",
    "UE_DEFAULT_SAVE", "UE_DEFAULT_PIE", "UE_EXEC_MODE",
)


@contextmanager
def closed_env(**values):
    import os
    import tempfile

    import config

    saved_path = config.ENV_PATH
    saved_vars = {key: os.environ.pop(key, None) for key in _WATCHED}
    # config._env_bool 只读 os.getenv（.env 的值靠 config import 时 load_dotenv 灌进来）。
    # 临时 .env 文件没人再去 load，所以显式给的值要同时写进 os.environ 才生效。
    added = {key: str(val) for key, val in values.items() if key in _WATCHED}
    os.environ.update(added)
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text(
            "".join(f"{key}={val}\n" for key, val in added.items()), encoding="utf-8")
        config.ENV_PATH = env_file
        try:
            yield
        finally:
            config.ENV_PATH = saved_path
            for key in added:
                os.environ.pop(key, None)
            for key, val in saved_vars.items():
                if val is not None:
                    os.environ[key] = val


def close_now() -> None:
    """进程级封闭：整个检查进程内 ENV_PATH 指到空临时 .env，并清掉 watched 键。
    给离线检查在 import 处调用（检查进程本就单用途，不需要恢复）。"""
    import os
    import tempfile

    import config
    from pathlib import Path

    for key in _WATCHED:
        os.environ.pop(key, None)
    env_file = Path(tempfile.gettempdir()) / f"ue-rag-offline-{os.getpid()}.env"
    env_file.write_text("", encoding="utf-8")
    config.ENV_PATH = env_file
