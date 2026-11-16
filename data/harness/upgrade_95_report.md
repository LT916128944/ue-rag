# 从自证到端到端：20261111-1945 计划的施工结果

- 施工日期：2026-11-11 19:45 – 23:00
- 依据：`AIAgentProject/Plan/20261111-1945-从自证到端到端-抬到95.md`
- 结论：**W1 与 W2 的机制全部落地；W3/W4 按计划必须由人跑，今晚未跑。**
  记分口径改成「只算验过的分」之后，`check_scorecard.py` 报 **已验 55 / 未验 34**。
  **这个 55 不是退步，是原来那个 92 里 37 分本来就是没证据的。** 详见第 4 节。

---

## 1. 三条硬事实（先看这三条）

1. **`data/costs.jsonl` 丢了约 1480 行。** 施工中一个「清空后看 F 掉分」的断言误把真实文件截断。
   恢复途径全部试过并失败：该文件未被 git 跟踪（无历史）、E: 全盘只此一份、VSS 需管理员权限。
   **没有重建、没有补写、没有伪造任何一行。** 后果是 F 维诚实记「未验」。
   根因已修：那条断言现在只碰 tempfile，并新增一条可红断言钉住「检查不许改动真实数据」。
2. **`check_plan_parity.py` 现在是红的，而且应该红。** 它比对「离线规划」与「最近一次真机」的
   source，唯一 mismatch 是 `open-level`：离线 `rules(launch_editor→open_level)`，真机
   `20261111-181847` 是 `solver-fallback(solve→solve→integrate_verify)`。那是**修之前**的历史记录，
   修代码改不了历史。它要等你跑完 P7（十句 × 3 轮真机）才会真绿。**没有为了变绿去改比对口径。**
3. **今晚的 95 拿不到，因为 95 里 33 分（A 7 + B 16 + F 9 + I 2 中的大部分）现在没有证据。**
   计划第 1 节自己写过：「做完 W1+W2 而没跑 W3，最高 84，不许对外说 95」。而 84 里的 F(10) 今晚
   因为第 1 条已经不可验，I(4) 因为第 2 条不可验。所以今晚的诚实上限就是 **已验 55**。

---

## 2. 做完的部分（逐条对计划）

### W1 — 让分数说得清

| 计划 | 结果 | 证据 |
| --- | --- | --- |
| P1 基线分降级为「未验」 | 做了。每维多 `verified` / `basis` / `counted`；`total` 只加 `counted`，另出 `unverified_total`；终端两行 | `已验 55 / 未验 34（A、B、F、I）`；`data/harness/scorecard.json` |
| P2-I 健康检查不再写死 | 做了。`server/gateway.py` 顶层 `ok` 由五项探活派生；`check_scorecard` 起一个真 uvicorn 子进程、从进程外打真 HTTP `127.0.0.1:8011/api/health`，并要求「至少一项 ok=false 时顶层 ok 跟着 false」 | `eval/check_scoring_integrity.py` 断言 ③⑦ |
| P2-I 换机回执 | 做了。`shortcuts/setup.ps1` 不再自己写 `ok:True`，只转述 `check_all` 的退出码并记 `check_all_sha256` / `check_all_mtime`；`check_scorecard` 要求哈希一致且回执比 venv 新 | 断言 ④ |
| P2-F 成本分列 | 判据改成纯函数 `caller_split_detail(rows)`，只认真实 `data/costs.jsonl` 里 ≥2 个 caller。**真实数据现在只有 `stitch` 一个 caller，因此 F 诚实计未验** | 断言 ⑤ |
| P3 `check_distill` 进 CI + 缓存不得顶替重跑 | 做了。默认**一律当场重跑**；只有显式 `UE_CHECK_ALL_TRUST_CACHE=1` 才允许对 CI 内的检查吃缓存；**不在 workflow 的检查两种模式都拒吃缓存**；缓存说红一律采信 | 断言 ⑥（四种路径真红真绿） |

### W2 — 让离线验的就是在线跑的那条

| 计划 | 结果 | 证据 |
| --- | --- | --- |
| P4 双路径一致性 | 新增 `eval/check_plan_parity.py`。离线侧 `closed_env(UE_SOLVER_ENABLED="true")` + 抛异常替身（真的零模型调用），在线侧读 `data/events/<run_id>.jsonl` 的 `run_start`（权威 source + windows）。**当前红在 open-level，符合预期** | 见第 3 节对照表 |
| P4 修 open-level（优先项） | 按优先项「让在线也走 rules」做了两处：① `_solve_fallback_windows` 开头加闸——rules 已过 `_rules_cover` 就返回 `[]`；② 新增 `source_of_windows()`，`run_windows` 发 `run_start` 前按最终窗口校正 source（来源不再撒谎）。附带修了 `_clause_rows` 的关卡名闸门、`_ensure_requested_create_level` 的壳扇去重、`_merge_rule_windows` 的签名与依赖重映射 | 离线 open-level 现在稳定 `rules` |
| P5 文档三处漂移 + 一条结构断言 | 四条断言进 `eval/check_risk.py`（只在末尾追加，原有 10 条一字未改）：求解开关只认真值、`setup.ps1` 不许说「自己跑」、`UE_ALLOW_*` 必须全进「换机器」表、`rules`/`hud-rules`/`learned` 三条分支的闸门落点必须有文档说明 | `eval/check_risk.py` exit 0，0.22s；四条各自破坏一次确认会红 |

### W3 — 让 B 有生产者（代码部分做完，30 条记录必须人跑）

| 计划 | 结果 |
| --- | --- |
| P6 生产者 | 新增 `agent/bench_record.py`。只从 `data/runs/<日期>/<run_id>.json` 取 `sentence` 与 `criteria[].evidence_sha1`；不读手写 JSON、不读模型文本；无 `sentence` 或无任何 `evidence_sha1` 一律拒绝。写 `verdict.json` 并加 `round` / `record_sha256` |
| P6 `--verify` | `eval/run_bench.py --live --verify` 逐份复算哈希、核 `run_id` 在 `data/runs/` 里真有记录、核 `sentence` 与 `status` 一致。现在因 16 份历史记录全是手写的而整条失败——**这是对的** |
| P6 `data/archive/` 移出 gitignore | 做了 |
| P6 谓词 | 新增 `eval/check_bench_provenance.py`，三条正向断言 + 每条都配一条反向断言（防恒红） |
| P7 十句 × 3 轮 | **没有跑，也不该由模型跑。** 见第 5 节 |

---

## 3. `check_plan_parity` 当前对照（计划 P4 要求的输出）

| 句子 | 离线 source | 最近一次真机 source | 判定 |
| --- | --- | --- | --- |
| compile | rules | rules | ✅ 一致 |
| open-level | rules | solver-fallback | ❌ 不一致（历史记录，修前） |
| place-coins | planner | — | no-record |
| platform-191740 | planner | — | no-record |
| fps-hud | hud-rules | — | no-record |
| shooter | solver-fallback | — | no-record |
| grenade | solver-fallback | — | no-record |
| create-level | planner | — | no-record |
| mi-coin-scalar | solver-fallback | — | no-record |
| patrol-presentation | solver-fallback | — | missing（预期 partial，但必须有记录） |

计入 2 句：一致 1、不一致 1。**要 ≥9 句一致才绿，所以必须跑 P7。**

---

## 4. 分数（`data/harness/scorecard.json` 原文口径）

| 维 | score | verified | basis | counted | 缺口 |
| --- | ---: | --- | --- | ---: | --- |
| A 验收边界 | 7 | false | baseline | 0 | 没有哈希相符的人签 |
| B 整句闭环 | 16 | false | baseline | 0 | 档案不足 10 句×3 轮 |
| C 合同 | 15 | true | evidence | 15 | |
| D 编排与续接 | 12 | true | evidence | 12 | |
| E 后端与回滚 | 12 | true | evidence | 12 | |
| F 观测 | 9 | false | baseline | 0 | 只有 1 个 caller，成本没按两个 caller 分列 |
| G 记忆 | 9 | true | evidence | 9 | |
| H 维护 | 7 | true | evidence | 7 | |
| I 部署 | 2 | false | baseline | 0 | 回执哈希与现在的 check_all.json 不一致 |
| **合计** | **89** | | | **55** | 未验 34 |

为什么 I 现在不可验：`check_all` 有 1 条红（就是第 2 节说的 `check_plan_parity`），
而回执只许转述 `check_all` 的结论。所以「先让 check_all 全绿，再跑一次 `setup.ps1` 重写回执」
这一步现在做不了——**这是制度在说真话，不是 bug**。

### 与计划第 6 节表格的差额，逐条交代

- 计划 W2 后 = 84，其中 **F 10 分**要求真实 costs 里有两个 caller → 遥测丢了，今晚不可验。
- 其中 **I 4 分**要求 check_all 全绿 + 回执一致 → 被 P4 的预期红挡住，今晚不可验。
- 计划那 84 里 **A 2.3 / B 5.3** 是计划作者自己的「部分给分」口径；`check_scorecard.py`
  不做部分给分（`verified` 是布尔），所以这两维在本脚本里只能是 0 或满分。这个差异不是我改出来的。

---

## 4.1 最终验证（我亲手串行跑的，一次一个，带超时）

`check_all.py` 全量：**48 项，47 绿，1 红**（红的就是 `check_plan_parity`，P4 预期红）。
之后又单独复跑了动过的每一条：

| 检查 | 退出码 | 说明 |
| --- | --- | --- |
| `check_scoring_integrity` | 0 | 8 条断言全绿，62s（预算 240s） |
| `check_scorecard` | 0 | `已验 55 / 未验 34（A、B、F、I）` |
| `check_risk` | 0 | P5 四条新断言 |
| `check_create_level` | 0 | 曾因 P4 改动红过，已修 |
| `check_failure_192858` | 0 | 曾因 P4 改动红过，已修 |
| `check_bench_provenance` | 0 | P6 三条断言 |
| `check_plan_parity` | **1** | P4 预期红，需 P7 |
| `check_distill` | 0 | 新增进 CI |
| `check_statuses` | 0 | 先于本轮就红，已修 |
| `check_review_coverage` | 0 | 先于本轮就红，已修 |
| `run_bench.py --live` | 2 | 不满 30 条，不打印百分比 |
| `run_bench.py --live --verify` | 1 | 16 份历史记录全无产地，整条失败（正确） |

施工中还抓到并修掉一条**新写的恒真断言**：`check_scoring_integrity` 的 ③④ 原先都建立在
「I 的另一半本来就红」上（`I = health_ok and setup_ok`），所以把 health 做坏分数也不动。
现在 ③ 用临时夹具把 setup 造到真通过、让 health 成为唯一变量（**4 → 2 → 4，counted 4 → 0 → 4**），
④ 反过来钉住 health、让 setup 成为唯一变量。两条互为对照，任一条的夹具被换成空目录都会 FAIL。
**这就是这份计划说的「每条谓词要么能红，要么不算分」，连新写的检查也得过这一关。**

## 5. 剩下的必须你亲手做的事（按顺序）

```powershell
# 0) 前置：编辑器与 MCP 起好。工作目录 AIAgentProject
start.bat 3          # 把 bench_manifest.json 里那一句贴进去，等它跑完

# 1) 每跑完一句，在 RAG/ue-rag 记一轮（这就是 B 的唯一合法生产者）
python agent/bench_record.py --run-id <刚跑出来的 run_id>

# 2) 十句 × 3 轮跑满（30 条），然后：
python eval/run_bench.py --live --verify     # 必须 exit 0 且打印成功率
python eval/check_bench_provenance.py
python eval/check_plan_parity.py             # 这时才应该绿

# 3) check_all 全绿之后，重写换机回执（I 的那 4 分只认它）
shortcuts/setup.ps1

# 4) 记分
python eval/check_scorecard.py
```

**记分口径（容易看错的三条）**
1. `place-coins`：monolith 下已有实例的属性改动必须在拆句成为 `unresolved`。记成 `ok` 算假通过。
2. `patrol-presentation`：人还没签时**预期 `partial`**。`partial` 进分母、不算成功。
3. 其余八句以 `ok` 为成功。九句全 `ok` + 巡逻三轮 `partial` = 27/30 = 90%，正好踩线。

**W4 人签（可选，最后 1 分）**：按 `Plan/patrol-presentation.json` 逐项核完，写
`RAG/ue-rag/data/attestations/patrol-presentation.json`，`evidence_sha1` 必须是证据文件的字节哈希。
签完 A 到 8、B 的 90% 变 30/30，总分到 96。

**做完这些的理论上限**：A8 + B19 + C15 + D12 + E12 + F10 + G9 + H7 + I4 = **96**；
不签 W4、B 停在 90% 时是 **95**。

---

## 6. 本轮改动的文件

### 计划内
- `eval/check_scorecard.py` — P1 口径、P2 进程外健康探针与回执校验、F 纯函数判据、P3 缓存策略、子进程硬超时
- `eval/check_scoring_integrity.py`（新）— 八条断言，模块头把「只读真实数据、写只落 tempfile」写成硬规则
- `eval/check_plan_parity.py`（新）— P4
- `eval/check_bench_provenance.py`（新）— P6 谓词
- `agent/bench_record.py`（新）— P6 生产者
- `eval/run_bench.py` — `--live --verify`
- `eval/check_risk.py` — P5 四条断言（只在末尾追加）
- `agent/plan_windows.py` — P4 修 open-level + 两个回归修正
- `server/gateway.py` — 顶层 `ok` 派生
- `shortcuts/setup.ps1` — 回执只转述 check_all
- `.github/workflows/offline.yml` — 加四条：`check_distill`、`check_scoring_integrity`、`check_plan_parity`、`check_bench_provenance`
- `.gitignore` — `data/archive/` 移出
- `AIAgentProject/WORKFLOW.md`、`AIAgentProject/README.md`、`.env.example`（只那一行）

### 计划外，逐条说明理由
1. **`config.py`** — 加三个**只读注入点**：`UE_DOTENV_PATH`、`UE_DATA_DIR`、`UE_MCP_INDEX_PATH`。
   不加就无法起一个「配置自由」的子进程，P2 的「至少一项探活 ok=false」造假就验不成。
   已实测：不设这三个变量时 `ENV_PATH` / `DATA_DIR` / `MCP_INDEX_PATH` 与改动前同名同值。
2. **`agent/run_windows.py`** — 发 `run_start` 前调 `source_of_windows()`。不加则事件流写的 source
   与实际窗口不符（plan_windows 说 rules、跑的是 solve），P4 的在线侧证据就不可信。
3. **`eval/check_statuses.py`** — 修一条**先于本轮就红**的检查：`covered/uncovered/constraint`
   是 `_clause_rows` 的半句覆盖率词表，按 `agent/statuses.py` 文件头「不在本文件范围的词表要逐条
   显式跳过并写明原因」登记；另加 `check_half_clause_vocabulary()` 钉住这三值、防改名后跳过清单退化成死条目。
4. **`eval/check_review_coverage.py`** — 修另一条**先于本轮就红**的检查。**归因不是「多了噪音行」**：
   金标准与实际表格逐字相同，差的只是中间列宽 36 vs 35。根因是 rich 在 Windows 控制台
   `legacy_windows=True` 时把宽度算成 79、CI 上是 80。修法是把渲染宽度钉死 80，让断言只测内容不测终端宽度，
   并加 `table.count("现状：") == 1` 钉住这个功能在且没被复制成噪音。原有断言一条没放宽。

### 没做什么
- 没跑 P7 的 30 条真机记录（计划明令模型不代跑）。
- 没写 `data/attestations/`（人签）。
- 没改 `feature_list.json`。
- 没改 `Source/`、`Content/`、蓝图、`.env`。
- 没重开 `docker` / `metrics-stack` / `retry` 那批 `wont`。
- 没为了变绿放宽任何一条断言；`check_plan_parity` 现在是红的，就该红。
