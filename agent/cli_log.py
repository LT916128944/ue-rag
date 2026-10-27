# agent/cli_log.py
"""子 CLI 日志。屏幕上的每一行都落进 data/runs/YYYYMMDD/<run_id>.cli.jsonl，
事后按窗口回放，不用再跑一遍。本文件不画界面。"""
import contextvars
import json
import sys
import threading

from core import console

_CURRENT = contextvars.ContextVar("cli_window", default="")
_LOCK = threading.Lock()
_PAUSE_KEYS = threading.Event()
_pause_live = None

_VISIBLE = frozenset({
    "run_start", "window_state", "tool_call", "judge", "confirm",
    "handoff", "window_result", "integration", "run_end", "files_modified",
})


def bind_window(wid: str):
    """工作线程里调用。之后这个线程的 print 归到这扇窗口。"""
    return _CURRENT.set(str(wid or ""))


def reset_window(token) -> None:
    _CURRENT.reset(token)


def current_window() -> str:
    return str(_CURRENT.get() or "")


def keys_paused() -> bool:
    """人工 input() 期间别抢键盘，否则 E 键线程会把 s/c/r 吃掉。"""
    return _PAUSE_KEYS.is_set()


class _PauseKeys:
    def __enter__(self):
        _PAUSE_KEYS.set()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        _PAUSE_KEYS.clear()


def pause_keys() -> _PauseKeys:
    return _PauseKeys()


def set_status_sink(sink) -> None:
    """主面板接管之后，阶段说明进面板。没接上就直接打到 stdout。
    实现放在 core/console.py：core 层的 rag_core 也要打这类说明，
    不能反过来引用 agent（documents/harness/boundaries.md 的依赖方向表）。"""
    console.set_status_sink(sink)


def status(text: str) -> None:
    console.status(text)


def set_pause_live(fn) -> None:
    """人要打字时先把 Live 停掉，否则光标被藏起来，输入像卡死。"""
    global _pause_live
    _pause_live = fn


class _PauseLive:
    def __enter__(self):
        fn = _pause_live
        self._fn = fn
        if fn is not None:
            fn(False)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        fn = self._fn
        if fn is not None:
            fn(True)


def pause_live() -> _PauseLive:
    return _PauseLive()


def _kind(etype: str, tool: str, reasoning: str) -> str:
    if etype == "tool_call":
        if tool and tool != "llm":
            return "script"
        if reasoning or tool == "llm":
            return "think"
        return "tool"
    if etype == "judge":
        return "judge"
    return etype


def cli_record(event: dict) -> dict | None:
    """把一条运行事件收成一行 CLI 记录。不该给人看的事件返回 None。"""
    etype = str(event.get("type") or "")
    if etype not in _VISIBLE:
        return None
    tool = str(event.get("tool") or "")
    node = str(event.get("node") or "")
    reasoning = str(event.get("reasoning") or "").strip()
    summary = str(event.get("summary") or "").strip()
    note = str(event.get("note") or "").strip()
    ok = event.get("ok")
    mark = "" if ok is None else (" ok" if ok else " 失败")
    calls = [str(item) for item in (event.get("calls") or []) if str(item)] if isinstance(event.get("calls"), list) else []

    if etype == "run_start":
        ids = [
            str(item.get("id"))
            for item in (event.get("windows") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        text = "路线 " + " ".join(ids)
    elif etype == "window_state":
        text = f"状态 {event.get('state') or ''}".strip()
    elif etype == "tool_call":
        parts = []
        if reasoning:
            parts.append("思考 " + reasoning[:400])
        if calls:
            parts.append("准备 " + ", ".join(calls[:8]))
        if tool and tool != "llm":
            line = f"脚本 {tool}{mark}"
            if note and ok is False:
                line += " " + note[:120]
            parts.append(line)
        elif summary and not reasoning:
            parts.append("回复 " + summary[:200])
        elif node:
            parts.append(f"{node}{mark}".strip())
        text = " | ".join(parts) or f"调用{mark}".strip()
    elif etype == "judge":
        text = f"裁判 {tool or node}{mark}".strip()
        if summary:
            text += " " + summary[:160]
        elif note:
            text += " " + note[:160]
    elif etype == "confirm":
        text = "确认 " + str(event.get("option") or "")
        if event.get("path"):
            text += " " + str(event.get("path"))
    elif etype == "handoff":
        unmet = [str(item) for item in (event.get("unmet") or []) if str(item).strip()]
        text = "未完成 " + "；".join(unmet)
    elif etype == "window_result":
        text = f"结果 {event.get('state') or ''} {summary}".strip()
    elif etype == "integration":
        text = f"集成 {event.get('status') or ''} {summary}".strip()
    elif etype == "run_end":
        text = f"结束 {event.get('status') or ''}".strip()
    elif etype == "files_modified":
        text = ("修改了文件 " + str(event.get("kinds") or "")).strip()
    else:
        return None
    text = " ".join(str(text).split())
    if not text:
        return None
    record = {
        "ts": str(event.get("ts") or ""),
        "run_id": str(event.get("run_id") or ""),
        "window": str(event.get("window") or ""),
        "kind": _kind(etype, tool, reasoning),
        "text": text[:800],
    }
    if reasoning:
        record["reasoning"] = reasoning[:800]
    if tool:
        record["tool"] = tool
    if node:
        record["node"] = node
    if ok is not None:
        record["ok"] = bool(ok)
    return record


def append_record(path, record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class _Tap:
    """把 print 收成一行交给面板。Rich 自己的 Console 还拿着原来的 stdout，画面不会被 print 冲掉。"""

    def __init__(self, underlying, sink) -> None:
        self._underlying = underlying
        self._sink = sink
        self._local = threading.local()

    def write(self, text) -> int:
        if not isinstance(text, str):
            text = str(text)
        buf = getattr(self._local, "buf", "") + text
        # 没有换行也先交出去：input() 的提示不带换行，否则面板上看不到。
        if "\n" not in buf:
            self._emit(buf)
            self._local.buf = ""
            return len(text)
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            self._emit(line)
        if buf.strip():
            self._emit(buf)
            buf = ""
        self._local.buf = buf
        return len(text)

    def flush(self) -> None:
        buf = getattr(self._local, "buf", "")
        if buf.strip():
            self._emit(buf)
        self._local.buf = ""
        flush = getattr(self._underlying, "flush", None)
        if flush is not None:
            flush()

    def _emit(self, line: str) -> None:
        text = " ".join(str(line).split())
        if not text:
            return
        try:
            self._sink(text[:500])
        except Exception:
            pass

    def isatty(self) -> bool:
        fn = getattr(self._underlying, "isatty", None)
        return bool(fn()) if fn is not None else False

    def __getattr__(self, name):
        return getattr(self._underlying, name)


def install_tap(sink):
    """替换 sys.stdout。返回还原函数。已经装过就只换接收者。"""
    current = sys.stdout
    if current is None:
        return lambda: None
    if isinstance(current, _Tap):
        current._sink = sink
        return lambda: None
    tap = _Tap(current, sink)
    sys.stdout = tap

    def undo() -> None:
        if isinstance(sys.stdout, _Tap):
            sys.stdout.flush()
        if sys.stdout is tap:
            sys.stdout = current

    return undo
