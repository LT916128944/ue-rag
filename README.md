# ue-rag

Unreal Engine 5.8 项目的检索库和执行代理。把资产、C++、蓝图图、文档和历史错误做成向量库，用自然语言查项目；要改编辑器时，按 `.env` 里锁定的那一个后端调工具。

Python 3.12。模型走 DeepSeek，向量库是 Chroma，嵌入模型是 `BAAI/bge-small-zh-v1.5`。

操作手册在旁边的 [AIAgentProject/WORKFLOW.md](../../AIAgentProject/WORKFLOW.md)。本文只写怎么准备、怎么跑。

## 准备

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
| `UE_SOLVER_ENABLED` | `false` | `true`。表外需求补不出窗口时自动补一扇受控求解扇（预算闸门见 WORKFLOW） |
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

`backend=monolith` 时，`compile_blueprint`、`bind_blueprint`、`create_widget_blueprint`、`edit_widget_blueprint` 对同一张蓝图最多修 3 次。第 3 次仍失败就停，错误含「已尝试 3 次，Monolith 不具备该能力」。对应的 `UE_ALLOW_*` 不是 true 时次数是 0。场景/资产/材质原语（find_actors、add_to_scene_from_asset、set_actor_transform、save_assets 等）由 `core/monolith_bridge.py` 翻译成对应 monolith action，映射外的原语带原因拒绝（覆盖矩阵见 `eval/check_backend_coverage.py`）。

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

工程根目录的 `start.bat` 就是这四条：1 普通对话，2 单循环，3 多窗口，4 续接。

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
