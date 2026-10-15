# agent/state.py
from typing import TypedDict, Annotated
from operator import add


class AgentState(TypedDict):
    messages: Annotated[list, add]   # 对话历史（全部为可序列化 dict）
    user_query: str                  # 用户原始需求
    available_tools: list            # RAG 检索出的工具 Schema
    tool_calls: list                 # LLM 打算调用的工具（dict 列表）
    tool_results: list               # 工具执行结果（str 列表）
    retry_count: int                 # 重试次数（W7 容错核心）
    is_completed: bool               # 任务是否完成
    require_build: bool              # 是否需要编译验证
    build_result: str                # 编译输出（"" 表示未编译）
    test_result: str                 # 测试输出（"" 表示未测试）
    fix_plan: str                    # 上一次 fix 节点生成的修复方案
    last_error: str                  # 上一次执行失败的错误信息（"" 表示无错误）
    final_success: bool              # 最终是否成功（仅用于展示，不参与路由）
    task: dict                       # 任务 JSON。空 dict 表示手工对话
    max_retries: int                 # 失败后最多再试几次
    build_judge: dict                # 裁判结构体，路由只看 ok
    test_judge: dict
    source_written: bool             # 本轮是否写过 Source
    tools_used: list                 # 真正调用过的工具短名
    tool_outcomes: list              # 每次调用的 {tool, ok, error}，给打转熔断和排障用
    failure_log: list                # 失败裁判的 errors，成功重试后写入错误记忆
    llm_usage: dict                  # 最近一次 DeepSeek usage，没有则为空
    step_log: list                   # 本节点的工具调用，给 trace 用
