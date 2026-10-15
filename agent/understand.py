# agent/understand.py
"""L1 理解器（20261015-1705 P1）：一次模型调用，只输出意图 JSON。
输入 = 原句 + 动作卡片（catalog 一行一个动作，无 schema）+ 3 条检索标题；
三条铁律：只选表内动作／只填槽位名不编路径／分类推不出的写进 unresolved。
模型不碰工具 schema、不发规划窗口清单、不宣布成功。"""
import json
import os
import re

from agent.catalog import CATALOG, INTEGRATE_ACTION
from agent.intent import INTENT_SCHEMA, validate_intent


def _action_cards() -> list[str]:
    lines = []
    for name, spec in CATALOG.items():
        if name == INTEGRATE_ACTION:
            continue
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


def _complete_live_usage(messages: list[dict]) -> tuple[str, dict | None]:
    """与 plan_windows._complete_live 同一供应商同一模型；多带回 usage 记进事件。"""
    from openai import OpenAI

    from config import UE_LLM_MODEL

    key = os.getenv("DEEPSEEK_API_KEY") or ""
    if not key.strip():
        raise RuntimeError("没有 DEEPSEEK_API_KEY")
    client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
    response = client.chat.completions.create(model=UE_LLM_MODEL, messages=messages, temperature=0)
    usage = getattr(response, "usage", None)
    tokens = None
    if usage is not None:
        tokens = {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
        }
    return response.choices[0].message.content or "", tokens


def understand(query: str, complete=None) -> dict | None:
    """一次理解器调用 → validate_intent 验收。ok 的意图返回 dict（带 usage 时附 tokens），
    说不出合法意图（表外动作、解析失败、没 key）返回 None，由调用方回退现有行为。"""
    query = (query or "").strip()
    if not query:
        return None
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": user_prompt(query, _titles(query))},
    ]
    text = ""
    tokens = None
    try:
        if complete is not None:
            text = complete(messages)
        else:
            text, tokens = _complete_live_usage(messages)
    except Exception:
        return None
    match = re.search(r"\{.*\}", str(text or ""), re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    intent = validate_intent(data if isinstance(data, dict) else {}, query)
    if not intent.get("ok"):
        return None
    if tokens:
        intent["tokens"] = tokens
    return intent