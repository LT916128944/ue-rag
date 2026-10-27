"""发给模型的消息按角色切开。更早的工具正文可以换成工具名和 sha1。"""
import hashlib
import json
import re

from agent.roles import tool_allowed

_TOOL_LIMIT = 6
# tool_router 超长 safe name：前 55 字 + "_" + 8 位 md5。这种尾部不是工具短名。
_TRUNCATED_SAFE = re.compile(r"^.{55}_[0-9a-f]{8}$")


def _short_name(call: dict) -> str:
    """模型看到的是 safe name（点变成双下划线）。裁剪必须还原成 allow_writes 里的短名。"""
    name = str((call.get("function") or {}).get("name") or "")
    from core.tool_router import get_full_name, split_full_name

    full = get_full_name(name)
    if full:
        _, short = split_full_name(full)
        return short
    if "__" in name and not _TRUNCATED_SAFE.match(name):
        return name.split("__")[-1]
    return name.rsplit(".", 1)[-1]


def _payload_ok(content: str) -> bool:
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("ok") is True


def _tool_pairs(messages: list) -> list[tuple[int, int, dict, dict, str]]:
    """(assistant_index, tool_index, call, tool_msg, short_name)，按出现顺序。

    一次 assistant 可以带多个 tool_calls，后面连续的 tool 消息都要配上。
    配完第一条就丢掉 pending 时，第二条结果不会进入下一轮提示。
    """
    pairs = []
    pending = None
    pending_at = None
    seen: set = set()
    for index, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            pending = msg
            pending_at = index
            seen = set()
            continue
        if msg.get("role") != "tool" or pending is None:
            continue
        call_id = msg.get("tool_call_id")
        if call_id in seen:
            continue
        call = next((item for item in pending["tool_calls"] if item.get("id") == call_id), None)
        if not call:
            continue
        seen.add(call_id)
        pairs.append((pending_at, index, call, msg, _short_name(call)))
        if seen.issuperset({item.get("id") for item in pending["tool_calls"]}):
            pending = None
            pending_at = None
    return pairs


def _success_diff_index(pairs: list[tuple]) -> int | None:
    found = None
    for _assistant_at, tool_at, _call, tool_msg, name in pairs:
        if name == "apply_unified_diff" and _payload_ok(str(tool_msg.get("content") or "")):
            found = tool_at
    return found


def compact_tool_bodies(messages: list, limit: int = _TOOL_LIMIT) -> list:
    """checkpoint 里更早的工具正文换成工具名 + sha1。最近若干条和最近成功 diff 保留原文。"""
    pairs = _tool_pairs(messages)
    if not pairs:
        return list(messages)
    recent = {tool_at for _a, tool_at, _c, _m, _n in pairs[-limit:]}
    pinned = _success_diff_index(pairs)
    out = []
    stub_at = {}
    for assistant_at, tool_at, _call, tool_msg, name in pairs:
        if tool_at in recent or tool_at == pinned:
            continue
        content = str(tool_msg.get("content") or "")
        if content.startswith("tool=") and "可再读" in content:
            continue
        digest = hashlib.sha1(content.encode("utf-8")).hexdigest()
        stub_at[tool_at] = f"tool={name} sha1={digest} 可再读"
        del assistant_at
    if not stub_at:
        return list(messages)
    for index, msg in enumerate(messages):
        if index in stub_at and isinstance(msg, dict):
            cloned = dict(msg)
            cloned["content"] = stub_at[index]
            out.append(cloned)
        else:
            out.append(msg)
    return out


def _allowed_pairs(messages: list, task: dict | None) -> list[tuple[dict, dict, dict]]:
    allowed = []
    for assistant_at, _tool_at, call, tool_msg, name in _tool_pairs(messages):
        if tool_allowed(name, task):
            allowed.append((messages[assistant_at], call, tool_msg))
    return allowed


def goal_anchor() -> str:
    """progress.json 的目标锚点。压缩工具正文不影响它：每次切片都重新注入。"""
    try:
        from agent.harness import load_goal

        return load_goal()
    except Exception:
        return ""


def visible_messages(messages: list, task: dict | None, limit: int = _TOOL_LIMIT) -> list:
    """原始用户句、目标锚点、最近一条裁判失败、当前角色最近若干条工具结果。

    最近一条成功的 apply_unified_diff 不受条数上限排挤，正文保留。
    goal 来自 progress.json，每次重新读取，裁剪不会丢掉目标。
    """
    users = [msg for msg in messages if isinstance(msg, dict) and msg.get("role") == "user"]
    original = users[0] if users else {"role": "user", "content": ""}
    fixes = [
        msg for msg in users
        if str(msg.get("content") or "").startswith("裁判失败")
    ]
    latest_fix = fixes[-1] if fixes else None

    allowed = _allowed_pairs(messages, task)
    recent = allowed[-limit:] if allowed else []
    pinned = None
    for assistant, call, tool_msg in allowed:
        if _short_name(call) == "apply_unified_diff" and _payload_ok(str(tool_msg.get("content") or "")):
            pinned = (assistant, call, tool_msg)
    selected = list(recent)
    if pinned is not None and pinned not in selected:
        selected = [pair for pair in allowed if pair == pinned or pair in recent]

    visible = []
    goal = goal_anchor()
    if goal:
        visible.append({"role": "user", "content": f"目标：{goal}"})
    visible.append(original)
    if latest_fix is not None and latest_fix is not original:
        visible.append(latest_fix)
    # 同一次模型回复的多个 tool_calls 要留在同一条 assistant 上，并带回 reasoning_content。
    # 拆开后思考模式会 400：reasoning_content must be passed back。
    groups: list[tuple[dict, list]] = []
    for assistant, call, tool_msg in selected:
        if groups and groups[-1][0] is assistant:
            groups[-1][1].append((call, tool_msg))
        else:
            groups.append((assistant, [(call, tool_msg)]))
    for assistant, pairs in groups:
        payload = {
            "role": "assistant",
            "content": assistant.get("content") or "",
            "tool_calls": [call for call, _tool_msg in pairs],
        }
        reasoning = assistant.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            payload["reasoning_content"] = reasoning
        visible.append(payload)
        for _call, tool_msg in pairs:
            visible.append(tool_msg)
    return visible


def slice_counts(messages: list, task: dict | None, limit: int = _TOOL_LIMIT) -> dict:
    """只记计数，不记工具正文。"""
    visible = visible_messages(messages, task, limit=limit)
    user = 0
    judge_fail = 0
    tool_kept = 0
    kept_ids = set()
    for msg in visible:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            user += 1
            if str(msg.get("content") or "").startswith("裁判失败"):
                judge_fail += 1
        elif msg.get("role") == "tool":
            tool_kept += 1
            if msg.get("tool_call_id"):
                kept_ids.add(msg.get("tool_call_id"))
    allowed = _allowed_pairs(messages, task)
    tool_dropped = 0
    for _assistant, call, _tool_msg in allowed:
        if call.get("id") not in kept_ids:
            tool_dropped += 1
    return {
        "user": user,
        "judge_fail": judge_fail,
        "tool_kept": tool_kept,
        "tool_dropped": tool_dropped,
    }
