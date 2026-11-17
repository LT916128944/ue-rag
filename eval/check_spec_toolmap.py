# eval/check_spec_toolmap.py
"""P0 工具清单守卫（20261117 边界升级·真机 discover 核实）。全离线：只读快照，不连编辑器。

钉四件事：
  1. 证据快照在位且含预期命名空间（monolith discover 快照，不是空表）；
  2. 每个域的每个原语都登记了；action 非 None 的**必须真实存在于快照**（工具名不许猜）；
  3. 九域都有至少一个可核实原语；缺口（action=None）必须带说明，不许只留空；
  4. default_call 路由：monolith 原生命名空间走 backends.monolith.exec，其余走 exec_tool。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import spec_toolmap as tm  # noqa: E402

EXPECTED_NAMESPACES = ("material", "niagara", "animation", "level_sequence", "blueprint", "ui", "editor")
EXPECTED_DOMAINS = ("umg", "graph", "animgraph", "material", "niagara", "animation",
                    "sequencer", "postprocess", "cpp")


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def check_inventory() -> str:
    inv = tm.inventory()
    _check(bool(inv), "工具快照为空（读不到 data/harness/monolith_tool_inventory.json）")
    for ns in EXPECTED_NAMESPACES:
        _check(ns in inv and len(inv[ns]) > 0, f"快照缺命名空间 {ns} 或为空")
    total = sum(len(v) for v in inv.values())
    return f"证据快照在位：{len(inv)} 个命名空间、{total} 个动作（ref={tm.REF[:24]}…）"


def check_every_op_verified_or_pending() -> str:
    verified = 0
    pending = 0
    bad: list[str] = []
    for domain in EXPECTED_DOMAINS:
        _check(domain in tm.OPS, f"toolmap 缺域 {domain}")
        for op, row in tm.OPS[domain].items():
            ns = row.get("ns") or ""
            action = row.get("action")
            if action is None:
                _check(bool(str(row.get("note") or "").strip()),
                       f"{domain}.{op} 是缺口却没写 note（不许只留空）")
                pending += 1
                continue
            if not tm.has_action(ns, action):
                bad.append(f"{domain}.{op} → {ns}.{action}")
            else:
                verified += 1
    _check(not bad, "以下原语映射到快照里不存在的动作（工具名猜错）：\n  " + "\n  ".join(bad))
    return f"九域原语全部登记：verified {verified} 条、诚实缺口 {pending} 条（都带说明）"


def check_each_domain_has_verified() -> str:
    for domain in EXPECTED_DOMAINS:
        v = [op for op, row in tm.OPS[domain].items()
             if row.get("action") and tm.has_action(row.get("ns") or "", row["action"])]
        _check(bool(v), f"{domain} 一个可核实原语都没有（该域 kernel 无从落地）")
    return "九域各自都至少有一条可核实的真实工具"


def check_default_call_routing() -> str:
    from agent import spec_pipeline as sp

    hits: list[tuple[str, tuple]] = []

    def fake_monolith(namespace, action, params=None, task=None, transport=None):
        hits.append(("monolith", (namespace, action)))
        return {"ok": True, "data": {}}

    def fake_exec_tool(toolset, tool, args=None, task=None, timeout=None):
        hits.append(("exec_tool", (toolset, tool)))
        return {"ok": True, "data": {}}

    import backends.monolith as mono_mod
    import core.editor_exec as ee_mod

    saved_m, saved_e = mono_mod.exec, ee_mod.exec_tool
    mono_mod.exec = fake_monolith
    ee_mod.exec_tool = fake_exec_tool
    try:
        hits.clear()
        sp.default_call("material", "set_instance_parameter", {}, {})
        _check(hits == [("monolith", ("material", "set_instance_parameter"))],
               f"原生命名空间应走 monolith.exec，实际 {hits}")
        hits.clear()
        sp.default_call("editor_toolset.toolsets.asset.AssetTools", "save_assets", {}, {})
        _check(hits == [("exec_tool", ("editor_toolset.toolsets.asset.AssetTools", "save_assets"))],
               f"official 家族名应走 exec_tool，实际 {hits}")
    finally:
        mono_mod.exec = saved_m
        ee_mod.exec_tool = saved_e
    return "default_call 路由正确：原生命名空间→monolith.exec，其余→exec_tool"


def check_args_verified() -> str:
    """VERIFIED_ARGS 里的动作必须在快照里存在、且 required 非空；PENDING_ARGS 不许混进已核实的。"""
    for key, meta in tm.VERIFIED_ARGS.items():
        ns, _, action = key.partition(".")
        _check(tm.has_action(ns, action), f"VERIFIED_ARGS 声明的 {key} 在快照里不存在")
        _check(bool(meta.get("required")), f"VERIFIED_ARGS[{key}] 的 required 为空（等于没核实）")
    for ns, action in tm.PENDING_ARGS:
        _check(tm.has_action(ns, action), f"PENDING_ARGS 的 {ns}.{action} 在快照里不存在（名字都不对）")
        _check(f"{ns}.{action}" not in tm.VERIFIED_ARGS,
               f"PENDING_ARGS 的 {ns}.{action} 已进 VERIFIED_ARGS，该删")
    return f"参数核实：{len(tm.VERIFIED_ARGS)} 个动作入参已 describe 核实，{len(tm.PENDING_ARGS)} 个仍待 describe"


def main() -> int:
    try:
        rows = [
            check_inventory(),
            check_every_op_verified_or_pending(),
            check_each_domain_has_verified(),
            check_args_verified(),
            check_default_call_routing(),
        ]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print("  " + row)
    print("spec toolmap ok（真机核实清单 + 九域映射 + 缺口登记 + 路由，全离线通过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
