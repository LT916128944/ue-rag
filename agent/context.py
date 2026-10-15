"""发给模型的消息按角色切开。state['messages'] 仍保留全量。"""
from agent.roles import tool_allowed

_TOOL_LIMIT = 6


def _short_name(call: dict) -> str:
    name = str((call.get("function") or {}).get("name") or "")
    return name.rsplit(".", 1)[-1]


def visible_messages(messages: list, task: dict | None, limit: int = _TOOL_LIMIT) -> list:
    """原始用户句、最近一条裁判失败、当前角色允许的最近若干条工具结果。"""
    users = [msg for msg in messages if isinstance(msg, dict) and msg.get("role") == "user"]
    original = users[0] if users else {"role": "user", "content": ""}
    fixes = [
        msg for msg in users
        if str(msg.get("content") or "").startswith("裁判失败")
    ]
    latest_fix = fixes[-1] if fixes else None

    kept = []
    pending = None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            pending = msg
            continue
        if msg.get("role") != "tool" or not pending:
            continue
        call_id = msg.get("tool_call_id")
        call = next((item for item in pending["tool_calls"] if item.get("id") == call_id), None)
        if not call:
            continue
        if tool_allowed(_short_name(call), task):
            kept.append((call, msg))
    kept = kept[-limit:]

    visible = [original]
    if latest_fix is not None and latest_fix is not original:
        visible.append(latest_fix)
    for call, tool_msg in kept:
        visible.append({"role": "assistant", "content": "", "tool_calls": [call]})
        visible.append(tool_msg)
    return visible
