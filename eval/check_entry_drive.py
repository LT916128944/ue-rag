# eval/check_entry_drive.py
"""驱动通道（20261116-1530 HTML 入口层计划 §7 第 3 期 3-3 / 3-4 / 3-5）。

钉三件事，全离线：

* **3-3 起 run**：`POST /api/run` 写 `data/ui/req/<ts>.txt`（一行、只读、不覆盖），
  再起 `python agent/run_windows.py --plain --force < req.txt`。入口层 20261116 改成
  直接起 Python（原先起同级仓库的 `start.bat 3 --force`），这里把 `subprocess.Popen`
  换成替身——所以不起真 run、不叫模型、不碰编辑器。拒发级装配必须**一个文件都不写**。
* **3-4 答裁决**：`POST /api/answer` 只写一行，双键（run_id + question_id）缺一就拒绝；
  `/api/questions/<run_id>` 只从事件流读，`answer` 之后那条 question 不能再算 pending。
* **3-5 续接**：`:resume latest` 落到请求文件里时必须带 `:quit`，否则 REPL 会停在提示符上。

所有目录（runs / events / ui）都指到临时目录，不污染 `data/`。
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROMPT = "在关卡 L_NewTest3 里放一个 BP_TestActor，然后编译工程"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@contextlib.contextmanager
def fake_popen():
    """替换 subprocess.Popen，yield 记录列表。`_spawn` 是 `import subprocess` 后调用，
    打在模块属性上就行，被测函数一个字都不用改。"""
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


def _client(tmp: Path):
    from fastapi.testclient import TestClient

    from server import entry

    return TestClient(entry.app), entry


def setup(tmp: Path) -> None:
    os.environ["UE_ENTRY_RUNS_DIR"] = str(tmp / "runs")
    os.environ["UE_ENTRY_EVENTS_DIR"] = str(tmp / "events")
    os.environ["UE_ENTRY_UI_DIR"] = str(tmp / "ui")
    (tmp / "runs").mkdir(parents=True, exist_ok=True)
    (tmp / "events").mkdir(parents=True, exist_ok=True)


def teardown() -> None:
    for key in ("UE_ENTRY_RUNS_DIR", "UE_ENTRY_EVENTS_DIR", "UE_ENTRY_UI_DIR"):
        os.environ.pop(key, None)

def _write_event(tmp: Path, run_id: str, rows: list[dict]) -> None:
    path = tmp / "events" / f"{run_id}.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def check_run_channel(tmp: Path) -> str:
    """3-3：请求文件 + 子进程 + 拒发不写盘。"""
    client, _ = _client(tmp)

    with fake_popen() as calls:
        # 拒发：两条 /Game，一个文件都不许写，也不许起进程。
        bad = client.post("/api/run", json={
            "text": "把 HUD 改成显示载具信息",
            "atts": [
                {"id": "a1", "path": "Content/UI/WBP_HUD.uasset"},
                {"id": "a2", "path": "Content/Maps/L_NewTest3.umap"},
            ],
        }).json()
        check(not bad["ok"], f"两条 /Game 必须拒发，实际 {bad}")
        check("multi_game_path" in {w["code"] for w in bad.get("warnings") or []},
              f"拒发原因应为 multi_game_path，实际 {bad.get('warnings')}")
        req_dir = tmp / "ui" / "req"
        check(not req_dir.is_dir() or not list(req_dir.iterdir()), "拒发时不许写请求文件")
        check(not calls, f"拒发时不许起进程，实际 {calls}")

        # 正常一次：请求文件写完即只读，内容就是装配后的 prompt 一行。
        res = client.post("/api/run", json={
            "text": PROMPT,
            "atts": [{"id": "a1", "path": "Content/Maps/L_NewTest3.umap"}],
        }).json()
        check(res.get("ok"), f"起 run 应成功，实际 {res}")
        req = Path(res["request"])
        check(req.is_file(), f"请求文件应在 {req}（发起后被改名为 .consumed）")
        text = req.read_text(encoding="utf-8")
        check(text == res["prompt"].rstrip("\n") + "\n", f"请求文件正文不对：{text!r}")
        check(text.count("\n") == 1, f"请求文件只该有一行（+结尾换行），实际 {text.count(chr(10))}")
        check("。参考关卡：/Game/Maps/L_NewTest3" in text, f"装配句没进请求文件：{text!r}")
        mode = req.stat().st_mode
        check(not (mode & 0o222), f"请求文件生成后必须只读，实际 mode={oct(mode)}")
        check(Path(res["log"]).parent.name == "logs", f"子进程日志应在 data/ui/logs：{res['log']}")
        check(res["inbox"].endswith(os.path.join("ui", "inbox")), f"收件箱目录不对：{res['inbox']}")

        # 同名不覆盖：连发两次拿到两个不同的文件（同一秒内也要分开）。
        res2 = client.post("/api/run", json={"text": PROMPT}).json()
        check(res2.get("ok"), f"第二次起 run 也应成功，实际 {res2}")
        check(res2["request"] != res["request"], "两次请求不许写同一个文件")

        # 子进程收到的 argv：解释器 + 多窗口脚本 + --plain + --force（契约里 force 默认为真）。
        argv = calls[-1]["argv"]
        check(Path(argv[0]).name.lower().startswith("python"),
              f"argv[0] 应是解释器而不是 bat：{argv}")
        check(argv[1].endswith("run_windows.py"), f"缺省模式应跑 run_windows.py：{argv}")
        check("--plain" in argv, f"stdin 重定向路径必须带 --plain：{argv}")
        check("--force" in argv, f"契约里 force 默认为真，argv 应带 --force：{argv}")
    return f"  请求文件 {req.name}（只读、一行）+ 子进程 argv {argv} + 拒发不写盘"


def check_resume(tmp: Path) -> str:
    """3-5：续接写请求文件时必须带 :quit，否则 REPL 停在提示符上。"""
    client, _ = _client(tmp)

    with fake_popen() as calls:
        res = client.post("/api/run", json={"text": "", "command": ":resume latest"}).json()
        check(res.get("ok"), f"续接应成功，实际 {res}")
        lines = Path(res["request"]).read_text(encoding="utf-8").splitlines()
        check(lines[0] == ":resume latest", f"第一行应是续接指令，实际 {lines!r}")
        check(":quit" in lines, f"续接必须带 :quit（否则 REPL 等下一句），实际 {lines!r}")
        check(len(lines) == 2, f"续接请求文件应恰好两行，实际 {lines!r}")
        # 续接只对多窗口有意义，且同样带 --force（计划 3-5「续接也要带 --force」）
        argv = calls[-1]["argv"]
        check(argv[1].endswith("run_windows.py"), f"续接应走多窗口脚本：{argv}")
        check("--force" in argv, f"续接也要带 --force：{argv}")
    return f"  续接请求文件 {lines}"


def check_answer(tmp: Path) -> str:
    """3-4：一行裁决、双键。"""
    from server import entry

    client, _ = _client(tmp)
    run_id = "20261116-171500"
    _write_event(tmp, run_id, [
        {"ts": "2026-11-16T17:15:01", "run_id": run_id, "type": "question",
         "question_id": "Q1", "kind": "decide", "window": "W1", "prompt": "裁决：…", "options": []},
    ])

    pending = client.get(f"/api/questions/{run_id}").json()
    check([row["question_id"] for row in pending["pending"]] == ["Q1"],
          f"未答的 question 应算 pending，实际 {pending}")

    # 缺一就拒绝：不许用「猜 run_id」的方式写收件箱。
    for body in ({"answer": "c"}, {"run_id": run_id, "answer": "c"}, {"question_id": "Q1", "answer": "c"}):
        row = client.post("/api/answer", json=body).json()
        check(not row.get("ok"), f"双键缺一时必须拒绝，实际 {row}")
    inbox = tmp / "ui" / "inbox"
    check(not inbox.is_dir() or not list(inbox.iterdir()), "被拒绝的写入不许落盘")

    ok = client.post("/api/answer", json={
        "run_id": run_id, "question_id": "Q1", "answer": "c", "kind": "decide",
    }).json()
    check(ok.get("ok"), f"写裁决应成功，实际 {ok}")
    path = Path(ok["path"])
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    check(len(lines) == 1, f"一次点击只写一行，实际 {len(lines)} 行")
    row = json.loads(lines[0])
    check(row["id"] == "Q1" and row["run_id"] == run_id and row["answer"] == "c", f"收件箱行不对：{row}")

    # 答完之后：那条 question 不再 pending（UI 据此禁用按钮等下一问）。
    _write_event(tmp, run_id, [
        {"ts": "2026-11-16T17:15:01", "run_id": run_id, "type": "question",
         "question_id": "Q1", "kind": "decide", "window": "W1", "prompt": "裁决：…", "options": []},
        {"ts": "2026-11-16T17:15:02", "run_id": run_id, "type": "answer",
         "question_id": "Q1", "option": "c", "path": "", "source": "ui"},
    ])
    after = client.get(f"/api/questions/{run_id}").json()
    check(after["pending"] == [], f"答过的 question 不该再 pending，实际 {after}")
    check(after["answered"].get("Q1") == "ui", f"answered 要记来源，实际 {after['answered']}")

    # 两次点击写两行（append，不覆盖）——但 UI 侧只允许一次一条。
    client.post("/api/answer", json={"run_id": run_id, "question_id": "Q2", "answer": "s"})
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    check(len(lines) == 2, f"第二条裁决应追加，实际 {len(lines)} 行")
    return f"  收件箱 {path.name}：双键拒绝 / 一次一行 / 答完不再 pending"


def main() -> int:
    try:
        # 子进程（替身 start.bat）可能还握着日志句柄；Windows 上删不掉临时目录不算失败。
        with tempfile.TemporaryDirectory(prefix="entry-drive-", ignore_cleanup_errors=True) as raw:
            tmp = Path(raw)
            setup(tmp)
            try:
                rows = [check_run_channel(tmp), check_resume(tmp), check_answer(tmp)]
            finally:
                teardown()
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print(row)
    print("entry drive ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
