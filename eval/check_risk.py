# eval/check_risk.py
"""风险矩阵与熔断的离线检查。不连编辑器：裁决走 risk.decide，拒绝走 exec_tool 的既有分支。
末尾带 context-switch-drift 只读断言。阈值只在 core/fault.py，这里不另写一套。"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SEE = str(ROOT / "data" / "harness" / "risk_matrix.json")

from core import fault, risk  # noqa: E402
from core.editor_exec import exec_tool  # noqa: E402

ASSET_TOOLSET = "editor_toolset.toolsets.asset.AssetTools"
BLUEPRINT_TOOLSET = "editor_toolset.toolsets.blueprint.BlueprintTools"


def fail(msg: str, fix: str) -> None:
    print(f"❌ {msg}")
    print(f"✅ FIX: {fix}")
    print(f"📖 See: {SEE}")
    sys.exit(1)


def check_write_refusals() -> None:
    """缺省任务下 save_assets / StartPIE 被拒，error 精确等于 write refused。"""
    for tool in ("save_assets", "StartPIE"):
        refused = exec_tool(ASSET_TOOLSET, tool, {}, task={"save": False, "pie": False, "allow_writes": []})
        if refused.get("error") != "write refused":
            fail(f"缺省任务下 {tool} 的 error={refused.get('error')!r}，应为 'write refused'",
                 f"core/editor_exec.py 里 {tool} 的拒绝分支改回 error='write refused'，policy 键不动")


def check_read_allowed() -> None:
    """query_actor 不在 allow_writes 时仍允许（未登记不当 high 拒掉）。"""
    verdict = risk.decide("query_actor", {"save": False, "pie": False, "allow_writes": []})
    if verdict["allow"] is not True or verdict["tier"] != "low":
        fail(f"query_actor 的裁决是 {verdict}",
             "core/risk.py 的 tier/decide 对矩阵 low 名字放行；未登记名字不得当成 high 拒绝")


def check_blueprint_switch() -> None:
    """UE_ALLOW_BLUEPRINT=false 时 create_node 被拒。测完恢复环境变量。"""
    previous = os.environ.get("UE_ALLOW_BLUEPRINT")
    try:
        os.environ["UE_ALLOW_BLUEPRINT"] = "false"
        blocked = exec_tool(
            BLUEPRINT_TOOLSET,
            "create_node",
            {},
            task={"allow_writes": ["create_node"], "save": False, "pie": False},
        )
        if blocked.get("error") not in ("blueprint edit disabled", "write refused"):
            fail(f"UE_ALLOW_BLUEPRINT=false 时 create_node 的 error={blocked.get('error')!r}",
                 "core/editor_exec.py 里开关为 false 的改蓝图文案保持 'blueprint edit disabled'，decide 不再重复拒绝")
    finally:
        if previous is None:
            os.environ.pop("UE_ALLOW_BLUEPRINT", None)
        else:
            os.environ["UE_ALLOW_BLUEPRINT"] = previous


def check_high_tiers() -> None:
    """矩阵里每个 high 名字都被 tier() 认成 high，graph_write 展开也认。"""
    matrix = json.loads((ROOT / "data" / "harness" / "risk_matrix.json").read_text(encoding="utf-8"))
    for name in matrix["high"]:
        if risk.tier(name) != "high":
            fail(f"矩阵 high 名字 {name} 被 tier() 认成 {risk.tier(name)!r}",
                 f"data/harness/risk_matrix.json 的 high 名单与 core/risk.py 的 tier() 对齐")
    for name in risk.GRAPH_WRITE_TOOLS:
        if risk.tier(name) != "high":
            fail(f"graph_write 展开的名字 {name} 被 tier() 认成 {risk.tier(name)!r}",
                 "core/risk.py 的 GRAPH_WRITE_TOOLS 与矩阵 high 的 graph_write 展开对齐")


def check_circuit() -> None:
    """连续同短名失败达到阈值后 decide 拒绝且 reason 含 circuit；exec_tool 报 circuit open。"""
    previous = os.environ.get("UE_CIRCUIT_PATH")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["UE_CIRCUIT_PATH"] = str(Path(tmp) / "circuit.json")
            tool = "save_assets"
            count = 0
            while count < fault.CIRCUIT_THRESHOLD:
                count = fault.record_failure(tool)
            verdict = risk.decide(tool, {})
            if verdict["allow"] is not False or "circuit" not in str(verdict["reason"]):
                fail(f"第 {fault.CIRCUIT_THRESHOLD} 次失败后 decide 是 {verdict}",
                     "core/fault.py 的 record_failure 达到 CIRCUIT_THRESHOLD 后写 circuit.json；core/risk.py 的 decide 读它并带 circuit 拒绝")
            refused = exec_tool(ASSET_TOOLSET, tool, {}, task={"save": True, "pie": False, "allow_writes": ["save_assets"]})
            if refused.get("error") != "circuit open":
                fail(f"熔断打开时 exec_tool 的 error={refused.get('error')!r}，应为 'circuit open'",
                     "core/editor_exec.py 的追加拒绝保持 error='circuit open'，policy 键不动")
            for key in ("save", "pie", "allow_writes", "UE_ALLOW_BLUEPRINT", "UE_ALLOW_MATERIAL", "UE_ALLOW_UMG", "UE_ALLOW_ANIM", "UE_ALLOW_NIAGARA"):
                if key not in (refused.get("policy") or {}):
                    fail(f"熔断拒绝的 policy 缺 {key}", "core/editor_exec.py 的熔断拒绝走 _refused，policy 键不动")
    finally:
        if previous is None:
            os.environ.pop("UE_CIRCUIT_PATH", None)
        else:
            os.environ["UE_CIRCUIT_PATH"] = previous


def check_context_switch_drift() -> None:
    """context-switch-drift：config.allow_blueprint() 与 project_context.json 的开关一致。"""
    from config import allow_blueprint

    context_path = ROOT / "data" / "project_context.json"
    recorded = bool((json.loads(context_path.read_text(encoding="utf-8")).get("switches") or {}).get("UE_ALLOW_BLUEPRINT"))
    live = bool(allow_blueprint())
    if recorded != live:
        print("❌ project_context.json 记 UE_ALLOW_BLUEPRINT="
              f"{recorded}，config.allow_blueprint()={live}，开关漂移")
        print("✅ FIX: 重跑 python pipelines/export_project_context.py 重新导出；不要手改 JSON，也不要改 .env")
        print(f"📖 See: {context_path}")
        sys.exit(1)


def main() -> int:
    check_write_refusals()
    check_read_allowed()
    check_blueprint_switch()
    check_high_tiers()
    check_circuit()
    check_context_switch_drift()
    print("risk ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
