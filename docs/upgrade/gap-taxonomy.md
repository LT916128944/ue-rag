# 附录：能力缺口分类、证据与断言清单

配套主文档： [`next-upgrade-plan.md`](next-upgrade-plan.md)（下文简称「主文档」）。
本文件是**证据与骨架**，不含结论性论证。所有输出都是实测采集，采集脚本都附在正文里，可复跑。

- 采集时间：2026-10-24 19:44 – 20:05（Asia/Shanghai）
- 基准：`RAG/ue-rag` 未提交工作树（`git HEAD` 是 `463077b`，2026-10-20；此后 20261021–20261024 的全部改动都未提交，所以 **HEAD 不能当基准**）
- 同批并行改动：T1（`create_level` 全链路）、T2（零窗口不静默 + 三态）、T3（离线套件）——本附录逐处标明"哪些数字受它们影响"
- **行号漂移实测**：19:57 → 20:05，`agent/brief.py` 的符号行号 +2（`brief_system_prompt` 185→187、`validate_brief` 283→285、`render_brief_md` 542→544、`brief_flow` 791→793、`materialize_scopes` 847→849），`agent/run_windows.py` 的 `_run_integrate` 960→999。引用时**以符号名为准**。

---

## 附录 A：能力声明点与实测集合差

### A.1 采集脚本（可复跑，只读，不写盘）

```pwsh
cd E:\UEProject\AIWork\RAG\ue-rag
@'
import ast, re
def ev(node, names):
    if isinstance(node, ast.Name): return names[node.id]
    if isinstance(node, ast.Constant): return node.value
    if isinstance(node, ast.Dict): return {ev(k,names): ev(v,names) for k,v in zip(node.keys, node.values)}
    if isinstance(node, ast.Tuple): return tuple(ev(e,names) for e in node.elts)
    if isinstance(node, ast.List): return [ev(e,names) for e in node.elts]
    if isinstance(node, ast.Set): return set(ev(e,names) for e in node.elts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        vals = [ev(a,names) for a in node.args]; inner = vals[0] if vals else []
        return {"frozenset": frozenset, "set": set, "tuple": tuple, "list": list}.get(node.func.id, lambda x:x)(inner)
    return ast.literal_eval(node)
def load_assign(path, name, names=None):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for n in tree.body:
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == name:
            return ev(n.value, names or {})
ns = {}
exec(compile(open("agent/catalog.py", encoding="utf-8").read(), "c", "exec"), ns)
CATALOG, DIRECT, LOCK = ns["CATALOG"], ns["DIRECT_ACTIONS"], ns["EDITOR_LOCK_ACTIONS"]
READ, GRAPH = ns["READ_ONLY_ACTIONS"], ns["GRAPH_ACTIONS"]
KBA = load_assign("agent/harness.py", "_KIND_BY_ACTION")
EST = load_assign("agent/brief.py", "_ACTION_ESTIMATE", {"INTEGRATE_ACTION": "integrate_verify"})
SCENE = load_assign("agent/run_windows.py", "_SCENE_WRITE_ACTIONS")
sq = open("agent/sequence.py", encoding="utf-8").read()
seq = set()
for c in re.findall(r'action (?:==|in) (\([^)]*\)|"[^"]+")', sq):
    seq |= set(re.findall(r'"([^"]+)"', c)) if c.startswith("(") else {c.strip('"')}
print("DIRECT-CATALOG =", sorted(DIRECT - set(CATALOG)))
print("seq-CATALOG    =", sorted(seq - set(CATALOG)))
print("CATALOG-seq    =", sorted(set(CATALOG) - seq))
print("CATALOG-无证据类型 =", sorted(set(CATALOG) - set(KBA) - {"integrate_verify", "dispose_residual"}))
print("CATALOG-无估时     =", sorted(set(CATALOG) - set(EST) - {"dispose_residual"}))
print("并集动作数 =", len(set(CATALOG)|DIRECT|LOCK|READ|GRAPH|seq|set(KBA)|set(EST)|SCENE))
'@ | .\.venv\Scripts\python.exe -
```

### A.2 第一次观测（19:44，T1 未落地）——**这是本次失败 run 时代的真实状态**

```
CATALOG(20)                   # 不含 create_level，也不含 create_blueprint
DIRECT_ACTIONS(22)  EDITOR_LOCK(23)  READ_ONLY(6)  GRAPH(4)
_KIND_BY_ACTION(18)  _ACTION_ESTIMATE(20)  _SCENE_WRITE(9)
EVIDENCE_KINDS = ('cpp_build','blueprint_compile','editor_assert','automation','widget_graph','manual')

DIRECT - CATALOG = ['bind_slot','check_pickup','create_blueprint',
                    'place_blueprint','place_pair','probe_look','probe_move','probe_play']   # 8 个
seq    - CATALOG = 同上 8 个
CATALOG 无证据类型 = ['bind_blueprint','create_widget_blueprint']
CATALOG 无估时     = ['check_platform_hud','create_widget_blueprint']
create_level 全仓库出现次数 = 0（agent/ core/ tests/ 全搜）
```

结论（主文档 D1）：**8 个动作"执行器能跑、规划者看不见"**；`create_blueprint` 是其中最重要的一条（它就是 T1/T2 里被代码反复插入的"创建蓝图前置扇"，`agent/plan_windows.py:552-614`）。

### A.3 第二次观测（19:56:33，T1 在途）——**守卫会红的现场**

```
CATALOG(21)  # 已含 create_level
DIRECT_ACTIONS(23)  EDITOR_LOCK(24)  READ_ONLY(6)  GRAPH(4)
_KIND_BY_ACTION(18)  _ACTION_ESTIMATE(20)  _SCENE_WRITE(9)  _SOURCE_WRITE(1)

DIRECT - CATALOG = ['bind_slot','check_pickup','create_blueprint',
                    'place_blueprint','place_pair','probe_look','probe_move','probe_play']   # 仍是 8 个
seq    - CATALOG = 同上 8 个
CATALOG - seq    = ['compile_blueprint','compile_project','create_level',
                    'dispose_residual','edit_cpp','edit_widget_blueprint','integrate_verify']
CATALOG 无证据类型 = ['bind_blueprint','create_level','create_widget_blueprint']
CATALOG 无估时     = ['check_platform_hud','create_level','create_widget_blueprint']
DIRECT 无证据类型   = 9 个（上表 8 个 + create_level）
声明点并集动作数 = 29
```

**同一时刻的状态**：`create_level` 已在 `catalog.py:31/123/166/183`、`plan_windows.resolve_slots`、`supervise.py:1089+`、`brief.py:124` 出现，**但 `sequence._run_action` 没有分支，`_KIND_BY_ACTION` 没有它**。

⇒ 此刻 `eval/check_catalog_parity.py`（主文档 §4.2 的 I1/I2/I3）**会失败**，报三条：
1. `create_level` 在 `CATALOG` 但不在 `sequence._run_action`（I2 违反：能规划、不能执行）；
2. `create_level` 在 `CATALOG` 但没有 `evidence_kind`（I3 违反：能写东西、没裁判）；
3. `DIRECT - CATALOG` 的 8 条白名单里，`create_blueprint` 等 8 条仍需逐条写明 `inserted_by`（I1 通过但需登记）。

这不是谁写错了——**T1 只落地了 13 处声明点中的一部分，而没有任何机制告诉它"还差几处"**。这就是 `[事实]` 级的证据，证明主文档 D1 的载体是"13 处手工声明 + 0 处守卫"。

> 提醒：A.3 是**中间态**。T1 收口（`sequence._run_action` 加 `create_level` 分支、`_KIND_BY_ACTION` 加映射）后会绿。复跑 A.1 脚本可自证。

### A.4 13 处声明点（19:56:33）

| # | 声明点 | 位置 | 规模 | 内容 |
| --- | --- | --- | --- | --- |
| 1 | `CATALOG` | `agent/catalog.py:8-100` | 21 | 规划者可见动作 + 2 个代码专用（`integrate_verify`/`dispose_residual`） |
| 2 | `DIRECT_ACTIONS` | `agent/catalog.py:102-125` | 23 | 直接执行白名单 |
| 3 | `READ_ONLY_ACTIONS` | `agent/catalog.py:127-134` | 6 | 只读裁决 |
| 4 | `GRAPH_ACTIONS` | `agent/catalog.py:136-141` | 4 | 走图/编译的串行动作 |
| 5 | `EDITOR_LOCK_ACTIONS` | `agent/catalog.py:143-167` | 24 | 占编辑器锁 |
| 6 | `_VERIFY` | `agent/catalog.py:169-190` | 21 | 准入声明（`action_meta` `:193` 读它） |
| 7 | `_KIND_BY_ACTION` | `agent/harness.py:20-39` | 18 | 动作 → `evidence_kind` |
| 8 | `EVIDENCE_KINDS` | `agent/harness.py:13` | 6 | 证据类型全集 |
| 9 | `sequence._run_action` | `agent/sequence.py:128-273` | 22 | 真正的执行分支 |
| 10 | `_SCENE_WRITE_ACTIONS` / `_SOURCE_WRITE_ACTIONS` | `agent/run_windows.py:84-88` | 9 / 1 | 快照与"触及文件"口径 |
| 11 | `_ACTION_ESTIMATE` | `agent/brief.py:21-42` | 20 | 估时 |
| 12 | `core/judge.judge` | `core/judge.py:544-555` | 4 | 真裁判分支（`editor_assert`/`blueprint_compile`/`cpp_build`/`automation`） |
| 13 | `_ASSET_GROUND_ACTIONS` | `agent/plan_windows.py:603` | 3 | 归地/补窗 |

**第 12 条的一个陷阱**：`EVIDENCE_KINDS` 有 6 个，`core/judge.judge` 只认 4 个。`widget_graph` 靠 `agent/run_windows.py:864-866` **现造**一条判决（"控件图扇跑完并编译通过"），`manual` 永远不由机器判。所以"新增动作的 verification kind"这一项必须区分 `judge: "core"` 与 `judge: "synthetic:<站点>"`（主文档 §4.2）。

---

## 附录 B：两次历史对照（字段级）

| 维度 | 20261021-195220「五项空转全绿」 | 20261024-192858（本 run） |
| --- | --- | --- |
| 现象 | 一句什么都没做的 run 拿到「矩阵全部通过」 | 一句只启动编辑器的 run 拿到「矩阵没有可验证的项，不算全过」+ `judge_failed` |
| 矩阵口径 | 全不适用的项被当成通过 | 全不适用的项被当成失败 |
| 交付物 | 19 条交付物、3 个零窗口范围、C++ 窗口一行没改（`eval/check_harness.py:686-687`）`[事实]` | 13 条交付条目（A2+B2+C5+集成4）、2 个零窗口范围（B/C）、业务窗口 1 扇（`20261024-192858.json:14-94`、`.md:24`）`[事实]` |
| 修复位置 | `agent/run_windows.py:967-969`（口径注释）、`:1045-1049`（`checked` 为空不算全过）`[事实]` | 无（口径已正确，暴露的是缺计数与归因） |
| 回归断言 | `eval/check_harness.py:685-774` `check_under_delivery_guard`，末尾 `assert data["status"] != "成功"` | 无（本次修复的 T3 才会补） |
| 零窗口范围的处理 | `agent/harness.py:250` 的注释直接引用该 run 的 B/D/E 零窗口 | 同一函数 `:266-267` 给出 `reason`，但被 `run_windows.py:1640` 的前置条件挡住 → 主文档 D9 |
| 共同缺失 | **`evidence_count` 与 `scope_ids`/归因**：两边的矩阵项都只有 `{item, ok, note}`;`data/archive/20261024-192858/deliverables.json:3-4` 连矩阵都被覆盖成空（主文档 D10） | 同左 |

**读法**：同一段代码在两个方向上各错一次（放水一次、冤枉一次），两次都不是"某个 `if` 写错"，而是**判决项缺少"这个结论由哪些证据支撑、支撑了几条"这两个字段**。补 `if` 只会产生第三个极端。

### B.1 同类的第三次预演（对照 `data/harness/progress.json`）

`data/harness/progress.json` 的 `risks` 里已经写着：

> 20261021-221446 的教训：理解器的 unresolved 是需求黑洞——不进窗口、不进合同，只在交接文本里留一句。任何「句子里点名要进场景」的东西都必须由代码转成 place_batch + editor_assert，不能靠模型自觉；level-assembly-persist 这条 pending 未闭环前，别把整句报成成功 `[事实]`

对照本次：`data/events/20261024-192858.jsonl:2` 有两条 `unresolved`（"新建一个关卡，名字为L_NewTest2（动作表无新建关卡动作）"、"…FPS射击游戏逻辑（…无法落到具体动作）"），而任务书 JSON 里**没有 `unresolved` 字段**、md 里**没有「尚未完成清单」章节** `[事实]`。

⇒ **同一个"需求黑洞"在 2026-10-21 出现一次（场景内容），2026-10-24 换入口又出现一次（整段范围）**。2026-10-21 的解法是加专用闸门（`agent/scene_gate.py`），它只覆盖"点位摆放"类动词。这证明主文档 D2/D5/D9 的方向：**要修的是"缺口必须改变流程走向"这条通用规则，不是再加第三个专用闸门**。

---

## 附录 C：七件套准入清单（机读草稿）

```json
{
  "name": "create_level",
  "slots": ["level"],
  "summary": "新建关卡资产（当前实现是复制模板 /Game/Maps/L_NewTest，可被 UE_LEVEL_TEMPLATE 覆盖）并切为当前编辑关卡；不保存其它资产。",
  "planner_visible": true,
  "direct": true,
  "editor_lock": true,
  "read_only": false,
  "graph": false,
  "scene_write": true,
  "source_write": false,
  "verify": ["关卡资产存在", "get_current_level 复查含目标名"],
  "evidence_kind": "editor_assert",
  "judge": "core",
  "estimate_hours": [1.0, 4.0],
  "asset_ground": false,
  "inserted_by": null,
  "since": "2026-10-24",
  "tests": ["eval/check_create_level.py", "eval/check_failure_192858.py"]
}
```

字段 → 今天的手工位置映射，见主文档 §4.2 代码块与 §4.3 表。生成规则：

```python
CATALOG              = {n: {"slots": tuple(a["slots"]), "summary": a["summary"]}
                        for n, a in ACTIONS.items() if a["planner_visible"] and a["name"] not in CODE_ONLY}
DIRECT_ACTIONS       = frozenset(n for n, a in ACTIONS.items() if a["direct"])
EDITOR_LOCK_ACTIONS  = frozenset(n for n, a in ACTIONS.items() if a["editor_lock"])
READ_ONLY_ACTIONS    = frozenset(n for n, a in ACTIONS.items() if a["read_only"])
GRAPH_ACTIONS        = frozenset(n for n, a in ACTIONS.items() if a["graph"])
_VERIFY              = {n: list(a["verify"]) for n, a in ACTIONS.items()}
_KIND_BY_ACTION      = {n: a["evidence_kind"] for n, a in ACTIONS.items() if a["evidence_kind"]}
_ACTION_ESTIMATE     = {n: tuple(a["estimate_hours"]) for n, a in ACTIONS.items()}
_SCENE_WRITE_ACTIONS = frozenset(n for n, a in ACTIONS.items() if a["scene_write"])
_SOURCE_WRITE_ACTIONS= frozenset(n for n, a in ACTIONS.items() if a["source_write"])
_ASSET_GROUND_ACTIONS= frozenset(n for n, a in ACTIONS.items() if a["asset_ground"])
```

`CODE_ONLY` = `{"integrate_verify", "dispose_residual"}`（两者今天在 `CATALOG` 里但被 `iter_prompt_specs` 过滤掉，`agent/catalog.py:279-284`）。

---

## 附录 D：`eval/check_catalog_parity.py` 骨架（复制即用，约 70 行）

> 本骨架**只读**：不导入编辑器、不调模型、不写盘。P0-1 的交付物。

```python
# eval/check_catalog_parity.py
"""能力表 vs 执行器 parity 守卫（主文档 §4）。
两个方向的差集必须为空：执行器支持但规划者看不见（白名单外）、规划者能写但执行器不认。
退出码非 0 时打印差集，CI 直接红。"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 代码专用扇：执行器认，但规划者不该看见。每条必须写明插入点，且插入点必须真实存在。
CODE_ONLY_ALLOWED = {
    "create_blueprint":       "plan_windows._create_and_goal",   # 前置创建扇
    "create_widget_blueprint": "plan_windows (UMG 归地)",
    "bind_slot":              "plan_windows._materialize_goals",
    "place_blueprint":        "plan_windows.materialize_goal",
    "place_pair":             "plan_windows._place_pair",
    "probe_move":             "supervise / 学习路径",
    "probe_look":             "supervise / 学习路径",
    "probe_play":             "supervise / 学习路径",
    "check_pickup":           "supervise 拾取裁决",
}


def _executor_actions() -> set[str]:
    """静态解析 sequence._run_action 的 action == / action in 分支。"""
    src = (ROOT / "agent" / "sequence.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_run_action")
    out: set[str] = set()
    for node in ast.walk(target):
        if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                and node.left.id == "action"):
            continue
        for comp in node.comparators:
            if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                out.add(comp.value)
            elif isinstance(comp, (ast.Tuple, ast.Set, ast.List)):
                out |= {e.value for e in comp.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return out


def _planner_actions() -> set[str]:
    from agent.catalog import iter_prompt_specs
    return {name for name, _ in iter_prompt_specs()}


def _brief_prompt_actions() -> set[str]:
    """brief_system_prompt 里真正播出去的动作名（D1 的第二条看不见的路径）。"""
    from agent.brief import brief_system_prompt
    text = brief_system_prompt()
    return set(re.findall(r"^- ([a-z_]+)。", text, re.M))


def main() -> int:
    from agent.catalog import CATALOG, GRAPH_ACTIONS, READ_ONLY_ACTIONS
    from agent.harness import EVIDENCE_KINDS, _KIND_BY_ACTION

    problems: list[str] = []
    executor, planner = _executor_actions(), _planner_actions()

    # I1 执行器支持但规划者看不见 -> 必须在白名单，且写明插入点
    for name in sorted(executor - planner):
        if name not in CODE_ONLY_ALLOWED:
            problems.append(f"I1 {name} 执行器能跑但规划者看不见，且不在白名单")
        elif not CODE_ONLY_ALLOWED[name].strip():
            problems.append(f"I1 {name} 在白名单里但没写 inserted_by")

    # I2 规划者能写但执行器不认
    for name in sorted(planner - executor - set(GRAPH_ACTIONS)):
        problems.append(f"I2 {name} 规划者能写，sequence._run_action 不认")

    # I3 能写东西的动作必须有证据类型或合成裁判
    for name in sorted(set(CATALOG) - set(READ_ONLY_ACTIONS) - {"launch_editor"}):
        if not _KIND_BY_ACTION.get(name) and name not in ("integrate_verify", "dispose_residual"):
            problems.append(f"I3 {name} 能写但既无 evidence_kind 也无合成裁判")

    # I4 证据类型必须都在全集里
    for name, kind in _KIND_BY_ACTION.items():
        if kind not in EVIDENCE_KINDS:
            problems.append(f"I4 {name} 的 kind={kind} 不在 EVIDENCE_KINDS")

    # I5 任务书生成器与规划者提示必须看见同一套动作（D1 附带事实）
    if _brief_prompt_actions() != planner:
        diff = (planner - _brief_prompt_actions()) | (_brief_prompt_actions() - planner)
        problems.append(f"I5 任务书提示与规划者提示动作集不一致：{sorted(diff)}")

    if problems:
        print("catalog parity FAILED")
        for line in problems:
            print("  -", line)
        return 1
    print(f"catalog parity ok（执行器 {len(executor)} 扇、规划者 {len(planner)} 扇、"
          f"白名单 {len(CODE_ONLY_ALLOWED)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**I5 的注意点**：今天 `brief_system_prompt` 与 `catalog.system_prompt()` 的动作集**本来就不同**（前者遍历 `CATALOG`，后者遍历 `iter_prompt_specs()` 含 promoted），所以 I5 一上线**必红**——这是设计意图：它把"第二条看不见的路径"钉成一条必须显式处理的差（要么统一真值源，要么把差异写进白名单）。

---

## 附录 E：`data/skills/lexicon.json` 草稿（人写、机读、CI 校验）

```json
{
  "version": 1,
  "owner": "人（工程 owner / Lead）",
  "note": "模型只能提议（action_candidates），不能改本表。每行要么指向已过七件套的动作，要么 unplannable 且带 reason。",
  "rows": [
    {"verbs": ["新建关卡", "建一个关卡", "新建地图"], "action": "create_level",
     "evidence": "工具面无 new_level；实现=AssetTools.duplicate + SceneTools.load_level",
     "caveat": "模板复制，不是空白关卡"},
    {"verbs": ["创建蓝图", "新建蓝图", "BP_"], "action": "create_blueprint",
     "evidence": "sequence.py:136；当前由 plan_windows._create_and_goal 代码插入"},
    {"verbs": ["打开编辑器", "启动编辑器"], "action": "launch_editor", "evidence": "catalog.py:9"},
    {"verbs": ["编译这张蓝图", "编译蓝图"], "action": "compile_blueprint", "evidence": "catalog.py:18"},
    {"verbs": ["编译工程", "编译项目"], "action": "compile_project", "evidence": "catalog.py:14"},
    {"verbs": ["加输入", "输入映射", "WASD", "按键", "鼠标"], "unplannable": true,
     "reason": "830 工具面无 Enhanced Input / InputMapping 工具（索引 2026-09-21）",
     "suggested_path": "edit_cpp（C++ 侧 InputComponent/EnhancedInput）+ 人工蓝图图（UE_ALLOW_BLUEPRINT）"},
    {"verbs": ["开火", "射击", "命中", "子弹", "伤害"], "unplannable": true,
     "reason": "830 工具面无 spawn/projectile/damage 工具",
     "suggested_path": "edit_cpp + 人工蓝图图；玩法读数只能来自 Automation 或人点界面"},
    {"verbs": ["计分", "HUD", "文本反馈", "结算", "倒计时"], "action": "edit_widget_blueprint",
     "requires": ["UE_ALLOW_UMG"], "pair_with": ["create_widget_blueprint", "check_platform_hud"],
     "evidence": "catalog.py:72/:77；plan_windows.py:410/:446；只读核对 catalog.py:86"},
    {"verbs": ["移动", "移到", "坐标"], "action": "move_actor", "evidence": "catalog.py:55"},
    {"verbs": ["摆放", "布置", "放进去", "批量摆"], "action": "place_batch", "evidence": "catalog.py:35"},
    {"verbs": ["删掉多余的", "离群实例"], "action": "remove_batch", "evidence": "catalog.py:41"},
    {"verbs": ["材质参数"], "action": "set_scalar_param", "evidence": "catalog.py:63"},
    {"verbs": ["跑测试", "验证", "自动化"], "action": "run_automation_test", "evidence": "catalog.py:82"},
    {"verbs": ["改蓝图图", "加节点", "连线"], "unplannable": true,
     "reason": "动作表内无改图动作（bind_blueprint 明写不改图，catalog.py:29），工具面有 BlueprintTools.create_node/connect_pins",
     "suggested_path": "UE_ALLOW_BLUEPRINT 门的人工图路径；步骤写入 Plan/"}
  ]
}
```

### E.1 `eval/check_lexicon.py` 断言（4 条）

```python
rows = json.loads((ROOT / "data/skills/lexicon.json").read_text(encoding="utf-8"))["rows"]
assert all(("action" in r) ^ bool(r.get("unplannable")) for r in rows), "每行二选一"
assert all(r.get("reason") for r in rows if r.get("unplannable")), "unplannable 必须写原因"
assert {r["action"] for r in rows if r.get("action")} <= set(CATALOG), "lexicon 不许发明动作"
# 覆盖断言：本 run 的两条 unresolved 必须被接住
unresolved = json.loads((ROOT / "data/events/20261024-192858.jsonl").read_text(encoding="utf-8")
                        .splitlines()[1])["unresolved"]
hits = [r for r in rows if any(v in unresolved[0] for v in r["verbs"])]
assert hits, "「新建一个关卡」必须被 lexicon 接住"
hits = [r for r in rows if any(v in unresolved[1] for v in r["verbs"])]
assert any(r.get("unplannable") for r in hits), "FPS 玩法动词必须落到 unplannable 而非静默"
```

---

## 附录 F：P0 离线断言清单（交给 T3 的写法）

8 组 P0，每组一个文件。全部**不打开编辑器、不调模型**，风格沿用 `eval/check_harness.py`（`assert` + 末尾打印 `xxx ok`），并接进 `.github/workflows/offline.yml`（今天到 `retrieval_bench` 为止，`:42`）。

| # | 文件 | 关键断言（摘要） | 反例守卫（必须同时有） |
| --- | --- | --- | --- |
| 1 | `eval/check_catalog_parity.py`（骨架=附录 D） | I1 白名单外差集为空；I2 `planner - executor == ∅`；I3 能写的动作有 kind/合成裁判；I4 kind ∈ `EVIDENCE_KINDS`；I5 任务书提示与规划者提示动作集一致 | 白名单每条必须有真实 `inserted_by` |
| 2 | `eval/check_verdict_tristate.py` | 复现 5 项全 `ok=None` ⇒ `verdict == "no_evidence"`、`stop_reason != "judge_failed"`；`verdict` 四值互斥穷尽 | **只有不适用项的矩阵永远 `ok is False`**（沿用 `check_harness.py:773`） |
| 3 | `eval/check_evidence_count.py` | 每个矩阵项有 `evidence_count` + `scope_ids`；本 run 复现 ⇒ 计数 0 且 `scope_ids == {"B","C"}`；`sum(evidence_count) == len(passed criteria)` | 有真实裁判通过的正常 run，`evidence_count > 0` |
| 4 | `eval/check_review_coverage.py` | 人审渲染含每范围覆盖数/缺口；存在零窗口范围时 `review_status != "通过"`；dry-run 预演与显示一致 | 覆盖齐全的任务书渲染与旧版逐字一致 |
| 5 | `eval/check_brief_semantics.py` | `{deliverables 非空, windows 空}` ⇒ `capability_gap` 或 `blocked`；全局不变量「有交付无窗口必带缺口」 | `external_dep` / `blocked` / 集成末行**不得**判成缺口 |
| 6 | `eval/check_undelivered_outlet.py` | run md 含「未交付」+ B/C 各自原因；run json 有结构化 `undelivered`；任务书 md 有 `intent.unresolved` 章节 | 全部范围完成时**不出现**「未交付」段 |
| 7 | `eval/check_archive_fidelity.py` | `archive/deliverables.json["matrix"]` 长度 == run json 集成扇矩阵长度；重复归档幂等 | 存量坏档 `data/archive/20261024-192858/deliverables.json` 的 `matrix == []` 保持原样（钉住，不手工修） |
| 8 | `eval/check_escalation.py` | 本 run 任务书 ⇒ B/C 有 `capability_gap`；`contract_status != "部分完成"`；缺口时流程必须要求显式确认 | 正常 run（有真裁判、无零窗口）仍记「成功」 |

**接 CI 的写法**（追加到 `offline.yml` 末尾）：

```yaml
      - name: check_catalog_parity
        run: python eval/check_catalog_parity.py
      - name: check_verdict_tristate
        run: python eval/check_verdict_tristate.py
      - name: check_evidence_count
        run: python eval/check_evidence_count.py
      - name: check_review_coverage
        run: python eval/check_review_coverage.py
      - name: check_brief_semantics
        run: python eval/check_brief_semantics.py
      - name: check_undelivered_outlet
        run: python eval/check_undelivered_outlet.py
      - name: check_archive_fidelity
        run: python eval/check_archive_fidelity.py
      - name: check_escalation
        run: python eval/check_escalation.py
```

---

## 附录 G：本次失败 run 的证据字段索引（冻结）

| 证据 | 位置 | 值 |
| --- | --- | --- |
| run 状态 | `data/runs/20261024/20261024-192858.json:7` | `"部分完成"` |
| 人审 | 同 `:8-11` | `{"rounds":1,"status":"通过"}` |
| 范围 B 零窗口 + 缺能力原话 | 同 `:41-58`（`windows: []` `:52`，`block_note` `:57`，`pending_note` `:47`） | 「动作表里没有建关卡的动作…」 |
| 范围 C 零窗口 | 同 `:59-80`（`windows: []` `:74`） | 「原句只说"自由发挥"…」 |
| 物化后 `window_ids` | 同 `:143`（B）、`:156`（C） | `[]` |
| W1 结果 | 同 `:189-253` | `launch_editor` 成功，`tool_calls: 0`、`elapsed: 0` |
| W2 集成失败 | 同 `:262`、`:266`、`:331` | `ok:false`、`0 项通过口径、5 项不适用`、`stop_reason:"judge_failed"` |
| 矩阵 5 项 | 同 `:272-298` | 全部 `ok: null` |
| 集成 note | 同 `:335-339` | 「完成 W1；失败 W2（无下游依赖，不阻塞别的范围）」 |
| 合同 | 同 `:340-350` | 只有 1 条 `c-editor_assert`（W1） |
| 理解器 unresolved | `data/events/20261024-192858.jsonl:2` | 2 条（建关卡、FPS 玩法） |
| 无候选事件 | 同文件 1-14 行 | 无 `action_candidate` 事件（对照 `data/skills/action_candidates.json` 不存在） |
| 任务书落盘件 | `data/briefs/20261024-192830-L_NewTest2-FPS.md` | 21 行，三列表，无「尚未完成清单」章节 |
| 归档件（坏） | `data/archive/20261024-192858/deliverables.json:3-4` | `"hashes": [], "matrix": []`（真实矩阵 5 项被覆盖） |
| 人读 run 记录 | `data/runs/20261024/20261024-192858.md:19-31` | 矩阵 5 行「不适用」，范围行无原因 |
| 台账 | `data/ledger.json:11-37` | B/C `status:"pending"`，`note` 末尾带「没有可执行的窗口，只记待办」 |