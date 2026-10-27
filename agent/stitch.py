# agent/stitch.py
"""同一 run 里把中断接上。脚本先分类；对不上才问一次模型。

已完成的窗口保持 done。模型只许补表内的一扇，不能把条款说成通过。
"""
import json
import re

from agent.catalog import DISPOSE_ACTION, INTEGRATE_ACTION, allows, iter_prompt_specs, spec_of

_COMPILE_ACTIONS = ("compile_project", "edit_cpp")
_CONNECT = ("10061", "拒绝连接", "ConnectError", "积极拒绝", "Connection refused")
_LEVEL = re.compile(r"L_[A-Za-z0-9_]+")


def apply_stitch(state, query: str, complete=None) -> bool:
    """改了队列或合同就返回 True。调用方应先消化这次改动，再派窗口。"""
    notes: list[str] = []
    changed = False
    if _drop_or_compile(state, query, notes):
        changed = True
    if _requeue_connection(state, notes):
        changed = True
    elif _ask_model(state, query, notes, complete):
        changed = True
    if changed:
        state.data["stitch_note"] = "；".join(notes)
        state.save()
    return changed


def _owners(item: dict) -> list[str] | None:
    windows = item.get("windows")
    if not isinstance(windows, list):
        return None
    return [str(one) for one in windows if str(one).strip()]


def _criteria(state) -> list[dict]:
    return [item for item in (state.data.get("criteria") or []) if isinstance(item, dict)]


def _actions(state) -> list[str]:
    return [str(entry.get("action") or "") for entry in state.entries()]


def _drop_or_compile(state, query: str, notes: list[str]) -> bool:
    """没有窗口能证明的条款拿掉。原句点了编译却没排编译扇时，补一扇并重新绑定。"""
    if state.data.get("stitch_script"):
        return False
    pending = []
    for item in _criteria(state):
        if item.get("passed") or str(item.get("evidence_kind") or "") == "manual":
            continue
        owners = _owners(item)
        if owners is None or owners:
            continue
        pending.append(item)
    state.data["stitch_script"] = True
    if not pending:
        return False
    compile_needed = "编译" in (query or "") and not any(action in _COMPILE_ACTIONS for action in _actions(state))
    compile_rows = [item for item in pending if str(item.get("evidence_kind") or "") == "cpp_build"]
    drop = pending
    inserted = ""
    if compile_needed and compile_rows:
        inserted = _insert_window(state, "compile_project", {}, "编译工程")
        if inserted:
            drop = [item for item in pending if item not in compile_rows]
            for item in compile_rows:
                item["windows"] = [inserted]
                _sync_scope_criterion(state, item)
            notes.append(f"原句要求编译，补上 {inserted}")
    dropped = [str(item.get("id") or "") for item in drop]
    if dropped:
        state.data["criteria"] = [item for item in _criteria(state) if str(item.get("id") or "") not in dropped]
        _drop_scope_criteria(state, dropped)
        notes.append("拿掉没有窗口能证明的条款：" + "、".join(dropped))
    return bool(inserted or dropped)


def _requeue_connection(state, notes: list[str]) -> bool:
    """端口拒绝只重开那一扇，不重拆整句。每扇只重排一次。"""
    changed = False
    for entry in state.entries():
        if str(entry.get("action") or "") in (INTEGRATE_ACTION, DISPOSE_ACTION):
            continue
        if entry.get("state") != "failed" or entry.get("stitch_connect"):
            continue
        text = str(entry.get("summary") or entry.get("error") or "")
        if not any(token in text for token in _CONNECT):
            continue
        _requeue(entry)
        entry["stitch_connect"] = 1
        notes.append(f"{entry.get('id')} 断线，只重跑这一扇")
        changed = True
    if changed:
        _requeue_integrate(state)
    return changed


def _ask_model(state, query: str, notes: list[str], complete) -> bool:
    """脚本没接住的失败扇，用已有上下文问一次。表外动作和补不出的槽位都不插入。"""
    if state.data.get("stitch_model"):
        return False
    failed = [
        entry for entry in state.entries()
        if entry.get("state") == "failed" and str(entry.get("action") or "") not in (INTEGRATE_ACTION, DISPOSE_ACTION)
    ]
    if not failed:
        return False
    state.data["stitch_model"] = True
    action, unresolved, record = _complete_action(state, query, failed, complete)
    tokens = state.data.setdefault("stitch_tokens", {"prompt_tokens": 0, "completion_tokens": 0})
    tokens["prompt_tokens"] = int(tokens.get("prompt_tokens") or 0) + int(record.get("prompt_tokens") or 0)
    tokens["completion_tokens"] = int(tokens.get("completion_tokens") or 0) + int(record.get("completion_tokens") or 0)
    slots = _slots_for(action, query, _criteria(state)) if action else None
    if not action or slots is None:
        why = unresolved or (f"{action} 槽位补不出" if action else "模型没有给出表内动作")
        state.data["stitch_unresolved"] = why
        notes.append("模型补不上：" + why)
        return True
    existing = next((entry for entry in state.entries() if str(entry.get("action") or "") == action), None)
    if existing is not None and existing.get("state") == "failed" and not existing.get("stitch_model_requeue"):
        _requeue(existing)
        existing["stitch_model_requeue"] = 1
        notes.append(f"模型要求重跑已有的 {existing.get('id')}")
        _requeue_integrate(state)
        return True
    if existing is not None:
        state.data["stitch_unresolved"] = f"已有 {action}，不重复补"
        notes.append(state.data["stitch_unresolved"])
        return True
    inserted = _insert_window(state, action, slots, action)
    if not inserted:
        return True
    for entry in failed:
        if entry.get("stitch_model_requeue"):
            continue
        depends = [str(dep) for dep in (entry.get("depends_on") or [])]
        if inserted not in depends:
            depends.append(inserted)
        entry["depends_on"] = depends
        window = entry.get("window")
        if isinstance(window, dict):
            window["depends_on"] = list(depends)
        _requeue(entry)
        entry["stitch_model_requeue"] = 1
    _requeue_integrate(state)
    notes.append(f"模型补上一扇 {inserted} {action}")
    return True


def _complete_action(state, query: str, failed: list[dict], complete) -> tuple[str, str, dict]:
    messages = _model_messages(state, query, failed)
    record: dict = {}
    try:
        if complete is None:
            from core.llm_call import complete_logged

            response, record = complete_logged(messages, temperature=0, caller="stitch")
            if record.get("error"):
                return "", str(record["error"]), record
            text = ""
            if response is not None:
                text = response.choices[0].message.content or ""
        else:
            text = complete(messages)
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"[:200], record
    action, unresolved = _parse_action(text)
    if action and not allows(action):
        return "", unresolved or f"表外动作 {action}", record
    if action in (INTEGRATE_ACTION, DISPOSE_ACTION):
        return "", "集成和处置扇不能由模型补", record
    return action, unresolved, record


def _model_messages(state, query: str, failed: list[dict]) -> list[dict]:
    names = [name for name, _spec in iter_prompt_specs()]
    done = []
    for entry in state.entries():
        if entry.get("state") != "done":
            continue
        done.append({
            "id": entry.get("id"),
            "action": entry.get("action"),
            "summary": str(entry.get("summary") or "")[:120],
        })
    unmet = []
    for item in _criteria(state):
        if item.get("passed"):
            continue
        unmet.append({
            "id": item.get("id"),
            "evidence_kind": item.get("evidence_kind"),
            "text": str(item.get("text") or "")[:120],
        })
    broken = [{
        "id": entry.get("id"),
        "action": entry.get("action"),
        "summary": str(entry.get("summary") or entry.get("error") or "")[:160],
    } for entry in failed]
    payload = {"原句": str(query or "")[:300], "已完成": done[:6], "失败": broken[:4], "未通过条款": unmet[:6]}
    system = (
        "你只输出一个 JSON：{\"action\":\"动作表中的名字或空字符串\",\"unresolved\":\"补不上的原因\"}。"
        "action 只能取这些名字：" + "、".join(names) + "。"
        "不要解释，不要把条款标成通过，不要重拆已经完成的窗口。"
        "补不上就把 action 留空，原因写进 unresolved。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _parse_action(text: str) -> tuple[str, str]:
    match = re.search(r"\{.*\}", str(text or ""), re.S)
    if not match:
        return "", "没有 JSON"
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "", "JSON 解析失败"
    if not isinstance(data, dict):
        return "", "没有 JSON"
    return str(data.get("action") or "").strip(), str(data.get("unresolved") or "").strip()


def _slots_for(action: str, query: str, criteria: list[dict]) -> dict | None:
    spec = spec_of(action)
    if not isinstance(spec, dict):
        return None
    slots: dict = {}
    for name in spec.get("slots") or ():
        if name == "level":
            found = list(dict.fromkeys(_LEVEL.findall(query or "")))
            if len(found) != 1:
                return None
            slots["level"] = found[0]
        elif name == "filter":
            found = list(dict.fromkeys(
                str(item.get("filter") or "") for item in criteria
                if str(item.get("evidence_kind") or "") == "automation" and str(item.get("filter") or "").strip()
            ))
            if len(found) != 1:
                return None
            slots["filter"] = found[0]
        else:
            return None
    return slots


def _insert_window(state, action: str, slots: dict, title: str) -> str:
    existing = {str(entry.get("id") or "") for entry in state.entries()}
    number = 1
    while f"S{number}" in existing:
        number += 1
    ident = f"S{number}"
    window = {
        "id": ident,
        "title": title,
        "action": action,
        "slots": dict(slots),
        "depends_on": [],
        "max_retries": 1,
        "realizable": True,
    }
    state.data["windows"][ident] = state._entry(window)
    for entry in state.entries():
        if str(entry.get("action") or "") != "launch_editor" or entry.get("state") != "queued":
            continue
        if action != "compile_project":
            continue
        depends = [str(dep) for dep in (entry.get("depends_on") or [])]
        if ident not in depends:
            depends.append(ident)
        entry["depends_on"] = depends
        hosted = entry.get("window")
        if isinstance(hosted, dict):
            hosted["depends_on"] = list(depends)
    for entry in state.entries():
        if str(entry.get("action") or "") != INTEGRATE_ACTION:
            continue
        depends = [str(dep) for dep in (entry.get("depends_on") or [])]
        if ident not in depends:
            depends.append(ident)
        entry["depends_on"] = depends
        hosted = entry.get("window")
        if isinstance(hosted, dict):
            hosted["depends_on"] = list(depends)
    for scope in state.data.get("scopes") or []:
        if not isinstance(scope, dict):
            continue
        if str(scope.get("id") or "") == "INT" or str(scope.get("kind") or "") == "integration":
            continue
        ids = [str(item) for item in (scope.get("window_ids") or [])]
        if ident not in ids:
            ids.append(ident)
        scope["window_ids"] = ids
        break
    return ident


def _requeue(entry: dict) -> None:
    entry["state"] = "queued"
    entry["ok"] = None
    entry["result"] = None
    entry["summary"] = ""
    entry["error"] = ""


def _requeue_integrate(state) -> None:
    if state.data.get("stitch_integrate"):
        return
    for entry in state.entries():
        if str(entry.get("action") or "") != INTEGRATE_ACTION or entry.get("state") != "failed":
            continue
        _requeue(entry)
        state.data["stitch_integrate"] = True


def _drop_scope_criteria(state, dropped: list[str]) -> None:
    gone = set(dropped)
    for scope in state.data.get("scopes") or []:
        if not isinstance(scope, dict) or not isinstance(scope.get("criteria"), list):
            continue
        scope["criteria"] = [
            item for item in scope["criteria"]
            if not isinstance(item, dict) or str(item.get("id") or "") not in gone
        ]


def _sync_scope_criterion(state, item: dict) -> None:
    ident = str(item.get("id") or "")
    for scope in state.data.get("scopes") or []:
        if not isinstance(scope, dict):
            continue
        for other in scope.get("criteria") or []:
            if isinstance(other, dict) and str(other.get("id") or "") == ident:
                other["windows"] = list(item.get("windows") or [])
