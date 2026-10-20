# agent/clarify_policy.py
"""澄清策略。推不出路径时只问一次 r，问法从策略库取。

路径仍要唯一。策略库缺条目时用内置问法，不猜目录。"""
import json
import os
from pathlib import Path

_DEFAULTS = {
    "asset_refPath": "只给一次：r /Game/某目录，或 r /Game/某目录/BP_名字。",
    "source_file": "只给一次：r Public/类名.h 或 r Private/类名.cpp。",
}


def policy_path() -> Path:
    override = os.environ.get("UE_CLARIFY_POLICY", "").strip()
    if override:
        return Path(override)
    routes_override = os.environ.get("UE_LEARNED_ROUTES", "").strip()
    if routes_override:
        return Path(routes_override).with_name("clarify_policy.json")
    from config import DATA_DIR

    return DATA_DIR / "skills" / "clarify_policy.json"


def load_policies(path: Path | None = None) -> list[dict]:
    target = Path(path) if path is not None else policy_path()
    if not target.is_file():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("policies") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _save(policies: list[dict], path: Path | None = None) -> None:
    target = Path(path) if path is not None else policy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"policies": policies}
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)


def slot_for(kind: str) -> str:
    if kind in ("source", "source_file"):
        return "source_file"
    return "asset_refPath"


def question_for(kind: str) -> str:
    slot = slot_for(kind)
    for policy in load_policies():
        if str(policy.get("missing_slot") or "") == slot:
            text = str(policy.get("question_template") or "").strip()
            if text and "只给一次" in text:
                return text
    return _DEFAULTS[slot]


def apply_note(kind: str, note: str) -> str:
    """把已有暂停句里的「只给一次」换成策略库问法。问法本身只出现一次。"""
    question = question_for(kind)
    text = str(note or "").strip()
    if not question:
        return text
    if question in text:
        return text
    if "只给一次" in text:
        head = text.split("只给一次", 1)[0].strip().rstrip("。")
        return (head + "。" + question) if head else question
    return (text + question) if text else question


def remember_reply(kind: str, supplied: str, *, source_run: str = "") -> dict | None:
    """人用 r 补过的格式记进接受形。不因此放宽唯一性。"""
    text = str(supplied or "").strip()
    if not text:
        return None
    slot = slot_for(kind)
    policies = load_policies()
    found = None
    for item in policies:
        if str(item.get("missing_slot") or "") == slot:
            found = item
            break
    if found is None:
        found = {
            "missing_slot": slot,
            "question_template": question_for(kind),
            "accepted_forms": [],
            "resolution": "路径唯一才用，不猜",
            "confidence": 0.5,
            "source_runs": [],
        }
        policies.append(found)
    forms = [str(item) for item in (found.get("accepted_forms") or [])]
    if text not in forms:
        forms.append(text)
    found["accepted_forms"] = forms[-20:]
    runs = [str(item) for item in (found.get("source_runs") or [])]
    if source_run and source_run not in runs:
        runs.append(source_run)
    found["source_runs"] = runs
    _save(policies)
    return found
