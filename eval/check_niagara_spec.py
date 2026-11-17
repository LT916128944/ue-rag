# eval/check_niagara_spec.py
"""Niagara 域 Spec 内核守卫（20261117 边界升级·粒子域）。全离线：不连编辑器、不调模型。

除标准电池（validate/kernel/adapter/verifier/route/repair）外，额外钉住一条域纪律：
**B 级 `kind=="niagara_system"` 必须结构化暂停（paused=True）**，不伪造模块栈工具调用。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval._spec_guard_lib import run  # noqa: E402
from agent import niagara_spec  # noqa: E402

# A 级（自动化档，good_spec 取这档，与计划 §3 完全同形）。
GOOD = {
    "kind": "niagara_component",
    "target": "BP_TestActor:BP_TestActor",
    "system": "/Game/FX/NS_Explosion.NS_Explosion",
    "auto_activate": True,
    "user_params": [
        {"name": "Color", "type": "vector", "value": [1, 0.5, 0, 1]},
    ],
    "verify": ["component:Niagara has NS_Explosion", "param:Color==(1,0.5,0,1)", "saved"],
}

GOOD_RB = {
    "saved": True,
    "component": {"exists": True, "system": "/Game/FX/NS_Explosion.NS_Explosion"},
    "params": {"Color": [1, 0.5, 0, 1]},
}
BAD_RB = {
    "saved": True,
    "component": {"exists": True, "system": "/Game/FX/NS_Wrong.NS_Wrong"},
    "params": {"Color": [1, 0, 0, 1]},
}

CASES = {
    "good_spec": GOOD,
    "good_readback": GOOD_RB,
    "bad_readback": BAD_RB,
    "step": {},
    "bad_specs": [
        ({"kind": "niagara_component", "target": "", "system": "/Game/FX/NS_X.NS_X"}, "bad_target"),
        ({"kind": "niagara_component", "target": "BP_X", "system": "/Temp/NS_X.NS_X"}, "bad_system"),
        ({"kind": "niagara_component", "target": "BP_X", "system": "/Game/FX/NS_X.NS_X",
          "user_params": [{"name": "Color", "type": "quaternion", "value": [1, 0, 0, 1]}]}, "bad_param_type"),
        ({"kind": "niagara_system", "asset": "C:/FX/NS_X.NS_X"}, "bad_asset"),
        ({"kind": "weird_spec"}, "unknown_kind"),
    ],
    "positive_texts": [
        "加一个爆炸粒子，死亡时播放",
        "给角色加个 Niagara 火焰拖尾特效",
        "把 NS_Explosion 粒子特效挂到 actor 上",
    ],
    "negative_texts": [
        "做一个过场镜头序列，相机从A推到B",
        "把一盏灯放进关卡",
        "编译整个工程",
        "给该UI资产加一个滑动条",
    ],
}

# B 级样例：建/改 System 资产的模块栈——今日必须结构化暂停，不落模块栈工具调用。
SYSTEM_SPEC = {
    "kind": "niagara_system",
    "asset": "/Game/FX/NS_Explosion.NS_Explosion",
    "modules": [
        {"stack": "ParticleSpawn", "module": "SpawnRate", "params": {"Rate": 50}},
        {"stack": "ParticleUpdate", "module": "GravityForce", "params": {"Z": -980}},
    ],
    "verify": ["emitter:0 has SpawnRate", "emitter:0 has GravityForce", "saved"],
}

if __name__ == "__main__":
    rc = run(niagara_spec, CASES)
    if rc != 0:
        raise SystemExit(rc)
    # 额外守卫：B 级 niagara_system 必须结构化暂停（paused=True），不伪造模块栈调用。
    result = niagara_spec.build({"spec": SYSTEM_SPEC})
    if result.get("paused") is not True:
        print(f"FAIL niagara: B 级 niagara_system 未结构化暂停：{result}")
        raise SystemExit(1)
    if result.get("tools"):
        print(f"FAIL niagara: B 级暂停却仍报了工具调用：{result.get('tools')}")
        raise SystemExit(1)
    print("niagara_system → paused（B 级结构化暂停，无工具调用）")
    raise SystemExit(0)
