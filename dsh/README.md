# DSH 外壳（DeepSeek Harness）

本目录只放 DSH 的配置模板与说明，不是第二套闭环。DSH 负责「对话 / 研究 / 只读并行 / 任务书」，`RAG/ue-rag` 仍是检索、执行、裁判、回滚内核。

## 文件

| 文件 | 作用 |
| --- | --- |
| `cordis.patch.yml` | DSH profile 的 patch 条目模板（薄 MCP 网关 + 技能目录 + 目录长度） |
| `README.md` | 本文件：安装、工具面、策略文件、校验、回滚、禁止项 |
| `workflows/` | 只读并行研究模板（`research.js` / `triage.js`）与用法，见 `workflows/README.md` |

## 安装（人做）

0. 预检：先跑 `shortcuts\dsh.bat`（内部调 `shortcuts/dsh_preflight.py`），venv / `.env` / 配置 / 索引 / 编辑器连通有一项不过就先修那一项，再往下走。
1. 备份：`C:\Users\X\.dsh\profiles\desktop\cordis.patch.yml` → `C:\Users\X\.dsh\backups\ue-rag\cordis.patch.<ts>.yml`
2. 把 `cordis.patch.yml` 的条目**追加**到 profile patch。DSH patch 方言（`cordis-composition-reference`）：
   - `mcp-uerag` 是本项目新增行 → 必须写在 `- insert:` 列表里；裸行会被当成覆盖、按 id 找不到就跳过；
   - `skill-filesystem` / `tool-skill` 是 `@deepseek-ai/dsh-base` 已有行 → 裸行按 id 覆盖，`config` 整条替换、不是深合并；
   - 模板已按该方言写好，直接抄。
3. 技能发现：DSH 默认扫 `<projectRoot>/.dsh/skills` 与 `<projectRoot>/.agents/skills`。本工作区根不是 git 仓库，projectRoot 回退为工作区根，所以建联接（模板的 `customSkillDirs` 指同一目录，作冗余）：
   `New-Item -ItemType Junction -Path 'E:\UEProject\AIWork\.dsh\skills' -Target 'E:\UEProject\AIWork\AIAgentProject\.agents\skills'`
4. 复制 `data/dsh/session_task.example.json` 为 `data/dsh/session_task.json`（不提交）。第一次先保持 `allow_writes: []`。
5. 重启 DSH（patch 在启动或热加载时生效），`/permission` 设 `workspace-write`，工作区 `E:\UEProject\AIWork`。

## 工具面（9 个，`mcp__uerag__*`）

| 工具 | 作用 |
| --- | --- |
| `search_project` | 语义检索项目语料（资产/C++/蓝图/文档/历史错误） |
| `scan_editor_tools` | 按自然语言找编辑器工具，只出卡片（无 schema） |
| `get_editor_tool_schema` | 只取一个工具的 schema |
| `exec_editor_tool` | 调 `core/editor_exec.exec_tool`；写策略只读 `session_task.json` |
| `read_contract` | 读 run 的 criteria/passed/状态 |
| `verify` | 跑对应裁判；只有 `judge.ok && kind` 匹配才把 passed 改 true |
| `read_handoff` | 读扇间交接 JSON（不带工具正文） |
| `window_state` | `snapshot` / `restore` 源码快照（不保存关卡） |
| `resume_context` | goal/phase/pending/最新 unmet，≤200 字 |

## 策略文件

`data/dsh/session_task.json`（人写，不提交）：

```json
{"allow_writes": [], "save": false, "pie": false, "allow_blueprint_ref": ""}
```

- 文件缺失 → 所有写操作拒绝（fail-closed）。
- 模型参数里没有 `allow_writes/save/pie`，网关只读文件。
- 拒绝结果带 `policy`（当时的 save/pie/allow_writes 与五个 `UE_ALLOW_*`）。

## 校验（离线）

```powershell
python eval/check_dsh_config.py    # dsh-config ok
python eval/check_dsh_bridge.py    # dsh-bridge ok（mock）
python eval/check_dsh_contract.py  # dsh-contract ok（假 judge，不连编辑器）
$env:UE_EXEC_MODE="mock"; python eval/check_dsh_lock.py   # dsh-lock ok（写锁并发 / 900s stale / 伪造 pid）
python eval/dsh_ab.py              # A/B 表（DSH 侧数值由人填 data/harness/dsh_ab.json）
python shortcuts/dsh_preflight.py  # venv/.env/配置/索引/编辑器连通
```

`check_dsh_bridge.py` 第一次会拉起检索模型（约 1–3 分钟）；若报 180s 超时不是回归，模型热缓存后重跑一次即可。

## 技能与合同（P3）

| 技能 | 触发 |
| --- | --- |
| `ue-dsh-route-skill` | 动编辑器、编译、Automation |
| `ue-dsh-verify-skill` | 说完成 / 修好 / 通过 / 已生成之前 |
| `ue-dsh-handoff-skill` | 多步骤或多扇收尾 |

说完成之前先 `verify`。没有 `{ok:true, kind}` 的条款就是没完成。`manual` 由人在 `Plan/` 里绑，模型不改。

```powershell
python agent/dsh_cli.py verify --run <run_id> --criterion <id>
python agent/dsh_cli.py verify --run <run_id> --all
python agent/dsh_cli.py verify --run <run_id> --criterion <id> --dry-run
python agent/dsh_cli.py handoff-validate --file data/handoffs/<run_id>/<window_id>.json
python agent/dsh_cli.py handoff-merge --file <DSH 子代理产出的单窗交接.json> --run <run_id>
```

- `handoff-validate` 按 `harness.build_handoff` 的字段与截断口径校验：`did`/`next`/`goal` ≤240 字、`evidence[].summary` ≤240、`unmet` ≤8 条且 `unmet_total - len(unmet) == unmet_omitted`、`artifacts` 只留 `{ref, in_level}`、出现 `read_source` 即失败。
- `handoff-merge` 先校验再复制到 `data/handoffs/<run>/<window_id>.json`；目标已存在直接拒绝（不覆盖），只收单窗文件（带 `windows`/`criteria` 的 run 记录不收）。`--window` 缺省取源文件名。

- `criterion.windows` 为空则失败，不猜窗口。
- 同一窗口多个同 kind 的 spec 按顺序跑，采用第一个返回且 kind 匹配的裁判。
- `--all` 按合同顺序跑未过且非 manual 的条款；一条失败不跳过后面的条款。
- `--dry-run` 只算不写。真实写只经 `apply_judge`，裁判必须带 `kind`。
- 拒绝结果带 `policy`。模型文本「已完成」不改 `passed`。

DSH 里人工确认：技能目录出现 `ue-dsh-*`；工具列表恰好 9 个 `mcp__uerag__*`。

## 只读工作流（P4）

`workflows/research.js` 与 `workflows/triage.js` 是 DSH `workflow` 工具的脚本模板：贴全文进 `script`，`args` 给 `{question, top_k}` 或 `{error_text}`。

- 只用 `pipeline`，不设阶段屏障；每个 workflow ≤6 个子代理，结果 ≤8KB，每路最多读 5 个文件。
- 只读红线：不调 `exec_editor_tool` / `verify` / `window_state`，不写工程文件，不跑 git；唯一写入是 `data/briefs/` 下的任务书。
- DSH 不跑闭环：要动编辑器回 `run_windows`，两边用同一份 handoff 格式。

细节与 token 预算见 `workflows/README.md`。

## 真机验证三步（人做，DSH 开着）

1. 检索：问「项目里有哪些金币相关资产」，应答走 `search_project`（含 `asset_path` / `class_name`），不是列目录、不是回退文件扫描。
2. 写拒绝：`data/dsh/session_task.json` 为空（`allow_writes: []`、`save` / `pie` false）时请求改 Actor，应回 `write refused` 且带 `policy`。
3. verify 改合同：说「已完成」但不调 `verify`，再 `read_contract` 该条款仍为 false；模型文本不改 `passed`。

任一不过，先看 `python shortcuts/dsh_preflight.py` 与 `python eval/check_dsh_bridge.py` 的输出。

## 隐私与排障

- 隐私：DSH 设置里「在使用官方模型 API 时上传 Session Log」开关默认开启，接入前先确认这一项；不在 DSH 会话里贴密钥，`.env` 由服务自己从 cwd 读。
- 排障：E 盘沙箱授权失败（`SetNamedSecurityInfoW ... grantWrite`）先跑 `diagnose-windows-sandbox-acl` 技能；临时 Full access 只用于研究任务，不跑 UE 写。

## 回滚

用备份恢复 profile，重启 DSH；不再需要时删 `E:\UEProject\AIWork\.dsh\skills` 联接。仓库内文件可保留，不影响菜单 1/2/3。

## 禁止

- 不要接官方 UE MCP 的 830 个工具：DSH 会把 server 全部工具 schema 注入每个请求。
- 不要把 `.cursor/skills` 同时加进 `customSkillDirs`：与 `.codex/skills` 重复。
- 不要用 DSH 的 read/write 直接改 `.uasset` / `.umap` / `Source/`；UE 写操作只经网关。
- 不要指望 DSH 的 plan mode 或审批替代 `core/editor_exec.py` 的写闸门。
- 不要把 `.env` 密钥写进 patch；stdio 子进程会清掉 `*KEY/*TOKEN/*SECRET/*PASSWORD*`，服务自己从 cwd 的 `.env` 读。
