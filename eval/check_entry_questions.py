# eval/check_entry_questions.py
"""Q-1 / Q-2（20261116-1530 HTML 入口层计划 §8）：决策通道。

Q-1（必发、同源、可追溯）
  * `question` 必须在拿答案**之前**发出去，带 `question_id / kind / window / prompt / options[] / default`；
  * `options` 与 CLI 提示同源：按钮 value 只能是 `s/c/r`，label 必须逐字出现在 `_pause_hint` 的提示里；
  * `answer` 必须带 `source`（ui / stdin / eof / timeout）；
  * 缺口语的按钮只有一个词：`agent/brief.py::GAP_ACK_PHRASE`（两侧各要一个词就会把人问两遍）。

Q-2（收件箱）
  * 领取即改名：同一份收件箱两次领取，第二次一行都拿不到（`os.replace` 语义）；
  * 对不上的行丢弃并告警，不猜着分配；
  * **缺行不自动 skip**：等不到答案 → `timeout` → 调用方按停止处理。

全离线：不连编辑器、不调模型、不写 `data/`（收件箱在临时目录里造）。
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
    """替身事件出口：按 `_EventBus.emit(etype, **fields)` 的形状收。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, etype, **fields) -> None:
        row = {"type": etype}
        row.update(fields)
        self.events.append(row)

    def types(self) -> list[str]:
        return [row["type"] for row in self.events]

    def first(self, etype: str) -> dict:
        for row in self.events:
            if row["type"] == etype:
                return row
        raise AssertionError(f"没有 {etype} 事件（收到 {self.types()}）")


WINDOW = {"id": "W1", "action": "place_blueprint", "title": "放一个 BP_TestActor"}
ENTRY = {"id": "W1", "error": "缺 /Game 目录", "summary": ""}
FILTER_ENTRY = {"id": "W2", "error": "只给一次：r 过滤器全名", "summary": ""}


def _fresh_channel(recorder: _Recorder, *, inbox=None, stdin=None, wait: float = 0.3):
    from agent import ui_ask

    ui_ask.reset()
    channel = ui_ask.configure(inbox=inbox, stdin=stdin or input, printer=lambda *a, **k: None, wait=wait)
    channel.run_id = "20261116-170000"
    channel.emit = recorder
    return channel


def check_question_first_and_same_source() -> str:
    """Q-1：question 先发、options 与提示同源、answer 带 source。"""
    from agent import ui_ask
    from agent.run_windows import _decide_window

    recorder = _Recorder()
    asked: list[str] = []

    def _stdin(prompt=""):
        asked.append(str(prompt))
        return "c"

    _fresh_channel(recorder, stdin=_stdin)
    decision = _decide_window(WINDOW, ENTRY)
    check(decision == "skip", f"答 c 应判成 skip，实际 {decision!r}")

    types = recorder.types()
    check(types[:2] == ["question", "answer"], f"事件顺序必须是 question→answer，实际 {types}")

    question = recorder.first("question")
    check(question.get("question_id"), "question 必须带 question_id")
    check(question.get("kind") == "decide", f"kind 应为 decide，实际 {question.get('kind')!r}")
    check(question.get("window") == "W1", f"window 应为 W1，实际 {question.get('window')!r}")
    check("default" in question, "question 必须带 default")
    options = question.get("options") or []
    values = [row.get("value") for row in options]
    check(values == ["s", "c", "r"], f"按钮 value 只能是 s/c/r，实际 {values}")

    hint = ui_ask.decide_options.__doc__ is not None  # 只为下面同一份字面比对留痕
    from agent.run_windows import _pause_hint

    real_hint = _pause_hint(WINDOW, ENTRY)
    for row in options:
        label = str(row.get("label") or "")
        check(label, "按钮必须带 label")
        check(
            label in real_hint or label.split(" ")[0] in real_hint,
            f"按钮「{label}」的字面不在 CLI 提示里，两边已经漂移：{real_hint!r}",
        )
    check(
        str(question.get("prompt") or "").strip() == "裁决：" + real_hint + ":",
        f"question.prompt 必须就是终端那句提示：{question.get('prompt')!r}",
    )
    check(asked and real_hint in asked[0], f"终端提示也必须带同一份 hint，实际 {asked!r}")

    answer = recorder.first("answer")
    check(answer.get("question_id") == question["question_id"], "answer 必须回同一个 question_id")
    check(answer.get("option") == "c", f"answer.option 应为 c，实际 {answer.get('option')!r}")
    check(answer.get("source") == "stdin", f"answer.source 应为 stdin，实际 {answer.get('source')!r}")

    # 过滤器那种暂停：kind 必须分开，否则 UI 会把「r 路径」的按钮给到要过滤器全名的扇。
    recorder2 = _Recorder()
    _fresh_channel(recorder2, stdin=lambda prompt="": "s")
    _decide_window(FILTER_ENTRY, FILTER_ENTRY)
    check(
        recorder2.first("question").get("kind") == "filter",
        f"过滤器暂停的 kind 应为 filter，实际 {recorder2.first('question').get('kind')!r}",
    )

    # brief 门的按钮：缺口确认词只有 brief 侧那一个常量。
    from agent.brief import GAP_ACK_PHRASE
    from agent.run_windows import _gap_ack_phrase

    check(_gap_ack_phrase() == GAP_ACK_PHRASE, "缺口确认词两侧必须同源（_gap_ack_phrase == GAP_ACK_PHRASE）")
    gap_prompt = f"确认不做这些范围，只执行其余部分？输入 {GAP_ACK_PHRASE} 确认，其它任何输入都取消: "
    gap_options = ui_ask.brief_options(gap_prompt)
    check(
        [row["value"] for row in gap_options] == [GAP_ACK_PHRASE, "放弃"],
        f"缺口门按钮应为 [{GAP_ACK_PHRASE}, 放弃]，实际 {gap_options}",
    )
    review_options = ui_ask.brief_options(
        "任务书审阅（确认 / e 就地编辑，e help 看写法 / 写修改意见交生成器 / 放弃）: "
    )
    check(
        [row["label"] for row in review_options] == ["确认", GAP_ACK_PHRASE, "放弃"],
        f"任务书人审门按钮不对：{review_options}",
    )

    # 这两条事件不许进子 CLI 的可见事件表（它们给人看，不是 CLI 日志）。
    from agent.cli_log import _VISIBLE

    check("question" not in _VISIBLE and "answer" not in _VISIBLE,
          "question/answer 不许进 cli_log._VISIBLE")
    return "  question→answer 顺序 / kind=decide|filter / options 与 _pause_hint 同源 / answer.source"


def check_inbox(tmp: Path) -> str:
    """Q-2：领取原子性、对不上丢弃、缺行不自动 skip、给对了就是 ui 来源。"""
    from agent import ui_ask
    from agent.run_windows import _decide_window

    inbox = tmp / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    run_id = "20261116-170000"

    # 1) 领取即改名：同一份收件箱，第二次一行都拿不到。
    path = inbox / f"{run_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps({"id": "Q1", "answer": "c", "kind": "decide"}, ensure_ascii=False)
                  for _ in range(2)) + "\n",
        encoding="utf-8",
    )
    first = ui_ask.claim_rows(inbox, run_id)
    second = ui_ask.claim_rows(inbox, run_id)
    check(len(first) == 2, f"第一次领取应拿到 2 行，实际 {len(first)}")
    check(second == [], f"第二次领取必须一行都拿不到（os.replace 语义），实际 {second}")
    check(not path.exists(), "领取后原文件必须已经不在了")

    # 2) 收件箱给了对的答案 ⇒ source=ui，且 decision 按答案解析。
    recorder = _Recorder()
    channel = _fresh_channel(recorder, inbox=inbox, stdin=io.StringIO(""), wait=2.0)
    path.write_text(json.dumps({"id": "Q1", "answer": "c"}, ensure_ascii=False) + "\n", encoding="utf-8")
    decision = _decide_window(WINDOW, ENTRY)
    check(decision == "skip", f"收件箱答 c 应判成 skip，实际 {decision!r}")
    answer = recorder.first("answer")
    check(answer.get("source") == "ui", f"收件箱来的答案 source 应为 ui，实际 {answer.get('source')!r}")
    check(channel.run_id == run_id, "run_id 必须挂在通道上（收件箱按它命名）")

    # 3) 对不上的行：丢弃并告警，不猜着分配。
    recorder = _Recorder()
    _fresh_channel(recorder, inbox=inbox, stdin=io.StringIO(""), wait=0.6)
    path.write_text(json.dumps({"id": "Q999", "answer": "c"}, ensure_ascii=False) + "\n", encoding="utf-8")
    decision = _decide_window(WINDOW, ENTRY)
    dropped = recorder.first("inbox_dropped")
    check(dropped.get("dropped") == ["Q999"], f"应记下被丢弃的行，实际 {dropped}")
    check(decision == "stop", f"对不上就是等不到答案，应停止，实际 {decision!r}")

    # 4) 缺行不自动 skip：空收件箱 → timeout → 停止（不是 skip）。
    recorder = _Recorder()
    _fresh_channel(recorder, inbox=inbox, stdin=io.StringIO(""), wait=0.3)
    decision = _decide_window(WINDOW, ENTRY)
    check(decision == "stop", f"缺行必须停止，不许自动 skip，实际 {decision!r}")
    check(recorder.first("answer").get("source") == "timeout", "缺行时 answer.source 应为 timeout")
    check("skip" not in json.dumps(recorder.events, ensure_ascii=False), "缺行时不许出现 skip")

    # 5) 没有收件箱时行为与改动前一致：走 stdin。
    recorder = _Recorder()
    _fresh_channel(recorder, inbox=None, stdin=lambda prompt="": "r /Game/Blueprints")
    decision = _decide_window(WINDOW, ENTRY)
    check(decision == "/Game/Blueprints", f"stdin 补路径应原样返回，实际 {decision!r}")
    check(recorder.first("answer").get("source") == "stdin", "没配收件箱时 source 应为 stdin")
    return "  领取原子性 / 对不上丢弃告警 / 缺行 timeout 不 skip / 无收件箱仍走 stdin"


def main() -> int:
    try:
        rows = [check_question_first_and_same_source()]
        with tempfile.TemporaryDirectory() as tmp:
            rows.append(check_inbox(Path(tmp)))
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            from agent import ui_ask

            ui_ask.reset()
        except Exception:
            pass
    for row in rows:
        print(row)
    print("entry questions ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
