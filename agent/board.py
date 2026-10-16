# agent/board.py
"""终端面板（图二落地）：主代理区（顶）、运行窗口区（左下，实线）、完成区（右下，暗色）。
只用已装的 rich，不加新依赖。E 键展开/收起完成区明细。echo 是订阅者，不吞 stdout：
面板开着时控制台输出走 board.echo 进「最近输出」，run 记录照常落盘，不受面板影响。"""
import threading
import time
from collections import deque

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

try:
    import msvcrt
except ImportError:  # 非 Windows：面板照常渲染，只是没有 E 键
    msvcrt = None

_SPINNER_NAME = "dots"
_STATUS_ICON = {
    "queued": "·", "running": "▶", "retry": "↻", "revising": "✎",
    "done": "✔", "failed": "✘", "paused": "⏸", "skipped": "⏭",
    "waiting_external": "⏳", "interrupted": "✖",
}
_ACTIVE_STATES = {"queued", "running", "retry", "revising"}
_FINISHED_STATES = {"done", "failed", "paused", "skipped", "waiting_external"}


class Board:
    """一次 run 一个实例。event() 吃事件流，echo() 吃控制台行。线程安全。"""

    def __init__(self, console: Console | None = None) -> None:
        self._lock = threading.RLock()
        self._console = console or Console()
        self._live: Live | None = None
        self._key_thread: threading.Thread | None = None
        self._key_stop = threading.Event()
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
            self._notes.clear()

    def set_review(self, text: str) -> None:
        with self._lock:
            self.review = str(text or "")
            self._refresh()

    def set_resumable(self, run_ids: list[str]) -> None:
        with self._lock:
            self.resumable = [str(x) for x in run_ids or []]
            self._refresh()

    def echo(self, text: str) -> None:
        """控制台订阅者：面板开着时收集最近几行，不向真 stdout 打字，避免打花 Live。"""
        with self._lock:
            self._notes.append(str(text))
            self._refresh()

    def event(self, event: dict) -> None:
        with self._lock:
            self._apply(event)
            self._refresh()

    def _apply(self, event: dict) -> None:
        etype = str(event.get("type") or "")
        if etype == "run_start":
            self.query = str(event.get("query") or self.query)
            self.phase = "执行"
            for window in event.get("windows") or []:
                self._window(window.get("id"), title=window.get("title"),
                             action=window.get("action"), state="queued")
        elif etype == "window_state":
            window = self._window(event.get("window"))
            state = str(event.get("state") or "")
            if state:
                window["state"] = state
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
            if event.get("tool"):
                window["tool"] = str(event["tool"])
            if etype == "judge":
                window["note"] = str(event.get("note") or window.get("note") or "")
        elif etype == "files_modified":
            self.files_modified = True
        elif etype == "window_result":
            window = self._window(event.get("window"))
            window["state"] = "done" if event.get("ok") else str(event.get("state") or "failed")
            window["ok"] = bool(event.get("ok"))
            window["summary"] = str(event.get("summary") or "")[:120]
            window["elapsed"] = event.get("elapsed")
            window["retries"] = event.get("retries")
        elif etype == "integration":
            self.integration = str(event.get("summary") or "")
            self.phase = "集成：" + str(event.get("status") or "")
        elif etype == "run_end":
            self.phase = "结束：" + str(event.get("status") or "")

    def _window(self, wid, **fields) -> dict:
        key = str(wid or "?")
        if key not in self.windows:
            self.windows[key] = {
                "title": "", "action": "", "state": "queued", "node": "",
                "note": "", "ok": None, "elapsed": None, "retries": None, "summary": "",
            }
            self.order.append(key)
        self.windows[key].update({k: v for k, v in fields.items() if v not in (None, "")})
        return self.windows[key]

    # ---------- 生命周期 ----------

    def start(self) -> None:
        with self._lock:
            if self._live is not None:
                return
        live = Live(
            self.render(),
            console=self._console,
            refresh_per_second=4,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        live.start()
        with self._lock:
            self._live = live
        if msvcrt is not None:
            self._key_stop.clear()
            self._key_thread = threading.Thread(target=self._key_loop, name="board-keys", daemon=True)
            self._key_thread.start()

    def stop(self) -> None:
        with self._lock:
            live, self._live = self._live, None
        self._key_stop.set()
        if self._key_thread is not None:
            self._key_thread.join(timeout=0.5)
            self._key_thread = None
        if live is not None:
            try:
                live.update(self.render())
                live.stop()
            except Exception:
                pass

    def _key_loop(self) -> None:
        while not self._key_stop.is_set():
            try:
                if msvcrt.kbhit():
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
        if live is not None:
            try:
                live.update(self.render())
            except Exception:
                pass

    # ---------- 渲染 ----------

    def render(self):
        with self._lock:
            layout = Layout()
            layout.split_column(Layout(name="head", size=7), Layout(name="body"))
            layout["body"].split_row(Layout(name="run", ratio=1), Layout(name="done", ratio=1))

            head = Table.grid(padding=(0, 1))
            head.add_row(Text(f"指令：{self.query[:76]}", style="bold"))
            line = Text()
            line.append(f"阶段：{self.phase}")
            if self.review:
                line.append(f"    审阅：{self.review}")
            if self.resumable:
                line.append(f"    可续接 run：{'、'.join(self.resumable[:3])}", style="yellow")
            head.add_row(line)
            if self.files_modified:
                head.add_row(Text("● 修改了文件", style="green"))
            else:
                head.add_row(Text("○ 未修改文件", style="dim"))
            if self._notes:
                head.add_row(Text("最近输出：" + self._notes[-1][:72], style="dim"))
            else:
                head.add_row(Text("最近输出：（无）", style="dim"))
            layout["head"].update(Panel(head, title="主代理", border_style="blue"))

            run_grid = Table.grid(padding=(0, 1))
            actives = [self.windows[wid] for wid in self.order if self.windows[wid].get("state") in _ACTIVE_STATES]
            if not actives:
                run_grid.add_row(Text("没有运行中的窗口", style="dim"))
            for window in actives[:6]:
                title = f"{window_key(self.order, self.windows, window)} {window.get('title') or ''}"[:44]
                state = str(window.get("state") or "queued")
                if state == "running":
                    left = Group(
                        Spinner(_SPINNER_NAME, text=f"▶ {title}", style="cyan"),
                        Text(f"  正在：{window.get('node') or '待启动'}"
                             + (f" · {window.get('tool')}" if window.get("tool") else ""), style="cyan"),
                    )
                    run_grid.add_row(left)
                else:
                    icon = _STATUS_ICON.get(state, "·")
                    style = "yellow" if state in ("retry", "revising") else "dim"
                    run_grid.add_row(Text(f"{icon} {title}", style=style))
            if self._notes:
                for note in list(self._notes)[-2:-1]:
                    run_grid.add_row(Text(f"· {note[:70]}", style="dim"))
            layout["run"].update(Panel(run_grid, title="运行窗口", subtitle="子代理 · 可续接", border_style="cyan"))

            done_grid = Table.grid(padding=(0, 1))
            finished = [self.windows[wid] for wid in self.order if self.windows[wid].get("state") in _FINISHED_STATES]
            ok_n = sum(1 for window in finished if window.get("ok") is True)
            done_grid.add_row(Text(f"✓ {ok_n} 已完成", style="green"))
            done_grid.add_row(Text("E 展开明细" if not self.expanded else "E 收起明细", style="dim"))
            if self.expanded and finished:
                detail = Table.grid(padding=(0, 1))
                for window in finished:
                    wid = window_key(self.order, self.windows, window)
                    icon = _STATUS_ICON.get(str(window.get("state")), "·")
                    mark = "✔" if window.get("ok") is True else "✘"
                    elapsed = window.get("elapsed")
                    retries = window.get("retries")
                    extra = f"elapsed={elapsed}" if elapsed is not None else ""
                    extra += f" retries={retries}" if retries else ""
                    title = str(window.get("title") or "")[:24]
                    line = Text(f"{icon} {wid} {title} {mark} {extra}".rstrip())
                    detail.add_row(line)
                    if window.get("summary"):
                        detail.add_row(Text(f"  {str(window['summary'])[:70]}", style="dim"))
                done_grid.add_row(detail)
            elif finished:
                done_grid.add_row(Text("  ".join(window_key(self.order, self.windows, w) for w in finished[:8]), style="dim"))
            if self.integration:
                done_grid.add_row(Text(f"集成：{self.integration[:60]}", style="bold"))
            layout["done"].update(Panel(done_grid, title="完成", subtitle=f"{len(finished)} 已终态", border_style="dim"))
            return layout


def window_key(order: list[str], windows: dict[str, dict], window: dict) -> str:
    for key in order:
        if windows.get(key) is window:
            return key
    return "?"
