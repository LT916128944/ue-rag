# 下次升级计划：能力边界、静默降级与裁决语义（20261024-192858 之后）

- 性质：**下次升级计划**，不是本次修复记录。本次修复（T1/T2/T3）只写它们与升级项的关系，不重复叙述。
- 触发：run `data/runs/20261024/20261024-192858.json` —— 原句「打开编辑器，新建一个关卡，名字为L_NewTest2，在这个关卡实现逻辑如下：需要实现简易的FPS射击游戏，内容你自由发挥」，整句只跑了 W1 `launch_editor`，范围 B/C 整段蒸发，状态「部分完成」，人审「审阅 1：通过」。
- 根因分析见 [`docs/failure/20261024-192858-root-cause.md`](../failure/20261024-192858-root-cause.md)（Lead 写，RC-1..RC-5 + 本次三轴修复）。
- 本文件回答的问题：**同一个缺陷类别下次还会不会发生，怎么在它进执行之前就认出来。**
- 人读/机读证据附录：[`gap-taxonomy.md`](gap-taxonomy.md)（能力声明点清单、实测集合差、历史对照、parity 检查脚本骨架、lexicon JSON 草稿、P0 断言清单）。

## 0. 阅读与引用约定

1. **行号基准**：`agent/` 下的行号是 **2026-10-24 19:43–19:57 未提交工作树**上的实测值。同一时间 T1/T2/T3 正在改同一批文件，行号会位移。**检索时以符号名为主、行号为参考**；只引用行号的地方（`data/runs/`、`data/archive/`、`data/events/` 记录）是冻结的。
   - **漂移实测**：20:05 复核同一批符号，`agent/brief.py` 的 `brief_system_prompt` 185→187、`validate_brief` 283→285、`render_brief_md` 542→544、`brief_flow` 791→793、`materialize_scopes` 847→849；`agent/run_windows.py` 的 `_run_integrate` 960→999（T2 继续收尾）。**符号名没变，行号在动**——这正是主文档 §4 要装 parity/一致性守卫的理由之一 `[事实]`。
   - 本文档里凡是"原 `:xxx`"的写法，指的是 T1/T2 落地**之前**的行号，用于对照根因分析写下的判断。
2. **事实 / 推测分界**：每条结论后面标 `[事实]`（有文件行号或 run 字段可追）或 `[推测]`（工程判断，需要下一步验证）。没有标记的句子是结论性归纳。
3. 本文件只写文档，不改代码、测试、配置，不碰编辑器。

---

## 1. 一句话定位与 30 秒判别

### 1.1 定位

> 这次失败不是 UE 技巧问题，也不是模型写错了蓝图，而是 **「能力边界不可见 + 静默降级 + 裁决语义混淆」三合一**：
> **系统知道这段需求做不到，但没有任何一层把「做不到」当成必须改变流程走向的事件。**

拆成三个必需条件（缺一条都不会出现「36 秒跑完 + 人审通过」）：

| 条件 | 本 run 的证据 |
| --- | --- |
| 能力边界不可见 | 范围 B 的 `pending_note` 原话就是「动作表里没有建关卡的动作」（`data/runs/20261024/20261024-192858.json:47`）`[事实]` |
| 静默降级 | 这段判断只换来一句 `block_note`「没有可执行的窗口，只记录待办，不进执行。」（同文件 `:57`、`:79`），流程照走 `[事实]` |
| 裁决语义混淆 | 矩阵 5 项全部 `ok=null`（`:272-298`）⇒ 整扇记 `ok:false` + `stop_reason:"judge_failed"`（`:262`、`:331`），「没证据」被写成「失败」`[事实]` |

归到一类，叫 **capability-gap blind spot（能力缺口盲区）**：表示层已经算出了缺口，流程层没有把缺口当事件。

### 1.2 30 秒判别特征（四条全中 ⇒ 同一类缺陷）

按顺序问，全部是"是"就停止分析 UE 层面，直接查能力表与裁决链：

1. **有效交付条目数 ≫ 有窗口动作数**：任务书 `scopes[].deliverables` 加起来十几条，业务窗口只有 1 扇。
   - 本 run：交付条目 2(A)+2(B)+5(C)+4(集成末行) = **13 条**，业务窗口 **1 扇（W1）**（`20261024-192858.json:14-94`；W2 是代码自动追加的集成扇，`agent/brief.py:1031` 起）`[事实]`
2. **存在 `window_ids: []` 的非集成范围**：`scopes[]` 里 B/C 的 `window_ids` 是空数组（`:143`、`:156`）`[事实]`
3. **集成矩阵 `checked == 0` 或全部 `ok=None`**：summary 里同时出现「0 项通过口径」和「N 项不适用」（`:266`、`.md:19`）`[事实]`
4. **人审 `review.status` 是「通过」而 open scope 非空**：`review: {rounds: 1, status: "通过"}`（`:8-11`）`[事实]`

第 4 条是判别力最强的一条：**审阅通过 + 有范围没窗口 = 人审门没审覆盖**。它同时也说明为什么这次没人拦——人在审一份"形态完整"的任务书。

---

## 2. 缺陷模式库（举一反三）

共 12 条。D1–D10 覆盖任务书点名的全部模式；D11（状态词表）与 D12（替代实现冒充）是本次审计额外发现的两条。字段口径：**现象 / 触发条件 / 在哪些模块 / 可检测信号**。优先级与验收见 §3。

### D1 规划者能力表 ≠ 执行器能力集

- **现象**：执行器有完整分支的动作，规划者提示里从来没有；本次 `create_blueprint` 在 `agent/sequence.py:136` 有执行分支、在 `agent/catalog.py` 的 `DIRECT_ACTIONS`（`:102`）与 `EDITOR_LOCK_ACTIONS`（`:143`）里，但 **不在 `CATALOG`**（`:8-100`，实测于 19:44；T1 前）`[事实]`。「新建关卡」当时连执行器都没有（全仓库 `create_level` 0 处，19:44 实测）`[事实]`。
- **触发条件**：新增/保留一个动作时只改执行器或只改表。任何**只改一处**的提交都会留下这个洞。
- **在哪些模块**：`agent/catalog.py`（`CATALOG` / `DIRECT_ACTIONS` / `READ_ONLY_ACTIONS` / `GRAPH_ACTIONS` / `EDITOR_LOCK_ACTIONS` / `_VERIFY`）、`agent/sequence.py:_run_action`、`agent/plan_windows.py:materialize`（`:568` 的 `allows()` 门）、`agent/brief.py:brief_system_prompt`（`:185`，动作表循环 `:217`）。
- **可检测信号**：集合差非空。**19:44 实测 8 个动作执行器能跑、规划者看不见**：`bind_slot`、`check_pickup`、`create_blueprint`、`place_blueprint`、`place_pair`、`probe_look`、`probe_move`、`probe_play`；T1 落地 `create_level` 后（19:56 实测）仍是这 8 个 `[事实]`。详见附录 A。
- **附带事实（同类的第二张表）**：`brief_system_prompt` 直接遍历 `CATALOG`，而 `CATALOG.system_prompt()` 遍历 `iter_prompt_specs()`（含人审通过的 `promoted` 候选）⇒ **即使候选被 promote，任务书生成器仍然写不出它**，换了一条路径继续"看不见" `[事实]`。

### D2 无法规划 = 静默降级（silent drop），缺 fail-fast 与人工升级回路

- **现象**：一段需求拿不到窗口时，代码只补一句文案就放行：`agent/brief.py` 原 `:236-237` 的 `if not wins and not scope.get("blocked"): scope["block_note"] = "没有可执行的窗口，只记录待办，不进执行。"` `[事实]`。这句话在后续 4 个环节（物化、执行、裁决、人审）里没有一处改变流程走向。
- **触发条件**：模型给出「`deliverables` 非空 + `windows: []` + 非等外部」的范围——也就是**"我看不准但我知道该做什么"**这个最需要人介入的形态。
- **在哪些模块**：`agent/brief.py`（`validate_brief` `:283`、`materialize_scopes` `:847`）、`agent/harness.py:scope_marks`（`:274`）、`agent/run_windows.py` 收尾（`:1624`）。
- **可检测信号**：`scopes[].window_ids == []` 且 `block_note`/`pending_note` 含「只记录待办」；或 events 里 `intent.unresolved` 非空而任务书 JSON 里没有 `unresolved` 字段（本 run 正是如此：`data/events/20261024-192858.jsonl:2` 有两条 unresolved，`20261024-192858.json` 的 brief 段 `:12-113` 无此字段）`[事实]`。
- **本次修复的边界**：T2 已把这句话换成结构化 `capability_gap`（`agent/brief.py` 的 `_GAP_NOTE` `:55`、`capability_gap_note` `:75`、`_capability_gap_record` `:98`、`_rescue_scope_windows` `:129`、`scopes_meta["capability_gap"]` `:997`；`agent/harness.py` 的 `_gap_mark_reason` `:256`、`scope_marks` gap 字段 `:292-315`、`contract_status` 记「未完成」`:326-327`；`agent/run_windows.py:1624-1639` 缺口范围必进 note）。**这治的是"说不说"，不治"谁会管"**：升级项要补的是 ① 缺口必须让本句**停**（不留可执行窗口以外的假进展）、② 缺口进**人审门必须被显式确认**、③ 缺口有 owner 与到期。

### D3 需求动词未覆盖（建关卡/建蓝图/加输入/开火/计分/HUD）与动作表膨胀的矛盾

- **现象**：自然语言需求里的动词在动作表里找不到归属，而动作表只有 21 项（19:56 实测，含 `integrate_verify`/`dispose_residual` 两个代码专用）。实测 MCP 工具面（`data/ue_mcp_index.json`，830 工具，`generated_at: 2026-09-21`）`[事实]`：
  - 关卡：只有 `SceneTools.load_level` / `SceneTools.get_current_level`；**没有** `new_level` / `create_map` / `create_level`；
  - 输入：`input` 相关命中全部是 `MaterialTools.get_expression_input*` 与 Niagara 的 `*InputValues`，**没有** Enhanced Input / InputMapping 工具；
  - 开火类：`spawn/damage/projectile/fire` 命中只有 Sequencer 的 `add_spawnable_*` 与 `PCGToolset.SpawnGraphInstance`，**没有**生成投射物或伤害的工具；
  - 蓝图图：`BlueprintTools.create/create_node/connect_pins/compile_blueprint` 等**都存在**，但动作表里只有 `compile_blueprint`（"不改图"）与 `bind_blueprint`，没有任何"改图"动作。
- **触发条件**：用户用自由发挥的说法提功能（「实现简易的FPS射击游戏，内容你自由发挥」），动词落在表外，而规划者被明确要求「看不准就不要编」（`catalog.py:368`）。
- **在哪些模块**：`agent/catalog.py`、`agent/intent.py:validate_intent`（`:46`，`note_candidate` `:70`）、`agent/plan_windows.py`、`core/graph_write_policy.py`（`UE_ALLOW_*` 门）、`data/ue_mcp_index.json`。
- **可检测信号**：events 的 `intent.unresolved` 条目里逐条都是「动作表无 XXX 动作」/「无法落到具体动作」（本 run 两条都是，`events:2`）`[事实]`。
- **矛盾**：**动作表不能无限膨胀，但"表外"不能等于"悬空"**。所以升级项不是继续加动作，而是引入 **lexicon（术语→动作映射表）+ unplannable 显式态**（§5）。

### D4 三态混一：没证据 / 不适用 / 失败 被塞进同一个 `ok=False`

- **现象**：矩阵项的 `ok` 只有 `true/false/null`，而 `null` 的语义是「本次不适用」；当**全部**项目都是 `null` 时，总判定走 `checked` 为空的分支，一律 `ok=False`，窗口 `stop_reason` 记成 `judge_failed`。本 run：`checked` 0 项、`ok:false`、`stop_reason:"judge_failed"`（`20261024-192858.json:266`、`:262`、`:331`；代码 `agent/run_windows.py:1087-1103`）`[事实]`。
- **触发条件**：整句只做了"基础设施动作"（启动编辑器），没有任何可裁决产物 ⇒ 矩阵 5 项全部不适用。这与"交付了但没过"是两种事，却拿到同一个结论。
- **在哪些模块**：`agent/run_windows.py:_run_integrate`（`:960`，判定 `:1087-1103`）、`agent/harness.py:classify_stop`（`:349`）、`agent/harness.py:STOP_REASONS`（`:14`）。
- **可检测信号**：summary 同时含「0 项通过口径」与「N 项不适用」；或 `stop_reason == "judge_failed"` 而 `matrix` 里 `ok` 非 None 的项数为 0。

### D5 集成矩阵的两个历史极端（全不适用 ⇒ 通过 / 全不适用 ⇒ 失败）说明缺的是显式 evidence 计数与归因

- **现象（两个方向都发生过）**：
  - 20261021-195220「五项空转全绿」：矩阵把「本次没发生」当通过，给一句什么都没做的 run 加了「矩阵全部通过」。修复注释就在 `agent/run_windows.py:967-969`（"只有真的验过且通过才算 ok"）与 `agent/run_windows.py:1045-1049`；回归断言在 `eval/check_harness.py:685-774`（`check_under_delivery_guard`，注释写「19 条交付物、3 个零窗口范围、C++ 窗口一行没改，旧逻辑仍判成功/矩阵全部通过」）`[事实]`。
  - 20261024-192858（本 run）：口径修对了，反向把「全不适用」判成失败，但仍然**没有归因**：读者从 `matrix` 里看不出"哪几段需求没交付"。
- **共同根因**：矩阵项只有 `{item, ok, note}`，**没有 `evidence_count`，没有 `scope_ids`/`window_ids`**（`20261024-192858.json:272-298`，5 项全是这三个字段）`[事实]`。没有计数就只能靠布尔推断，两个极端都是从同一个空洞里长出来的。
- **在哪些模块**：`agent/run_windows.py:_run_integrate`、`agent/harness.py`（条款/窗口绑定 `_window_ids_for` `:110`、`build_criteria` `:156`）、`agent/harness.py:scope_marks`。
- **可检测信号**：矩阵项缺 `evidence_count`；总状态与「有证据的交付条目数」之间存在无法解释的落差。

### D6 人审门只审形态、不审覆盖（任务书看着齐，其实 2/3 范围没窗口）

- **现象**：范围 B/C 一扇窗口都没有，而人审记录是「审阅 1：通过」（`20261024-192858.json:8-11`）`[事实]`。三个成因叠加：
  1. **顺序**：人审发生在物化之前——`agent/run_windows.py:_run_query` 先 `brief_flow`（`:1974`）再 `materialize_scopes`（`:1986`），人审看到的是**未经物化**的任务书，天然的窗口缺失要在确认之后才暴露 `[事实]`；
  2. **人读件没有窗口列**：`render_brief_md`（`agent/brief.py:542`）的表头固定三列「任务范围 / 尚未完成的全部主要内容 / 估时区间」（原 `:447`），落盘件 `data/briefs/20261024-192830-L_NewTest2-FPS.md:7` 同样三列 `[事实]`；
  3. **控制台只在有窗口时才画窗口行**：`_print_brief_pretty` 的 `wins = scope.get("windows") or []` / `if wins:`（`:769-770`，T2 前是 `:659-662`）⇒ 缺口的表现为**空白**，而空白与"一切正常"在人眼里无法区分 `[事实]`。
- **触发条件**：任何"形态完整、覆盖不全"的任务书。
- **在哪些模块**：`agent/brief.py`（`render_brief_md` `:542`、`_print_brief_pretty`、`brief_flow` `:791`）、`agent/run_windows.py:_run_query`（`:1946`）。
- **可检测信号**：`review.status == "通过"` 且存在 `windows == []` 的范围；或人审渲染文本里范围数与"窗口行数"不相等。

### D7 模型输出 schema 合法但语义空洞（`windows: []` + `pending_note`）没有语义校验

- **现象**：B/C 的 JSON 完全合法，`_SCOPE_KEYS`（`agent/brief.py:44-45`）只声明形态字段（id/title/deliverables/pending_note/estimate_hours/depends_on/external_dep/windows/blocked/block_note），`validate_brief`（`:283`）只做三件事：丢未知字段、估时越界改用代码估算、交付清单为空则暂停 `[事实]`。**「有交付、0 窗口、不等外部」这个致命组合不在校验范围内**。
- **触发条件**：模型"诚实"地交出空窗口（本 run 就是这么做的，而且它写得比代码还准：`pending_note` 直接写「动作表里没有建关卡的动作」）。
- **在哪些模块**：`agent/brief.py:validate_brief`、`agent/brief.py:materialize_scopes`（`:847`）。
- **可检测信号**：JSON 全绿而 `deliverables` 数与业务窗口数为 `13 : 1`（本 run 实测；其中集成末行的 4 条不算业务交付）。
- **修法边界**：语义校验只允许**把范围标成缺口/暂停**，不允许替模型编窗口名（编窗口是 §4 代码补窗的职责，且必须有 materialize 证据）。

### D8 能力候选表（`action_candidates`）没有闭环

- **现象**：只有写方，没有读方与人审队列。写方三处：`agent/catalog.py:note_candidate`（`:295`）、`agent/plan_windows.py:materialize` 表外分支（`:568-570`）、`agent/intent.py:validate_intent`（`:70`）。读方只有 `iter_prompt_specs()`（`catalog.py:279`，仅输出 `status == "approved"`）与 `eval/check_distill.py:222-257`（测试用）`[事实]`。
- **更硬的事实**：`data/skills/action_candidates.json` **当前不存在**（`data/skills/` 下只有 `clarify_policy.json`、`failure_patterns.json`、`learned_skills.json`，19:50 实测；`load_candidates()` 对不存在的文件返回 `[]`，`catalog.py:226-238`）`[事实]`。⇒ 缺口既没被记成候选，也没有到期与优先级。
- **触发条件**：任何表外动作或缺口动词。
- **在哪些模块**：`agent/catalog.py`（`load_candidates`/`note_candidate`/`promote_candidate` `:322`/`iter_prompt_specs`）、`agent/intent.py`、`agent/plan_windows.py`、`data/skills/`。
- **可检测信号**：候选文件缺失，或存在 `status == "review"` 且无 `first_seen`/到期字段的记录；`run_windows._emit_action_candidates`（`:1229`，事件 `:1243`）发出的事件没有任何消费方。

### D9 长链任务的部分交付没有「未交付清单」出口到人

- **现象**：用户看到的是「部分完成」，不是「哪几件事没做、为什么」。三处证据：
  1. 集成 note 只有「完成 W1；失败 W2（无下游依赖，不阻塞别的范围）」（`20261024-192858.json:338`、`.md:23`），**没有**范围级原因 `[事实]`；
  2. 范围级原因其实算出来了（`harness.scope_marks` 的 `reason`），但写进 note 的分支被前置条件挡住：`agent/run_windows.py:1640` 是 `elif adjusted == "部分完成" and status == "成功"`——本 run 的 `status` 早在 `:1597` 就已经定成「部分完成」（W1 done + W2 failed 且 W2 无下游依赖），这个分支永不成立，`open_scopes` 的说明（`:1643-1647`）根本不会进入 note `[事实]`；
  3. 人读 run md 只有一行「范围：A 完成；B 未开始；C 未开始」（`.md:24`），没有"缺什么/缺哪个动作" `[事实]`。
- **同类第二处**：理解器的 `unresolved`（原始半句）**进不了任务书文档**：`brief_flow`（`:791`）不接收 intent，`generate_brief`（`:514`）签名里没有 `unresolved`，而 `render_brief_md` 只在 `brief["unresolved"]` 存在时才渲染「尚未完成清单」（`:592` 取 `brief.get("unresolved")`，`unfinished_text` `:598`）⇒ 本 run 的任务书 md 里完全没有这个章节（`data/briefs/20261024-192830-L_NewTest2-FPS.md` 共 21 行，只有「外部依赖」「总工作量」两节）`[事实]`。
- **触发条件**：任何非「成功」收尾的长链 run。
- **在哪些模块**：`agent/run_windows.py`（收尾 `:1580-1649`、`_write` `:1223`）、`agent/brief.py`（`unfinished_text` `:598`、`render_brief_md` `:542`、`brief_flow` `:791`）、`agent/ledger.py`。
- **可检测信号**：run md 里"未开始/未完成"的范围数 > 0，而同文件里这些范围的原因文本数为 0。

### D10 归档证据被二次覆盖（审计额外发现）

- **现象**：`_run_integrate` 把 5 项矩阵归档（`agent/run_windows.py:1083`，`_archive_run(state, hashes=hashes, matrix=matrix)`），run 收尾又调一次 `_archive_run(state, md_path=path)`（`:1734`，**不带 `matrix`/`hashes`**），`_archive_run`（`:1188-1201`）用 `matrix or []` 直接覆盖同名文件 ⇒ 归档件 `data/archive/20261024-192858/deliverables.json:3-4` 是 `"hashes": [], "matrix": []`，而 `20261024-192858.json:272-298` 明明有 5 项矩阵 `[事实]`。
- **触发条件**：任何带集成扇的 run（几乎全部）。
- **在哪些模块**：`agent/run_windows.py:_archive_run` 与两个调用点。
- **可检测信号**：`archive/<run>/deliverables.json` 的 `matrix` 长度 ≠ `runs/<run>.json` 里集成扇 `detail.matrix` 的长度。**这条影响所有"用归档件做回归证据"的离线断言**——包括 T3 正在写的那几条。

### D11 状态词表未登记（新增状态会漂）

- **现象**：T2 给整句引入了新状态「未完成」（`agent/harness.py:327`），但状态词表分散在至少四处：`agent/harness.py:14`（`STOP_REASONS`）、`agent/run_windows.py:78`（`_MD_STATUS`，窗口状态中文名）、`agent/brief.py:616`（台账 `{"done": "完成", "partial": "部分完成", "pending": "未完成"}`）、`agent/ledger.py` 的 `status` 字段 `[事实]`。
- **触发条件**：以后任何一次"加一个状态"只改一处。
- **可检测信号**：渲染/台账里出现未映射的裸状态串；或 run md 的状态与 `_MD_STATUS` 的用词不一致。
- **优先级低但与 D1 同源**：都是**同一概念有多个声明点、没有守卫**。

### D12 一个能力只能用替代实现冒充（模板复制 ≠ 新关卡）

- **现象**：T1 的 `create_level` 落地后，"新建关卡"的实际实现是 `AssetTools.duplicate` 复制模板 + `SceneTools.load_level`（根因分析第 7 节已写明）`[事实]`；而 MCP 830 工具面**没有** `new_level` / `create_map`（19:56 实测，只有 `SceneTools.load_level` / `get_current_level`）`[事实]`。于是"新建一个空关卡"这个需求只能被"复制一份带内容的模板关卡"满足——**需求被替代实现悄悄改写**，且用户不知道。
- **触发条件**：请求的能力在工具面没有一等实现，但有一个"差不多"的替代路径。
- **在哪些模块**：`agent/sequence.py`（`create_level` 分支）、`agent/catalog.py`（action summary 的 caveat 文案）、`data/ue_mcp_index.json`、UE 插件侧源码（本仓库之外：`AIAgentProject/Plugins/`）`[推测：插件侧是唯一能加 `new_level` 的地方]`。
- **可检测信号**：动作 summary / 结果 summary 里出现「模板」「复制自」而原句说的是「新建」；或新关卡继承了模板里的 Actor（模板 `/Game/Maps/L_NewTest` 里已有内容）。
- **为什么是 P2 而不是 P0**：它有诚实的替代实现、有 `UE_LEVEL_TEMPLATE` 可覆盖，不产生"静默蒸发"，所以排在插件侧升级批次；但前提是 D1–D11 的**显式声明**已经到位——即 summary 必须写"复制模板"（§7 不做清单第 5 条）。

---

## 3. 逐项处置：优先级 / 改动面 / 验收 / 风险与回滚 / 工作量

### 3.1 汇总

| 编号 | 名称 | 优先级 | 主要改动面 | 工作量 | 离线验收（P0 必须有） |
| --- | --- | --- | --- | --- | --- |
| D1 | 能力表 ≠ 执行器（parity） | **P0** | `agent/catalog.py`、`agent/brief.py:brief_system_prompt`、新增 `agent/actions.py`(可选)、`eval/check_catalog_parity.py` | 守卫 1–2h；单源化 4–8h | `eval/check_catalog_parity.py` |
| D2 | 静默降级 → 缺 fail-fast 与升级回路 | **P0** | `agent/brief.py`（`_rescue_scope_windows` 之后）、`agent/run_windows.py:1624`、`agent/board.py` | 2–4h | `eval/check_escalation.py` |
| D3 | 需求动词未覆盖（lexicon） | P1 | `data/skills/lexicon.json`、`agent/plan_windows.py`、`eval/check_lexicon.py` | 2–3h | （P0 子集：`check_escalation.py`） |
| D4 | 三态混一 | **P0** | `agent/run_windows.py:_run_integrate`、`agent/harness.py:classify_stop`/`STOP_REASONS` | 2–4h | `eval/check_verdict_tristate.py` |
| D5 | 缺 evidence 计数与归因 | **P0** | `agent/run_windows.py:_run_integrate`、`agent/harness.py:scope_marks` | 3–5h | `eval/check_evidence_count.py` |
| D6 | 人审只审形态不审覆盖 | **P0** | `agent/brief.py`（`render_brief_md`/`_print_brief_pretty`/`brief_flow`）、`agent/run_windows.py:_run_query` | 3–5h | `eval/check_review_coverage.py` |
| D7 | schema 合法但语义空洞 | **P0** | `agent/brief.py:validate_brief` | 2–3h | `eval/check_brief_semantics.py` |
| D8 | 候选表无闭环 | P1 | `agent/catalog.py`、`data/skills/action_candidates.json`、`eval/check_candidate_aging.py` | 3–5h | — |
| D9 | 部分交付无未交付清单出口 | **P0** | `agent/run_windows.py:1580-1649`/`_write`、`agent/brief.py`（`unresolved` 通路） | 3–5h | `eval/check_undelivered_outlet.py` |
| D10 | 归档证据被二次覆盖 | **P0** | `agent/run_windows.py:_archive_run` 两处调用点 | 0.5–1h | `eval/check_archive_fidelity.py` |
| D11 | 状态词表未登记 | P1 | `agent/harness.py`、`agent/run_windows.py:78`、`agent/brief.py:616`、新增 `agent/statuses.py` | 1–2h | — |
| D12 | 能力只能用替代实现冒充（模板复制） | P2 | `agent/sequence.py`（`create_level` 分支）、`agent/catalog.py`（caveat 文案）、`AIAgentProject/Plugins/`（插件侧 `new_level`） | 文案 0.5h；插件侧 1–3 天 | 插件侧落地后：`eval/check_create_level.py` 增"空关卡"断言 |

**P0 合计 8 条，约 16–27 人时（2–3.5 人日）。** P1 三条合计约 6–10 人时，可与真实 run 并行。P2 一条（D12 的插件侧部分）不进本次升级窗口。

### 3.2 P0 逐条：验收断言 / 风险与回滚

> 断言写法沿用仓库既有 `eval/check_*.py` 的风格（`import` 真函数 + `assert` + 打印 `xxx ok`），不打开编辑器、不调模型。骨架见 [`gap-taxonomy.md`](gap-taxonomy.md) 附录 D。

#### D1 parity 守卫

- **验收断言**（`eval/check_catalog_parity.py`）：
  1. `assert EXECUTOR_SET - PLANNER_SET <= CODE_ONLY_ALLOWED`，其中白名单每条必须写明插入点（今天 8 条，见附录 A）；
  2. `assert PLANNER_SET - EXECUTOR_SET == set()`（图形动作经 `GRAPH_ACTIONS` 白名单）；
  3. `assert {a for a in PLANNER_SET} - {a for a in PLANNER_SET if a in _KIND_BY_ACTION}` 中每一项都在只读/启动白名单里（即"能写东西的动作必须有 evidence kind"）；
  4. `assert set(_KIND_BY_ACTION.values()) <= set(EVIDENCE_KINDS)`；
  5. **反向探测**：`brief_system_prompt()` 里出现的动作名集合 == `{name for name, _ in catalog.iter_prompt_specs()}`（这条直接钉死 D1 的"第二条看不见的路径"）。
- **风险**：守卫太严会把"代码插入扇"判成违规，导致 T1 式的补窗代码被误伤。
  **回滚**：`CODE_ONLY_ALLOWED` 是显式白名单文件（不是逻辑），收紧/放宽都只改一行；守卫本身是只读检查，失败只影响 CI，不影响运行时的 `allows()` 门。
  **反向风险**：白名单被当垃圾桶——所以断言 1 里每条必须带 `site` 字段，且断言 `site` 指向的函数在 `agent/` 里真实存在。

#### D2 fail-fast 与人工升级回路

- **验收断言**（`eval/check_escalation.py`）：
  1. 用本次真实任务书 `data/briefs/20261024-192830-L_NewTest2-FPS.json` 走 `materialize_scopes` ⇒ `assert any(s["capability_gap"] for s in scopes_meta if s["id"] in ("B","C"))`；
  2. `assert contract_status([], "成功", scope_view) == "未完成"` 且**不等于**「部分完成」；
  3. `assert run_status not in ("成功", "部分完成")` ⇒ 人审门必须被要求二次确认（用假的 `reader` 回答空串，断言流程进入"必须显式确认缺口"分支，而不是直接执行）；
  4. **反例守卫**：一个正常 run（有真实裁判通过、无零窗口范围）仍记「成功」——不许把缺口机制写成"永远未完成"。
- **风险**：把太多范围判成缺口 ⇒ 正常任务开始要求人工确认，退化成人肉开关。
  **回滚**：判定条件严格限制为「`deliverables` 非空 + `windows` 空 + 非 `blocked` + 无 `external_dep` + 非集成末行」（已经是 T2 `_zero_window_scope` 的形状，`agent/brief.py:59`），不放宽；二次确认可用环境变量旁路（`UE_ESCALATE_GAPS=false` 时只记录不阻塞）——但**不允许它在真实 run 里默认关闭**。

#### D4 三态/四态裁决

- **验收断言**（`eval/check_verdict_tristate.py`）：
  1. 复现本 run 的矩阵形态（5 项全 `ok=None`，无缺口范围）⇒ `assert verdict == "no_evidence"` 且 `stop_reason != "judge_failed"`；
  2. `assert matrix[i]["verdict"] in {"passed","failed","not_applicable","no_evidence"}`，四值互斥且穷尽；
  3. 有缺口范围时 ⇒ `verdict == "no_evidence"` 且 summary 含缺口范围 id；
  4. **反例守卫**（沿用 `eval/check_harness.py:773`）⇒ 只有不适用项的矩阵**永远** `ok is False`，不许被"三态化"改成通过。
- **风险**：三态化被误用成"把失败降级成 no_evidence"来提成功率。
  **回滚**：`ok` 字段语义保持不变（`no_evidence` 仍是 `ok=False`），三态只加在 `verdict` 与 `stop_reason` 上——这是**加法**，删掉只是少一个字段。
  **红线**：`not_applicable` 与 `no_evidence` 都不撑成功（§7 不做清单第 2 条）。

#### D5 evidence 计数与归因

- **验收断言**（`eval/check_evidence_count.py`）：
  1. `assert all("evidence_count" in item and "scope_ids" in item for item in matrix)`；
  2. 本 run 复现 ⇒ 每个矩阵项 `evidence_count == 0`，且 `scope_ids` 精确等于未交付的范围集合 `{"B","C"}`；
  3. `assert sum(item["evidence_count"] for item in matrix) == len([c for c in criteria if c["passed"]])`（计数与合同口径一致，防止另起一套统计）；
  4. 归因覆盖：`assert set(item["scope_ids"] for item in matrix if item["evidence_count"] == 0) == {未交付范围}`。
- **风险**：计数口径与其他统计（合同 `passed`、窗口 `done`）不一致，出现"三套数字"。
  **回滚**：`evidence_count` 由 `criteria` 直接派生（断言 3 就是防这个），不作为独立真值；不一致时以 `criteria` 为准。

#### D6 人审覆盖校验

- **验收断言**（`eval/check_review_coverage.py`）：
  1. 用本 run 的任务书 JSON 渲染人审文本 ⇒ `assert "窗口：0" in text or "能力缺口" in text`，且 B、C 两行的覆盖列非空；
  2. `assert review_status != "通过"` 在存在零窗口范围时（即人审门必须先看见缺口才可能通过）；
  3. **顺序断言**：`brief_flow` 返回前必须先做一次 dry-run 物化（`materialize_scopes` 的只读预演），并断言预演结果的 `window_ids` 与人审显示的覆盖数一致；
  4. 反例守卫：每范围都 ≥1 可跑窗口的正常任务书，人审文本与旧版逐字一致（不加噪音）。
- **风险**：dry-run 物化有副作用（今天 `materialize_scopes` 会调 `note_candidate` 写文件，`plan_windows.py:570`）。
  **回滚**：dry-run 必须走"不写盘"开关（`note_candidate(..., dry_run=True)` 或先在临时目录物化）；如果做不到，退化成"只算窗口数、不物化"的静态体检（用同一套 `_rescue_scope_windows` 的纯函数部分）。**这条是实现顺序的先决条件，必须先落地再改人审门。**

#### D7 语义不变量

- **验收断言**（`eval/check_brief_semantics.py`）：
  1. `validate_brief({"scopes":[{"id":"B","deliverables":["x"],"windows":[]}]}, q)` ⇒ `assert scope["capability_gap"]`（或 `blocked=True` + 缺口原因），**不允许**静默通过；
  2. `assert not any(s["deliverables"] and not s["windows"] and not s.get("capability_gap") and not s["blocked"] and not s["external_dep"] for s in brief["scopes"])`；
  3. 本 run 的真实 JSON 重放 ⇒ B/C 都命中缺口路径（`eval/check_failure_192858.py` 已有同向断言，本项只加"不变量"口径）；
  4. 反例守卫：`external_dep` 非空 / `blocked=True` / 集成末行三种正常情形**不得**被判成缺口。
- **风险**：语义校验拦掉合法的"先规划、后补窗口"（例如范围窗口引用候选 id 的情况，`materialize_scopes` `:785-803` 会按候选补窗）。
  **回滚**：校验在**补窗尝试之后**执行（顺序：`validate_brief` → 记缺口候选 → `materialize_scopes` 补窗 → 仍为空才判缺口）；校验本身是纯函数，可单独关掉。

#### D9 未交付清单出口

- **验收断言**（`eval/check_undelivered_outlet.py`）：
  1. 复现本 run ⇒ `assert "未交付" in run_md_text`，且文本里同时出现 `B`、`C` 与各自原因（"没有可执行的窗口"/"能力缺口"）；
  2. `assert run_json["undelivered"] == [{"scope":"B","deliverables":[...],"reason":"..."},{"scope":"C",...}]`（新增结构化字段，机读）；
  3. `assert any("新建一个关卡" in line or "FPS" in line for line in brief_md_unresolved_section)`——即 `intent.unresolved` 必须进入任务书 md 的「尚未完成清单」（今天完全没有这个章节，`data/briefs/20261024-192830-L_NewTest2-FPS.md` 21 行）；
  4. 反例守卫：全部范围完成时，run md **不出现**「未交付」段。
- **风险**：新增 `undelivered` 字段与既有 `ledger.pending` 重复，出现两个真值。
  **回滚**：`undelivered` 由 `scope_marks` 派生（同一个 `reason` 源），台账仍写 `ledger.json`，字段只用于渲染与断言；两处不一致时以 `scope_marks` 为准。

#### D10 归档保真

- **验收断言**（`eval/check_archive_fidelity.py`）：
  1. 跑一次带集成扇的离线 run（mock `run_task`，参考 `eval/check_harness.py:740-774` 的写法）⇒ `assert len(archive_deliverables["matrix"]) == len(run_json["windows"][integrate_id]["detail"]["matrix"])`；
  2. `assert archive_deliverables["run_id"] == run_json["run_id"]`；
  3. 幂等：连续归档两次，第二次不得把非空字段写成空；
  4. 存量回归：`assert data/archive/20261024-192858/deliverables.json["matrix"] == []`（**钉住已知坏档**，防止有人手工"修"它而不改代码）——这条建议写成注释式断言，见 §3.4。
- **风险**：合并逻辑把旧 run 的矩阵串到新 run 上。
  **回滚**：合并前校验 `run_id` 相同；不同则整份覆盖。

### 3.3 P1 / P2 逐条：验收断言 / 风险与回滚

#### D3 lexicon（P1）

- **验收断言**（`eval/check_lexicon.py`，4 条见 [`gap-taxonomy.md`](gap-taxonomy.md) 附录 E.1）：① 每行"指向动作"与 `unplannable` 二选一；② `unplannable` 必带 `reason`；③ lexicon 里的动作名 ⊆ `CATALOG`（不放宽 D1）；④ 覆盖断言——本 run 的两条 `unresolved`（建关卡 / FPS 玩法）必须分别被"动作行"与"unplannable 行"接住。
- **风险**：lexicon 变成第二张动作表，与 `CATALOG` 漂移；或有人把需要的动作偷偷写成 `unplannable` 来回避实现。
  **回滚**：断言 ③ 把 lexicon 钉在 `CATALOG` 之下；`unplannable` 行必须带 `suggested_path`，并且**每次出现都必须有对应的候选记录**（D8），否则 CI 红。
- **工作量**：落表 + 断言 + 覆盖真实 run 的 unresolved 文本，2–3h。

#### D8 候选闭环（P1）

- **验收断言**（`eval/check_candidate_aging.py`）：① 每条候选有 `first_seen`/`last_seen`/`run_ids`/`hits`；② 存在超过阈值（建议 14 天）仍是 `status == "review"` 的候选时，**run 收尾必须出声**（一条 `action_candidate_stale` 事件 + 人审门一行），不许静默；③ `promote_candidate` 仍要求 `reviewed=True` + 非空 `verification`（`catalog.py:322-361`），不许加默认值；④ 本 run 的缺口动词（建关卡 / FPS 玩法类）必须在候选表里有记录或已被 lexicon 显式覆盖。
- **风险**：候选表变成垃圾桶，噪音淹没真需求。
  **回滚**：`hits` 按 run 计数、按 `source` 分组，阈值只控制"出声"不控制"入库"；人审可一键 `dismiss`（新状态 `dismissed`，仍不许自动 promote）。
- **工作量**：3–5h。

#### D11 状态词表（P1）

- **验收断言**（`eval/check_statuses.py`）：① 新增 `agent/statuses.py` 声明 `RUN_STATUSES` / `WINDOW_STATES` / `LEDGER_STATUSES` / `STOP_REASONS`；② `assert set(_MD_STATUS) == WINDOW_STATES`；③ `assert set(STOP_REASONS) <= RUN_STATUSES | WINDOW_STATES`；④ `assert set(brief.py:616 的 label) == LEDGER_STATUSES`；⑤ run md 渲染出的状态串必须能反查回词表。
- **风险**：把状态词表集中后，某处依赖了"任意字符串都能渲染"的宽松行为。
  **回滚**：渲染层保留 `dict.get(status, status)` 的回落（`run_windows.py:948`、`:2007` 已有这种写法），断言只覆盖"已知集合"。
- **工作量**：1–2h。

#### D12 替代实现的诚实声明（P2 的可做部分）

- **验收断言**：① `create_level` 的 `summary`（`catalog.py` 内）必须含"复制模板"字样，且 `assert "模板" in summary`；② `eval/check_create_level.py` 里加一条：`assert llm_visible_summary 不含"新建空关卡"这类主张`（防止文案反过来夸大）。
- **风险**：文案改动是低风险；真正的风险在插件侧 `new_level`（编辑器稳定性、要重生成索引、要重启编辑器）。
  **回滚**：保留 `duplicate` 分支作为回落（`UE_LEVEL_TEMPLATE` 指向模板），插件侧失败只影响"空关卡"这一条断言，不影响已有链路。
- **工作量**：文案 + 断言 0.5h；插件侧 1–3 天（不进本次升级窗口）。

### 3.4 一条特别说明：D10 的"坏归档"

`data/archive/20261024-192858/deliverables.json` 是**已存在的坏档**（matrix 被覆盖成空）。升级时不要手工编辑它：

- 它是**证据**（证明覆盖 bug 真实发生过），也是离线断言的目标；
- 手工补齐会同时破坏"归档件 = 代码产物"的信任前提；
- 正确做法：改 `_archive_run` 的调用点 + 加断言，让**下一次 run** 的归档件自动正确，坏档留在原地并在 `gap-taxonomy.md` 附录 B 里登记。

---

## 4. 「能力表 vs 执行器」parity 守卫最小设计草案

### 4.1 现实：一个动作名散落在 13 处

实测（19:56:33，脚本见 [`gap-taxonomy.md`](gap-taxonomy.md) 附录 A）`[事实]`：

| # | 声明点 | 位置（基准工作树） | 今天的内容规模 |
| --- | --- | --- | --- |
| 1 | 规划者可见动作表 `CATALOG` | `agent/catalog.py:8-100` | 21 |
| 2 | 直接执行白名单 `DIRECT_ACTIONS` | `agent/catalog.py:102` | 23 |
| 3 | 只读白名单 `READ_ONLY_ACTIONS` | `agent/catalog.py:127` | 6 |
| 4 | 图形动作 `GRAPH_ACTIONS` | `agent/catalog.py:136` | 4 |
| 5 | 编辑器锁 `EDITOR_LOCK_ACTIONS` | `agent/catalog.py:143` | 24 |
| 6 | 验证声明 `_VERIFY` | `agent/catalog.py:169` | 21 |
| 7 | 动作→证据类型 `_KIND_BY_ACTION` | `agent/harness.py:20` | 18 |
| 8 | 证据类型全集 `EVIDENCE_KINDS` | `agent/harness.py:13` | 6 |
| 9 | 执行器分支 `sequence._run_action` | `agent/sequence.py:128-273` | 22 |
| 10 | 场景写/源码写动作 | `agent/run_windows.py:84`、`:88` | 9 / 1 |
| 11 | 估时表 `_ACTION_ESTIMATE` | `agent/brief.py:21` | 20 |
| 12 | 裁判分支 `core/judge.judge` | `core/judge.py:544-555` | 4 |
| 13 | 归地动作 `_ASSET_GROUND_ACTIONS` | `agent/plan_windows.py:603` | 3 |

**13 处、最多 29 个不同动作名，没有一处是自动推导的。** 这就是 D1 的载体：不是"忘了加 create_level"，而是**加一个动作要记得改 13 个地方**。

> **现场证据（最有说服力的一条）**：19:56:33 实测，T1 在途期间出现过瞬时不一致——`create_level` 已经在 `CATALOG`（`:31`）、`DIRECT_ACTIONS`（`:123`）、`EDITOR_LOCK_ACTIONS`（`:166`）、`_VERIFY`（`:183`）里，**但 `sequence._run_action` 还没有分支，`_KIND_BY_ACTION` 也没有它**。
> 换句话说：**此刻跑一次 parity 守卫会红**。这不是谁写错了，而是"改 13 处、无守卫"的必然中间态。T1 收口后会绿——但下一个动作还会重演。`[事实]`

### 4.2 最小设计：单一声明 + 双向探测 + 七件套准入

**目标**：一个动作的"是什么"只写一次；两张自动推导的差集必须为空。

**接口级草案**（新增 `agent/actions.py`，其余模块改为从它派生；不改运行时行为，只换真值来源）：

```python
# agent/actions.py —— 动作的唯一声明源。运行时与离线守卫都读这里。
ACTION = {                       # 一个动作一条记录，字段全部必填（None 显式写 None）
    "name": "create_level",
    "slots": ("level",),
    "summary": "...",            # 进规划者提示的一句话
    "planner_visible": True,     # → CATALOG / iter_prompt_specs / brief_system_prompt
    "direct": True,              # → DIRECT_ACTIONS
    "editor_lock": True,         # → EDITOR_LOCK_ACTIONS
    "read_only": False,          # → READ_ONLY_ACTIONS
    "graph": False,              # → GRAPH_ACTIONS
    "scene_write": True,         # → _SCENE_WRITE_ACTIONS
    "source_write": False,       # → _SOURCE_WRITE_ACTIONS
    "verify": ("关卡资产存在，且是当前关卡",),   # → _VERIFY
    "evidence_kind": "editor_assert",           # → _KIND_BY_ACTION；None 表示"代码专用"
    "judge": "core",             # "core" | "synthetic:<模块:函数>" | "none"
    "estimate_hours": (1.0, 4.0),# → _ACTION_ESTIMATE
    "asset_ground": False,       # → _ASSET_GROUND_ACTIONS
    "inserted_by": None,         # 代码专用扇写插入点，例如 "plan_windows._create_and_goal"
    "since": "2026-10-24",
    "tests": ("eval/check_create_level.py",),
}
ACTIONS: dict[str, dict] = {...}
def catalog() -> dict: ...        # planner_visible and not code_only
def direct_actions() -> frozenset: ...
def kind_by_action() -> dict: ...
```

兼容层（避免一次性大改）：`catalog.CATALOG = catalog()`、`catalog.DIRECT_ACTIONS = direct_actions()`、`harness._KIND_BY_ACTION = kind_by_action()`（保持既有 `import` 不变）。

**自动探测（两个方向）**：

```python
# 方向 1：执行器支持但规划者看不见 —— 静态解析执行器分支，不信人手维护的表
EXECUTOR_SET = ast_walk_actions("agent/sequence.py", func="_run_action")
PLANNER_SET  = {name for name, _ in catalog.iter_prompt_specs()}

I1: EXECUTOR_SET - PLANNER_SET <= CODE_ONLY_ALLOWED      # 白名单每条必须带 inserted_by
I2: PLANNER_SET - (EXECUTOR_SET | GRAPH_ACTIONS) == set()  # 规划者能写，执行器必须认

# 方向 2：规划者能写但执行器不认 / 能写但没裁判
I3: for a in PLANNER_SET - READ_ONLY_ACTIONS - {"launch_editor"}:
        assert ACTIONS[a]["evidence_kind"] or ACTIONS[a]["judge"].startswith("synthetic:")
I4: set(kind_by_action().values()) <= set(EVIDENCE_KINDS)
I5: ACTIONS[a]["tests"] 非空 且 文件存在        # 七件套最后一项
```

方向 1 的可行性**已经在本文件写作过程中被验证**：附录 A 的集合差就是用 `ast` 解析 `sequence.py` 的 `action ==` / `action in` 分支跑出来的，不依赖任何人工表。

**"合成裁判"是新发现的一类**：`widget_graph` 这个 `EVIDENCE_KINDS` 成员**不在 `core/judge.judge` 的分支里**（只有 4 个 kind，`core/judge.py:544-555`），它是 `agent/run_windows.py:864-866` 现造的一条判决（"控件图扇跑完并编译通过"）。所以准入清单里"verification kind"这一项必须能表达 **`judge: "synthetic:<模块:行>"`**，否则 `create_widget_blueprint`（在 `CATALOG` 里、`_KIND_BY_ACTION` 里没有）这类动作会被守卫误判成"没裁判"。

### 4.3 新增动作准入清单（七件套）

任何新动作合入前，七项全绿；缺一项就不许进 `CATALOG`：

| # | 项 | 今天落在哪 | 缺了会怎样 | 守卫断言 |
| --- | --- | --- | --- | --- |
| 1 | `CATALOG`（规划者能看见） | `agent/catalog.py:8-100` | 模型永远写不出这个动作（D1 本体） | I2 |
| 2 | `DIRECT_ACTIONS` / `EDITOR_LOCK_ACTIONS` | `agent/catalog.py:102` / `:143` | `materialize` 的 `allows()` 门放行但调度不认 | `assert a in direct or a in editor_lock` |
| 3 | `_VERIFY`（准入声明） | `agent/catalog.py:169` | 合同里没有这条动作的验收口径 | `assert _VERIFY[a]` 非空 |
| 4 | `sequence._run_action` 分支 | `agent/sequence.py:128-273` | **能规划、不能执行**（最坏的一种：假成功） | I1/I2 |
| 5 | `verification kind` | `core/judge.py:544-555` 或合成点 `run_windows.py:864` | 跑完没有任何裁判，只能靠模型自述 | I3 |
| 6 | `_KIND_BY_ACTION` 绑定 | `agent/harness.py:20` | 条款绑不到这一扇（`build_criteria` 静默跳过，`:173-175`） | I3/I4 |
| 7 | 离线测试 | `eval/` + `.github/workflows/offline.yml` | 下一次重构静默破坏它 | I5 |

**顺带覆盖**：`_ACTION_ESTIMATE`（`agent/brief.py:21`）、`_SCENE_WRITE_ACTIONS`（`agent/run_windows.py:84`）、`_ASSET_GROUND_ACTIONS`（`agent/plan_windows.py:603`）、`core/graph_write_policy` 的开关映射（`:20-30`）不进七件套，但由 I1–I5 派生（见 `actions.py` 字段），不再手工维护。

---

## 5. 术语→动作映射（lexicon）草案

### 5.1 为什么需要它

动作表不能无上限扩张（每个动作 13 处声明 + 一套裁判 + 一个测试）。所以需要一张**独立的、可审的映射表**：它把用户动词映射到"已有动作"或**显式 `unplannable`**，后者带原因与建议路径。表的维护者是**人**，代码只读；模型不允许写它。

**落盘位置（建议）**：`data/skills/lexicon.json`（与 `learned_skills.json`/`clarify_policy.json` 同目录，已经被 `_skills_dir_file` 机制支持），机读、可 diff、可 CI 校验。

### 5.2 草案表

| 需求动词 / 短语 | 映射 | 依据（工具面/代码） | 处置 |
| --- | --- | --- | --- |
| 新建关卡 / 建一个关卡 / 新建地图 / L_xxx | `create_level`（T1 已补） | 工具面无 `new_level`；实现是 `AssetTools.duplicate` 复制模板 + `SceneTools.load_level` | 执行，但 summary 必须写明**模板复制**而非空白关卡；真空白关卡 → `unplannable`（需插件侧 `SceneTools.new_level`） |
| 创建蓝图 / 新建蓝图 / BP_xxx | `create_blueprint` | 执行分支 `sequence.py:136`；当前由代码插入（`plan_windows.py:552`） | 执行；升级项 D1 后转为规划者可见 |
| 打开/启动编辑器 | `launch_editor` | `CATALOG:9` | 执行 |
| 编译某张蓝图 / 编译工程 | `compile_blueprint` / `compile_project` | `CATALOG:14`/`:18` | 执行 |
| 改蓝图图 / 加节点 / 连线 | `unplannable`（表内无"改图"动作） | `BlueprintTools.create_node/connect_pins` 存在，但动作表里 `bind_blueprint` 明写"不改图"（`catalog.py:29`） | 缺口 + 建议走 `UE_ALLOW_BLUEPRINT` 门的人工图路径（`core/graph_write_policy.py`），或在 `Plan/` 写人工步骤 |
| 加输入 / 输入映射 / WASD / 按键 / 鼠标 | `unplannable` | 830 工具面**无** Enhanced Input 工具（19:56 实测） | 缺口 + 记候选（`intent_action`）；建议 `edit_cpp` 或人工图；不得编动作名 |
| 开火 / 射击 / 命中 / 子弹 / 伤害 | `unplannable` | 830 工具面**无** spawn/projectile/damage 工具（实测命中只有 Sequencer/PCG） | 同上 |
| 计分 / HUD / 文本反馈 / 结算 | `create_widget_blueprint` + `edit_widget_blueprint`（复合） | `CATALOG:72`/`:77`，受 `UE_ALLOW_UMG` 门（`plan_windows.py:410`/`:446`） | 执行或等外部（`external_dep: UE_ALLOW_UMG`）；核对用 `check_platform_hud`（只读） |
| 移动 / 移到 / 坐标 | `move_actor` | `CATALOG:55` | 执行 |
| 摆放 / 布置 / 放进去 / 批量摆 | `place_actor` / `place_batch` | `CATALOG:31`/`:35` | 执行 |
| 删掉多余的 / 离群实例 | `remove_batch` | `CATALOG:41` | 执行 |
| 材质参数 | `set_scalar_param` / `query_material` | `CATALOG:63`/`:68` | 执行 |
| 跑测试 / 验证 / 自动化 | `run_automation_test` | `CATALOG:82` | 执行（只读） |

### 5.3 谁维护、怎么改

- **维护者**：人（工程 owner / Lead）。模型可以**提议**（写进候选表，D8），但不能改 lexicon。
- **改动准入**：每条 lexicon 行要么指向一个**已过七件套**的动作，要么是 `unplannable` 且带 `reason` + `suggested_path`。两者都不许留空。
- **机读校验**（`eval/check_lexicon.py`）：
  1. `assert all(row["action"] in CATALOG or row["unplannable"] for row in LEXICON)`；
  2. `assert all(row.get("reason") for row in LEXICON if row["unplannable"])`；
  3. **覆盖断言**：对真实 run 的 `intent.unresolved` 文本，每一个动词至少被一条 lexicon 行匹配（匹配不上 ⇒ 输出"未覆盖动词"，CI 红）；本 run 的两条 unresolved 必须被 B/C 两行接住；
  4. `assert set(row["action"] for row in LEXICON if not row["unplannable"]) <= set(CATALOG)`（lexicon 不许发明动作，与 D1 的守卫咬合）。

### 5.4 `unplannable` 时的标准处置（五步，不许省略）

1. **暂停**：该范围不生成窗口、不替模型编动作、不执行兄弟范围以外的伪动作（`materialize` 的 `allows()` 门是硬门，`plan_windows.py:568`）。
2. **结构化缺口**：写 `capability_gap`（`reason` / `deliverables` / `missing_actions` / `pending_note`）——T2 已落地（`agent/brief.py:93`）。
3. **点名**：人审门必须出现「能力缺口」字样 + 范围 id + 缺什么（T2 已让任务书话说清，`brief.py:70`；**待办**：人审表加覆盖列，D6）。
4. **升级人工**：本句**不得自动进入执行**；必须有人在审阅时显式确认"这段我不做"（**待办**，D2）。确认与拒绝都写事件流。
5. **记候选**：`note_candidate(name, source="lexicon", ...)`（`catalog.py:295`）写进候选表，带 `first_seen` 与来源 run（**待办**，D8）。

**红线**：`unplannable` **不等于**"待办"。待办是"我知道怎么做、这次没排上"；unplannable 是"系统没有这条路径"——前者可以放行，后者必须停。

---

## 6. 交付节奏建议

### 6.1 本次修复（T1/T2/T3）与升级项的关系

| 本次修复 | 观测状态（2026-10-24 19:50–19:57） | 它治的是 | 治不了什么（→ 升级项） |
| --- | --- | --- | --- |
| **T1** `create_level` 全链路 | 部分落地：`catalog.py:31/123/166/183`（CATALOG/DIRECT/LOCK/_VERIFY）、`plan_windows.resolve_slots` 有分支、`supervise.py:1089+`、`brief.py:124` 调 `scope_windows`；19:56 实测 `sequence._run_action` 与 `_KIND_BY_ACTION` **尚无** create_level（收口中） | 补齐**一个**动作 | 补一个动作修一次 → **D1 parity 守卫**；`create_level` 是模板复制 → §6.3 阶段 3 的插件侧项 |
| **T2** 零窗口不静默 + 集成三态 + 能力缺口记录 | 已落地：`brief.py` `_GAP_NOTE` `:55`/`capability_gap_note` `:75`/`_rescue_scope_windows` `:129`、`harness.py:256/292-315/326-327`、`run_windows.py:1097/1624-1639` | 缺口**说出来**、状态记「未完成」 | 谁来管缺口（D2 第 4 步）、人审覆盖（D6）、evidence 计数（D5）、未交付清单（D9） |
| **T3** 离线套件 | 19:55 实测 `eval/` 下还没有 `check_create_level.py` / `check_capability_gap.py` / `check_failure_192858.py` | 把本次修复钉住 | 升级项的断言（本文件 §3.2 的 8 组）需要另开一批文件 |

一句话：**T1/T2/T3 是"把这一次的事做对"，本计划的 P0 是"让下一次不会以另一种形式再来一次"。**

### 6.2 阶段划分

**阶段 1（必须在**下一次真实 run**之前完成）= 8 条 P0**

理由很直接：下一次真实 run 很可能就是原句重跑（"L_NewTest2 + 简易 FPS"）。那时 B 段靠补窗能过，**C 段必然又撞 lexicon 空白**。如果没有 D6/D9，用户会第二次看到「部分完成」而不知道少了什么；如果没有 D5，工具作者会第二次在两个极端之间摇摆。

| 序号 | 项 | 为什么必须在跑之前 |
| --- | --- | --- |
| 1 | D10 归档保真（0.5–1h） | 最便宜，且所有"用归档做证据"的断言都依赖它 |
| 2 | D1 parity 守卫（1–2h） | 不装守卫，T1 收口本身就可能留下新的不一致 |
| 3 | D7 语义不变量（2–3h） | C 段的空窗口必须在进人审前就有结构 |
| 4 | D6 人审覆盖（3–5h） | 顺序依赖 D7（先有缺口结构，人审才有东西显示） |
| 5 | D5 evidence 计数（3–5h） | 没有计数，跑完还是说不清"哪几件没交付" |
| 6 | D4 三态 verdict（2–4h） | 与 D5 同批落，共用 `_run_integrate` 的改动 |
| 7 | D9 未交付清单出口（3–5h） | 用户看到的最后一屏必须说清缺什么 |
| 8 | D2 fail-fast + 升级回路（2–4h） | 缺口必须挡住自动执行（顺序上依赖 D6 的人审确认通道） |

**阶段 2（可与真实 run 并行）**：D3 lexicon 落盘 + `check_lexicon.py`（2–3h）、D8 候选闭环（3–5h）、D11 状态词表（1–2h）。

**阶段 3（P2，下一次升级再排）**：
- **D12** 插件侧 `SceneTools.new_level`（真空白关卡，而不是模板复制）；
- Enhanced Input / 投射物 / 伤害的 MCP 工具面（把"加输入/开火"从 unplannable 变成可执行）；
- 蓝图图动作对规划者可见化（今天只有人工路径 + `UE_ALLOW_BLUEPRINT` 门）；
- 动作表满（≥30 项）之后的检索式动作选择（lexicon 只能撑到几十条，再往上要换机制）`[推测]`。

### 6.3 下一次真实 run 前的门槛检查（5 条命令，全绿才开跑）

```pwsh
cd E:\UEProject\AIWork\RAG\ue-rag
.\.venv\Scripts\python.exe eval\check_catalog_parity.py       # D1
.\.venv\Scripts\python.exe eval\check_verdict_tristate.py     # D4 + D5
.\.venv\Scripts\python.exe eval\check_review_coverage.py      # D6 + D7
.\.venv\Scripts\python.exe eval\check_failure_192858.py       # T3 本次失败链路端到端
.\.venv\Scripts\python.exe eval\check_upgrade.py              # 既有口径不许回退
```

并在 `.github/workflows/offline.yml` 里接上这五条（今天的 workflow 只跑到 `retrieval_bench`，`:42`）。

**跑真实 run 前的人工 checklist（3 条）**：
1. `data/skills/session_task.json` 由人写好 `allow_writes`/`save`/`pie`（本次会话无法替人做真机写入，DSH 侧只读该文件）；
2. 人审门看到「能力缺口」时**显式确认**，不许默认回车通过；
3. 跑完读 `docs/upgrade/gap-taxonomy.md` 附录 B 的字段对照，确认 `undelivered` / `verdict` / `evidence_count` 三个新字段都落了。

---

## 7. 不做清单（明确不引入什么）

1. **不为让一句话跑通而放宽裁判口径**：不把 `ok=None`（不适用）改成通过；不把「全不适用」算成「矩阵全部通过」。这是 20261021-195220 已经付过学费的口径（`run_windows.py:967-969`、`eval/check_harness.py:773`）。
2. **不把 `ok=None` 改回通过，也不把 `stop_reason="judge_failed"` 直接改成 `ok`**：三态化只加 `verdict`/`no_evidence` 字段，`ok` 语义不动。
3. **不把表外动作直接执行**：`plan_windows.materialize` 的 `allows()` 门（`:568`）是硬门，不许"先跑起来再补 catalog"，也不许把 `promote_candidate` 的 `reviewed=True` 默认成真。
4. **不让规划者/理解器自造动作名**：保持"看不准就不要编"（`catalog.py:368`）。缺口必须走 `capability_gap`，不许靠模型自由发挥补表。
5. **不为了让「新建关卡」好看而把"模板复制"说成"空关卡"**：`create_level` 的 summary 必须显式写"复制模板"（模板 `/Game/Maps/L_NewTest`），真空白关卡留给插件侧 P2。
6. **不给 C 段（FPS 玩法）造假证据**：不许用自动生成的 `place_batch` + `editor_assert` 把"摆了东西"包装成"玩法实现"；玩法读数只能来自 Automation 或人点界面。参考 `eval/check_harness.py:711-722`（跑了 0 个测试不算证明）。
7. **不引入"人审门自动确认"或"缺省回车即通过"**：缺口范围必须有人显式表态（§5.4 第 4 步）。
8. **不加新的状态词而不登记**：加状态必须同时改 `harness.STOP_REASONS`（`:14`）、`run_windows._MD_STATUS`（`:78`）、`brief.py:616` 的映射，并有 `check_statuses.py` 断言（D11）。
9. **不允许 lexicon 由模型维护**：lexicon 是人写的白名单，模型只能提议（候选表），不能改。
10. **不做"多套真值"**：`undelivered`（D9）、`evidence_count`（D5）、`capability_gap`（D2）都必须从既有源（`scope_marks`、`criteria`）派生，不新增独立统计。出现两套数字时以合同（`criteria`）为准。
11. **不改 `.uasset` / `.umap` / `Source/`，不在本计划里碰编辑器**：本文件是文档交付，任何编辑器写入仍按铁律经 `mcp__uerag__*` 网关或既有 `editor_exec`。
12. **不把本文档当代码变更授权**：本文档是计划，执行前每一条 P0 都要单独开任务、单独评审、单独跑离线断言。

---

## 8. 与既有文档 / 历史的关系（避免重复造轮子）

| 既有材料 | 它已经说了什么 | 本文件的关系 |
| --- | --- | --- |
| [`docs/failure/20261024-192858-root-cause.md`](../failure/20261024-192858-root-cause.md) | RC-1..RC-5 根因 + 本次三轴修复（T1/T2/T3）+ 遗留边界（模板复制、在线终验受限） | 本文件**承接它的第 7 节「未覆盖/遗留」**，把"补一个动作修一次""能力表无守卫""词典覆盖窄"三条扩成 12 条模式库与 8 条 P0 |
| `AIAgentProject/Plan/20261019-1130-Harness对照与能力缺口升级.md` §4.5「三套工具在闭环里只看得见一套」 | 最早指出"能力可见性"这一层缺口 | 本文件 D1 是它的**可执行化**：从"缺口描述"变成 parity 守卫 + 七件套准入 + 差集断言 |
| 同文件 §4.1「验收合同没有挂到这句话上」/ §3 边界清单（模型不当裁判） | 确立了"合同先默认失败、编译器当裁判" | 本文件 D4/D5/D7 沿用该边界，只补三态与计数，不动裁判权威 |
| `data/harness/progress.json` 的 risks（20261021-221446：理解器的 unresolved 是需求黑洞，`level-assembly-persist` 未闭环前别报成功） | 2026-10-21 就把同一类问题写成了风险 | **这是本次失败的同类第二次发生**：20261021 是"场景内容被 unresolved 吞掉"，本次是"整段需求被 0 窗口吞掉"。⇒ 结论：加闸门（`scene_gate.py`）治了一次，**下一次换了个入口又进来**——所以升级项必须是"缺口必须改变流程走向"这一条通用规则（D2/D5/D9），而不是第三个专用闸门 |
| `eval/check_harness.py:685-774`（`check_under_delivery_guard`，20261021-195220 回归）、`eval/check_scene_gate.py` | 两次历史教训的离线回归 | 本文件 §3.2 的断言**直接复用这两处的写法和反例守卫习惯**，不另立风格 |
| `AIAgentProject/Plan/20261023-1520-DeepSeekHarness深度绑定.md`、`RAG/ue-rag/dsh/README.md` | DSH 第二入口与写策略（`session_task.json` 只由人写） | 本文件 §6.3 的人工 checklist 沿用该策略，不在 DSH 里写 `allow_writes`/`save/pie` |

**不重复本次已修的项**：T1 的 `create_level`、T2 的 `capability_gap`/三态雏形、T3 的三条离线检查，本文件只把它们标成"治标已完成"，并在 §6.1 逐条写出"治不了什么"。

---

## 附录 A：行号基线

| 位置 | 基准工作树行号（2026-10-24 19:43–19:57） | 是否会被 T1/T2/T3 位移 |
| --- | --- | --- |
| `agent/catalog.py` `CATALOG` / `DIRECT_ACTIONS` / `READ_ONLY_ACTIONS` / `GRAPH_ACTIONS` / `EDITOR_LOCK_ACTIONS` / `_VERIFY` | `:8` / `:102` / `:127` / `:136` / `:143` / `:169` | 是（T1 已加 `create_level`） |
| `agent/catalog.py` `iter_prompt_specs` / `note_candidate` / `promote_candidate` / `system_prompt` | `:279` / `:295` / `:322` / `:364` | 是 |
| `agent/harness.py` `EVIDENCE_KINDS` / `_KIND_BY_ACTION` / `_window_ids_for` / `build_criteria` / `scope_marks` / `contract_status` / `classify_stop` | `:13` / `:20` / `:110` / `:156` / `:274` / `:320` / `:349` | 是（T2 已改 `scope_marks`/`contract_status`） |
| `agent/brief.py` `_ACTION_ESTIMATE` / `_SCOPE_KEYS` / `_GAP_NOTE` / `brief_system_prompt` / `validate_brief` / `render_brief_md` / `unfinished_text` / `brief_flow` / `materialize_scopes` | `:21` / `:44` / `:53` / `:185` / `:283` / `:542` / `:598` / `:791` / `:847` | 是（T2 已大幅改） |
| `agent/run_windows.py` `_MD_STATUS` / `_SCENE_WRITE_ACTIONS` / `_run_integrate` / `_archive_run` / `_run_query` / 收尾状态判定 / 归档调用 | `:78` / `:84` / `:960` / `:1188` / `:1946` / `:1580-1649` / `:1083`+`:1734` | 是（T2 已改收尾） |
| `agent/sequence.py` `_run_action`（`create_blueprint` 分支 `:136`） | `:128-273` | 是（T1 加 `create_level` 分支） |
| `agent/plan_windows.py` `materialize`（`allows` 门 `:568`）/ `_ASSET_GROUND_ACTIONS` / `_create_and_goal` | `:494` / `:603` / `:552` | 是 |
| `agent/intent.py` `validate_intent` / `note_candidate` | `:46` / `:70` | 否（本批未改） |
| `core/judge.py` `judge` 分支 | `:544-555` | 否 |
| `data/runs/20261024/20261024-192858.json` 全部字段 | 冻结 | 否 |
| `data/events/20261024-192858.jsonl` / `data/archive/20261024-192858/deliverables.json` | 冻结 | 否 |

全部证据明细、实测集合差、parity 检查脚本骨架、lexicon JSON 草稿、P0 断言清单见 [`gap-taxonomy.md`](gap-taxonomy.md)。

---

## 附录 B：落地状态矩阵（2026-10-24 23:59 实测）

> 本附录是**执行记录**，不是计划。上面 §1–§8 保持原样（那是判断），这里只回答一个问题：
> **12 条缺陷里哪些已经有了离线守卫、哪些还只是文档。**
> 判定标准很硬：**有一个能在 CI 里跑红/跑绿的 `eval/check_*.py` 才算落地**；只有代码改动没有断言的，
> 一律记为「已改未守卫」——因为它会在下一次重构里静默退化。

### B.1 汇总

| 缺陷 | 状态 | 守卫文件 | 关键落点 |
| --- | --- | --- | --- |
| **D1** 能力表 ≠ 执行器 | **已落地** | `eval/check_catalog_parity.py` | I1–I5 全实现；三张表（`CODE_ONLY_ALLOWED` 8 条 / `PENDING_JUDGE` 2 条 / `SYNTHETIC_JUDGES` 1 条）；**19 个负例自证会红** |
| **D2** 静默降级 | **已落地** | `eval/check_escalation.py` + `eval/check_review_coverage.py` | 补窗后仍空 ⇒ `confirm-gaps` 显式确认，否则不执行；`UE_ESCALATE_GAPS=false` 只记不阻塞且默认开 |
| **D3** 需求动词未覆盖 | **已落地** | `eval/check_lexicon.py` | `data/skills/lexicon.json` 19 行；`agent/lexicon.py`；`plan_windows`/`intent` 只读挂接（异常一律回落） |
| **D4** 三态混一 | **已落地** | `eval/check_evidence_attribution.py` | 矩阵项新增 `verdict` 四态；`ok` 语义**一字未动**（None 仍是 None）；`no_evidence` 只给「交付验收」且必须有缺口 |
| **D5** 缺证据计数与归因 | **已落地** | `eval/check_evidence_attribution.py` | 每项 `evidence_count` + `scope_ids`；计数一律从 `criteria` 派生 |
| **D6** 人审只审形态不审覆盖 | **已落地** | `eval/check_review_coverage.py` | md 加「窗口覆盖」列；控制台加覆盖行；dry-run 预演**不写盘**（临时替换 `note_candidate` 再恢复） |
| **D7** schema 合法但语义空洞 | **已落地** | `eval/check_review_coverage.py`（覆盖口径）+ `check_capability_gap.py`（既有） | 零窗口在**人审阶段**就有覆盖结论，不再等到物化后才暴露 |
| **D8** 候选表无闭环 | **已落地** | `eval/check_candidate_aging.py` | `first_seen`/`last_seen`/`hits`/`sources`；`stale_candidates()` 14 天阈值；`dismiss_candidate()`；收尾发 `action_candidate_stale` 事件 |
| **D9** 部分交付无出口 | **已落地** | `eval/check_evidence_attribution.py`（run 侧）+ `check_review_coverage.py`（brief 侧） | run json 新增结构化 `undelivered`；run md 出「未交付清单」段；`intent.unresolved` 进任务书 md |
| **D10** 归档证据被二次覆盖 | **已落地（更早）** | `eval/check_archive_fidelity.py` | T3 已修；本次未动 |
| **D11** 状态词表未登记 | **已落地** | `eval/check_statuses.py` | `agent/statuses.py` 四表 + 人审覆盖表；静态扫描五个锚点；13 条显式跳过带原因 |
| **D12** 替代实现冒充 | **部分**（文案已诚实，插件侧未做） | 无独立守卫 | `create_level` summary 已写明模板复制；真空白关卡仍留 P3 |

### B.2 这次实测发现的两件事（计划里没有的）

1. **`bind_blueprint` 与 `create_widget_blueprint` 会跑完却没有裁判。** 它们在 `CATALOG` 里、有 `_VERIFY` 口径、
   `sequence` 侧也能执行，但**不产 `judge_kind`**，所以 `_KIND_BY_ACTION` 里故意没有它们（`harness.py:29-33` 的
   T8 回滚记录）。这是 D1 家族里最坏的一种：**能规划、能执行、没裁判 ⇒ 最坏是假成功**。
   正确修法是**先在 producer 侧补判词，再同时加映射**（只加映射会造出永远 `passed=False` 的条款）。
   `check_catalog_parity.py` 用 `PENDING_JUDGE` 把它**钉住而不是豁免**：它断言 producer 里**没有** `judge_kind`，
   一旦有人补了判词就立刻红，逼两侧一起动。**这条仍未修，属已知真缺陷。**
2. **D11 的守卫上线当天就抓到了一次真实漂移。** D6 落地时 `brief.py` 引入了 `state ∈ {"", "gap", "rescued"}`
   这张新人审覆盖词表，`check_statuses.py` 当场红在四处比较上。处置是把它登记成 `statuses.REVIEW_COVERAGE_STATES`
   （并加断言**不许混进**四表并集），而不是放宽扫描——这正是 D11 想防的「加一个状态只改一处」。

### B.3 用真实失败数据复验（2026-10-25 00:10）

拿 `data/runs/20261024/20261024-192858.json` 的**真实 brief 段**（只读）走一遍新链路，
这是「治好了没有」最直接的证据：

| 范围 | 失败当时的判定 | 现在的判定 | 变化 |
| --- | --- | --- | --- |
| A 启动编辑器 | 窗口 1（launch_editor） | `窗口：1 扇（launch_editor）` | 无变化（本来对） |
| B 新建关卡 L_NewTest2 | **`windows: []` 整段蒸发** | `rescued: 1` → `create_level` 补出，显示「窗口：1 扇 ※补窗预告」 | **T1 的 `create_level` 让它重新可规划** |
| C FPS 射击玩法 | `windows: []`，一句 `block_note` 放行 | `gap: true`，人审门显式列进「能力缺口（人审必须逐条表态）」 | **从空白变成必须表态** |

任务书 md 现在同时具备三样失败当时没有的东西：`窗口覆盖` 列、`## 能力缺口` 章节、
`## 尚未完成清单` 章节（后者由 `intent.unresolved` 喂入，失败当时是完全不渲染的）。

⇒ **同一个原句重跑，B 会真跑，C 会停下来等人表态，而不是整段无声蒸发后报「部分完成」。**

### B.4 门槛检查（下一次真实 run 前必须全绿）

比 §6.3 的五条多了升级批次的六条，共 11 条（已全部接进 `.github/workflows/offline.yml`）：

```pwsh
cd E:\UEProject\AIWork\RAG\ue-rag
.\.venv\Scripts\python.exe eval\check_catalog_parity.py        # D1
.\.venv\Scripts\python.exe eval\check_evidence_attribution.py  # D4 + D5 + D9(run)
.\.venv\Scripts\python.exe eval\check_escalation.py            # D2
.\.venv\Scripts\python.exe eval\check_review_coverage.py       # D6 + D7 + D9(brief)
.\.venv\Scripts\python.exe eval\check_candidate_aging.py       # D8
.\.venv\Scripts\python.exe eval\check_lexicon.py               # D3
.\.venv\Scripts\python.exe eval\check_statuses.py              # D11
# 以及 §6.3 原有的五条
.\.venv\Scripts\python.exe eval\check_failure_192858.py
.\.venv\Scripts\python.exe eval\check_upgrade.py
.\.venv\Scripts\python.exe eval\check_harness.py
.\.venv\Scripts\python.exe eval\check_archive_fidelity.py
.\.venv\Scripts\python.exe eval\check_integrate_verdict.py
```

### B.5 遗留（明确没做的，不许当成做完了）

| # | 遗留项 | 为什么没做 | 下次怎么接 |
| --- | --- | --- | --- |
| 1 | `bind_blueprint` / `create_widget_blueprint` **无裁判** | 修法要先在 producer 侧补 `judge_kind`，再同步加 `_KIND_BY_ACTION`；只加映射会造出永远 `passed=False` 的条款（`harness.py:29-33` 的 T8 回滚记录）。属**真缺陷** | `PENDING_JUDGE` 已钉住：补了判词它自动红，逼两侧一起动 |
| 2 | D12 真空白关卡 | 需要插件侧 `SceneTools.new_level`（本仓库之外） | lexicon 已有 unplannable 行指向那儿 |
| 3 | 人审顺序仍是「先审后物化」 | dry-run 预演已让覆盖可见，但真物化仍在人审之后（改顺序会吃掉 candidates、改动人审对象） | 若要人审直接看物化结果，改 `_run_query` 的顺序 |
| 4 | 人审预演用 monkeypatch 掐掉 `note_candidate` 写盘 | 人审前写盘 = 还没确认就改项目状态。掐掉只丢一次候选计数，不影响判定（`allows()` 不读候选表） | 若要更干净，给 `materialize` 加 `dry_run=` 形参 |
| 5 | `offline.yml` 新增的 7 条只在本机验证过 | 本会话无法跑 GitHub Actions | 首次 push 后看 CI 日志；`check_lexicon`/`check_statuses` 对缺失数据文件已做替身回落 |

---

## 附录 C：本次升级的改动清单（供评审 / 回滚）

新增 7 个守卫 + 3 个模块/数据文件；改动 5 个既有模块。**每一处改动都有对应断言**：

| 文件 | 性质 | 对应缺陷 |
| --- | --- | --- |
| `eval/check_catalog_parity.py` | 新增 | D1 |
| `eval/check_evidence_attribution.py` | 新增 | D4 + D5 + D9(run) |
| `eval/check_escalation.py` | 新增 | D2 |
| `eval/check_review_coverage.py` | 新增 | D6 + D7 + D9(brief) |
| `eval/check_candidate_aging.py` | 新增 | D8 |
| `eval/check_lexicon.py` | 新增 | D3 |
| `eval/check_statuses.py` | 新增 | D11 |
| `agent/statuses.py` | 新增 | D11 唯一真值源 |
| `agent/lexicon.py` + `data/skills/lexicon.json` | 新增 | D3 |
| `agent/run_windows.py` | 改 | D5 归因 / D4 四态 / D9 `undelivered` / D2 执行前闸 / D8 出声 |
| `agent/brief.py` | 改 | D6 覆盖列 / D2 人审闸 / D9 `unresolved` 通路 |
| `agent/catalog.py` | 改 | D8 `first_seen` / `hits` / `stale_candidates` / `dismiss_candidate` |
| `agent/plan_windows.py` | 改 | D3 只读挂接 |
| `agent/intent.py` | 改 | D3 `lexicon_unplannable` 标记 |
| `.github/workflows/offline.yml` | 改 | 七条新守卫接进 CI |

**回滚策略**：七条新守卫都是独立文件，删掉即回滚断言、不影响运行时。`agent/` 侧改动全部是**加法**
（新字段 / 新分支），`ok` 语义、run `status` 取值、`criteria` 口径**一个字没动** ——
所以这些改动可以回滚，而历史结论不会被改写。
