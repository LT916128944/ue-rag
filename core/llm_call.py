# core/llm_call.py
"""一次模型调用的记录。不换供应商，不改 base_url。模型名每次读 UE_LLM_MODEL。"""
import json
import os
import time
from datetime import datetime

from config import DATA_DIR

BASE_URL = "https://api.deepseek.com"
COSTS_PATH = DATA_DIR / "costs.jsonl"
_CACHE_KEYS = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cached_tokens")


def model_name() -> str:
    raw = os.getenv("UE_LLM_MODEL")
    if raw is None or not str(raw).strip():
        return "deepseek-chat"
    return str(raw).strip().strip('"').strip("'")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dumped
    return {}


def usage_record(usage) -> dict:
    """只抄响应里真正有的字段。没有缓存字段就不写，不估算。"""
    if usage is None:
        return {}
    raw = _as_dict(usage)
    extra = getattr(usage, "model_extra", None)
    if isinstance(extra, dict):
        raw.update(extra)
    if not isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens", *_CACHE_KEYS):
            if key not in raw:
                value = getattr(usage, key, None)
                if value is not None:
                    raw[key] = value
    out = {}
    for key in ("prompt_tokens", "completion_tokens"):
        number = _number(raw.get(key))
        if number is not None:
            out[key] = number
    for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        number = _number(raw.get(key))
        if number is not None:
            out[key] = number
    details = _as_dict(raw.get("prompt_tokens_details"))
    cached = _number(details.get("cached_tokens"))
    if cached is not None:
        out["cached_tokens"] = cached
    return out


def usage_for_trace(record: dict) -> dict:
    """写进 trace 的那一份。缓存键只在这次响应里有时才带上。"""
    usage = {
        "prompt_tokens": record.get("prompt_tokens"),
        "completion_tokens": record.get("completion_tokens"),
        "model": record.get("model"),
        "llm_elapsed": record.get("elapsed"),
    }
    for key in _CACHE_KEYS:
        if key in record:
            usage[key] = record[key]
    if record.get("error"):
        usage["llm_error"] = record["error"]
    return usage


def append_cost(record: dict) -> None:
    COSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = dict(record)
    line["ts"] = datetime.now().isoformat(timespec="seconds")
    with COSTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def reasoning_content(message) -> str:
    """思考模式的 reasoning_content。下一轮必须原样带回，否则接口 400。"""
    value = getattr(message, "reasoning_content", None)
    if not isinstance(value, str) or not value:
        extra = getattr(message, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get("reasoning_content")
    if not isinstance(value, str) or not value:
        return ""
    return value


def complete_logged(messages, *, tools=None, tool_choice=None, temperature=None, caller: str = "llm") -> tuple[object | None, dict]:
    """调用并落一行。失败不抛，错误写在 record['error']。"""
    from openai import OpenAI

    record = {
        "caller": caller,
        "model": model_name(),
        "elapsed": 0,
        "prompt_tokens": None,
        "completion_tokens": None,
        "error": None,
    }
    key = os.getenv("DEEPSEEK_API_KEY") or ""
    if not key.strip():
        record["error"] = "没有 DEEPSEEK_API_KEY"
        append_cost(record)
        return None, record
    client = OpenAI(api_key=key, base_url=BASE_URL)
    kwargs = {"model": record["model"], "messages": messages}
    if tools is not None:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"
    if temperature is not None:
        kwargs["temperature"] = temperature
    started = time.perf_counter()
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as exc:
        record["elapsed"] = round(time.perf_counter() - started, 3)
        record["error"] = f"{type(exc).__name__}: {exc}"[:500]
        append_cost(record)
        return None, record
    record["elapsed"] = round(time.perf_counter() - started, 3)
    fields = usage_record(getattr(response, "usage", None))
    record["prompt_tokens"] = fields.get("prompt_tokens")
    record["completion_tokens"] = fields.get("completion_tokens")
    for key in _CACHE_KEYS:
        if key in fields:
            record[key] = fields[key]
    append_cost(record)
    return response, record


def complete(messages, *, tools=None, tool_choice=None, temperature=None, caller: str = "llm"):
    """和 complete_logged 相同，但调用失败时抛 RuntimeError。记录已经落盘。"""
    response, record = complete_logged(
        messages, tools=tools, tool_choice=tool_choice, temperature=temperature, caller=caller,
    )
    if record.get("error"):
        raise RuntimeError(record["error"])
    return response, record
