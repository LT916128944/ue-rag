# agent/catalog.py
"""规划者能用的动作。表外的 action 不执行。integrate_verify 与 dispose_residual 只由代码追加，规划者写不出。

20261028 P1-6：动作的单一声明源在 agent/actions.py。这里的 CATALOG / DIRECT_ACTIONS /
READ_ONLY_ACTIONS / GRAPH_ACTIONS / EDITOR_LOCK_ACTIONS / _VERIFY 全部由它推导——
加一个新动作只改 actions.py 一行，不再散改多处。"""
from pathlib import Path

from agent.actions import (
    ACTIONS as _ACTION_SPECS,
    DIRECT as _DIRECT_ROWS,
    DISPOSE_ACTION,
    EDITOR_LOCK as _EDITOR_LOCK_ROWS,
    ESTIMATES as _ESTIMATES,
    GRAPH as _GRAPH_ROWS,
    INTEGRATE_ACTION,
    READ_ONLY as _READ_ONLY_ROWS,
    VERIFY_BY_ACTION as _VERIFY_ROWS,
)

# CATALOG 的键序就是提示词里动作的出场序，actions.ACTIONS 按声明序生成，原样保留。
CATALOG: dict[str, dict] = dict(_ACTION_SPECS)

DIRECT_ACTIONS = frozenset(_DIRECT_ROWS)

READ_ONLY_ACTIONS = frozenset(_READ_ONLY_ROWS)

# 要改蓝图或控件图：monolith 下同一目标最多 3 次。compile_project / edit_cpp 不在这里。
BLUEPRINT_ATTEMPT_ACTIONS = frozenset({
    "compile_blueprint",
    "bind_blueprint",
    "create_widget_blueprint",
    "edit_widget_blueprint",
})

GRAPH_ACTIONS = frozenset(_GRAPH_ROWS)

EDITOR_LOCK_ACTIONS = frozenset(_EDITOR_LOCK_ROWS)

_VERIFY = {name: list(rows) for name, rows in _VERIFY_ROWS.items()}
_VERIFY[INTEGRATE_ACTION] = ["矩阵子判定"]
_VERIFY[DISPOSE_ACTION] = ["残差被同 kind 裁判通过，或归因标成 out_of_contract / equivalent"]

# 估时表（只进文档）也由 actions 推导；INTEGRATE 的区间在 actions.ESTIMATES 里。
_ACTION_ESTIMATE = dict(_ESTIMATES)


def action_meta(name: str) -> dict:
    """内置动作的版本、冲突和验证。候选动作不在这里。"""
    return {
        "version": 1,
        "conflicts": [],
        "verification": list(_VERIFY.get(name) or []),
        "status": "approved",
    }


def _skills_dir_file(filename: str, env_key: str) -> Path:
    import os
    from pathlib import Path

    override = os.environ.get(env_key, "").strip()
    if override:
        return Path(override)
    routes = os.environ.get("UE_LEARNED_ROUTES", "").strip()
    if routes:
        return Path(routes).with_name(filename)
    from config import DATA_DIR

    return DATA_DIR / "skills" / filename


def candidates_path() -> Path:
    return _skills_dir_file("action_candidates.json", "UE_ACTION_CANDIDATES")


def promoted_path() -> Path:
    return _skills_dir_file("promoted_actions.json", "UE_PROMOTED_ACTIONS")


def _read_rows(path: Path, key: str) -> list[dict]:
    import json

    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get(key) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _write_rows(path: Path, key: str, rows: list[dict]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: rows}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_candidates() -> list[dict]:
    return _read_rows(candidates_path(), "candidates")


def load_promoted() -> list[dict]:
    return _read_rows(promoted_path(), "actions")


def spec_of(name: str) -> dict | None:
    if name in CATALOG:
        return CATALOG[name]
    for item in load_promoted():
        if str(item.get("name") or "") == name and str(item.get("status") or "") == "approved":
            spec = item.get("spec")
            return spec if isinstance(spec, dict) else None
    return None


def allows(name: str) -> bool:
    """内置表，或人审通过后的候选。未审候选返回 False，执行器不会跑它。"""
    if name in CATALOG:
        return True
    for item in load_promoted():
        if str(item.get("name") or "") == name and str(item.get("status") or "") == "approved":
            return True
    return False


def iter_prompt_specs():
    """给规划者和理解器看的动作。未审候选不出现。"""
    for name, spec in CATALOG.items():
        if name in (INTEGRATE_ACTION, DISPOSE_ACTION):
            continue
        yield name, spec
    for item in load_promoted():
        name = str(item.get("name") or "")
        spec = item.get("spec") if isinstance(item.get("spec"), dict) else None
        if not name or name in CATALOG or spec is None:
            continue
        if str(item.get("status") or "") != "approved":
            continue
        yield name, spec


# 候选表闭环（D8）：只有写方没有读方时，缺口既不进人审队列也没有到期，
# 「记了」等于「没记」。这三处是闭环的三条腿：记次数、算年龄、出声。
CANDIDATE_STALE_DAYS = 14


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def _age_days(stamp: str) -> int | None:
    """ISO 日期到今天的整天数。解析不出来返回 None（不猜、不冒充新鲜）。"""
    from datetime import date

    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        then = date.fromisoformat(text[:10])
    except ValueError:
        return None
    return max(0, (date.today() - then).days)


def note_candidate(name: str, *, source: str = "", summary: str = "") -> dict | None:
    """记下表外动作。只进候选文件，不进 CATALOG，不执行。

    D8：每次命中都累计 hits + 刷新 last_seen + 追加 run/source 到来源集合。
    「同一个缺口被提过几次」是唯一能区分「偶发表外动作」与「真缺能力」的信号，
    没有它就没有优先级可言。first_seen 只写一次，不许被后来的命中改写。"""
    action = str(name or "").strip()
    if not action or action in (INTEGRATE_ACTION, DISPOSE_ACTION) or action in CATALOG:
        return None
    rows = load_candidates()
    found = next((item for item in rows if str(item.get("name") or "") == action), None)
    today = _today()
    if found is None:
        found = {
            "name": action,
            "version": 1,
            "conflicts": [],
            "verification": [],
            "status": "review",
            "source": source,
            "summary": summary or "人审前不执行",
            "spec": {"slots": (), "summary": summary or "人审前不执行"},
            "first_seen": today,
            "last_seen": today,
            "hits": 1,
            "sources": [source] if source else [],
        }
        rows.append(found)
    else:
        found["status"] = found.get("status") or "review"
        if source:
            found["source"] = source
        # 兼容老记录：没有这些字段的（20261024 之前写下的）在第一次被再次命中时补上，
        # first_seen 用 last_seen 回落，实在没有就记今天 —— 不许倒填一个假的更早日期。
        found.setdefault("first_seen", str(found.get("last_seen") or today))
        found["last_seen"] = today
        try:
            found["hits"] = int(found.get("hits") or 0) + 1
        except (TypeError, ValueError):
            found["hits"] = 1
        sources = [str(item) for item in (found.get("sources") or []) if str(item).strip()]
        if source and source not in sources:
            sources.append(source)
        found["sources"] = sources
        if summary:
            found["summary"] = summary
    _write_rows(candidates_path(), "candidates", rows)
    return found


def stale_candidates(days: int = CANDIDATE_STALE_DAYS) -> list[dict]:
    """超过阈值仍是 review 的候选：这些是「记了但没人管」的缺口，run 收尾必须出声。

    只读。`dismissed` 不算（人已经明确表过态），`approved` 也不算（已经闭环）。
    first_seen 缺失或坏掉时用 last_seen 回落，两者都没有就**不算过期**——
    宁可少报，不许凭一个猜出来的年龄去催人。"""
    out: list[dict] = []
    for row in load_candidates():
        if str(row.get("status") or "") != "review":
            continue
        age = _age_days(row.get("first_seen"))
        if age is None:
            age = _age_days(row.get("last_seen"))
        if age is None or age < days:
            continue
        item = dict(row)
        item["age_days"] = age
        out.append(item)
    out.sort(key=lambda item: (-int(item.get("age_days") or 0), str(item.get("name") or "")))
    return out


def dismiss_candidate(name: str, *, note: str = "") -> dict | None:
    """人一键关掉一条候选：状态记 dismissed，永不自动 promote。

    这是「候选表不是垃圾桶」的出口：噪音要能被人明确清掉，而不是靠阈值假装看不见。"""
    action = str(name or "").strip()
    if not action:
        return None
    rows = load_candidates()
    found = next((item for item in rows if str(item.get("name") or "") == action), None)
    if found is None:
        return None
    found["status"] = "dismissed"
    found["dismissed_at"] = _today()
    if note:
        found["dismiss_note"] = str(note)
    _write_rows(candidates_path(), "candidates", rows)
    return found


def promote_candidate(name: str, *, reviewed: bool, verification: list, conflicts: list | None = None) -> dict | None:
    """人审、验证和冲突都齐了才入已提升表。仍不改 CATALOG 常量，执行器只认 allows()。"""
    if not reviewed:
        return None
    action = str(name or "").strip()
    if not action or action in CATALOG:
        return None
    if not isinstance(verification, list) or not verification:
        return None
    if conflicts is None:
        conflicts = []
    if not isinstance(conflicts, list):
        return None
    rows = load_candidates()
    found = next((item for item in rows if str(item.get("name") or "") == action), None)
    if found is None or str(found.get("status") or "") not in ("review", "approved"):
        return None
    try:
        version = int(found.get("version") or 1)
    except (TypeError, ValueError):
        version = 1
    record = {
        "name": action,
        "version": version,
        "conflicts": list(conflicts),
        "verification": [str(item) for item in verification if str(item).strip()],
        "status": "approved",
        "spec": found.get("spec") if isinstance(found.get("spec"), dict) else {"slots": (), "summary": action},
    }
    if not record["verification"]:
        return None
    promoted = [item for item in load_promoted() if str(item.get("name") or "") != action]
    promoted.append(record)
    _write_rows(promoted_path(), "actions", promoted)
    found["status"] = "approved"
    found["version"] = version
    found["conflicts"] = record["conflicts"]
    found["verification"] = record["verification"]
    _write_rows(candidates_path(), "candidates", rows)
    return record


def system_prompt() -> str:
    """只有动作表。不放检索、技能或工具说明。未审候选动作不写进提示。"""
    lines = [
        "你只输出 JSON，把用户这一句话拆成窗口清单。",
        "action 只能取下面的名字，槽位名只能用这一行写出的。看不准就不要编新动作。",
        "每个窗口只有 id、title、action、slots、depends_on、max_retries。",
        "有依赖的窗口把 depends_on 写成上游 id。互不依赖的不要互相等待。不要解释。",
        "编译某一个 BP_ 用 compile_blueprint，槽位 blueprint 填那个名字。只有「编译工程」才用 compile_project。",
        "打可交互标签用 set_pickable。让另一个对象与它交互用 judge_pickup，depends_on 指向上一扇。",
        "只要求启动或打开编辑器时用 launch_editor，不要用 compile_project（那句里没有编译）。",
        "移动用 move_actor，location 写成 x,y,z。",
        "query_actor 和 run_automation_test 是只读，不改场景。",
        "材质参数用 set_scalar_param：target 填 MI_ 名或 /Game 路径，param 填参数名，value 填数字，blueprint 填要点名的 BP_。",
        "读材质参数用 query_material（只读）。",
        "批量布置用 place_batch：blueprint 填蓝图名，layout 填设计图文件名（data/harness/level_layout/ 下的 JSON）。",
        "精确删离群实例用 remove_batch：targets 填 [{name, x, y, z}]，名称按 label、坐标容差 2uu，坐标对不上就不删。",
        "新建一个关卡用 create_level：level 填 L_ 名或 /Game 路径，建完会切到那个关卡。"
        "句子里还要求打开编辑器时，create_level 和 launch_editor 各一扇。",
        "打开已经存在的关卡用 open_level，不要用 create_level。关卡不存在就失败，不要新建。",
        "实现简易射击或 FPS 用 implement_shooter：level 填句中唯一的 L_ 名或 /Game 路径。"
        "这个动作会把人和靶放进该关卡。没有唯一关卡名就不要编一个。不要改用 compile_blueprint。",
        "加手榴弹补给用 implement_grenade：level 填句中唯一的 L_ 名或 /Game 路径。"
        "它要求那一关已有射击内容；句子同时要射击和手榴弹时，implement_grenade 依赖 implement_shooter。",
        "",
    ]
    for name, spec in iter_prompt_specs():
        slots = "、".join(spec["slots"]) if spec.get("slots") else "无"
        if spec.get("mode") == "one":
            slots += "（有一个即可）"
        lines.append(f"- {name}。槽位：{slots}。{spec['summary']}")
    lines.extend([
        "",
        "integrate_verify 由代码在任务书确认后自动追加，你不要写它。",
        "dispose_residual 由代码在 Automation 残差后追加，你不要写它。",
        "输出形状：",
        '{"windows":[{"id":"W1","title":"这一扇要做完的事","action":"set_pickable","slots":{"target":"BP_名字"},"depends_on":[],"max_retries":1}]}',
    ])
    return "\n".join(lines)
