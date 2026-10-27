# 20261024-192858 失败链条整改 · 独立复验报告（T3）

- 复验方：T3（独立复验 / 证伪方，不是任何一个修复任务的作者）
- 复验时间：2026-10-24 20:42–21:25（Asia/Shanghai）
- 被验对象：run `data/runs/20261024/20261024-192858.json`（人读版 `.md`）的失败链条
- 原句：`打开编辑器，新建一个关卡，名字为L_NewTest2，在这个关卡实现逻辑如下：需要实现简易的FPS射击游戏，内容你自由发挥`
- 原故障：只跑出 `launch_editor` 一扇；B（新建关卡）、C（实现 FPS 玩法）两段交付清单非空但窗口为 0，只记「没有可执行的窗口，只记录待办」就放行。
- 我的交付：`eval/check_failure_192858.py`（新建）、`.github/workflows/offline.yml`（改）、本文件。
- 工作目录 `RAG/ue-rag`，Python 一律 `.venv\Scripts\python.exe`。

---

## 0. 结论（一句话）

**真过 —— 20/20 全绿，我判定的两条新缺陷（A/B）由 task-10 修到根、我自己的严格断言在代码修好后自己转绿，不是我把断言放宽。**

具体：T1/T2/T5/T6/T7/T8/T9/T10 的整改我都用**独立写的**检查复现通过了（规划层补窗、缺口三态、归档保真、交付验收四态、veto 聚合、矩阵第 5 项取反、等外部话术），20 条离线套件全部 `exit=0`（§2.1）。
过程要点：我第一轮报出 T9（缺口/失败/暂停时仍报「矩阵全部通过」），修好后又报出 A/B 两条同族缺陷（A：矩阵第 5 项语义反转；B：等外部范围话术 + label/note 打架）。**A/B 我是先写好严格断言、让套件保持红，再由 task-10 修代码的** —— 所以现在的绿是「缺陷被修掉」的绿，不是「断言被改软」的绿（§6）。

仍未覆盖（不影响本次结论，但要留着）：真实 LLM 规划、CI 真跑、缺陷 A 的端到端可达性（§8）。

另：建关卡链路的**真实编辑器行为**已由 **Lead 真机终验**覆盖（§8 末），那是我原来最大的未覆盖面；但那不是我做的，我保持中立表述。

判定用语：
- **真过** = 我自己写了独立用例、跑出了预期输出（不是读别人结论）。
- **假过** = 看起来过了但实际没验到（本报告列出 3 处，见 §7）。
- **未覆盖** = 我明确没验的边界（§8）。

---

## 1. 基线怎么取的（没用 `git stash`）

工作树在我接手前已经很脏（`data/dsh/baseline-git.txt` 里几十个 `M`/`??`，含上一届会话的未提交改动），`git stash` / `git diff HEAD` 分不出「本次改动」与「历史未提交」。因此：

| 基线来源 | 结论 | 我怎么用 |
| --- | --- | --- |
| `data/dsh/baseline-checks.txt`（2026-10-23 15:34 跑批） | check_routes 当时先红（skill registry）后修好 → `exit=0`；check_harness / info_layer / architecture / schema / risk / automation / scene_gate / distill 全 `exit=0` | 作为「本来全绿」的依据 |
| traceback 归因 | 失败时看栈帧是否指向本轮改动的符号（`create_level` / `scope_windows` / `capability_gap` / `no_evidence` / `交付验收` / `_archive_run`） | 判定「回归」还是「新发现」 |

补充：我**没有**用「git 里 HEAD 版本 vs 工作树」做基线来判缺陷。HEAD 与工作树的 diff 我只看过一次，用途是确认缺陷 A 的**方向**（见 §6 A：HEAD 里是 `"ok": not stuck`，现在被改成 `"ok": True if stuck else None`）——这属于「代码语义演进」的证据，不是「本次改动」的归属依据。

---

## 2. 我实际跑了什么（命令 + 真实退出码）

全部在 `RAG/ue-rag` 下、用 `.venv\Scripts\python.exe eval\<name>.py`，逐条记 `$LASTEXITCODE`。

### 2.1 全量离线套件（task-10 落地后，最终一次）

| # | 套件 | exit | 备注 |
| --- | --- | --- | --- |
| 1 | `check_routes` | 0 | |
| 2 | `check_info_layer` | 0 | |
| 3 | `check_architecture` | 0 | |
| 4 | `check_schema` | 0 | |
| 5 | `check_risk` | 0 | |
| 6 | `check_automation` | 0 | |
| 7 | `check_harness` | 0 | 含 773 行「只有不适用项的矩阵永远 ok is False」老断言（task-10 后我单独复跑复刻场景，仍成立，见 §5） |
| 8 | `check_upgrade` | 0 | |
| 9 | `check_create_level` | 0 | T1 |
| 10 | `check_capability_gap` | 0 | T2/T6/T8 |
| 11 | `check_archive_fidelity` | 0 | T5 |
| 12 | `check_integrate_verdict` | 0 | T7/T9/T10（**task-10 动了这里，我核对过：没有被波及**，见下） |
| 13 | **`check_failure_192858`** | **0** | **我的文件。task-10 前红在缺陷 A/B；task-10 后转绿，我没改过这两条断言** |
| 14 | `check_dsh_contract` | 0 | |
| 15 | `check_verify_spec` | 0 | |
| 16 | `check_dsh_lock` | 0 | |
| 17 | `check_dsh_costs` | 0 | |
| 18 | `retrieval_bench` | 0 | 纯向量 Recall@5=1.000 / BM25=0.955 / 混合=1.000 |
| 19 | `check_scene_gate` | 0 | |
| 20 | `check_distill` | 0 | |

> **20/20 全 `exit=0`。** 与我此前唯一的红（`check_failure_192858`，红在 §6 的缺陷 A）相比，转绿的原因是 **task-10 修了 A/B 两个缺陷，我没有降低任何断言强度**——
> 那两条断言（`check_known_defect_stuck_window_passes` / `check_known_defect_external_scope_wording`）是我在缺陷还没修的时候**先写好的严格断言**，代码一修好它们自己就绿了。这是我作为证伪方要的顺序：先钉住缺陷，再等它被修掉，不是反过来把断言改成当前行为。
> `check_failure_192858` 现在有 **23 个 check 函数、201 条 assert**（`Select-String '^\s*assert '` 计数，1257 行）。

**`check_integrate_verdict` 有没有被 task-10 波及 —— 我专门查过**（这是 review 里最容易出假绿的地方）：
- task-10 动的是 `_run_integrate` 的矩阵第 5 项（`"ok": False if stuck else None`）与 label 限定，以及 `harness.scope_marks` 的判断顺序。
- `check_integrate_verdict` 的 5 条断言（四态 + 归档时序）全部仍 `exit=0`；我另外**用我自己的用例**复刻了它最容易踩的那条（空转 / 全不适用矩阵），确认 `entry.ok is False` 仍成立（§5）。
- 结论：**不是「新增断言没跟口径」的假绿，也不是真回归** —— 是 T7/T9 的口径与 T10 的修法互不冲突。

### 2.2 我另跑的探针（不进仓库，只在 `%TEMP%` 下，用于取证据）

| 探针 | 用途 | 结果 |
| --- | --- | --- |
| 规划层复现 | `validate_brief` + `materialize_scopes` 走真实任务书 | 真过，见 §3 |
| 矩阵三态 | 4 种结局各跑一条真 run（假 `run_task`） | 四种 label 互斥，见 §4 |
| veto 反向 | `waiting_external` / `skipped` / 集成扇自身是否被误判成失败 | 都不误判，见 §5 |
| 中断形态 | 模拟 `KeyboardInterrupt` | 集成扇排不上队（`queued`），矩阵为空 → 不假过 |
| 全部离线 | 全程 `core.editor_exec.exec_tool` 换成会抛异常的函数 | 无一次真调用 |

---

## 3. 断言 a：规划/物化后 B 段必须有 `create_level` 窗口，C 段必须是缺口或窗口

**真过。** 我的检查 `check_materialize_no_silent_zero_window()` 用 `data/briefs/20261024-192830-L_NewTest2-FPS.json`（真实任务书）走 `validate_brief` + `materialize_scopes`，实测：

```
W1 launch_editor   realizable=True   depends_on=[]
W2 create_level    realizable=True   depends_on=['W1']   level_path=/Game/Maps/L_NewTest2   goal=True
W3 integrate_verify                 depends_on=['W1','W2']

范围 A: window_ids=['W1']       capability_gap=False
范围 B: window_ids=['W2']       capability_gap=False      ← 从 [] 变成 ['W2']
范围 C: window_ids=[]           capability_gap=True       ← 诚实的缺口
范围 INT: window_ids=['W3']     capability_gap=False

条款：A/B 各有 1 条 editor_assert（分别绑到 W1 / W2）；C/INT 无条款
```

与 Lead 早前独立跑的结果**一致**（我是自己重跑的，不是复述）。

同一组还钉住了：
- `catalog.system_prompt()` 含 `create_level` 与「新建一个关卡用 create_level」这句路由指令；`brief_system_prompt()`（任务书生成器的动作表）也含 `create_level` —— 之前生成器看不见这个动作，正是 B 段蒸发的一半原因。
- `plan_windows("新建一个关卡，名字为 L_NewTest2")` → `source="rules"`，`[create_level]`，`realizable=True`，路径归到 `/Game/Maps/L_NewTest2`。
- 理解器/规划者漏拆时，`_ensure_requested_create_level` 会在**上一扇之后**补一扇 `create_level`，且**幂等**（已有一扇就不再补）。
- `scope_windows` 对无关范围返回 `[]`：`移动 Cube 到 x=100`、`启动 UE 编辑器并等待 MCP 端口连通`、`在 L_NewTest2 内实现简易 FPS 射击玩法`、`做一个平台跳跃关卡`（中间夹了别的词）全部 `[]` —— 不会把 B 段的需求挪到别的范围。
- 名字缺失/多义时补出的是**暂停窗**（`realizable=False, blocked=True, error="缺少槽位 level，不猜。"`）而不是空列表、也不是猜一个名字。这一点很关键：如果补窗返回 `[]`，就会退回「缺口」；如果猜名字，就会**乱建关卡**。

---

## 4. 断言 b / c / d：三态裁决与反例守卫

**真过（T9 修好之后）。** 四种结局各跑一条完整 run（假 `run_task`，临时目录隔离），实测：

| 结局 | exit | run status | 集成扇 | label | verdict | stop_reason |
| --- | --- | --- | --- | --- | --- | --- |
| ① 有缺口（补不出窗） | 1 | **未完成** | failed / ok=False | 矩阵未全过：…**能力缺口**… | `no_evidence` | `no_evidence` |
| ② 无缺口但条款真失败 | 1 | 部分完成 | failed / ok=False | **矩阵未全过** | `""` | `judge_failed` |
| ③ 无缺口、条款全过 | **0** | **成功** | done / ok=True | **矩阵全部通过** | `""` | `ok` |
| ④ 一条条款都没有且无缺口 | 1 | 部分完成 | failed / ok=False | **矩阵没有可验证的项，不算全过** | `""` | `judge_failed` |

- 断言 b（192858 形状）：exit=1、status=`未完成`（**不是**「部分完成」，也**不是**「成功」）、`integration.ok=False`、note 点名 B 与 C、md 里出现 `- 状态：未完成` 与 `未完成（能力缺口）`、`run_end` 事件的 `stop_reason=no_evidence` 且 `status=未完成`。**真过。**
- 断言 c：`detail.verdict == "no_evidence"`，且与②的 `("", "judge_failed")` **可区分**。**真过。**
- 断言 d 第一条（反例守卫）：③ 仍然 `exit=0` + `成功` + label 含「矩阵全部通过」 —— T7 的修复没有把成功路径弄坏。**真过。**
- 断言 d 第二条：④ 的「交付验收」为 `ok=None`、checked 为空、`ok=False`、label 不含「矩阵全部通过」。**真过。**

另外 4 个 label 我做了**互斥断言**：`能力缺口` / `矩阵全部通过` / `没有可验证的项` 三个标记只许出现在各自那种结局里（不互相冒充）；「矩阵未全过」允许与「能力缺口」同时出现（那是正确的叠加，不是冒充）。

用**真实任务书 + 真 `scope_windows`** 再跑一次（B 真跑、C 是缺口）：`launch_editor(done) / create_level(done) / integrate_verify(failed)`，`status=未完成`，note 只点名 C（不点 B，因为 B 真做完了），`verdict=no_evidence`。**真过。**

---

## 5. 历史坑两条必须继续成立

| 历史坑 | 我的验证 | 结论 |
| --- | --- | --- |
| 20261021-195220「五项空转全绿」：`ok=None` = 不适用，不算通过、不撑成功 | ①/④ 的矩阵里都有多项 `None`；断言 `任何 ok=None 的项都不许被读成通过`，并断言「只有不适用项的矩阵」`entry.ok is False` | **继续成立** |
| `eval/check_harness.py:773` 断言「只有不适用项的矩阵永远 ok is False」 | 我的检查**读源码**断言这句话还在（`"只有不适用项的矩阵不能是「全过」" in harness_src`），并用空转形态实测 `entry.ok is False` | **继续成立**（且 check_harness 本身 exit=0） |

> 说明：这两条我只是「断言它们仍然成立」，没有改它们。`check_harness.py` 我也没改（它是 T1/T2 之前就存在的文件，不在我的写入范围）。

**task-10 之后我专门复跑了这两条**（T10 同时动了矩阵第 5 项的取反与 label 限定，最容易踩到它们）：

1. **空转形态**（一扇失败 + 集成扇，所有矩阵项都不适用）：
```
exit=1 | status=失败 | matrix 6 项全 None | checked=[] | entry.ok=False
label = 「矩阵没有可验证的项，不算全过；0 项通过口径、6 项不适用」
```
   → 「只有不适用项的矩阵永远 ok is False」**仍成立**。

2. **直接复刻 `check_harness.py:740-774` 的场景**（`hollow` 假 `run_task`：`edit_cpp` 编译 `exit 0` 但零改动）：
```
exit=1 | W1 state=failed | matrix 6 项全 None | W2 ok=False | status=失败
```
   → 老断言原文（`assert data["windows"]["W2"]["ok"] is False, "只有不适用项的矩阵不能是「全过」"`）**逐条仍成立**，且 `check_harness.py` 本身 `exit=0`。

结论：task-10 的修法**没有**把「不适用」重新撑成「通过」——这是它最需要担心的回归面，我按原场景原封不动复刻验过了。

---

## 6. 我找到的缺陷（含最小复现）

### 已修：T9 —— 缺口/失败/暂停时仍报「矩阵全部通过」（我在第一轮报给 Lead，Lead 开 task-9 由 fix-gap 修）

**修前**（我实测两次）：缺口 + `launch_editor` 重试一次成功 + `create_level` 真失败 ⇒ 集成扇 `done / ok=True / stop_reason="ok"`，label「矩阵全部通过；1 项通过口径、5 项不适用」，而 run 级 `status=未完成 / exit=1` —— md 里同时出现「W3 状态：成功」与「集成 状态：未完成」。

**修后**（我刚重跑）：集成扇 `failed / ok=False / stop_reason="no_evidence"`，label「矩阵未全过：有范围整段没交付（能力缺口）——C…」，与 run 级一致。

根因（供后来者理解）：`checked = [矩阵项里 ok is not None 的]`、`ok = all(checked)`；而「延迟窗口」的判据是 `state in (done, failed, paused, skipped)`（**失败也算过**），「拒答路径」在有 paused 扇时给 True；有缺口时「交付验收」又被强制成 `None`（不投票）。于是一项运行期项的 `True` 就能凑出「checked 非空且全 True」。

这两条最小复现现在是我检查里的 `check_known_defect_gap_with_delayed_item()` 与 `check_known_defect_paused_window()`，**已转绿**。

### 已修（task-10，交给 fix-gap）：缺陷 A —— 矩阵第 5 项「未知态恢复」语义反转（假通过方向）

- 位置：`agent/run_windows.py` 的 `_run_integrate`，第 5 项。
- 修前：`"ok": True if stuck else None`（HEAD 里原本是 `"ok": not stuck`）。后果：**没有任何窗口卡住**时才给 `None`（对），**有窗口卡在 `running/retry/revising/interrupted`** 时反而给 `True`——而这一项存在的意义正是「每扇都有明确终态」，卡住就该是 `False`。更糟的是它会占一个 `checked` 名额去撑「矩阵全部通过」。
- 最小复现（纯单测，不跑 run、不碰编辑器、临时目录隔离；**我自己写的，没抄 fix-gap 的**）：

```python
from agent import run_windows as rw
st = rw._RunState("20260909-160000", "中断形态", "brief", [])
st.data["windows"] = {
  "W1": {"id":"W1","action":"launch_editor","state":"done","ok":True,"result":{"ok":True,"summary":"ok"},"window":{},"detail":{}},
  "W2": {"id":"W2","action":"create_level","state":"running","ok":None,"result":None,"window":{}},
  "W3": {"id":"W3","action":"integrate_verify","state":"running","ok":None,"result":None,"window":{}},
}
st.data["criteria"] = [{"id":"c-editor_assert","evidence_kind":"editor_assert","passed":True,"windows":["W1"]}]
out = rw._run_integrate(st.data["windows"]["W3"], st, "q", {}, rw._EventBus("20260909-160000"))
print(out["summary"], out["ok"], [i for i in out["matrix"] if i["item"]=="未知态恢复"])
```

**修前 / 修后（同一条探针，我自己复跑的）**：

| | 未知态恢复 | 集成扇 ok | label |
| --- | --- | --- | --- |
| 修前（task-10 前） | `{'ok': True, 'note': 'W2=running'}` | `True` | 「矩阵全部通过；2 项通过口径、4 项不适用」 |
| **修后（task-10 后）** | `{'ok': False, 'note': 'W2=running'}` | **`False`** | **「矩阵未全过；2 项通过口径、4 项不适用」** |

即：W2 悬在半路时，矩阵不再说「未知态恢复：通过」，也不再报「矩阵全部通过」。

- **反向守卫（我自己补的，防止修过头）**：没有 stuck 窗口时，这一项必须仍是 `None`（不适用），不许变成 `True`。修后实测：

```
未知态恢复: {'item': '未知态恢复', 'ok': None, 'note': '每扇都有明确终态或未开始'}
```

  这条很重要 —— 如果把修法写成 `"ok": not stuck`（HEAD 的老写法），无 stuck 时会变成 `True`，那就等于把「本次没发生」记成「通过了」，正好撞上 20261021-195220 那条教训。task-10 用的是 `"ok": False if stuck else None`，两种情况都对。

- 可达性（如实标注，**修后也没变**）：端到端我**没有**造出确定的 stuck 窗口 —— 模拟 `Ctrl+C` 时集成扇排不上队（停在 `queued`），矩阵没生成；`_RunState.from_resume` 会把 `interrupted/running` 归一成 `queued`；未知依赖的窗口停在 `queued` 且 `queued` 被排除。所以这条自始至终是**纯单测层面坐实的缺陷**，我按「方向是假通过」报，从未按「生产路径已复现」报。
- 我把它写成 `check_known_defect_stuck_window_passes()`（严格），task-10 前是红的，现在绿。

### 已修（task-10，交给 fix-gap）：缺陷 B —— 等外部范围被说成「只记待办」+ label 与 note 自相矛盾

- 位置：`agent/harness.py` 的 `scope_marks`（判定顺序）+ `agent/run_windows.py` 的聚合 label。
- 修前：`elif not window_ids:` 排在 `elif scope.get("external_dep"):` **之前**，所以「零窗口 + 等外部」落到「只记录待办」那句——正是本轮要根除的话术（同一 scope 有窗口时才能正确说出「等待外部 M-GO」）：

```python
from agent.harness import scope_marks
# 修前实测：
scope_marks([{"id":"C","window_ids":[],"external_dep":"M-GO","criteria":[],
              "deliverables":["接口恢复后核对"],"window_states":[]}])
# → [{'id': 'C', 'ok': False, 'reason': '没有可执行的窗口，只记待办'}]   ← 错
scope_marks([{"id":"C","window_ids":["W2"],"external_dep":"M-GO","criteria":[],
              "deliverables":["接口恢复后核对"],"window_states":["waiting_external"]}])
# → [{'id': 'C', 'ok': False, 'reason': '等待外部 M-GO'}]    ← 有窗口才说对
```

- 修前端到端后果（brief：A `launch_editor` 有窗 / B `create_level` 有窗 / C 等外部 M-GO 零窗口）：

```
run status = 部分完成   ok=False   exit=1          ← 对的
集成扇     = done / ok=True / stop=ok / label「矩阵全部通过；1 项通过口径」
收尾 note  = 「全部窗口完成。有范围没完成：C（没有可执行的窗口，只记待办）」
```

即：**同一份记录里「矩阵全部通过」与「有范围没完成」同时出现**，读记录的人会各信一半。注意这**不是**要求「等外部也算失败」——run 级 `部分完成 / exit 1` 是正确的口径，我不主张把 `waiting_external` 加进 veto；要的只是「话术别错」+「label 别和 note 打架」（例如 `open_scopes` 非空时 label 不说「全部通过」）。Lead 明确同意了同一口径。

**修前 / 修后（同一条判断，我自己复跑的）**：

```
修前: scope_marks([{id:C, window_ids:[], external_dep:"M-GO", ...}])
      → [{'id': 'C', 'ok': False, 'reason': '没有可执行的窗口，只记待办'}]     ← 话术错
修后: → [{'id': 'C', 'ok': False, 'reason': '等待外部 M-GO'}]                 ← 对
```

端到端（brief：A `launch_editor` 有窗 / B `create_level` 有窗 / C 等外部 M-GO 零窗口）修后：

```
run status = 部分完成   ok=False   exit=1        ← 一直是这个，是对的（等外部不该被否决成失败）
集成扇 / 收尾 note                               ← 不再出现「label 说全部通过 + note 说有范围没完成」的打架
```

- **反向守卫（我自己补的，防止修过头）**：零窗口 + **没有** `external_dep` 的范围，仍必须说「没有可执行的窗口，只记待办」——修 B 不许把「等外部」的分支提到所有零窗口之前。修后实测：

```
scope_marks([{id:C, window_ids:[], criteria:[], deliverables:["x"], window_states:[]}])
→ [{'id': 'C', 'ok': False, 'reason': '没有可执行的窗口，只记待办'}]          ← 保持原样，正确
```

- **我也复核了「waiting_external 没被塞进 veto」**：端到端形状里 run 级仍是 `部分完成 / exit 1`，不是「失败」——符合我和 Lead 共同的口径（等外部不是失败）。
- 我把它写成 `check_known_defect_external_scope_wording()`（严格），task-10 前是红的，现在绿。
- 断言的粒度说明（避免把「等外部」误报成必须失败）：这条只钉两件事 —— (1) 零窗口 + 等外部的 reason 必须说「等待外部 X」而不是「只记待办」；(2) 同一份记录里 note 说「有范围没完成」时，label 不许**不加限定**地只写「矩阵全部通过」（允许「矩阵全部通过（另有范围未完成：C）」这类补充写法）。run 级 `部分完成 / exit 1` 我认为是对的，**不**要求把 `waiting_external` 加进 veto。**Lead 已确认同此口径：task-10 明确「不许把 waiting_external 加进 veto」。**

### 不报为缺陷的两条（刻意保留的兼容取舍）

1. **老记录（无 `capability_gap` 字段）续跑时状态词仍是「部分完成」**，而集成 `verdict=no_evidence`。这是刻意的：`harness.scope_marks` 只认显式字段（人工拼的范围/老记录走老口径），`eval/check_harness.py:724-737` 那段老用例钉住了「零窗口范围 ⇒ 部分完成」（`:731` 断言 reason 是「没有可执行的窗口」、`:732` 断言 `contract_status(...) == "部分完成"`）。我实测续跑真实那份老 run json：缺口被认出（B/C）、note 点名、`verdict=no_evidence`、归档一致，**不炸**。两边口径不同是取舍，不是 bug。
2. **`check_binding_parity()`（T8 回滚）**：`create_widget_blueprint` / `bind_blueprint` 的 `_KIND_BY_ACTION` 映射被回滚是**正确判断**。我用自己的写法重验了这条守卫（读 producer 源码判断是否声明 `judge_kind`；没声明 ⇒ 表里不许先有映射，否则会造出永远 `passed=False` 的条款，把「没判过」说成「做坏了」）。同时反向钉住：`create_level` 的 producer **确实**声明了判词，所以映射必须在、条款必须绑得上。**真过。**

---

## 7. 我在自己这边堵掉的 3 处「假过」

写这个检查的过程中，我自己的断言先假过 3 次，都改掉了：

1. **「缺口 ⇒ checked 为空」** 这个前提在第一轮成立，但 T9 改了聚合口径后**不再成立**（缺口时可以由 veto 直接 `ok=False` 而不要求 checked 为空）。我改成断言真正重要的东西：`gaps 非空 ⇒ 没有任何矩阵项被判成通过、交付验收不许判 True`。
2. **「四个 label 两两不同」** 过度严格：T9 之后缺口的 label 合法地是「矩阵未全过：…能力缺口…」，同时含两个标记。我改成**只对三个互斥标记**做互斥断言。
3. **一条我写错的前提断言**：`plan_windows` 补出来的 `create_level` 扇的 `depends_on` 指向的是**上一扇的 id**（不是「action 列表的前一个」——两者在窗口 id 不连续时会错位）。这条是我自己的断言写错，不是产品缺陷。

---

## 8. 未覆盖的边界（明确没验的）

| 边界 | 为什么没覆盖 | 风险 |
| --- | --- | --- |
| **真实 LLM 规划/理解器** | 离线，不调模型；`complete` 一律是假函数或抛异常。 | 真实模型是否会在 `system_prompt` 里看到 `create_level` 之后就**真的**拆对（而不是补窗兜住），我没验。补窗是代码兜底，不是模型行为验证。 |
| **缺陷 A 的端到端可达性** | 我没造出确定的 stuck 窗口（见 §6 A）。 | 它可能只是潜在缺陷；我按「方向是假通过」报，没按「已复现」报。 |
| `check_upgrade` / `check_harness` 接入 CI 后的**实际 GitHub Actions 跑批** | 本机没有 actions runner，我只改了 yml 并本机跑同名命令。 | yml 语法/步骤顺序正确性我按现有风格核对过，但**没有真跑过 CI**。 |
| `retrieval_bench` 的检索质量 | 只跑退出码，没分析指标。 | 纯向量 1.000 / BM25 0.955 / 混合 1.000，与本次整改无关。 |
| 台账回写（`agent/ledger.py`）的人工可读性 | 只验「缺口范围记 `未完成（能力缺口）`、`ledger_status` 不许是 done」，没验台账文件实际写出来的内容。 | 低。 |

### 原本列在这里、现已由**真机终验**覆盖的一条

| 边界 | 状态 |
| --- | --- |
| **真实编辑器行为**（真的 `duplicate` / `save_assets` / `load_level` / `get_current_level`） | **已覆盖 —— 但不是我做的主，是 Lead 做的真机终验。** 我保持证伪方中立：以下证据是 Lead 提供的，我没有独立执行、也没有独立复核；我自己的那条链路仍然只到「假编辑器 + 生产代码路径」为止（见 §3、§6）。 |

Lead 的真机证据（生产代码路径 `agent/sequence._create_level`，非假件）：

- **前置**：`AssetTools.exists /Game/Maps/L_NewTest` = `true`；`/Game/Maps/L_NewTest2` = `false`；`get_current_level` = `/Game/Maps/L_NewTest`。
- **首次执行**：`ok=true`，summary「已从模板 /Game/Maps/L_NewTest 复制出 /Game/Maps/L_NewTest2；已成为当前关卡。」，`judge_kind="editor_assert"`，`level_path=/Game/Maps/L_NewTest2`，`in_level=true`。
- **独立复查**：另跑一次 `get_current_level` 返回 `/Game/Maps/L_NewTest2`（不复用返回值）。
- **磁盘**：`AIAgentProject/Content/Maps/L_NewTest2.umap` 60470 字节、22:56:02 落盘（`L_NewTest.umap` 60477 字节）。
- **幂等**：第二次执行 `ok=true`，summary「（已存在，不重复新建）；已成为当前关卡。」，未重复 `duplicate` —— 与我在 §3/§6 用假编辑器验到的两条路径**行为一致**。
- **条款链路**：`build_criteria(windows=[create_level 扇])` → `[('c-editor_assert','editor_assert',['W2'])]`；`apply_judge` 用真结果后 `passed=True` —— 与我的 `check_window_to_judge_chain()` 结论一致（我用假结果得到同一个绑定与同一个 `passed=True`）。

**这条覆盖的意义（对我的结论有什么影响）**：我原来把「真实工具层返回形状（`exists` 的 `data` 是不是 bool、`get_current_level` 的键名）」列为本报告**最大的未覆盖面**——因为 T1 的执行链如果对真实返回形状判断错了，假编辑器再逼真也验不出来。真机证据表明 `_create_level` 对真实返回形状的处理是**对的**（`exists` 走 false → `duplicate` → `save_assets` → `load_level` → `get_current_level` 复查命中；第二次走 true → 跳过 `duplicate`），且条款在真机上确实绑到 W2 并勾成 `passed=True`（T6 的 `_KIND_BY_ACTION` 映射不只是离线自洽）。
**但不改变**：缺陷 A/B 仍在（它们是矩阵聚合层，与建关卡链路无关），CI 真跑仍未做，真实 LLM 规划仍未验。

**我能独立核对的那一半（只读，不碰编辑器）**：上面那组证据里唯一落在文件系统上的那一项，我自己查过一遍 ——
`AIAgentProject/Content/Maps/L_NewTest2.umap` **存在，60470 字节，mtime 2026-10-24 22:56:02**；`L_NewTest.umap` **60477 字节**。与 Lead 报的数字**逐字节一致**。
这只证明「磁盘上真的多了一个从模板复制出来的关卡资产」，**不**证明「编辑器内部的当前关卡真的切过去了」——后者只能靠 `get_current_level` 的返回值，那是 Lead 在在线会话里做的，我没有执行、也没有复核。我把它写进来是为了标注「哪些部分我能佐证、哪些部分我仅转述」，不是为了让这份报告的结论更强。

---

## 9. 我给 CI 接了什么、为什么

`.github/workflows/offline.yml` 追加了 7 个 step（跟随现有 `check_xxx` / `run: python eval/xxx.py` 风格）：

```
check_harness            ← 之前不在 CI
check_upgrade            ← 之前不在 CI
check_create_level       ← 本轮新增（T1）
check_capability_gap     ← 本轮新增（T2/T6/T8）
check_archive_fidelity   ← 本轮新增（T5）
check_integrate_verdict  ← 本轮新增（T7/T9）
check_failure_192858     ← 本轮新增（我）
```

**为什么把 `check_harness` / `check_upgrade` 也接进来**：它们是 `run_windows` 聚合口径与范围判定的**老护栏**（`check_harness.py:773` 的「只有不适用项不算全过」、`check_upgrade` 的矩阵/台账/续接口径）。本轮所有缺陷（T9 假通过、A 语义反转、B 话术）恰好都发生在这些老护栏**没有覆盖到的新分支**上；把它们放进 CI 才能保证「下次改聚合层时，老口径先响」。二者都只依赖本仓库（临时目录 + 假件），CI 上可跑，我已本机确认 `exit=0`。

**为什么没接 `check_dsh_config` / `check_dsh_bridge`**：沿用 yml 里已有的说明——前者要同级 `AIAgentProject` 仓库与绝对路径 venv，后者要拉检索模型与 `data/chroma`。

**关于 `data/` 是 gitignore 的**：`check_failure_192858` 里真实任务书（`data/briefs/...`）与真实 run json（`data/runs/...`）缺失时，会打印一句说明并改用**同形状内联替身**（原句、A/B/C 交付清单、B/C 的旧 `pending_note` 与旧 `block_note` 一字不差），断言**照样全跑**；有真实件时则用真实件，并在输出里注明。归档件 `data/archive/20261024-192858/deliverables.json` 我加了**字节级红线**断言：跑完必须与跑前一致（它是历史证据，谁都不许动）。

**CI 形态自检（task-10 后我重跑过）**：把 `REAL_BRIEF` / `REAL_RUN` / `REAL_ARCHIVE` 三个路径都指向不存在的文件，再逐个跑所有 check —— 结果 **23 个 check 全 OK，0 FAIL**（含 A/B 那两条现在已转绿的断言）。
（task-10 之前跑同一次自检时是 21 OK / 2 FAIL，那 2 条正是当时未修的 A/B —— 两次对照说明缺文件只影响「用真件还是替身」，**不会让断言被静默跳过**。）

**没有污染仓库**：我对比了跑 `check_failure_192858` 前后的 `data/runs`（93）/`data/archive`（17）/`data/events`（10）/`data/handoffs`（1169）文件计数，**前后完全一致** —— 隔离是有效的，历史 run 记录与归档件一个字节都没动（`20261024-192858.json/.md` 的 mtime 仍是 19:29:34，归档件内容仍是 `{"hashes":[],"matrix":[]}` 那个坏样子）。

---

## 10. 我这轮没做的事（范围纪律）

- 没有改 `agent/` 下任何文件（缺陷 A/B 都只报给 Lead，由 task-10 交给 fix-gap）。
- 没有改 `eval/` 下已存在的文件（T1/T2/T5-T8 的自测文件一个都没碰；它们红了我会报缺陷，不会去改断言）。
- 没有碰真实编辑器，没有跑 `agent/run_windows.py` 真跑，没有 `git stash`。真机终验是 **Lead** 做的（§8 末），我没有参与，也没有独立复核。
- 只改了：`eval/check_failure_192858.py`（新建）、`.github/workflows/offline.yml`、`docs/verify/`（新建）。

---

## 11. 收尾（task-10 落地后我做的四件事，已完成）

1. ✅ 重跑全量 20 条 → **20/20 全 `exit=0`**（§2.1）；`check_integrate_verdict` 未被 task-10 波及，有单独核对记录（§2.1 末）。
2. ✅ §2.1 退出码表更新为 20/20，§0 结论改写为「真过、全绿」。
3. ✅ §6 的 A/B 两节从「未修」改标「已修（task-10）」，并补**我自己复跑**的修前/修后对照（含两条反向守卫：无 stuck 时必须仍是 `None`；零窗口 + 无外部时话术不许被改坏）。
4. ✅ CI 形态自检重跑 → **23 个 check 全 OK / 0 FAIL**（§9 末）。
5. ✅ 额外：`check_harness.py:773` 在 task-10 之后仍成立，按原场景原封不动复刻验过（§5）。

**过程中我没有做的事**（保持证伪方纪律）：没有为了让套件变绿去改任何断言强度；A/B 两条断言是缺陷还在时就写好的严格版本，代码修好后它们自己转绿。fix-gap 没有改过我的检查文件（我复核了 `eval/check_failure_192858.py` 内容与我自己写的一致）。
