# agent/decompose.py
"""表外需求的一次拆分。不读工具 schema，不读源文件正文。

输入只有三样：这一句、执行器一行卡片、最多 3 条检索标题。
模型返回的每一步必须点名已有脚本，或 apply_spec（新的 C++ 文件）。
脚本接不住的步骤丢弃，不改写成别的动作。
"""
import json
import re

_PATH_RE = re.compile(r"^(Public|Private)/[A-Za-z0-9_]+\.(h|cpp)$")
_MAX_FILES = 4
_MAX_CONTENT = 4000
_MAX_STEPS = 6
_MAX_PACKET = 3500

_SYSTEM = (
    "你只输出 JSON，把这一句拆成脚本步骤。不要解释。\n"
    "script 只能取「执行器」里的名字，或 apply_spec。\n"
    "现有执行器表达不了、需要新的 C++ 文件时才用 apply_spec。"
    "spec.files 每项是 {path, content}。path 只能是 Public/名字.h 或 Private/名字.cpp，"
    f"最多 {_MAX_FILES} 个，每个 content 不超过 {_MAX_CONTENT} 字。\n"
    "slots 只填原句里出现的 token，不要编 /Game 路径，不要编工具名。\n"
    "推不出就输出 {\"steps\":[]}。\n"
    "形状：{\"steps\":[{\"script\":\"动作名\",\"slots\":{},\"spec\":{}}]}"
)


def executor_cards() -> list[str]:
    """执行器菜单：名字加一句摘要。不是短语到动作的硬编码。"""
    from agent.catalog import iter_prompt_specs

    cards = []
    for name, spec in iter_prompt_specs():
        summary = str(spec.get("summary") or "").split("。", 1)[0]
        cards.append(f"- {name}。{summary[:72]}")
    return cards


def packet(clause: str, titles: list[str] | None = None) -> dict:
    """给模型的那一包。超长就截断，不另开第二次检索。"""
    body = {
        "原句": str(clause or "")[:500],
        "执行器": executor_cards(),
        "检索标题": [str(title)[:60] for title in (titles or [])[:3]],
    }
    encoded = json.dumps(body, ensure_ascii=False)
    if len(encoded) <= _MAX_PACKET:
        return body
    body["执行器"] = body["执行器"][:12]
    return body


def messages_for(clause: str, titles: list[str] | None = None) -> list[dict]:
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": json.dumps(packet(clause, titles), ensure_ascii=False)},
    ]


def safe_files(files) -> tuple[list[dict], str]:
    """只收模块内的 .h/.cpp。路径不合法或超长则整包拒绝，不挑着写。"""
    if not isinstance(files, list) or not files:
        return [], "apply_spec 没有 files"
    if len(files) > _MAX_FILES:
        return [], f"apply_spec 超过 {_MAX_FILES} 个文件"
    out = []
    for item in files:
        if not isinstance(item, dict):
            return [], "文件项不是对象"
        path = str(item.get("path") or "").replace("\\", "/").lstrip("/")
        content = str(item.get("content") or "")
        if not _PATH_RE.match(path):
            return [], f"路径不收：{path}"
        if not content.strip() or len(content) > _MAX_CONTENT:
            return [], f"内容空或超长：{path}"
        out.append({"path": path, "content": content})
    return out, ""


def _text_of(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, tuple):
        raw = raw[0]
    try:
        return raw.choices[0].message.content or ""
    except Exception:
        return str(raw or "")


def parse_steps(text: str) -> tuple[list[dict], list[str]]:
    """返回 (可执行步骤, 丢弃原因)。表外 script 丢弃，不换一个别的动作顶上。"""
    from agent.catalog import allows, spec_of

    match = re.search(r"\{.*\}", str(text or ""), re.S)
    if not match:
        return [], ["没有 JSON"]
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return [], ["JSON 解析失败"]
    raw_steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(raw_steps, list):
        return [], ["没有 steps"]
    steps: list[dict] = []
    dropped: list[str] = []
    for raw in raw_steps[:_MAX_STEPS]:
        if not isinstance(raw, dict):
            dropped.append("步骤不是对象")
            continue
        script = str(raw.get("script") or "").strip()
        if script == "apply_spec":
            files, error = safe_files((raw.get("spec") or {}).get("files") if isinstance(raw.get("spec"), dict) else None)
            if error:
                dropped.append(error)
                continue
            steps.append({"script": "apply_spec", "slots": {}, "spec": {"files": files}})
            continue
        if not allows(script):
            dropped.append(f"脚本不存在：{script or '空'}")
            continue
        spec = spec_of(script) or {}
        allowed = set(spec.get("slots") or ())
        slots = {}
        raw_slots = raw.get("slots") if isinstance(raw.get("slots"), dict) else {}
        for key, value in raw_slots.items():
            if key in allowed and str(value or "").strip():
                slots[str(key)] = str(value).strip()[:200]
        steps.append({"script": script, "slots": slots, "spec": {}})
    return steps, dropped


def _slot_pause_cannot_take_path(window: dict) -> bool:
    """缺必填槽位，而且这扇没有 task。r /Game 或 r Public/类名.h 写不回去。

    原句没给这个槽就算接不住：留着会在执行期变成补路径提问
    （20261027-142031：query_actor 没有 name，却问人填关卡目录或头文件）。"""
    if not isinstance(window, dict) or window.get("waiting_external") or window.get("task"):
        return False
    if "缺少槽位" not in str(window.get("error") or ""):
        return False
    return window.get("blocked") is True or window.get("realizable") is False


def windows_from_steps(steps: list[dict]) -> list[dict]:
    """脚本步骤变成窗口。apply_spec 不进动作表，由这里直接生成。
    缺槽位又接不住路径的步骤丢掉，不改写成暂停窗。"""
    from agent.plan_windows import materialize_goal

    windows = []
    previous = ""
    for index, step in enumerate(steps or [], 1):
        script = str(step.get("script") or "")
        wid = f"D{index}"
        depends = [previous] if previous else []
        if script == "apply_spec":
            window = {
                "id": wid,
                "title": "按拆分写入 C++",
                "action": "apply_spec",
                "slots": {},
                "depends_on": depends,
                "max_retries": 0,
                "realizable": True,
                "ok": True,
                "blocked": False,
                "goal": True,
                "spec": dict(step.get("spec") or {}),
            }
        else:
            made = materialize_goal(
                script,
                dict(step.get("slots") or {}),
                title="",
                wid=wid,
                depends=depends,
                retries=1,
            )
            window = made[0] if made else None
        if window and _slot_pause_cannot_take_path(window):
            continue
        if window:
            windows.append(window)
            previous = str(window.get("id") or wid)
    return windows


def windows_for_clause(clause: str, complete, titles: list[str] | None = None) -> list[dict]:
    """一次模型调用。complete 由调用方注入；这里不自己连模型。失败或空步骤返回 []。"""
    text = str(clause or "").strip()
    if not text or not callable(complete):
        return []
    try:
        raw = complete(messages_for(text, titles))
    except Exception:
        return []
    steps, _dropped = parse_steps(_text_of(raw))
    return windows_from_steps(steps)
