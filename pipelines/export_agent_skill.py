# pipelines/export_agent_skill.py
"""在编辑器里放一条 AgentSkill，并把 GeneratePrompt 的结果写入 data/agent_skill.json。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.project_rules import PROJECT_RULES, SKILL_ASSET_NAME, SKILL_DESCRIPTION, SKILL_FOLDER
from config import AGENT_SKILL_PATH
from core.editor_exec import exec_tool
from core.ue_mcp_client import get_ue_mcp_client

SKILL_TOOLSET = "ToolsetRegistry.AgentSkillToolset"
ASSET_TOOLSET = "editor_toolset.toolsets.asset.AssetTools"
PACKAGE = f"{SKILL_FOLDER}/{SKILL_ASSET_NAME}"


def _task(save: bool = False) -> dict:
    return {"allow_writes": ["CreateSkill", "UpdateSkill", "save_assets"], "save": save, "pie": False}


def _call(toolset: str, tool: str, args: dict, *, save: bool = False) -> dict:
    return exec_tool(toolset, tool, args, task=_task(save))


def _parse(data):
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return ""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return data


def _path_from(data) -> str:
    parsed = _parse(data)
    if isinstance(parsed, str) and parsed.startswith("/"):
        return parsed.split()[0]
    if isinstance(parsed, dict):
        for key in ("returnValue", "path", "skillPath"):
            if key in parsed:
                found = _path_from(parsed[key])
                if found:
                    return found
    return ""


def _listing(data) -> dict:
    parsed = _parse(data)
    if isinstance(parsed, dict) and "returnValue" in parsed:
        parsed = _parse(parsed["returnValue"])
    return parsed if isinstance(parsed, dict) else {}


def _existing_path(listing: dict) -> str:
    for key in listing:
        if SKILL_ASSET_NAME in str(key):
            return str(key)
    return ""


def _prompt_from(data) -> tuple[str, str]:
    parsed = _listing(data)
    for key, value in parsed.items():
        if not str(key).startswith("/"):
            continue
        body = _parse(value)
        if isinstance(body, str) and body.strip():
            return str(key), body.strip()
        if isinstance(body, dict):
            for field in ("instructions", "Instructions"):
                text = str(body.get(field) or "").strip()
                if text:
                    return str(key), text
    return "", ""


def main() -> None:
    try:
        listed = _call(SKILL_TOOLSET, "ListSkills", {})
        if not listed.get("ok"):
            print(listed.get("error") or "ListSkills 失败")
            raise SystemExit(1)
        skill_path = _existing_path(_listing(listed.get("data")))
        details = {"instructions": PROJECT_RULES}
        if skill_path:
            changed = _call(SKILL_TOOLSET, "UpdateSkill", {
                "skillPath": skill_path,
                "description": SKILL_DESCRIPTION,
                "details": details,
            })
        else:
            changed = _call(SKILL_TOOLSET, "CreateSkill", {
                "folderPath": SKILL_FOLDER,
                "assetName": SKILL_ASSET_NAME,
                "description": SKILL_DESCRIPTION,
                "details": details,
            })
            skill_path = _path_from(changed.get("data"))
        if not changed.get("ok"):
            print(changed.get("error") or "写入技能失败")
            raise SystemExit(1)
        if not skill_path.startswith("/"):
            print("没有技能路径")
            raise SystemExit(1)
        got = _call(SKILL_TOOLSET, "GetSkills", {"skillPaths": [skill_path]})
        if not got.get("ok"):
            print(got.get("error") or "GetSkills 失败")
            raise SystemExit(1)
        found, prompt = _prompt_from(got.get("data"))
        if not prompt:
            print("GetSkills 没有 instructions")
            raise SystemExit(1)
        saved = _call(ASSET_TOOLSET, "save_assets", {"asset_paths": [PACKAGE]}, save=True)
        if not saved.get("ok"):
            print(saved.get("error") or "save_assets 失败")
            raise SystemExit(1)
        payload = {
            "path": found or skill_path,
            "description": SKILL_DESCRIPTION,
            "prompt": prompt,
        }
        AGENT_SKILL_PATH.parent.mkdir(parents=True, exist_ok=True)
        AGENT_SKILL_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"path={payload['path']} prompt_chars={len(prompt)} saved={PACKAGE}")
    finally:
        get_ue_mcp_client().close()


if __name__ == "__main__":
    main()
