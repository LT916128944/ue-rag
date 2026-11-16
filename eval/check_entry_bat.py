# eval/check_entry_bat.py
"""`AIAgentProject/start.bat`（20261116 起只做「起服务」，不含菜单）。

原来的 `check_start_bat.py` 钉的是旧菜单式 bat（`:warn_live_runs` / `ren` / `--plain`），
那份菜单已删除、逻辑搬进 `server/entry.py::_spawn`（由 `check_entry_spawn.py` 钉住）。
现行 `start.bat` 只剩一个职责：**在任意工作目录下把 `server/entry.py` 正确拉起来**。
它坏起来没有运行时症状（双击闪一下、页面没出来），所以这里钉四件事：

1. **`%~dp0` 不被 `shift` 吃掉。** `shift` 会把 `%0` 一起左移，之后 `%~dp0` 变成**当前
   工作目录**。双击时 cwd 是 `C:\\Windows\\System32`，于是 `..\\RAG\\ue-rag` 全指错地方。
   这条是真实踩过的坑，只能靠「换个 cwd 跑」才验得出来——在脚本所在目录里跑永远是绿的。
2. **默认端口 8011、其余参数原样转发。** 页面上「发送」起的 run 走 `data/ui/*`，
   端口写死在人的书签里；`--port` 打错而无人发现，症状是「页面打不开」。
3. **`nopause` 不暂停。** CI / 脚本里如果它停下来等按键，会挂住整条流水线。
   没给 `nopause` 时必须有 `pause`（双击失败时窗口不许闪退）。
4. **缺 `.venv` / 缺仓库时给明确指引并以非 0 退出**，不静默继续。

做法：把 `start.bat` 复制进临时目录里搭一个同级结构，`.venv\\Scripts\\python.exe` 用
真的解释器（venv 的 exe 靠 `pyvenv.cfg` 找到 base），`server\\entry.py` 换成「把 argv
写到文件里」的替身。全程离线：不起 uvicorn、不开浏览器、不叫模型、不碰编辑器。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# AIWork/{AIAgentProject, RAG/ue-rag}：本文件在 RAG/ue-rag/eval/，往上三层才是 AIWork。
BAT = ROOT.parent.parent / "AIAgentProject" / "start.bat"
STUB = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "Path('argv.json').write_text(json.dumps(sys.argv[1:], ensure_ascii=False))\n"
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _venv_python() -> Path:
    exe = ROOT / ".venv" / "Scripts" / "python.exe"
    check(exe.is_file(), f"找不到解释器，先跑 shortcuts/setup.ps1：{exe}")
    return exe


def _make_tree(tmp: Path, *, with_repo: bool = True, with_venv: bool = True) -> Path:
    """搭 AIWork/{AIAgentProject, RAG/ue-rag} 同级结构，返回 bat 路径。"""
    project = tmp / "AIAgentProject"
    project.mkdir(parents=True)
    bat = project / "start.bat"
    shutil.copy2(BAT, bat)

    repo = tmp / "RAG" / "ue-rag"
    (repo / "server").mkdir(parents=True)
    if with_repo:
        (repo / "server" / "entry.py").write_text(STUB, encoding="utf-8")
    if with_venv:
        scripts = repo / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(_venv_python(), scripts / "python.exe")
        cfg = ROOT / ".venv" / "pyvenv.cfg"
        if cfg.is_file():
            shutil.copy2(cfg, repo / ".venv" / "pyvenv.cfg")
    return bat


def _run(bat: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """跑真 cmd。cwd 故意给一个与脚本无关的目录——`%~dp0` 坏掉只有这样才能抓到。"""
    return subprocess.run(
        ["cmd.exe", "/c", str(bat), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def check_cwd_independent_and_forwarding(tmp: Path) -> str:
    """1) 换到无关 cwd 仍然找对仓库；2) 默认 8011、显式 --port 覆盖；3) nopause 不暂停。"""
    bat = _make_tree(tmp)
    # cwd 必须下潜得够深：`shift` 之后 `%~dp0` 变成本地 cwd，若 cwd 只下潜一层，
    # 它拼出的 `..\\RAG\\ue-rag` 仍可能命中真树，这一项就成了假绿（实测过）。
    foreign = tmp / "elsewhere" / "deep" / "deeper"
    foreign.mkdir(parents=True)

    recorded = tmp / "RAG" / "ue-rag" / "argv.json"

    # ---- 不带 --port：默认 8011，且 nopause 不转发 ----
    proc = _run(bat, foreign, "nopause")
    check(proc.returncode == 0, f"正常路径应退 0，实际 {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    check(recorded.is_file(),
          "入口脚本没被跑到：`%~dp0` 大概被 shift 吃掉了（脚本没找到临时目录里的仓库）"
          f"\n{proc.stdout}\n{proc.stderr}")
    argv_default = recorded.read_text(encoding="utf-8")
    check('--port' in argv_default and '8011' in argv_default,
          f"不给 --port 时必须是 8011：{argv_default}")
    check("nopause" not in argv_default,
          f"`nopause` 是 bat 自己的开关，不许转发给 Python：{argv_default}")

    # ---- 显式 --port 8012：只传 8012，不许同时留一个 8011（argparse 取最后一个，
    #      两个都留着就等于端口靠 argv 顺序碰运气）----
    recorded.unlink()
    proc2 = _run(bat, foreign, "nopause", "--port", "8012")
    check(proc2.returncode == 0, f"带 --port 8012 应退 0，实际 {proc2.returncode}\n{proc2.stdout}")
    argv_port = recorded.read_text(encoding="utf-8")
    check("8012" in argv_port, f"`--port 8012` 必须原样转发：{argv_port}")
    check('"8011"' not in argv_port,
          f"显式给了 --port 8012 就不许再塞一个 8011（端口靠 argv 顺序碰运气）：{argv_port}")
    return "  任意 cwd 都找对仓库；默认 8011、显式 --port 覆盖；nopause 不外传"


def check_pause_present() -> str:
    """3) 没给 nopause 时必须留 `pause`（双击失败不许闪退）。静态判据。"""
    text = BAT.read_text(encoding="utf-8", errors="replace")
    lines = [row.strip().lower() for row in text.splitlines()]
    check(any(row.startswith("pause") for row in lines), "缺 `pause`：双击失败时窗口会一闪而过")
    match = [i for i, row in enumerate(lines) if row == "if defined nopause exit /b %err%"]
    check(match, "没找到「nopause 就不暂停」的那一行：CI 会挂在 pause 上")
    after = lines[match[0]:]
    check(any(row.startswith("pause") for row in after), "`pause` 必须在 nopause 判断之后")
    return "  给了 nopause 才跳过 pause；缺省一定停住"


def check_missing_pieces(tmp: Path) -> str:
    """4) 缺 venv / 缺仓库：明确指引 + 非 0 退出。"""
    foreign = tmp / "nowhere"
    foreign.mkdir()

    no_venv = tmp / "no_venv"
    bat1 = _make_tree(no_venv, with_venv=False)
    proc1 = _run(bat1, foreign, "nopause")
    check(proc1.returncode != 0, f"缺 .venv 必须非 0 退出，实际 {proc1.returncode}")
    check("setup.ps1" in proc1.stdout, f"缺 .venv 时要指到 setup.ps1：{proc1.stdout}")

    no_repo = tmp / "no_repo"
    bat2 = _make_tree(no_repo, with_repo=False)
    proc2 = _run(bat2, foreign, "nopause")
    check(proc2.returncode != 0, f"缺 server/entry.py 必须非 0 退出，实际 {proc2.returncode}")
    check("RAG" in proc2.stdout, f"缺仓库时要说清两个仓库要同级：{proc2.stdout}")
    return "  缺 .venv / 缺仓库 → 明确指引 + 非 0 退出"


def main() -> int:
    # 这条检查测的是**同级 AIAgentProject 仓库里的** start.bat。单独克隆 ue-rag 时它不在：
    # 按仓库既有约定（见 check_capability_surface.py）打印 skip、退 0，不假装验过，
    # 也不因为「文件不在」把 CI 弄红——那和被测的事无关。
    if not BAT.is_file():
        print(f"entry bat skip：{BAT} 不存在（要同级 AIAgentProject 仓库；单独克隆 ue-rag 时正常）")
        return 0
    try:
        with tempfile.TemporaryDirectory(prefix="entry-bat-", ignore_cleanup_errors=True) as raw:
            tmp = Path(raw)
            rows = [
                check_cwd_independent_and_forwarding(tmp / "full"),
                check_pause_present(),
                check_missing_pieces(tmp),
            ]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print(row)
    print("entry bat ok（cwd 无关 / 端口与参数转发 / pause 口径 / 缺件指路）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
