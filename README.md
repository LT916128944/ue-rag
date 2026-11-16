# ue-rag

Unreal Engine 5.8 项目的检索库和执行代理。把资产、C++、蓝图图、文档和历史错误做成向量库，用自然语言查项目；要改编辑器时，按 `.env` 里锁定的那一个后端调工具。

Python 3.12。模型走 DeepSeek，向量库是 Chroma，嵌入模型是 `BAAI/bge-small-zh-v1.5`。

操作手册在旁边的 [AIAgentProject/WORKFLOW.md](../../AIAgentProject/WORKFLOW.md)。本文只写怎么准备、怎么跑。

## 准备

换机可以跑 `shortcuts\setup.ps1`：找到 Python 3.12，建 `.venv`，`pip install -r requirements.txt`。`.env` 没有才从 `.env.example` 复制，已有则不动。然后它打印三条下一步：`python eval/check_all.py`、`shortcuts\ingest.bat`、`python server/entry.py --port 8011`（**这一步就够**：服务会自己打开浏览器；`--no-open` 可关掉）。不下载 UE，不写密钥。

也可以自己做：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

入库脚本会把 Hugging Face 端点设为 `https://hf-mirror.com`。

`.env.example` 里未注释的项是当前这台机器正在用的值。`.env` 不提交。键空着或没写时，走 `config.py` 的缺省。

| 变量 | 代码缺省 | `.env.example` 里正在用的值 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 空，必须自己填 | 空 |
| `UE_LLM_MODEL` | `deepseek-chat` | `deepseek-flash` |
| `UE_EDITOR_BACKEND` | `official` | `monolith` |
| `UE_MCP_URL` | `http://127.0.0.1:8000/mcp` | 注释掉，只给 `official` |
| `UE_MONOLITH_URL` | `http://127.0.0.1:9316/mcp` | 同缺省 |
| `UE_ALLOW_BLUEPRINT` 及材质 / UMG / 动画 / Niagara | `false` | `true` |
| `UE_ALLOW_AI` | `false` | `true` |
| `UE_ALLOW_MONOLITH_WRITE` | `false` | `true` |
| `UE_ALLOW_PYTHON` | `false` | `true`。`editor.run_python` 的缺省放行；任务显式 `allow_python: false` 仍拒绝（DSH session_task.json 优先） |
| `UE_DEFAULT_SAVE` / `UE_DEFAULT_PIE` | `true` | `true` |
| `UE_EXEC_MODE` | `real` | 注释掉。`mock` 才启用注入错误 |
| `UE_SOLVER_ENABLED` | `false` | `true`。表外需求补不出窗口、或理解器 goals 盖不住整句时进求解兜底（预算闸门见 WORKFLOW） |
| `UE_SOLVER_EDITOR_CALLS` / `UE_SOLVER_BUILD_ROUNDS` / `UE_SOLVER_MODEL_CALLS` | 8 / 3 / 6 | 注释掉 |
| `UE_SOLVER_META_CALLS` | 12 | 注释掉。discover / describe / 列测试走这份额度；describe 命中 `capability_surface.json` 的 params 缓存时不占 |
| `UE_SOLVER_NAMESPACES` | `editor,mesh,niagara,blueprint,material` | 注释掉。monolith 没有 `asset` namespace，别再往里放 |

`UE_EDITOR_CMD`、`UE_PROJECT`、`UE_SOURCE_ROOT` 空着就用 `config.py` 的本机路径。换机器再改。

任务没写 `save` / `pie` 时跟随上面两个缺省，缺省都会保存、都会允许 PIE。要关掉，任务里写 `save: false` / `pie: false`，或 CLI 加 `--no-save` / `--no-pie`。

## 编辑器后端

一次运行只连一个 MCP。`backends/select.py` 在入口读一次 `UE_EDITOR_BACKEND`，本进程之后不跟 `.env` 改口。选错入口直接拒绝，不改连另一边。

| 后端 | 怎么调 |
| --- | --- |
| `official` | `scripts/scan_mcp_tools.py` 出卡片，`--schema` 取一个工具，`scripts/call_mcp_tool.py` 经 `core/editor_exec.py` 打到 8000 |
| `monolith` | `scripts/monolith_backend.py` 的 `--discover` / `--describe` / `--exec`，经 `backends/monolith.py` 打到 9316。不扫官方 830 个工具，不传 `detail=true` |

两边同时在听时，启动会被拒绝：`monolith` 要关掉插件 Unreal MCP（ModelContextProtocol），`official` 要关掉 Monolith，然后重启编辑器。PCG 在 Monolith 上没有命名空间，调用失败，不回退 8000。

`backend=monolith` 时，`compile_blueprint`、`bind_blueprint`、`create_widget_blueprint`、`edit_widget_blueprint` 对同一张蓝图最多修 3 次。第 3 次仍失败就停，错误含「已尝试 3 次，Monolith 不具备该能力」。对应的 `UE_ALLOW_*` 不是 true 时次数是 0。场景/资产/材质原语（find_actors、add_to_scene_from_asset、set_actor_transform、save_assets 等）由 `core/monolith_bridge.py` 翻译成对应 monolith action，映射外的原语带原因拒绝。`eval/check_backend_coverage.py` 里除 `judge_pickup`、`set_pickable`（没有 `get_properties` / `set_properties`）标 `partial` 外，动作清单是 `both`。monolith 下这两条在拆句转入 unresolved，不进执行窗口；官方后端仍可进窗口。写前快照方式是 `compensating`，不代替 Undo。内容条款被「编辑器已打开」翻绿的形状由 `eval/check_criteria_binding.py` 钉住。翻牌带判词 `evidence_sha1`；回放时印章对不上，条款回到 false。`run_windows` 收尾写整句 `ok` / `partial` / `failed`，退出码是 0 / 3 / 1。有活心跳仍是退出码 2。`manual` 只由人写的 `data/attestations/<criterion_id>.json` 翻，模型文本不改 `passed`。

`/api/health` 返回 `ok`、`model`、`mcp_url`、`mcp_up`，以及 `backend`、`index_present`、`circuit_closed`、`checkpoint_writable`、`disk_free_mb`。后五项各带 `ok` 和 `reason`。不调用模型。本机把 CI 里已有的离线检查串起来用 `python eval/check_all.py`。分数由 `python eval/check_scorecard.py` 按证据写入 `data/harness/scorecard.json`。成本按 `caller` 分列，没有行的入口不填 0。

浏览器侧的 HTML 入口层是 `server/entry.py`（`python server/entry.py --port 8011`，127.0.0.1:8011），入口页在 `server/static/entry.html`（本仓库内，页面由本服务提供，`GET /` 直接读那个文件）：目录侧边栏（`/api/tree?path=` 一次只列一层，屏蔽 `Binaries / Intermediate / Saved / DerivedDataCache / .vs`）、拖拽附件、装配预览、运行面板（卡片与连线来自 `data/runs/**/*.json` 与 `data/events/<run_id>.jsonl`）、决策面板（只在出现 `question` 事件时弹出）。**读**走 `/api/runs`、`/api/run/<id>`、`/api/stream/<id>`、`/api/tree`、`/api/health`、`/api/questions/<id>`；**写**只有三个文件——`data/ui/req/<ts>.txt`（`POST /api/run` 写一行需求，再 `subprocess` 起对应模式的 agent 脚本；多窗口带 `--plain --force`）、`data/ui/inbox/<run_id>.jsonl`（`POST /api/answer` 一行一条裁决）、`data/ui/logs/<ts>.log`。装配预览 `POST /api/preview` 仍是纯函数（`server/prompt_builder.py`）。这一层只 import `config/core/server`，不 import `agent`——`run_guard` 的活 run 互斥、`UE_RAG_STDIN` 的原子改名、`editor_lock` 的跨进程锁都长在「一个进程一条 run」上，UI 只喂文件、只读文件。`--force` 由 `_spawn` 按请求体透传，改名（`.consumed`）在 `subprocess` 打开之前完成，`--plain` 只挂多窗口那条路——三条都由 `eval/check_entry_spawn.py` 钉住（原 `check_start_bat.py` 钉的是旧版菜单式 `start.bat`，那份菜单已删除；现在的 `AIAgentProject/start.bat` 只做启动，不含任何菜单与 run 逻辑）。

裁决通道在 `agent/ui_ask.py`，四条口径：`question` 事件发在 `input()` **之前**（带 `question_id/kind/window/prompt/options[]/default`）；`options` 与 `_pause_hint` 的同一份字面生成，UI 按钮与终端提示不会漂移；收件箱按 `run_id`+`question_id` 双键、**领取即改名**（`os.replace`，两进程只有一个拿到），对不上的行丢弃并告警；**缺行不许当成自动 skip**——等不到答案就按 `timeout` 收口、走与 EOF 同一条「停止」路。`question`/`answer` 不进 `cli_log._VISIBLE`（它们给人看，不是子 CLI 日志）。`entry.py` 从人写的 `UE_RAG_UI_INBOX` 透传 `--ui-inbox`；不设就只有终端裁决。

## 入库

资产导出要在编辑器 Python 里跑，其余可以在本目录跑：

```powershell
python pipelines/export_cpp_metadata.py
python pipelines/export_blueprint_graph.py
python pipelines/export_docs.py
python pipelines/ingest_assets.py
```

`ingest_assets.py` 读取 `data/` 下已有的 JSONL，写入 `data/chroma/`，collection 名是 `ue_assets`。快捷方式是 `shortcuts\cpp.bat`、`shortcuts\asset.bat`、`shortcuts\mcp.bat`。

## 使用

只检索、不改工程：

```powershell
python agent/chat.py
```

按任务文件跑闭环，退出码 0 表示裁判通过：

```powershell
python agent/run_task.py --task eval/tasks/bp-gun-compile.json
```

用一句话跑。菜单 2 是 `run_multi.py`，菜单 3 是 `run_windows.py`，两边共用 `plan_windows`：

```powershell
python agent/run_multi.py
python agent/run_windows.py
python agent/run_windows.py --resume latest
```

`server/static/entry.html` 就是这四条入口的页面形态：模式「普通对话 / 单循环 / 多窗口」，加「续接最近」。双击 `AIAgentProject\start.bat` 即起服务并开页（`start.bat` 只做启动，不做菜单；三个模式在页面上选）。

项目语料 MCP（stdio）提供 `search_ue_project` 和 `get_project_summary`：

```powershell
python server/server.py
```

DSH 外壳见 `dsh/README.md`。工具仍是 9 个 `mcp__uerag__*`；其中扫描、取 schema、执行会转到当前锁定的后端。`allow_writes` / `save` / `pie` 只从人写的 `data/dsh/session_task.json` 读。

只读诊断句（「看看 / 检查 / 核对 / 是否有问题」，且没有「实现 / 新建 / 创建 / 修改 / 摆 / 放置」）由 `agent/diagnose.py` 归到源码里唯一的一条 Automation 过滤器，收成已有的 `run_automation_test`。零条或多条都不挑，也不往动作表加行。

核对评测清单（不打开编辑器、不调模型）：

```powershell
python eval/run_bench.py
```

`--profile pipeline` 跑蓝图编译，`--profile slow` 再加整工程编译，`--profile agent` 跑会调模型或改未保存关卡的任务。改关卡的任务不计入成功率。

## 目录

| 路径 | 内容 |
| --- | --- |
| `agent/` | 对话、任务环、多窗口、理解器、诊断归地 |
| `backends/` | 一次运行锁一个后端：`select.py`、`official.py`、`monolith.py` |
| `core/` | 检索、MCP 客户端、裁判、官方执行器 |
| `pipelines/` | 导出元数据并写入向量库 |
| `scripts/` | 官方扫描 / 单次调用，以及 `monolith_backend.py` |
| `tools/llm-gateway/` | 本地模型名映射网关（Node，零依赖），见该目录 `README.md` |
| `server/` | 项目语料 MCP、六工具契约、DSH 网关 |
| `dsh/` | DeepSeek Harness 配置模板，不是第二套闭环 |
| `eval/` | 任务和可恢复评测 |
| `docs/` | 选型记录 |
| `data/` | JSONL、向量库、运行痕迹。`harness/feature_list.json` 决定完成与否 |

`.env`、`data/chroma/`、轨迹、运行记录和 `data/ue_mcp_index.json` 不入库。
