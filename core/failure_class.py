"""失败分类的固定枚举。trace 写 code，报告仍显示中文。

旧 trace 没有 failure_class 时，按 judge_kind / error 归类，规则与原来的 classify_row 相同。
"""

FAILURE_CLASSES = (
    ("compile", "编译"),
    ("test", "测试"),
    ("tool_refused", "工具拒绝"),
    ("mcp_down", "MCP 连不上"),
    ("retry_exceeded", "超过重试"),
    ("level_precheck", "关卡预检"),
    ("other", "其他"),
)

_LABEL = {code: label for code, label in FAILURE_CLASSES}
FAILURE_CODES = tuple(_LABEL)


def _classify(row: dict) -> str:
    error = str(row.get("error") or "")
    kind = str(row.get("judge_kind") or "")
    node = str(row.get("node") or "")
    if node == "precheck" or "请打开关卡" in error:
        return "level_precheck"
    if any(token in error for token in (
        "WinError 10061", "10061", "拒绝连接", "Connection refused",
        "Bad Gateway", "ConnectError", "ConnectTimeout", "ReadTimeout",
    )):
        return "mcp_down"
    if any(token in error for token in ("write refused", "role refused", "blueprint edit disabled")):
        return "tool_refused"
    if kind in ("cpp_build", "blueprint_compile") or "error C" in error:
        return "compile"
    if kind in ("editor_assert", "automation") or error.startswith("failed="):
        return "test"
    try:
        retry = int(row.get("retry_count") or 0)
    except (TypeError, ValueError):
        retry = 0
    if retry >= 3:
        return "retry_exceeded"
    return "other"


def failure_code(row: dict) -> str | None:
    """成功行返回 None。已写入的合法枚举优先于原文。"""
    if row.get("ok") is not False:
        return None
    stored = str(row.get("failure_class") or "")
    if stored in _LABEL:
        return stored
    return _classify(row)


def failure_label(row: dict) -> str | None:
    code = failure_code(row)
    if code is None:
        return None
    return _LABEL[code]


def stamp_failure(row: dict) -> dict:
    """失败行补上 failure_class。已有合法枚举则不改。"""
    if row.get("ok") is not False:
        return row
    stored = str(row.get("failure_class") or "")
    if stored not in _LABEL:
        row["failure_class"] = _classify(row)
    return row
