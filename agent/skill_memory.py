# agent/skill_memory.py
"""技能记忆。learned_skills.json 是主库，learned_routes.json 只读兼容。

影子条目只记在技能文件里，match 不读。提升要整句可回放且调用方确认 unresolved 为空。
删一条即可作废。不执行动作，不写场景。"""
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SKILL_LIMIT = 200
SKILL_FIELDS = (
    "id",
    "version",
    "when",
    "roles",
    "goals",
    "preconditions",
    "action_sequence",
    "verification",
    "failure_recovery",
    "confidence",
    "source_runs",
    "conflicts",
    "lru",
)


def skills_path() -> Path:
    override = os.environ.get("UE_LEARNED_SKILLS", "").strip()
    if override:
        return Path(override)
    routes_override = os.environ.get("UE_LEARNED_ROUTES", "").strip()
    if routes_override:
        return Path(routes_override).with_name("learned_skills.json")
    from config import DATA_DIR

    return DATA_DIR / "skills" / "learned_skills.json"


def load_skills(path: Path | None = None) -> list[dict]:
    target = Path(path) if path is not None else skills_path()
    if not target.is_file():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("skills") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _save_skills(skills: list[dict], path: Path | None = None) -> None:
    target = Path(path) if path is not None else skills_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    skills = _trim(skills)
    payload = {"skills": skills}
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)


def _trim(skills: list[dict]) -> list[dict]:
    ranked = sorted(skills, key=lambda item: int(item.get("lru") or 0))
    return ranked[-SKILL_LIMIT:]


def _next_lru(skills: list[dict]) -> int:
    best = 0
    for item in skills:
        try:
            best = max(best, int(item.get("lru") or 0))
        except (TypeError, ValueError):
            continue
    return best + 1


def validate_skill(skill: dict) -> list[str]:
    """缺字段、置信度出界、冲突不是列表，都算不合格。不改文件。"""
    errors = []
    if not isinstance(skill, dict):
        return ["技能不是对象"]
    for key in SKILL_FIELDS:
        if key not in skill:
            errors.append(f"缺 {key}")
    if not str(skill.get("id") or "").strip():
        errors.append("id 为空")
    try:
        version = int(skill.get("version"))
        if version < 1:
            errors.append("version 要从 1 起")
    except (TypeError, ValueError):
        errors.append("version 不是整数")
    try:
        confidence = float(skill.get("confidence"))
        if confidence < 0 or confidence > 1:
            errors.append("confidence 要在 0 到 1")
    except (TypeError, ValueError):
        errors.append("confidence 不是数")
    if not isinstance(skill.get("conflicts"), list):
        errors.append("conflicts 要是列表")
    if not isinstance(skill.get("source_runs"), list):
        errors.append("source_runs 要是列表")
    goals = skill.get("goals")
    if not isinstance(goals, list) or not goals:
        errors.append("goals 为空")
    status = str(skill.get("status") or "promoted")
    if status not in ("promoted", "shadow", "rejected"):
        errors.append(f"status {status} 不认识")
    return errors


def skill_from_route(route: dict, *, status: str = "shadow", source_run: str = "") -> dict | None:
    """把一条旧路由收成技能。不记资产名，规格沿用 windows 或 goals。"""
    if not isinstance(route, dict):
        return None
    rid = str(route.get("id") or "").strip()
    roles = [str(role) for role in (route.get("roles") or []) if str(role).strip()]
    when_terms = [str(word) for word in (route.get("when") or []) if str(word).strip()]
    specs = route.get("goals") or route.get("windows") or []
    if not rid or not roles or not when_terms or not isinstance(specs, list) or not specs:
        return None
    actions = []
    for spec in specs:
        if isinstance(spec, dict) and spec.get("action"):
            actions.append(str(spec["action"]))
        elif isinstance(spec, str) and spec.strip():
            actions.append(spec.strip())
    if not actions:
        return None
    runs = [source_run] if source_run else []
    skill = {
        "id": rid,
        "version": 1,
        "when": " ".join(when_terms),
        "when_terms": when_terms,
        "roles": roles,
        "goals": actions,
        "preconditions": ["动作在 catalog 内", "槽位可解析才执行"],
        "action_sequence": actions,
        "verification": [],
        "failure_recovery": ["缺路径问一次 r", "路径不唯一就暂停"],
        "confidence": 0.9 if status == "promoted" else 0.4,
        "source_runs": runs,
        "conflicts": [],
        "lru": 1,
        "status": status,
        "supersedes": "",
    }
    if route.get("goals"):
        skill["goal_specs"] = specs
    else:
        skill["windows"] = specs
    return skill


def _upsert(skill: dict, *, promote: bool) -> str | None:
    errors = validate_skill(skill)
    if errors:
        return None
    skills = load_skills()
    rid = str(skill["id"])
    found = None
    for item in skills:
        if str(item.get("id") or "") == rid:
            found = item
            break
    if promote:
        if found and str(found.get("status") or "") == "promoted":
            runs = list(found.get("source_runs") or [])
            for run in skill.get("source_runs") or []:
                if run and run not in runs:
                    runs.append(run)
            found["source_runs"] = runs
            found["lru"] = _next_lru(skills)
            _save_skills(skills)
            return rid
        skill["status"] = "promoted"
        skill["confidence"] = max(float(skill.get("confidence") or 0), 0.9)
    else:
        if found and str(found.get("status") or "") == "promoted":
            return None
        skill["status"] = "shadow"
    skill["lru"] = _next_lru(skills)
    skills = [item for item in skills if str(item.get("id") or "") != rid]
    skills.append(skill)
    _save_skills(skills)
    return rid


def remember_shadow(route: dict) -> str | None:
    """规划者先记下的句式只进影子，不参与 L0。"""
    skill = skill_from_route(route, status="shadow")
    if skill is None:
        return None
    return _upsert(skill, promote=False)


def remember_solver_skill(skill: dict) -> str | None:
    """求解轨迹收成技能影子（20261028 P2-7）。只进影子，永不由代码提升：
    动作表的写入权在人——同族动词要等 lexicon 提名行被人审通过、绑上真动作后，
    才由人把它变成可规划的路由。"""
    if not isinstance(skill, dict):
        return None
    row = dict(skill)
    row.setdefault("version", 1)
    row.setdefault("when", "")
    row.setdefault("roles", [])
    row.setdefault("goals", [])
    row.setdefault("preconditions", [])
    row.setdefault("action_sequence", [])
    row.setdefault("verification", [])
    row.setdefault("failure_recovery", [])
    row.setdefault("confidence", 0.4)
    row.setdefault("source_runs", [])
    row.setdefault("conflicts", [])
    row.setdefault("lru", 1)
    row["status"] = "shadow"  # 代码只写影子；promoted 只由人审路径写
    return _upsert(row, promote=False)


def _actions_of(run: dict) -> list[str]:
    windows = run.get("windows")
    if isinstance(windows, dict):
        rows = list(windows.values())
    elif isinstance(windows, list):
        rows = windows
    else:
        rows = []
    return [str((item or {}).get("action") or "") for item in rows if isinstance(item, dict) and item.get("action")]


def _find_run(run_id: str) -> dict | None:
    if not str(run_id or "").strip():
        return None
    roots = []
    override = os.environ.get("UE_DISTILL_ROOT", "").strip()
    if override:
        roots.append(Path(override))
    try:
        from config import DATA_DIR

        roots.append(DATA_DIR)
    except Exception:
        pass
    seen = set()
    for root in roots:
        if str(root) in seen or not root.is_dir():
            continue
        seen.add(str(root))
        run_dir = root / "runs"
        if not run_dir.is_dir():
            continue
        paths = list(run_dir.glob("*.json")) + list(run_dir.glob("*/*.json"))
        for path in paths[:80]:
            if path.stem != run_id:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                return data
    return None


def replay_skill(skill: dict, run: dict) -> dict:
    """离线规划，只比动作序列和 unresolved 是否变多。不执行，不是结果回放。"""
    query = str((run or {}).get("query") or "")
    before = (run or {}).get("unresolved") or []
    if not isinstance(before, list):
        before = []
    skill_actions = [str(item) for item in (skill.get("action_sequence") or []) if str(item)]
    run_actions = _actions_of(run or {})
    after = list(before)
    if query:
        try:
            from agent.plan_windows import plan_windows

            planned = plan_windows(query, complete=lambda _messages: "")
            intent = planned.get("intent") if isinstance(planned, dict) else {}
            after = list((intent or {}).get("unresolved") or [])
        except Exception:
            after = list(before)
    grew = len(after) > len(before)
    mismatch = bool(run_actions) and bool(skill_actions) and skill_actions != run_actions
    return {
        "consistent": (not grew) and (not mismatch),
        "unresolved_before": len(before),
        "unresolved_after": len(after),
        "skill_actions": skill_actions,
        "run_actions": run_actions,
        "executed": False,
        "result_replay": False,
    }


def _content_sha1(action_sequence: list) -> str:
    """动作序列的确定性哈希。只算 action_sequence，不掺资产名。"""
    raw = json.dumps(list(action_sequence or []), sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def promote_from_route(route: dict, source_run: str = "") -> str | None:
    """整句集成成功后才提升。回放不一致时保持影子。调用方要先确认 unresolved 为空。

    记忆要有来源：source_run 为空时不提升，停在 shadow。提升时写 content_sha1。
    已有条目不回写哈希；content_sha1 不进 validate_skill 必填项。"""
    if not str(source_run or "").strip():
        skill = skill_from_route(route, status="shadow")
        if skill is None:
            return None
        return _upsert(skill, promote=False)
    skill = skill_from_route(route, status="promoted", source_run=source_run)
    if skill is None:
        return None
    skill["content_sha1"] = _content_sha1(skill.get("action_sequence"))
    run = _find_run(source_run) if source_run else None
    if run is not None:
        try:
            report = replay_skill(skill, run)
        except Exception:
            report = None
        if report is not None:
            skill["replay"] = {
                "consistent": report["consistent"],
                "unresolved_before": report["unresolved_before"],
                "unresolved_after": report["unresolved_after"],
                "executed": False,
                "result_replay": False,
            }
            if not report["consistent"]:
                return _upsert(skill, promote=False)
    return _upsert(skill, promote=True)


def promoted_as_routes() -> list[dict]:
    """只把已提升技能还原成 match_learned 认得的路由形状。影子不返回。"""
    routes = []
    for skill in load_skills():
        if str(skill.get("status") or "") != "promoted":
            continue
        if validate_skill(skill):
            continue
        route = {
            "id": skill.get("id"),
            "when": list(skill.get("when_terms") or []),
            "roles": list(skill.get("roles") or []),
        }
        if skill.get("goal_specs"):
            route["goals"] = skill["goal_specs"]
        elif skill.get("windows"):
            route["windows"] = skill["windows"]
        else:
            continue
        routes.append(route)
    routes.sort(key=lambda item: int(next(
        (skill.get("lru") or 0 for skill in load_skills() if skill.get("id") == item.get("id")),
        0,
    )), reverse=True)
    return routes


def unfinished_skills() -> list[dict]:
    """还没提升的技能。任务书用来列尚未完成，不拿去执行。"""
    rows = []
    for skill in load_skills():
        if str(skill.get("status") or "") in ("shadow", "rejected"):
            rows.append({"id": skill.get("id"), "status": skill.get("status"), "when": skill.get("when")})
    return rows


def reject_skill(skill_id: str) -> bool:
    skills = load_skills()
    hit = False
    for item in skills:
        if str(item.get("id") or "") == skill_id:
            item["status"] = "rejected"
            hit = True
    if hit:
        _save_skills(skills)
    return hit


def drop_skill(skill_id: str) -> bool:
    """删一条即回滚。旧路由文件不动。"""
    skills = load_skills()
    kept = [item for item in skills if str(item.get("id") or "") != skill_id]
    if len(kept) == len(skills):
        return False
    _save_skills(kept)
    return True


def compare_actions(query: str) -> dict:
    """A/B：同一句先走已提升技能，再只走旧路由。不执行。"""
    from agent.plan_windows import extract_names, load_routes, match_learned

    names = extract_names(query)
    skill_hit = match_learned(query)
    saved = os.environ.get("UE_LEARNED_SKILLS")
    # 临时把技能文件指到不存在的路径，match 只剩旧路由
    missing = str(skills_path().with_name("learned_skills.missing.json"))
    os.environ["UE_LEARNED_SKILLS"] = missing
    try:
        route_hit = match_learned(query)
    finally:
        if saved is None:
            os.environ.pop("UE_LEARNED_SKILLS", None)
        else:
            os.environ["UE_LEARNED_SKILLS"] = saved

    def actions(rows):
        if not rows:
            return []
        return [str(row.get("action") or "") for row in rows]

    left = actions(skill_hit)
    right = actions(route_hit)
    return {
        "query": query,
        "names": names,
        "skill_actions": left,
        "route_actions": right,
        "same": left == right,
        "executed": False,
        "result_replay": False,
        "note": "只比动作名，不是结果回放",
    }


def validate_store() -> list[str]:
    errors = []
    for skill in load_skills():
        for item in validate_skill(skill):
            errors.append(f"{skill.get('id')}: {item}")
    return errors


def main() -> int:
    import sys

    if "--validate" in sys.argv:
        errors = validate_store()
        if errors:
            print("\n".join(errors))
            return 1
        print("skills ok")
        return 0
    print("用法: python agent/skill_memory.py --validate")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
