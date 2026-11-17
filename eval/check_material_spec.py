# eval/check_material_spec.py
"""材质域 Spec 内核守卫（20261117 边界升级·材质域）。全离线：不连编辑器、不调模型。

标准电池（validate/kernel/adapter/verifier/route/repair）见 ``eval/_spec_guard_lib.run``；
本文件额外钉一条材质域独有边界：``kind == "material_graph"`` 今日无 MCP 原语，
``build`` 必须**结构化暂停**（paused is True），绝不伪造连线工具调用。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval._spec_guard_lib import run  # noqa: E402
from agent import material_spec  # noqa: E402

GOOD = {
    "kind": "material_instance",
    "target": "/Game/Materials/MI_Test.MI_Test",
    "params": [
        {"name": "BaseColor", "type": "vector", "value": [1, 0, 0, 1]},
        {"name": "Roughness", "type": "scalar", "value": 0.4},
        {"name": "NormalTex", "type": "texture", "value": "/Game/Materials/T_Normal.T_Normal"},
    ],
    "verify": ["params.BaseColor == [1,0,0,1]", "params.Roughness == 0.4",
               "params.NormalTex == /Game/Materials/T_Normal.T_Normal"],
}

GOOD_RB = {
    "params": {
        "BaseColor": [1, 0, 0, 1],
        "Roughness": 0.4,
        "NormalTex": "/Game/Materials/T_Normal.T_Normal",
    },
}
BAD_RB = {
    "params": {
        "BaseColor": [0, 1, 0, 1],
        "Roughness": 0.9,
        "NormalTex": "/Game/Materials/T_Normal.T_Normal",
    },
}

CASES = {
    "good_spec": GOOD,
    "good_readback": GOOD_RB,
    "bad_readback": BAD_RB,
    "step": {},
    "bad_specs": [
        ({"kind": "material_instance",
          "params": [{"name": "X", "type": "scalar", "value": 1}]}, "bad_target"),
        ({"kind": "material_instance", "target": "/Game/x/MI_x.MI_x", "params": []}, "no_params"),
        ({"kind": "material_instance", "target": "/Game/x/MI_x.MI_x",
          "params": [{"name": "X", "type": "float", "value": 1}]}, "unknown_param_type"),
        ({"kind": "material_instance", "target": "/Game/x/MI_x.MI_x",
          "params": [{"name": "X", "type": "scalar", "value": "red"}]}, "bad_scalar_value"),
        ({"kind": "material_instance", "target": "/Game/x/MI_x.MI_x",
          "params": [{"name": "X", "type": "vector", "value": [1, 0, 0]}]}, "bad_vector_value"),
        ({"kind": "material_instance", "target": "/Game/x/MI_x.MI_x",
          "params": [{"name": "X", "type": "texture", "value": "T_Foo"}]}, "bad_texture_value"),
        ({"kind": "material_instance", "target": "/Game/x/MI_x.MI_x",
          "params": [{"name": "X", "type": "scalar", "value": 1},
                     {"name": "X", "type": "scalar", "value": 2}]}, "dup_param"),
    ],
    "positive_texts": [
        "把材质实例 MI_Door 的基础色改成红色",
        "把材质参数 Roughness 粗糙度调到 0.4",
        "给这个材质加一个自发光参数",
    ],
    "negative_texts": [
        "加个灯",
        "编译工程",
        "滑动条",
    ],
}


if __name__ == "__main__":
    rc = run(material_spec, CASES)
    if rc != 0:
        raise SystemExit(rc)
    # 材质域独有边界：material_graph 必须结构化暂停（走人工，绝不伪造连线）
    graph_step = {
        "spec": {
            "kind": "material_graph",
            "target": "/Game/Materials/M_Test.M_Test",
            "nodes": [{"id": "c", "cls": "Constant3Vector", "params": {"R": 1, "G": 0, "B": 0}}],
            "edges": [{"from": "c.0", "to": "Root.BaseColor"}],
        }
    }
    result = material_spec.build(graph_step, call=(lambda *a, **k: {"ok": True, "data": {}}))
    if result.get("paused") is not True:
        print(f"FAIL material: material_graph 未结构化暂停：{result}")
        raise SystemExit(1)
    if result.get("phase") != "unsupported":
        print(f"FAIL material: material_graph 未走 unsupported 暂停：{result}")
        raise SystemExit(1)
    print("  boundary：material_graph 走人工 ⇒ 结构化暂停（不伪造连线）")
    print("material spec ok（material_graph 边界亦通过）")
    raise SystemExit(0)
