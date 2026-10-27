# agent/scene_gate.py
"""场景内容闸门：句子里点名要摆进关卡的东西，不能因为理解器把它写进 unresolved 就静默消失。

20261021-221446 的教训：原句要「金币、平台、门」进关卡，理解器把「金币、门、平台的具体蓝图
资产名与摆放位置」整段扔进 unresolved，代码只把它记进交接文本，窗口清单里一扇放置都没有；
UI 编译 + 内存里的 Automation 全绿，于是 6/7 扇记成成功，关卡里其实一枚金币都没放。

这里的口径只有一条：**设计图目录里存在的设计图 + 原句点名了它 + 句子里有关卡/场景语义**，
就由代码补一扇 place_batch，并补一条 editor_assert 条款。设计图是唯一权威，坐标不猜。
纯函数，不调模型，不动编辑器。
"""
import json
import re
from pathlib import Path

# 关键词 → 设计图里的 blueprint 名。只认设计图里真有的那几个，句子说了别的也补不出来。
_KEYWORDS = {
    "BP_Coin": ("金币", "coin"),
    "BP_Platform": ("平台", "platform"),
    "BP_Door": ("门", "door"),
    "BP_Progress": ("进度", "中枢", "progress"),
}

# 句子里不点名、但没有它整关跑不起来的实体：金币靠它注册总数，门靠它收齐开门。
_IMPLIED = ("BP_Progress",)

# 固定补窗顺序：先进度中枢（金币一进关就注册），再金币、平台、门
_ORDER = ("BP_Progress", "BP_Coin", "BP_Platform", "BP_Door")

# 只有句子里出现关卡/场景语义时才算「要摆进场景」。只调材质、只改图的句子不该被补放置扇。
_SCENE_INTENT = re.compile(
    r"关卡|场景|放进|放到|放入|摆放|摆|布置|组装|搭建|做一个|做个|搭个|能玩|可玩",
    re.I,
)

_PLACE_ACTIONS = ("place_batch", "place_actor")


def layout_dir() -> Path:
    from config import DATA_DIR

    return DATA_DIR / "harness" / "level_layout"


def _stem(blueprint: str) -> str:
    """`/Game/BP_Coin`、`/Game/BP_Coin.BP_Coin`、`BP_Coin` 统一成 `BP_Coin`。"""
    text = str(blueprint or "").strip().replace("\\", "/").rstrip("/")
    if not text:
        return ""
    leaf = text.rsplit("/", 1)[-1]
    return leaf.split(".", 1)[0]


def layouts() -> list[dict]:
    """读设计图目录：每条 `{"blueprint", "file", "stem", "names"}`。读不动就跳过。"""
    folder = layout_dir()
    if not folder.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        blueprint = _stem(data.get("blueprint") or "")
        if not blueprint:
            continue
        names = [
            str(row.get("name") or "").strip()
            for row in (data.get("actors") or [])
            if isinstance(row, dict) and str(row.get("name") or "").strip()
        ]
        out.append({"blueprint": blueprint, "file": path.name, "stem": path.stem, "names": names})
    return out


def _names_of(query: str) -> set[str]:
    """句子里点名到的实体（按关键词）。"""
    lowered = str(query or "").lower()
    return {
        name for name, words in _KEYWORDS.items()
        if any(word.lower() in lowered for word in words)
    }


def demanded(query: str) -> list[dict]:
    """句子点名了、且设计图里有的设计图条目。没有关卡语义时返回空。
    `_IMPLIED` 里的实体只要这句是关卡句就一起带上（句子里不会点名进度中枢）。"""
    text = str(query or "")
    if not text.strip() or not _SCENE_INTENT.search(text):
        return []
    named = _names_of(text)
    if not named:
        return []
    wanted = named | set(_IMPLIED)
    found = [item for item in layouts() if item["blueprint"] in wanted]
    rank = {name: index for index, name in enumerate(_ORDER)}
    found.sort(key=lambda item: (rank.get(item["blueprint"], len(rank)), item["file"]))
    return found


def missing_layout(query: str) -> list[str]:
    """句子点名了、设计图目录里却没有的实体。补不出窗口时由调用方记进未完成。"""
    text = str(query or "")
    if not text.strip() or not _SCENE_INTENT.search(text):
        return []
    present = {item["blueprint"] for item in layouts()}
    return [name for name in _ORDER if name in _names_of(text) and name not in present]


def _covered(windows: list) -> tuple[set[str], set[str]]:
    """已接住的：place_batch 按设计图文件 stem 算（同一蓝图可以有多张设计图），
    place_actor 按蓝图名算（它没有设计图）。"""
    files: set[str] = set()
    blueprints: set[str] = set()
    for window in windows or []:
        if not isinstance(window, dict):
            continue
        action = str(window.get("action") or "")
        if action not in _PLACE_ACTIONS:
            continue
        slots = window.get("slots") if isinstance(window.get("slots"), dict) else {}
        layout = str(slots.get("layout") or "").strip()
        if layout:
            name = Path(layout).name
            files.add(name[:-5] if name.lower().endswith(".json") else name)
        if action == "place_actor":
            for key in ("blueprint", "class"):
                stem = _stem(slots.get(key) or "")
                if stem:
                    blueprints.add(stem)
    return files, blueprints


def _next_id(windows: list) -> str:
    """下一个窗口 id：现有 W 编号最大值 +1（不用 len()+1，中间被拿掉的扇会让编号撞上）。"""
    highest = 0
    for window in windows or []:
        if not isinstance(window, dict):
            continue
        found = re.fullmatch(r"W(\d+)", str(window.get("id") or "").strip())
        if found:
            highest = max(highest, int(found.group(1)))
    return f"W{highest + 1}"


def _shown(names: list[str]) -> str:
    return "、".join(names[:6]) + ("…" if len(names) > 6 else "")


def _place_window(item: dict, wid: str, depends: list[str]) -> dict:
    from config import task_policy

    return {
        "id": wid,
        "title": f"按设计图放置 {item['blueprint']}（{item['stem']}）",
        "action": "place_batch",
        "slots": {"blueprint": item["blueprint"], "layout": item["file"]},
        "depends_on": [dep for dep in depends if dep],
        "max_retries": 1,
        "realizable": True,
        "ok": True,
        "blocked": False,
        **task_policy(),
        "scene_gate": {"layout": item["file"], "names": list(item["names"])},
    }


def _criterion(item: dict, wids: list[str]) -> dict:
    row = {
        "id": f"c-scene-{item['stem']}",
        "text": f"关卡里有 {_shown(item['names'])}，各一个，位置与设计图 {item['file']} 一致",
        "evidence_kind": "editor_assert",
    }
    if wids:
        # 显式绑到这一扇放置，别让别的 editor_assert 扇（核对 HUD 之类）顺手把它勾上
        row["windows"] = list(wids)
    return row


def ensure_scene_windows(query: str, windows: list | None, criteria: list | None = None,
                         unresolved: list | None = None) -> dict:
    """缺哪张设计图就补一扇 place_batch + 一条 editor_assert 条款。

    补窗位置固定在 PIE 测试扇之前（先摆好再玩）；已被放置扇接住的设计图不重复补。
    返回 `{"windows", "criteria", "added", "unmet", "unresolved"}`：`added` 是补上的设计图文件名，
    `unmet` 是点名了但设计图缺失的半句，`unresolved` 是拿掉已接住项后的剩余半句。
    """
    current = [dict(window) for window in (windows or []) if isinstance(window, dict)]
    rows = [dict(item) for item in (criteria or []) if isinstance(item, dict)]
    wanted = demanded(query)
    unmet = [f"{name} 没有设计图，补不出放置扇" for name in missing_layout(query)]
    if not wanted:
        kept = [str(item) for item in (unresolved or [])]
        return {"windows": current, "criteria": rows, "added": [], "unmet": unmet,
                "unresolved": kept}

    files, blueprints = _covered(current)
    todo = [
        item for item in wanted
        if item["stem"] not in files and item["blueprint"] not in blueprints
    ]
    kept, _dropped = prune_unresolved(unresolved, wanted)
    if not todo:
        return {"windows": current, "criteria": rows, "added": [], "unmet": unmet,
                "unresolved": kept}

    insert_at = len(current)
    for index, window in enumerate(current):
        if str(window.get("action") or "") == "probe_play":
            insert_at = index
            break
    previous = str(current[insert_at - 1].get("id") or "") if insert_at > 0 else ""

    added: list[dict] = []
    for item in todo:
        window = _place_window(item, _next_id(current + added), [previous])
        added.append(window)
        previous = str(window["id"])

    current[insert_at:insert_at] = added
    if insert_at < len(current) - len(added):
        # 插在已有的 PIE 扇前面：让它等新加的放置扇
        tail = current[insert_at + len(added)]
        deps = [str(dep) for dep in (tail.get("depends_on") or []) if str(dep)]
        if previous and previous not in deps:
            deps.append(previous)
        tail["depends_on"] = deps

    seen = {str(item.get("id") or "") for item in rows}
    for item, window in zip(todo, added):
        row = _criterion(item, [str(window.get("id") or "")])
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        rows.append(row)

    return {
        "windows": current,
        "criteria": rows,
        "added": [item["file"] for item in todo],
        "unmet": unmet,
        "unresolved": kept,
    }


def scene_summary(query: str) -> dict:
    """只读：这句要哪些实体、设计图里有没有。给检查脚本和记录用。"""
    wanted = demanded(query)
    return {
        "demanded": [item["blueprint"] for item in wanted],
        "layouts": [item["file"] for item in wanted],
        "missing_layout": missing_layout(query),
    }


# 这些词出现，说明这条 unresolved 讲的是「摆进场景 / 关卡布局」——闸门补了放置扇就已经接住
_PLACEMENT_WORDS = ("摆放", "放置", "位置", "布局", "关卡", "布点", "点位", "装配")

# 这些词出现，说明这条 unresolved 还带着闸门接不住的东西（参数、表现、手感），一律留下
_KEEP_WORDS = ("参数", "表现", "视觉", "发光", "音效", "动画", "手感", "难度", "节奏", "数值")


def prune_unresolved(unresolved: list | None, items: list | None) -> tuple[list[str], list[str]]:
    """闸门补了放置扇之后，把**纯摆放**的 unresolved 拿掉。
    带参数 / 表现 / 手感说法的半句一律留下——闸门只接了「摆进去」，没接「调好」。
    返回 (kept, dropped)。"""
    covered = list(items or [])
    if not covered:
        return [str(item) for item in (unresolved or [])], []
    words: set[str] = set()
    for item in covered:
        words.update(_KEYWORDS.get(str(item.get("blueprint") or ""), ()))
    kept: list[str] = []
    dropped: list[str] = []
    for raw in unresolved or []:
        text = str(raw or "")
        lowered = text.lower()
        hit_word = any(word.lower() in lowered for word in words)
        hit_place = any(word in text for word in _PLACEMENT_WORDS)
        hit_keep = any(word in text for word in _KEEP_WORDS)
        if text.strip() and hit_word and hit_place and not hit_keep:
            dropped.append(text)
        else:
            kept.append(text)
    return kept, dropped
