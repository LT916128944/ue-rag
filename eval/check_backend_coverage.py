# eval/check_backend_coverage.py
"""U3a（20261027 升级计划）：动作 × 后端覆盖矩阵。

20261027-180955：place_batch 等场景动作在 monolith 后端下直连 official 入口被拒
（错误原文 `UE_EDITOR_BACKEND=monolith`），四扇全败——10-26 切后端只迁了蓝图类
动作与 open_level / implement_shooter 等少数分支。守卫钉住两件事：

① agent/sequence.py 里每个调用 exec_tool 的函数：函数体里没有 monolith 分支的
  （函数源码不含 "monolith"），它用到的每个 official 原语必须在
  core/monolith_bridge.py 的 MAPPED（走桥翻译）或 UNSUPPORTED（带原因的结构化
  拒绝）里。两边都不是 ⇒ monolith 后端下这一扇是笼统拒绝 ⇒ 红。
② agent/catalog.py 的每个 CATALOG 动作都要在 COVERAGE 里有一行声明。
  新加动作没声明就红——加动作的人必须同时回答「另一条后端下它怎么跑」。

纯 AST + 纯函数，不连编辑器。"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SEQUENCE = ROOT / "agent" / "sequence.py"


def collect_tool_literals() -> dict[str, list[str]]:
    """函数名 → 该函数里 exec_tool 第二参的字面量列表（只收字符串常量）。"""
    tree = ast.parse(SEQUENCE.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        segment = ast.get_source_segment(SEQUENCE.read_text(encoding="utf-8"), node) or ""
        tools: list[str] = []
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            name = getattr(func, "id", "") or getattr(func, "attr", "")
            if name != "exec_tool" or len(sub.args) < 2:
                continue
            arg = sub.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                tools.append(arg.value)
        if tools:
            out[node.name] = {"tools": sorted(set(tools)), "monolith": "monolith" in segment}
    return {name: row["tools"] for name, row in out.items() if not row["monolith"]}


def check_bridge_covers_sequence_tools() -> None:
    """① 没有 monolith 分支的函数，用到的原语必须已映射或已声明不支持。"""
    from core.monolith_bridge import MAPPED, UNSUPPORTED

    per_func = collect_tool_literals()
    assert per_func, "AST 没找到任何 exec_tool 调用，检查本身失效"
    bad: list[str] = []
    for func, tools in sorted(per_func.items()):
        for tool in tools:
            if tool in MAPPED or tool in UNSUPPORTED:
                continue
            bad.append(f"{func} 用了未映射原语 {tool}")
    assert not bad, "monolith 覆盖缺口：\n  " + "\n  ".join(bad)


# ② 动作 × 后端覆盖声明。both：两条后端都有可执行路径；
# official_only：monolith 下没有一等路径，reason 必须写实。
COVERAGE = {
    "launch_editor": "both",            # 进程 + MCP 端口探活，不分后端
    "compile_project": "both",          # Build.bat，不经 MCP
    "edit_cpp": "both",                 # Build.bat，不经 MCP
    "compile_blueprint": "both",        # sequence 有 monolith 分支（repair 路径）
    "bind_blueprint": "both",           # bind_slot 纯解析 + _bind_evidence 的 find_assets 已桥
    "create_level": "both",             # _create_level_monolith
    "open_level": "both",               # sequence 有 monolith 分支
    "place_actor": "both",              # find_actors / add_to_scene_from_asset / save 已桥
    "place_batch": "both",              # 已桥；props 在生成时写（U3a）
    "remove_batch": "both",             # find_actors / remove_from_scene 已桥
    "judge_pickup": "partial:monolith 读不到实例属性（get_properties 不支持），裁决如实失败",
    "set_pickable": "partial:标签可写（add_tag 已桥），bPickable 写不上（set_properties 不支持）",
    "move_actor": "both",               # set_actor_transform / get_actor_transform 已桥
    "query_actor": "both",              # find_actors / get_actor_transform 已桥
    "set_scalar_param": "both",         # get/set_scalar_parameter 已桥到 material 命名空间
    "query_material": "both",           # get_scalar_parameter 已桥
    "create_widget_blueprint": "both",  # editor_exec 的 _monolith_widget_create 走 ui.create_widget_blueprint；set_parent/save_assets 已桥
    "edit_widget_blueprint": "both",    # 控件树原语经桥走 ui.*（20261029）；事件图绑定仍无 monolith 路径，见 UNSUPPORTED
    "run_automation_test": "both",      # judge._monolith_automation
    "implement_shooter": "both",        # sequence 有 monolith 分支（20261027-150134 实跑）
    "implement_grenade": "both",        # _implement_grenade 走 monolith 原语（load/stats/author/save），official 走同路径
    "check_platform_hud": "both",       # GetWidgets/get_parent 已桥到 ui.get_widget_tree / blueprint.get_parent_class
    "check_fps_hud": "both",            # GetWidgets/get_parent 已桥；挂视口入口读源码，不进编辑器
    "integrate_verify": "both",         # 集成矩阵纯代码，不进编辑器
    "dispose_residual": "both",         # 处置扇走既有路径，不新增后端差异
}


def check_catalog_rows() -> None:
    """② 每个 CATALOG 动作一行声明；值只能是 both / partial:原因 / official_only:原因。"""
    from agent.catalog import CATALOG

    missing = [name for name in CATALOG if name not in COVERAGE]
    assert not missing, f"COVERAGE 缺行：{missing}——新动作必须同时声明另一条后端怎么跑"
    stale = [name for name in COVERAGE if name not in CATALOG]
    assert not stale, f"COVERAGE 有 CATALOG 里不存在的行：{stale}"
    for name, row in COVERAGE.items():
        assert row == "both" or row.split(":", 1)[0] in ("partial", "official_only"), (
            f"{name} 的覆盖声明形状不对：{row!r}")
        if row != "both":
            assert len(row.split(":", 1)) == 2 and row.split(":", 1)[1].strip(), (
                f"{name} 的非 both 声明必须带原因：{row!r}")


def check_unsupported_have_reason() -> None:
    """UNSUPPORTED 的每条拒绝都要带原因，扇级错误必须可读。"""
    from core.monolith_bridge import MAPPED, UNSUPPORTED

    for tool, reason in UNSUPPORTED.items():
        assert reason.strip(), f"{tool} 的不支持声明没有原因"
    overlap = set(MAPPED) & set(UNSUPPORTED)
    assert not overlap, f"原语同时出现在 MAPPED 和 UNSUPPORTED：{overlap}"


def main() -> None:
    check_bridge_covers_sequence_tools()
    check_catalog_rows()
    check_unsupported_have_reason()
    print("backend coverage ok")


if __name__ == "__main__":
    main()
