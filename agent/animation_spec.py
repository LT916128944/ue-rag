# agent/animation_spec.py
"""动画 & ControlRig 域：Spec → 确定性执行 kernel → 只读复核（20261117 边界升级）。

与 `agent/umg_spec.py` **完全同形**的数据驱动内核：

  - A 级 关键帧动画（`kind == "keyframe_anim"`）：模型产 `{target, bone, channel, range,
    keys:[{t,value}], verify}`；脚本用 keyframing 一族打 key，再只读读回关键帧，
    逐条比对「时间 / 值」== Spec。
  - B 级 ControlRig（`kind == "controlrig"`）：今天脚本侧**诚实暂停点名**——
    rig 图接线复用蓝图图 kernel / 待 P0 工具确认。`build()` 对任何 controlrig Spec
    直接返回 `sp.unsupported_result(...)`（paused），不假装做完。

关键帧打 key 的真实工具在 `animation_toolset.toolsets.keyframing.SequencerKeyframingTools`
（add_key_float/get_keys 等）；但「骨骼 + rotation 向量通道 → keyframing 标量通道」的
映射与 t(秒)→frame 的 display rate 换算**待 P0 describe 确认**（见 `_frame_of` 注释）。
本模块只出确定性原语清单与只读断言，工具名以计划为准、拿不准处已标「待 P0 describe」。
"""
from __future__ import annotations

import json
import re

from agent import spec_pipeline as sp

KIND = "animation"
JUDGE_KIND = "editor_assert"

# 关键帧通道白名单（表外 → 结构化「不支持」）。
CHANNELS: tuple[str, ...] = ("rotation", "translation", "scale")

# t(秒) → frame 的换算率：Sequencer keyframing 按整数帧打 key。
# 真实 display rate（24/30/60）待 P0 describe 确认；这里用 30 作为确定性缺省。
_DISPLAY_RATE = 30

# keyframing 工具集（计划 §0 相关 toolset）。动作名/参数以 P0 describe 为准。
_KEY_TOOLSET = "animation_toolset.toolsets.keyframing.SequencerKeyframingTools"
_ASSET_TOOLSET = "editor_toolset.toolsets.asset.AssetTools"

SPEC_HINT = (
    "动画 Spec，JSON。A 关键帧放大档："
    "{kind:'keyframe_anim', target:'/Game/.../SK_X.SK_X', bone:'hand_r',"
    " channel:(rotation|translation|scale), range:[t0,t1],"
    " keys:[{t:<秒>,value:[x,y,z]}], verify:[...]}。"
    "channel 只能取 rotation/translation/scale；keys 的 t 必须落在 range 内且严格递增。"
    "B ControlRig：{kind:'controlrig', asset:'/Game/Rigs/CR_X.CR_X',"
    " controls:[{name,parent}], bindings:[{control,bone}], verify:[...]}——"
    "B 今天脚本侧暂停点名：rig 图接线复用蓝图图 kernel / 待 P0 工具确认。"
    "复核口径：关键帧读回的时间/值必须与 Spec 逐条一致，资产已保存。"
)

# 写闸白名单：keyframing 打 key / 读 key / 保存资产。
_WRITE_ALLOW = (
    "SequencerKeyframingTools", "add_key_float", "add_key_integer", "add_key_bool",
    "add_key_string", "remove_key_at_frame", "save_assets",
)


def _module():
    import sys

    return sys.modules[__name__]


# ------------------------------------------------------------------ 校验
def _validate_controlrig(spec: dict) -> list:
    """B 级 ControlRig 的最小结构校验（实际执行会在 build 里暂停点名）。"""
    errors: list = []
    asset = spec.get("asset")
    if not isinstance(asset, str) or not asset.strip():
        errors.append(sp.spec_error("bad_asset", "缺 asset（/Game/Rigs/CR_X.CR_X）",
                                    where="asset"))
    controls = spec.get("controls", [])
    if controls is not None and not isinstance(controls, list):
        errors.append(sp.spec_error("bad_controls", "controls 必须是数组", where="controls"))
    bindings = spec.get("bindings", [])
    if bindings is not None and not isinstance(bindings, list):
        errors.append(sp.spec_error("bad_bindings", "bindings 必须是数组", where="bindings"))
    return errors


def validate_spec(spec: dict) -> list:
    errors: list = []
    if not isinstance(spec, dict):
        return ["spec 必须是对象"]
    kind = spec.get("kind", "keyframe_anim")
    if kind == "controlrig":
        return _validate_controlrig(spec)
    if kind != "keyframe_anim":
        errors.append(sp.spec_error("unknown_kind", f"未知动画 Spec kind：{kind!r}", where="kind"))
        return errors

    target = spec.get("target")
    if not isinstance(target, str) or not target.strip():
        errors.append(sp.spec_error("bad_target", "缺 target（/Game/.../SK_X.SK_X）",
                                    where="target"))
    bone = spec.get("bone")
    if not isinstance(bone, str) or not bone.strip():
        errors.append(sp.spec_error("bad_bone", "缺 bone（要打 key 的骨骼名）", where="bone"))

    channel = spec.get("channel")
    if channel not in CHANNELS:
        errors.append(sp.spec_error("unknown_channel", f"channel {channel!r} 不在白名单",
                                    where="channel", hint="可用：" + "/".join(CHANNELS)))

    lo = hi = None
    rng = spec.get("range")
    if not (isinstance(rng, (list, tuple)) and len(rng) == 2
            and all(isinstance(x, (int, float)) for x in rng)):
        errors.append(sp.spec_error("bad_range", "range 必须是 [t0,t1] 两个数字", where="range"))
    else:
        lo, hi = float(rng[0]), float(rng[1])
        if hi <= lo:
            errors.append(sp.spec_error("bad_range", "range 的 t1 必须大于 t0", where="range"))

    keys = spec.get("keys")
    if not isinstance(keys, list) or not keys:
        errors.append(sp.spec_error("no_keys", "keys 必须是非空数组", where="keys"))
        return errors
    prev = None
    for i, key in enumerate(keys):
        where = f"keys[{i}]"
        if not isinstance(key, dict):
            errors.append(sp.spec_error("bad_key", "关键帧必须是对象", where=where))
            continue
        t = key.get("t")
        if not isinstance(t, (int, float)) or isinstance(t, bool):
            errors.append(sp.spec_error("bad_key_time", f"{where} 缺数字 t", where=f"{where}.t"))
            continue
        if lo is not None and (float(t) < lo or float(t) > hi):
            errors.append(sp.spec_error(
                "key_out_of_range",
                f"{where} 的 t={t} 不在 range [{lo},{hi}] 内", where=f"{where}.t"))
        if prev is not None and float(t) <= prev:
            errors.append(sp.spec_error(
                "keys_not_increasing",
                f"{where} 的 t={t} 必须大于前一个 {prev}（关键帧按时间递增）",
                where=f"{where}.t"))
        prev = float(t)
        val = key.get("value")
        if not (isinstance(val, (list, tuple)) and len(val) == 3
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in val)):
            errors.append(sp.spec_error("bad_value", f"{where}.value 必须是 [x,y,z] 三个数字",
                                        where=f"{where}.value"))
    return errors


def extract_spec(step: dict) -> dict | None:
    if not isinstance(step, dict):
        return None
    for key in ("spec", "animation_spec"):
        if isinstance(step.get(key), dict):
            return step[key]
    return None


def write_task(step: dict) -> dict:
    save = bool((step or {}).get("save", True))
    return sp.make_write_task(_WRITE_ALLOW, save=save, pie=False, allow_key="allow_anim")


# ------------------------------------------------------------------ 执行 plan
def plan_ops(spec: dict) -> list[dict]:
    """确定性、幂等、无时间/随机的抽象原语清单。"""
    target = spec.get("target")
    bone = spec.get("bone")
    channel = spec.get("channel")
    ops: list[dict] = [{"op": "snapshot", "target": target, "bone": bone, "channel": channel}]
    for key in spec.get("keys") or []:
        ops.append({"op": "set_key", "target": target, "bone": bone, "channel": channel,
                    "t": key.get("t"), "value": key.get("value")})
    ops.append({"op": "save", "target": target})
    ops.append({"op": "readback", "target": target, "bone": bone, "channel": channel})
    return ops


def _result(ok: bool, *, tools=None, readback=None, error: str = "") -> dict:
    return {"ok": ok, "tools": list(tools or []), "readback": dict(readback or {}), "error": error}


def _chan_name(bone: str, channel: str) -> str:
    return f"{bone}.{channel}"


def _frame_of(t) -> int:
    """t(秒) → 整数帧。display rate 待 P0 describe：这里用 _DISPLAY_RATE 确定性换算。"""
    try:
        return int(round(float(t) * _DISPLAY_RATE))
    except (TypeError, ValueError):
        return 0


def _parse_keys(data) -> list[dict]:
    """把 get_keys 的返回解析成 [{t, value}]。空/坏返回 → 空表，绝不抛。"""
    rows = data
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except (ValueError, TypeError):
            return []
    if isinstance(rows, dict):
        rows = rows.get("keys") or rows.get("data") or []
    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        t = row.get("t")
        if t is None and row.get("frame") is not None:
            try:
                t = float(row["frame"]) / _DISPLAY_RATE
            except (TypeError, ValueError):
                t = None
        val = row.get("value")
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (ValueError, TypeError):
                pass
        out.append({"t": t, "value": val})
    return out


def exec_op(op: dict, call, write: dict) -> dict:
    """抽象原语 → 真实调用。snapshot/readback 容忍空 data 仍返回 ok。"""
    kind = op.get("op")
    target = op.get("target") or ""
    bone = op.get("bone") or ""
    channel = op.get("channel") or ""
    chan = _chan_name(bone, channel)
    package = str(target).split(".", 1)[0] if target else ""

    if kind == "snapshot":
        # 只拿基线（已有 key）；读不到/空 data 不阻断 —— 快照不是裁判。
        got = call(_KEY_TOOLSET, "get_keys",
                   {"section": {"refPath": target}, "channel_name": chan}, write)
        existing = _parse_keys(got.get("data")) if isinstance(got, dict) else []
        return _result(True, tools=["get_keys"], readback={"__existing__": {chan: existing}})

    if kind == "readback":
        got = call(_KEY_TOOLSET, "get_keys",
                   {"section": {"refPath": target}, "channel_name": chan}, write)
        keys = _parse_keys(got.get("data")) if isinstance(got, dict) else []
        return _result(True, tools=["get_keys"],
                       readback={"bone_keys": {bone: {channel: keys}}})

    if kind == "set_key":
        val = op.get("value")
        frame = _frame_of(op.get("t"))
        # 工具名 / t→frame / 向量通道映射「待 P0 describe」。最佳努力：
        # 标量 → add_key_float 单通道；[x,y,z] → 拆 X/Y/Z 三个浮点通道分别打 key。
        made: list[str] = []
        ok = True
        err = ""
        try:
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                r = call(_KEY_TOOLSET, "add_key_float",
                         {"section": {"refPath": target}, "channel_name": chan,
                          "frame": frame, "value": float(val)}, write)
                made.append("add_key_float")
                ok = ok and bool(isinstance(r, dict) and r.get("ok"))
            elif isinstance(val, (list, tuple)):
                comps = ("X", "Y", "Z")
                for i, comp in enumerate(list(val)[:3]):
                    r = call(_KEY_TOOLSET, "add_key_float",
                             {"section": {"refPath": target},
                              "channel_name": f"{chan}.{comps[i]}",
                              "frame": frame, "value": float(comp)}, write)
                    made.append("add_key_float")
                    ok = ok and bool(isinstance(r, dict) and r.get("ok"))
            else:
                r = call(_KEY_TOOLSET, "add_key_string",
                         {"section": {"refPath": target}, "channel_name": chan,
                          "frame": frame, "value": json.dumps(val, ensure_ascii=False)}, write)
                made.append("add_key_string")
                ok = ok and bool(isinstance(r, dict) and r.get("ok"))
        except Exception as exc:  # noqa: BLE001 —— 打 key 异常定位到 op
            return _result(False, tools=made, error=f"打 key 失败：{type(exc).__name__}: {exc}")
        if not ok:
            err = "打 key 失败"
        return _result(ok, tools=made, error=err)

    if kind == "save":
        r = call(_ASSET_TOOLSET, "save_assets", {"asset_paths": [package]}, write)
        ok = bool(isinstance(r, dict) and r.get("ok"))
        return _result(ok, tools=["save_assets"], readback={"saved": ok},
                       error="" if ok else "保存动画资产失败")

    return _result(False, error=f"未知原语 {kind!r}")


# ------------------------------------------------------------------ 只读复核
def assertions(spec: dict) -> list:
    out: list = [{"path": "saved", "op": "eq", "value": True}]
    bone = spec.get("bone")
    channel = spec.get("channel")
    keys = spec.get("keys") or []
    path = f"bone_keys.{bone}.{channel}"
    out.append({"path": path, "op": "count", "value": len(keys)})
    for key in keys:
        out.append({"path": path, "op": "contains",
                    "value": {"t": key.get("t"), "value": key.get("value")}})
    return out


def verify_spec(spec: dict, readback: dict) -> list:
    return sp.eval_assertions(assertions(spec), readback or {})


def summarize(spec: dict) -> str:
    keys = spec.get("keys") or []
    return (f"{spec.get('target')} 骨骼 {spec.get('bone')} 的 {spec.get('channel')} "
            f"打了 {len(keys)} 个关键帧，读回时间/值逐条一致并已保存")


# ------------------------------------------------------------------ 路由识别（宁缺毋滥）
_STRONG = ("动画", "关键帧", "骨骼动画", "挥手", "走跑循环", "走跑",
           "controlrig", "control rig", "控制绑定")
_RIG_RE = re.compile(r"(?<![a-z])rig(?![a-z])")
# 「动画蓝图」是动画蓝图域（animgraph）的地盘，不是本域（关键帧/ControlRig）的。命中即让位，
# 免得两个域同时认领同一句 → 登记表判歧义 → 交回模型（route 不唯一）。
_ANIMBP_RE = re.compile(r"(动画蓝图|animgraph|anim\s*graph|anim\s*blueprint|abp_|状态机)")


def recognize(text: str) -> bool:
    """一句话是否稳定属于动画 & ControlRig 域。命中强特征词或独立 'rig'。"""
    blob = str(text or "").lower()
    if not blob:
        return False
    if _ANIMBP_RE.search(blob):
        return False
    if any(tok.lower() in blob for tok in _STRONG):
        return True
    return bool(_RIG_RE.search(blob))


# ------------------------------------------------------------------ 对外入口
def build(step: dict, **kwargs) -> dict:
    """交付接口：controlrig 一律诚实暂停点名；keyframe 走完整闭环。"""
    spec = step.get("spec") if isinstance(step, dict) else None
    if not isinstance(spec, dict):
        spec = extract_spec(step)
    if isinstance(spec, dict) and str(spec.get("kind") or "keyframe_anim") == "controlrig":
        return sp.unsupported_result(
            _module(),
            "ControlRig rig 图接线复用蓝图图 kernel / 待 P0 工具确认，暂停点名")
    return sp.build_domain(_module(), step, **kwargs)
