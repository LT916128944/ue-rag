# agent/run_multi.py
"""单循环：一句话拆成步骤，每一步跑同一张图。失败或做不了时停下来等裁决。"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.sequence import run_sequence


def _decide(step: dict, summary: dict | None) -> str:
    needs = step.get("needs")
    if summary is None and needs == "source":
        print("这一步缺源文件，已暂停。只给一次补路径。")
        prompt = "裁决：s 停止 / c 跳过 / r Public/类名.h 或 r Private/类名.cpp: "
    elif summary is None and needs in ("path", "blueprint"):
        print("这一步缺目录，已暂停。只给一次补路径。")
        prompt = "裁决：s 停止 / c 跳过 / r /Game/目录 或 r /Game/目录/BP_名字: "
    elif summary is None:
        print("这一步解决不了，已暂停。")
        prompt = "裁决：s 停止 / c 跳过这一步: "
    else:
        print("这一步失败，已暂停。")
        prompt = "裁决：s 停止 / c 跳过这一步 / r 重试这一步: "
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n停止")
        return "stop"
    low = answer.lower()
    if low == "c":
        return "skip"
    if low in ("", "s"):
        return "stop"
    if summary is None and needs in ("path", "blueprint", "source"):
        if low.startswith("r ") or low.startswith("r\t"):
            return answer.split(None, 1)[1].strip()
        if needs != "source" and answer.startswith("/Game"):
            return answer
        if needs == "source" and (".h" in answer or ".cpp" in answer):
            return answer
        print("路径格式不对，这次不记成补路径。")
        return "stop"
    if low == "r":
        return "retry"
    return "stop"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print("单循环。一句话拆成步骤，每一步跑同一张图。")
    print("失败或做不了时暂停。记录在 data/runs。输入 :quit 退出。\n")
    while True:
        try:
            query = input("单循环 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            return
        if not query:
            continue
        if query in (":quit", ":q", "exit", "quit"):
            print("退出")
            return
        code, path = run_sequence(query, _decide, choices=True, layered=True)
        print(f"\n序列结束，退出码 {code}。记录：{path}\n")


if __name__ == "__main__":
    main()
