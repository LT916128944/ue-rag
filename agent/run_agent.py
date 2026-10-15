# agent/run_agent.py
import os
import sys
import uuid
import time
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from langgraph.checkpoint.sqlite import SqliteSaver
from agent.graph import builder


NODE_ICONS = {
    "retrieve": "🔍",
    "plan":     "🧠",
    "execute":  "⚙️ ",
    "build":    "🔨",
    "test":     "🧪",
    "observe":  "👀",
    "fix":      "🔧",
}


# ---------- 自动判定 require_build ----------
_BUILD_HINTS = (
    "编译", "构建", "build", "compile", "rebuild",
    "跑测试", "单元测试", "测试通过", "test", "unit test", "automation test",
)


def detect_require_build(query: str) -> bool:
    q = query.lower()
    return any(k in q for k in _BUILD_HINTS)


# ---------- 初始 state ----------
def build_initial_state(user_query: str, require_build: bool) -> dict:
    return {
        "messages": [{"role": "user", "content": user_query}],
        "user_query": user_query,
        "available_tools": [],
        "tool_calls": [],
        "tool_results": [],
        "retry_count": 0,
        "is_completed": False,
        "require_build": require_build,
        "build_result": "",
        "test_result": "",
        "fix_plan": "",
        "last_error": "",
        "final_success": False,
        "task": {},
        "max_retries": 3,
        "build_judge": {},
        "test_judge": {},
        "source_written": False,
        "tools_used": [],
        "failure_log": [],
        "llm_usage": {},
        "step_log": [],
    }


# ---------- 打印 ----------
def print_banner(thread_id: str, user_query: str, require_build: bool):
    print("╔" + "═" * 62 + "╗")
    print("║" + " 🚀 LangGraph W7 · Streaming 模式 ".center(52) + "║")
    print("╚" + "═" * 62 + "╝")
    print(f"  🆔 thread_id      : {thread_id}")
    print(f"  🔧 require_build  : {require_build}")
    print(f"  💬 user_query     : {user_query}")
    print("─" * 64)


def print_final_state(final_state: dict, elapsed: float):
    print("\n" + "═" * 64)
    print("📊 最终状态")
    print("═" * 64)
    ok = final_state.get("final_success", False)
    print(f"  完成状态 : {'✅ 成功' if ok else '❌ 失败 / 未完成'}")
    print(f"  重试次数 : {final_state.get('retry_count', 0)}")
    print(f"  总耗时   : {elapsed:.2f} s")
    if final_state.get("build_result"):
        print(f"  Build    : {final_state['build_result']}")
    if final_state.get("test_result"):
        print(f"  Test     : {final_state['test_result']}")
    last = final_state["messages"][-1]
    content = last.get("content", "") if isinstance(last, dict) else str(last)
    print(f"\n  最终消息:\n  {content}")
    print("═" * 64)


# ---------- 单次任务执行 ----------
def run_one(query: str, checkpointer) -> bool:
    require_build = detect_require_build(query)
    thread_id = datetime.now().strftime("dev_%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]

    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 100,
    }

    print_banner(thread_id, query, require_build)
    initial_state = build_initial_state(query, require_build)

    final_state = None
    step = 0
    t0 = time.time()

    graph = builder.compile(checkpointer=checkpointer)

    for mode, chunk in graph.stream(
        initial_state,
        config=config,
        stream_mode=["updates", "values"],
    ):
        if mode == "updates":
            if not chunk:
                continue
            for node_name, update in chunk.items():
                if update is None:
                    continue
                step += 1
                icon = NODE_ICONS.get(node_name, "• ")
                keys = [k for k in update.keys() if k != "messages"]
                suffix = ", ".join(keys) if keys else "messages"
                print(f"\n[{step:02d}] {icon} {node_name:<8} ← {suffix}")
        elif mode == "values":
            final_state = chunk

    elapsed = time.time() - t0
    if final_state is None:
        print("\n⚠️ 未捕获到最终状态")
        return False
    print_final_state(final_state, elapsed)
    return bool(final_state.get("final_success", False))


# ---------- 交互式主循环 ----------
HELP_TEXT = """
可用命令：
  :quit / :q      退出
  :help / :h      显示帮助
  :clear          清屏（仅 Windows PowerShell 生效）

直接输入自然语言即可，例如：
  - List all actors in the current level
  - Move the SkyLight_0 actor up by 100 units and verify

改 C++ 不在这里写文件。用 start.bat 菜单 3，确认后会编译；源码留下后启动编辑器。
"""


def main():
    print("╔" + "═" * 62 + "╗")
    print("║" + " UE-RAG Agent · 交互模式 ".center(50) + "║")
    print("╚" + "═" * 62 + "╝")
    print(" 输入 :help 查看帮助，:quit 退出\n")

    with SqliteSaver.from_conn_string("agent_checkpoints.db") as checkpointer:
        while True:
            try:
                query = input("💬 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n👋 退出")
                break

            if not query:
                continue

            if query in (":quit", ":q", "exit", "quit"):
                print("👋 退出")
                break

            if query in (":help", ":h"):
                print(HELP_TEXT)
                continue

            if query == ":clear":
                os.system("cls" if os.name == "nt" else "clear")
                continue

            try:
                run_one(query, checkpointer)
            except Exception as e:
                print(f"\n❌ 任务执行异常: {type(e).__name__}: {e}")
                continue


if __name__ == "__main__":
    main()