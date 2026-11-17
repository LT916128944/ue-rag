# eval/check_animation_spec.py
"""动画 & ControlRig 域 Spec 内核守卫（20261117 边界升级）。全离线：不连编辑器、不调模型。

沿用 `eval/_spec_guard_lib.run` 的标准电池（validate / kernel 确定性 / adapter 覆盖 /
verifier / route / repair 暂停）。A 级关键帧取 good_spec；B 级 ControlRig 额外断言
`build(...)` 必须 `paused is True`（rig 图接线复用蓝图图 kernel / 待 P0，诚实暂停不假成功）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval._spec_guard_lib import run  # noqa: E402
from agent import animation_spec  # noqa: E402

GOOD = {
    "kind": "keyframe_anim",
    "target": "/Game/Characters/SK_Mannequin.SK_Mannequin",
    "bone": "hand_r",
    "channel": "rotation",
    "range": [0, 1.0],
    "keys": [
        {"t": 0.0, "value": [0, 0, 0]},
        {"t": 0.5, "value": [0, 60, 0]},
        {"t": 1.0, "value": [0, 0, 0]},
    ],
    "verify": ["keys:hand_r.rotation count=3", "t0.5==(0,60,0)", "saved"],
    "save": True,
}

GOOD_RB = {
    "bone_keys": {
        "hand_r": {
            "rotation": [
                {"t": 0.0, "value": [0, 0, 0]},
                {"t": 0.5, "value": [0, 60, 0]},
                {"t": 1.0, "value": [0, 0, 0]},
            ]
        }
    },
    "saved": True,
}

# 中段关键帧的值被改错（时间对、值不对）→ contains 至少一条不过。
BAD_RB = {
    "bone_keys": {
        "hand_r": {
            "rotation": [
                {"t": 0.0, "value": [0, 0, 0]},
                {"t": 0.5, "value": [0, 30, 0]},
                {"t": 1.0, "value": [0, 0, 0]},
            ]
        }
    },
    "saved": True,
}

CASES = {
    "good_spec": GOOD,
    "good_readback": GOOD_RB,
    "bad_readback": BAD_RB,
    "step": {},
    "bad_specs": [
        ({"kind": "keyframe_anim", "bone": "hand_r", "channel": "rotation",
          "range": [0, 1.0], "keys": [{"t": 0.0, "value": [0, 0, 0]}]}, "bad_target"),
        ({"kind": "keyframe_anim", "target": "/Game/SK.SK", "bone": "hand_r",
          "channel": "color", "range": [0, 1.0],
          "keys": [{"t": 0.0, "value": [0, 0, 0]}]}, "unknown_channel"),
        ({"kind": "keyframe_anim", "target": "/Game/SK.SK", "bone": "hand_r",
          "channel": "rotation", "range": [0, 1.0],
          "keys": [{"t": 0.0, "value": [0, 0, 0]}, {"t": 1.5, "value": [0, 60, 0]}]},
         "key_out_of_range"),
        ({"kind": "keyframe_anim", "target": "/Game/SK.SK", "bone": "hand_r",
          "channel": "rotation", "range": [0, 1.0],
          "keys": [{"t": 0.0, "value": [0, 0, 0]}, {"t": 0.5, "value": [0, 60, 0]},
                   {"t": 0.4, "value": [0, 0, 0]}]}, "keys_not_increasing"),
        ({"kind": "keyframe_anim", "target": "/Game/SK.SK", "bone": "hand_r",
          "channel": "rotation", "range": [0, 1.0], "keys": []}, "no_keys"),
    ],
    "positive_texts": [
        "做一个角色挥手动画",
        "给角色打一个走路循环关键帧",
        "做一段骨骼动画",
        "建一个 controlrig 控制绑定",
        "给 rig 加控件",
    ],
    "negative_texts": [
        "把一盏灯放进关卡",
        "给该UI资产加一个滑动条",
        "编译整个工程",
        "加一个C++类",
    ],
}

CONTROLRIG = {
    "kind": "controlrig",
    "asset": "/Game/Rigs/CR_Player.CR_Player",
    "controls": [{"name": "ctrl_arm", "parent": "root"}],
    "bindings": [{"control": "ctrl_arm", "bone": "upperarm_r"}],
    "verify": ["control:ctrl_arm exists", "binding:ctrl_arm->upperarm_r", "saved"],
}


def check_controlrig_pauses() -> str:
    """B 级 ControlRig：build 必须 paused is True（rig 图接线待 P0，不许假成功）。"""
    result = animation_spec.build({"spec": CONTROLRIG})
    if not isinstance(result, dict):
        raise SystemExit("FAIL animation：controlrig build 没有返回 dict")
    if result.get("ok") is True:
        raise SystemExit(f"FAIL animation：controlrig 竟报成功（假成功）：{result}")
    if result.get("paused") is not True:
        raise SystemExit(f"FAIL animation：controlrig build 必须 paused is True，实际 {result}")
    return "controlrig：rig 图接线待 P0，build 诚实 paused"


if __name__ == "__main__":
    rc = run(animation_spec, CASES)
    if rc != 0:
        raise SystemExit(rc)
    print("  " + check_controlrig_pauses())
    print("animation spec ok（关键帧 Spec/kernel/verifier/repair/route + ControlRig 暂停，全离线通过）")
    raise SystemExit(0)
