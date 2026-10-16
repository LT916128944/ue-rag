# eval/call_contract.py
"""用外部 MCP 客户端调用窄 server 一次。只调 query_actor，不编译，不 PIE。"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from agent.catalog import CATALOG
from core.contract_tools import tool_names

REPORTS = Path(__file__).resolve().parent / "reports"
ACTOR = "PlayerStart"


def _check_contract() -> None:
    names = tool_names()
    expected = [
        "query_actor",
        "query_blueprint",
        "find_asset",
        "build_project",
        "run_automation_test",
        "get_build_error",
    ]
    if names != expected:
        raise SystemExit(f"contract names {names}")
    overlap = sorted(set(names) & set(CATALOG))
    if overlap:
        raise SystemExit(f"catalog overlap {overlap}")


def _text(result) -> str:
    structured = getattr(result, "structuredContent", None)
    if structured:
        return json.dumps(structured, ensure_ascii=False)
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", "")
        if text:
            parts.append(text)
    return "\n".join(parts)


async def _call() -> tuple[list[str], dict]:
    params = StdioServerParameters(
        command=sys.executable,
        args=["server/contract_server.py"],
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = [tool.name for tool in listed.tools]
            result = await session.call_tool("query_actor", {"name": ACTOR}, read_timeout_seconds=90)
    payload = json.loads(_text(result))
    return names, payload


def main() -> int:
    _check_contract()
    names, payload = asyncio.run(_call())
    expected = tool_names()
    if names != expected:
        print(json.dumps({"ok": False, "listed": names}, ensure_ascii=False))
        return 1
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORTS / f"{stamp}-contract-call.md"
    REPORTS.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {stamp} 契约调用",
        "",
        "客户端是独立进程，stdio 连接 `server/contract_server.py`。`tools/list` 只有契约里的六个名字。",
        "",
        "工具：" + "、".join(names),
        "",
        f"`query_actor` 名字 `{ACTOR}`：`ok={payload.get('ok')}`，`ref_path={payload.get('ref_path', '')}`，"
        f"location={payload.get('location', payload.get('error', ''))}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(path)
    print(json.dumps({"ok": payload.get("ok"), "ref_path": payload.get("ref_path"), "location": payload.get("location"), "error": payload.get("error")}, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
