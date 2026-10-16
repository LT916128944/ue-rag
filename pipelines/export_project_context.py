# pipelines/export_project_context.py
"""写出 project_context.json。只有模块、开关、模型、源码根、当前关卡名。"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (
    PROJECT_CONTEXT_PATH,
    UE_LLM_MODEL,
    UE_SOURCE_ROOT,
    allow_anim,
    allow_blueprint,
    allow_material,
    allow_niagara,
    allow_umg,
)

CONTEXT_KEYS = ("module", "switches", "model", "source_root", "level_name")
SWITCH_NAMES = (
    "UE_ALLOW_BLUEPRINT",
    "UE_ALLOW_MATERIAL",
    "UE_ALLOW_UMG",
    "UE_ALLOW_ANIM",
    "UE_ALLOW_NIAGARA",
)


def current_switches() -> dict:
    values = {
        "UE_ALLOW_BLUEPRINT": allow_blueprint(),
        "UE_ALLOW_MATERIAL": allow_material(),
        "UE_ALLOW_UMG": allow_umg(),
        "UE_ALLOW_ANIM": allow_anim(),
        "UE_ALLOW_NIAGARA": allow_niagara(),
    }
    return {name: bool(values[name]) for name in SWITCH_NAMES}


def level_name_from_text(text: str) -> str:
    """从关卡工具的返回里取关卡名。不读 Actor 列表。"""
    raw = text or ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        value = data.get("returnValue", data.get("name", ""))
        if isinstance(value, dict):
            value = value.get("name") or value.get("path") or ""
        raw = str(value or "")
    match = re.search(r"\b(L_[A-Za-z0-9_]+)\b", raw)
    return match.group(1) if match else ""


def project_context_payload(level_name: str) -> dict:
    return {
        "module": UE_SOURCE_ROOT.name,
        "switches": current_switches(),
        "model": UE_LLM_MODEL,
        "source_root": str(UE_SOURCE_ROOT),
        "level_name": level_name,
    }


def validate_project_context(data: dict) -> list[str]:
    errors = []
    if not isinstance(data, dict) or set(data) != set(CONTEXT_KEYS):
        return ["project_context 字段不对"]
    if not str(data.get("module") or "").strip():
        errors.append("没有模块名")
    switches = data.get("switches")
    if not isinstance(switches, dict) or set(switches) != set(SWITCH_NAMES):
        errors.append("开关字段不对")
    elif not all(isinstance(value, bool) for value in switches.values()):
        errors.append("开关不是布尔")
    if not str(data.get("model") or "").strip():
        errors.append("没有模型名")
    if not str(data.get("source_root") or "").strip():
        errors.append("没有源码根")
    level_name = str(data.get("level_name") or "")
    if not level_name.startswith("L_"):
        errors.append("没有当前关卡名")
    return errors


def read_level_name() -> tuple[str, str]:
    from core.editor_exec import exec_tool

    result = exec_tool(
        "editor_toolset.toolsets.scene.SceneTools",
        "get_current_level",
        {},
        task={"allow_writes": [], "save": False, "pie": False},
    )
    if not result.get("ok"):
        return "", str(result.get("error") or "get_current_level 失败")
    data = result.get("data")
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return level_name_from_text(text), ""


def write_project_context(level_name: str) -> dict:
    payload = project_context_payload(level_name)
    errors = validate_project_context(payload)
    if errors:
        raise ValueError("; ".join(errors))
    PROJECT_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROJECT_CONTEXT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    level_name, error = read_level_name()
    if error or not level_name:
        print(error or "关卡名解析失败")
        raise SystemExit(1)
    payload = write_project_context(level_name)
    print(f"level={payload['level_name']} module={payload['module']} model={payload['model']}")


if __name__ == "__main__":
    main()
