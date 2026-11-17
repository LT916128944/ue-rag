# agent/material_spec.py
"""材质域 Spec 内核（20261117 边界升级·材质域兜住升级计划）。

与 ``agent/umg_spec.py`` **完全同形**：模型只产 Spec，脚本按 Spec 确定性执行
（``MaterialInstanceTools`` 设 scalar/vector/texture 参数），再**参数读回**做只读复核；
复核不过回灌模型改 Spec（≤2），仍不过暂停点名。裁判 ``JUDGE_KIND = "editor_assert"``
——「参数读回 == 期望」本身就是硬裁判，**不再借用 blueprint_compile**。

两种 Spec 形态（见计划 §3）：
- 形态 A（已落地）：``kind == "material_instance"``，覆写材质实例参数。
- 形态 B（**今日走人工**）：``kind == "material_graph"``，材质图节点/连线。
  ``aiagent_material_toolset.py`` 已证实引擎侧没有材质图连线的 MCP 一等原语，按约定走人工。
  所以本模块**绝不伪造连线工具调用**：``build`` 收到 material_graph 直接
  ``sp.unsupported_result`` 结构化暂停点名。将来补了原语再在这里加节点/连线 kernel。

顶层不 import 编辑器：``exec_op`` 只用注入的 ``call``；``build`` 里才惰性连
``spec_pipeline.build_domain``。这样离线守卫能在完全不碰编辑器的情况下钉死内核。
"""
from __future__ import annotations

import json

from agent import spec_pipeline as sp

KIND = "material"
JUDGE_KIND = "editor_assert"   # 参数读回就是裁判（区别于 umg 的 blueprint_compile）

# 参数类型白名单（表外 type 一律结构化「不支持」）。
PARAM_TYPES: tuple[str, ...] = ("scalar", "vector", "texture")

# 各类型对应的 MaterialInstanceTools 写/读工具（裸名；toolset 见 _MI）。
_SET_TOOL = {"scalar": "set_scalar_parameter", "vector": "set_vector_parameter",
             "texture": "set_texture_parameter"}
_GET_TOOL = {"scalar": "get_scalar_parameter", "vector": "get_vector_parameter",
             "texture": "get_texture_parameter"}

_MI = "editor_toolset.toolsets.material_instance.MaterialInstanceTools"
_ASSET = "editor_toolset.toolsets.asset.AssetTools"

# 写闸白名单：覆写 MI 参数 + 保存资产。
_WRITE_ALLOW = ("set_scalar_parameter", "set_vector_parameter", "set_texture_parameter",
                "save_assets")

SPEC_HINT = (
    "材质域 Spec，JSON：{kind:\"material_instance\", target:/Game/.../MI_X.MI_X,"
    " params:[{name,type,value}], verify:[...]}。type 只能取：" + "/".join(PARAM_TYPES) +
    "；scalar 的 value 是数字，vector 是 [r,g,b,a] 四元数字，texture 是 /Game 路径。"
    "（材质图节点/连线 kind:\"material_graph\" 今日无 MCP 原语，走人工、结构化暂停。）"
)


def _package(ref: str) -> str:
    """``/Game/A/B.B`` → ``/Game/A/B``（保存资产用包路径）。"""
    return str(ref or "").split(".", 1)[0]


# ------------------------------------------------------------------ 校验
def _validate_param(i: int, param: dict) -> list:
    errors: list = []
    where = f"params[{i}]"
    if not isinstance(param, dict):
        return [sp.spec_error("bad_param", "参数项必须是对象 {name,type,value}", where=where)]
    name = param.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(sp.spec_error("bad_param_name", "参数缺 name", where=f"{where}.name"))
    typ = param.get("type")
    if typ not in PARAM_TYPES:
        errors.append(sp.spec_error("unknown_param_type",
                                    f"参数类型 {typ!r} 不在白名单 {PARAM_TYPES}",
                                    where=f"{where}.type",
                                    hint="可用 scalar/vector/texture"))
        return errors
    value = param.get("value")
    if typ == "scalar":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(sp.spec_error("bad_scalar_value", "scalar 的 value 必须是数字",
                                        where=f"{where}.value"))
    elif typ == "vector":
        if not (isinstance(value, (list, tuple)) and len(value) == 4
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
            errors.append(sp.spec_error("bad_vector_value", "vector 的 value 必须是 4 元数字 [r,g,b,a]",
                                        where=f"{where}.value"))
    elif typ == "texture":
        if not (isinstance(value, str) and value.strip().startswith("/Game")):
            errors.append(sp.spec_error("bad_texture_value", "texture 的 value 必须是 /Game 路径",
                                        where=f"{where}.value"))
    return errors


def validate_spec(spec: dict) -> list:
    errors: list = []
    if not isinstance(spec, dict):
        return [sp.spec_error("bad_spec", "spec 必须是对象")]
    kind = spec.get("kind")
    if kind not in ("material_instance", "material_graph"):
        errors.append(sp.spec_error("unknown_kind",
                                    f"kind {kind!r} 不是材质域可识别形态",
                                    where="kind", hint="可用 material_instance / material_graph"))
        return errors
    target = spec.get("target")
    if not isinstance(target, str) or not target.strip():
        errors.append(sp.spec_error("bad_target", "缺 target（/Game/.../MI_X.MI_X）",
                                    where="target"))
    if kind == "material_graph":
        # 形态 B 今日走人工：只做最轻的结构校验，交给 build 结构化暂停。
        nodes = spec.get("nodes")
        if nodes is not None and not isinstance(nodes, list):
            errors.append(sp.spec_error("bad_nodes", "nodes 必须是数组", where="nodes"))
        edges = spec.get("edges")
        if edges is not None and not isinstance(edges, list):
            errors.append(sp.spec_error("bad_edges", "edges 必须是数组", where="edges"))
        return errors
    params = spec.get("params")
    if not isinstance(params, list) or not params:
        errors.append(sp.spec_error("no_params", "params 必须是非空数组", where="params"))
        return errors
    seen: list[str] = []
    for i, param in enumerate(params):
        errors.extend(_validate_param(i, param))
        if isinstance(param, dict) and isinstance(param.get("name"), str):
            nm = param["name"]
            if nm in seen:
                errors.append(sp.spec_error("dup_param", f"参数名重复：{nm}", where=f"params[{i}].name"))
            seen.append(nm)
    return errors


def extract_spec(step: dict) -> dict | None:
    if not isinstance(step, dict):
        return None
    for key in ("spec", "material_spec"):
        if isinstance(step.get(key), dict):
            return step[key]
    return None


def write_task(step: dict) -> dict:
    save = bool((step or {}).get("save", True))
    return sp.make_write_task(_WRITE_ALLOW, save=save, pie=False, allow_key="allow_material")


# ------------------------------------------------------------------ 执行 plan
def plan_ops(spec: dict) -> list[dict]:
    """确定性、幂等、无时间/随机的抽象原语清单。"""
    target = spec.get("target")
    params = list(spec.get("params") or [])
    ops: list[dict] = [{"op": "snapshot", "target": target}]
    for param in params:
        ops.append({"op": "set_param", "target": target, "param": param})
    ops.append({"op": "save", "target": target})
    ops.append({"op": "readback", "target": target, "params": params})
    return ops


def _result(ok: bool, *, tools=None, readback=None, error: str = "") -> dict:
    return {"ok": bool(ok), "tools": list(tools or []), "readback": dict(readback or {}), "error": error}


def _extract(data, typ: str):
    """从 get_*_parameter 的返回 data 里抽值；抽不到返回 None（不抛、不猜）。"""
    value = data
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            pass
    if typ == "scalar":
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            for key in ("returnValue", "value", "scalar", "Value"):
                inner = value.get(key)
                if isinstance(inner, bool):
                    continue
                if isinstance(inner, (int, float)):
                    return float(inner)
        if isinstance(value, str):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        return None
    if typ == "vector":
        cand = None
        if isinstance(value, (list, tuple)):
            cand = value
        elif isinstance(value, dict):
            for key in ("returnValue", "value", "vector", "Value"):
                inner = value.get(key)
                if isinstance(inner, (list, tuple)):
                    cand = inner
                    break
        if isinstance(cand, (list, tuple)) and len(cand) == 4:
            try:
                return [float(v) for v in cand]
            except (TypeError, ValueError):
                return None
        return None
    if typ == "texture":
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for key in ("returnValue", "value", "path", "texture", "Value"):
                inner = value.get(key)
                if isinstance(inner, str) and inner:
                    return inner
        return None
    return None


def exec_op(op: dict, call, write: dict) -> dict:
    """抽象原语 → 真实调用。只读原语（snapshot/readback）容忍空 data 仍返回 ok。"""
    kind = op.get("op")
    ref = op.get("target") or ""
    if kind == "snapshot":
        got = call(_MI, "list_parameters", {"instance": {"refPath": ref}}, write)
        existing = got.get("data") if got.get("ok") else {}
        return _result(True, tools=["list_parameters"], readback={"__existing__": existing or {}})
    if kind == "readback":
        params: dict = {}
        for param in op.get("params") or []:
            if not isinstance(param, dict):
                continue
            name = param.get("name")
            typ = param.get("type")
            tool = _GET_TOOL.get(typ)
            if not tool or not isinstance(name, str):
                continue
            got = call(_MI, tool, {"instance": {"refPath": ref}, "name": name}, write)
            value = _extract(got.get("data"), typ) if got.get("ok") else None
            if value is not None:
                params[name] = value
        return _result(True, tools=[t for t in _GET_TOOL.values()], readback={"params": params})
    # 以下为写原语
    if kind == "set_param":
        param = op.get("param") or {}
        typ = param.get("type")
        tool = _SET_TOOL.get(typ)
        if not tool:
            return _result(False, error=f"未知参数类型 {typ!r}（不该走到这里，validate 已拦）")
        r = call(_MI, tool, {"instance": {"refPath": ref}, "name": param.get("name"),
                             "value": param.get("value")}, write)
        return _result(bool(r.get("ok")), tools=[tool],
                       error=str(r.get("error") or f"设参数 {param.get('name')} 失败"))
    if kind == "save":
        if not write.get("save", True):
            return _result(True, tools=[])
        r = call(_ASSET, "save_assets", {"asset_paths": [_package(ref)]}, write)
        return _result(bool(r.get("ok")), tools=["save_assets"],
                       error=str(r.get("error") or "保存失败"))
    return _result(False, error=f"未知原语 {kind!r}")


# ------------------------------------------------------------------ 只读复核
def assertions(spec: dict) -> list:
    out: list = []
    for param in spec.get("params") or []:
        if isinstance(param, dict) and isinstance(param.get("name"), str):
            out.append({"path": f"params.{param['name']}", "op": "eq", "value": param.get("value")})
    return out


def verify_spec(spec: dict, readback: dict) -> list:
    return sp.eval_assertions(assertions(spec), readback or {})


def summarize(spec: dict) -> str:
    params = spec.get("params") or []
    names = "、".join(str(p.get("name")) for p in params if isinstance(p, dict))
    return f"{spec.get('target')} 覆写 {len(params)} 个材质实例参数（{names}），参数读回一致"


# ------------------------------------------------------------------ 路由识别（宁缺毋滥）
_STRONG = ("材质实例", "mi_", "材质参数", "基础色", "粗糙度", "自发光", "material")


def recognize(text: str) -> bool:
    """一句话是否稳定属于材质域。命中材质特征词才算；纯场景/编译/UMG 不算。"""
    blob = str(text or "").lower()
    if not blob:
        return False
    return any(tok.lower() in blob for tok in _STRONG)


# ------------------------------------------------------------------ 对外入口
def build(step: dict, **kwargs) -> dict:
    """交付接口。material_graph 今日无 MCP 原语 ⇒ 结构化暂停点名（不伪造连线）。"""
    import sys

    module = sys.modules[__name__]
    spec = step.get("spec") if isinstance(step, dict) else None
    if not isinstance(spec, dict):
        spec = extract_spec(step) if isinstance(step, dict) else None
    if isinstance(spec, dict) and spec.get("kind") == "material_graph":
        return sp.unsupported_result(
            module,
            "材质图节点/连线今日无 MCP 原语，走人工（见 aiagent_material_toolset 注释）",
        )
    return sp.build_domain(module, step, **kwargs)
