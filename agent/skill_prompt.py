# agent/skill_prompt.py
"""引擎 AgentSkill 的生成结果。plan 只在选出域时附上，并按现有 1200 字裁。"""
import json

from config import AGENT_SKILL_PATH

# 与 matching_solution 附在 plan 上的那一截同一上限。
CARD_BUDGET = 1200

SKILL_DESCRIPTION = "本项目已经踩过的规矩：单位、编译目标、路径唯一、裁判。"
SKILL_FOLDER = "/Game/Skills"
SKILL_ASSET_NAME = "AIAgentProjectRules"
PROJECT_RULES = (
    "1 米 = 100 uu。\n"
    "编的是 AIAgentProjectEditor。\n"
    "路径不唯一就停。\n"
    "裁判不由模型宣布。"
)


def skill_card_line(card: dict) -> str:
    """plan 只带一张卡片的说明，不带触发、工具、版本和冲突。"""
    return f"相关 Skill：{card.get('name', '')}：{card.get('description', '')}"


def project_rules_suffix(scoped: bool, prompt: str) -> str:
    if not scoped:
        return ""
    text = (prompt or "").strip()
    if not text:
        return ""
    return "\n项目规矩：" + text[:CARD_BUDGET]


def load_agent_skill_prompt() -> str:
    if not AGENT_SKILL_PATH.is_file():
        return ""
    try:
        data = json.loads(AGENT_SKILL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if isinstance(data, dict):
        return str(data.get("prompt") or "")
    return ""
