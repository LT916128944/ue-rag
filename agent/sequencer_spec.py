# agent/sequencer_spec.py
"""Sequencer（关卡序列）域：Spec → 确定性执行 kernel → 只读复核（20261117 边界升级·序列域兜住）。

与 `agent/umg_spec.py` 同形的域模块（计划 §2/§3）：

  Spec 描述一个 LevelSequence：新建/打开序列 → 按 binding 绑 actor/相机 → 加 track →
  加 section/keyframe → 设播放范围 → 保存；随后**只读复核**轨道存在、绑定正确、
  关键帧时间/值读回==Spec、range 对。

先做四条常用轨：Transform / CameraCut / Float / Event（计划 §6）。表外 track type 结构化拒绝。

模型只产 Spec；脚本执行 + 复核；复核不过回灌模型改 Spec（≤2）；仍不过暂停。
顶层不 import 编辑器——`exec_op` 只用注入的 ``call``，离线守卫可完全替身。
"""
from __future__ import annotations

from agent import spec_pipeline as sp

KIND = "sequencer"
JUDGE_KIND = "editor_assert"

# track 类型白名单（计划 §6：先做这四条常用轨；表外一律结构化「不支持」）。
TRACK_TYPES: tuple[str, ...] = ("Transform", "CameraCut", "Float", "Event")

SPEC_HINT = (
    "Sequencer（关卡序列）Spec，JSON：{kind:'level_sequence', asset:'/Game/Cinematics/LS_X.LS_X',"
    " range:[start,end], bindings:[{actor, tracks:[{type, keys:[{t,...值}]}]}], verify:[...]}。"
    "track.type 只能取：" + "/".join(TRACK_TYPES) + "；range 必须 [start,end] 且 end>start；"
    "keys.t 必须落在 range 内；Transform key 用 loc:[x,y,z]，CameraCut key 用 on:true/false。"
)

# 写闸白名单：建/开序列、加绑定、加轨道、加 section/key、设 range、保存。
_WRITE_ALLOW = (
    "CreateLevelSequence", "OpenLevelSequence", "AddBinding", "AddTrack", "AddSection",
    "AddKeyframe", "SetPlaybackRange", "SetRange", "save_assets",
)

# toolset 家族名（计划 §1：animation_toolset.toolsets.sequencer.SequencerTools）。
_SEQ = "animation_toolset.toolsets.sequencer.SequencerTools"
_ASSET = "editor_toolset.toolsets.asset.AssetTools"

# key 里除 t 之外的取值字段（比较用：Transform->loc、CameraCut->on、Float->value、Event->name）。
_TIME_FIELD = "t"


def _package(ref: str) -> str:
    return str(ref or "").split(".", 1)[0]


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ------------------------------------------------------------------ 校验
def validate_spec(spec: dict) -> list:
    """Spec 结构 + 白名单 + range/关键帧越界校验（纯函数，返回结构化错误清单）。"""
    errors: list = []
    if not isinstance(spec, dict):
        return ["spec 必须是对象"]
    kind = spec.get("kind")
    if kind != "level_sequence":
        errors.append(sp.spec_error("unknown_kind", f"kind {kind!r} 不是 level_sequence",
                                    where="kind", hint="kind 只能是 level_sequence"))
        return errors

    asset = spec.get("asset")
    if not isinstance(asset, str) or not asset.strip():
        errors.append(sp.spec_error("bad_asset", "缺 asset（/Game/.../LS_X.LS_X）", where="asset"))
    elif not asset.startswith("/Game/"):
        errors.append(sp.spec_error("bad_asset", f"asset 必须是 /Game 路径：{asset}", where="asset"))

    start = end = None
    range_ok = False
    rng = spec.get("range")
    if not (isinstance(rng, (list, tuple)) and len(rng) == 2 and all(_is_number(x) for x in rng)):
        errors.append(sp.spec_error("bad_range", "range 必须是 [start,end] 两个数字", where="range"))
    else:
        start, end = float(rng[0]), float(rng[1])
        if end <= start:
            errors.append(sp.spec_error("bad_range", f"range 必须递增：start={start} 需小于 end={end}",
                                        where="range"))
        else:
            range_ok = True

    bindings = spec.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        errors.append(sp.spec_error("no_bindings", "bindings 必须是非空数组", where="bindings"))
        return errors

    for bi, b in enumerate(bindings):
        where = f"bindings[{bi}]"
        if not isinstance(b, dict):
            errors.append(sp.spec_error("bad_binding", "binding 必须是对象", where=where))
            continue
        actor = b.get("actor")
        actor_ok = isinstance(actor, str) and bool(actor.strip())
        if not actor_ok:
            errors.append(sp.spec_error("bad_binding", "binding 缺 actor", where=where))
        tracks = b.get("tracks")
        if not isinstance(tracks, list) or not tracks:
            errors.append(sp.spec_error("no_tracks", f"{actor if actor_ok else where} 没有 tracks",
                                        where=f"{where}.tracks"))
            continue
        for ti, tr in enumerate(tracks):
            tw = f"{where}.tracks[{ti}]"
            if not isinstance(tr, dict):
                errors.append(sp.spec_error("bad_track", "track 必须是对象", where=tw))
                continue
            typ = tr.get("type")
            if typ not in TRACK_TYPES:
                errors.append(sp.spec_error(
                    "bad_track_type", f"track 类型 {typ!r} 不在白名单", where=f"{tw}.type",
                    hint="可用：/" + "/".join(TRACK_TYPES)))
                continue
            keys = tr.get("keys")
            if not isinstance(keys, list):
                errors.append(sp.spec_error("bad_keys", f"{typ} track 缺 keys 数组", where=f"{tw}.keys"))
                continue
            for ki, k in enumerate(keys):
                kw = f"{tw}.keys[{ki}]"
                if not isinstance(k, dict):
                    errors.append(sp.spec_error("bad_key", "key 必须是对象", where=kw))
                    continue
                t = k.get(_TIME_FIELD)
                if not _is_number(t):
                    errors.append(sp.spec_error("bad_key_time", "key 缺数字 t", where=f"{kw}.t"))
                    continue
                if range_ok and not (start <= float(t) <= end):
                    errors.append(sp.spec_error(
                        "bad_key_time", f"key t={t} 不在 range [{start},{end}] 内", where=f"{kw}.t"))
    return errors


def extract_spec(step: dict) -> dict | None:
    if not isinstance(step, dict):
        return None
    for key in ("spec", "sequencer_spec"):
        if isinstance(step.get(key), dict):
            return step[key]
    return None


def write_task(step: dict) -> dict:
    save = bool((step or {}).get("save", True))
    return sp.make_write_task(_WRITE_ALLOW, save=save, pie=False, allow_key="allow_anim")


# ------------------------------------------------------------------ 执行 plan
def plan_ops(spec: dict) -> list[dict]:
    """确定性、幂等、无时间/随机的抽象原语清单。"""
    asset = spec.get("asset")
    ops: list[dict] = [{"op": "ensure_sequence", "target": asset}]
    for b in spec.get("bindings") or []:
        actor = b.get("actor")
        ops.append({"op": "add_binding", "target": asset, "actor": actor})
        for tr in b.get("tracks") or []:
            typ = tr.get("type")
            ops.append({"op": "add_track", "target": asset, "actor": actor, "type": typ})
            for k in tr.get("keys") or []:
                ops.append({"op": "add_key", "target": asset, "actor": actor, "type": typ, "key": k})
    rng = spec.get("range") or [0, 0]
    ops.append({"op": "set_range", "target": asset, "start": rng[0], "end": rng[1]})
    ops.append({"op": "save", "target": asset})
    ops.append({"op": "readback", "target": asset})
    return ops


def _result(ok: bool, *, tools=None, readback=None, error: str = "") -> dict:
    return {"ok": ok, "tools": list(tools or []), "readback": dict(readback or {}), "error": error}


def _sequence_readback(data) -> dict:
    """把序列读回（或空 data）归一成 {range:{start,end}, bindings:{actor:{exists,tracks:{type:{exists,keys}}}}}。"""
    d = data if isinstance(data, dict) else {}
    rng = d.get("range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        rng = {"start": rng[0], "end": rng[1]}
    elif not isinstance(rng, dict):
        rng = {}

    raw_binds = d.get("bindings")
    if isinstance(raw_binds, dict):
        b_items = list(raw_binds.items())
    elif isinstance(raw_binds, list):
        b_items = [(str(row.get("actor") or ""), row)
                   for row in raw_binds if isinstance(row, dict)]
    else:
        b_items = []

    binds_out: dict = {}
    for actor, row in b_items:
        tracks_out: dict = {}
        raw_tracks = row.get("tracks") if isinstance(row, dict) else None
        if isinstance(raw_tracks, dict):
            t_items = list(raw_tracks.items())
        elif isinstance(raw_tracks, list):
            t_items = [(str(t.get("type") or ""), t) for t in raw_tracks if isinstance(t, dict)]
        else:
            t_items = []
        for typ, trow in t_items:
            keys = trow.get("keys") if isinstance(trow, dict) else []
            tracks_out[typ] = {"exists": True, "keys": list(keys or [])}
        binds_out[actor] = {"exists": True, "tracks": tracks_out}
    return {"range": rng, "bindings": binds_out}


def exec_op(op: dict, call, write: dict) -> dict:
    """抽象原语 → 真实调用（经注入的 ``call``；只读原语容忍空 data 仍返回 ok）。

    工具名按计划 §1 toolset 家族（SequencerTools · 140 个动作不枚举）；具体动作名拿不准处
    标「待 P0 describe」。
    """
    kind = op.get("op")
    asset = op.get("target") or ""
    if kind == "ensure_sequence":
        # 待 P0 describe：建/开序列动作名（SequencerTools 动作名未实锤）。
        r = call(_SEQ, "CreateLevelSequence", {"assetPath": asset}, write)
        return _result(bool(r.get("ok")), tools=["CreateLevelSequence"],
                       error=str(r.get("error") or "建/开序列失败"))
    if kind == "add_binding":
        r = call(_SEQ, "AddBinding", {
            "sequence": asset, "actor": op.get("actor"),
        }, write)  # 待 P0 describe
        return _result(bool(r.get("ok")), tools=["AddBinding"],
                       error=str(r.get("error") or "加绑定失败"))
    if kind == "add_track":
        r = call(_SEQ, "AddTrack", {
            "sequence": asset, "binding": op.get("actor"), "trackType": op.get("type"),
        }, write)  # 待 P0 describe
        return _result(bool(r.get("ok")), tools=["AddTrack"],
                       error=str(r.get("error") or f"加 {op.get('type')} 轨道失败"))
    if kind == "add_key":
        r = call(_SEQ, "AddKeyframe", {
            "sequence": asset, "binding": op.get("actor"),
            "trackType": op.get("type"), "key": op.get("key"),
        }, write)  # 待 P0 describe
        return _result(bool(r.get("ok")), tools=["AddKeyframe"],
                       error=str(r.get("error") or "加关键帧失败"))
    if kind == "set_range":
        r = call(_SEQ, "SetPlaybackRange", {
            "sequence": asset, "start": op.get("start"), "end": op.get("end"),
        }, write)  # 待 P0 describe
        return _result(bool(r.get("ok")), tools=["SetPlaybackRange"],
                       error=str(r.get("error") or "设播放范围失败"))
    if kind == "save":
        r = call(_ASSET, "save_assets", {"asset_paths": [_package(asset)]}, write)
        return _result(bool(r.get("ok")), tools=["save_assets"],
                       readback={"saved": bool(r.get("ok"))},
                       error=str(r.get("error") or "保存失败"))
    if kind == "readback":
        r = call(_SEQ, "GetSequenceInfo", {"sequence": asset}, write)  # 待 P0 describe
        if isinstance(r, dict) and not r.get("ok"):
            return _result(False, tools=["GetSequenceInfo"],
                           error=str(r.get("error") or "复核读不到序列"))
        data = r.get("data") if isinstance(r, dict) else None
        return _result(True, tools=["GetSequenceInfo"], readback=_sequence_readback(data))
    return _result(False, error=f"未知原语 {kind!r}")


# ------------------------------------------------------------------ 只读复核
def assertions(spec: dict) -> list:
    """期望：range 对、每 binding/轨道存在、关键帧时间/值读回==Spec、saved。"""
    rng = spec.get("range") or [0, 0]
    out: list = [
        {"path": "range.start", "op": "eq", "value": rng[0]},
        {"path": "range.end", "op": "eq", "value": rng[1]},
    ]
    for b in spec.get("bindings") or []:
        actor = b.get("actor")
        out.append({"path": f"bindings.{actor}.exists", "op": "eq", "value": True})
        for tr in b.get("tracks") or []:
            typ = tr.get("type")
            base = f"bindings.{actor}.tracks.{typ}"
            out.append({"path": base + ".exists", "op": "eq", "value": True})
            keys = tr.get("keys") or []
            out.append({"path": base + ".keys", "op": "count", "value": len(keys)})
            for i, k in enumerate(keys):
                for field, val in k.items():
                    out.append({"path": f"{base}.keys.{i}.{field}", "op": "eq", "value": val})
    out.append({"path": "saved", "op": "eq", "value": True})
    return out


def verify_spec(spec: dict, readback: dict) -> list:
    # 不补默认值：空 readback 上必须至少一条断言不过（诚实失败，不假成功）。
    return sp.eval_assertions(assertions(spec), readback or {})


def summarize(spec: dict) -> str:
    bindings = spec.get("bindings") or []
    n_tracks = sum(len(b.get("tracks") or []) for b in bindings)
    rng = spec.get("range") or [0, 0]
    return (f"{spec.get('asset')} 落地 {len(bindings)} 个绑定 / {n_tracks} 条轨道，"
            f"range {rng[0]}..{rng[1]}，已保存并复核通过")


# ------------------------------------------------------------------ 路由识别（宁缺毋滥）
_STRONG = ("过场", "序列", "sequencer", "levelsequence", "ls_", "相机推进", "相机切换",
           "cameracut", "轨道", "关键帧")


def recognize(text: str) -> bool:
    """一句话是否稳定属于 Sequencer 域。命中强特征词才算。"""
    blob = str(text or "").lower()
    if not blob:
        return False
    return any(tok.lower() in blob for tok in _STRONG)


# ------------------------------------------------------------------ 对外入口
def build(step: dict, **kwargs) -> dict:
    """交付接口：跑完整闭环。默认无 fix_model（离线/直接执行）；调用方可注入修复口。"""
    return sp.build_domain(_module(), step, **kwargs)


def _module():
    import sys

    return sys.modules[__name__]
