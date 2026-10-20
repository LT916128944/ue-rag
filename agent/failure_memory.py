# agent/failure_memory.py
"""失败模式。只收裁判成功过的修复，标题或症状对不上就不用。

提升至少要两次成功。查不到不编造改法。fix 先问这里，再问 ue_error。"""
import json
import os
from pathlib import Path


def patterns_path() -> Path:
    override = os.environ.get("UE_FAILURE_PATTERNS", "").strip()
    if override:
        return Path(override)
    routes_override = os.environ.get("UE_LEARNED_ROUTES", "").strip()
    if routes_override:
        return Path(routes_override).with_name("failure_patterns.json")
    from config import DATA_DIR

    return DATA_DIR / "skills" / "failure_patterns.json"


def load_patterns(path: Path | None = None) -> list[dict]:
    target = Path(path) if path is not None else patterns_path()
    if not target.is_file():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("patterns") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _save(patterns: list[dict], path: Path | None = None) -> None:
    target = Path(path) if path is not None else patterns_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"patterns": patterns}
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)


def _symptom_hits(pattern: dict, message: str) -> bool:
    text = str(message or "")
    symptom = str(pattern.get("symptom") or "").strip()
    if not symptom or symptom not in text:
        return False
    detail = str(pattern.get("symptom_detail") or "").strip()
    if detail and detail not in text:
        return False
    return True


def lookup_pattern(message: str) -> dict | None:
    """症状对上、且已有至少两次成功，才返回。否则 None，不编造。"""
    if not str(message or "").strip():
        return None
    for pattern in load_patterns():
        if str(pattern.get("status") or "") != "promoted":
            continue
        try:
            successes = int(pattern.get("successes") or 0)
        except (TypeError, ValueError):
            successes = 0
        if successes < 2:
            continue
        if _symptom_hits(pattern, message):
            return pattern
    return None


def observe_success(pattern_id: str, *, source_run: str = "") -> dict | None:
    """记一次裁判成功。凑满两次才提升，失败轨迹不要调用这里。"""
    patterns = load_patterns()
    found = None
    for item in patterns:
        if str(item.get("id") or "") == pattern_id:
            found = item
            break
    if found is None:
        return None
    try:
        found["successes"] = int(found.get("successes") or 0) + 1
    except (TypeError, ValueError):
        found["successes"] = 1
    runs = [str(item) for item in (found.get("source_runs") or [])]
    if source_run and source_run not in runs:
        runs.append(source_run)
    found["source_runs"] = runs
    if int(found["successes"]) >= 2:
        found["status"] = "promoted"
        try:
            found["confidence"] = max(float(found.get("confidence") or 0), 0.8)
        except (TypeError, ValueError):
            found["confidence"] = 0.8
    else:
        found["status"] = "shadow"
    _save(patterns)
    return found


def add_shadow(pattern: dict) -> str | None:
    """蒸馏候选。successes 不到 2 时保持影子，fix 不会用它。"""
    if not isinstance(pattern, dict) or not str(pattern.get("id") or "").strip():
        return None
    if not str(pattern.get("symptom") or "").strip():
        return None
    pattern = dict(pattern)
    pattern.setdefault("successes", 0)
    pattern.setdefault("status", "shadow")
    pattern.setdefault("source_runs", [])
    pattern.setdefault("fix_tools", [])
    pattern.setdefault("conflicts", [])
    patterns = [item for item in load_patterns() if item.get("id") != pattern["id"]]
    patterns.append(pattern)
    _save(patterns)
    return str(pattern["id"])


def hint_text(pattern: dict) -> str:
    tools = "、".join(str(item) for item in (pattern.get("fix_tools") or []))
    cause = str(pattern.get("root_cause") or "").strip()
    validation = str(pattern.get("validation") or "").strip()
    parts = [str(pattern.get("id") or "")]
    if cause:
        parts.append(cause)
    if tools:
        parts.append("工具 " + tools)
    if validation:
        parts.append("验证 " + validation)
    return " ".join(part for part in parts if part)
