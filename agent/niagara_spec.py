# agent/niagara_spec.py
"""Niagara 粒子域：Spec → 确定性执行 kernel → 只读复核（20261117 边界升级·粒子域兜住）。

与 `agent/umg_spec.py` 同形的域模块。分两级（计划 §2/§3）：

  - **A 级** ``kind=="niagara_component"``：在对象上加 NiagaraComponent，引用已有 System
    并设 User 参数。低垂果实，今天脚本侧能做、能读回复核。
  - **B 级** ``kind=="niagara_system"``：建/改 System 资产（发射器/模块/渲染器/模块栈）。
    模块栈 schema 深且版本相关（计划 §6），今日 **结构化暂停点名**，不伪造模块栈工具调用：
    ``build`` 对 niagara_system 直接返回 `sp.unsupported_result(...)`（paused=True）。

模型只产 Spec（见 SPEC_HINT）；脚本执行 + 只读复核；复核不过回灌模型改 Spec（≤2）；仍不过暂停。
顶层不 import 编辑器——`exec_op` 只用注入的 ``call``，离线守卫可完全替身。
"""
from __future__ import annotations

from agent import spec_pipeline as sp

KIND = "niagara"
JUDGE_KIND = "editor_assert"

# User 参数 type 白名单（计划 §2 A 级：{scalar,vector,color}；表外一律结构化「不支持」）。
USER_PARAM_TYPES: tuple[str, ...] = ("scalar", "vector", "color")

# 可接受的 Spec kind（A 级 / B 级）。B 级今日只做结构化暂停，不落模块栈工具。
SPEC_KINDS: tuple[str, ...] = ("niagara_component", "niagara_system")

SPEC_HINT = (
    "Niagara（粒子）Spec，JSON：A 级 {kind:'niagara_component', target:'Actor:Class',"
    " system:'/Game/FX/NS_X.NS_X', auto_activate:true,"
    " user_params:[{name,type,value}], verify:[...]}；"
    "user_params.type 只能取：" + "/".join(USER_PARAM_TYPES) + "；system 必须是 /Game 路径。"
    "B 级 {kind:'niagara_system', asset:'/Game/FX/NS_X.NS_X', modules:[...], verify:[...]}"
    " 今日暂停点名（模块栈待 P0 工具确认）。"
)

# 写闸白名单：加组件 / 设 System / 设 User 参数 / 保存。
_WRITE_ALLOW = (
    "AddComponent", "SetSystem", "SetAutoActivate", "SetVariable", "SetVariableByName",
    "SetComponentVariable", "save_assets",
)

# toolset 家族名（计划 §1：NiagaraToolsets.NiagaraToolset_Info/_Component/_Blueprint/_System/_Assets）。
_COMP = "NiagaraToolsets.NiagaraToolset_Component"
_ASSET = "editor_toolset.toolsets.asset.AssetTools"

# B 级暂停的诚实理由（今日脚本侧不落模块栈写工具）。
_SYSTEM_PAUSED_REASON = "Niagara 系统资产模块栈今日待 P0 工具确认，暂停点名"


def _package(ref: str) -> str:
    return str(ref or "").split(".", 1)[0]


# ------------------------------------------------------------------ 校验
def validate_spec(spec: dict) -> list:
    """Spec 结构 + 白名单校验（纯函数，返回结构化错误清单）。B 级只验外壳结构。"""
    errors: list = []
    if not isinstance(spec, dict):
        return ["spec 必须是对象"]
    kind = spec.get("kind")
    if kind not in SPEC_KINDS:
        errors.append(sp.spec_error("unknown_kind", f"kind {kind!r} 不在白名单",
                                    where="kind", hint="可用：/" + "/".join(SPEC_KINDS)))
        return errors

    if kind == "niagara_system":
        # B 级：只校验能定位到 System 资产，模块栈今日不校验（build 直接暂停点名）。
        asset = spec.get("asset")
        if not isinstance(asset, str) or not asset.strip():
            errors.append(sp.spec_error("bad_asset", "缺 asset（/Game/.../NS_X.NS_X）", where="asset"))
        elif not asset.startswith("/Game/"):
            errors.append(sp.spec_error("bad_asset", f"asset 必须是 /Game 路径：{asset}", where="asset"))
        return errors

    # A 级 niagara_component
    target = spec.get("target")
    if not isinstance(target, str) or not target.strip():
        errors.append(sp.spec_error("bad_target", "缺 target（Actor:Class，如 BP_TestActor:BP_TestActor）",
                                    where="target"))
    system = spec.get("system")
    if not isinstance(system, str) or not system.strip():
        errors.append(sp.spec_error("bad_system", "缺 system（/Game/.../NS_X.NS_X）", where="system"))
    elif not system.startswith("/Game/"):
        errors.append(sp.spec_error("bad_system", f"system 必须是 /Game 路径：{system}", where="system"))
    params = spec.get("user_params", [])
    if not isinstance(params, list):
        errors.append(sp.spec_error("bad_params", "user_params 必须是数组", where="user_params"))
    else:
        seen: set[str] = set()
        for i, p in enumerate(params):
            where = f"user_params[{i}]"
            if not isinstance(p, dict):
                errors.append(sp.spec_error("bad_param", "参数必须是对象", where=where))
                continue
            name = p.get("name")
            if not isinstance(name, str) or not name.strip():
                errors.append(sp.spec_error("bad_param", "参数缺 name", where=where))
                continue
            if name in seen:
                errors.append(sp.spec_error("dup_param", f"参数名重复：{name}", where=where))
            seen.add(name)
            typ = p.get("type")
            if typ not in USER_PARAM_TYPES:
                errors.append(sp.spec_error(
                    "bad_param_type", f"{name} 的 type {typ!r} 不在白名单", where=f"{where}.type",
                    hint="可用：/" + "/".join(USER_PARAM_TYPES)))
    return errors


def extract_spec(step: dict) -> dict | None:
    if not isinstance(step, dict):
        return None
    for key in ("spec", "niagara_spec"):
        if isinstance(step.get(key), dict):
            return step[key]
    return None


def write_task(step: dict) -> dict:
    save = bool((step or {}).get("save", True))
    return sp.make_write_task(_WRITE_ALLOW, save=save, pie=False, allow_key="allow_niagara")


# ------------------------------------------------------------------ 执行 plan
def plan_ops(spec: dict) -> list[dict]:
    """确定性、幂等、无时间/随机的抽象原语清单。"""
    target = spec.get("target")
    ops: list[dict] = [{"op": "snapshot", "target": target}]
    ops.append({"op": "ensure_component", "target": target})
    ops.append({"op": "set_system", "target": target, "system": spec.get("system")})
    if "auto_activate" in spec:
        ops.append({"op": "set_auto_activate", "target": target,
                    "auto_activate": bool(spec.get("auto_activate"))})
    for p in spec.get("user_params") or []:
        ops.append({"op": "set_user_param", "target": target, "param": p})
    ops.append({"op": "save", "target": target})
    ops.append({"op": "readback", "target": target})
    return ops


def _result(ok: bool, *, tools=None, readback=None, error: str = "") -> dict:
    return {"ok": ok, "tools": list(tools or []), "readback": dict(readback or {}), "error": error}


def _component_readback(data) -> dict:
    """把组件读回（或空 data）归一成 {component:{exists,system}, params:{name:value}}。"""
    d = data if isinstance(data, dict) else {}
    system = str(d.get("system") or d.get("systemPath") or "")
    exists = bool(d.get("hasComponent") or d.get("exists") or system)
    params = d.get("userParams")
    if not isinstance(params, dict):
        params = d.get("params")
    if not isinstance(params, dict):
        params = {}
    return {"component": {"exists": exists, "system": system}, "params": params}


def exec_op(op: dict, call, write: dict) -> dict:
    """抽象原语 → 真实调用（经注入的 ``call``；只读原语容忍空 data 仍返回 ok）。

    工具名按计划 §1 toolset 家族；具体动作名拿不准处标「待 P0 describe」。
    每个写原语幂等：已存在的组件不重建。
    """
    kind = op.get("op")
    target = op.get("target") or ""
    if kind == "snapshot":
        # 待 P0 describe：Component 家族读回动作名（技能只实锤了 SetSystem / SetVariable）。
        got = call(_COMP, "GetComponentInfo", {"actor": {"refPath": target}}, write)
        if isinstance(got, dict) and not got.get("ok"):
            return _result(False, tools=["GetComponentInfo"],
                           error=str(got.get("error") or "读不到组件"))
        data = got.get("data") if isinstance(got, dict) else None
        return _result(True, tools=["GetComponentInfo"],
                       readback={"__existing__": _component_readback(data)})
    if kind == "readback":
        got = call(_COMP, "GetComponentInfo", {"actor": {"refPath": target}}, write)
        if isinstance(got, dict) and not got.get("ok"):
            return _result(False, tools=["GetComponentInfo"],
                           error=str(got.get("error") or "复核读不到组件"))
        data = got.get("data") if isinstance(got, dict) else None
        return _result(True, tools=["GetComponentInfo"], readback=_component_readback(data))
    if kind == "ensure_component":
        snap = call(_COMP, "GetComponentInfo", {"actor": {"refPath": target}}, write)
        existing = (_component_readback(snap.get("data"))
                    if isinstance(snap, dict) and snap.get("ok") else {})
        if (existing.get("component") or {}).get("exists"):
            return _result(True, tools=["GetComponentInfo"])  # 幂等：已有组件不重建
        made = call(_COMP, "AddComponent", {
            "actor": {"refPath": target},
            "componentClass": {"refPath": "/Script/Niagara.NiagaraComponent"},
        }, write)  # 待 P0 describe：Component 家族加组件动作名/参数
        return _result(bool(made.get("ok")), tools=["AddComponent"],
                       error=str(made.get("error") or "加 NiagaraComponent 失败"))
    if kind == "set_system":
        r = call(_COMP, "SetSystem", {
            "actor": {"refPath": target},
            "system": {"refPath": op.get("system")},
        }, write)
        return _result(bool(r.get("ok")), tools=["SetSystem"],
                       error=str(r.get("error") or "设 System 失败"))
    if kind == "set_auto_activate":
        r = call(_COMP, "SetAutoActivate", {
            "actor": {"refPath": target},
            "autoActivate": bool(op.get("auto_activate")),
        }, write)  # 待 P0 describe：auto_activate 动作名/参数
        return _result(bool(r.get("ok")), tools=["SetAutoActivate"],
                       error=str(r.get("error") or "设 auto_activate 失败"))
    if kind == "set_user_param":
        p = op.get("param") or {}
        r = call(_COMP, "SetVariable", {
            "actor": {"refPath": target},
            "name": p.get("name"),
            "type": p.get("type"),
            "value": p.get("value"),
        }, write)
        return _result(bool(r.get("ok")), tools=["SetVariable"],
                       error=str(r.get("error") or f"设 User 参数 {p.get('name')} 失败"))
    if kind == "save":
        r = call(_ASSET, "save_assets", {"asset_paths": [_package(target)]}, write)
        return _result(bool(r.get("ok")), tools=["save_assets"],
                       readback={"saved": bool(r.get("ok"))},
                       error=str(r.get("error") or "保存失败"))
    return _result(False, error=f"未知原语 {kind!r}")


# ------------------------------------------------------------------ 只读复核
def assertions(spec: dict) -> list:
    """A 级期望：组件存在且 System 引用对、User 参数读回==期望、saved。"""
    out: list = [
        {"path": "component.exists", "op": "eq", "value": True},
        {"path": "component.system", "op": "eq", "value": spec.get("system")},
    ]
    for p in spec.get("user_params") or []:
        out.append({"path": f"params.{p.get('name')}", "op": "eq", "value": p.get("value")})
    out.append({"path": "saved", "op": "eq", "value": True})
    return out


def verify_spec(spec: dict, readback: dict) -> list:
    # 不补默认值：空 readback 上必须至少一条断言不过（诚实失败，不假成功）。
    return sp.eval_assertions(assertions(spec), readback or {})


def summarize(spec: dict) -> str:
    params = spec.get("user_params") or []
    names = "、".join(str(p.get("name")) for p in params)
    return (f"{spec.get('target')} 挂上 {spec.get('system')}"
            f"（{len(params)} 个 User 参数：{names or '无'}），已保存并复核通过")


# ------------------------------------------------------------------ 路由识别（宁缺毋滥）
_STRONG = ("粒子", "niagara", "特效", "爆炸", "火焰", "拖尾", "ns_")


def recognize(text: str) -> bool:
    """一句话是否稳定属于 Niagara 域。命中强特征词才算。"""
    blob = str(text or "").lower()
    if not blob:
        return False
    return any(tok.lower() in blob for tok in _STRONG)


# ------------------------------------------------------------------ 对外入口
def build(step: dict, **kwargs) -> dict:
    """交付接口：A 级跑完整闭环；B 级（niagara_system）结构化暂停点名，不伪造模块栈调用。"""
    spec = None
    if isinstance(step, dict):
        spec = step.get("spec")
        if not isinstance(spec, dict):
            spec = extract_spec(step)
    if isinstance(spec, dict) and str(spec.get("kind") or "") == "niagara_system":
        return sp.unsupported_result(_module(), _SYSTEM_PAUSED_REASON)
    return sp.build_domain(_module(), step, **kwargs)


def _module():
    import sys

    return sys.modules[__name__]
