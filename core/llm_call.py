# core/llm_call.py
"""一次模型调用的记录。模型名每次读 UE_LLM_MODEL，API 地址每次读 UE_LLM_BASE_URL。"""
import json
import os
import random
import time
from datetime import datetime

from config import DATA_DIR, llm_base_url

COSTS_PATH = DATA_DIR / "costs.jsonl"
_OVERRIDE: str | None = None

# ---- 错误分类与有限退避 ---------------------------------------------------
# 分类只有这五种，落 costs.jsonl 与 trace 归因用；不认识的异常一律 other，不猜。
ERROR_KINDS = ("auth", "rate", "server", "network", "other")
# 只对限流与上游抖动退避：401/403 的解法是换键，重试只会多烧时间和预算。
RETRYABLE_KINDS = ("rate", "server")
MAX_RETRIES = 2                 # 首次尝试之外最多再试 2 次（总共 3 次请求）
BACKOFF_BASE_SECONDS = 0.5      # 第一次退避的基准秒数
BACKOFF_JITTER = 0.25           # ±25% 抖动：同一刻多个窗口不同时撞回来
BACKOFF_MAX_SECONDS = 8.0       # 单次退避上限

# 退避的 sleep 走模块级函数：离线检查把它换成记录器，断言就不会真睡住。
_sleep = time.sleep


def set_base_url(url: str | None) -> None:
    """临时覆盖本次进程的 API 地址（评测用）。None 恢复读配置。"""
    global _OVERRIDE
    _OVERRIDE = (url or "").strip().strip('"').strip("'") or None


def current_base_url() -> str:
    if _OVERRIDE:
        return _OVERRIDE
    return llm_base_url()
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
    # 失败是重试过还是第一次就退，看 attempts；为什么退，看 error_kind。
    if record.get("attempts") is not None:
        usage["llm_attempts"] = record["attempts"]
    if record.get("error"):
        usage["llm_error"] = record["error"]
    if record.get("error_kind"):
        usage["llm_error_kind"] = record["error_kind"]
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


def classify_error(exc: BaseException) -> str:
    """把一次调用异常归成 ERROR_KINDS 里的一个。只看形状，不看报错文案。

    openai 的 AuthenticationError / RateLimitError / InternalServerError 都带
    status_code；连接层（httpx.TransportError）没有状态码，只能按类名认。
    都不认识的落 other —— 宁可归错也不许猜成可重试。
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        if status in (401, 403):
            return "auth"
        if status == 429:
            return "rate"
        if 500 <= status <= 599:
            return "server"
        return "other"
    by_name = {
        "AuthenticationError": "auth",
        "PermissionDeniedError": "auth",
        "RateLimitError": "rate",
        "InternalServerError": "server",
        "APIConnectionError": "network",
        "APITimeoutError": "network",
        "ConnectError": "network",
        "ConnectTimeout": "network",
        "ReadTimeout": "network",
        "WriteTimeout": "network",
        "PoolTimeout": "network",
        "TransportError": "network",
        "Timeout": "network",
        "TimeoutException": "network",
    }
    return by_name.get(type(exc).__name__, "other")


def retryable(kind: str) -> bool:
    """只有限流与上游抖动值得再试。auth（换键才有用）与其他都一次就停。"""
    return kind in RETRYABLE_KINDS


def backoff_delay(attempt: int) -> float:
    """第 attempt 次重试（从 1 起）前的睡眠秒数：指数增长 + ±25% 抖动，封顶 8 秒。"""
    base = BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1))
    jitter = 1.0 + random.uniform(-BACKOFF_JITTER, BACKOFF_JITTER)
    return round(min(BACKOFF_MAX_SECONDS, base * jitter), 3)


def complete_logged(messages, *, tools=None, tool_choice=None, temperature=None, caller: str = "llm",
                    max_tokens: int | None = None) -> tuple[object | None, dict]:
    """调用并落一行。失败不抛，错误写在 record['error']。

    429 与 5xx（error_kind 为 rate / server）最多再试 MAX_RETRIES 次，退避 sleep
    走模块级 _sleep；401/403（auth）与其他错误一次就停，不重试。整次调用只落
    一行 costs，attempts 是真实请求次数。
    """
    from openai import OpenAI

    record = {
        "caller": caller,
        "model": model_name(),
        "elapsed": 0,
        "prompt_tokens": None,
        "completion_tokens": None,
        "error": None,
        "error_kind": None,
        "attempts": 0,
    }
    key = os.getenv("DEEPSEEK_API_KEY") or ""
    if not key.strip():
        record["error"] = "没有 DEEPSEEK_API_KEY"
        record["error_kind"] = "auth"
        append_cost(record)
        return None, record
    client = OpenAI(api_key=key, base_url=current_base_url())
    kwargs = {"model": record["model"], "messages": messages}
    if tools is not None:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    started = time.perf_counter()
    # 退避也计入 elapsed：这一格问的是「这次调用一共花了多久」，不是单次往返。
    while True:
        record["attempts"] += 1
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:
            kind = classify_error(exc)
            if retryable(kind) and record["attempts"] <= MAX_RETRIES:
                _sleep(backoff_delay(record["attempts"]))
                continue
            record["elapsed"] = round(time.perf_counter() - started, 3)
            record["error"] = f"{type(exc).__name__}: {exc}"[:500]
            record["error_kind"] = kind
            if kind == "auth":
                # 换键才是解法：文案要让人一眼知道下一步做什么。
                record["error"] = (
                    f"{record['error']}（鉴权失败：请检查 .env 里的 DEEPSEEK_API_KEY 是否已失效）"
                )[:500]
            append_cost(record)
            return None, record
        break
    record["elapsed"] = round(time.perf_counter() - started, 3)
    record["error_kind"] = None
    fields = usage_record(getattr(response, "usage", None))
    record["prompt_tokens"] = fields.get("prompt_tokens")
    record["completion_tokens"] = fields.get("completion_tokens")
    for key in _CACHE_KEYS:
        if key in fields:
            record[key] = fields[key]
    append_cost(record)
    return response, record


def complete(messages, *, tools=None, tool_choice=None, temperature=None, caller: str = "llm",
             max_tokens: int | None = None):
    """和 complete_logged 相同，但调用失败时抛 RuntimeError。记录已经落盘。"""
    response, record = complete_logged(
        messages, tools=tools, tool_choice=tool_choice, temperature=temperature, caller=caller,
        max_tokens=max_tokens,
    )
    if record.get("error"):
        raise RuntimeError(record["error"])
    return response, record
