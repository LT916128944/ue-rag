# agent/understand.py
"""L1 理解器（20261015-1705 P1）：一次模型调用，只输出意图 JSON。
输入 = 原句 + 动作卡片（catalog 一行一个动作，无 schema）+ 3 条检索标题；
三条铁律：只选表内动作／只填槽位名不编路径／分类推不出的写进 unresolved。
模型不碰工具 schema、不发规划窗口清单、不宣布成功。"""
import json
import re

from agent.catalog import iter_prompt_specs
from agent.intent import INTENT_SCHEMA, validate_intent


def _action_cards() -> list[str]:
    lines = []
    for name, spec in iter_prompt_specs():
        slots = "、".join(spec["slots"]) if spec["slots"] else "无"
        if spec.get("mode") == "one":
            slots += "（有一个即可）"
        lines.append(f"- {name}。槽位：{slots}。{spec['summary']}")
    return lines


def system_prompt() -> str:
    lines = [
        "你只输出一个 JSON 意图对象，不解释。",
        "三条铁律：",
        "1. goals 里的 action 只能取动作表的名字；表外动作不要写，推不出的半句写进 unresolved。",
        "2. slots 只填原句里的原始 token（类名、BP_ 名、Actor 标签、坐标），不要编 /Game 路径、资产全名或工具名——路径由代码解析。",
        "3. 数量、位置这类约束写进 args（如 再放一个 / 不要与已有的叠一起），不要因此多拆 goal。",
        "goals 的先后顺序就是执行顺序；后半句依赖前半句时保持先后即可，不要写 depends。",
        "constraints 只作记录：editor 在句子要求启动编辑器时为 true；save 一律 false。",
        "",
        "动作表：",
        *_action_cards(),
        "",
        "输出形状：",
        json.dumps(INTENT_SCHEMA, ensure_ascii=False),
    ]
    return "\n".join(lines)


def user_prompt(query: str, titles: list[str]) -> str:
    """控制在 1KB 上下：原句 + 3 条检索标题。不带检索正文。"""
    payload = {"原句": query, "检索标题": [str(title)[:60] for title in (titles or [])[:3]]}
    return json.dumps(payload, ensure_ascii=False)


def _titles(query: str) -> list[str]:
    """3 条检索标题级摘要。失败就空，不阻塞理解。"""
    try:
        from agent.brief import _retrieval_titles

        return _retrieval_titles(query, 3)
    except Exception:
        return []


_TOKEN_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "cached_tokens",
)


def _messages(query: str) -> list[dict]:
    return [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": user_prompt(query, _titles(query))},
    ]


def _parse_intent(text: str, query: str) -> tuple[dict | None, str | None]:
    match = re.search(r"\{.*\}", str(text or ""), re.S)
    if not match:
        return None, "没有 JSON"
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None, "JSON 解析失败"
    intent = validate_intent(data if isinstance(data, dict) else {}, query)
    if not intent.get("ok"):
        return intent, "validate_intent 未通过"
    return intent, None


def understand_once(query: str) -> dict:
    """一次真实调用。ok 表示 JSON 通过 validate_intent。不回退规划者。"""
    from core.llm_call import complete_logged, model_name

    query = (query or "").strip()
    out = {
        "ok": False,
        "intent": None,
        "error": None,
        "model": model_name(),
        "elapsed": 0,
        "prompt_tokens": None,
        "completion_tokens": None,
    }
    if not query:
        out["error"] = "原句为空"
        return out
    response, record = complete_logged(_messages(query), temperature=0, caller="understand")
    out["model"] = record.get("model")
    out["elapsed"] = record.get("elapsed")
    out["prompt_tokens"] = record.get("prompt_tokens")
    out["completion_tokens"] = record.get("completion_tokens")
    for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cached_tokens"):
        if key in record:
            out[key] = record[key]
    if record.get("error"):
        out["error"] = record["error"]
        return out
    text = ""
    if response is not None:
        text = response.choices[0].message.content or ""
    intent, error = _parse_intent(text, query)
    out["intent"] = intent
    out["error"] = error
    out["ok"] = error is None and bool(intent and intent.get("ok"))
    return out


def understand(query: str, complete=None) -> dict | None:
    """一次理解器调用 → validate_intent 验收。ok 的意图返回 dict（带 usage 时附 tokens），
    说不出合法意图（表外动作、解析失败、没 key）返回 None，由调用方回退现有行为。"""
    query = (query or "").strip()
    if not query:
        return None
    if complete is None:
        detail = understand_once(query)
        if not detail.get("ok"):
            return None
        intent = detail.get("intent") or {}
        tokens = {key: detail[key] for key in _TOKEN_KEYS if key in detail}
        if detail.get("model"):
            tokens["model"] = detail["model"]
        if tokens:
            intent["tokens"] = tokens
        return intent
    try:
        text = complete(_messages(query))
    except Exception:
        return None
    intent, error = _parse_intent(text, query)
    if error or not intent or not intent.get("ok"):
        return None
    return intent