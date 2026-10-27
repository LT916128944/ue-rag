# scripts/call_mcp_tool.py
# 直连 UE MCP HTTP，调用一个已经选定的工具。不加载工具目录，不注入错误，不等待确认。
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.editor_exec import exec_tool
from core.ue_mcp_client import get_ue_mcp_client
from config import default_pie, default_save

CLIP = 200


def _clip(text: str, limit: int = CLIP) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def main() -> int:
    parser = argparse.ArgumentParser(description="Call one UE MCP tool by toolset + short name.")
    parser.add_argument("--toolset", default="", help="Toolset full name. Empty for a top-level tool.")
    parser.add_argument("--tool", required=True, help="Short tool name")
    parser.add_argument("--args", default="{}", help="JSON object of arguments")
    parser.add_argument("--save", dest="save", action="store_true", help="Allow save_assets / save_actor for this call (default: UE_DEFAULT_SAVE).")
    parser.add_argument("--no-save", dest="save", action="store_false", help="Refuse save_assets / save_actor for this call.")
    parser.add_argument("--pie", dest="pie", action="store_true", help="Allow StartPIE / StopPIE for this call (default: UE_DEFAULT_PIE).")
    parser.add_argument("--no-pie", dest="pie", action="store_false", help="Refuse StartPIE / StopPIE for this call.")
    parser.set_defaults(save=None, pie=None)
    ns = parser.parse_args()

    try:
        arguments = json.loads(ns.args) if ns.args else {}
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"bad --args json: {exc}"}, ensure_ascii=False))
        return 1
    if not isinstance(arguments, dict):
        print(json.dumps({"ok": False, "error": "--args must be a JSON object"}, ensure_ascii=False))
        return 1

    from backends.official import official_block

    blocked = official_block()
    if blocked:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps({
            "ok": False,
            "error": blocked,
            "toolset": ns.toolset,
            "tool": ns.tool,
        }, ensure_ascii=False))
        return 1

    task = {
        "allow_writes": [ns.tool],
        "save": default_save() if ns.save is None else bool(ns.save),
        "pie": default_pie() if ns.pie is None else bool(ns.pie),
    }
    try:
        result = exec_tool(ns.toolset, ns.tool, arguments, task=task)
    finally:
        get_ue_mcp_client().close()

    data = result.get("data", result.get("error", ""))
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False)
    payload = {
        "ok": bool(result.get("ok")),
        "toolset": ns.toolset,
        "tool": ns.tool,
        "data": _clip(data),
    }
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
