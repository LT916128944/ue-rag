# eval/check_entry_spawn.py
"""S-0（20261116-1530 HTML 入口层计划 §8）：入口层起子进程的四个口子。

原 `eval/check_start_bat.py` 钉的是 `AIAgentProject/start.bat` 的 `:warn_live_runs`、
`ren` 顺序与 `--plain` 位置。20261116 入口页迁出到 `AIAgentProject/entry.html`（随后又
搬回本仓库 `server/static/entry.html`），那份菜单式 bat 删除、逻辑搬进
`server/entry.py::_spawn` / `_claim_request`，这里按同一判据重新钉住（20261116 之后
`AIAgentProject/start.bat` 只做「起服务」一件事，不含菜单、不改名、不碰 run，因此这里
仍然只钉 Python 侧这四件事）：

1. **`--force` 按参数透传。** `run_guard.admit_live_runs` 靠 argv 里这一项越过活 run；
   `force=False` 时不许出现。原 bat 那个「子程序形参恒为空」的坑在 Python 里不存在，
   但「带没带」仍必须有断言，否则字段被写死也没人发现。
2. **先改名再打开。** `_claim_request` 必须在 `subprocess` 打开 `.consumed` **之前**完成。
   证据不读源码：请求文件写成只读后跑一次真实改名——改得动说明没被打开过。文件若先被
   进程打开，Windows 拒绝改名（WinError 32），顺序反过来这里就是红的。
3. **`--plain` 只在多窗口的 stdin 重定向路。** 无 tty 时 rich 走非终端分支，交互路径带上
   它会把面板关掉；`loop` / `chat` 没有面板也不认这个参数。
4. **占位失败就是退出码 2 的口径**，且不许起进程。UI 靠这个把「另一个 run 在跑」和
   普通失败分开显示。未知模式同样拒绝，不静默回落（回落到多窗口会跑错脚本、烧错预算）。

做法：`_spawn` 照常调用，但 `subprocess.Popen` 换成记录器，只记 argv / env / stdin。
不起真 run、不叫模型、不碰编辑器。
"""
from __future__ import annotations

import contextlib
import importlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@contextlib.contextmanager
def fake_popen():
    """临时替换 subprocess.Popen，yield 记录列表；退出时还原。

    `_spawn` 内部是 `import subprocess` 再 `subprocess.Popen(...)`，所以打在模块属性上
    就够了，被测函数一个字都不用改。
    """
    calls: list[dict] = []
    original = subprocess.Popen

    class _Recorder:
        def __init__(self, argv, **kwargs):
            calls.append({"argv": list(argv), **kwargs})
            self.pid = 4242

    subprocess.Popen = _Recorder  # type: ignore[assignment]
    try:
        yield calls
    finally:
        subprocess.Popen = original  # type: ignore[assignment]


def _make_request(tmp: Path, name: str) -> Path:
    folder = tmp / "ui" / "req"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("编译工程\n", encoding="utf-8")
    path.chmod(0o444)          # `_write_request` 写完就改成只读（计划 §6.2）
    return path


def _setup_env(tmp: Path) -> None:
    os.environ["UE_ENTRY_RUNS_DIR"] = str(tmp / "runs")
    os.environ["UE_ENTRY_EVENTS_DIR"] = str(tmp / "events")
    os.environ["UE_ENTRY_UI_DIR"] = str(tmp / "ui")
    for name in ("runs", "events"):
        (tmp / name).mkdir(parents=True, exist_ok=True)


def _clear_env() -> None:
    for key in ("UE_ENTRY_RUNS_DIR", "UE_ENTRY_EVENTS_DIR", "UE_ENTRY_UI_DIR"):
        os.environ.pop(key, None)


def check_force_and_plain(tmp: Path) -> str:
    """1) --force 透传；3) --plain 只在 multi。"""
    from server import entry

    with fake_popen() as calls:
        started = entry._spawn(_make_request(tmp, "20261116-170000.txt"), force=True, mode="multi")
        check(calls, "带 --force 那次必须真的起进程")
        check(started.get("ok"), f"起进程应成功：{started}")
        argv = calls[-1]["argv"]
        check("--force" in argv, f"--force 没进 argv：{argv}")
        check("--plain" in argv, f"多窗口走 stdin 重定向，argv 必须带 --plain：{argv}")
        check(Path(argv[0]).name.lower().startswith("python"),
              f"argv[0] 应是解释器而不是 bat：{argv}")
        check(argv[1].endswith("run_windows.py"), f"multi 模式应跑 run_windows.py：{argv}")

        entry._spawn(_make_request(tmp, "20261116-170001.txt"), force=False, mode="multi")
        argv2 = calls[-1]["argv"]
        check("--force" not in argv2, f"force=False 时 argv 不许带 --force：{argv2}")

        entry._spawn(_make_request(tmp, "20261116-170002.txt"), force=True, mode="loop")
        argv3 = calls[-1]["argv"]
        check("--plain" not in argv3, f"单循环没有面板，不许带 --plain：{argv3}")
        check(argv3[1].endswith("run_multi.py"), f"loop 模式应跑 run_multi.py：{argv3}")

        entry._spawn(_make_request(tmp, "20261116-170003.txt"), force=True, mode="chat")
        argv4 = calls[-1]["argv"]
        check("--plain" not in argv4, f"普通对话不许带 --plain：{argv4}")
        check(argv4[1].endswith("chat.py"), f"chat 模式应跑 chat.py：{argv4}")
    return "  --force 按参数透传；--plain 只在 multi；三模式各跑对脚本"


def check_claim_before_open(tmp: Path) -> str:
    """2) 先改名再打开：只读请求文件能被改名，说明改名发生在打开之前。"""
    from server import entry

    with fake_popen() as calls:
        req = _make_request(tmp, "20261116-171000.txt")
        taken = Path(str(req) + ".consumed")
        check(not taken.exists(), "前置：.consumed 不该已经存在")
        started = entry._spawn(req, force=True, mode="multi")
        check(started.get("ok"), f"起进程应成功：{started}")
        check(taken.is_file(), f"请求文件必须已改名成 .consumed：{taken}")
        check(not req.exists(), f"原名不许还在（改名是 os.replace，不是复制）：{req}")
        stdin_arg = calls[-1].get("stdin")
        check(getattr(stdin_arg, "name", "") == str(taken),
              f"子进程的 stdin 应指向 .consumed：{getattr(stdin_arg, 'name', stdin_arg)!r}")
        env = calls[-1].get("env") or {}
        check(env.get("UE_RAG_STDIN") == str(taken),
              f"UE_RAG_STDIN 应指向 .consumed：{env.get('UE_RAG_STDIN')!r}")
        check(env.get("UE_RAG_STDIN_CLAIMED") == "1",
              "UE_RAG_STDIN_CLAIMED 必须是 1：否则 run_guard 会再改一次名而失败")
        check(env.get("UE_RAG_UI_INBOX"), "决策通道要透传收件箱目录")
    return "  只读请求文件在打开前完成改名；.consumed 进 stdin 与环境变量"


def check_busy_is_code_2(tmp: Path) -> str:
    """4) 占位失败 = 退出码 2 的口径，且不许起进程。"""
    from server import entry

    with fake_popen() as calls:
        req = _make_request(tmp, "20261116-172000.txt")
        os.replace(req, Path(str(req) + ".consumed"))   # 模拟另一个进程先抢到
        check(not req.exists(), "前置：原名应已不在")
        started = entry._spawn(req, force=True, mode="multi")
        check(not started.get("ok"), f"占位失败时必须报失败：{started}")
        check(started.get("code") == 2,
              f"占位失败的退出码口径是 2（另一个 run 在跑）：{started}")
        check(not calls, f"占位失败时不许起进程：{calls}")
    return "  占位失败 → ok=False / code=2 / 不起进程"


def check_api_mode_validation(tmp: Path) -> str:
    """未知模式必须拒绝；`force` 必须从请求体真的流到 argv。

    后一条是变异测试补的：只测 `_spawn(force=...)` 挡不住「`api_run` 里把 force 写死」
    ——那样每个请求都带 --force，活 run 的互斥就形同虚设。
    """
    from fastapi.testclient import TestClient

    from server import entry

    req_dir = tmp / "ui" / "req"
    # 只比「还没被占用的请求文件」：前面用例留下的 `.consumed` 是正常产物，
    # 拿它当「这次写了文件」的证据会把前一个用例的账算到这一条上。
    def _pending() -> set[str]:
        if not req_dir.is_dir():
            return set()
        return {p.name for p in req_dir.iterdir() if not p.name.endswith(".consumed")}

    before = _pending()
    client = TestClient(entry.app)
    with fake_popen() as calls:
        res = client.post("/api/run", json={"text": "编译工程", "mode": "nope"}).json()
        check(not res.get("ok"), f"未知模式必须拒绝：{res}")
        check("nope" in str(res.get("error") or ""), f"错误里要点出非法模式：{res}")
        check(not calls, f"模式不合法时不许起进程：{calls}")
        mid = _pending()
        check(before == mid, f"模式不合法时不许写请求文件：多了 {sorted(mid - before)}")

        # force 走通：请求体 force=False ⇒ argv 里没有 --force。
        res2 = client.post("/api/run", json={
            "text": "编译工程", "mode": "multi", "force": False,
        }).json()
        check(res2.get("ok"), f"装配能过就该起进程：{res2}")
        check(calls, "force=False 那次也必须真起进程（否则断言空转）")
        check("--force" not in calls[-1]["argv"],
              f"请求体 force=False 时 argv 不许带 --force：{calls[-1]['argv']}")

        # 反方向：force 缺省为真 ⇒ argv 带 --force。
        res3 = client.post("/api/run", json={"text": "编译工程", "mode": "multi"}).json()
        check(res3.get("ok"), f"缺省 force 也该起进程：{res3}")
        check("--force" in calls[-1]["argv"],
              f"缺省 force=True 时 argv 必须带 --force：{calls[-1]['argv']}")
    return "  未知模式被拒（不写请求文件、不起进程）；force 从请求体流到 argv"


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="entry-spawn-", ignore_cleanup_errors=True) as raw:
            tmp = Path(raw)
            _setup_env(tmp)
            try:
                rows = [
                    check_force_and_plain(tmp),
                    check_claim_before_open(tmp),
                    check_busy_is_code_2(tmp),
                    check_api_mode_validation(tmp),
                ]
            finally:
                _clear_env()
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print(row)
    print("entry spawn ok（--force 透传 / 改名早于打开 / --plain 只在 multi / 占位失败退 2）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
