# eval/check_entry_launch.py
"""一步拉起（20261116）：`server/entry.py` 起服务的同时开浏览器。

要钉三件事，全离线（不起 uvicorn、不开真浏览器、不连编辑器）：

1. **默认开、`--no-open` 不开。** 「一步拉起」的全部价值就在这个开关上：默认值被改成
   False、或参数名打错，人就得回到「起服务 + 手敲 URL」两步，而这件事没有任何
   运行时症状——所以只能在参数层钉住。
2. **已经有实例在跑时不抢端口。** 重复执行同一条命令是很容易发生的动作（记不清有没有
   起过）。如果这时走 uvicorn，会得到一个地址占用错误；正确行为是直接把页面打开、退 0。
3. **开浏览器等端口真的 listen 之后再开。** 抢在 `uvicorn.run` 之前开会先拿到连接被拒的
   白页，用户以为坏了。这里用「假 socket 记录何时被调用」证明是轮询之后才开。

做法：把 `webbrowser.open` / `socket.create_connection` / `uvicorn.run` 三处都换成替身，
只记录调用；`_probe_existing` 用假 urlopen 控制「是否已有实例」。
"""
from __future__ import annotations

import io
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _Resp(io.BytesIO):
    """假 HTTP 响应：`urlopen` 用在 with 里，要同时支持上下文管理与 status。"""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def check_flags() -> str:
    """1) 默认开浏览器；`--no-open` 关掉。

    走真 `entry.main`（不是只看 `_parse_args`）：参数名打错、`set_defaults` 被漏掉这类
    错法，只读参数表看不出来。

    浏览器是在 `_open_when_ready` 的线程里开的，所以这里把**线程**换成立即执行，
    再把 `socket.create_connection` 打成「端口已就绪」——这样既不用等真端口，
    又能证明 main 把正确的 host/port/url 交给了那一步。
    """
    import socket
    import threading

    from server import entry

    seen: list[str] = []
    started: dict = {}
    original_probe = entry._probe_existing
    original_open = entry._open_browser
    original_thread = threading.Thread
    original_conn = socket.create_connection

    class _InlineThread:
        """把 target 就地跑完：确定性，不用 sleep 等线程。"""

        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target = target
            self._args = args

        def start(self):
            if self._target:
                self._target(*self._args)

    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    entry._probe_existing = lambda host, port: False          # type: ignore[assignment]
    entry._open_browser = lambda url: seen.append(url)        # type: ignore[assignment]
    threading.Thread = _InlineThread                          # type: ignore[assignment]
    socket.create_connection = lambda addr, timeout=None: _Sock()  # type: ignore[assignment]
    original_run = _patch_uvicorn(entry, started)
    try:
        entry.main(["--port", "18011"])
        check(seen, "默认必须打开浏览器（一步拉起的全部价值在这里）")
        check("18011" in seen[0], f"开出去的 URL 端口不对：{seen}")
        check(started.get("uvicorn"), "默认仍要真的起服务（不能只开浏览器）")

        seen.clear()
        entry.main(["--port", "18011", "--no-open"])
        check(not seen, "--no-open 不许打开浏览器（脚本 / CI 用）")
    finally:
        threading.Thread = original_thread                    # type: ignore[assignment]
        socket.create_connection = original_conn              # type: ignore[assignment]
        entry._probe_existing = original_probe   # type: ignore[assignment]
        entry._open_browser = original_open      # type: ignore[assignment]
        _restore_uvicorn(original_run)
    return "  默认开浏览器；--no-open 关掉"


def check_existing_instance() -> str:
    """2) 已有实例：不抢端口，直接开页面，退 0。"""
    from server import entry

    opened: list[str] = []
    original_probe = entry._probe_existing
    original_open = entry._open_browser
    entry._probe_existing = lambda host, port: True            # type: ignore[assignment]
    entry._open_browser = lambda url: opened.append(url)       # type: ignore[assignment]
    started = {"uvicorn": False}
    original_run = _patch_uvicorn(entry, started)
    try:
        code = entry.main(["--port", "18012"])
    finally:
        entry._probe_existing = original_probe      # type: ignore[assignment]
        entry._open_browser = original_open          # type: ignore[assignment]
        _restore_uvicorn(original_run)
    check(code == 0, f"已有实例时应退 0（不是错误），实际 {code}")
    check(not started["uvicorn"], "已有实例时不许再起 uvicorn（会撞端口占用）")
    check(opened and "18012" in opened[0], f"已有实例时应直接开页面：{opened}")
    return "  已有实例 → 不起 uvicorn、直接开页面、退 0"


def check_probe_shape() -> str:
    """`_probe_existing` 认的是 health 的形状，不是 ok 字段。

    `ok` 在子项有病（例如索引缺失）时会为 False，那时服务仍然活着、页面照常能用——
    拿 `ok` 当判据会导致「明明有服务在跑却报端口占用」。
    """
    from server import entry

    original = urllib.request.urlopen

    def _fake(payload):
        def _open(url, timeout=None):
            return _Resp(json.dumps(payload).encode("utf-8"))
        return _open

    try:
        urllib.request.urlopen = _fake({"ok": False, "runs_dir": "x", "project_dir": "y"})  # type: ignore[assignment]
        check(entry._probe_existing("127.0.0.1", 1), "ok=False 但形状对，仍算已有实例")

        urllib.request.urlopen = _fake({"hello": "world"})  # type: ignore[assignment]
        check(not entry._probe_existing("127.0.0.1", 1), "不是入口服务的响应，不许当成已有实例")

        def _down(url, timeout=None):
            raise OSError("connection refused")
        urllib.request.urlopen = _down  # type: ignore[assignment]
        check(not entry._probe_existing("127.0.0.1", 1), "连不上就是没有实例")
    finally:
        urllib.request.urlopen = original  # type: ignore[assignment]
    return "  _probe_existing 认 health 形状（不看 ok 字段）"


def check_open_after_listen() -> str:
    """3) 浏览器在端口 listen 之后才开，且这条顺序由 `main` 保证。

    两个层次都要钉，缺一个就有盲区：
    * `_open_when_ready` 自己要会重试直到端口通（下面第 1 段）；
    * `main` 必须真的走这个函数，不能直接 `_open_browser`（第 2 段——只测前者时，
      把 main 里的线程换成「立刻开浏览器」这种改法不会被发现）。
    """
    import socket
    import threading

    from server import entry

    # ---- 第 1 段：_open_when_ready 会重试 ----
    opened: list[str] = []
    attempts = {"n": 0}
    ordering: list[str] = []

    original_conn = socket.create_connection
    original_open = entry._open_browser

    def _flaky(addr, timeout=None):
        attempts["n"] += 1
        ordering.append("connect")
        if attempts["n"] < 3:
            raise OSError("not listening yet")

        class _Sock:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Sock()

    socket.create_connection = _flaky            # type: ignore[assignment]
    entry._open_browser = lambda url: ordering.append("open:" + url)  # type: ignore[assignment]
    try:
        entry._open_when_ready("127.0.0.1", 18013, "http://127.0.0.1:18013/")
    finally:
        socket.create_connection = original_conn  # type: ignore[assignment]
        entry._open_browser = original_open       # type: ignore[assignment]
    check(attempts["n"] == 3, f"端口没 listen 时要一直重试，实际试了 {attempts['n']} 次")
    check(ordering and ordering[-1].startswith("open:"),
          f"浏览器必须在 listen 成功之后才开：{ordering}")

    # ---- 第 2 段：main 把它用在正确的 target 上 ----
    # 断言的是「main 把哪个函数交给了线程」，不是「有没有开浏览器」：后者在 target 被
    # 换成 `_open_browser` 时反而还是「开了」，而漏掉等监听这件事恰恰藏在 target 里。
    captured: list[dict] = []
    started: dict = {}
    original_thread = threading.Thread
    original_probe = entry._probe_existing

    class _CapturingThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            captured.append({"target": target, "args": args})

        def start(self):
            pass          # 不执行：只比对 main 怎么组装的这一步

    entry._probe_existing = lambda host, port: False          # type: ignore[assignment]
    threading.Thread = _CapturingThread                       # type: ignore[assignment]
    original_run = _patch_uvicorn(entry, started)
    try:
        entry.main(["--port", "18014"])
    finally:
        threading.Thread = original_thread                    # type: ignore[assignment]
        entry._probe_existing = original_probe                # type: ignore[assignment]
        _restore_uvicorn(original_run)
    check(len(captured) == 1, f"默认应起一个「等监听再开」的线程，实际 {len(captured)} 个")
    check(captured[0]["target"] is entry._open_when_ready,
          "main 必须把 _open_when_ready 交给线程；直接开浏览器会先给一张连接被拒的白页"
          f"（实际 target={captured[0]['target']!r}）")
    args = captured[0]["args"] or ()
    check(len(args) == 3, f"_open_when_ready 要收 (host, port, url)，实际 {args!r}")
    check(args[1] == 18014, f"交给 _open_when_ready 的端口不对：{args!r}")
    check(str(args[2]).endswith(":18014/"), f"交给 _open_when_ready 的 URL 不对：{args!r}")
    return "  端口 listen 之后才开浏览器（重试 + main 必须走这条路）"


def _patch_uvicorn(entry, started: dict):
    """把 `uvicorn.run` 换成记录器。`main` 里是 `import uvicorn` 再调用，打模块属性即可。"""
    try:
        import uvicorn
    except ImportError:      # 没装依赖时这条检查没有意义，直接跳过这一项
        return None
    original = uvicorn.run

    def _run(*args, **kwargs):
        started["uvicorn"] = True

    uvicorn.run = _run  # type: ignore[assignment]
    return original


def _restore_uvicorn(original) -> None:
    if original is None:
        return
    import uvicorn

    uvicorn.run = original  # type: ignore[assignment]


def main() -> int:
    try:
        rows = [
            check_flags(),
            check_existing_instance(),
            check_probe_shape(),
            check_open_after_listen(),
        ]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print(row)
    print("entry launch ok（默认开 / --no-open 关 / 已有实例不抢端口 / listen 后才开）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
