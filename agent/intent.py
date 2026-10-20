# agent/intent.py
"""L1 意图层（20261015-1705 P1）：句子 → 意图 JSON 的 schema 与代码验收。
模型只负责选目录内动作、填槽位名、把推不出的写进 unresolved；
路径、父类、refPath 一律由 L2 归地，裁判不由模型宣布成功。
schema 验收抄 validate_brief 的风格：表外动作拒收、未声明槽位丢弃并列进 dropped。"""
import json

from agent.catalog import CATALOG, INTEGRATE_ACTION, allows, note_candidate, spec_of

INTENT_SCHEMA = {
    "query": "原句",
    "goals": [{
        "action": "动作表里的动作名",
        "slots": {"槽位名": "原句里的原始 token（类名 / BP_ 名 / Actor 标签 / 坐标）"},
        "args": {"数量": 1, "位置": "贴地 / 错开已有的，不叠一起 等约束，只作记录"},
    }],
    "constraints": {"editor": False, "pie": False, "save": False},
    "unresolved": ["分类推不出的那半句"],
}

_INTENT_KEYS = ("query", "goals", "constraints", "unresolved")
_GOAL_KEYS = ("action", "slots", "args", "title")
_CONSTRAINT_KEYS = ("editor", "pie", "save")
_MAX_GOALS = 8
_MAX_ARGS = 6
_MAX_UNRESOLVED = 4


def intent_example_prompt() -> str:
    """给理解器系统提示的输出形状。与 INTENT_SCHEMA 同源。"""
    return json.dumps(INTENT_SCHEMA, ensure_ascii=False)


def _text(value, limit: int = 160) -> str:
    return str(value or "").strip()[:limit]


def validate_intent(data: dict, query: str) -> dict:
    """代码验收：未声明槽位、未知字段、超界约束一律丢弃并列进 dropped；
    表外动作的 goal 拒收。全部 goal 被拒时 ok=False，由调用方回退现有行为。"""
    dropped: list[str] = []
    intent: dict = {"query": _text(data.get("query") or query, 400), "ok": False}
    if not isinstance(data, dict):
        return intent
    unknown_top = [key for key in data if key not in _INTENT_KEYS]
    if unknown_top:
        dropped.append("意图丢弃未知字段：" + "、".join(sorted(set(map(str, unknown_top)))))

    goals: list[dict] = []
    candidates: list[str] = []
    for raw in data.get("goals") or []:
        if not isinstance(raw, dict):
            dropped.append("一条 goal 不是对象，已丢弃")
            continue
        action = _text(raw.get("action"), 80)
        if action == INTEGRATE_ACTION or not allows(action):
            dropped.append(f"表外动作 {action or '空'} 拒收")
            if action and action != INTEGRATE_ACTION and action not in CATALOG:
                try:
                    note_candidate(action, source="understand")
                except Exception:
                    pass
                if action not in candidates:
                    candidates.append(action)
            continue
        spec = spec_of(action)
        if not isinstance(spec, dict):
            dropped.append(f"表外动作 {action or '空'} 拒收")
            continue
        unknown_goal = [key for key in raw if key not in _GOAL_KEYS]
        if unknown_goal:
            dropped.append(f"goal {action} 丢弃未知字段：" + "、".join(sorted(set(map(str, unknown_goal)))))
        raw_slots = raw.get("slots") if isinstance(raw.get("slots"), dict) else {}
        slots: dict[str, str] = {}
        for key, value in raw_slots.items():
            if key not in spec["slots"]:
                dropped.append(f"goal {action} 未声明槽位 {key} 丢弃")
                continue
            slots[str(key)] = _text(value, 200)
        goal = {"action": action, "slots": slots, "args": {}}
        raw_args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
        for key, value in list(raw_args.items())[:_MAX_ARGS]:
            goal["args"][_text(key, 60)] = _text(value, 120)
        title = _text(raw.get("title"), 80)
        if title:
            goal["title"] = title
        if len(goals) < _MAX_GOALS:
            goals.append(goal)

    raw_constraints = data.get("constraints") if isinstance(data.get("constraints"), dict) else {}
    unknown_c = [key for key in raw_constraints if key not in _CONSTRAINT_KEYS]
    if unknown_c:
        dropped.append("constraints 丢弃未知字段：" + "、".join(sorted(set(map(str, unknown_c)))))
    constraints = {
        "editor": bool(raw_constraints.get("editor")),
        "pie": bool(raw_constraints.get("pie")),
        "save": False,  # 记录字段：默认不保存的开关不动，模型说了也不算
    }

    raw_unresolved = data.get("unresolved") if isinstance(data.get("unresolved"), list) else []
    unresolved = [_text(item, 160) for item in raw_unresolved if _text(item, 160)][:_MAX_UNRESOLVED]

    intent.update({"goals": goals, "constraints": constraints, "unresolved": unresolved})
    intent["dropped"] = dropped
    intent["candidates"] = candidates
    intent["ok"] = bool(goals)
    return intent


def intent_decision(intent: dict) -> dict:
    """给事件流看的决策字段：只留动作名、unresolved 和是否回写受限，不带原文。"""
    if not isinstance(intent, dict) or not intent.get("ok"):
        return {}
    return {
        "actions": [goal.get("action") or "" for goal in intent.get("goals") or []],
        "unresolved": list(intent.get("unresolved") or []),
    }