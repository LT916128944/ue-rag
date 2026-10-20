# core/risk.py
"""风险矩阵与执行前裁决。矩阵里的名字分级；未登记的名字不当 high 拒掉。"""
from __future__ import annotations

import json

from config import DATA_DIR
from core import fault

MATRIX_PATH = DATA_DIR / "harness" / "risk_matrix.json"

# graph_write 在矩阵里是一个名字，实际展开成这些蓝图写工具。
GRAPH_WRITE_TOOLS = frozenset({
    "write_graph_dsl",
    "create_node",
    "connect_pins",
    "add_event",
    "add_variable",
    "set_pin_value",
})


def _load_matrix() -> dict[str, list[str]]:
    try:
        data = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): [str(item) for item in value] for key, value in data.items() if isinstance(value, list)}


def tier(name: str) -> str:
    """矩阵里的名字返回 low/mid/high；未登记返回空串。"""
    key = str(name or "")
    if not key:
        return ""
    for level, names in _load_matrix().items():
        if key in names:
            return level
        if "graph_write" in names and key in GRAPH_WRITE_TOOLS:
            return level
    return ""


def decide(name: str, task: dict | None = None) -> dict:
    """执行前裁决。熔断打开才在这里拒绝；其余放行，拒绝留给现有逻辑。"""
    level = tier(name)
    if fault.circuit_open(name):
        return {"allow": False, "reason": f"circuit open：{name} 连续失败熔断中，冷却后自动恢复", "tier": level}
    return {"allow": True, "reason": "", "tier": level}
