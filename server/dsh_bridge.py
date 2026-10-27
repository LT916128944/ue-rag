# server/dsh_bridge.py
"""DSH 薄 MCP 网关：9 个工具，只调用 ue-rag 现有执行器与裁判。

边界：只 import config / core；agent 层（合同、交接、快照）一律通过子进程调
agent/dsh_cli.py。写策略只从 data/dsh/session_task.json 读，模型参数里没有
allow_writes / save / pie。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import DATA_DIR  # noqa: E402
from core import editor_exec, risk  # noqa: E402
from core.rag_core import retrieve  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

mcp = MCPServer("ue-rag-dsh")
_STALE_LOCK = 900.0
_CLI_TIMEOUT = 320.0
_INLINE_LIMIT = 1200


def _data_dir() -> Path:
    override = os.environ.get("UE_DSH_DATA_DIR", "").strip()
    return Path(override) if override else DATA_DIR


def _task_path() -> Path:
    override = os.environ.get("UE_DSH_TASK_PATH", "").strip()
    return Path(override) if override else _data_dir() / "dsh" / "session_task.json"


def _policy_task() -> dict:
    path = _task_path()
    if not path.is_file():
        return {"allow_writes": [], "save": False, "pie": False, "allow_blueprint_ref": "", "allow_python": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"allow_writes": [], "save": False, "pie": False, "allow_blueprint_ref": "", "allow_python": False}
    if not isinstance(data, dict):
        data = {}
    return {
        "allow_writes": [str(item) for item in (data.get("allow_writes") or []) if isinstance(item, str)],
        "save": bool(data.get("save")),
        "pie": bool(data.get("pie")),
        "allow_blueprint_ref": str(data.get("allow_blueprint_ref") or ""),
        "allow_python": data.get("allow_python") is True,
    }


def _is_write(toolset: str, tool: str) -> bool:
    if tool in editor_exec.SAVE_TOOLS or tool in editor_exec.PIE_TOOLS:
        return True
    if tool in editor_exec.BLUEPRINT_EDIT_TOOLS:
        return True
    if editor_exec.is_write_tool(tool):
        return True
    return risk.tier(tool) in ("mid", "high")


def _pid_alive(pid: int) -> bool:
    """持锁进程还在不在。Windows 不能用 os.kill(pid, 0)（那会真把进程杀掉），走 tasklist。

    查不出来时按「还活着」算：宁可多等 900 秒，也不抢活进程的锁造成并发写编辑器。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return True
        return str(pid) in proc.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _lock_stale(lock: Path) -> bool:
    """超过 900 秒，或记的 pid 已经不在了（进程崩溃留下的锁）。读不懂锁文件就当它还持有。"""
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return True
    if age > _STALE_LOCK:
        return True
    try:
        held = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(held, dict):
        return False
    try:
        pid = int(held.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    return not _pid_alive(pid)


@contextmanager
def _editor_lock():
    lock = _data_dir() / "locks" / "editor.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_stale(lock):
                lock.unlink(missing_ok=True)
                continue
            raise RuntimeError("editor busy") from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "ts": time.time()}, handle)
        acquired = True
        break
    if not acquired:
        raise RuntimeError("editor busy")
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _spill(payload: dict, name: str) -> dict:
    spill_dir = _data_dir() / "spill"
    spill_dir.mkdir(parents=True, exist_ok=True)
    path = spill_dir / f"dsh-{int(time.time() * 1000)}-{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": bool(payload.get("ok")),
        "summary": str(payload.get("error") or payload.get("summary") or "结果较大，已落盘")[:240],
        "artifacts": [str(path)],
        "keys": list(payload)[:10],
    }


def _bounded(payload: dict, name: str) -> dict:
    if len(json.dumps(payload, ensure_ascii=False)) <= _INLINE_LIMIT:
        return payload
    return _spill(payload, name)


def _run_json(cmd: list[str], timeout: float = _CLI_TIMEOUT) -> dict:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, *cmd],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return {"ok": False, "error": "子进程无输出: " + " | ".join(tail)[:400]}
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return {"ok": False, "error": "子进程输出不是 JSON", "tail": lines[-1][:200]}


def _cli(*args: str) -> dict:
    return _run_json([str(ROOT / "agent" / "dsh_cli.py"), *args])


def _scan(*args: str) -> dict:
    payload = _run_json([str(ROOT / "scripts" / "scan_mcp_tools.py"), *args], timeout=180.0)
    text = json.dumps(payload, ensure_ascii=False)
    if "input_schema" in text:
        return {"ok": False, "error": "卡片里出现 input_schema，拒绝返回"}
    return payload


@mcp.tool()
def search_project(query: str, top_k: int = 5, where: dict | None = None) -> dict:
    """语义检索项目语料（资产 / C++ / 蓝图图 / 文档 / 历史错误）。只读。"""
    try:
        rows = retrieve(query, max(1, min(int(top_k or 5), 10)), where=where)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    items = [
        {
            "id": str(row.get("id") or ""),
            "metadata": row.get("metadata") or {},
            "content": str(row.get("content") or "")[:400],
            "distance": row.get("distance"),
        }
        for row in rows
    ]
    return _bounded({"ok": True, "count": len(items), "items": items}, "search")


def _backend_gate() -> tuple[str, str]:
    from backends.select import backend_error, current_backend

    name = current_backend()
    return name, backend_error(name)


def _monolith_pair(full_name: str) -> tuple[str, str]:
    text = (full_name or "").strip().replace("/", ".")
    if "." not in text:
        return text, ""
    namespace, action = text.rsplit(".", 1)
    return namespace, action


@mcp.tool()
def scan_editor_tools(query: str, top_k: int = 8) -> dict:
    """按自然语言找编辑器工具，只返回卡片（full_name/toolset/tool_name/required/description），不含 schema。"""
    name, err = _backend_gate()
    if err:
        return {"ok": False, "error": err}
    if name == "monolith":
        from backends.monolith import discover

        token = str(query or "").strip()
        namespace = token if token and " " not in token and "/" not in token else ""
        return discover(namespace)
    return _scan("--query", str(query or ""), "--top-k", str(max(1, min(int(top_k or 8), 20))))


@mcp.tool()
def get_editor_tool_schema(full_name: str) -> dict:
    """只取一个工具的 input_schema。一次一个，不要批量拉。"""
    name, err = _backend_gate()
    if err:
        return {"ok": False, "error": err}
    if name == "monolith":
        from backends.monolith import describe

        namespace, action = _monolith_pair(full_name)
        return describe(namespace, action)
    return _scan("--schema", str(full_name or ""))


@mcp.tool()
def exec_editor_tool(toolset: str, tool: str, args: dict | None = None) -> dict:
    """执行一个编辑器工具。写策略只读 data/dsh/session_task.json，模型无法传 allow_writes/save/pie。"""
    name, err = _backend_gate()
    if err:
        return {"ok": False, "error": err}
    if name == "monolith":
        from backends.monolith import exec as monolith_exec

        return _bounded(monolith_exec(toolset, tool, args or {}, task=_policy_task()), "exec")
    task = _policy_task()
    write = _is_write(toolset, tool)
    try:
        if write:
            with _editor_lock():
                result = editor_exec.exec_tool(toolset, tool, args or {}, task=task)
        else:
            result = editor_exec.exec_tool(toolset, tool, args or {}, task=task)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    return _bounded(result, "exec")


@mcp.tool()
def read_contract(run_id: str) -> dict:
    """读一份 run 的验收合同与状态（criteria/passed）。只读，不改任何条款。"""
    return _bounded(_cli("contract-status", "--run", str(run_id or "")), "contract")


@mcp.tool()
def verify(run_id: str, criterion_id: str) -> dict:
    """对一条条款跑对应裁判；只有裁判 ok 且 kind 匹配才把 passed 改成 true。"""
    return _bounded(_cli("verify", "--run", str(run_id or ""), "--criterion", str(criterion_id or "")), "verify")


@mcp.tool()
def read_handoff(run_id: str, window_id: str = "") -> dict:
    """读扇间交接 JSON（did/evidence/unmet/next），不带工具正文。只读。"""
    args = ["handoff", "--run", str(run_id or "")]
    if str(window_id or "").strip():
        args += ["--window", str(window_id)]
    return _bounded(_cli(*args), "handoff")


@mcp.tool()
def window_state(action: str, run_id: str, window_id: str) -> dict:
    """窗口源码快照：action 只能是 snapshot 或 restore。不保存关卡。"""
    if action not in ("snapshot", "restore"):
        return {"ok": False, "error": "action 只能是 snapshot 或 restore"}
    return _bounded(_cli("window-state", action, "--run", str(run_id or ""), "--window", str(window_id or "")), "window")


@mcp.tool()
def resume_context() -> dict:
    """开工读档：goal/phase/next_action/第一条 pending/最新交接 unmet，≤200 字。"""
    return _cli("resume-line")


if __name__ == "__main__":
    from backends.select import lock_backend

    lock_backend()
    mcp.run()
