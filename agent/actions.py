"""动作的单一声明源（20261028 P1-6，next-upgrade-plan §4）。

此前一个动作名散落在十几处手工声明里（catalog 的 CATALOG/DIRECT/READ_ONLY/GRAPH/
EDITOR_LOCK/_VERIFY、harness 的 _KIND_BY_ACTION、brief 的估时表、parity 的代码专用
白名单……），补一个动作要改 8~13 处，漏一处就是「能规划不能执行」或「能执行看不见」。

现在一处声明、处处推导：
- ``ROWS``：规划者可见动作（plannable）——CATALOG 由它生成；
- ``CODE_ONLY``：代码专用扇——规划者写不出，只有代码补窗给得出；inserted_by 记录
  每个插入站点，check_catalog_parity 逐站 ast 复核；
- ``JUDGE_BY_ACTION`` / ``VERIFY_BY_ACTION`` / ``DIRECT`` / ``GRAPH`` / ``READ_ONLY``
  / ``EDITOR_LOCK`` / ``ESTIMATES``：全部由行推导，下游（catalog / harness / brief /
  check_catalog_parity）只读这里。

加一个新动作 = 在这里加一行（必要时 sequence._run_action 补执行分支），不再散改。
surface 字段声明该动作在 monolith 执行面上真实用到的原语（P1-5 能力面对账的数据源）。
"""
from __future__ import annotations

INTEGRATE_ACTION = "integrate_verify"
DISPOSE_ACTION = "dispose_residual"

# 行字段：name / slots / summary / judge_kind / dispatch(direct|graph) / read_only /
# editor_lock / estimate / verify / mode / inserted_by / surface
_ROWS: list[dict] = [
    {
        "name": "launch_editor",
        "slots": (),
        "summary": "启动 UE 编辑器并等 MCP 端口连通。不编译、不改场景、不保存、不进 PIE。"
                   "句子里只要求启动/打开编辑器时用这一扇。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (0.1, 0.5),
        "verify": ["编辑器进程在，MCP 端口连通"],
    },
    {
        "name": "compile_project",
        "slots": (),
        "summary": "编译整个工程，不改源码。这句话里要启动编辑器时，编译通过后再启动。",
        "judge_kind": "cpp_build",
        "dispatch": "graph",
        "editor_lock": True,
        "estimate": (2.0, 8.0),
        "verify": ["cpp_build 退出码 0 且无 error C"],
    },
    {
        "name": "compile_blueprint",
        "slots": ("blueprint",),
        "summary": "编译一张已经点名的蓝图。不改图。",
        "judge_kind": "blueprint_compile",
        "dispatch": "graph",
        "editor_lock": True,
        "estimate": (1.0, 4.0),
        "verify": ["blueprint_compile 通过"],
    },
    {
        "name": "edit_cpp",
        "slots": ("class",),
        "summary": "只改一个 C++ 类。类不是唯一的一条就暂停。",
        "judge_kind": "cpp_build",
        "dispatch": "graph",
        "editor_lock": True,
        "estimate": (4.0, 16.0),
        "verify": ["cpp_build 退出码 0"],
    },
    {
        "name": "bind_blueprint",
        "slots": ("class", "blueprint"),
        "mode": "one",
        "summary": "解析唯一蓝图。没有资产、而且这句话里已经给了 /Game 路径时才新建，不改图。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (1.0, 4.0),
        "verify": ["唯一蓝图路径"],
    },
    {
        "name": "create_level",
        "slots": ("level",),
        "summary": "新建一个关卡并切过去：把模板关卡复制成 level 给的关卡，落盘后载入为当前关卡。"
                   "level 填 L_ 名（解析成 /Game/Maps/名字）或 /Game 路径。已存在就不重复复制，只切关卡。"
                   "名字为空或不唯一就暂停。不改场景内容，不动源码。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "editor_lock": True,
        "verify": ["/Game 下关卡资产存在", "该关卡已成为当前关卡"],
        "surface": [
            "project.get_asset_details",
            "editor.create_empty_map",
            "editor.load_level",
            "editor.save_packages",
            "editor.list_dirty_packages",
        ],
    },
    {
        "name": "open_level",
        "slots": ("level",),
        "summary": "打开已经存在的关卡并切过去。level 填 L_ 名、裸名或 /Game 路径。"
                   "没有这份关卡就失败，不新建。不改场景内容。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (0.2, 1.0),
        "verify": ["/Game 下关卡资产存在", "该关卡已成为当前关卡", "不存在就不新建"],
        "surface": [
            "editor.load_level",
            "editor.save_packages",
            "editor.list_dirty_packages",
        ],
    },
    {
        "name": "place_actor",
        "slots": ("blueprint",),
        "summary": "把蓝图放进当前关卡。不保存。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (1.0, 3.0),
        "verify": ["实例在当前关卡"],
    },
    {
        "name": "place_batch",
        "slots": ("blueprint", "layout"),
        "summary": "按设计图 JSON 批量放置同一蓝图（layout 填设计图文件名，"
                   "条目带 name 的幂等跳过，不重复放）。复查数量。"
                   "默认不保存；窗口显式 save: true 才存当前关卡一次。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (1.0, 4.0),
        "verify": ["每个条目在当前关卡有唯一实例，未授权不保存"],
    },
    {
        "name": "remove_batch",
        "slots": ("targets",),
        "summary": "按 targets 里每条 {name, x, y, z} 精确删 Actor（name 是 label，坐标容差 2uu）。"
                   "名字命中但坐标对不上就不删；命中多条筛不出唯一一条就整批失败、一个都不删。"
                   "删完逐条复查坐标已归零。默认不保存；窗口显式 save: true 才存当前关卡一次。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (0.5, 2.0),
        "verify": ["每条目标按名字+坐标唯一命中才删，删后该坐标归零", "未授权不保存"],
    },
    {
        "name": "judge_pickup",
        "slots": ("target", "picker"),
        "summary": "只读裁决能否拾取。不写标签。距离不够仍是裁决结果。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "read_only": True,
        "estimate": (0.5, 2.0),
        "verify": ["只读裁决，距离不超过 100 uu"],
    },
    {
        "name": "set_pickable",
        "slots": ("target",),
        "summary": "让场景里的实例可拾取：bPickable 为真，并带 InteractableActor。没有这个实例就失败。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (1.0, 4.0),
        "verify": ["bPickable 为真", "InteractableActor 存在"],
    },
    {
        "name": "move_actor",
        "slots": ("actor", "location"),
        "summary": "把一个已经点名的 Actor 移到给出的坐标。不保存。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (0.5, 2.0),
        "verify": ["复查坐标误差不超过 0.1"],
    },
    {
        "name": "query_actor",
        "slots": ("name",),
        "summary": "只读。按名字读当前关卡里唯一 Actor 的位置。零个或多个就停。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "read_only": True,
        "editor_lock": True,
        "estimate": (0.5, 2.0),
        "verify": ["唯一 Actor 的位置"],
    },
    {
        "name": "set_scalar_param",
        "slots": ("target", "param", "value", "blueprint"),
        "summary": "覆写材质实例的一个标量参数（target 填 MI_ 名或 /Game 路径），"
                   "写完复读；blueprint 槽位给了蓝图就再编译它。不改材质图，不保存。",
        "judge_kind": "blueprint_compile",
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (0.5, 2.0),
        "verify": ["参数复读等于给定值", "点名的蓝图编译通过"],
    },
    {
        "name": "query_material",
        "slots": ("target", "param"),
        "summary": "只读。读材质实例一个标量参数的当前值。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "read_only": True,
        "editor_lock": True,
        "estimate": (0.5, 1.0),
        "verify": ["读出参数取值"],
    },
    {
        "name": "create_widget_blueprint",
        "slots": ("folder", "name", "parent"),
        "summary": "新建句子里点名的 Widget Blueprint。路径和 WBP_ 名交给执行层；"
                   "没有唯一名字也先试，不在调用前判死。"
                   "UE_ALLOW_UMG 不是 true 时不调用编辑器。",
        "judge_kind": "blueprint_compile",
        "dispatch": "direct",
        "editor_lock": True,
        "verify": ["句子点名的 Widget Blueprint 存在，父类与槽位一致"],
    },
    {
        "name": "edit_widget_blueprint",
        "slots": (),
        "summary": "UE_ALLOW_UMG 为 true 时改句子里那份控件图，然后编译并保存这份资产。"
                   "没有唯一名字也先试。开关不是 true 时不执行。",
        "judge_kind": "blueprint_compile",
        "dispatch": "graph",
        "editor_lock": True,
        "estimate": (1.0, 4.0),
        "verify": ["句子点名的控件图编译通过，只保存这份资产"],
    },
    {
        "name": "run_automation_test",
        "slots": ("filter",),
        "summary": "只读。按过滤器跑 Automation，失败数不是 0 就不算通过。不另开 PIE。",
        "judge_kind": "automation",
        "dispatch": "direct",
        "read_only": True,
        "estimate": (0.5, 2.0),
        "verify": ["过滤器失败数为 0"],
    },
    {
        "name": "implement_shooter",
        "slots": ("level",),
        "summary": "把简易射击放进唯一关卡：地面、方向光、PlayerStart、一个 FpsPawn、三个 FpsTarget。"
                   "level 填 L_ 名或 /Game 路径，没有或不唯一就暂停，不猜。"
                   "不改蓝图图，不改项目默认 GameMode，不改 ATestGameMode，不改 Pawn 的自动占据。"
                   "这一关的 GameMode Override 设为 FpsGameMode，开始游戏时占据 FpsPawn，左键打看得见的靶。"
                   "先核对这个关卡里真有 FpsPawn、FpsTarget、地面和灯，并且 Override 已设上；没有就不算通过。"
                   "AIAgent.Fps.Hit 只证明临时世界里的命中，不能单独当成关卡已做好。",
        "judge_kind": "automation",
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (1.0, 4.0),
        "verify": ["关卡里有 FpsPawn、FpsTarget、地面和灯", "AIAgent.Fps.Hit 失败数为 0", "关卡名唯一"],
        "surface": [
            "editor.load_level",
            "mesh.get_scene_statistics",
            "mesh.spawn_actor",
            "editor.author_map_settings",
            "editor.save_packages",
            "editor.list_dirty_packages",
        ],
    },
    {
        "name": "implement_grenade",
        "slots": ("level",),
        "summary": "把手榴弹补给放进唯一关卡并收口：这一关必须已有 FpsPawn、FpsTarget、地面和灯"
                   "（没有就引导先跑 implement_shooter，不算过）。"
                   "补足两个 FpsGrenadePickup（幂等：已够数量就跳过），保存关卡，"
                   "再以 AIAgent.Fps.Grenade 失败数 0 收口。"
                   "level 填 L_ 名或 /Game 路径，没有或不唯一就暂停，不猜。"
                   "AIAgent.Fps.Grenade 只证明临时世界里的爆炸链路，不能单独当成关卡已做好。",
        "judge_kind": "automation",
        "dispatch": "direct",
        "editor_lock": True,
        "verify": ["关卡里有 FPS 前置与两个 FpsGrenadePickup", "AIAgent.Fps.Grenade 失败数为 0", "关卡名唯一"],
        "surface": [
            "editor.load_level",
            "mesh.get_scene_statistics",
            "editor.author_map_settings",
            "editor.save_packages",
            "editor.list_dirty_packages",
        ],
    },
    {
        "name": "check_platform_hud",
        "slots": (),
        "summary": "只读核对 /Game/UI/WBP_HUD 是否有 txt_CoinCount、txt_Timer、txt_Clear、txt_Fail。"
                   "缺任何一个就不算通过。不改控件图。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "read_only": True,
        "editor_lock": True,
        "verify": ["txt_CoinCount、txt_Timer、txt_Clear、txt_Fail 都在"],
    },
    {
        "name": "check_fps_hud",
        "slots": (),
        "summary": "只读核对 FPS HUD 控件图：血条/子弹/手雷/载具名/E 提示/时速六个控件是否都在这张"
                   "Widget Blueprint 里，父类是不是 FpsHUDWidget，并确认 C++ 侧有挂视口的入口。"
                   "不填路径就按 /Game/UI/WBP_FpsHUD 核对；句子点了 WBP_ 名或 /Game 路径就核那一张。"
                   "缺任何一条就不算通过，不改控件图、不进 PIE。",
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "read_only": True,
        "editor_lock": True,
        "estimate": (0.5, 2.0),
        "verify": ["六个控件名都在", "父类是 FpsHUDWidget", "AFpsPawn 有挂视口的入口"],
        "surface": ["ui.get_widget_tree", "blueprint.get_parent_class"],
    },
    {
        "name": INTEGRATE_ACTION,
        "slots": (),
        "summary": "集成矩阵验收：重复请求幂等、拒答路径、延迟窗口、断线重连、未知态恢复，各一个子判定；"
                   "产出触及文件哈希、token 汇总与归档。只在任务书确认后由代码追加，规划者不要写它。",
        "judge_kind": "",
        "dispatch": "direct",
        "estimate": (2.0, 8.0),
        "verify": ["矩阵子判定"],
    },
    {
        "name": DISPOSE_ACTION,
        "slots": (),
        "summary": "编译已过、Automation 仍失败时，由代码按失败测试名追加。规划者不要写它。",
        "judge_kind": "",
        "dispatch": "direct",
        "verify": ["残差被同 kind 裁判通过，或归因标成 out_of_contract / equivalent"],
    },
]

# 代码专用扇：执行器认，规划者看不见。inserted_by 的站点由 check_catalog_parity 逐站
# ast 复核（模块文件存在 + 函数存在 + 函数体内真的出现这个动作名）。
CODE_ONLY: dict[str, dict] = {
    "apply_spec": {
        "judge_kind": "cpp_build",   # P0-3 起写完强制编译，收口就是 cpp_build 裁判
        "dispatch": "direct",
        "estimate": (2.0, 8.0),
        "inserted_by": (
            "decompose.windows_from_steps",
            "sequence._run_action",
        ),
    },
    "create_blueprint": {
        "dispatch": "direct",
        "inserted_by": (
            "plan_windows._create_and_goal",
            "supervise._try_blueprint_create",
            "supervise._try_named_blueprint_place",
            "supervise._plan_rules",
        ),
    },
    "bind_slot": {
        "dispatch": "direct",
        "editor_lock": True,
        "inserted_by": (
            "supervise._bind_slot",
            "sequence._run_action",
        ),
    },
    "check_pickup": {
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "read_only": True,
        "inserted_by": (
            "supervise._append_pickup_slots",
            "supervise._pickup_step",
            "plan_windows._action_of",
            "sequence._run_action",
        ),
    },
    "place_blueprint": {
        "judge_kind": "editor_assert",
        "dispatch": "direct",
        "editor_lock": True,
        "inserted_by": (
            "supervise._try_named_blueprint_place",
            "supervise._plan_rules",
        ),
    },
    "place_pair": {
        "dispatch": "direct",
        "editor_lock": True,
        "inserted_by": (
            "supervise._append_pickup_slots",
        ),
    },
    "probe_move": {
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (1.0, 4.0),
        "inserted_by": (
            "supervise._plan_rules",
        ),
    },
    "probe_look": {
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (1.0, 4.0),
        "inserted_by": (
            "supervise._plan_rules",
        ),
    },
    "probe_play": {
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (1.0, 4.0),
        "inserted_by": (
            "scene_gate.ensure_scene_windows",
        ),
    },
    # 受控求解扇（20261028 P0-2）：零窗口范围补窗失败后由 brief.materialize_scopes 插入，
    # UE_SOLVER_ENABLED 缺省 false；收口判据固定 automation，产不出过滤器就诚实失败。
    "solve": {
        "judge_kind": "automation",
        "dispatch": "direct",
        "editor_lock": True,
        "estimate": (1.0, 4.0),
        "inserted_by": (
            "brief._solver_scope_windows",
            "sequence._run_action",
        ),
    },
}


# ---------------------------------------------------------------- 推导
ACTIONS: dict[str, dict] = {}
for _row in _ROWS:
    _spec = {"slots": tuple(_row.get("slots") or ()), "summary": _row.get("summary") or ""}
    if _row.get("mode"):
        _spec["mode"] = _row["mode"]
    ACTIONS[_row["name"]] = _spec

JUDGE_BY_ACTION: dict[str, str] = {}
for _row in _ROWS:
    if _row.get("judge_kind"):
        JUDGE_BY_ACTION[_row["name"]] = _row["judge_kind"]
for _name, _row in CODE_ONLY.items():
    if _row.get("judge_kind"):
        JUDGE_BY_ACTION[_name] = _row["judge_kind"]

VERIFY_BY_ACTION: dict[str, list[str]] = {
    _row["name"]: list(_row.get("verify") or []) for _row in _ROWS if _row.get("verify")
}

DIRECT = frozenset(
    _row["name"] for _row in _ROWS if _row.get("dispatch") == "direct"
) | frozenset(
    name for name, row in CODE_ONLY.items() if row.get("dispatch") == "direct"
)
GRAPH = frozenset(_row["name"] for _row in _ROWS if _row.get("dispatch") == "graph")
READ_ONLY = frozenset(
    _row["name"] for _row in _ROWS if _row.get("read_only")
) | frozenset(name for name, row in CODE_ONLY.items() if row.get("read_only"))
EDITOR_LOCK = frozenset(
    _row["name"] for _row in _ROWS if _row.get("editor_lock")
) | frozenset(name for name, row in CODE_ONLY.items() if row.get("editor_lock"))

ESTIMATES: dict[str, tuple[float, float]] = {
    _row["name"]: tuple(_row["estimate"]) for _row in _ROWS if _row.get("estimate")
}
for _name, _row in CODE_ONLY.items():
    if _row.get("estimate"):
        ESTIMATES[_name] = tuple(_row["estimate"])

# P1-5 能力面对账：namespace.action → 用它的动作名列表。
SURFACE: dict[str, dict[str, list[str]]] = {}
for _row in _ROWS + [{**row, "name": name} for name, row in CODE_ONLY.items()]:
    for _primitive in _row.get("surface") or []:
        _ns, _, _action = str(_primitive).partition(".")
        if not _ns or not _action:
            continue
        SURFACE.setdefault(_ns, {}).setdefault(_action, []).append(_row["name"])