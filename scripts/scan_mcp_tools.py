# 按自然语言从 ue_tools 取出少量工具卡片。默认不打印 input_schema。
import argparse
import json
import os
import sys

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import MCP_INDEX_PATH

DESC_LIMIT = 160


def _split_full_name(full_name: str) -> tuple[str, str]:
    if "." not in full_name:
        return "", full_name
    toolset, tool = full_name.rsplit(".", 1)
    return toolset, tool


def _clip(text: str, limit: int = DESC_LIMIT) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _emit(payload: dict) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


def _lookup(full_name: str) -> dict | None:
    toolset, tool = _split_full_name(full_name)
    with open(MCP_INDEX_PATH, "r", encoding="utf-8") as f:
        index = json.load(f)
    try:
        return index["toolsets"][toolset]["tools"][tool]
    except KeyError:
        return None


def print_schema(full_name: str) -> int:
    data = _lookup(full_name)
    if data is None:
        _emit({"ok": False, "error": "unknown tool", "full_name": full_name})
        return 1
    schema = data.get("input_schema") or {"type": "object"}
    required = data.get("required") or schema.get("required") or []
    _emit({
        "ok": True,
        "full_name": full_name,
        "required": required,
        "input_schema": schema,
    })
    return 0


def print_cards(query: str, top_k: int) -> int:
    from core.tool_router import get_full_name, retrieve_tools, split_full_name

    tools = retrieve_tools(query, top_k=top_k)
    cards = []
    for item in tools:
        fn = item.get("function") or {}
        params = fn.get("parameters") or {}
        if not isinstance(params, dict):
            params = {}
        full_name = get_full_name(fn.get("name", "")) or fn.get("name", "")
        toolset, tool_name = split_full_name(full_name)
        cards.append({
            "full_name": full_name,
            "toolset": toolset,
            "tool_name": tool_name,
            "required": params.get("required") or [],
            "description": _clip(fn.get("description") or ""),
        })
    _emit({"ok": True, "count": len(cards), "cards": cards})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan UE MCP tools via RAG. Cards only unless --schema.")
    parser.add_argument("--query", default="", help="Natural language editor request")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--schema", default="", help="Print input_schema for this full_name only")
    args = parser.parse_args()

    if args.schema:
        return print_schema(args.schema)
    if not args.query.strip():
        _emit({"ok": False, "error": "pass --query or --schema"})
        return 1
    return print_cards(args.query.strip(), max(1, args.top_k))


if __name__ == "__main__":
    raise SystemExit(main())
