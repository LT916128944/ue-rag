# agent/brief.py
"""任务书：一句话 → 范围 / 尚未完成内容清单 / 估时区间 / 依赖 / 外部依赖。
估算与免责只是记录字段，执行层不读、不作为预算。表外内容由代码丢弃。
人审门：确认 / 修改意见 / 放弃；意见会带进下一轮并记审阅轮次。"""
import json
import re
from datetime import datetime
from pathlib import Path

from agent.catalog import CATALOG, INTEGRATE_ACTION
from agent.ledger import ledger_summary_text
from config import PROJECT_ROOT

BRIEFS_DIR = PROJECT_ROOT / "data" / "briefs"
BRIEF_WINDOW_THRESHOLD = 3
_BRIEF_KEYWORD_RE = re.compile(r"新功能|重构")

_DISCLAIMER = ("任务之间有重叠", "估算是粗的，不构成承诺", "不含等待外部依赖的时间")

# 估时按动作类型给区间（小时）：新功能宽、只读裁决窄。只进文档。
_ACTION_ESTIMATE = {
    "edit_cpp": (4.0, 16.0),
    "compile_project": (2.0, 8.0),
    "compile_blueprint": (1.0, 4.0),
    "bind_blueprint": (1.0, 4.0),
    "place_actor": (1.0, 3.0),
    "set_pickable": (1.0, 4.0),
    "move_actor": (0.5, 2.0),
    "judge_pickup": (0.5, 2.0),
    "query_actor": (0.5, 2.0),
    "run_automation_test": (0.5, 2.0),
    "set_scalar_param": (0.5, 2.0),
    "query_material": (0.5, 1.0),
    "place_batch": (1.0, 4.0),
    "probe_move": (1.0, 4.0),
    "probe_look": (1.0, 4.0),
    "probe_play": (1.0, 4.0),
    INTEGRATE_ACTION: (2.0, 8.0),
}

_SCOPE_KEYS = ("id", "title", "deliverables", "pending_note", "estimate_hours", "depends_on",
               "external_dep", "windows", "blocked", "block_note")
_WINDOW_KEYS = ("id", "title", "action", "slots", "depends_on", "max_retries")
_INTEGRATION_KEYS = ("title", "deliverables", "estimate_hours")
_EXT_KEYS = ("name", "provides", "status")


def needs_brief(query: str, windows: list[dict] | None, intent: dict | None = None) -> bool:
    """大任务判定：说了新功能/重构，或拆出的窗口超过 3 扇，或理解器把半句扔进了
    unresolved（20261015-1705：没着落的半句正是「尚未完成的全部主要内容」该记的）。
    窄任务走快路径，不出任务书。"""
    if _BRIEF_KEYWORD_RE.search(query or ""):
        return True
    if isinstance(intent, dict) and intent.get("unresolved"):
        return True
    return len(windows or []) > BRIEF_WINDOW_THRESHOLD


def _retrieval_titles(query: str, k: int = 3) -> list[str]:
    """search_ue_project 同一漏斗的 3 条标题级摘要。只要标题，不进正文。失败就空。"""
    try:
        from core.rag_core import retrieve

        rows = retrieve(query, top_k=k)
    except Exception:
        return []
    titles: list[str] = []
    for row in rows or []:
        meta = row.get("metadata") or {}
        title = str(meta.get("display_name") or meta.get("asset_name") or meta.get("class_name") or "").strip()
        if not title:
            title = str(row.get("content") or "").strip()[:30]
        if title and title not in titles:
            titles.append(title)
    return titles[:k]


def brief_system_prompt() -> str:
    """任务书生成器只看动作表和任务书 schema。工具 schema、检索正文一律不给。"""
    lines = [
        "你只输出 JSON 任务书，不解释。",
        "把用户一句话拆成范围（A、B、C…），每个范围列「尚未完成」的交付条目，最后带一行累计集成。",
        "deliverables 枚举还没完成的内容，包括「代码已有但未接线、未验证」这类现状，写进 pending_note。",
        "windows 只能用动作表里的动作和槽位名；看不准就不要编。windows 的 depends_on 只引用先出现的窗口 id。",
        "候选窗口给了 id 时，范围 windows 必须原样引用候选的 id 和 action，不要自己编动作、槽位或路径，也不要漏掉候选窗口。",
        "只能等、不能跑的外部服务写进 external_dependencies。范围等外部服务时，把服务名写进该范围的 external_dep。",
        "estimate_hours 是记录字段，不是执行预算；给不出就不写，代码会按动作表估。免责由代码自动带，不要写。",
        "",
        "输出形状：",
        json.dumps({
            "query": "原句",
            "scopes": [{
                "id": "A",
                "title": "范围名",
                "deliverables": ["尚未完成的条目"],
                "pending_note": "代码已有，缺接线和验证",
                "estimate_hours": [4, 16],
                "depends_on": [],
                "external_dep": "",
                "windows": [{"id": "W1", "title": "这一扇要做完的事", "action": "set_pickable",
                             "slots": {"target": "BP_名字"}, "depends_on": []}],
            }],
            "integration": {"title": "累计集成、验证与交付", "deliverables": ["对修改做一次只读复验"]},
            "external_dependencies": [{"name": "Main/M-UE", "provides": "权威初始化", "status": "服务未恢复"}],
        }, ensure_ascii=False),
        "",
        "动作表：",
    ]
    for name, spec in CATALOG.items():
        if name == INTEGRATE_ACTION:
            continue
        slots = "、".join(spec["slots"]) if spec["slots"] else "无"
        if spec.get("mode") == "one":
            slots += "（有一个即可）"
        lines.append(f"- {name}。槽位：{slots}。{spec['summary']}")
    return "\n".join(lines)


def brief_user_prompt(query: str, windows: list[dict] | None, ledger_text: str, titles: list[str]) -> str:
    """请求体控制在 1KB 上下：原句 + 台账摘要 + 检索标题 + 候选窗口。"""
    candidates = [
        {"id": str(w.get("id") or ""), "title": str(w.get("title") or ""),
         "action": str(w.get("action") or ""), "slots": w.get("slots") or {}}
        for w in (windows or [])
    ]
    payload = {
        "原句": query,
        "台账摘要": ledger_text,
        "检索标题": titles,
        "候选窗口": candidates,
    }
    return json.dumps(payload, ensure_ascii=False)


def parse_brief_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("scopes"), list) or not data["scopes"]:
        return None
    return data


def _clamp_hours(value) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        pair = (float(value[0]), float(value[1]))
    except (TypeError, ValueError):
        return None
    low, high = min(pair), max(pair)
    if low <= 0 or high > 999:
        return None
    return [max(0.1, low), min(999.0, high)]


def compute_estimate(scope: dict) -> list[float]:
    """代码侧估算：按窗口动作类型给区间；没有窗口就按交付条目数。只进文档。"""
    windows = scope.get("windows") or []
    if windows:
        low = high = 0.0
        for window in windows:
            pair = _ACTION_ESTIMATE.get(str(window.get("action") or "")) or (1.0, 4.0)
            low += pair[0]
            high += pair[1]
        return [round(low, 1), round(high, 1)]
    count = max(1, len(scope.get("deliverables") or []))
    return [round(1.0 * count, 1), round(4.0 * count, 1)]


def validate_brief(data: dict, query: str) -> dict:
    """schema 由代码验收：未知字段丢弃、估时超界丢弃改用代码估算、交付清单为空的范围暂停。"""
    dropped: list[str] = []
    brief: dict = {"query": str(data.get("query") or query)}
    scopes: list[dict] = []
    seen: set[str] = set()
    for raw in data.get("scopes") or []:
        if not isinstance(raw, dict):
            dropped.append("一条范围不是对象，已丢弃")
            continue
        unknown = [k for k in raw if k not in _SCOPE_KEYS]
        if unknown:
            dropped.append("范围 " + str(raw.get("id") or "?") + " 丢弃未知字段：" + "、".join(sorted(set(map(str, unknown)))))
        sid = str(raw.get("id") or "").strip()
        if not sid:
            dropped.append("无 id 的范围已丢弃")
            continue
        if sid in seen:
            dropped.append(f"范围 {sid} 重复，后一条已丢弃")
            continue
        seen.add(sid)
        deliverables = [str(x).strip() for x in raw.get("deliverables") or [] if str(x).strip()]
        raw_windows = raw.get("windows") if isinstance(raw.get("windows"), list) else []
        wins: list[dict] = []
        for rw in raw_windows:
            if not isinstance(rw, dict):
                dropped.append(f"范围 {sid} 有一条窗口不是对象，已丢弃")
                continue
            unknown_w = [k for k in rw if k not in _WINDOW_KEYS]
            if unknown_w:
                dropped.append("窗口 " + str(rw.get("id") or rw.get("action") or "?") + " 丢弃未知字段：" + "、".join(sorted(set(map(str, unknown_w)))))
            try:
                retries = max(0, int(rw.get("max_retries") or 1))
            except (TypeError, ValueError):
                retries = 1
            wins.append({
                "id": str(rw.get("id") or "").strip(),
                "title": str(rw.get("title") or "").strip(),
                "action": str(rw.get("action") or "").strip(),
                "slots": rw.get("slots") if isinstance(rw.get("slots"), dict) else {},
                "depends_on": [str(x) for x in rw.get("depends_on") or [] if str(x).strip()],
                "max_retries": retries,
            })
        scope = {
            "id": sid,
            "title": str(raw.get("title") or "").strip(),
            "deliverables": deliverables,
            "pending_note": str(raw.get("pending_note") or "").strip(),
            "depends_on": [str(x) for x in raw.get("depends_on") or [] if str(x).strip()],
            "external_dep": str(raw.get("external_dep") or "").strip(),
            "windows": wins,
        }
        hours = _clamp_hours(raw.get("estimate_hours"))
        scope["estimate_hours"] = hours if hours is not None else compute_estimate(scope)
        if not deliverables:
            scope["blocked"] = True
            scope["block_note"] = "交付清单为空，这一范围暂停，不执行它的窗口。"
        if not wins and not scope.get("blocked"):
            scope["block_note"] = "没有可执行的窗口，只记录待办，不进执行。"
        scopes.append(scope)
    brief["scopes"] = scopes

    raw_integration = data.get("integration")
    integration: dict = {}
    if isinstance(raw_integration, dict):
        unknown_i = [k for k in raw_integration if k not in _INTEGRATION_KEYS]
        if unknown_i:
            dropped.append("集成行丢弃未知字段：" + "、".join(sorted(set(map(str, unknown_i)))))
        integration = {
            "title": str(raw_integration.get("title") or "累计集成、验证与交付").strip(),
            "deliverables": [str(x).strip() for x in raw_integration.get("deliverables") or [] if str(x).strip()],
        }
        hours = _clamp_hours(raw_integration.get("estimate_hours"))
        integration["estimate_hours"] = hours if hours is not None else compute_estimate({"windows": [{"action": INTEGRATE_ACTION}]})
    brief["integration"] = integration

    ext: list[dict] = []
    for raw_ext in data.get("external_dependencies") or []:
        if not isinstance(raw_ext, dict):
            dropped.append("一条外部依赖不是对象，已丢弃")
            continue
        unknown_e = [k for k in raw_ext if k not in _EXT_KEYS]
        if unknown_e:
            dropped.append("外部依赖 " + str(raw_ext.get("name") or "?") + " 丢弃未知字段：" + "、".join(sorted(set(map(str, unknown_e)))))
        name = str(raw_ext.get("name") or "").strip()
        if not name:
            dropped.append("无名的外部依赖已丢弃")
            continue
        ext.append({
            "name": name,
            "provides": str(raw_ext.get("provides") or "").strip(),
            "status": str(raw_ext.get("status") or "未登记").strip(),
        })
    brief["external_dependencies"] = ext

    low = sum(float(scope["estimate_hours"][0]) for scope in scopes)
    high = sum(float(scope["estimate_hours"][1]) for scope in scopes)
    if integration:
        low += float(integration["estimate_hours"][0])
        high += float(integration["estimate_hours"][1])
    total = data.get("total_estimate")
    if isinstance(total, dict):
        clamp = _clamp_hours([total.get("min_hours"), total.get("max_hours")])
        if clamp is not None:
            brief["total_estimate"] = {"min_hours": clamp[0], "max_hours": clamp[1]}
    brief.setdefault("total_estimate", {"min_hours": round(low, 1), "max_hours": round(high, 1)})
    brief["disclaimer"] = list(_DISCLAIMER)
    brief["dropped"] = dropped
    return brief


def generate_brief(query: str, windows: list[dict] | None = None, complete=None,
                   feedback: str = "", ledger_text: str | None = None) -> dict | None:
    """生成器只看：原句、动作表、任务书 schema、台账摘要、3 条检索标题、候选窗口。"""
    from agent.plan_windows import _complete_live

    titles = _retrieval_titles(query)
    ledger = ledger_text if ledger_text is not None else ledger_summary_text()
    user = brief_user_prompt(query, windows, ledger, titles)
    if feedback:
        user += "\n上一轮审阅意见（据此修改）：" + str(feedback)[:600]
    messages = [
        {"role": "system", "content": brief_system_prompt()},
        {"role": "user", "content": user},
    ]
    try:
        text = complete(messages) if complete is not None else _complete_live(messages)
    except Exception:
        return None
    data = parse_brief_json(text)
    if data is None:
        return None
    return validate_brief(data, query)


def render_brief_md(brief: dict) -> str:
    """渲染成模版样式：三列表 + 依赖 + 外部依赖 + 总工作量与免责。"""
    lines: list[str] = ["# 任务书" + (" " + str(brief["stamp"]) if brief.get("stamp") else ""), ""]
    lines.append(f"- 原句：{brief.get('query')}")
    if brief.get("json_path"):
        lines.append(f"- JSON：{brief['json_path']}")
    review = brief.get("review") or {}
    if review.get("rounds"):
        lines.append(f"- 审阅：{review['rounds']}（{review.get('status') or '待确认'}）")
    lines += ["", "| 任务范围 | 尚未完成的全部主要内容 | 估时区间（小时） |", "| --- | --- | --- |"]
    for scope in brief.get("scopes") or []:
        items = "<br>".join("- " + d for d in scope.get("deliverables") or []) or "（空）"
        if scope.get("pending_note"):
            items += f"<br>现状：{scope['pending_note']}"
        if scope.get("blocked"):
            items += f"<br>※ {scope.get('block_note') or '这一范围暂停'}"
        deps = "、".join(scope.get("depends_on") or []) or "无"
        items += f"<br>依赖：{deps}"
        if scope.get("external_dep"):
            items += f"<br>等外部：{scope['external_dep']}"
        hours = scope.get("estimate_hours") or [0, 0]
        lines.append(f"| {scope['id']} {scope.get('title') or ''} | {items} | {hours[0]:g}–{hours[1]:g} |")
    integration = brief.get("integration") or {}
    if integration:
        items = "<br>".join("- " + d for d in integration.get("deliverables") or [])
        hours = integration.get("estimate_hours") or [0, 0]
        lines.append(f"| （末行）{integration.get('title') or '累计集成、验证与交付'} | {items} | {hours[0]:g}–{hours[1]:g} |")
    lines += ["", "## 外部依赖（目前依赖）", ""]
    ext = brief.get("external_dependencies") or []
    if ext:
        for item in ext:
            lines.append(f"- {item.get('name')}：{item.get('provides') or ''}。状态：{item.get('status') or '未登记'}")
    else:
        lines.append("- 无")
    total = brief.get("total_estimate") or {"min_hours": 0, "max_hours": 0}
    lines += [
        "",
        "## 总工作量",
        "",
        f"- 合计：{total.get('min_hours', 0):g}–{total.get('max_hours', 0):g} 小时（估算不作为执行预算）",
        "- 免责：" + "；".join(_DISCLAIMER) + "。",
    ]
    if brief.get("dropped"):
        lines += ["", "## 代码验收丢弃的内容", ""]
        lines.extend("- " + str(x) for x in brief["dropped"])
    pending = unfinished_text(unresolved=brief.get("unresolved") if isinstance(brief.get("unresolved"), list) else None)
    if pending:
        lines += ["", "## 尚未完成清单", "", pending]
    return "\n".join(lines) + "\n"


def unfinished_text(ledger_path: Path | None = None, unresolved=None) -> str:
    """台账范围、未提升技能、unresolved 合成尚未完成清单。人不审不执行。"""
    from agent.ledger import read_ledger

    bits = []
    try:
        data = read_ledger(ledger_path)
    except Exception:
        data = {"scopes": []}
    for scope in data.get("scopes") or []:
        if not isinstance(scope, dict) or not scope.get("id"):
            continue
        status = str(scope.get("status") or "pending")
        label = {"done": "完成", "partial": "部分完成", "pending": "未完成"}.get(status, status)
        bits.append(f"{scope.get('id')} {label}")
    lines = []
    if bits:
        lines.append("；".join(bits))
    try:
        from agent.skill_memory import unfinished_skills

        for skill in unfinished_skills():
            lines.append(f"技能 {skill.get('id')} 尚未提升")
    except Exception:
        pass
    for item in unresolved or []:
        text = str(item or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def write_brief(brief: dict, directory: Path | None = None) -> tuple[Path, Path]:
    stamp = str(brief.get("stamp") or datetime.now().strftime("%Y%m%d-%H%M%S"))
    slug = re.sub(r"[^0-9A-Za-z_-]+", "-", str(brief.get("query") or ""))[:24].strip("-") or "brief"
    folder = Path(directory) if directory is not None else BRIEFS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    md = folder / f"{stamp}-{slug}.md"
    js = folder / f"{stamp}-{slug}.json"
    brief.update(stamp=stamp, json_path=str(js))
    js.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    md.write_text(render_brief_md(brief), encoding="utf-8")
    return md, js


_EDIT_HELP = (
    "就地编辑（每条改完立即保存进同一对 .md/.json，元任务是 INT 一行）：\n"
    "  e B.title 新标题        改范围标题\n"
    "  e B.pending 现状说明     改 pending_note（现状）\n"
    "  e B.del+ 新交付条目     加一条「尚未完成」交付\n"
    "  e B.del- 2              删第 2 条交付\n"
    "  e B.est 2 8             改估时区间（小时）\n"
    "  e B.dep A,C             改范围依赖，留空(e B.dep) = 无\n"
    "  e INT.title 新末行       改累计集成行的标题\n"
    "  e INT.del+ 新交付条目    改累计集成行的交付\n"
    "想大改范围结构或窗口，就写一句修改意见交给生成器；手改只动文字不动窗口。"
)
_EDIT_KEY_RE = re.compile(r"^e\s+([A-Za-z]\w*)\.([A-Za-z+-]+)\s*(.*)$")


def _edit_target(brief: dict, tid: str) -> dict | None:
    if tid.upper() == "INT":
        return brief.get("integration") or None
    for scope in brief.get("scopes") or []:
        if str(scope.get("id") or "") == tid:
            return scope
    return None


def _refresh_total(brief: dict) -> None:
    """手改估时或交付后合计跟着变。手改不再走 validate 的重算。"""
    low = high = 0.0
    for scope in (brief.get("scopes") or []) + [brief.get("integration") or {}]:
        hours = scope.get("estimate_hours") or [0, 0]
        low += float(hours[0])
        high += float(hours[1])
    brief["total_estimate"] = {"min_hours": round(low, 1), "max_hours": round(high, 1)}


def _apply_manual_edit(brief: dict, answer: str) -> tuple[bool, str]:
    """e 命令的就地编辑。成功即改在 brief 上（落盘由调用方做），返回 (ok, note)。
    窗口与动作不在手改范围——那要过动作表和槽位校验，写修改意见走生成器。"""
    match = _EDIT_KEY_RE.match((answer or "").strip())
    if not match:
        return False, "写法：e <范围>.<字段> 内容，如 e B.title 接线逻辑。输入 e help 看全部写法。"
    tid, key, value = match.group(1), match.group(2).lower(), match.group(3).strip()
    target = _edit_target(brief, tid)
    if target is None:
        ids = [str(s.get("id")) for s in brief.get("scopes") or []]
        if brief.get("integration"):
            ids.append("INT")
        return False, f"没有范围 {tid}。可用：" + "、".join(ids)
    if key == "title":
        if not value:
            return False, "标题不能为空。"
        target["title"] = value
        return True, f"{tid}.title 已改。"
    if key == "pending":
        target["pending_note"] = value
        return True, f"{tid}.pending 已改。"
    if key in ("del+", "del-"):
        items = list(target.get("deliverables") or [])
        if key == "del+":
            if not value:
                return False, "要写上新条目内容，如 e B.del+ 验证受击表现。"
            items.append(value)
        else:
            if not value.isdigit() or not items or not (1 <= int(value) <= len(items)):
                return False, f"要删第几条：e {tid}.del- 1 到 {len(items)}。"
            items = items[: int(value) - 1] + items[int(value):]
        target["deliverables"] = items
        return True, f"{tid} 交付现在 {len(items)} 条。"
    if key == "est":
        nums = []
        for piece in value.split():
            try:
                nums.append(float(piece))
            except ValueError:
                nums.append(-1.0)
        if len(nums) != 2 or min(nums) < 0 or nums[0] > nums[1]:
            return False, "估时写两个数：e " + tid + ".est 2 8"
        target["estimate_hours"] = [round(nums[0], 1), round(nums[1], 1)]
        _refresh_total(brief)
        return True, f"{tid}.est 已改。"
    if key == "dep":
        if tid.upper() == "INT":
            return False, "末行不接依赖。"
        wanted = [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]
        known = {str(s.get("id")) for s in brief.get("scopes") or []}
        bad = [x for x in wanted if x not in known or x == tid]
        if bad:
            return False, f"依赖里不认识 {('、'.join(bad))}，或指了自己。可用：" + "、".join(sorted(known - {tid}))
        target["depends_on"] = wanted
        return True, (f"{tid}.dep 已改：" + ("、".join(wanted) if wanted else "无"))
    return False, "字段只有 title / pending / del+ / del- / est / dep。输入 e help 看写法。"


def _print_brief_pretty(brief: dict, printer=print) -> None:
    """人审门里给眼睛看的表。文件里落的仍是 render_brief_md 的 markdown。"""
    try:
        from rich.console import Console
        from rich.table import Table

        table = Table(title="任务书·人审门", show_lines=True, expand=True)
        table.add_column("范围", no_wrap=True, style="bold cyan")
        table.add_column("尚未完成的全部主要内容", ratio=1)
        table.add_column("估时(小时)", no_wrap=True, justify="right")

        def _cell(scope: dict) -> str:
            bits = ["• " + item for item in scope.get("deliverables") or []] or ["（空）"]
            if scope.get("pending_note"):
                bits.append(f"[dim]现状：{scope['pending_note']}[/dim]")
            if scope.get("blocked"):
                bits.append(f"[yellow]※ {scope.get('block_note') or '这一范围暂停'}[/yellow]")
            deps = "、".join(scope.get("depends_on") or []) or "无"
            bits.append(f"[dim]依赖：{deps}[/dim]")
            if scope.get("external_dep"):
                bits.append(f"[yellow]等外部：{scope['external_dep']}[/yellow]")
            wins = scope.get("windows") or []
            if wins:
                marks = "、".join(f"{w.get('id') or ''} {w.get('action') or ''}".strip() for w in wins)
                bits.append(f"[dim]窗口：{marks}[/dim]")
            return "\n".join(bits)

        for scope in brief.get("scopes") or []:
            hours = scope.get("estimate_hours") or [0, 0]
            table.add_row(f"{scope['id']} {scope.get('title') or ''}", _cell(scope), f"{hours[0]:g}–{hours[1]:g}")
        integration = brief.get("integration") or {}
        if integration:
            hours = integration.get("estimate_hours") or [0, 0]
            table.add_row(f"[INT] {integration.get('title') or '累计集成、验证与交付'}",
                          _cell(integration), f"{hours[0]:g}–{hours[1]:g}")
        Console().print(table)
    except Exception:
        printer(render_brief_md(brief))
        return
    ext = brief.get("external_dependencies") or []
    total = brief.get("total_estimate") or {"min_hours": 0, "max_hours": 0}
    printer(f"外部依赖：{'；'.join(f"{x.get('name')}（{x.get('status') or '未登记'}）" for x in ext) or '无'}")
    printer(f"合计：{total.get('min_hours', 0):g}–{total.get('max_hours', 0):g} 小时（估算只记录，不作为执行预算）")


def brief_flow(query: str, windows: list[dict] | None = None, complete=None,
               reader=input, printer=print, directory: Path | None = None):
    """生成任务书并走人审门。确认键是人按；e 命令就地编辑并实时落盘同一对文件。
    返回 (status, brief, md, js)；status ∈ confirm / abort。生成失败返回 None（调用方按窄任务继续）。"""
    brief = generate_brief(query, windows=windows, complete=complete)
    if brief is None:
        printer("写不出任务书：生成器没有给出可验收的 JSON。这次按窄任务直接执行。")
        return None
    rounds = 0
    edits = 0
    md = None
    js = None
    while True:
        if md is None:
            rounds += 1
            brief["review"] = {"rounds": rounds, "status": "待确认"}
            md, js = write_brief(brief, directory)
        printer(f"任务书第 {rounds} 版：{md}")
        _print_brief_pretty(brief, printer)
        if edits:
            printer(f"人工编辑 {edits} 次，已实时保存进上面这对 .md/.json。")
        try:
            answer = str(reader(
                "任务书审阅（确认 / e 就地编辑，e help 看写法 / 写修改意见交生成器 / 放弃）: "
            )).strip()
        except (EOFError, KeyboardInterrupt):
            printer("\n放弃")
            return ("abort", brief, md, js)
        if not answer or answer in ("确认", "confirm", "y", "yes", "ok", "通过"):
            brief["review"]["status"] = "通过" if (rounds == 1 and not edits) else "通过，附修改"
            brief["manual_edits"] = edits
            write_brief(brief, directory)
            return ("confirm", brief, md, js)
        if answer in ("放弃", "abort", "q", "quit", "停止"):
            return ("abort", brief, md, js)
        if answer in ("e", "e help"):
            printer(_EDIT_HELP)
            continue
        if answer.split()[0] == "e":
            ok, note = _apply_manual_edit(brief, answer)
            if not ok:
                printer(note)
                continue
            edits += 1
            brief["review"] = {"rounds": rounds, "status": "待确认"}
            write_brief(brief, directory)
            printer(f"已保存：{note}")
            continue
        revised = generate_brief(query, windows=windows, complete=complete, feedback=answer)
        if revised is None:
            printer("按意见重出失败，回到上一版继续审。")
            continue
        brief = revised
        md = None


def materialize_scopes(brief: dict, candidates: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """确认后把范围物化成窗口。范围窗口引用了候选窗口 id 的，原样继承候选
    （快路径已解析的路径、父类、task 一并保留，不叫生成器重编）；引用不上才
    按动作表 materialize，表外照旧暂停。asset 槽位动作统一走归地 materialize_goal
    （20261015-1705 P2）：索引没有但父类可推时，自动插「创建蓝图」前置扇，前置扇
    id 用本扇 id 加 c 后缀，依赖反向挂到本扇。任务书没归类的候选窗口不丢，按原顺序补跑。
    依赖换算分两遍：候选可能排到最后才补跑，先把新旧 id 对照表填全，再统一
    重映射并禁止依赖指向自己，避免撞出自环（2026-10-15 环：W2→W2 修的）。
    等外部的范围标 waiting_external；末尾由代码追加一扇 integrate_verify。
    返回 (windows, scopes_meta)。scopes_meta 供集成报告与台账用。"""
    from agent.plan_windows import materialize, materialize_goal

    candidates = [cand for cand in (candidates or []) if isinstance(cand, dict)]
    used: list[dict] = []
    windows: list[dict] = []
    scopes_meta: list[dict] = []
    old_to_new: dict[str, str] = {}
    counter = 0
    raw_scopes = list(brief.get("scopes") or [])
    win_by_id: dict[str, dict] = {}
    leftover_ids: list[str] = []
    leftover_titles: list[str] = []
    from agent.plan_windows import _intent_query_paths

    origin_paths = _intent_query_paths(str(brief.get("query") or ""))

    def _match_candidate(raw: dict) -> dict | None:
        rid = str(raw.get("id") or "").strip()
        act = str(raw.get("action") or "").strip()
        if rid:
            for cand in candidates:
                if cand not in used and str(cand.get("id") or "") == rid:
                    return cand
        if act:
            same = [
                cand for cand in candidates
                if cand not in used and str(cand.get("action") or "") == act
            ]
            if len(same) == 1:
                return same[0]
        return None

    # 第一遍：定下每扇的新 id 和它引用的候选，把新旧 id 对照表先填全。
    # 候选可能排到最后才被补跑，提前换算会把还没进表的候选 id 错当成新 id，撞出自环。
    plan: list[tuple[int, dict | None, dict | None, str]] = []
    scope_wids: dict[int, list[str]] = {}
    for si, scope in enumerate(raw_scopes):
        for raw in scope.get("windows") or []:
            counter += 1
            wid = f"W{counter}"
            if raw.get("id"):
                old_to_new[str(raw["id"])] = wid
            cand = _match_candidate(raw)
            if cand is not None:
                used.append(cand)
            plan.append((si, raw, cand, wid))
    for cand in candidates:
        if cand in used:
            continue
        counter += 1
        wid = f"W{counter}"
        old_id = str(cand.get("id") or "")
        if old_id:
            old_to_new[old_id] = wid
        plan.append((-1, None, cand, wid))

    # 第二遍：物化窗口。依赖用完整对照表重映射，且不允许指向自己。
    for si, raw, cand, wid in plan:
        deps: list[str] = []
        for dep in ((raw or {}).get("depends_on") or []):
            mapped = old_to_new.get(str(dep), str(dep))
            if mapped != wid and mapped not in deps:
                deps.append(mapped)
        for dep in ((cand or {}).get("depends_on") or []):
            mapped = old_to_new.get(str(dep), str(dep))
            if mapped != wid and mapped not in deps:
                deps.append(mapped)
        if cand is not None:
            window = dict(cand)
            window["id"] = wid
            if raw and raw.get("title"):
                window["title"] = str(raw["title"])
            window["depends_on"] = deps
        else:
            made = materialize_goal(
                raw["action"],
                raw.get("slots") or {},
                title=raw.get("title"),
                wid=wid,
                depends=deps,
                retries=int(raw.get("max_retries") or 1),
                paths=origin_paths,
            )
            window = made[0]
            for extra_index, extra in enumerate(made[1:], 1):
                # 归地插出的 create 前置扇：id 加 c 后缀，依赖与本扇同源，本扇回头等它
                extra_id = f"{wid}c{extra_index}"
                extra["id"] = extra_id
                extra["depends_on"] = list(deps)
                merged = list(window.get("depends_on") or [])
                if extra_id not in merged:
                    merged.append(extra_id)
                window["depends_on"] = merged
                windows.append(extra)
                win_by_id[extra_id] = extra
        if si >= 0:
            scope = raw_scopes[si]
            blocked = bool(scope.get("blocked"))
            external = str(scope.get("external_dep") or "").strip()
            if blocked:
                window["realizable"] = False
                window["ok"] = False
                window["blocked"] = True
                window["error"] = f"范围 {scope['id']} 的交付清单为空，这一窗暂停。"
            elif external:
                window["waiting_external"] = True
                window["external_dep"] = external
            scope_wids.setdefault(si, []).append(wid)
        else:
            leftover_ids.append(wid)
            leftover_titles.append(str(cand.get("title") or cand.get("action") or ""))
        windows.append(window)
        win_by_id[wid] = window

    for si, scope in enumerate(raw_scopes):
        scopes_meta.append({
            "id": str(scope.get("id") or ""),
            "title": str(scope.get("title") or ""),
            "window_ids": list(scope_wids.get(si) or []),
            "external_dep": str(scope.get("external_dep") or "").strip(),
            "blocked": bool(scope.get("blocked")),
            "deliverables": list(scope.get("deliverables") or []),
            "pending_note": str(scope.get("pending_note") or ""),
        })

    # 范围级依赖：本范围第一扇接在依赖范围的最后一扇后面
    for si, scope in enumerate(raw_scopes):
        ids = scope_wids.get(si) or []
        if not ids:
            continue
        first = win_by_id.get(ids[0])
        if not first:
            continue
        for dep_scope in scope.get("depends_on") or []:
            last = None
            for other in scopes_meta:
                if other is scopes_meta[si]:
                    continue
                if other["id"] == str(dep_scope) and other.get("window_ids"):
                    last = other["window_ids"][-1]
            if last and last != first["id"]:
                merged = list(first.get("depends_on") or [])
                if last not in merged:
                    merged.append(last)
                first["depends_on"] = merged
    if leftover_ids:
        scopes_meta.append({
            "id": "EX",
            "title": "任务书未归类的窗口（快路径已拆好，照跑不丢）",
            "window_ids": leftover_ids,
            "external_dep": "",
            "blocked": False,
            "deliverables": leftover_titles,
            "pending_note": "生成器没有把这些窗口归进任何范围，按原候补跑。",
        })
    if brief.get("integration"):
        counter += 1
        wid = f"W{counter}"
        deps = [w["id"] for w in windows]
        window = materialize(
            INTEGRATE_ACTION,
            {},
            title=str(brief["integration"].get("title") or "累计集成、验证与交付"),
            wid=wid,
            depends=deps,
            retries=1,
        )
        windows.append(window)
        scopes_meta.append({
            "id": "INT",
            "title": str(brief["integration"].get("title") or "累计集成、验证与交付"),
            "window_ids": [wid],
            "external_dep": "",
            "blocked": False,
            "deliverables": list(brief["integration"].get("deliverables") or []),
            "pending_note": "",
            "kind": "integration",
        })
    try:
        from agent.harness import build_criteria

        origin = str(brief.get("query") or "")
        for meta in scopes_meta:
            scoped = [win_by_id[wid] for wid in (meta.get("window_ids") or []) if wid in win_by_id]
            meta["criteria"] = build_criteria(origin, scoped)
    except Exception:
        pass
    return windows, scopes_meta
