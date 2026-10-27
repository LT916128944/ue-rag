# eval/check_dsh_bridge.py
"""离线检查 DSH 薄 MCP 网关：mock 模式，不连编辑器、不调模型。"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "server" / "dsh_bridge.py"
RUN_ID = "20261023-000001"
ACTOR_TOOLSET = "editor_toolset.toolsets.actor.ActorTools"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dsh-bridge-"))
    (tmp / "runs").mkdir(parents=True, exist_ok=True)
    runs_root = tmp / "runs"
    task_path = tmp / "session_task.json"
    os.environ["UE_EXEC_MODE"] = "mock"
    os.environ["INJECT_ERRORS"] = "false"
    os.environ["UE_DSH_DATA_DIR"] = str(tmp)
    os.environ["UE_DSH_TASK_PATH"] = str(task_path)
    os.environ["UE_DSH_RUNS_DIR"] = str(runs_root)

    # 1) 工具签名里不能出现 allow_writes/save/pie
    spec = importlib.util.spec_from_file_location("dsh_bridge", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    params = set(inspect.signature(module.exec_editor_tool).parameters)
    check(params == {"toolset", "tool", "args"}, f"exec_editor_tool 参数异常: {params}")

    # 2) 卡片不带 schema
    cards = module.scan_editor_tools("查找 actor 工具", 2)
    check(cards.get("ok") is True, f"scan 失败: {cards}")
    check("input_schema" not in json.dumps(cards, ensure_ascii=False), "卡片里出现 input_schema")

    # 3) 空策略：写工具拒绝且带 policy；读工具放行
    task_path.write_text(json.dumps({"allow_writes": [], "save": False, "pie": False}), encoding="utf-8")
    refused = module.exec_editor_tool(
        ACTOR_TOOLSET,
        "set_actor_transform",
        {"actor": {"refPath": "EnemySpawnPoint_1"}, "xform": {"location": {"x": 1, "y": 0, "z": 0}}},
    )
    check(refused.get("ok") is False, f"写工具没被拒: {refused}")
    check("write refused" in str(refused.get("error")), f"拒绝原因不对: {refused}")
    read = module.exec_editor_tool(
        ACTOR_TOOLSET, "get_actor_transform", {"actor": {"refPath": "EnemySpawnPoint_1"}}
    )
    check(read.get("ok") is True, f"读工具失败: {read}")

    # 4) 合同：verify 只由裁判改 passed；manual 不动
    run = {
        "run_id": RUN_ID,
        "status": "失败",
        "criteria": [
            {"id": "c-ok", "text": "在盒子内", "evidence_kind": "editor_assert", "passed": False, "windows": ["W1"]},
            {"id": "c-manual", "text": "人工绑", "evidence_kind": "manual", "passed": False},
            {"id": "c-miss", "text": "缺 spec", "evidence_kind": "blueprint_compile", "passed": False, "windows": ["W1"]},
        ],
        "windows": {
            "W1": {
                "window": {
                    "task": {
                        "verify": [
                            {
                                "kind": "editor_assert",
                                "actor_name": "EnemySpawnPoint_1",
                                "expect": {"location": {"x": 0, "y": 0, "z": 100}},
                            }
                        ]
                    }
                }
            }
        },
    }
    (runs_root / f"{RUN_ID}.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    contract = module.read_contract(RUN_ID)
    check(contract.get("ok") is True and len(contract.get("criteria") or []) == 3, f"合同读取失败: {contract}")

    verified = module.verify(RUN_ID, "c-ok")
    check(verified.get("ok") is True, f"verify 执行失败: {verified}")
    check(verified.get("passed") is True, f"editor_assert 裁判应通过: {verified}")

    manual = module.verify(RUN_ID, "c-manual")
    check(manual.get("verified", [{}])[0].get("ok") is False, f"manual 条款不应被 verify: {manual}")
    saved = json.loads((runs_root / f"{RUN_ID}.json").read_text(encoding="utf-8"))
    manual_row = next(item for item in saved["criteria"] if item["id"] == "c-manual")
    check(manual_row.get("passed") is False, "manual 条款被改成了 passed")
    ok_row = next(item for item in saved["criteria"] if item["id"] == "c-ok")
    check(ok_row.get("passed") is True, "裁判通过后合同没写回")

    missing = module.verify(RUN_ID, "c-miss")
    check(missing.get("verified", [{}])[0].get("ok") is False, f"无 spec 不应猜: {missing}")

    # dry-run 不写
    before = (runs_root / f"{RUN_ID}.json").read_text(encoding="utf-8")
    cli = module._cli("verify", "--run", RUN_ID, "--criterion", "c-ok", "--dry-run")
    after = (runs_root / f"{RUN_ID}.json").read_text(encoding="utf-8")
    check(cli.get("ok") is True and cli.get("dry_run") is True, f"dry-run 失败: {cli}")
    check(before == after, "dry-run 改写了 run JSON")

    # 5) 写锁：持锁时第二次写返回 editor busy
    try:
        with module._editor_lock():
            busy = module.exec_editor_tool(
                ACTOR_TOOLSET,
                "set_actor_transform",
                {"actor": {"refPath": "EnemySpawnPoint_1"}, "xform": {"location": {"x": 2, "y": 0, "z": 0}}},
            )
    finally:
        pass
    check(busy.get("ok") is False and "editor busy" in str(busy.get("error")), f"写锁未生效: {busy}")

    # 6) 交接校验：缺字段/带 read_source 要失败
    bad = tmp / "bad-handoff.json"
    bad.write_text(json.dumps({"did": "x", "unmet": [], "read_source": "long body"}), encoding="utf-8")
    validate = module._cli("handoff-validate", "--file", str(bad))
    check(validate.get("ok") is False and validate.get("errors"), f"坏交接没被拒: {validate}")

    print("dsh-bridge ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
