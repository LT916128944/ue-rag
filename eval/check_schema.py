# eval/check_schema.py
"""契约 schema 检查。读 contract/tools.json；末尾带三个纯函数断言里的前两个（circuit 那条在 check_risk.py）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SEE = str(ROOT / "contract" / "tools.json")
TOOLS_PATH = ROOT / "contract" / "tools.json"

REQUIRED_KEYS = ("name", "summary", "permission", "timeout")
VALID_TIMEOUTS = (60, 300)

# 字符串字段只补 maxLength 和 description，不加 pattern。
FIELD_RULES = {
    ("query_actor", "name"): (200, "Actor 名字"),
    ("query_blueprint", "ref_path"): (300, "/Game 路径"),
    ("find_asset", "name"): (200, "资产名"),
    ("run_automation_test", "filter"): (200, "Automation 过滤器"),
}


def fail(msg: str, fix: str) -> None:
    print(f"❌ {msg}")
    print(f"✅ FIX: {fix}")
    print(f"📖 See: {SEE}")
    sys.exit(1)


def main() -> int:
    try:
        data = json.loads(TOOLS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"contract/tools.json 读不出来：{exc}", "contract/tools.json 修回合法 JSON")
    tools = data.get("tools") if isinstance(data, dict) else None
    if not isinstance(tools, list) or not tools:
        fail("contract/tools.json 没有 tools 数组", "contract/tools.json 的 tools 数组补回六个工具")

    for tool in tools:
        name = str(tool.get("name") or "?")
        for key in REQUIRED_KEYS:
            if not tool.get(key):
                fail(f"工具 {name} 缺 {key}", f"contract/tools.json 里 {name} 补 {key}")
        summary = str(tool.get("summary") or "")
        if "适用" not in summary or "不适用" not in summary:
            fail(f"工具 {name} 的 summary 要同时含「适用」和「不适用」", f"contract/tools.json 里 {name} 的 summary 补适用与不适用")
        if tool.get("timeout") not in VALID_TIMEOUTS:
            fail(f"工具 {name} 的 timeout={tool.get('timeout')}，只允许 60 或 300", f"contract/tools.json 里 {name} 的 timeout 改成 60 或 300")
        if not isinstance(tool.get("retryable"), bool):
            fail(f"工具 {name} 缺 retryable 布尔", f"contract/tools.json 里 {name} 挂上 retryable：参数错误为 false，超时和连接失败为 true")
        output = tool.get("output_schema") or {}
        if "ok" not in (output.get("required") or []):
            fail(f"工具 {name} 的 output_schema.required 缺 ok", f"contract/tools.json 里 {name} 的 output_schema.required 补 ok")
        error = tool.get("error_schema") or {}
        for field in ("ok", "error"):
            if field not in (error.get("properties") or {}) or field not in (error.get("required") or []):
                fail(f"工具 {name} 的 error_schema 缺 {field}", f"contract/tools.json 里 {name} 的 error_schema 补 {field}（properties 与 required）")

    for (tool_name, field), (max_length, keyword) in FIELD_RULES.items():
        tool = next((item for item in tools if item.get("name") == tool_name), None)
        if tool is None:
            fail(f"契约里没有 {tool_name}", f"contract/tools.json 补回 {tool_name}，不改工具名")
        prop = ((tool.get("input_schema") or {}).get("properties") or {}).get(field)
        if not isinstance(prop, dict):
            fail(f"{tool_name}.{field} 缺字符串字段", f"contract/tools.json 里 {tool_name} 的 {field} 补回 string 字段")
        if prop.get("maxLength") != max_length:
            fail(f"{tool_name}.{field} 的 maxLength={prop.get('maxLength')}，应为 {max_length}", f"contract/tools.json 里 {tool_name}.{field} 的 maxLength 改成 {max_length}")
        if keyword not in str(prop.get("description") or ""):
            fail(f"{tool_name}.{field} 的 description 缺「{keyword}」", f"contract/tools.json 里 {tool_name}.{field} 的 description 补「{keyword}」")
        if "pattern" in prop:
            fail(f"{tool_name}.{field} 出现 pattern", f"contract/tools.json 里 {tool_name}.{field} 删掉 pattern，只留 maxLength 和 description")

    for tool in tools:
        for field, prop in ((tool.get("input_schema") or {}).get("properties") or {}).items():
            if isinstance(prop, dict) and "pattern" in prop:
                fail(f"工具 {tool.get('name')} 的 {field} 出现 pattern", f"contract/tools.json 里 {tool.get('name')}.{field} 删掉 pattern，只留 maxLength 和 description")

    # 纯函数断言一：参数类错误不重试。二：mcp_down 可重试，最多 1 次。
    # （第 5 次失败后 circuit 拒绝在 check_risk.py；阈值只在 core/fault.py。）
    from core.fault import max_retries, retryable

    if retryable("tool_refused") is not False:
        fail("参数类错误（tool_refused）应是不可重试", "core/fault.py 的 RETRYABLE 把 tool_refused 改回 False")
    if retryable("mcp_down") is not True or max_retries("mcp_down") != 1:
        fail("mcp_down 应可重试且最多 1 次", "core/fault.py 的 RETRYABLE 把 mcp_down 改回 True；MAX_MCP_RETRIES 改回 1")

    print("schema ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
