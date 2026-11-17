# agent/ui_ask.py
"""UI 决策通道（20261116-1530 HTML 入口层计划第 3 期）：`question` / `answer` 两条事件 + 收件箱。

四条口径，`eval/check_entry_questions.py` 钉着：

1. **事件发在 `input()` 之前。** 没有 `question` 事件，UI 不知道该在什么时候弹按钮；只有
   `confirm`（答完之后写的）不够用。`question` 带 `question_id / kind / window / prompt /
   options[] / default`。
2. **答案要能追溯到是谁写的。** `answer` 带 `question_id / option / path / source`，
   `source ∈ ui / stdin / eof / timeout`。
3. **收件箱按 `run_id` + `question_id` 双键。** 领取即改名（`os.replace`），两个进程只有一个
   能拿到那份文件——与 `run_guard.claim_stdin` 同一套手势。对不上的行**丢弃并告警**，
   不「猜着分配」（猜错就等于替人做了决定）。
4. **缺行不许当成「自动 skip」。** 等不到答案就按 `timeout` 收口，调用方照 EOF 同一条路
   处理（停止），绝不替人跳过。

边界：本模块不读 `data/runs`、不写事件文件（事件经调用方给的 `emit` 出口）、不碰编辑器。
`question` / `answer` 两条**不进** `agent/cli_log.py` 的 `_VISIBLE`——它们是给人看的，
不是子 CLI 的日志。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

QUESTION_PREFIX = "Q"
DEFAULT_WAIT_SECONDS = 600.0
POLL_SECONDS = 0.25
WAIT_ENV = "UE_UI_ANSWER_WAIT"
INBOX_ENV = "UE_RAG_UI_INBOX"

# 按钮文案与 CLI 逐字对齐（计划 §4.6）。value 是写进收件箱、再由 CLI 解析的那一行。
STOP_OPTION = {"value": "s", "label": "s 停止"}
SKIP_OPTION = {"value": "c", "label": "c 跳过"}
PATH_OPTION = {"value": "r", "label": "r <路径或重试>"}
ABORT_OPTION = {"value": "放弃", "label": "放弃"}

_ABORT_WORDS = ("放弃", "abort", "q", "quit", "停止")


def _data_dir() -> Path:
    from config import PROJECT_ROOT

    return PROJECT_ROOT / "data"


def default_inbox_dir() -> Path:
    """`data/ui/inbox`。与 `server/entry.py` 的写面同一个目录（计划 §6.2）。"""
    override = os.environ.get(INBOX_ENV, "").strip()
    return Path(override) if override else _data_dir() / "ui" / "inbox"


def wait_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get(WAIT_ENV, "") or DEFAULT_WAIT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_WAIT_SECONDS


def decide_options(hint: str) -> list[dict]:
    """从 CLI 的暂停提示生成按钮。**只认提示里真出现的动作**，不凭空多给。

    提示文本是 `run_windows._pause_hint` 的产物；这里按同一份字面解析，
    所以「按钮上写什么」与「终端里让人敲什么」不可能漂移（Q-1）。
    """
    text = str(hint or "")
    rows = []
    if "s 停止" in text:
        rows.append(dict(STOP_OPTION))
    if "c 跳过" in text:
        rows.append(dict(SKIP_OPTION))
    if "r " in text:
        rows.append(dict(PATH_OPTION))
    return rows


def brief_options(prompt: str) -> list[dict]:
    """任务书人审门 / 缺口确认的按钮。词以 `agent/brief.py` 的常量为准。"""
    from agent.brief import GAP_ACK_PHRASE

    text = str(prompt or "")
    phrase = str(GAP_ACK_PHRASE)
    if phrase in text:
        # 缺口确认（含 brief_flow 的「输入「确认缺口」才算表态」与 _escalate_gaps 那句）。
        return [{"value": phrase, "label": phrase}, dict(ABORT_OPTION)]
    if "任务书审阅" in text:
        return [
            {"value": "", "label": "确认"},
            {"value": phrase, "label": phrase},
            dict(ABORT_OPTION),
        ]
    return []


def split_answer(answer: str) -> tuple[str, str]:
    """一行答案 → (option, path)。option 是 CLI 认的那个记号，认不出就把原文当 option。"""
    text = str(answer or "").strip()
    low = text.lower()
    if low in ("c", "skip"):
        return "c", ""
    if low in ("", "s", "stop"):
        return "s", ""
    if low == "r":
        return "r", ""
    if low.startswith("r ") or low.startswith("r\t"):
        return "r", text.split(None, 1)[1].strip()
    return (text or "s"), ""


def claim_rows(folder: Path, run_id: str) -> list[dict]:
    """领取收件箱：`<run_id>.jsonl` → `<run_id>.jsonl.<pid>.claimed`，再逐行读。

    领取即改名：`os.replace` 之后源文件就不在了，第二个来领的进程拿到
    `FileNotFoundError`，一行都读不到。这正是「两进程只有一个成功」的实现，
    与 `run_guard.claim_stdin` 同一套手势。
    """
    if not run_id:
        return []
    path = Path(folder) / f"{run_id}.jsonl"
    if not path.is_file():
        return []
    claimed = path.with_name(f"{path.name}.{os.getpid()}.claimed")
    try:
        os.replace(path, claimed)
    except OSError:
        return []
    try:
        text = claimed.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and str(row.get("id") or ""):
            rows.append(row)
    return rows


def _is_tty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


class Channel:
    """一条 run 的提问通道。`ask()` 一次 = 一条 question + 一条 answer。"""

    def __init__(self, *, inbox=None, stdin=input, printer=print, emit=None,
                 run_id: str = "", wait: float | None = None) -> None:
        self.inbox = Path(inbox) if inbox else None
        self.stdin = stdin
        self.printer = printer
        self.emit = emit
        self.run_id = str(run_id or "")
        self.wait = wait
        self._seq = 0

    # ---------- 事件 ----------
    def emit_event(self, etype: str, **fields) -> None:
        if self.emit is None:
            return
        payload = {"run_id": self.run_id}
        payload.update({key: value for key, value in fields.items() if value is not None})
        try:
            self.emit(etype, **payload)
        except TypeError:
            # 调用方的 emit 只收一个 dict（面板回调的形状）
            try:
                self.emit({"type": etype, **payload})
            except Exception:
                pass
        except Exception:
            pass

    def next_question_id(self) -> str:
        self._seq += 1
        return f"{QUESTION_PREFIX}{self._seq}"

    # ---------- 提问 ----------
    def ask(self, *, kind: str, window: str = "", prompt: str = "", hint: str = "",
            options: list[dict] | None = None, default: str = "") -> tuple[str, str]:
        """返回 (answer, source)。source ∈ ui / stdin / eof / timeout。

        `answer` 是**原文**：解析仍由调用方（`_decide_window` / `brief_flow`）做，
        本模块不替它们判断 s/c/r 的含义。
        """
        rows = list(options) if options is not None else decide_options(hint)
        qid = self.next_question_id()
        self.emit_event(
            "question", question_id=qid, kind=kind, window=window,
            prompt=prompt, options=rows, default=default,
        )

        answer, source = "", "stdin"
        if self.inbox is not None and not _is_tty(self.stdin):
            answer, found = self._wait_inbox(qid, kind=kind, window=window)
            # 空答案 ≠ 超时：任务书人审的「确认」按钮写的就是空串（brief_flow 把空输入当通过）。
            # 旧写 `"ui" if answer else "timeout"` 会把「确认」误判成 timeout → 调用方按放弃处理。
            source = "ui" if found else "timeout"
            if not found:
                self.printer(
                    f"\n[UI] 收件箱里没有 {qid} 的答案（{self.inbox}），按停止处理，不替你跳过。"
                )
        else:
            try:
                answer = str(self.stdin(prompt) or "").strip()
            except (EOFError, KeyboardInterrupt):
                source = "eof"
                answer = ""

        option, path = split_answer(answer)
        self.emit_event(
            "answer", question_id=qid, option=option, path=path,
            source=source, window=window, kind=kind,
        )
        return answer, source

    # ---------- 收件箱 ----------
    def _wait_inbox(self, qid: str, *, kind: str, window: str) -> tuple[str, bool]:
        """等收件箱。返回 (answer, found)：found 表示「领取到了这个 qid 的那一行」，
        与 answer 是否为空无关——任务书「确认」就是空串，但仍算拿到了答案。"""
        deadline = time.monotonic() + (self.wait if self.wait is not None else wait_seconds())
        told = False
        while time.monotonic() <= deadline:
            rows = claim_rows(self.inbox, self.run_id)
            for row in rows:
                if str(row.get("id") or "") == qid:
                    return str(row.get("answer") or "").strip(), True
            if rows:
                # 双键对不上：丢弃并告警，不猜着分配（计划 §11）。
                dropped = [str(row.get("id") or "") for row in rows]
                self.printer(f"[UI] 收件箱里这些行对不上 {qid}，已丢弃：{'、'.join(dropped)}")
                self.emit_event("inbox_dropped", question_id=qid, dropped=dropped, kind=kind)
            if not told:
                told = True
                self.printer(f"\n[UI] 等待裁决 {qid}：把答案写进 {self.inbox}（{kind}）")
            time.sleep(POLL_SECONDS)
        return "", False


# ---------------------------------------------------------------------------
# 进程级通道：`_EventBus` 建好的地方 attach 一次，提问点直接 ask。
# ---------------------------------------------------------------------------
_ACTIVE: Channel | None = None


def configure(*, inbox=None, stdin=input, printer=print, wait: float | None = None) -> Channel:
    """装一条通道（进程内唯一）。`inbox` 为空表示只走 stdin，但仍发 question/answer 事件。"""
    global _ACTIVE
    _ACTIVE = Channel(inbox=inbox, stdin=stdin, printer=printer, wait=wait)
    return _ACTIVE


def attach(run_id: str, emit) -> Channel:
    """`_EventBus` 建好后调一次：接上 run_id 与事件出口，问题编号从 Q1 重新数。"""
    global _ACTIVE
    if _ACTIVE is None:
        inbox = os.environ.get(INBOX_ENV, "").strip()
        _ACTIVE = Channel(inbox=Path(inbox) if inbox else None, stdin=input, printer=print)
    if _ACTIVE.run_id != str(run_id or ""):
        _ACTIVE._seq = 0
    _ACTIVE.run_id = str(run_id or "")
    _ACTIVE.emit = emit
    return _ACTIVE


def active() -> Channel | None:
    return _ACTIVE


def reset() -> None:
    """给离线检查用：清掉进程级通道。"""
    global _ACTIVE
    _ACTIVE = None
