# 架构

这一页是 WORKFLOW「框架」那张图，并标出六个高层工具和引擎 AgentSkill 在哪一侧。2026-10-16。

## 框架

```text
start.bat ─┬─ 普通对话 chat.py ─ retrieve ─ 回答
           ├─ 单循环 run_multi.py ─ plan_sequence ─ 每步同一张图
           └─ 多窗口 run_windows.py ─ 规划者/任务书/快路径 ─ 每扇 run_task（只读并行过编辑器闸门）
                                          board.py 面板 · brief.py 任务书+人审门 · ledger.py 台账
                                                            │
                    ┌─ Cursor 对话 ─ scan_mcp_tools.py ─ call_mcp_tool.py ─┐
                    │                                                      │
任务 JSON 或一扇 ───┤                                                      ├─ editor_exec.py ─ UE MCP :8000
                    │                                                      │
                    └─ run_task.py ─ LangGraph ────────────────────────────┘
                              │
                              ├─ llm_call.py    每次模型调用的用量。gateway 也走这里
                              ├─ plan_sequence  手写规则，然后 learned_routes.json
                              ├─ plan_windows   L0 规则强命中/已学句式 → L1 理解器 → L2 归地 → 老规划者兜底
                              ├─ intent/understand/grounding  意图验收、一次模型理解、asset 槽位归地
                              ├─ retrieve  路由，未命中再 BM25 + bge。不进规划者
                              ├─ plan      按 verify 选 C++ 或编辑器角色
                              ├─ execute   本地工具，或 editor_exec
                              ├─ build     blueprint_compile / cpp_build（裁判，无 LLM）
                              ├─ test      editor_assert / automation，没有则 skip
                              ├─ observe   只看裁判 ok。freeze_source 时失败即结束
                              └─ fix ─→ plan     只编译工程的步骤不进这里
```

Python 这一侧是上面整张图，加上网关和契约 server。编辑器那一侧是 `UE MCP :8000` 右边的进程。

## 六工具在哪一侧

六个名字：`query_actor`、`query_blueprint`、`find_asset`、`build_project`、`run_automation_test`、`get_build_error`。

```text
Python 这一侧                                      编辑器这一侧
contract/tools.json                                官方 MCP :8000
  每个工具有 input / output / error schema、         Cursor 和 LangGraph 经 editor_exec 走这里
  permission、timeout
core/contract_tools.py                             query_actor 另有一个 Python Toolset
  包住现有查找、cpp_build、Automation                  Content/Python/aiagent_query_actor_toolset.py
server/contract_server.py                            只注册 query_actor
  tools/list 只有这六个                              类名 aiagent_query_actor_toolset.AIAgentQueryActorToolset
server/gateway.py  :8010                             schema 在 contract/query_actor.toolset_schema.json
  /api/tool-call 只转发这六个
  不跑 LangGraph
```

规划者的动作目录仍是 `agent/catalog.py` 里原来的 8 个动作。`scripts/scan_mcp_tools.py` 仍只回卡片。这六个契约工具给外部客户端和网关，不塞进那 8 个动作。

## AgentSkill 在哪一侧

```text
Python 这一侧                                      编辑器这一侧
data/skill_cards.json                              /Game/Skills/AIAgentProjectRules
  19 条卡片：triggers、tools、version、conflicts       资产 AIAgentProjectRules
  没有技能正文                                        instructions 四条：
data/agent_skill.json                                1 米 = 100 uu
  是编辑器 GeneratePrompt 的结果                       编的是 AIAgentProjectEditor
  选出域才附到 plan，最长 1200 字                       路径不唯一就停
  没选出域不附                                         裁判不由模型宣布
```

技能卡片参与路由，在 Python 这一侧。规矩正文在编辑器资产里。`data/agent_skill.json` 只是把 `GeneratePrompt` 抄回来，给选中的那一域用。
