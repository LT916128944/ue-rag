# server/gateway.py
"""四个 HTTP 路由。不是第二条 Agent 循环：不拆窗口，不跑 LangGraph。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI
from pydantic import BaseModel, Field

from config import UE_MCP_URL
from core.contract_tools import call_named
from core.editor_launch import mcp_up
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


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "model": model_name(),
        "mcp_url": UE_MCP_URL,
        "mcp_up": mcp_up(),
    }


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
