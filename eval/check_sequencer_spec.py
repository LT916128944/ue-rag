# eval/check_sequencer_spec.py
"""Sequencer 域 Spec 内核守卫（20261117 边界升级·序列域）。全离线：不连编辑器、不调模型。

标准电池：validate/kernel/adapter/verifier/route/repair。轨道类型白名单 Transform/
CameraCut/Float/Event，range 递增，关键帧 t 落在 range 内，表外 track type 结构化拒绝。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval._spec_guard_lib import run  # noqa: E402
from agent import sequencer_spec  # noqa: E402

# 与计划 §3 完全同形（相机从 A 推到 B 的过场）。
GOOD = {
    "kind": "level_sequence",
    "asset": "/Game/Cinematics/LS_Intro.LS_Intro",
    "range": [0, 5.0],
    "bindings": [
        {"actor": "BP_PlayerCamera", "tracks": [
            {"type": "Transform", "keys": [{"t": 0, "loc": [0, 0, 0]},
                                           {"t": 5, "loc": [1000, 0, 200]}]},
            {"type": "CameraCut", "keys": [{"t": 0, "on": True}]},
        ]},
    ],
    "verify": ["binding:BP_PlayerCamera", "track:Transform keys=2",
               "t5 loc==(1000,0,200)", "range==0..5", "saved"],
}

GOOD_RB = {
    "saved": True,
    "range": {"start": 0, "end": 5.0},
    "bindings": {
        "BP_PlayerCamera": {
            "exists": True,
            "tracks": {
                "Transform": {"exists": True,
                              "keys": [{"t": 0, "loc": [0, 0, 0]},
                                       {"t": 5, "loc": [1000, 0, 200]}]},
                "CameraCut": {"exists": True, "keys": [{"t": 0, "on": True}]},
            },
        },
    },
}
BAD_RB = {
    "saved": True,
    "range": {"start": 0, "end": 6.0},
    "bindings": {
        "BP_PlayerCamera": {
            "exists": True,
            "tracks": {
                "Transform": {"exists": True,
                              "keys": [{"t": 0, "loc": [0, 0, 0]},
                                       {"t": 5, "loc": [900, 0, 200]}]},
                # CameraCut 轨道整个缺失 → 必有一条断言不过
            },
        },
    },
}

CASES = {
    "good_spec": GOOD,
    "good_readback": GOOD_RB,
    "bad_readback": BAD_RB,
    "step": {},
    "bad_specs": [
        ({"kind": "level_sequence", "asset": "", "range": [0, 5.0],
          "bindings": [{"actor": "A", "tracks": [
              {"type": "Transform", "keys": [{"t": 0, "loc": [0, 0, 0]}]}]}]}, "bad_asset"),
        ({"kind": "level_sequence", "asset": "/Game/C/LS_X.LS_X", "range": [5.0, 0],
          "bindings": [{"actor": "A", "tracks": [
              {"type": "Transform", "keys": [{"t": 0, "loc": [0, 0, 0]}]}]}]}, "bad_range"),
        ({"kind": "level_sequence", "asset": "/Game/C/LS_X.LS_X", "range": [0, 5.0],
          "bindings": [{"actor": "A", "tracks": [
              {"type": "Visibility", "keys": [{"t": 0}]}]}]}, "bad_track_type"),
        ({"kind": "level_sequence", "asset": "/Game/C/LS_X.LS_X", "range": [0, 5.0],
          "bindings": [{"actor": "A", "tracks": [
              {"type": "Transform", "keys": [{"t": 9, "loc": [0, 0, 0]}]}]}]}, "bad_key_time"),
        ({"kind": "level_sequence", "asset": "/Game/C/LS_X.LS_X", "range": [0, 5.0],
          "bindings": [{"tracks": [{"type": "Transform", "keys": [{"t": 0, "loc": [0, 0, 0]}]}]}]},
         "bad_binding"),
        ({"kind": "not_a_sequence"}, "unknown_kind"),
    ],
    "positive_texts": [
        "做一个过场镜头序列，相机从A推到B",
        "加一个相机切换 CameraCut",
        "在 LS_Intro 里加一条 Transform 轨道和关键帧",
    ],
    "negative_texts": [
        "加一个爆炸粒子，死亡时播放",
        "把一盏灯放进关卡",
        "编译整个工程",
        "给该UI资产加一个滑动条",
    ],
}

if __name__ == "__main__":
    raise SystemExit(run(sequencer_spec, CASES))
