# pipelines/export_skill_cards.py
"""从 .codex/skills 与 .cursor/skills 的 SKILL.md 前 20 行抽出 name 和 description。

同名时 .codex 优先。

再补 triggers、tools、version、conflicts。正文不进卡片，也不进向量库。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import SKILL_CARDS_PATH, SKILL_DIRS

CARD_KEYS = ("name", "description", "path", "triggers", "tools", "version", "conflicts")
CONFLICT_KEYS = ("switch", "when", "action", "output")
CONFLICT_OUTPUT = "Plan/ 里的人工步骤"
SWITCHES = (
    "UE_ALLOW_BLUEPRINT",
    "UE_ALLOW_MATERIAL",
    "UE_ALLOW_UMG",
    "UE_ALLOW_ANIM",
    "UE_ALLOW_NIAGARA",
)
KNOWN_TOOLS = {
    "query_actor",
    "query_blueprint",
    "find_asset",
    "build_project",
    "run_automation_test",
    "get_build_error",
    "search_ue_project",
    "compile_project",
    "compile_blueprint",
    "edit_cpp",
    "bind_blueprint",
    "place_actor",
    "judge_pickup",
    "set_pickable",
    "move_actor",
}
VERSION = "1.0"


def _conflict(switch: str, action: str) -> dict:
    return {
        "switch": switch,
        "when": "false",
        "action": action,
        "output": CONFLICT_OUTPUT,
    }


# 正文仍在 SKILL.md。这里只登记已经存在的触发词、依赖工具和开关。
REGISTRY = {
    "ue-actor-skill": {
        "triggers": ["actor", "关卡", "移动", "坐标"],
        "tools": ["query_actor", "move_actor"],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-animation-skill": {
        "triggers": ["动画", "骨骼", "control rig", "skeletal"],
        "tools": [],
        "version": VERSION,
        "conflicts": [_conflict("UE_ALLOW_ANIM", "动画改图")],
    },
    "ue-asset-skill": {
        "triggers": ["资产", "资源"],
        "tools": ["find_asset"],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-blueprint-skill": {
        "triggers": ["蓝图", "blueprint"],
        "tools": ["query_blueprint", "compile_blueprint"],
        "version": VERSION,
        "conflicts": [_conflict("UE_ALLOW_BLUEPRINT", "蓝图改图")],
    },
    "ue-cpp-plan-skill": {
        "triggers": ["c++", "编译错误", "实施计划"],
        "tools": ["edit_cpp", "build_project"],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-debug-skill": {
        "triggers": ["报错", "失败", "不生效", "查不到"],
        "tools": ["get_build_error"],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-dsh-handoff-skill": {
        "triggers": ["dsh", "交接", "handoff", "unmet"],
        "tools": [],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-dsh-route-skill": {
        "triggers": ["dsh", "编辑器", "编译", "automation"],
        "tools": [],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-dsh-session-skill": {
        "triggers": ["dsh", "mcp__uerag", "会话开工", "verify"],
        "tools": ["search_ue_project", "query_actor"],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-dsh-verify-skill": {
        "triggers": ["dsh", "完成", "verify", "read_contract"],
        "tools": [],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-history-skill": {
        "triggers": ["写不成任务", "历史解决方案", "不猜"],
        "tools": ["search_ue_project"],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-low-token-skill": {
        "triggers": ["未知工具", "toolset"],
        "tools": [],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-manual-blueprint-skill": {
        "triggers": ["蓝图改图", "人工步骤"],
        "tools": [],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-material-skill": {
        "triggers": ["材质", "material"],
        "tools": [],
        "version": VERSION,
        "conflicts": [_conflict("UE_ALLOW_MATERIAL", "材质改图")],
    },
    "ue-niagara-skill": {
        "triggers": ["niagara", "粒子"],
        "tools": [],
        "version": VERSION,
        "conflicts": [_conflict("UE_ALLOW_NIAGARA", "Niagara 改图")],
    },
    "ue-object-skill": {
        "triggers": ["属性", "类查询"],
        "tools": ["query_actor"],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-pcg-skill": {
        "triggers": ["pcg"],
        "tools": [],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-programmatic-skill": {
        "triggers": ["批处理", "execute_tool_script"],
        "tools": [],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-rag-route-skill": {
        "triggers": ["扫描", "mcp"],
        "tools": [],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-sequencer-skill": {
        "triggers": ["sequencer", "过场"],
        "tools": [],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-static-mesh-skill": {
        "triggers": ["静态网格", "nanite", "static mesh"],
        "tools": ["find_asset"],
        "version": VERSION,
        "conflicts": [],
    },
    "ue-umg-skill": {
        "triggers": ["umg", "widget", "控件"],
        "tools": [],
        "version": VERSION,
        "conflicts": [_conflict("UE_ALLOW_UMG", "UMG 改图")],
    },
    "ue-verify-skill": {
        "triggers": ["验证", "编译"],
        "tools": ["query_actor", "run_automation_test"],
        "version": VERSION,
        "conflicts": [],
    },
}


def _front_matter(text: str) -> tuple[str, str] | None:
    lines = text.splitlines()[:20]
    if not lines or lines[0].strip() != "---":
        return None
    name = ""
    description = ""
    index = 1
    while index < len(lines):
        line = lines[index]
        if line.strip() == "---":
            break
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("'\"")
        elif line.startswith("description:"):
            rest = line.split(":", 1)[1].strip()
            if rest in {">", ">-", "|", "|-"}:
                parts = []
                index += 1
                while index < len(lines) and (lines[index].startswith(" ") or lines[index].startswith("\t")):
                    parts.append(lines[index].strip())
                    index += 1
                description = " ".join(parts)
                continue
            description = rest.strip("'\"")
        index += 1
    if not name and not description:
        return None
    return name, description


def build_cards() -> tuple[list[dict], list[str]]:
    cards = []
    errors = []
    seen = set()
    if not SKILL_DIRS:
        return [], ["找不到技能目录"]
    for skills_dir in SKILL_DIRS:
        cards_from_dir, dir_errors, seen = _cards_in(skills_dir, seen)
        cards.extend(cards_from_dir)
        errors.extend(dir_errors)
    return cards, errors


def _cards_in(skills_dir: Path, seen: set) -> tuple[list[dict], list[str], set]:
    cards = []
    errors = []
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        parsed = _front_matter(path.read_text(encoding="utf-8", errors="replace"))
        if not parsed:
            errors.append(f"{path.parent.name} 没有 front matter")
            continue
        name, description = parsed
        if name in seen:
            continue
        meta = REGISTRY.get(name)
        if not meta:
            errors.append(f"{name} 没有注册字段")
            continue
        cards.append({
            "name": name,
            "description": description,
            "path": str(path),
            "triggers": list(meta["triggers"]),
            "tools": list(meta["tools"]),
            "version": meta["version"],
            "conflicts": [dict(item) for item in meta["conflicts"]],
        })
        seen.add(name)
    return cards, errors, seen


def validate_skill_cards(cards: list) -> list[str]:
    errors = []
    if not isinstance(cards, list) or not cards:
        return ["技能卡片为空"]
    seen = set()
    blueprint_rule = False
    for card in cards:
        if not isinstance(card, dict):
            errors.append("卡片不是对象")
            continue
        name = str(card.get("name") or "")
        extra = set(card) - set(CARD_KEYS)
        if extra:
            errors.append(f"{name} 多了字段 {sorted(extra)}")
        missing = [key for key in CARD_KEYS if key not in card]
        if missing:
            errors.append(f"{name} 缺 {missing}")
            continue
        if name in seen:
            errors.append(f"重复 name {name}")
        seen.add(name)
        if not str(card.get("description") or "").strip():
            errors.append(f"{name} 没有 description")
        if "body" in card:
            errors.append(f"{name} 带了正文")
        triggers = card.get("triggers")
        if not isinstance(triggers, list) or not triggers or not all(isinstance(item, str) and item for item in triggers):
            errors.append(f"{name} triggers 非法")
        tools = card.get("tools")
        if not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
            errors.append(f"{name} tools 非法")
        else:
            unknown = [item for item in tools if item not in KNOWN_TOOLS]
            if unknown:
                errors.append(f"{name} 未知工具 {unknown}")
        if not str(card.get("version") or "").strip():
            errors.append(f"{name} 没有 version")
        conflicts = card.get("conflicts")
        if not isinstance(conflicts, list):
            errors.append(f"{name} conflicts 非法")
            continue
        for rule in conflicts:
            if not isinstance(rule, dict) or set(rule) != set(CONFLICT_KEYS):
                errors.append(f"{name} 冲突字段不对")
                continue
            if rule.get("switch") not in SWITCHES or rule.get("when") != "false":
                errors.append(f"{name} 冲突开关不对")
            if rule.get("output") != CONFLICT_OUTPUT:
                errors.append(f"{name} 冲突产出不对")
            if name == "ue-blueprint-skill" and rule.get("switch") == "UE_ALLOW_BLUEPRINT" and rule.get("action") == "蓝图改图":
                blueprint_rule = True
    if "ue-blueprint-skill" not in seen:
        errors.append("没有 ue-blueprint-skill")
    elif not blueprint_rule:
        errors.append("蓝图改图没有写上 UE_ALLOW_BLUEPRINT=false 的冲突")
    return errors


def main() -> None:
    cards, errors = build_cards()
    errors.extend(validate_skill_cards(cards))
    if errors:
        for item in errors:
            print(item)
        raise SystemExit(1)
    SKILL_CARDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKILL_CARDS_PATH.write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(cards)} skill cards to {SKILL_CARDS_PATH}")


if __name__ == "__main__":
    main()
