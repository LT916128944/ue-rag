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
    from agent import ui_ask

    channel = ui_ask.active()
    try:
        if channel is None:
            answer = input(prompt).strip()
        else:
            # 与菜单 3 同一条通道：question 先发，答案可能来自收件箱（UI）或 stdin。
            answer, source = channel.ask(
                kind="decide", window="", prompt=prompt,
                hint=prompt, default="s",
            )
            if source in ("eof", "timeout"):
                print("\n停止")
                return "stop"
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


def _decide_model_first(step: dict, summary: dict | None, query: str = "") -> str:
    """`_decide` 的「模型优先」包装：缺路径/蓝图/源文件时先让模型补一次。

    补得出就当成人敲了 `r <值>` 直接返回；补不出（或离线检查把 `_complete_live` 换成抛异常
    的替身）→ 原样退回 `_decide` 问人。人工裁决这条路不变，只是把「能问模型解决」的先问模型。
    """
    try:
        needs = str(step.get("needs") or "")
        if summary is None and needs in ("path", "blueprint", "source"):
            from agent import model_fill

            kind = "source" if needs == "source" else "path"
            value = model_fill.fill_missing(
                query, kind, error=str(step.get("error") or ""),
                action=str(step.get("action") or ""), slots=step.get("slots"),
            )
            if value:
                print(f"\n[model-fill] 缺 {kind}，模型补：{value}（先问模型，不再停下问人）")
                return value
    except Exception:
        pass
    return _decide(step, summary)


def main(argv=None) -> None:
    import argparse

    from agent.run_guard import admit_live_runs

    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="单循环")
    parser.add_argument("--force", action="store_true", help="有活心跳时仍启动，并写下 forced_by")
    parser.add_argument(
        "--ui-inbox", default="",
        help="UI 收件箱目录（data/ui/inbox）。缺行不自动 skip：等不到就按停止处理",
    )
    args = parser.parse_args(argv)
    from agent import ui_ask

    # 与菜单 3 同一个通道。菜单 2 的 run 没有机读 json，UI 侧看不到卡片，
    # 但答案通道一致：给 `--ui-inbox` 就能从收件箱答（计划 §7 的 3-2）。
    ui_ask.configure(inbox=(args.ui_inbox or None), stdin=input, printer=print)
    code = admit_live_runs(force=args.force)
    if code:
        raise SystemExit(code)
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
        code, path = run_sequence(
            query, lambda step, summary: _decide_model_first(step, summary, query),
            choices=True, layered=True,
        )
        print(f"\n序列结束，退出码 {code}。记录：{path}\n")


if __name__ == "__main__":
    main()
