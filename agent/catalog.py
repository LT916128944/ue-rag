# agent/catalog.py
"""规划者能用的动作。表外的 action 不执行。integrate_verify 与 dispose_residual 只由代码追加，规划者写不出。"""
from pathlib import Path

INTEGRATE_ACTION = "integrate_verify"
DISPOSE_ACTION = "dispose_residual"

CATALOG = {
    "launch_editor": {
        "slots": (),
        "summary": "启动 UE 编辑器并等 MCP 端口连通。不编译、不改场景、不保存、不进 PIE。"
                   "句子里只要求启动/打开编辑器时用这一扇。",
    },
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
    "create_level": {
        "slots": ("level",),
        "summary": "新建一个关卡并切过去：把模板关卡复制成 level 给的关卡，落盘后载入为当前关卡。"
                   "level 填 L_ 名（解析成 /Game/Maps/名字）或 /Game 路径。已存在就不重复复制，只切关卡。"
                   "名字为空或不唯一就暂停。不改场景内容，不动源码。",
    },
    "open_level": {
        "slots": ("level",),
        "summary": "打开已经存在的关卡并切过去。level 填 L_ 名、裸名或 /Game 路径。"
                   "没有这份关卡就失败，不新建。不改场景内容。",
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
    "remove_batch": {
        "slots": ("targets",),
        "summary": "按 targets 里每条 {name, x, y, z} 精确删 Actor（name 是 label，坐标容差 2uu）。"
                   "名字命中但坐标对不上就不删；命中多条筛不出唯一一条就整批失败、一个都不删。"
                   "删完逐条复查坐标已归零。默认不保存；窗口显式 save: true 才存当前关卡一次。",
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
        "summary": "只读。按名字读当前关卡里唯一 Actor 的位置。零个或多个就停。",
    },
    "set_scalar_param": {
        "slots": ("target", "param", "value", "blueprint"),
        "summary": "覆写材质实例的一个标量参数（target 填 MI_ 名或 /Game 路径），"
                   "写完复读；blueprint 槽位给了蓝图就再编译它。不改材质图，不保存。",
    },
    "query_material": {
        "slots": ("target", "param"),
        "summary": "只读。读材质实例一个标量参数的当前值。",
    },
    "create_widget_blueprint": {
        "slots": ("folder", "name", "parent"),
        "summary": "新建句子里点名的 Widget Blueprint。路径和 WBP_ 名交给执行层；"
                   "没有唯一名字也先试，不在调用前判死。"
                   "UE_ALLOW_UMG 不是 true 时不调用编辑器。",
    },
    "edit_widget_blueprint": {
        "slots": (),
        "summary": "UE_ALLOW_UMG 为 true 时改句子里那份控件图，然后编译并保存这份资产。"
                   "没有唯一名字也先试。开关不是 true 时不执行。",
    },
    "run_automation_test": {
        "slots": ("filter",),
        "summary": "只读。按过滤器跑 Automation，失败数不是 0 就不算通过。不另开 PIE。",
    },
    "implement_shooter": {
        "slots": ("level",),
        "summary": "把简易射击放进唯一关卡：地面、方向光、PlayerStart、一个 FpsPawn、三个 FpsTarget。"
                   "level 填 L_ 名或 /Game 路径，没有或不唯一就暂停，不猜。"
                   "不改蓝图图，不改项目默认 GameMode，不改 ATestGameMode，不改 Pawn 的自动占据。"
                   "这一关的 GameMode Override 设为 FpsGameMode，开始游戏时占据 FpsPawn，左键打看得见的靶。"
                   "先核对这个关卡里真有 FpsPawn、FpsTarget、地面和灯，并且 Override 已设上；没有就不算通过。"
                   "AIAgent.Fps.Hit 只证明临时世界里的命中，不能单独当成关卡已做好。",
    },
    "check_platform_hud": {
        "slots": (),
        "summary": "只读核对 /Game/UI/WBP_HUD 是否有 txt_CoinCount、txt_Timer、txt_Clear、txt_Fail。"
                   "缺任何一个就不算通过。不改控件图。",
    },
    INTEGRATE_ACTION: {
        "slots": (),
        "summary": "集成矩阵验收：重复请求幂等、拒答路径、延迟窗口、断线重连、未知态恢复，各一个子判定；"
                   "产出触及文件哈希、token 汇总与归档。只在任务书确认后由代码追加，规划者不要写它。",
    },
    DISPOSE_ACTION: {
        "slots": (),
        "summary": "编译已过、Automation 仍失败时，由代码按失败测试名追加。规划者不要写它。",
    },
}

DIRECT_ACTIONS = frozenset({
    "launch_editor",
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
    "create_level",
    "open_level",
    "create_widget_blueprint",
    "query_actor",
    "run_automation_test",
    "implement_shooter",
    "apply_spec",
    "check_platform_hud",
    "set_scalar_param",
    "query_material",
    "place_batch",
    "remove_batch",
})

READ_ONLY_ACTIONS = frozenset({
    "judge_pickup",
    "check_pickup",
    "query_actor",
    "run_automation_test",
    "check_platform_hud",
    "query_material",
})

# 要改蓝图或控件图：monolith 下同一目标最多 3 次。compile_project / edit_cpp 不在这里。
BLUEPRINT_ATTEMPT_ACTIONS = frozenset({
    "compile_blueprint",
    "bind_blueprint",
    "create_widget_blueprint",
    "edit_widget_blueprint",
})

GRAPH_ACTIONS = frozenset({
    "compile_project",
    "edit_cpp",
    "compile_blueprint",
    "edit_widget_blueprint",
})

EDITOR_LOCK_ACTIONS = frozenset({
    "launch_editor",
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
    "create_level",
    "open_level",
    "create_widget_blueprint",
    "edit_widget_blueprint",
    "query_actor",
    "implement_shooter",
    "check_platform_hud",
    "set_scalar_param",
    "query_material",
    "place_batch",
    "remove_batch",
})

_VERIFY = {
    "launch_editor": ["编辑器进程在，MCP 端口连通"],
    "compile_project": ["cpp_build 退出码 0 且无 error C"],
    "compile_blueprint": ["blueprint_compile 通过"],
    "edit_cpp": ["cpp_build 退出码 0"],
    "bind_blueprint": ["唯一蓝图路径"],
    "create_level": ["/Game 下关卡资产存在", "该关卡已成为当前关卡"],
    "open_level": ["/Game 下关卡资产存在", "该关卡已成为当前关卡", "不存在就不新建"],
    "place_actor": ["实例在当前关卡"],
    "judge_pickup": ["只读裁决，距离不超过 100 uu"],
    "set_pickable": ["bPickable 为真", "InteractableActor 存在"],
    "move_actor": ["复查坐标误差不超过 0.1"],
    "query_actor": ["唯一 Actor 的位置"],
    "run_automation_test": ["过滤器失败数为 0"],
    "implement_shooter": ["关卡里有 FpsPawn、FpsTarget、地面和灯", "AIAgent.Fps.Hit 失败数为 0", "关卡名唯一"],
    "check_platform_hud": ["txt_CoinCount、txt_Timer、txt_Clear、txt_Fail 都在"],
    "set_scalar_param": ["参数复读等于给定值", "点名的蓝图编译通过"],
    "query_material": ["读出参数取值"],
    "create_widget_blueprint": ["句子点名的 Widget Blueprint 存在，父类与槽位一致"],
    "edit_widget_blueprint": ["句子点名的控件图编译通过，只保存这份资产"],
    "place_batch": ["每个条目在当前关卡有唯一实例，未授权不保存"],
    "remove_batch": ["每条目标按名字+坐标唯一命中才删，删后该坐标归零", "未授权不保存"],
    INTEGRATE_ACTION: ["矩阵子判定"],
    DISPOSE_ACTION: ["残差被同 kind 裁判通过，或归因标成 out_of_contract / equivalent"],
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
