# core/project_rules.py
"""项目规矩常量的唯一住处。agent.skill_prompt 从这里再导出，原调用方行为不变。"""
from __future__ import annotations

SKILL_DESCRIPTION = "本项目已经踩过的规矩：单位、编译目标、路径唯一、裁判。"
SKILL_FOLDER = "/Game/Skills"
SKILL_ASSET_NAME = "AIAgentProjectRules"
PROJECT_RULES = (
    "1 米 = 100 uu。\n"
    "编的是 AIAgentProjectEditor。\n"
    "路径不唯一就停。\n"
    "裁判不由模型宣布。"
)
