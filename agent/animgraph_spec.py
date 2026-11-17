# agent/animgraph_spec.py
"""动画蓝图（AnimBlueprint / AnimGraph）域：Spec → 确定性执行 kernel → 只读复核。

与 `agent/umg_spec.py` / `agent/graph_spec.py` **完全同形**（动画蓝图计划 §3）。动画蓝图本质是
一张 `蓝图(AnimInstance)` 的**动画图 + 状态机**；仓库里**没有专用 animBP toolset**，所以复用蓝图图
kernel（`editor_toolset.toolsets.blueprint.BlueprintTools`）——只是换节点类白名单为动画节点
（SequencePlayer 等）+ 解析 AnimSequence 资产。

自动化档（本域 good_spec 取这档）：
  - AnimBP 变量（`add_variable`）
  - AnimGraph 的 SequencePlayer 引用 AnimSequence 资产（`create_node` + `set_pin_value`）
  - 编译（`compile_blueprint`，JUDGE_KIND = blueprint_compile）

**状态机转换（state_machine.transitions）的接线**：计划判定今天无确定性工具（无法据 Spec 可靠建
状态机容器 / 连转换）。这种子形态按 `sp.unsupported_result(module, ...)` **结构化暂停**，绝不伪造
工具调用。守卫会在 run() 通过后额外断言：只含 transitions 的 Spec 经 build 必须 `paused is True`。

边界纪律：本文件**不在顶层 import 任何编辑器**；`exec_op` 只经注入的 `call(toolset, tool, args,
write)` 落真实工具。离线守卫注入替身，从不碰编辑器。
"""
from __future__ import annotations

import sys

from agent import spec_pipeline as sp

KIND = "animgraph"
JUDGE_KIND = "blueprint_compile"   # 图能编过就是硬裁判

# 动画图节点类白名单（最小骨架：SequencePlayer 等）。
ANIM_NODE_CLASSES: tuple[str, ...] = (
    "SequencePlayer",
    "AnimGraph|SequencePlayer",
    "BlendSpacePlayer",
    "AnimGraph|BlendSpacePlayer",
    "StateMachine",
    "AnimGraph|StateMachine",
)

# 变量类型白名单（AnimBP 变量）。
VARIABLE_TYPES: tuple[str, ...] = (
    "bool", "float", "int32", "int", "int64", "double", "name", "string", "text",
    "vector", "rotator", "transform", "byte", "enum", "object",
)

SPEC_HINT = (
    "动画蓝图（AnimGraph）Spec，JSON：{anim_blueprint:/Game/Characters/ABP_Player.ABP_Player,"
    " skeleton:/Game/Characters/SK_Mannequin.SK_Mannequin,"
    " variables:[{name,type}],"
    " state_machine:{name, states:[{name,anim}], transitions:[{from,to,rule}]}, verify:[...]}。"
    "自动化档：建 AnimBP 变量 + 为每个 state 建 SequencePlayer 引用其 anim（AnimSequence 资产）+ 编译。"
    "状态机转换（transitions）的接线无确定性工具，走结构化暂停（需人工/P0 工具确认），不要伪造。"
)

# 写闸白名单：建节点/连引脚/变量/默认值/编译/保存等图写原语（动画图复用蓝图图工具）。
_WRITE_ALLOW = (
    "write_graph_dsl", "create_node", "connect_pins", "add_event", "set_pin_value",
    "add_variable", "compile_blueprint", "save_assets",
)

_GRAPH_NAME = "AnimGraph"


# ------------------------------------------------------------------ 子形态判定
def has_state_machine_transitions(spec: dict) -> bool:
    """Spec 里是否带需要接线的 state_machine.transitions（该子形态今天无确定性工具）。"""
    if not isinstance(spec, dict):
        return False
    sm = spec.get("state_machine")
    if not isinstance(sm, dict):
        return False
    transitions = sm.get("transitions")
    return isinstance(transitions, list) and len(transitions) > 0


def _states(spec: dict) -> list:
    sm = spec.get("state_machine")
    if isinstance(sm, dict):
        states = sm.get("states")
        if isinstance(states, list):
            return states
    return []


# ------------------------------------------------------------------ 校验
def validate_spec(spec: dict) -> list:
    errors: list = []
    if not isinstance(spec, dict):
        return ["spec 必须是对象"]

    abp = spec.get("anim_blueprint")
    if not isinstance(abp, str) or not abp.strip():
        errors.append(sp.spec_error("bad_target", "缺 anim_blueprint（/Game/.../ABP_X.ABP_X）",
                                    where="anim_blueprint"))
    skeleton = spec.get("skeleton")
    if not isinstance(skeleton, str) or not skeleton.strip():
        errors.append(sp.spec_error("bad_skeleton", "缺 skeleton（/Game/.../SK_X.SK_X）",
                                    where="skeleton"))

    # 变量
    var_names: set[str] = set()
    variables = spec.get("variables")
    if variables is not None and not isinstance(variables, list):
        errors.append(sp.spec_error("bad_variables", "variables 必须是数组", where="variables"))
        variables = []
    for i, var in enumerate(variables or []):
        where = f"variables[{i}]"
        if not isinstance(var, dict):
            errors.append(sp.spec_error("bad_variable", "变量必须是对象", where=where))
            continue
        name = var.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(sp.spec_error("bad_var_name", "变量缺 name", where=where))
            continue
        if name in var_names:
            errors.append(sp.spec_error("dup_var", f"变量名重复：{name}", where=where))
        var_names.add(name)
        vtype = var.get("type")
        if vtype not in VARIABLE_TYPES:
            errors.append(sp.spec_error("unknown_var_type",
                                        f"变量 {name} 类型 {vtype!r} 不在类型白名单",
                                        where=f"{where}.type",
                                        hint="可用：" + "/".join(VARIABLE_TYPES)))

    # 状态机
    sm = spec.get("state_machine")
    if not isinstance(sm, dict):
        errors.append(sp.spec_error("no_state_machine", "缺 state_machine 对象", where="state_machine"))
        return errors
    sm_name = sm.get("name")
    if not isinstance(sm_name, str) or not sm_name.strip():
        errors.append(sp.spec_error("bad_sm_name", "state_machine 缺 name", where="state_machine.name"))
    states = sm.get("states")
    if not isinstance(states, list) or not states:
        errors.append(sp.spec_error("no_states", "state_machine.states 必须是非空数组",
                                     where="state_machine.states"))
        states = []
    state_names: set[str] = set()
    for i, st in enumerate(states):
        where = f"state_machine.states[{i}]"
        if not isinstance(st, dict):
            errors.append(sp.spec_error("bad_state", "state 必须是对象", where=where))
            continue
        name = st.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(sp.spec_error("bad_state_name", "state 缺 name", where=where))
            continue
        if name in state_names:
            errors.append(sp.spec_error("dup_state", f"state 名重复：{name}", where=where))
        state_names.add(name)
        anim = st.get("anim")
        if not isinstance(anim, str) or not anim.strip():
            errors.append(sp.spec_error("bad_anim", f"state {name} 缺 anim（AnimSequence 资产）",
                                        where=f"{where}.anim"))

    # 转换（结构校验；接线本身走 unsupported）
    transitions = sm.get("transitions")
    if transitions is not None and not isinstance(transitions, list):
        errors.append(sp.spec_error("bad_transitions", "transitions 必须是数组",
                                    where="state_machine.transitions"))
        transitions = []
    for i, tr in enumerate(transitions or []):
        where = f"state_machine.transitions[{i}]"
        if not isinstance(tr, dict):
            errors.append(sp.spec_error("bad_transition", "transition 必须是对象", where=where))
            continue
        for end in ("from", "to"):
            val = tr.get(end)
            if not isinstance(val, str) or not val.strip():
                errors.append(sp.spec_error("bad_transition", f"transition 缺 {end}",
                                            where=f"{where}.{end}"))
            elif state_names and val not in state_names:
                errors.append(sp.spec_error("bad_transition", f"transition.{end} 指向未知状态 {val!r}",
                                            where=f"{where}.{end}"))
        if not isinstance(tr.get("rule"), str) or not tr.get("rule", "").strip():
            errors.append(sp.spec_error("bad_transition", "transition 缺 rule",
                                        where=f"{where}.rule"))

    return errors


def extract_spec(step: dict) -> dict | None:
    if not isinstance(step, dict):
        return None
    for key in ("spec", "animgraph_spec"):
        if isinstance(step.get(key), dict):
            return step[key]
    return None


def write_task(step: dict) -> dict:
    save = bool((step or {}).get("save", True))
    return sp.make_write_task(_WRITE_ALLOW, save=save, pie=False, allow_key="allow_blueprint")


# ------------------------------------------------------------------ 执行 plan
def plan_ops(spec: dict) -> list[dict]:
    """确定性、幂等、无时间/随机：snapshot→变量→每个 state 的 SequencePlayer→compile→回读。

    只覆盖自动化档（变量 + SequencePlayer 引用 anim + 编译）；不含 transitions 接线。"""
    abp = spec.get("anim_blueprint")
    ops: list[dict] = [{"op": "snapshot", "target": abp}]
    for var in spec.get("variables") or []:
        ops.append({"op": "add_variable", "target": abp, "var": var})
    for state in _states(spec):
        if isinstance(state, dict) and state.get("name"):
            ops.append({"op": "add_sequence_player", "target": abp, "graph": _GRAPH_NAME,
                        "state": state})
    ops.append({"op": "compile", "target": abp})
    ops.append({"op": "readback", "target": abp})
    return ops


def _result(ok: bool, *, tools=None, readback=None, error: str = "") -> dict:
    return {"ok": ok, "tools": list(tools or []), "readback": dict(readback or {}), "error": error}


def _parse_readback(data) -> dict:
    """把动画图回读归一成 {nodes, variables}。容忍空 data，绝不把缺失默认成通过。"""
    nodes: dict[str, dict] = {}
    variables: dict[str, dict] = {}
    if isinstance(data, dict):
        raw_nodes = data.get("nodes")
        if isinstance(raw_nodes, dict):
            for nid, info in raw_nodes.items():
                nodes[str(nid)] = info if isinstance(info, dict) else {}
        elif isinstance(raw_nodes, list):
            for info in raw_nodes:
                if isinstance(info, dict):
                    nid = str(info.get("id") or info.get("name") or "")
                    if nid:
                        nodes[nid] = info
        raw_vars = data.get("variables")
        if isinstance(raw_vars, dict):
            variables = {str(k): (v if isinstance(v, dict) else {}) for k, v in raw_vars.items()}
        elif isinstance(raw_vars, list):
            for info in raw_vars:
                if isinstance(info, dict):
                    name = str(info.get("name") or "")
                    if name:
                        variables[name] = info
    return {"nodes": nodes, "variables": variables}


def exec_op(op: dict, call, write: dict) -> dict:
    """抽象原语 → 真实工具调用。读原语容忍空 data；写原语按 call 的 ok 判定。"""
    kind = op.get("op")
    abp = op.get("target") or ""
    graph = op.get("graph", _GRAPH_NAME)

    if kind == "snapshot":
        got = call("editor_toolset.toolsets.blueprint.BlueprintTools", "read_graph_dsl",
                   {"graph": {"refPath": f"{abp}:{graph}"}}, write)
        if not got.get("ok"):
            return _result(False, tools=["read_graph_dsl"], error=str(got.get("error") or "读不到动画图"))
        parsed = _parse_readback(got.get("data"))
        return _result(True, tools=["read_graph_dsl"], readback={"__existing__": parsed})

    if kind == "readback":
        got = call("editor_toolset.toolsets.blueprint.BlueprintTools", "read_graph_dsl",
                   {"graph": {"refPath": f"{abp}:{graph}"}}, write)
        if not got.get("ok"):
            return _result(False, tools=["read_graph_dsl"],
                           error=str(got.get("error") or "复核读不到动画图"))
        parsed = _parse_readback(got.get("data"))
        return _result(True, tools=["read_graph_dsl"], readback=parsed)

    if kind == "add_variable":
        var = op.get("var") or {}
        r = call("editor_toolset.toolsets.blueprint.BlueprintTools", "add_variable",
                 {"blueprint": {"refPath": abp}, "name": var.get("name"), "type": var.get("type")},
                 write)
        return _result(bool(r.get("ok")), tools=["add_variable"],
                       error=str(r.get("error") or f"建 AnimBP 变量 {var.get('name')} 失败"))

    if kind == "add_sequence_player":
        state = op.get("state") or {}
        sid = str(state.get("name") or "")
        anim = str(state.get("anim") or "")
        # ⚠️ 待 P0 单点 describe：create_node 的 node_type 期望 type_id
        #    （如 "AnimGraph|SequencePlayer"），set_pin_value 的 anim 引脚名需单点确认。
        made = call("editor_toolset.toolsets.blueprint.BlueprintTools", "create_node",
                    {"blueprint": {"refPath": abp}, "graph_name": graph,
                     "node_type": "AnimGraph|SequencePlayer", "node_id": sid,
                     "position": {"x": 0, "y": 0}}, write)
        if not made.get("ok"):
            return _result(False, tools=["create_node"],
                           error=str(made.get("error") or f"建 SequencePlayer({sid}) 失败"))
        bound = call("editor_toolset.toolsets.blueprint.BlueprintTools", "set_pin_value",
                     {"blueprint": {"refPath": abp}, "graph_name": graph,
                      "node": sid, "pin": "Sequence", "value": anim}, write)
        return _result(bool(bound.get("ok")), tools=["create_node", "set_pin_value"],
                       error=str(bound.get("error") or f"给 {sid} 绑 AnimSequence({anim}) 失败"))

    if kind == "compile":
        r = call("editor_toolset.toolsets.blueprint.BlueprintTools", "compile_blueprint",
                 {"blueprint": {"refPath": abp}}, write)
        ok = bool(r.get("ok"))
        return _result(ok, tools=["compile_blueprint"],
                       readback={"compiled": ok} if ok else {},
                       error=str(r.get("error") or "编译失败"))

    return _result(False, error=f"未知原语 {kind!r}")


# ------------------------------------------------------------------ 只读复核
def assertions(spec: dict) -> list:
    out: list = [{"path": "compiled", "op": "eq", "value": True}]
    for var in spec.get("variables") or []:
        if isinstance(var, dict) and var.get("name"):
            out.append({"path": f"variables.{var['name']}", "op": "exists"})
    for state in _states(spec):
        if not isinstance(state, dict) or not state.get("name"):
            continue
        name = state.get("name")
        out.append({"path": f"nodes.{name}", "op": "exists"})
        out.append({"path": f"nodes.{name}.anim", "op": "contains", "value": state.get("anim")})
    return out


def verify_spec(spec: dict, readback: dict) -> list:
    # 不 setdefault("compiled", True)：空 readback 必须至少一条断言不通过（repair 测试靠它）。
    return sp.eval_assertions(assertions(spec), readback or {})


def summarize(spec: dict) -> str:
    states = _states(spec)
    names = "、".join(str(s.get("name")) for s in states if isinstance(s, dict))
    return (f"{spec.get('anim_blueprint')} 落地 {len(states)} 个 SequencePlayer（{names}）、"
            f"变量 {len(spec.get('variables') or [])} 个，已编译并复核通过"
            f"（状态机转换接线未做：需人工/P0 工具）")


# ------------------------------------------------------------------ 路由识别（宁缺毋滥）
_STRONG = ("动画蓝图", "animgraph", "anim graph", "anim blueprint", "abp_", "状态机",
           "seq player", "sequence player", "sequenceplayer", "animinstance")


def recognize(text: str) -> bool:
    blob = str(text or "").lower().strip()
    if not blob:
        return False
    return any(tok in blob for tok in _STRONG)


# ------------------------------------------------------------------ 对外入口
def build(step: dict, **kwargs) -> dict:
    """交付接口：先拦「状态机转换接线」子形态（无确定性工具 ⇒ 结构化暂停），再跑闭环。"""
    spec = step.get("spec") if isinstance(step, dict) else None
    if not isinstance(spec, dict):
        spec = extract_spec(step)
    if has_state_machine_transitions(spec):
        return sp.unsupported_result(
            sys.modules[__name__],
            "状态机转换接线需人工/P0 工具确认",
        )
    return sp.build_domain(sys.modules[__name__], step, **kwargs)
