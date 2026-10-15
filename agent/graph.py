# agent/graph.py
from langgraph.graph import StateGraph, END

from agent.state import AgentState
from agent.nodes import (
    retrieve_node, plan_node, execute_node, observe_node,
    build_node, test_node, fix_node,
)


def _retry_limit(state: AgentState) -> int:
    try:
        return int(state.get("max_retries") or 3)
    except (TypeError, ValueError):
        return 3


def _judge_ok(judge) -> bool:
    return isinstance(judge, dict) and judge.get("ok") is True


def _judge_failed(judge) -> bool:
    return isinstance(judge, dict) and judge.get("ok") is False


# ---------- 条件边 1：plan 之后 ----------
def _read_stall(state: AgentState) -> bool:
    """编译已经失败，模型却只反复读源码。停住，把编译错误交回去。"""
    if not _judge_failed(state.get("build_judge")) or state.get("source_written"):
        return False
    tools = [name for name in (state.get("tools_used") or []) if name]
    return len(tools) >= 8 and all(name == "read_source" for name in tools[-8:])


def route_after_plan(state: AgentState):
    if state["is_completed"]:
        return "end"
    if state["retry_count"] >= _retry_limit(state):
        print("⚠️ 达到最大重试次数，强制结束")
        return "end"
    if _read_stall(state):
        print("⚠️ 编译失败后只在读源码，停止")
        return "end"
    if state["tool_calls"]:
        return "execute"
    if state.get("require_build") and not state.get("build_result"):
        return "build"
    return "observe"


# ---------- 条件边 2：execute 之后 ----------
def route_after_execute(state: AgentState):
    if state.get("is_completed"):
        return "end"
    return "plan"


# ---------- 条件边 3：build 之后（关键！） ----------
def route_after_build(state: AgentState):
    if _judge_ok(state.get("build_judge")):
        return "test"
    return "observe"        # 失败必须回 observe，不许直接到 fix


# ---------- 条件边 4：observe 之后 ----------
def route_after_observe(state: AgentState):
    if state["is_completed"]:
        return "end"
    if state["retry_count"] >= _retry_limit(state):
        print("⚠️ 达到最大重试次数，强制结束")
        return "end"
    if _judge_failed(state.get("build_judge")) or _judge_failed(state.get("test_judge")):
        return "fix"
    return "plan"


builder = StateGraph(AgentState)

builder.add_node("retrieve", retrieve_node)
builder.add_node("plan", plan_node)
builder.add_node("execute", execute_node)
builder.add_node("build", build_node)
builder.add_node("test", test_node)
builder.add_node("observe", observe_node)
builder.add_node("fix", fix_node)

builder.set_entry_point("retrieve")
builder.add_edge("retrieve", "plan")

builder.add_conditional_edges(
    "plan",
    route_after_plan,
    {"execute": "execute", "build": "build", "observe": "observe", "end": END},
)

builder.add_conditional_edges(
    "execute",
    route_after_execute,
    {"plan": "plan", "end": END},
)

builder.add_conditional_edges(
    "build",
    route_after_build,
    {"test": "test", "observe": "observe"},
)

builder.add_edge("test", "observe")

builder.add_conditional_edges(
    "observe",
    route_after_observe,
    {"plan": "plan", "fix": "fix", "end": END},
)

builder.add_edge("fix", "plan")