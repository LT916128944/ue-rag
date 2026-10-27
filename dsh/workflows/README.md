# dsh/workflows —— 只读研究模板

DSH 在这里只做**只读并行研究**与任务书；执行仍回 `python agent/run_windows.py`，两边共用同一份 handoff 格式（`data/handoffs/<run>/<window>.json`）。不追求「DSH 跑闭环」。

| 文件 | 入参 | 产出 |
| --- | --- | --- |
| `research.js` | `{question, top_k=5}` | `data/briefs/<ts>-<slug>.md` + 同名 `.json`（`{goal, findings[], files[], unresolved[], next}`） |
| `triage.js` | `{error_text}` | `{root_cause, evidence_kind, next_step, confidence}`；证据对不上给 `unknown` |

## 怎么用

1. 把对应 `.js` 的**全文**贴进 DSH `workflow` 工具的 `script`，`args` 按上表给；脚本顶层 `return` 的就是结果。
2. 也可以让会话直接读这个文件再贴——文件本身是唯一正文，不要在别处再抄一份。
3. `research.js` 用 `pipeline` 分三路读（C++ / 蓝图资产 / 文档历史），`triage.js` 用 `pipeline` 查两处（`source_type=ue_error` / `doc_kind=debug`）。

## token 预算

- 每个 workflow **≤6 个子代理**（`research.js` 5 个、`triage.js` 2 个）。
- 结果 **≤8KB**；子代理只回结论，不回正文。
- **禁止**把整份文件塞进子代理提示；每路最多读 5 个文件，只读需要的片段。

## 只读红线

- 不得调 `exec_editor_tool`、`window_state`；不得调 `verify` 去改 `passed`。
- 不得写 `.uasset` / `.umap` / `Source/` / `Content/`；不跑 git。
- 唯一允许的写入是 `data/briefs/` 下的任务书。
- 要动编辑器：回到 `run_windows`，DSH 侧只经 `mcp__uerag__*` 网关。

## 校验

```powershell
python eval/check_dsh_lock.py   # dsh-lock ok（写锁并发 / 900s stale / 伪造 pid）
python eval/dsh_ab.py           # 打印 A/B 表；DSH 侧数值由人填 data/harness/dsh_ab.json
```
