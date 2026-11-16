# server/gateway.py
"""四个 HTTP 路由。不是第二条 Agent 循环：不拆窗口，不跑 LangGraph。"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI
from pydantic import BaseModel, Field

from core.contract_tools import call_named
from core.editor_launch import _mcp_urls, mcp_up
from core.llm_call import complete_logged, model_name

app = FastAPI(title="ue-rag gateway")
_CACHE_KEYS = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cached_tokens")


class ChatBody(BaseModel):
    message: str = ""


class StructuredBody(BaseModel):
    query: str = ""


class ToolBody(BaseModel):
    name: str = ""
    arguments: dict = Field(default_factory=dict)


def _cost(record: dict) -> dict:
    out = {
        "model": record.get("model"),
        "elapsed": record.get("elapsed"),
        "prompt_tokens": record.get("prompt_tokens"),
        "completion_tokens": record.get("completion_tokens"),
        "error": record.get("error"),
    }
    for key in _CACHE_KEYS:
        if key in record:
            out[key] = record[key]
    return out


def _field(ok: bool, reason: str, **extra) -> dict:
    payload = {"ok": bool(ok), "reason": str(reason)}
    payload.update(extra)
    return payload


def _circuit_field(path) -> dict:
    """只读熔断文件。没有文件或已经过期算关闭。不记新的失败。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _field(True, "没有打开的熔断")
    until = 0.0
    tool = ""
    if isinstance(data, dict):
        tool = str(data.get("tool") or "")
        try:
            until = float(data.get("opened_until") or 0)
        except (TypeError, ValueError):
            until = 0.0
    if until > time.time():
        return _field(False, f"{tool or '工具'} 熔断未到点")
    return _field(True, "熔断已关闭或已过期")


def _checkpoint_field(path) -> dict:
    """同一目录能写临时文件，且已有的库文件不是只读。不新建、不截断 checkpoint。"""
    parent = path.parent
    if not parent.is_dir():
        return _field(False, f"目录不在: {parent}")
    probe = parent / ".checkpoint_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return _field(False, str(exc))
    if path.exists() and not os.access(path, os.W_OK):
        return _field(False, f"不可写: {path}")
    return _field(True, str(path))


def _disk_field(path) -> dict:
    try:
        free = shutil.disk_usage(path).free
    except OSError as exc:
        return _field(False, str(exc), mb=None)
    mb = int(free // (1024 * 1024))
    return _field(True, str(mb), mb=mb)


def _health_extra() -> dict:
    """五个探活字段。不调用模型，不往编辑器写。"""
    from config import MCP_INDEX_PATH, PROJECT_ROOT, editor_backend
    from core import fault

    name = editor_backend()
    known = name in ("official", "monolith")
    index_ok = MCP_INDEX_PATH.is_file() and MCP_INDEX_PATH.stat().st_size > 0
    return {
        "backend": _field(known, name if known else f"未知后端 {name}"),
        "index_present": _field(
            index_ok,
            str(MCP_INDEX_PATH) if index_ok else "索引文件不在",
        ),
        "circuit_closed": _circuit_field(fault.circuit_path()),
        "checkpoint_writable": _checkpoint_field(PROJECT_ROOT / "agent_checkpoints.db"),
        "disk_free_mb": _disk_field(PROJECT_ROOT),
    }


@app.get("/api/health")
def health() -> dict:
    """顶层 ok 由五个探活字段派生：任一为 false 就 false。不再是写死的 true。"""
    extra = _health_extra()
    payload = {
        "ok": all(bool(item.get("ok")) for item in extra.values()),
        "model": model_name(),
        "mcp_url": _mcp_urls()[0],
        "mcp_up": mcp_up(),
    }
    payload.update(extra)
    return payload


@app.post("/api/chat")
def chat(body: ChatBody) -> dict:
    """一次补全。不检索，不调用工具，不进图。"""
    text = (body.message or "").strip()
    if not text:
        return {"ok": False, "content": "", "error": "message 为空", "model": model_name()}
    response, record = complete_logged(
        [{"role": "user", "content": text}],
        caller="api_chat",
    )
    content = ""
    if response is not None:
        content = response.choices[0].message.content or ""
    payload = {"ok": not record.get("error"), "content": content}
    payload.update(_cost(record))
    return payload


@app.post("/api/structured")
def structured(body: StructuredBody) -> dict:
    """包一层理解器。验收不过就返回 ok=false，不回退规划者。"""
    from agent.understand import understand_once

    detail = understand_once(body.query or "")
    payload = {
        "ok": bool(detail.get("ok")),
        "intent": detail.get("intent"),
        "error": detail.get("error"),
    }
    for key in ("model", "elapsed", "prompt_tokens", "completion_tokens", *_CACHE_KEYS):
        if key in detail:
            payload[key] = detail[key]
    return payload


@app.post("/api/tool-call")
def tool_call(body: ToolBody) -> dict:
    """只转发契约里的六个工具。"""
    return call_named(body.name, body.arguments)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8010)
