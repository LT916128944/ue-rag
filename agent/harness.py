# agent/harness.py
"""Harness 决策字段、验收合同、交接文件。

不调用模型，不改图，不保存关卡，不 PIE。合同的 passed 只由对得上的裁判改写。
"""
import json
import os
import re
from datetime import date
from pathlib import Path

EVIDENCE_KINDS = ("cpp_build", "blueprint_compile", "editor_assert", "automation", "manual")
STOP_REASONS = ("ok", "paused", "retry_exceeded", "read_stall", "judge_failed", "unresolved")
PATROL_FILTER = "AIAgent.Enemy.Patrol"
PLATFORM_FILTER = "AIAgent.Platform"
_CLIP = 240
_ENEMY_RE = re.compile(r"敌人|Enemy|TestEnemy", re.I)
_COIN_RE = re.compile(r"金币|coin", re.I)
_PLATFORM_RE = re.compile(r"平台跳跃|平台關卡|关卡.*金币|金币.*(倒计时|收齐|通关|计数)", re.I)
PLATFORM_MANUAL_TEXT = "HUD（左上角计数、90 秒倒计时、通关/失败显示）由人按 Plan/ 人工步骤文档做"

_KIND_BY_ACTION = {
    "compile_project": "cpp_build",
    "edit_cpp": "cpp_build",
    "compile_blueprint": "blueprint_compile",
    "move_actor": "editor_assert",
    "judge_pickup": "editor_assert",
    "check_pickup": "editor_assert",
    "set_pickable": "editor_assert",
    "place_actor": "editor_assert",
    "place_blueprint": "editor_assert",
    "query_actor": "editor_assert",
    "run_automation_test": "automation",
    "set_scalar_param": "blueprint_compile",
    "query_material": "editor_assert",
    "place_batch": "editor_assert",
}

_KIND_TEXT = {
    "cpp_build": "C++ 编译通过，退出码 0 且没有 error C",
    "blueprint_compile": "已点名的蓝图能编过",
    "editor_assert": "编辑器读数与期望一致",
    "automation": "Automation 过滤器失败数为 0",
    "manual": "行为树、动画、受击由人在 Plan/ 里绑",
}


def clip(text, limit: int = _CLIP) -> str:
    raw = str(text or "").replace("\n", " ").strip()
    if "read_source" in raw:
        return ""
    return raw[:limit]


def policy_snapshot(task: dict | None) -> dict:
    """拒绝时记下的当前开关。不发明新的策略语言。"""
    from config import allow_anim, allow_blueprint, allow_material, allow_niagara, allow_umg

    task = task or {}
    allow = task.get("allow_writes") or []
    return {
        "save": bool(task.get("save")),
        "pie": bool(task.get("pie")),
        "allow_writes": [str(item) for item in allow],
        "UE_ALLOW_BLUEPRINT": bool(allow_blueprint()),
        "UE_ALLOW_MATERIAL": bool(allow_material()),
        "UE_ALLOW_UMG": bool(allow_umg()),
        "UE_ALLOW_ANIM": bool(allow_anim()),
        "UE_ALLOW_NIAGARA": bool(allow_niagara()),
    }


def wants_patrol(query: str) -> bool:
    text = query or ""
    return "巡逻" in text and bool(_ENEMY_RE.search(text))


def wants_platform(query: str) -> bool:
    text = query or ""
    if "平台跳跃" in text:
        return True
    return bool(_COIN_RE.search(text)) and bool(_PLATFORM_RE.search(text))


def _ensure_automation_window(windows: list, *, want: bool, flt: str, title: str) -> list:
    """命中语义就补一只读 Automation 扇。不因为这一扇去开 PIE。"""
    if not want:
        return list(windows or [])
    windows = [dict(item) for item in (windows or []) if isinstance(item, dict)]
    for window in windows:
        if str(window.get("action") or "") == "run_automation_test":
            window["automation_filter"] = str(window.get("automation_filter") or flt)
            slots = dict(window.get("slots") or {})
            slots.setdefault("filter", window["automation_filter"])
            window["slots"] = slots
            window["pie"] = False
            window["save"] = False
            return windows
    dep = [str(windows[-1]["id"])] if windows else []
    wid = f"W{len(windows) + 1}"
    windows.append({
        "id": wid,
        "title": title,
        "action": "run_automation_test",
        "slots": {"filter": flt},
        "automation_filter": flt,
        "depends_on": dep,
        "max_retries": 1,
        "realizable": True,
        "ok": True,
        "blocked": False,
        "save": False,
        "pie": False,
    })
    return windows


def ensure_platform_window(query: str, windows: list) -> list:
    """平台跳跃语义时追加 AIAgent.Platform 玩法 Automation。不因为这一扇去开 PIE。"""
    return _ensure_automation_window(
        windows, want=wants_platform(query), flt=PLATFORM_FILTER, title="平台玩法 Automation")


def ensure_patrol_window(query: str, windows: list) -> list:
    """巡逻且点名了敌人时追加只读 Automation。不因为这一扇去开 PIE。"""
    return _ensure_automation_window(
        windows, want=wants_patrol(query), flt=PATROL_FILTER, title="巡逻 Automation")


def build_criteria(query: str, windows: list | None) -> list[dict]:
    """每条从 passed: false 起。manual 留给 Plan/，模型文本改不了。"""
    from config import allow_umg

    criteria: list[dict] = []
    seen: set[str] = set()
    for window in windows or []:
        if not isinstance(window, dict):
            continue
        action = str(window.get("action") or "")
        kind = _KIND_BY_ACTION.get(action)
        if not kind or kind in seen or kind not in EVIDENCE_KINDS:
            continue
        seen.add(kind)
        item = {
            "id": f"c-{kind}",
            "text": _KIND_TEXT[kind],
            "evidence_kind": kind,
            "passed": False,
        }
        if kind == "automation":
            slots = window.get("slots") if isinstance(window.get("slots"), dict) else {}
            item["filter"] = str(window.get("automation_filter") or slots.get("filter") or "")
            if wants_patrol(query) and not item["filter"]:
                item["filter"] = PATROL_FILTER
        criteria.append(item)
    if wants_patrol(query) and "automation" not in seen:
        criteria.append({
            "id": "c-automation",
            "text": "巡逻读数：相对出生点有位移、靠近后距离缩小、距离内 OnMeleeAttack",
            "evidence_kind": "automation",
            "filter": PATROL_FILTER,
            "passed": False,
        })
        seen.add("automation")
    if wants_platform(query) and "automation" not in seen:
        criteria.append({
            "id": "c-automation",
            "text": "平台玩法读数：金币计数递增、收齐后门开、倒计时超时走失败分支",
            "evidence_kind": "automation",
            "filter": PLATFORM_FILTER,
            "passed": False,
        })
        seen.add("automation")
    if wants_platform(query) and "manual" not in seen and not allow_umg():
        criteria.append({
            "id": "c-manual",
            "text": PLATFORM_MANUAL_TEXT,
            "evidence_kind": "manual",
            "passed": False,
        })
        seen.add("manual")
    if wants_patrol(query) and "manual" not in seen:
        criteria.append({
            "id": "c-manual",
            "text": _KIND_TEXT["manual"],
            "evidence_kind": "manual",
            "passed": False,
        })
    if not criteria and re.search(r"编译工程|编译项目|编译一下工程|编译代码", query or ""):
        criteria.append({
            "id": "c-cpp_build",
            "text": _KIND_TEXT["cpp_build"],
            "evidence_kind": "cpp_build",
            "passed": False,
        })
    return criteria


def apply_judge(criteria: list | None, judge: dict | None) -> list[dict]:
    """只有 judge.ok 且 kind 等于 evidence_kind 才改成通过。manual 保持 false。"""
    out = [dict(item) for item in (criteria or []) if isinstance(item, dict)]
    if not isinstance(judge, dict) or judge.get("ok") is not True:
        return out
    kind = str(judge.get("kind") or "")
    if kind not in ("cpp_build", "blueprint_compile", "editor_assert", "automation"):
        return out
    for item in out:
        if str(item.get("evidence_kind") or "") == "manual":
            continue
        if str(item.get("evidence_kind") or "") == kind:
            item["passed"] = True
    return out


def ignore_model_claim(criteria: list | None, text: str = "") -> list[dict]:
    """模型说「已完成」不改合同。text 只为了调用点显式带上这句话。"""
    del text
    return [dict(item) for item in (criteria or []) if isinstance(item, dict)]


def apply_judges(criteria: list | None, *judges) -> list[dict]:
    current = [dict(item) for item in (criteria or []) if isinstance(item, dict)]
    for judge in judges:
        if isinstance(judge, dict):
            current = apply_judge(current, judge)
    return current


def contract_status(criteria: list | None, status: str) -> str:
    """自动条款都过了、还有 manual 时，整句记部分完成。别的状态保持原样。"""
    if status != "成功":
        return status
    rows = [item for item in (criteria or []) if isinstance(item, dict)]
    manuals = [item for item in rows if item.get("evidence_kind") == "manual"]
    autos = [item for item in rows if item.get("evidence_kind") != "manual"]
    if not manuals:
        return status
    if autos and not all(item.get("passed") for item in autos):
        return status
    if any(not item.get("passed") for item in manuals):
        return "部分完成"
    return status


def classify_stop(*, ok: bool = False, paused: bool = False, retries: int = 0,
                  max_retries: int = 0, summary: str = "", error: str = "",
                  unresolved=None) -> str:
    if unresolved:
        return "unresolved"
    text = f"{summary or ''}\n{error or ''}"
    if any(token in text for token in ("unresolved", "没着落", "推不出")) and (paused or not ok):
        return "unresolved"
    if ok and not paused:
        return "ok"
    if "打转" in text or "read_stall" in text:
        return "read_stall"
    if paused:
        return "paused"
    try:
        if int(max_retries) > 0 and int(retries) >= int(max_retries):
            return "retry_exceeded"
    except (TypeError, ValueError):
        pass
    return "judge_failed"


def run_stop(reasons: list, ok: bool) -> str:
    found = {str(item) for item in reasons if item}
    for name in ("unresolved", "read_stall", "retry_exceeded", "judge_failed", "paused"):
        if name in found:
            return name
    return "ok" if ok else "judge_failed"


def handoff_root() -> Path:
    override = os.environ.get("UE_HANDOFF_DIR", "").strip()
    if override:
        return Path(override)
    from config import DATA_DIR

    return DATA_DIR / "handoffs"


def load_goal() -> str:
    """progress.json 的 goal。没有文件或字段就返回空串，不编一句新目标。"""
    try:
        from config import DATA_DIR

        data = json.loads((DATA_DIR / "harness" / "progress.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str((data or {}).get("goal") or "")


def _handoff_path(run_id: str, window_id: str, root: Path | None = None) -> Path:
    return (root or handoff_root()) / str(run_id) / f"{window_id}.json"


def unfinished_titles(directory: Path | None = None, day: str | None = None) -> list[str]:
    """当天任务记录已经存在才读未完成标题。没有文件就不编。"""
    if directory is None:
        from config import UE_PROJECT_DIR

        directory = UE_PROJECT_DIR / ".cursor" / "memory" / "任务记录"
    path = Path(directory) / f"{day or date.today().isoformat()}.md"
    if not path.is_file():
        return []
    titles = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## ") and "未完成" in stripped:
            titles.append(stripped[3:].strip())
        elif stripped.startswith("- [ ]"):
            titles.append(stripped[5:].strip())
    return [item for item in titles if item][:12]


def _short_artifacts(artifacts: dict | None) -> dict:
    out = {}
    for key, value in (artifacts or {}).items():
        if not isinstance(value, dict):
            continue
        out[str(key)] = {
            "ref": value.get("ref"),
            "in_level": value.get("in_level"),
        }
    return out


def build_handoff(*, did: str, judges: list | None = None, summary: str = "", error: str = "",
                  ok: bool = False, criteria: list | None = None, next_title: str = "",
                  artifacts: dict | None = None, record_dir: Path | None = None,
                  day: str | None = None, goal: str | None = None) -> dict:
    """用裁判和窗口结果拼。不收工具正文。goal 缺省取 progress.json，没有就空串。"""
    evidence = []
    for judge in judges or []:
        if not isinstance(judge, dict) or not judge.get("kind"):
            continue
        evidence.append({
            "kind": str(judge.get("kind")),
            "ok": bool(judge.get("ok")),
            "summary": clip(judge.get("summary") or ""),
        })
    unmet = []
    if not ok:
        text = clip(error or summary)
        if text:
            unmet.append(text)
    for item in criteria or []:
        if not isinstance(item, dict) or item.get("passed"):
            continue
        label = clip(item.get("text") or item.get("evidence_kind") or "")
        if label and label not in unmet:
            unmet.append(label)
    nxt = [clip(next_title)] if next_title else []
    for title in unfinished_titles(record_dir, day):
        if title not in nxt:
            nxt.append(title)
    return {
        "did": clip(did, 200),
        "evidence": evidence,
        "unmet": unmet[:8],
        "next": "；".join(item for item in nxt if item)[:_CLIP],
        "artifacts": _short_artifacts(artifacts),
        "goal": clip(goal if goal is not None else load_goal()),
    }


def write_handoff(run_id: str, window_id: str, payload: dict, root: Path | None = None) -> Path:
    path = _handoff_path(run_id, window_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_handoff(run_id: str, window_id: str, root: Path | None = None) -> dict | None:
    path = _handoff_path(run_id, window_id, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def handoff_note(run_id: str, window_ids: list | None, root: Path | None = None) -> str:
    """下一扇用户消息里的交接。文件不在就返回空串，续接仍走 checkpoint。"""
    parts = []
    for wid in window_ids or []:
        doc = read_handoff(run_id, str(wid), root=root)
        if not doc:
            continue
        brief = {
            "window": str(wid),
            "unmet": list(doc.get("unmet") or []),
            "evidence": [
                {"kind": item.get("kind"), "ok": item.get("ok")}
                for item in (doc.get("evidence") or [])
                if isinstance(item, dict)
            ],
            "next": doc.get("next") or "",
        }
        parts.append(json.dumps(brief, ensure_ascii=False))
    if not parts:
        return ""
    return "上一扇交接：\n" + "\n".join(parts)
