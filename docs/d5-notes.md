
# D5：retrieve() 接入 MCP

## 做了什么

- 抽 rag_core.py（纯检索函数，绝对路径 + 懒加载）
- 新建 scripts/server.py，用 MCPServer（mcp 2.x）暴露 2 个 Tool
- 在 Codex config.toml / MCP 设置里挂载 ue-rag（命名空间 mcp_ue_rag）
- 改 AGENTS.md：路由表把 rag 提到最高优先级，铁律 9 硬门禁 RAG 先行

## 关键决策

- MCP SDK 版本：mcp 2.x，用 MCPServer（FastMCP 已重命名）
- 传输：stdio（Codex 自动拉起子进程）
- 路径：绝对路径（因为工作目录不确定）
- 不手动启动 server.py，Codex 全托管
- 和 unreal-mcp 并列挂载，不互斥；通过 docstring + AGENTS.md 区分职责

## 踩坑

- mcp 2.x 里 FastMCP → MCPServer，导入路径也变了
- MCP Inspector 用 uv 拉临时环境，重依赖装不上 → 放弃 Inspector，直接用 Codex 验证
- Windows 路径反斜杠在 Inspector 里被转义 → 用正斜杠
- Codex 默认先扫文件再调 RAG → AGENTS.md 铁律 9 硬门禁 + 换更纯的问题验证

## 验收

- 「项目里有哪些 C++ 类？用 RAG 查」→ 第一动作 Search ue project，13 秒返回 6 个类，全程不碰文件系统
- Codex 知道 RAG 边界：缺 parent_class 时主动提议走 unreal-mcp
- 失败案例：「跟枪有关的东西」→ Codex 先侦察再调 RAG，因为问题跨语料类型 + 需要蓝图内部结构

## 遗留

- 「跟枪有关的东西」这类跨类型模糊问题，Codex 仍倾向先侦察。根因是它不信任 RAG 覆盖面，属于 Agent 行为层，不是 RAG 层
- 下一步：D6 写 20 个 smoke query 覆盖不同语料类型和查询意图
