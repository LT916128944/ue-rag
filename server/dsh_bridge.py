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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import run_policy  # noqa: E402
from core import editor_exec  # noqa: E402
from core.editor_lock import data_dir, editor_lock as _editor_lock  # noqa: E402
from core.rag_core import retrieve  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

mcp = MCPServer("ue-rag-dsh")
_CLI_TIMEOUT = 320.0
_INLINE_LIMIT = 1200


def _data_dir() -> Path:
    return data_dir()


def _task_path() -> Path:
    override = os.environ.get("UE_DSH_TASK_PATH", "").strip()
    return Path(override) if override else _data_dir() / "dsh" / "session_task.json"


def _policy_task() -> dict:
    """allow_writes / save / pie 只走 run_policy('dsh')。图引用和 python 仍从同一份文件补。"""
    policy = run_policy("dsh")
    extra = {"allow_blueprint_ref": "", "allow_python": False}
    path = _task_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            extra = {
                "allow_blueprint_ref": str(data.get("allow_blueprint_ref") or ""),
                "allow_python": data.get("allow_python") is True,
            }
    return {**policy, **extra}


def _is_write(toolset: str, tool: str) -> bool:
    return editor_exec.is_editor_write(toolset, tool)


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
    task = _policy_task()
    write = _is_write(toolset, tool)

    def _run() -> dict:
        if name == "monolith":
            from backends.monolith import exec as monolith_exec

            return monolith_exec(toolset, tool, args or {}, task=task)
        return editor_exec.exec_tool(toolset, tool, args or {}, task=task)

    try:
        if write:
            with _editor_lock():
                result = _run()
        else:
            result = _run()
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
