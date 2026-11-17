# eval/check_postprocess_spec.py
"""后处理域 Spec 内核守卫（20261117 边界升级·后处理域）。全离线：不连编辑器、不调模型。

标准电池（validate/kernel/adapter/verifier/route/repair）见 ``eval/_spec_guard_lib.run``。
本文件只提供 CASES：setting 白名单、读回==期望、越界结构拒绝、route 正反例句。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval._spec_guard_lib import run  # noqa: E402
from agent import postprocess_spec  # noqa: E402

GOOD = {
    "kind": "post_process",
    "target": {"type": "volume", "name": "PP_LevelDefault", "unbound": True},
    "settings": {
        "BloomIntensity": 1.2,
        "VignetteIntensity": 0.4,
        "AutoExposureMinBrightness": 0.5,
        "AutoExposureMaxBrightness": 2.0,
        "SceneColorTint": [1, 1, 1, 1],
    },
    "verify": ["Settings.BloomIntensity == 1.2", "Settings.VignetteIntensity == 0.4",
               "unbound == true", "saved"],
}

GOOD_RB = {
    "Settings": {
        "BloomIntensity": 1.2,
        "VignetteIntensity": 0.4,
        "AutoExposureMinBrightness": 0.5,
        "AutoExposureMaxBrightness": 2.0,
        "SceneColorTint": [1, 1, 1, 1],
    },
    "unbound": True,
    "saved": True,
}
BAD_RB = {
    "Settings": {
        "BloomIntensity": 1.2,
        "VignetteIntensity": 0.9,          # 没设上
        "AutoExposureMinBrightness": 0.5,
        "AutoExposureMaxBrightness": 2.0,
        "SceneColorTint": [1, 1, 1, 1],
    },
    "unbound": False,                        # 没解绑
    "saved": True,
}

CASES = {
    "good_spec": GOOD,
    "good_readback": GOOD_RB,
    "bad_readback": BAD_RB,
    "step": {},
    "bad_specs": [
        ({"kind": "post_process", "settings": {"BloomIntensity": 1.2}}, "bad_target"),
        ({"kind": "post_process", "target": {"type": "light", "name": "L"},
          "settings": {"BloomIntensity": 1.2}}, "bad_target_type"),
        ({"kind": "post_process", "target": {"type": "volume"},
          "settings": {"BloomIntensity": 1.2}}, "bad_target_name"),
        ({"kind": "post_process", "target": {"type": "volume", "name": "PP", "unbound": "yes"},
          "settings": {"BloomIntensity": 1.2}}, "bad_target_unbound"),
        ({"kind": "post_process", "target": {"type": "volume", "name": "PP"},
          "settings": {}}, "no_settings"),
        ({"kind": "post_process", "target": {"type": "volume", "name": "PP"},
          "settings": {"BloomIntensity": 1.2, "BogusNestedField": 1}}, "unsupported_setting"),
        ({"kind": "post_process", "target": {"type": "volume", "name": "PP"},
          "settings": {"BloomIntensity": "high"}}, "bad_setting_value"),
        ({"kind": "post_process", "target": {"type": "volume", "name": "PP"},
          "settings": {"SceneColorTint": [1, 0, 0]}}, "bad_setting_value"),
    ],
    "positive_texts": [
        "给关卡加一个后处理泛光效果",
        "把泛光强度调到 1.2",
        "调一下曝光",
        "加个暗角",
        "给相机加 PostProcess",
    ],
    "negative_texts": [
        "把材质实例的基础色改成红色",
        "编译整个工程",
        "给关卡加一盏点光源",
    ],
}


if __name__ == "__main__":
    raise SystemExit(run(postprocess_spec, CASES))
