# core/console.py
"""阶段说明（「正在加载检索模型」这类一行进度）的唯一出口。

core 层不许引用 agent（documents/harness/boundaries.md 的依赖方向表），
而 core/rag_core.py 要打这类说明，所以状态行本身留在 core；
面板接管输出后由 agent/cli_log.py 调 set_status_sink 把接收者装到这里。
core 只认这个函数，不认识面板。"""

_status_sink = None


def set_status_sink(sink) -> None:
    """面板接管之后，阶段说明进面板。没接上就直接打到 stdout。"""
    global _status_sink
    _status_sink = sink


def status(text: str) -> None:
    text = " ".join(str(text or "").split())
    if not text:
        return
    sink = _status_sink
    if sink is not None:
        try:
            sink(text)
            return
        except Exception:
            pass
    print(text, flush=True)
