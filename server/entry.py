# server/entry.py
"""HTML 入口层：DSH 式输入框 + 目录侧边栏 + 拖拽带路径（20261116-1530 计划第 2–3 期）。

**三条不许破的线**（`eval/check_architecture.py` + `eval/check_entry_paths.py` 钉着）：

1. **投影只读文件**：不 import `agent`，不碰编辑器，不调模型。这个模块只 import
   `config` / `core` / `server`（架构检查的 server 行白名单只有那三个）。
2. **驱动只有一个写面**：`data/ui/req/<ts>.txt`（一行需求）、`data/ui/inbox/<run_id>.jsonl`
   （一行一条裁决）、`data/ui/logs/<ts>.log`（子进程的人类可读日志）。除此之外不写任何东西。
3. **进度全来自落盘文件**：`data/runs/**/*.json` 与 `data/events/<run_id>.jsonl`。
   stdout 只当人类可读日志展示，**不解析成状态**。

执行仍然只有一条路：`subprocess` 起 `agent/` 下的入口脚本，UI 只喂一行、只读文件。
`run_guard` 的活 run 互斥、`UE_RAG_STDIN` 的原子改名、`editor_lock` 的跨进程锁都长在
「一个进程一条 run」上，进程内调用等于把它们重做一遍（计划 §2）。
原先这一层起的是同级仓库的 `start.bat 3 --force`；20261116 起入口页迁出到
`AIAgentProject/entry.html`，bat 删除，改名与重定向由本模块自己按同一顺序做。
后续（本次）入口页又搬回本仓库 `server/static/entry.html`：页面是 server 的静态资源，
与 `entry.py` 同仓同层，AIAgentProject 根只留 `start.bat` 一个入口。

启动（一个动作，服务会自己开浏览器）：
`.venv\\Scripts\\python.exe server/entry.py --port 8011`。
`--no-open` 不自动开浏览器（脚本 / CI / 无 GUI 用）。
端口已被一个入口服务占着时，本命令不再报错，直接开页面。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from config import PROJECT_ROOT, UE_PROJECT_DIR, UE_SOURCE_ROOT  # noqa: E402
from server import prompt_builder  # noqa: E402

app = FastAPI(title="ue-rag entry")

DEFAULT_PORT = 8011
_RUNS_LIMIT = 200
_EVENT_TAIL = 400
# 铁律 2 的同栏目录：侧边栏不列、也不许经 /api/tree 绕过去。
TREE_BLOCKED = ("Binaries", "Intermediate", "Saved", "DerivedDataCache", ".vs")

# 模式 → 入口脚本。原 start.bat 的菜单 1/2/3 一一对应（菜单 4 续接走 multi + REPL 指令）。
MODE_SCRIPTS = {
    "chat": "chat.py",
    "loop": "run_multi.py",
    "multi": "run_windows.py",
}
DEFAULT_MODE = "multi"


def _runs_dir() -> Path:
    """与 `agent/sequence.py::RUNS_DIR` 同根（`PROJECT_ROOT/data/runs`）。

    `_runs_day` 的分桶规则在本文件重写了一份（不能 import agent），
    `eval/check_observer_labels.py` 的 O-2 按 5 个样本钉住两处逐字一致。
    """
    override = os.environ.get("UE_ENTRY_RUNS_DIR", "").strip()
    return Path(override) if override else PROJECT_ROOT / "data" / "runs"


def _events_dir() -> Path:
    """与 `agent/run_windows.py::EVENTS_DIR` 同根（`PROJECT_ROOT/data/events`）。"""
    override = os.environ.get("UE_ENTRY_EVENTS_DIR", "").strip()
    return Path(override) if override else PROJECT_ROOT / "data" / "events"


def _ui_dir() -> Path:
    """唯一的写面：`data/ui/{req,inbox,logs}`（计划 §6.2）。"""
    override = os.environ.get("UE_ENTRY_UI_DIR", "").strip()
    return Path(override) if override else PROJECT_ROOT / "data" / "ui"


def _page() -> Path:
    """入口页：`server/static/entry.html`（本仓库内，跟 `entry.py` 同仓同层）。

    历史：20261116 曾把页面迁到同级 `AIAgentProject/entry.html`，随后又搬回本仓库
    `server/static/`——页面是 server 的静态资源，随本仓库走不用两个仓库互相找文件；
    AIAgentProject 根只留 `start.bat` 一个入口。路径只有这一份，`check_observer_labels`
    通过本函数取，不再自己拼。
    """
    return ROOT / "server" / "static" / "entry.html"


def _agent_script(mode: str) -> Path:
    """模式 → `agent/<script>`。未知模式按 DEFAULT_MODE 处理（调用方已校验过）。"""
    return PROJECT_ROOT / "agent" / MODE_SCRIPTS.get(mode, MODE_SCRIPTS[DEFAULT_MODE])


def _runs_day(run_id: str) -> str:
    """run_id 形如 YYYYMMDD-HHMMSS，日期做子目录。对不上（外来 id）留平铺。

    **必须与 `agent.run_windows._runs_day` 逐样本一致**（O-2）。
    """
    head = str(run_id or "").split("-", 1)[0]
    return head if len(head) == 8 and head.isdigit() else ""


def _run_json_path(run_id: str) -> Path:
    day = _runs_day(run_id)
    base = _runs_dir()
    return (base / day if day else base) / f"{run_id}.json"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _iter_run_jsons() -> list[tuple[float, dict]]:
    """所有 run 记录，按 mtime 倒序。跳过原子替换留下的 .tmp。"""
    base = _runs_dir()
    if not base.is_dir():
        return []
    rows: list[tuple[float, dict]] = []
    for path in base.rglob("*.json"):
        if path.name.endswith(".tmp") or ".tmp" in path.suffixes:
            continue
        data = _read_json(path)
        if data is None or not data.get("run_id"):
            # 续接/快照之类别的 json 也在 data/runs 下，没有 run_id 就不算 run 记录。
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        rows.append((mtime, data))
    rows.sort(key=lambda row: row[0], reverse=True)
    return rows


def _summary(run: dict) -> dict:
    """卡片字段全部有出处：没有的字段就不给，不补默认值（§4.5）。"""
    windows = run.get("windows") if isinstance(run.get("windows"), dict) else {}
    states: dict[str, int] = {}
    for entry in windows.values():
        if isinstance(entry, dict):
            state = str(entry.get("state") or "")
            states[state] = states.get(state, 0) + 1
    return {
        "run_id": str(run.get("run_id") or ""),
        "query": str(run.get("query") or ""),
        "status": str(run.get("status") or ""),
        "source": str(run.get("source") or ""),
        "started_at": run.get("started_at"),
        "updated_at": run.get("updated_at"),
        "heartbeat_at": run.get("heartbeat_at"),
        "windows": len(windows),
        "states": states,
        "stop_reason": str(run.get("stop_reason") or ""),
    }


@app.get("/api/health")
def health() -> dict:
    """活 run、编辑器锁、索引、venv。判据口径与 `server/gateway.py::_health_extra` 同一套。"""
    from server.gateway import _health_extra

    live = _live_run_ids()
    payload = {
        "ok": True,
        "port": DEFAULT_PORT,
        "runs_dir": str(_runs_dir()),
        "project_dir": str(UE_PROJECT_DIR),
        "source_root": str(UE_SOURCE_ROOT),
        "live_runs": live,
        "editor_lock": _editor_lock_field(),
    }
    payload.update(_health_extra())
    payload["ok"] = all(bool(item.get("ok")) for item in payload.values() if isinstance(item, dict))
    return payload


def _live_run_ids() -> list[str]:
    """心跳仍在 30 分钟窗口内的 running。判据抄 `agent/run_guard.py::live_run_ids`（不能 import agent）。"""
    moment = datetime.now()
    found: list[str] = []
    for _mtime, run in _iter_run_jsons():
        if str(run.get("status") or "") != "running":
            continue
        stamp = str(run.get("heartbeat_at") or run.get("updated_at") or "").strip()
        try:
            beat = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if (moment - beat).total_seconds() > 30 * 60:
            continue
        found.append(str(run.get("run_id") or ""))
    return found


def _editor_lock_field() -> dict:
    """只读锁文件。**没有锁文件时 `held=False` 且 `window=""`**——UI 显示「写锁 —」，不显示「空闲」。"""
    from core.editor_lock import lock_path

    path = lock_path()
    if not path.is_file():
        return {"ok": True, "held": False, "window": "", "reason": "没有锁文件"}
    data = _read_json(path) or {}
    return {
        "ok": True,
        "held": True,
        "window": str(data.get("window") or ""),
        "pid": data.get("pid"),
        "acquired_at": data.get("acquired_at"),
        "reason": str(path),
    }


@app.get("/api/runs")
def runs(limit: int = 20) -> dict:
    rows = [_summary(run) for _mtime, run in _iter_run_jsons()[: max(1, min(limit, _RUNS_LIMIT))]]
    return {"ok": True, "runs": rows, "live": _live_run_ids()}


@app.get("/api/run/{run_id}")
def run_detail(run_id: str, tail: int = 200) -> dict:
    path = _run_json_path(run_id)
    data = _read_json(path)
    if data is None:
        raise HTTPException(status_code=404, detail=f"没有 run 记录：{path}")
    return {
        "ok": True,
        "run": data,
        "events": _tail_events(run_id, max(1, min(tail, _EVENT_TAIL))),
        "events_path": str(_events_dir() / f"{run_id}.jsonl"),
    }


def _tail_events(run_id: str, count: int) -> list[dict]:
    path = _events_dir() / f"{run_id}.jsonl"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[dict] = []
    for line in lines[-count:]:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            rows.append(event)
    return rows


def _stamp_seconds(stem: str) -> float | None:
    """`YYYYMMDD-HHMMSS` → epoch 秒；对不上返回 None。只用于给 run_id 找对应日志。"""
    try:
        return datetime.strptime(str(stem), "%Y%m%d-%H%M%S").timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _log_path_for(run_id: str) -> Path | None:
    """把 run_id 映射到它那次子进程写的 `data/ui/logs/<ts>.log`。

    正常情况 run_id 与请求文件名/日志名逐字相同（都是同一秒的时间戳）。跨秒时退一步取
    「时间戳最接近且相差 ≤120s」的那份，避免因为落 run 记录晚了 1 秒就读不到日志。
    找不到就返回 None（不瞎配一份别人的日志）。
    """
    folder = _ui_dir() / "logs"
    if not folder.is_dir():
        return None
    exact = folder / f"{run_id}.log"
    if exact.is_file():
        return exact
    want = _stamp_seconds(run_id)
    if want is None:
        return None
    best: tuple[float, Path] | None = None
    for path in folder.glob("*.log"):
        got = _stamp_seconds(path.stem)
        if got is None:
            continue
        delta = abs(got - want)
        if delta <= 120 and (best is None or delta < best[0]):
            best = (delta, path)
    return best[1] if best else None


@app.get("/api/uilog/{run_id}")
def api_uilog(run_id: str, tail: int = 400) -> dict:
    """后台日志尾巴：`data/ui/logs/<ts>.log` 的最后 N 行。**只读**，不解析成状态。

    入口页把它铺在中央区域（左对齐），让「正在写任务书 / 等待人审 / 执行中」这些阶段
    看得见——否则规划+任务书那几十秒到十几分钟里页面一片空白，用户会以为卡死。
    """
    path = _log_path_for(run_id)
    if path is None:
        return {"ok": True, "run_id": run_id, "exists": False, "path": "", "lines": [], "total": 0}
    count = max(1, min(int(tail or 400), 800))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "run_id": run_id, "exists": True, "path": str(path),
                "lines": [], "total": 0, "error": str(exc)}
    lines = text.splitlines()
    return {
        "ok": True,
        "run_id": run_id,
        "exists": True,
        "path": str(path),
        "total": len(lines),
        "lines": lines[-count:],
        "mtime": _mtime(path),
    }


@app.get("/api/stream/{run_id}")
def stream(run_id: str, request: Request) -> StreamingResponse:
    """tail 事件文件。`id:` 是行号，支持 `Last-Event-ID` 续传。不引 sse-starlette。"""
    try:
        start = int(request.headers.get("last-event-id") or 0)
    except (TypeError, ValueError):
        start = 0
    return StreamingResponse(
        _event_stream(run_id, start),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _event_stream(run_id: str, start: int):
    """一行一事件。空闲 15 秒发一个注释心跳，不伪造事件类型。"""
    path = _events_dir() / f"{run_id}.jsonl"
    index = max(0, start)
    idle = 0.0
    while True:
        lines: list[str] = []
        if path.is_file():
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
        if len(lines) > index:
            for offset, line in enumerate(lines[index:], start=index + 1):
                text = line.strip()
                if not text:
                    continue
                yield f"id: {offset}\ndata: {text}\n\n"
            index = len(lines)
            idle = 0.0
        else:
            idle += 1.0
            if idle >= 15.0:
                idle = 0.0
                yield ": keep-alive\n\n"
        time.sleep(1.0)


@app.get("/api/tree")
def tree(path: str = "") -> dict:
    """只列一层，不递归（铁律 2 的同类约束）。`path` 是工程相对路径，越界与屏蔽目录都拒绝。"""
    base = UE_PROJECT_DIR.resolve()
    rel = str(path or "").replace("\\", "/").strip("/")
    target = (base / rel).resolve() if rel else base
    if target != base and base not in target.parents:
        raise HTTPException(status_code=400, detail="路径越界（只接受工程内的相对路径）")
    parts = [] if target == base else target.relative_to(base).as_posix().split("/")
    if any(part in TREE_BLOCKED for part in parts):
        raise HTTPException(status_code=400, detail="屏蔽目录（Binaries/Intermediate/Saved/DDC/.vs）")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"不是目录：{rel or '.'}")

    rows = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读不了目录：{exc}")
    for child in children:
        if child.name in {".git", "__pycache__"}:
            continue
        is_dir = child.is_dir()
        child_rel = child.relative_to(base).as_posix()
        if is_dir and child.name in TREE_BLOCKED:
            continue
        token = prompt_builder.token_of(child_rel, prompt_builder.classify(child_rel, is_dir))
        rows.append({
            "name": child.name,
            "path": child_rel,
            "is_dir": is_dir,
            "size": None if is_dir else _size(child),
            "mtime": _mtime(child),
            "role": token["role"],
            "token": token["token"] if token["ok"] else "",
        })
    return {"ok": True, "path": rel, "entries": rows}


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


class AttBody(BaseModel):
    id: str = ""
    path: str = ""
    role: str = ""
    is_dir: bool = False
    external: bool = False
    name: str = ""


class PreviewBody(BaseModel):
    text: str = ""
    atts: list[AttBody] = Field(default_factory=list)


@app.post("/api/preview")
def preview(body: PreviewBody) -> dict:
    """装配预览。**纯函数，零 IO**：不写请求文件、不起子进程。"""
    res = prompt_builder.build(body.text, [item.model_dump() for item in body.atts])
    res["notes"] = prompt_builder.describe(res)
    return res


# ---------------------------------------------------------------------------
# 驱动（第 3 期）：唯一的写面 = 请求文件一行 / 收件箱一行 / 子进程日志
# ---------------------------------------------------------------------------
class RunBody(BaseModel):
    text: str = ""
    atts: list[AttBody] = Field(default_factory=list)
    force: bool = True
    # 原 start.bat 的三个菜单：multi / loop / chat
    mode: str = DEFAULT_MODE
    # 空 = 多窗口原句；":resume latest" 走多窗口的 REPL 续接路径
    command: str = ""


class AnswerBody(BaseModel):
    run_id: str = ""
    question_id: str = ""
    answer: str = ""
    # 人点按钮时 UI 会带上 kind，只用于记录；领取判据是 run_id + question_id 双键
    kind: str = ""


def _write_request(prompt: str) -> Path:
    """`data/ui/req/<YYYYMMDD-HHMMSS>.txt`：一行需求 + 预留空行。

    续接（计划 3-5）写的是 REPL 指令：`:resume latest` 后跟 `:quit`，与多窗口 REPL 的
    `_resume_command` 走同一条；不带 `:quit` 的话 REPL 会停在提示符上等下一句。
    文件名带时间戳，不覆盖；写完立刻改成只读（计划 §6.2「生成后立即只读」）。

    **占用判据不能只看原名**：`_claim_request` 会把已发的请求改名成 `.consumed`，原名
    于是又「空出来」了。同一秒内连发两次时，只看 `path.exists()` 会复用同一个 stem，
    接着改名撞上一个已存在且只读的 `.consumed`（WinError 5）——那会被误报成
    「另一个 run 在跑」。`.consumed` 同样算占用。
    """
    folder = _ui_dir() / "req"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    def _taken(candidate: Path) -> bool:
        return candidate.exists() or Path(str(candidate) + ".consumed").exists()

    path = folder / f"{stamp}.txt"
    suffix = 1
    while _taken(path):
        suffix += 1
        path = folder / f"{stamp}-{suffix}.txt"
    body = prompt.rstrip("\n")
    if body.lstrip().startswith(":"):
        lines = [body.strip()]
        if ":quit" not in lines[-1]:
            lines.append(":quit")
        body = "\n".join(lines)
    path.write_text(body + "\n", encoding="utf-8")
    try:
        path.chmod(0o444)
    except OSError:
        pass
    return path


def _claim_request(req: Path) -> tuple[Path, bool]:
    """把请求文件改名成 `.consumed`，返回（重定向用的路径, 是否由本次改名）。

    原来这一步在 `start.bat:100` 的 `ren`：**必须先改名再打开重定向**，文件一旦被
    进程打开，Windows 就拒绝改名（WinError 32）。改名失败说明另一个进程已经抢到，
    调用方据此返回退出码 2 的口径（另一个 run 在跑），不再往下起进程。
    """
    taken = Path(str(req) + ".consumed")
    try:
        os.replace(req, taken)
    except OSError:
        return req, False
    return taken, True


def _spawn(req: Path, *, force: bool, mode: str = DEFAULT_MODE) -> dict:
    """起一次真 run。子进程只做一件事：`python agent/<mode 对应的脚本> < req.txt`。

    原先是 `start.bat 3 --force < req.txt`，bat 干了三件事，现在由本模块按同一顺序做：
    1. 先改名占位（`_claim_request`）——顺序反过来就永远抢不到独占；
    2. 设 `UE_RAG_STDIN` 与 `UE_RAG_STDIN_CLAIMED=1`：前者给 `run_guard` 认路径，
       后者告诉它「入口已经改过名了，别再改第二次」；
    3. 设 `UE_RAG_UI_INBOX` 透传决策通道。

    `--plain` 只给走 stdin 重定向的多窗口那条路：无 tty 时 rich 走非终端分支，
    交互路径带上 --plain 会把面板关掉（原 `start.bat:105` 的同一判据）。
    续接也走 multi + stdin（`:resume latest` 是 REPL 指令），不单独用 `--resume`：
    那条路不重定向输入，UI 就喂不进去。
    退出码 2 不是失败，是「另一个 run 在跑」——UI 必须分开显示（计划 3-3）。
    """
    import subprocess
    import sys as _sys

    script = _agent_script(mode)
    if not script.is_file():
        return {"ok": False, "error": f"找不到入口脚本：{script}"}
    logs = _ui_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{req.stem}.log"
    taken, claimed = _claim_request(req)
    if not claimed:
        return {
            "ok": False,
            "error": "输入已被另一个进程占用（请求文件改名失败，另一个 run 在跑）",
            "code": 2,
        }
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env["UE_RAG_STDIN"] = str(taken)
    env["UE_RAG_STDIN_CLAIMED"] = "1"
    env["UE_RAG_UI_INBOX"] = str(_ui_dir() / "inbox")
    argv = [_sys.executable, str(script)]
    if mode == "multi":
        argv.append("--plain")
    if force:
        argv.append("--force")
    if mode in ("multi", "loop"):
        argv.extend(["--ui-inbox", str(_ui_dir() / "inbox")])
    handle = log_path.open("wb")
    try:
        # stdin 直接从 .consumed 读：bat 原先也是 shell 层重定向，Python 侧行为一致。
        with taken.open("rb") as stdin_handle:
            proc = subprocess.Popen(
                argv,
                cwd=str(PROJECT_ROOT),
                stdin=stdin_handle,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=env,
            )
    except OSError as exc:
        handle.close()
        return {"ok": False, "error": f"起子进程失败：{exc}"}
    finally:
        try:
            handle.close()
        except OSError:
            pass
    return {
        "ok": True,
        "pid": proc.pid,
        "argv": argv,
        "mode": mode,
        "request": str(taken),
        "log": str(log_path),
        "inbox": str(_ui_dir() / "inbox"),
        "python": _sys.executable,
    }


@app.post("/api/run")
def api_run(body: RunBody) -> dict:
    """写请求文件 → 起对应模式的 agent 进程。本接口**不等待** run 结束。

    `command` 非空时是 REPL 指令（`:resume latest`），**不过装配器**：它没有正文，
    走 A1–A6 只会得到 `empty_text` 告警。指令由 `_write_request` 自己补 `:quit`。
    """
    mode = str(body.mode or DEFAULT_MODE).strip()
    if mode not in MODE_SCRIPTS:
        return {"ok": False, "error": f"未知模式 {mode}（只认 {'/'.join(MODE_SCRIPTS)}）"}
    command = body.command.strip()
    if command:
        prompt = command
        mode = "multi"          # REPL 指令只对多窗口有意义
    else:
        built = prompt_builder.build(body.text, [item.model_dump() for item in body.atts])
        if not built["ok"]:
            return {
                "ok": False,
                "error": "装配没过：有拒发级告警，先修附件再发。",
                "warnings": built["warnings"],
            }
        prompt = built["prompt"]
    # 先记下"起进程之前"已有的 run_id 集合，再写请求文件、起进程。
    # 顺序不能反过来：子进程落 run 记录只需一两百毫秒，若起完进程再读，新 run 可能已经
    # 出现在这里，前端 `before` 便把它当成"本来就有"，于是永远等不到"新 run"——又是
    # 一次「等 12 秒」的假卡死。放在起进程之前就彻底排除这条竞态。
    before = sorted(_run_ids_now(), reverse=True)[:5]
    req = _write_request(prompt)
    started = _spawn(req, force=bool(body.force), mode=mode)
    started["prompt"] = prompt
    if started.get("ok"):
        # 记下这一刻的 run_id 候选：UI 可以据此在 /api/runs 里认出新 run。
        started["before"] = before
    return started


def _run_ids_now() -> list[str]:
    return [str(run.get("run_id") or "") for _mtime, run in _iter_run_jsons()]


@app.post("/api/answer")
def api_answer(body: AnswerBody) -> dict:
    """把一行裁决写进 `data/ui/inbox/<run_id>.jsonl`。

    双键：`agent/ui_ask.py` 只认 `id == question_id` 的那一行，对不上的丢弃并告警。
    一次点击只写一行；写完由 UI 禁用按钮直到下一条 `question`。
    """
    import json as _json

    run_id = str(body.run_id or "").strip()
    qid = str(body.question_id or "").strip()
    if not run_id or not qid:
        return {"ok": False, "error": "run_id 与 question_id 都要给（双键，缺一不放行）"}
    row = {
        "id": qid,
        "run_id": run_id,
        "kind": str(body.kind or ""),
        "answer": str(body.answer or ""),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    folder = _ui_dir() / "inbox"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run_id}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_json.dumps(row, ensure_ascii=False) + "\n")
    return {"ok": True, "path": str(path), "row": row}


@app.get("/api/questions/{run_id}")
def api_questions(run_id: str, tail: int = 200) -> dict:
    """这条 run 现在在等哪些表态。**只从事件流读**，不问进程。"""
    events = _tail_events(run_id, max(1, min(tail, _EVENT_TAIL)))
    open_questions: dict[str, dict] = {}
    answered: dict[str, str] = {}
    for ev in events:
        if ev.get("type") == "question" and str(ev.get("question_id") or ""):
            open_questions[str(ev["question_id"])] = ev
        elif ev.get("type") == "answer" and str(ev.get("question_id") or ""):
            answered[str(ev["question_id"])] = str(ev.get("source") or "")
    pending = [
        row for qid, row in open_questions.items() if qid not in answered
    ]
    return {
        "ok": True,
        "run_id": run_id,
        "pending": pending,
        "answered": answered,
    }


@app.get("/")
def index() -> FileResponse:
    page = _page()
    if not page.is_file():
        raise HTTPException(status_code=500, detail=f"缺入口页：{page}")
    return FileResponse(page, media_type="text/html; charset=utf-8")


def _probe_existing(host: str, port: int, timeout: float = 1.0) -> bool:
    """这个端口上是不是已经有一个入口服务在跑。

    判据是 `/api/health` 的**形状**（`runs_dir` + `project_dir`），不是状态码：
    `ok` 在子项有病时会为 False，那时服务仍然是活的，照样该复用。
    """
    import urllib.error
    import urllib.request

    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with urllib.request.urlopen(f"http://{target}:{port}/api/health", timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return False
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(data, dict) and "runs_dir" in data and "project_dir" in data


def _open_browser(url: str) -> None:
    """尽力打开浏览器。打不开不算失败：终端里那行 URL 同样能点。"""
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - 无 GUI / 无默认浏览器都只是「没开成」
        pass


def _open_when_ready(host: str, port: int, url: str, timeout: float = 20.0) -> None:
    """等服务真的开始监听再开浏览器。

    不能在 `uvicorn.run` 之前开：那时端口还没 listen，浏览器会先拿到连接被拒的白页，
    用户以为坏了。这里轮询端口，一旦能连就开一次，然后退出线程。
    """
    import socket
    import time

    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((target, port), timeout=0.5):
                _open_browser(url)
                return
        except OSError:
            time.sleep(0.15)


def main(argv: list[str] | None = None) -> int:
    """一步拉起：起服务 + 开浏览器。

    入口页本身在 `file://` 下**不能**拉起进程（浏览器没有这个 API），所以「打开页面」
    和「起服务」压缩不成两件事的一半——只能让服务自己开浏览器，人只做一个动作。
    `--no-open` 留给脚本/CI/无 GUI 的场合。
    """
    parser = argparse.ArgumentParser(description="ue-rag HTML 入口层（只读投影 + 驱动）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--no-open", dest="open_browser", action="store_false",
        help="不自动打开浏览器（默认会开；脚本、CI、无 GUI 环境用这个）",
    )
    parser.set_defaults(open_browser=True)
    args = parser.parse_args(argv)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::', '') else args.host}:{args.port}/"

    # 已经有实例在跑：不再抢端口，直接把页面打开。重复执行的第二个动作退化成「看页面」。
    if _probe_existing(args.host, args.port):
        print(f"{url} 上已经有一个入口服务在跑，直接开页面。", flush=True)
        if args.open_browser:
            _open_browser(url)
        return 0

    import threading

    import uvicorn

    if args.open_browser:
        threading.Thread(
            target=_open_when_ready, args=(args.host, args.port, url), daemon=True
        ).start()

    print(f"入口页：{url}（Ctrl+C 停止）", flush=True)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except OSError as exc:
        print(f"端口 {args.port} 起不来：{exc}")
        print(f"换一个端口：python server/entry.py --port {args.port + 1}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
