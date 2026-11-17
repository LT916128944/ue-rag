# eval/check_umg_spec.py
"""UMG 域 Spec 内核守卫（20261117 边界升级·参考样板）。全离线：不连编辑器、不调模型。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval._spec_guard_lib import run  # noqa: E402
from agent import umg_spec  # noqa: E402

GOOD = {
    "widget_blueprint": "/Game/UI/WBP_Test.WBP_Test",
    "root": "CanvasPanel",
    "nodes": [
        {"id": "Slider_Volume", "class": "Slider", "parent": "CanvasPanel", "anchor": "top_left",
         "position": {"x": 40, "y": 60}, "size": {"x": 240, "y": 24},
         "properties": {"Value": 0.5, "StepSize": 0.1}},
        {"id": "Btn_Ok", "class": "Button", "anchor": "center",
         "position": {"x": 100, "y": 200}, "size": {"x": 120, "y": 40}},
    ],
}

GOOD_RB = {
    "compiled": True,
    "widgets": {
        "Slider_Volume": {"class": "Slider", "props": {"Value": 0.5, "StepSize": 0.1}},
        "Btn_Ok": {"class": "Button", "props": {}},
    },
}
BAD_RB = {
    "compiled": True,
    "widgets": {
        "Slider_Volume": {"class": "TextBlock", "props": {"Value": 0.9, "StepSize": 0.1}},
        "Btn_Ok": {"class": "Button", "props": {}},
    },
}

CASES = {
    "good_spec": GOOD,
    "good_readback": GOOD_RB,
    "bad_readback": BAD_RB,
    "step": {},
    "bad_specs": [
        ({"widget_blueprint": "", "nodes": [{"id": "a", "class": "Slider"}]}, "bad_target"),
        ({"widget_blueprint": "/x/W.W", "nodes": []}, "no_nodes"),
        ({"widget_blueprint": "/x/W.W", "root": "CanvasPanel",
          "nodes": [{"id": "a", "class": "HyperWidget"}]}, "unknown_class"),
        ({"widget_blueprint": "/x/W.W", "root": "CanvasPanel",
          "nodes": [{"id": "a", "class": "Slider", "parent": "b"},
                    {"id": "b", "class": "Slider", "parent": "a"}]}, "cycle"),
    ],
    "positive_texts": [
        "给该UI资产加一个滑动条",
        "在控件图 WBP_Setting 里加一个进度条",
        "左下角显示血条",
    ],
    "negative_texts": [
        "把一盏灯放进关卡",
        "编译整个工程",
        "做一个角色挥手动画",
    ],
}

if __name__ == "__main__":
    raise SystemExit(run(umg_spec, CASES))
