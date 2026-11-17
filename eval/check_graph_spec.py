# eval/check_graph_spec.py
"""蓝图图域 Spec 内核守卫（20261117 边界升级）。全离线：不连编辑器、不调模型。

照 `eval/check_umg_spec.py`：给一张 CASES 表 + 一行 run()，再补一条 graph 手工档断言
（表外节点类 → build 走 validate 失败 ⇒ 结构化暂停，不假成功）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval._spec_guard_lib import run  # noqa: E402
from agent import graph_spec  # noqa: E402

GOOD = {
    "blueprint": "/Game/Blueprints/BP_Door.BP_Door",
    "graph": "EventGraph",
    "variables": [{"name": "bOpen", "type": "bool", "default": False}],
    "nodes": [
        {"id": "ev", "class": "K2Node_Event", "event": "ReceiveBeginPlay"},
        {"id": "set", "class": "K2Node_VariableSet", "var": "bOpen", "defaults": {"bOpen": True}},
        {"id": "print", "class": "K2Node_CallFunction", "function": "PrintString",
         "defaults": {"InString": "opened"}},
    ],
    "edges": [
        {"from": "ev.exec", "to": "set.exec"},
        {"from": "set.then", "to": "print.execute"},
    ],
    "verify": ["node:set", "node:print", "edge:ev.exec->set.exec", "compile_ok"],
}

GOOD_RB = {
    "compiled": True,
    "variables": {"bOpen": {"type": "bool"}},
    "nodes": {
        "ev": {"class": "K2Node_Event"},
        "set": {"class": "K2Node_VariableSet"},
        "print": {"class": "K2Node_CallFunction"},
    },
    "edges": ["ev.exec->set.exec", "set.then->print.execute"],
}

BAD_RB = {
    "compiled": True,
    "variables": {"bOpen": {"type": "bool"}},
    "nodes": {
        "ev": {"class": "K2Node_Event"},
        "set": {"class": "K2Node_VariableSet"},
        "print": {"class": "K2Node_IfThenElse"},   # 类错
    },
    "edges": ["ev.exec->set.exec"],               # 缺第二条边
}

CASES = {
    "good_spec": GOOD,
    "good_readback": GOOD_RB,
    "bad_readback": BAD_RB,
    "step": {},
    "bad_specs": [
        # 缺 blueprint
        ({"graph": "EventGraph",
          "nodes": [{"id": "ev", "class": "K2Node_Event", "event": "ReceiveBeginPlay"}]},
         "bad_target"),
        # 空 nodes
        ({"blueprint": "/x/BP.X", "nodes": []}, "no_nodes"),
        # 表外节点类
        ({"blueprint": "/x/BP.X", "nodes": [{"id": "a", "class": "K2Node_HyperWidget"}]},
         "unknown_class"),
        # 节点 id 重复
        ({"blueprint": "/x/BP.X",
          "nodes": [{"id": "a", "class": "K2Node_Event", "event": "E"},
                    {"id": "a", "class": "K2Node_Event", "event": "E"}]}, "dup_id"),
        # 边指向未知节点
        ({"blueprint": "/x/BP.X",
          "nodes": [{"id": "a", "class": "K2Node_Event", "event": "E"}],
          "edges": [{"from": "a.exec", "to": "b.exec"}]}, "bad_edge"),
        # 节点成环
        ({"blueprint": "/x/BP.X",
          "nodes": [{"id": "a", "class": "K2Node_Event", "event": "E"},
                    {"id": "b", "class": "K2Node_Event", "event": "E"}],
          "edges": [{"from": "a.exec", "to": "b.exec"},
                    {"from": "b.then", "to": "a.exec"}]}, "cycle"),
        # 引用未声明变量
        ({"blueprint": "/x/BP.X", "variables": [{"name": "x", "type": "bool"}],
          "nodes": [{"id": "s", "class": "K2Node_VariableSet", "var": "y"}]}, "unknown_var"),
    ],
    "positive_texts": [
        "在 BP_Door 的事件图里加一个节点",
        "把 BeginPlay 连到 PrintString 节点",
        "给蓝图图加节点并连线",
        "在事件图里把执行引脚连到打印节点",
    ],
    "negative_texts": [
        "把一盏灯放进关卡",
        "编译整个工程",
        "在界面左下角加一个滑动条",
    ],
}

if __name__ == "__main__":
    rc = run(graph_spec, CASES)
    if rc != 0:
        raise SystemExit(rc)
    # 额外手工档：表外节点类 → validate 失败、模型救不回 ⇒ 结构化暂停（不假成功）。
    bad = {"blueprint": "/x/BP.X", "nodes": [{"id": "a", "class": "K2Node_HyperWidget"}]}
    res = graph_spec.build(
        {"spec": bad},
        call=(lambda *a, **k: {"ok": True, "data": {}}),
        fix_model=(lambda spec, errs: None),
        max_rounds=2,
    )
    assert res.get("paused") is True, f"表外节点类应结构化暂停，实际：{res}"
    assert res.get("ok") is not True, f"表外节点类不该报成功：{res}"
    print("graph 额外档：表外节点类 ⇒ 结构化暂停 ✓")
    raise SystemExit(0)
