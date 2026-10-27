# agent/board.py
"""终端面板。主代理画路线图和每扇进度；正在跑的窗口各开一块 CLI，
结束后这块 CLI 收起。全部结束后主画面给出结果。
E 切换简略 / 详细：简略只留当前步骤，详细滚动脚本、思考和 print。
print 在面板开着时进对应窗口，不冲掉 Live。日志由运行层落盘，面板只负责画。"""
import threading
import time
from collections import deque
from datetime import datetime

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent.cli_log import cli_record, current_window, install_tap, keys_paused

try:
    import msvcrt
except ImportError:  # 非 Windows：面板照常渲染，只是没有 E 键
    msvcrt = None

_STATUS_ICON = {
    "queued": "·", "running": "▶", "retry": "↻", "revising": "✎",
    "done": "✔", "failed": "✘", "paused": "⏸", "skipped": "⏭",
    "waiting_external": "⏳", "interrupted": "✖",
}
_PANE_STATES = frozenset({"running", "retry", "revising"})
_FINISHED_STATES = frozenset({
    "done", "failed", "paused", "skipped", "waiting_external", "interrupted",
})


def _stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


class _Screen:
    """Live 每帧都重新问面板要画面，计时和转圈才会走。"""

    def __init__(self, board: "Board") -> None:
        self._board = board

    def __rich__(self):
        return self._board.render()


class Board:
    """一次 run 一个实例。event() 吃事件流，echo() 吃调度说明，feed_line() 吃 print。线程安全。"""

    def __init__(self, console: Console | None = None, on_log=None) -> None:
        self._lock = threading.RLock()
        self._console = console or Console()
        self._on_log = on_log
        self._live: Live | None = None
        self._key_thread: threading.Thread | None = None
        self._key_stop = threading.Event()
        self._tap_undo = None
        self._notes: deque[str] = deque(maxlen=4)
        self.reset("")

    # ---------- 状态 ----------

    def reset(self, query: str) -> None:
        with self._lock:
            self.query = str(query or "")
            self.phase = "待命"
            self.review = ""
            self.files_modified = False
            self.windows: dict[str, dict] = {}
            self.order: list[str] = []
            self.integration = ""
            self.resumable: list[str] = []
            self.expanded = False
            self.distill_count = 0
            self.run_id = ""
            self._notes.clear()

    def running(self) -> bool:
        return self._live is not None

    def set_phase(self, text: str) -> None:
        with self._lock:
            self.phase = str(text or "") or self.phase
        self._refresh()

    def set_review(self, text: str) -> None:
        with self._lock:
            self.review = str(text or "")
        self._refresh()

    def set_resumable(self, run_ids: list[str]) -> None:
        with self._lock:
            self.resumable = [str(item) for item in run_ids or []]
        self._refresh()

    def echo(self, text: str) -> None:
        """调度说明。面板开着时不往真 stdout 打，避免冲掉 Live。"""
        line = " ".join(str(text).split())
        if not line:
            return
        with self._lock:
            self._notes.append(line[:240])
            run_id = self.run_id
        self._refresh()
        self._write_log({
            "ts": _stamp(), "run_id": run_id, "window": "", "kind": "echo", "text": line[:500],
        })

    def feed_line(self, text: str) -> None:
        """工作线程的 print。有窗口绑定时进那扇 CLI，否则进主代理最近输出。"""
        line = " ".join(str(text).split())
        if not line:
            return
        wid = current_window()
        with self._lock:
            run_id = self.run_id
            if wid and wid in self.windows:
                self._push(wid, line[:300])
            else:
                self._notes.append(line[:240])
                wid = ""
        self._write_log({
            "ts": _stamp(), "run_id": run_id, "window": wid, "kind": "stdout", "text": line[:500],
        })

    def event(self, event: dict) -> None:
        with self._lock:
            self._apply(event)
        self._refresh()

    def _write_log(self, record: dict) -> None:
        if self._on_log is None or not record.get("run_id") or not record.get("text"):
            return
        try:
            self._on_log(record)
        except Exception:
            pass

    def _apply(self, event: dict) -> None:
        if event.get("run_id"):
            self.run_id = str(event["run_id"])
        etype = str(event.get("type") or "")
        if etype == "run_start":
            self.query = str(event.get("query") or self.query)
            self.phase = "执行"
            for window in event.get("windows") or []:
                if not isinstance(window, dict):
                    continue
                self._window(
                    window.get("id"),
                    title=window.get("title"),
                    action=window.get("action"),
                    state=str(window.get("state") or "queued"),
                    depends_on=[str(dep) for dep in (window.get("depends_on") or [])],
                )
        elif etype == "window_state":
            window = self._window(event.get("window"))
            state = str(event.get("state") or "")
            if state:
                window["state"] = state
            if state in _PANE_STATES and not window.get("t0"):
                window["t0"] = time.monotonic()
            if event.get("attempt"):
                window["attempt"] = event.get("attempt")
        elif etype == "confirm":
            wid = str(event.get("window") or "")
            if wid:
                label = str(event.get("option") or "")
                if event.get("path"):
                    label = f"{label} {event.get('path')}"
                self._window(wid)["note"] = "确认 " + label
        elif etype in ("tool_call", "judge"):
            window = self._window(event.get("window"))
            if event.get("node"):
                window["node"] = str(event["node"])
            calls = event.get("calls") if isinstance(event.get("calls"), list) else []
            tool = str(event.get("tool") or "")
            if calls:
                window["tool"] = str(calls[0])
            elif tool == "llm" or event.get("reasoning"):
                window["tool"] = "思考"
            elif tool:
                window["tool"] = tool
            if etype == "judge":
                window["note"] = str(event.get("note") or event.get("summary") or window.get("note") or "")
        elif etype == "files_modified":
            self.files_modified = True
        elif etype == "handoff":
            window = self._window(event.get("window"))
            unmet = [str(item) for item in (event.get("unmet") or []) if str(item).strip()]
            if unmet:
                total = event.get("unmet_total")
                prefix = "未完成：" if total is None else f"未完成 {len(unmet)}/{int(total)}："
                window["note"] = (prefix + "；".join(unmet))[:120]
        elif etype == "context_slice":
            return
        elif etype == "window_result":
            window = self._window(event.get("window"))
            window["state"] = "done" if event.get("ok") else str(event.get("state") or "failed")
            window["ok"] = bool(event.get("ok"))
            window["summary"] = str(event.get("summary") or "")[:200]
            window["elapsed"] = event.get("elapsed")
            window["retries"] = event.get("retries")
        elif etype == "integration":
            self.integration = str(event.get("summary") or "")
            self.phase = "集成：" + str(event.get("status") or "")
        elif etype == "run_end":
            self.phase = "结束：" + str(event.get("status") or "")
        elif etype == "distill_candidate":
            self.distill_count = int(getattr(self, "distill_count", 0) or 0) + 1
        rec = cli_record(event)
        if not rec:
            return
        wid = str(rec.get("window") or "")
        if wid:
            for part in str(rec.get("text") or "").split(" | "):
                self._push(wid, part.strip())
        elif rec.get("text"):
            self._notes.append(str(rec["text"])[:240])

    def _window(self, wid, **fields) -> dict:
        key = str(wid or "?")
        if key not in self.windows:
            self.windows[key] = {
                "title": "", "action": "", "state": "queued", "node": "", "tool": "",
                "note": "", "ok": None, "elapsed": None, "retries": None, "summary": "",
                "depends_on": [], "log": deque(maxlen=40), "t0": None,
            }
            self.order.append(key)
        clean = {key: value for key, value in fields.items() if value is not None and value != ""}
        self.windows[key].update(clean)
        return self.windows[key]

    def _push(self, wid: str, text: str) -> None:
        if not text:
            return
        window = self._window(wid)
        log = window.get("log")
        if not isinstance(log, deque):
            log = deque(maxlen=40)
            window["log"] = log
        log.append(str(text)[:300])

    def _show_result(self) -> bool:
        if str(self.phase).startswith("结束"):
            return True
        if not self.order:
            return False
        return all(self.windows[wid].get("state") in _FINISHED_STATES for wid in self.order)

    def _cli_hint(self) -> str:
        rid = str(self.run_id or "")
        if not rid:
            return ""
        day = rid.split("-", 1)[0]
        if len(day) == 8 and day.isdigit():
            return f"data/runs/{day}/{rid}.cli.jsonl"
        return f"data/runs/{rid}.cli.jsonl"

    # ---------- 生命周期 ----------

    def start(self) -> None:
        with self._lock:
            if self._live is not None:
                return
        live = Live(
            _Screen(self),
            console=self._console,
            refresh_per_second=4,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        live.start()
        with self._lock:
            self._live = live
        self._tap_undo = install_tap(self.feed_line)
        if msvcrt is not None:
            self._key_stop.clear()
            self._key_thread = threading.Thread(target=self._key_loop, name="board-keys", daemon=True)
            self._key_thread.start()

    def stop(self) -> None:
        undo, self._tap_undo = self._tap_undo, None
        if undo is not None:
            try:
                undo()
            except Exception:
                pass
        with self._lock:
            live, self._live = self._live, None
        self._key_stop.set()
        if self._key_thread is not None:
            self._key_thread.join(timeout=0.5)
            self._key_thread = None
        if live is not None:
            try:
                live.refresh()
                live.stop()
            except Exception:
                pass

    def _key_loop(self) -> None:
        while not self._key_stop.is_set():
            try:
                if keys_paused() or not msvcrt.kbhit():
                    time.sleep(0.05)
                    continue
                ch = msvcrt.getwch()
                if ch in ("e", "E"):
                    with self._lock:
                        self.expanded = not self.expanded
                    self._refresh()
            except Exception:
                pass
            time.sleep(0.05)

    def _refresh(self) -> None:
        live = self._live
        if live is None:
            return
        try:
            live.refresh()
        except Exception:
            pass

    # ---------- 渲染 ----------

    def render(self):
        with self._lock:
            layout = Layout()
            road = self._road_lines()
            note_rows = min(2, len(self._notes))
            head_size = min(18, 5 + len(road) + note_rows)
            layout.split_column(Layout(name="head", size=head_size), Layout(name="body"))
            layout["head"].update(Panel(
                self._head(road),
                title="主代理",
                subtitle="路线图",
                border_style="blue",
            ))
            layout["body"].update(self._body())
            return layout

    def _head(self, road: list[Text]):
        head = Table.grid(padding=(0, 1))
        head.add_row(Text(f"指令：{self.query[:76]}", style="bold"))
        line = Text()
        line.append(f"阶段：{self.phase}")
        if getattr(self, "distill_count", 0):
            line.append(f"    蒸馏候选 {self.distill_count}", style="dim")
        if self.review:
            line.append(f"    审阅：{self.review}")
        running = sum(1 for wid in self.order if self.windows[wid].get("state") in _PANE_STATES)
        finished = sum(1 for wid in self.order if self.windows[wid].get("state") in _FINISHED_STATES)
        if self.order:
            line.append(f"    进度 {finished}/{len(self.order)} 终态")
            if running:
                line.append(f" · {running} 运行中")
        if self.resumable:
            line.append(f"    可续接 run：{'、'.join(self.resumable[:3])}", style="yellow")
        head.add_row(line)
        if self.files_modified:
            head.add_row(Text("● 修改了文件", style="green"))
        else:
            head.add_row(Text("○ 未修改文件", style="dim"))
        mode = "E 简略" if self.expanded else "E 详细"
        hint = Text(mode + "    运行中的窗口是独立 CLI，完成后自动收起", style="dim")
        head.add_row(hint)
        for item in road[:6]:
            head.add_row(item)
        if len(road) > 6:
            head.add_row(Text(f"… 还有 {len(road) - 6} 扇", style="dim"))
        for note in list(self._notes)[-2:]:
            head.add_row(Text("最近：" + note[:72], style="dim"))
        return head

    def _road_lines(self) -> list[Text]:
        lines = []
        for wid in self.order:
            window = self.windows[wid]
            state = str(window.get("state") or "queued")
            icon = _STATUS_ICON.get(state, "·")
            deps = [str(dep) for dep in (window.get("depends_on") or []) if str(dep)]
            dep = (" ← " + ",".join(deps)) if deps else ""
            step = ""
            if state in _PANE_STATES:
                step = "  " + str(window.get("tool") or window.get("node") or "启动")
                clock = self._clock(window)
                if clock:
                    step += f" {clock}"
            style = "cyan" if state in _PANE_STATES else ("green" if window.get("ok") is True else "dim")
            if window.get("ok") is False:
                style = "red"
            title = str(window.get("title") or "")[:28]
            lines.append(Text(f"{icon} {wid} {title}{dep}{step}".rstrip(), style=style))
        return lines

    def _clock(self, window: dict) -> str:
        t0 = window.get("t0")
        if not t0:
            return ""
        return f"{int(time.monotonic() - t0)}s"

    def _body(self):
        if self._show_result():
            return self._result_panel()
        panes = [wid for wid in self.order if self.windows[wid].get("state") in _PANE_STATES]
        if not panes:
            return self._wait_panel()
        shown = panes[:3]
        hidden = panes[len(shown):]
        row = Layout()
        row.split_row(*[Layout(name=wid) for wid in shown])
        for wid in shown:
            row[wid].update(self._pane(wid))
        if not hidden:
            return row
        note = Text("另有运行中：" + " ".join(hidden) + "（见路线图）", style="dim")
        column = Layout()
        column.split_column(Layout(row, ratio=1), Layout(Panel(note, border_style="dim"), size=3))
        return column

    def _pane(self, wid: str):
        window = self.windows[wid]
        state = str(window.get("state") or "running")
        icon = _STATUS_ICON.get(state, "▶")
        clock = self._clock(window)
        step = str(window.get("tool") or window.get("node") or "启动")
        body = Text()
        body.append(f"{icon} {clock} {step}".strip() + "\n", style="cyan")
        if window.get("note"):
            body.append(str(window["note"])[:80] + "\n", style="yellow")
        logs = list(window.get("log") or [])
        shown = logs[-8:] if self.expanded else []
        if self.expanded and not shown:
            body.append("等待输出\n", style="dim")
        for line in shown:
            body.append(line + "\n", style="dim")
        if not self.expanded:
            body.append("简略。按 E 看脚本和思考。\n", style="dim")
        title = f"{wid} {window.get('title') or ''}"[:40]
        return Panel(body, title=title, subtitle="子代理", border_style="cyan")

    def _wait_panel(self):
        body = Text()
        waiting = [
            wid for wid in self.order
            if self.windows[wid].get("state") not in _FINISHED_STATES
            and self.windows[wid].get("state") not in _PANE_STATES
        ]
        if not waiting:
            body.append("没有运行中的窗口\n", style="dim")
        else:
            body.append("排队，依赖未完成时不打开 CLI\n", style="dim")
            for wid in waiting[:8]:
                window = self.windows[wid]
                deps = [str(dep) for dep in (window.get("depends_on") or []) if str(dep)]
                dep = (" ← " + ",".join(deps)) if deps else ""
                body.append(f"· {wid} {window.get('title') or ''}{dep}\n".rstrip() + "\n", style="dim")
        return Panel(body, title="排队", subtitle="子代理", border_style="dim")

    def _result_panel(self):
        body = Text()
        body.append(self.phase + "\n", style="bold")
        for wid in self.order:
            window = self.windows[wid]
            state = str(window.get("state") or "")
            icon = _STATUS_ICON.get(state, "·")
            mark = "✔" if window.get("ok") is True else ("✘" if window.get("ok") is False else icon)
            elapsed = window.get("elapsed")
            extra = f"  {elapsed}s" if elapsed is not None else ""
            title = str(window.get("title") or "")[:24]
            summary = str(window.get("summary") or "")[:48]
            body.append(f"{mark} {wid} {title}  {summary}{extra}".rstrip() + "\n")
            if self.expanded:
                for line in list(window.get("log") or [])[-6:]:
                    body.append(f"  {line[:70]}\n", style="dim")
        if self.integration:
            body.append(f"集成：{self.integration[:80]}\n", style="bold")
        hint = self._cli_hint()
        if hint:
            body.append(f"日志：{hint}\n", style="dim")
        return Panel(body, title="结果", subtitle="子代理已关闭", border_style="green")


def window_key(order: list[str], windows: dict[str, dict], window: dict) -> str:
    for key in order:
        if windows.get(key) is window:
            return key
    return "?"
