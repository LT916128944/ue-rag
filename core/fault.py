# core/fault.py
"""故障分类的重试语义与同工具熔断。阈值只住在这里，别处不另写一套。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from config import DATA_DIR

# 参数类错误不重试；只有 MCP 连不上值得再试一次。
RETRYABLE = {
    "compile": False,
    "test": False,
    "tool_refused": False,
    "level_precheck": False,
    "mcp_down": True,
    "retry_exceeded": False,
    "other": False,
}
MAX_MCP_RETRIES = 1

# 熔断：同一 tool 短名连续失败 CIRCUIT_THRESHOLD 次打开，冷却 CIRCUIT_COOLDOWN_S 秒。
CIRCUIT_THRESHOLD = 5
CIRCUIT_COOLDOWN_S = 30.0

_consecutive: dict[str, int] = {}


def retryable(failure_class: str) -> bool:
    return bool(RETRYABLE.get(str(failure_class or ""), False))


def max_retries(failure_class: str) -> int:
    """这一类失败最多再试几次。参数类错误是 0。"""
    return MAX_MCP_RETRIES if retryable(failure_class) else 0


def circuit_path() -> Path:
    override = os.getenv("UE_CIRCUIT_PATH")
    if override:
        return Path(override)
    return DATA_DIR / "harness" / "circuit.json"


def record_failure(tool: str) -> int:
    """记一次同短名失败。达到阈值就打开熔断并写 circuit.json。"""
    name = str(tool or "")
    count = _consecutive.get(name, 0) + 1
    _consecutive[name] = count
    if count >= CIRCUIT_THRESHOLD:
        _open_circuit(name)
    return count


def record_success(tool: str) -> None:
    _consecutive.pop(str(tool or ""), None)


def _open_circuit(tool: str) -> None:
    payload = {"tool": tool, "opened_until": time.time() + CIRCUIT_COOLDOWN_S}
    path = circuit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def circuit_opened_until(tool: str) -> float:
    """读 circuit.json。过期靠 opened_until 判断，条目不删。"""
    try:
        data = json.loads(circuit_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.0
    if not isinstance(data, dict) or data.get("tool") != str(tool or ""):
        return 0.0
    try:
        return float(data.get("opened_until") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def circuit_open(tool: str) -> bool:
    return circuit_opened_until(tool) > time.time()
