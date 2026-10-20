# agent/catalog.py
"""规划者能用的动作。表外的 action 不执行。integrate_verify 只由代码追加，规划者写不出。"""
from pathlib import Path

INTEGRATE_ACTION = "integrate_verify"

CATALOG = {
    "compile_project": {
        "slots": (),
        "summary": "编译整个工程，不改源码。这句话里要启动编辑器时，编译通过后再启动。",
    },
    "compile_blueprint": {
        "slots": ("blueprint",),
        "summary": "编译一张已经点名的蓝图。不改图。",
    },
    "edit_cpp": {
        "slots": ("class",),
        "summary": "只改一个 C++ 类。类不是唯一的一条就暂停。",
    },
    "bind_blueprint": {
        "slots": ("class", "blueprint"),
        "mode": "one",
        "summary": "解析唯一蓝图。没有资产、而且这句话里已经给了 /Game 路径时才新建，不改图。",
    },
    "place_actor": {
        "slots": ("blueprint",),
        "summary": "把蓝图放进当前关卡。不保存。",
    },
    "place_batch": {
        "slots": ("blueprint", "layout"),
        "summary": "按设计图 JSON 批量放置同一蓝图（layout 填设计图文件名，"
                   "条目带 name 的幂等跳过，不重复放）。复查数量。"
                   "默认不保存；窗口显式 save: true 才存当前关卡一次。",
    },
    "judge_pickup": {
        "slots": ("target", "picker"),
        "summary": "只读裁决能否拾取。不写标签。距离不够仍是裁决结果。",
    },
    "set_pickable": {
        "slots": ("target",),
        "summary": "让场景里的实例可拾取：bPickable 为真，并带 InteractableActor。没有这个实例就失败。",
    },
    "move_actor": {
        "slots": ("actor", "location"),
        "summary": "把一个已经点名的 Actor 移到给出的坐标。不保存。",
    },
    "query_actor": {
        "slots": ("name",),
        "summary": "只读。按名字读当前关卡里唯一 Actor 的位置。零个或多个就停。不保存，不 PIE。",
    },
    "set_scalar_param": {
        "slots": ("target", "param", "value", "blueprint"),
        "summary": "覆写材质实例的一个标量参数（target 填 MI_ 名或 /Game 路径），"
                   "写完复读；blueprint 槽位给了蓝图就再编译它。不改材质图，不保存。",
    },
    "query_material": {
        "slots": ("target", "param"),
        "summary": "只读。读材质实例一个标量参数的当前值。不保存，不 PIE。",
    },
    "run_automation_test": {
        "slots": ("filter",),
        "summary": "只读。按过滤器跑 Automation，失败数不是 0 就不算通过。不另开 PIE，不保存。",
    },
    INTEGRATE_ACTION: {
        "slots": (),
        "summary": "集成矩阵验收：重复请求幂等、拒答路径、延迟窗口、断线重连、未知态恢复，各一个子判定；"
                   "产出触及文件哈希、token 汇总与归档。只在任务书确认后由代码追加，规划者不要写它。",
    },
}

DIRECT_ACTIONS = frozenset({
    "set_pickable",
    "move_actor",
    "probe_move",
    "probe_look",
    "probe_play",
    "judge_pickup",
    "check_pickup",
    "place_actor",
    "place_blueprint",
    "place_pair",
    "bind_blueprint",
    "bind_slot",
    "create_blueprint",
    "query_actor",
    "run_automation_test",
    "set_scalar_param",
    "query_material",
    "place_batch",
})

READ_ONLY_ACTIONS = frozenset({
    "judge_pickup",
    "check_pickup",
    "query_actor",
    "run_automation_test",
    "query_material",
})

GRAPH_ACTIONS = frozenset({
    "compile_project",
    "compile_blueprint",
    "edit_cpp",
})

EDITOR_LOCK_ACTIONS = frozenset({
    "compile_project",
    "compile_blueprint",
    "edit_cpp",
    "set_pickable",
    "move_actor",
    "probe_move",
    "probe_look",
    "probe_play",
    "place_actor",
    "place_blueprint",
    "place_pair",
    "bind_blueprint",
    "bind_slot",
    "create_blueprint",
    "query_actor",
    "set_scalar_param",
    "query_material",
    "place_batch",
})

_VERIFY = {
    "compile_project": ["cpp_build 退出码 0 且无 error C"],
    "compile_blueprint": ["blueprint_compile 通过"],
    "edit_cpp": ["cpp_build 退出码 0"],
    "bind_blueprint": ["唯一蓝图路径"],
    "place_actor": ["实例在当前关卡，未保存"],
    "judge_pickup": ["只读裁决，距离不超过 100 uu"],
    "set_pickable": ["bPickable 为真", "InteractableActor 存在"],
    "move_actor": ["复查坐标误差不超过 0.1"],
    "query_actor": ["唯一 Actor 的位置"],
    "run_automation_test": ["过滤器失败数为 0"],
    "set_scalar_param": ["参数复读等于给定值", "点名的蓝图编译通过"],
    "query_material": ["读出参数取值"],
    "place_batch": ["每个条目在当前关卡有唯一实例，未授权不保存"],
    INTEGRATE_ACTION: ["矩阵子判定"],
}


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
        if name == INTEGRATE_ACTION:
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


def note_candidate(name: str, *, source: str = "", summary: str = "") -> dict | None:
    """记下表外动作。只进候选文件，不进 CATALOG，不执行。"""
    action = str(name or "").strip()
    if not action or action == INTEGRATE_ACTION or action in CATALOG:
        return None
    rows = load_candidates()
    found = next((item for item in rows if str(item.get("name") or "") == action), None)
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
        }
        rows.append(found)
    else:
        found["status"] = found.get("status") or "review"
        if source:
            found["source"] = source
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
        "移动用 move_actor，location 写成 x,y,z。",
        "query_actor 和 run_automation_test 是只读，不保存，不 PIE。",
        "材质参数用 set_scalar_param：target 填 MI_ 名或 /Game 路径，param 填参数名，value 填数字，blueprint 填要点名的 BP_。",
        "读材质参数用 query_material（只读）。",
        "批量布置用 place_batch：blueprint 填蓝图名，layout 填设计图文件名（data/harness/level_layout/ 下的 JSON）。",
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
        "输出形状：",
        '{"windows":[{"id":"W1","title":"这一扇要做完的事","action":"set_pickable","slots":{"target":"BP_名字"},"depends_on":[],"max_retries":1}]}',
    ])
    return "\n".join(lines)
