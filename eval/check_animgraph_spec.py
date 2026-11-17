# eval/check_animgraph_spec.py
"""动画蓝图域 Spec 内核守卫（20261117 边界升级）。全离线：不连编辑器、不调模型。

照 `eval/check_umg_spec.py`：给一张 CASES 表 + 一行 run()，再补一条本域**必有**的手工档断言：
只含 `state_machine.transitions` 的 Spec 经 build 必须 `paused is True`（结构化暂停，不伪造工具）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval._spec_guard_lib import run  # noqa: E402
from agent import animgraph_spec  # noqa: E402

GOOD = {
    "anim_blueprint": "/Game/Characters/ABP_Player.ABP_Player",
    "skeleton": "/Game/Characters/SK_Mannequin.SK_Mannequin",
    "variables": [{"name": "Speed", "type": "float"},
                  {"name": "bIsRunning", "type": "bool"}],
    "state_machine": {
        "name": "Locomotion",
        "states": [
            {"name": "Idle", "anim": "/Game/Anim/Idle.Idle"},
            {"name": "Run", "anim": "/Game/Anim/Run.Run"},
        ],
        "transitions": [],   # 自动化档不含转换接线
    },
    "verify": ["sm:Locomotion", "state:Run anim=Run", "compile_ok"],
}

GOOD_RB = {
    "compiled": True,
    "variables": {"Speed": {"type": "float"}, "bIsRunning": {"type": "bool"}},
    "nodes": {
        "Idle": {"class": "SequencePlayer", "anim": "/Game/Anim/Idle.Idle"},
        "Run": {"class": "SequencePlayer", "anim": "/Game/Anim/Run.Run"},
    },
}

BAD_RB = {
    "compiled": True,
    "variables": {"Speed": {"type": "float"}},   # 缺 bIsRunning
    "nodes": {
        "Idle": {"class": "SequencePlayer", "anim": "/Game/Anim/Idle.Idle"},
        "Run": {"class": "SequencePlayer", "anim": "/Game/Anim/Walk.Walk"},  # anim 错
    },
}

CASES = {
    "good_spec": GOOD,
    "good_readback": GOOD_RB,
    "bad_readback": BAD_RB,
    "step": {},
    "bad_specs": [
        # 缺 anim_blueprint
        ({"skeleton": "/x/S.S", "state_machine": {"name": "L",
          "states": [{"name": "Idle", "anim": "/Game/Anim/Idle.Idle"}]}}, "bad_target"),
        # 缺 skeleton
        ({"anim_blueprint": "/x/ABP.ABP", "state_machine": {"name": "L",
          "states": [{"name": "Idle", "anim": "/Game/Anim/Idle.Idle"}]}}, "bad_skeleton"),
        # 缺 state_machine
        ({"anim_blueprint": "/x/ABP.ABP", "skeleton": "/x/S.S"}, "no_state_machine"),
        # 空 states
        ({"anim_blueprint": "/x/ABP.ABP", "skeleton": "/x/S.S",
          "state_machine": {"name": "L", "states": []}}, "no_states"),
        # 表外变量类型
        ({"anim_blueprint": "/x/ABP.ABP", "skeleton": "/x/S.S",
          "variables": [{"name": "Speed", "type": "quaternion"}],
          "state_machine": {"name": "L",
                            "states": [{"name": "Idle", "anim": "/Game/Anim/Idle.Idle"}]}},
         "unknown_var_type"),
        # state 缺 anim
        ({"anim_blueprint": "/x/ABP.ABP", "skeleton": "/x/S.S",
          "state_machine": {"name": "L", "states": [{"name": "Idle", "anim": ""}]}}, "bad_anim"),
        # state 名重复
        ({"anim_blueprint": "/x/ABP.ABP", "skeleton": "/x/S.S",
          "state_machine": {"name": "L", "states": [
              {"name": "Idle", "anim": "/Game/Anim/Idle.Idle"},
              {"name": "Idle", "anim": "/Game/Anim/Run.Run"}]}}, "dup_state"),
    ],
    "positive_texts": [
        "做一个动画蓝图，移动时播放跑步动画",
        "在 ABP_Player 的状态机里加一个 Jump 状态",
        "给动画蓝图加一个 Sequence Player 播放 Run 动画",
        "animgraph 里放个 sequence player 引用 Idle",
    ],
    "negative_texts": [
        "把一盏灯放进关卡",
        "编译整个工程",
        "给界面加一个滑动条",
    ],
}

if __name__ == "__main__":
    rc = run(animgraph_spec, CASES)
    if rc != 0:
        raise SystemExit(rc)
    # 本域必有：只含 state_machine.transitions 的 Spec ⇒ 结构化暂停（不伪造工具调用）。
    transitions_only = {
        "anim_blueprint": "/Game/Characters/ABP_Player.ABP_Player",
        "skeleton": "/Game/Characters/SK_Mannequin.SK_Mannequin",
        "state_machine": {
            "name": "Locomotion",
            "states": [],
            "transitions": [{"from": "Idle", "to": "Run", "rule": "Speed > 10"}],
        },
    }
    res = animgraph_spec.build(
        {"spec": transitions_only},
        call=(lambda *a, **k: {"ok": True, "data": {}}),
    )
    assert res.get("paused") is True, f"只含 transitions 的 Spec 应结构化暂停，实际：{res}"
    assert res.get("ok") is not True, f"只含 transitions 的 Spec 不该报成功：{res}"
    assert not res.get("tools"), f"transitions 子形态不该伪造工具调用，实际 tools={res.get('tools')}"
    print("animgraph transitions 子形态：结构化暂停 ✓（未伪造工具调用）")
    raise SystemExit(0)
