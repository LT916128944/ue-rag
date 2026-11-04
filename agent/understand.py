# agent/understand.py
"""L1 理解器（20261015-1705 P1）：一次模型调用，只输出意图 JSON。
输入 = 原句 + 动作卡片（catalog 一行一个动作，无 schema）+ 3 条检索标题；
四条铁律：只选表内动作／只填槽位名不编路径／分类推不出的写进 unresolved／验收合同 criteria 也由这里给出。
模型不碰工具 schema、不发规划窗口清单、不宣布成功。业务口径写在提示词里，不写进 harness.py。"""
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


def criteria_guide() -> tuple[str, ...]:
    """验收口径。过滤器只抄原句或源码声明，不写死测试名。"""
    return (
        "1. 每条一行：id（c- 开头、同任务内不重复）、evidence_kind、text（点名对象和期望值）。",
        "2. evidence_kind 只能取：",
        "   - cpp_build：C++ 改完要编译通过。改 .cpp/.h 或说「编译工程」时给。",
        "   - blueprint_compile：已有蓝图要能编过。只动一张蓝图、不改源码时给。",
        "   - editor_assert：编辑器里能读出来的状态（位置、bPickable、控件名等）。",
        "   - automation：跑 Automation 过滤器，失败数必须为 0。必须带 filter，filter 用原句里出现的测试名。",
        "   - widget_graph：Widget 控件树与事件绑定真的连上了（读控件名不够）。",
        "   - manual：闭环确实做不到的部分（引擎外的手工美术、音频、手感调参）。"
        "控件图、C++、关卡摆放都能自动做，不要给它们写 manual。",
        "3. 常见形状（照抄形状，不要发明新的 evidence_kind，也不要编原句里没有的过滤器）：",
        "   - 原句点了玩法测试时：c-cpp_build，需要控件图时再加 c-widget_graph，c-automation 的 filter 只抄原句或源码里已声明的那一条。",
        "   - 只编译工程：c-cpp_build。只改一张蓝图：c-blueprint_compile。",
        "   - 只放/移 Actor：c-editor_assert。",
        "4. 条款只写你为这个任务选的内容；原句没提就不要加。全部条款都过，整句才算成功。",
    )


def system_prompt() -> str:
    lines = [
        "你只输出一个 JSON 意图对象，不解释。",
        "四条铁律：",
        "1. goals 里的 action 只能取动作表的名字；表外动作不要写，推不出的半句写进 unresolved。",
        "2. slots 只填原句里的原始 token（类名、BP_ 名、Actor 标签、坐标），不要编 /Game 路径、资产全名或工具名——路径由代码解析。",
        "3. 数量、位置这类约束写进 args（如 再放一个 / 不要与已有的叠一起），不要因此多拆 goal。",
        "4. criteria 是本任务的验收合同：裁判只认这里写下的条款，模型说「已完成」不算数。"
        "没有可验证条款就不要写，禁止编造原句里没有的交付物。",
        "goals 的先后顺序就是执行顺序；后半句依赖前半句时保持先后即可，不要写 depends。",
        "constraints 只作记录：editor 在句子要求启动编辑器时为 true；save 在句子明确要求保存时为 true。",
        "",
        "动作表：",
        *_action_cards(),
        "",
        "验收条款（criteria）写法：",
        *criteria_guide(),
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
        from agent.cli_log import status

        status("正在检索")
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
    try:
        from agent.cli_log import status

        status("正在理解这句话")
    except Exception:
        pass
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


_LAST_ERROR: str = ""


def last_error() -> str:
    """最近一次理解失败的原因。给 _try_understand 打印用——run 里理解器失败
    不能静默回退，人要看得到为什么（20261027-191303 三连败查不到原因的教训）。"""
    return _LAST_ERROR


def understand(query: str, complete=None) -> dict | None:
    """一次理解器调用 → validate_intent 验收。ok 的意图返回 dict（带 usage 时附 tokens），
    说不出合法意图（表外动作、解析失败、没 key）返回 None，由调用方回退现有行为。"""
    global _LAST_ERROR
    query = (query or "").strip()
    if not query:
        return None
    if complete is None:
        detail = understand_once(query)
        if not detail.get("ok"):
            _LAST_ERROR = str(detail.get("error") or "understand_once 未通过")
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
    except Exception as exc:
        _LAST_ERROR = f"{type(exc).__name__}: {exc}"
        return None
    intent, error = _parse_intent(text, query)
    if error or not intent or not intent.get("ok"):
        _LAST_ERROR = str(error or "validate_intent 未通过")
        return None
    return intent