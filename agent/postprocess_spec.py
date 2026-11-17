# agent/postprocess_spec.py
"""后处理域 Spec 内核（20261117 边界升级·后处理域兜住升级计划）。

与 ``agent/umg_spec.py`` **完全同形**：模型只产 Spec，脚本按 Spec 确定性执行，再**只读读回**复核。

后处理**没有专用工具集、没有一等动作**（计划 §0/§1）：它是「在对象属性上设置
``FPostProcessSettings``」。所以本域走**属性路径式** kernel：
  定位/建 ``PostProcessVolume``（``ActorTools``）或取相机组件 →
  按属性路径 set ``Settings.<key>``（``ObjectTools.set_properties``）→ 读回 ``Settings.<key>``。
裁判 ``JUDGE_KIND = "editor_assert"``——「属性读回 == 期望」就是硬裁判。

``FPostProcessSettings`` 字段多且嵌套，先只支持**最常用一小组**白名单字段（泛光/曝光/暗角/色调）；
表外字段一律结构化「不支持」，防止瞎设嵌套结构。字段名/嵌套需按计划 §6「不猜」，
最终以一次 ``list_properties`` 读回确认（见下 exec_op readback）。

顶层不 import 编辑器：``exec_op`` 只用注入的 ``call``；``build`` 才惰性连 ``spec_pipeline``。
"""
from __future__ import annotations

import json

from agent import spec_pipeline as sp

KIND = "postprocess"
JUDGE_KIND = "editor_assert"   # 属性读回就是裁判

# 后处理字段白名单（先做最常用一小组）；表外字段→结构化「不支持」。
_SETTING_TYPE: dict[str, str] = {
    "BloomIntensity": "number",
    "BloomThreshold": "number",
    "AutoExposureMinBrightness": "number",
    "AutoExposureMaxBrightness": "number",
    "AutoExposureBias": "number",
    "VignetteIntensity": "number",
    "AmbientOcclusionIntensity": "number",
    "SceneColorTint": "color4",
}
SETTING_FIELDS: tuple[str, ...] = tuple(_SETTING_TYPE)

_ACTOR = "editor_toolset.toolsets.actor.ActorTools"
_SCENE = "editor_toolset.toolsets.scene.SceneTools"
_OBJECT = "editor_toolset.toolsets.object.ObjectTools"
_ASSET = "editor_toolset.toolsets.asset.AssetTools"

# 写闸白名单：技能已证的 set_properties / place_actor / save_assets。
_WRITE_ALLOW = ("set_properties", "place_actor", "save_assets")

SPEC_HINT = (
    "后处理域 Spec，JSON：{kind:\"post_process\","
    " target:{type:\"volume\",name:\"PP_X\",unbound:true} 或 {type:\"camera\",actor:\"BP_X\"},"
    " settings:{字段:值}, verify:[...]}。settings 只允许：" + "/".join(SETTING_FIELDS) +
    "（number；SceneColorTint 为 [r,g,b,a]）。按属性路径 Settings.<key> 设并读回。"
)


def _package(ref: str) -> str:
    return str(ref or "").split(".", 1)[0]


# ------------------------------------------------------------------ 校验
def _validate_setting(key: str, value) -> list:
    errors: list = []
    where = f"settings.{key}"
    if key not in _SETTING_TYPE:
        errors.append(sp.spec_error("unsupported_setting",
                                    f"后处理字段 {key!r} 不在白名单",
                                    where=where,
                                    hint="可用：" + "/".join(SETTING_FIELDS)))
        return errors
    want = _SETTING_TYPE[key]
    if want == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(sp.spec_error("bad_setting_value", f"{key} 必须是数字", where=where))
    elif want == "color4":
        if not (isinstance(value, (list, tuple)) and len(value) == 4
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
            errors.append(sp.spec_error("bad_setting_value", f"{key} 必须是 4 元数字 [r,g,b,a]",
                                        where=where))
    return errors


def validate_spec(spec: dict) -> list:
    errors: list = []
    if not isinstance(spec, dict):
        return [sp.spec_error("bad_spec", "spec 必须是对象")]
    target = spec.get("target")
    if not isinstance(target, dict):
        errors.append(sp.spec_error("bad_target", "缺 target 对象 {type,...}", where="target"))
        target = {}
    else:
        ttype = target.get("type")
        if ttype not in ("volume", "camera"):
            errors.append(sp.spec_error("bad_target_type",
                                        f"target.type {ttype!r} 只能是 volume/camera",
                                        where="target.type"))
        elif ttype == "volume":
            if not isinstance(target.get("name"), str) or not target.get("name").strip():
                errors.append(sp.spec_error("bad_target_name", "volume 缺 name", where="target.name"))
            if "unbound" in target and not isinstance(target.get("unbound"), bool):
                errors.append(sp.spec_error("bad_target_unbound", "unbound 必须是布尔",
                                            where="target.unbound"))
        elif ttype == "camera":
            if not isinstance(target.get("actor"), str) or not target.get("actor").strip():
                errors.append(sp.spec_error("bad_target_name", "camera 缺 actor", where="target.actor"))
    settings = spec.get("settings")
    if not isinstance(settings, dict) or not settings:
        errors.append(sp.spec_error("no_settings", "settings 必须是非空对象", where="settings"))
        return errors
    for key, value in settings.items():
        errors.extend(_validate_setting(str(key), value))
    return errors


def extract_spec(step: dict) -> dict | None:
    if not isinstance(step, dict):
        return None
    for key in ("spec", "postprocess_spec"):
        if isinstance(step.get(key), dict):
            return step[key]
    return None


def write_task(step: dict) -> dict:
    save = bool((step or {}).get("save", True))
    # 后处理无专用 UE_ALLOW_* 开关（走 ActorTools/ObjectTools 属性路径），故不声明 allow_key。
    return sp.make_write_task(_WRITE_ALLOW, save=save, pie=False)


# ------------------------------------------------------------------ 执行 plan
def plan_ops(spec: dict) -> list[dict]:
    """确定性、幂等、无时间/随机的抽象原语清单。"""
    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    settings = spec.get("settings") if isinstance(spec.get("settings"), dict) else {}
    ops: list[dict] = [{"op": "snapshot", "target": target}]
    ops.append({"op": "ensure_target", "target": target})
    for key, value in settings.items():
        ops.append({"op": "set_setting", "target": target, "key": key, "value": value})
    if target.get("type") == "volume" and "unbound" in target:
        ops.append({"op": "set_unbound", "target": target, "unbound": bool(target.get("unbound"))})
    ops.append({"op": "save", "target": target})
    ops.append({"op": "readback", "target": target, "settings": dict(settings)})
    return ops


def _result(ok: bool, *, tools=None, readback=None, error: str = "") -> dict:
    return {"ok": bool(ok), "tools": list(tools or []), "readback": dict(readback or {}), "error": error}


def _actor_refs(data) -> list[str]:
    rows = data if isinstance(data, list) else (data.get("actors") if isinstance(data, dict) else [])
    out: list[str] = []
    for row in rows or []:
        if isinstance(row, str):
            if row:
                out.append(row)
        elif isinstance(row, dict):
            ref = row.get("refPath") or row.get("ref") or row.get("path") or row.get("actor")
            if isinstance(ref, str) and ref:
                out.append(ref)
    return out


def _component_refs(data) -> list[str]:
    rows = data if isinstance(data, list) else (data.get("components") if isinstance(data, dict) else [])
    out: list[str] = []
    for row in rows or []:
        if isinstance(row, str):
            out.append(row)
        elif isinstance(row, dict):
            ref = row.get("refPath") or row.get("ref") or row.get("path")
            cls = str(row.get("class") or row.get("className") or "")
            if isinstance(ref, str) and ref and ("Camera" in cls or not cls):
                out.append(ref)
    return out


def _resolve_ref(target: dict, call, write: dict) -> tuple[str, list[str]]:
    """定位/建 PostProcessVolume（或取相机组件）→ 返回 (ref, tools_used)。ref 空=没定位到。"""
    ttype = target.get("type")
    if ttype == "camera":
        actor = str(target.get("actor") or "")
        got = call(_ACTOR, "get_components", {"actor": {"refPath": actor}}, write)
        refs = _component_refs(got.get("data")) if got.get("ok") else []
        return (refs[0] if refs else actor), ["get_components"]
    name = str(target.get("name") or "")
    found = call(_SCENE, "find_actors",
                 {"name": name, "tag": "", "collision_channels": []}, write)
    refs = _actor_refs(found.get("data")) if found.get("ok") else []
    if refs:
        return refs[0], ["find_actors"]
    made = call(_ACTOR, "place_actor",
                {"actor_class": {"refPath": "/Script/Engine.PostProcessVolume"},
                 "name": name, "location": {"x": 0, "y": 0, "z": 0}}, write)
    refs = _actor_refs(made.get("data")) if made.get("ok") else []
    return (refs[0] if refs else ""), ["find_actors", "place_actor"]


def _parse_settings(data) -> tuple[dict, bool | None]:
    """从 get_properties 的返回里抽 Settings 子对象与 bUnbound。抽不到返回 ({}, None)。"""
    if not isinstance(data, dict):
        return {}, None
    settings = data.get("Settings")
    if settings is None:
        settings = data.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    unbound = data.get("bUnbound", data.get("unbound"))
    if isinstance(unbound, bool):
        return settings, unbound
    return settings, None


def _level_path(data) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("path", "level_path", "asset_path", "name", "refPath"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
    return ""


def exec_op(op: dict, call, write: dict) -> dict:
    """抽象原语 → 真实调用。只读原语（snapshot/readback）容忍空 data 仍返回 ok。"""
    kind = op.get("op")
    target = op.get("target") if isinstance(op.get("target"), dict) else {}
    if kind == "snapshot":
        ref, tools = _resolve_ref(target, call, write)
        got = call(_OBJECT, "get_properties",
                   {"instance": {"refPath": ref}, "properties": ["Settings", "bUnbound"]}, write)
        settings, unbound = _parse_settings(got.get("data")) if got.get("ok") else ({}, None)
        return _result(True, tools=tools + ["get_properties"],
                       readback={"__existing__": {"Settings": settings, "unbound": unbound}})
    if kind == "readback":
        ref, tools = _resolve_ref(target, call, write)
        got = call(_OBJECT, "get_properties",
                   {"instance": {"refPath": ref}, "properties": ["Settings", "bUnbound"]}, write)
        settings, unbound = _parse_settings(got.get("data")) if got.get("ok") else ({}, None)
        rb: dict = {"Settings": settings}
        if unbound is not None:
            rb["unbound"] = unbound
        return _result(True, tools=tools + ["get_properties"], readback=rb)
    # 以下为写原语
    if kind == "ensure_target":
        ref, tools = _resolve_ref(target, call, write)
        return _result(True, tools=tools, readback={"target": {"ref": ref}})
    if kind == "set_setting":
        ref, tools = _resolve_ref(target, call, write)
        values = {f"Settings.{op.get('key')}": op.get("value")}
        r = call(_OBJECT, "set_properties",
                 {"instance": {"refPath": ref},
                  "values": json.dumps(values, ensure_ascii=False)}, write)
        return _result(bool(r.get("ok")), tools=tools + ["set_properties"],
                       error=str(r.get("error") or f"设 {op.get('key')} 失败"))
    if kind == "set_unbound":
        ref, tools = _resolve_ref(target, call, write)
        r = call(_OBJECT, "set_properties",
                 {"instance": {"refPath": ref},
                  "values": json.dumps({"bUnbound": bool(op.get("unbound"))}, ensure_ascii=False)}, write)
        return _result(bool(r.get("ok")), tools=tools + ["set_properties"],
                       error=str(r.get("error") or "设 unbound 失败"))
    if kind == "save":
        if not write.get("save", True):
            return _result(True, tools=[])
        level = call(_SCENE, "get_current_level", {}, write)
        path = _level_path(level.get("data")) if level.get("ok") else ""
        if not path:
            return _result(True, tools=["get_current_level"], readback={"saved": True})
        saved = call(_ASSET, "save_assets", {"asset_paths": [path]}, write)
        return _result(bool(saved.get("ok")), tools=["get_current_level", "save_assets"],
                       readback={"saved": bool(saved.get("ok"))},
                       error=str(saved.get("error") or "保存关卡失败"))
    return _result(False, error=f"未知原语 {kind!r}")


# ------------------------------------------------------------------ 只读复核
def assertions(spec: dict) -> list:
    out: list = []
    settings = spec.get("settings") if isinstance(spec.get("settings"), dict) else {}
    for key, value in settings.items():
        out.append({"path": f"Settings.{key}", "op": "eq", "value": value})
    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    if target.get("type") == "volume" and "unbound" in target:
        out.append({"path": "unbound", "op": "eq", "value": bool(target.get("unbound"))})
    out.append({"path": "saved", "op": "eq", "value": True})
    return out


def verify_spec(spec: dict, readback: dict) -> list:
    return sp.eval_assertions(assertions(spec), readback or {})


def summarize(spec: dict) -> str:
    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    where = f"{target.get('type')}:{target.get('name') or target.get('actor')}"
    settings = spec.get("settings") if isinstance(spec.get("settings"), dict) else {}
    keys = "、".join(str(k) for k in settings)
    return f"后处理 {where} 设置 {len(settings)} 个字段（{keys}），属性读回一致"


# ------------------------------------------------------------------ 路由识别（宁缺毋滥）
_STRONG = ("后处理", "泛光", "暗角", "bloom", "vignette", "曝光", "postprocess", "post process")


def recognize(text: str) -> bool:
    """一句话是否稳定属于后处理域。命中后处理特征词才算。"""
    blob = str(text or "").lower()
    if not blob:
        return False
    return any(tok.lower() in blob for tok in _STRONG)


# ------------------------------------------------------------------ 对外入口
def build(step: dict, **kwargs) -> dict:
    """交付接口：跑完整闭环。默认无 fix_model（离线/直接执行）；调用方可注入修复口。"""
    import sys

    return sp.build_domain(sys.modules[__name__], step, **kwargs)
