# eval/check_entry_brief_confirm.py
"""任务书人审门在 UI 上的「确认」语义：空答案 ≠ 超时。

20261116「输入指令卡死」修复的配套断言。

背景：任务书人审门的「确认」按钮 value 是空串（`ui_ask.brief_options` →
`{"value": "", "label": "确认"}`，`brief_flow` 把空输入当「通过」）。但 `Channel.ask`
旧写法是 `source = "ui" if answer else "timeout"`：空串被判成 timeout，调用方（
`run_windows._ask`）见到 timeout 就 `raise EOFError` → 任务书被当「放弃」。于是即使在
网页上点「确认」，任务也会被放弃——配合「run 记录要等任务书人审之后才落盘」，就成了
用户看到的「输入指令卡死」。

这里钉两点（全离线，不连编辑器、不调模型）：

1. 收件箱里有该 qid 的一行、答案为**空串** ⇒ `ask` 返回 `source="ui"`、`answer=""`。
2. 收件箱里根本没有这一行 ⇒ `source="timeout"`（缺行不自动通过，保持原口径）。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, etype, **fields) -> None:
        row = {"type": etype}
        row.update(fields)
        self.events.append(row)


def _channel(run_id: str, inbox: Path, wait: float):
    from agent import ui_ask

    ui_ask.reset()
    channel = ui_ask.configure(inbox=inbox, stdin=io.StringIO(""), printer=lambda *a, **k: None, wait=wait)
    channel.run_id = run_id
    channel.emit = _Recorder()
    return channel


def check_empty_answer_is_ui(inbox: Path) -> str:
    run_id = "20261116-200000"
    (inbox / f"{run_id}.jsonl").write_text(
        json.dumps({"id": "Q1", "run_id": run_id, "answer": ""}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    channel = _channel(run_id, inbox, wait=2.0)
    answer, source = channel.ask(kind="brief", prompt="任务书审阅（确认 / 放弃）: ")
    check(answer == "", f"确认应是空答案，实际 {answer!r}")
    check(source == "ui", f"空答案来自收件箱 ⇒ source 必须是 ui，实际 {source!r}（空串被误判成 timeout 就会放弃任务书）")
    return "收件箱空答案（确认）→ source=ui，不会被当成 timeout"


def check_missing_is_timeout(inbox: Path) -> str:
    channel = _channel("20261116-200001", inbox, wait=0.3)
    answer, source = channel.ask(kind="brief", prompt="任务书审阅（确认 / 放弃）: ")
    check(answer == "", f"没答案应是空串，实际 {answer!r}")
    check(source == "timeout", f"收件箱缺该行 ⇒ source 必须是 timeout，实际 {source!r}")
    return "收件箱缺行 → source=timeout（不自动通过）"


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="entry-brief-") as raw:
            inbox = Path(raw)
            rows = [check_empty_answer_is_ui(inbox), check_missing_is_timeout(inbox)]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            from agent import ui_ask

            ui_ask.reset()
        except Exception:
            pass
    for row in rows:
        print("  " + row)
    print("entry brief confirm ok（确认=空答案走 ui，不误判 timeout）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
