# agent/chat.py
"""普通对话：检索后回答。不改工程，不编译，不进入修复环。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv

# 20261028：不裸调 load_dotenv()——统一从 config.ENV_PATH 读（见 agent/nodes.py 同注）。
from config import ENV_PATH as _UE_RAG_ENV_PATH
load_dotenv(str(_UE_RAG_ENV_PATH), override=True)

from core.llm_call import complete
from core.rag_core import retrieve

SYSTEM = """你是 Unreal 项目的检索助手。只根据给出的资料回答。
资料不够就说资料里没有。不要声称自己已经改了文件、编译或移动了 Actor。
用户如果要改工程，告诉他退出后改用 Loop 对话或多 Agent 对话。"""


def _context(query: str) -> str:
    try:
        hits = retrieve(query, top_k=5)
    except Exception as exc:
        return f"(检索失败: {type(exc).__name__})"
    if not hits:
        return "(检索无命中)"
    lines = []
    for hit in hits:
        meta = hit.get("metadata") or {}
        where = meta.get("source_path") or meta.get("file_path") or hit.get("id") or ""
        lines.append(f"[{meta.get('source_type') or ''}] {where}\n{hit.get('content') or ''}")
    return "\n\n".join(lines)


def answer(query: str) -> str:
    context = _context(query)
    response, _record = complete(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"资料：\n{context}\n\n问题：{query}"},
        ],
        caller="chat",
    )
    return response.choices[0].message.content or ""


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print("普通对话。只检索、只回答。不改工程，不编译。")
    print("输入 :quit 退出。要改工程请用 Loop 或多 Agent。\n")
    while True:
        try:
            query = input("问 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            return
        if not query:
            continue
        if query in (":quit", ":q", "exit", "quit"):
            print("退出")
            return
        if not (os.getenv("DEEPSEEK_API_KEY") or "").strip():
            print("没有 DEEPSEEK_API_KEY。")
            continue
        try:
            print(answer(query))
            print()
        except Exception as exc:
            print(f"失败: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
