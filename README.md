# ue-rag

Unreal Engine 5.8 项目的检索库和执行代理。把资产、C++、蓝图图、文档和历史错误做成向量库，用自然语言查项目；要改编辑器时，通过本机 UE MCP 调工具。

Python 3.12。模型走 DeepSeek，向量库是 Chroma，嵌入模型是 `BAAI/bge-small-zh-v1.5`。

## 准备

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

入库脚本会把 Hugging Face 端点设为 `https://hf-mirror.com`。编辑器需要开着，MCP 默认在 `http://127.0.0.1:8000/mcp`。

`.env` 里至少填 `DEEPSEEK_API_KEY`，并按本机改这几项：

| 变量 | 作用 |
| --- | --- |
| `UE_LLM_MODEL` | 默认 `deepseek-chat` |
| `UE_MCP_URL` | UE MCP 地址 |
| `UE_EDITOR_CMD` | `UnrealEditor-Cmd.exe` |
| `UE_PROJECT` | `.uproject` |
| `UE_SOURCE_ROOT` | 游戏模块源码目录 |
| `UE_EXEC_MODE` | `real` 走编辑器；`mock` 才启用注入错误 |
| `UE_ALLOW_BLUEPRINT` | 缺省 false：不改蓝图，只写 Plan |
| `UE_ALLOW_MATERIAL` / `UE_ALLOW_UMG` / `UE_ALLOW_ANIM` / `UE_ALLOW_NIAGARA` | 同样缺省 false |

未显式要求时不保存关卡，也不 StartPIE。

## 入库

资产导出要在编辑器 Python 里跑，其余可以在本目录跑：

```powershell
python pipelines/export_cpp_metadata.py
python pipelines/export_blueprint_graph.py
python pipelines/export_docs.py
python pipelines/ingest_assets.py
```

`ingest_assets.py` 读取 `data/` 下已有的 JSONL，写入 `data/chroma/`，collection 名是 `ue_assets`。

## 使用

只检索、不改工程：

```powershell
python agent/chat.py
```

按任务文件跑闭环，退出码 0 表示裁判通过：

```powershell
python agent/run_task.py --task eval/tasks/bp-gun-compile.json
```

用一句话跑：

```powershell
python agent/run_task.py --query "把方块往前移 100"
```

交互式闭环：

```powershell
python agent/run_agent.py
```

多窗口：

```powershell
python agent/run_windows.py
```

项目语料 MCP（stdio）提供 `search_ue_project` 和 `get_project_summary`：

```powershell
python server/server.py
```

编辑器工具先扫描再调用，不要一次拉全量 schema：

```powershell
python scripts/scan_mcp_tools.py --query "移动 Actor" --top-k 8
python scripts/scan_mcp_tools.py --schema "<full_name>"
python scripts/call_mcp_tool.py --toolset "<toolset>" --tool "<name>" --args "{}"
```

DSH 外壳：见 `dsh/README.md`（`shortcuts/dsh.bat` 预检，工具只经 9 个 `mcp__uerag__*`）。

核对评测清单（不打开编辑器、不调模型）：

```powershell
python eval/run_bench.py
```

`--profile pipeline` 跑蓝图编译，`--profile slow` 再加整工程编译，`--profile agent` 跑会调模型或改未保存关卡的任务。改关卡的任务不计入成功率。

## 目录

| 路径 | 内容 |
| --- | --- |
| `agent/` | 对话、任务环、多窗口 |
| `core/` | 检索、MCP 客户端、裁判 |
| `pipelines/` | 导出元数据并写入向量库 |
| `scripts/` | 扫描和调用单个 MCP 工具 |
| `tools/llm-gateway/` | 本地模型名映射网关（Node，零依赖），见该目录 `README.md` |
| `server/` | 项目语料 MCP |
| `eval/` | 任务和可恢复评测 |
| `docs/` | 选型记录 |
| `data/` | JSONL、向量库、运行痕迹 |

`.env`、`data/chroma/`、轨迹、运行记录和 `data/ue_mcp_index.json` 不入库。
