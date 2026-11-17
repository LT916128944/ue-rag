# agent/graph_spec.py
"""蓝图图（Blueprint Graph）域：Spec → 确定性执行 kernel → 只读拓扑复核。

与 `agent/umg_spec.py` **完全同形**（总览 §4 / 蓝图域计划 §3）。它把「在哪张蓝图/哪个 Graph、
放哪些节点（按类）、引脚级连线、变量、默认值、事件桩 → add_node → connect → set_defaults →
编译」收成一个域模块，交给 `agent/spec_pipeline` 的通用内核跑闭环：

    模型只产 Graph Spec（见 SPEC_HINT）→ 脚本 plan_ops 确定性执行 → 只读复核节点类/边/引脚/
    变量/编译 → 复核不过回灌模型改 Spec（≤2 轮）→ 仍不过暂停点名。

边界纪律：本文件**不在顶层 import 任何编辑器**；`exec_op` 只经注入的 `call(toolset, tool, args,
write)` 落真实工具（`editor_toolset.toolsets.blueprint.BlueprintTools`）。离线守卫注入替身，从不
碰编辑器。

工具名口径（按蓝图域计划 §1/§3 与 `agent/nodes.py`/事实库实测）：
  - `BlueprintTools.create_node`（按 type_id 建节点）
  - `BlueprintTools.add_event`（建事件桩，如 ReceiveBeginPlay）
  - `BlueprintTools.connect_pins`（output_pin / input_pin，引脚级）
  - `BlueprintTools.add_variable`（建成员变量）
  - `BlueprintTools.set_pin_value`（设引脚默认值）
  - `BlueprintTools.compile_blueprint`（硬裁判 = blueprint_compile）
  - `BlueprintTools.read_graph_dsl` / `get_node_infos`（只读回读）

⚠️ 待 P0 单点 describe 的诚实备注：`create_node` 的节点类型实数叫 `type_id`（如
`Utilities|FlowControl|Branch`），本 Spec 里写的是 K2Node 类名，**K2Node→type_id 的完整映射未在
仓库里定案**；`connect_pins` 的 `output_pin/input_pin` 在真实工具里要的是 `get_node_infos` 返回的
pin_id 对象（`{direction,index_id,node}`），本层先按 `"nodeId.pin"` 抽象字符串传，落地前需按 P0 结果
做一层解析。这些都在 exec_op 对应位置标了「待 P0 单点 describe」，不伪造已跑通。
"""
from __future__ import annotations

import sys

from agent import spec_pipeline as sp

KIND = "graph"
JUDGE_KIND = "blueprint_compile"   # 图能编过就是硬裁判（与 plan_windows 的蓝图图任务一致）

# 可接受的节点类白名单（K2Node 家族常用子集；表外一律结构化「不支持」）。
NODE_CLASSES: tuple[str, ...] = (
    "K2Node_Event",
    "K2Node_CustomEvent",
    "K2Node_VariableSet",
    "K2Node_VariableGet",
    "K2Node_CallFunction",
    "K2Node_ExecutionSequence",
    "K2Node_IfThenElse",
    "K2Node_MacroInstance",
    "K2Node_DynamicCast",
    "K2Node_CommutativeAssociativeBinaryOperator",
    "K2Node_Self",
    "K2Node_ComponentBoundEvent",
)

# 引脚后缀白名单（exec/then/execute/…；函数节点可能有自定义引脚名，故只要求非空合法标识）。
PIN_SUFFIXES: tuple[str, ...] = (
    "exec", "then", "execute", "else", "completed", "condition", "returnvalue",
    "self", "true", "false", "valid", "invalid", "case", "then_0", "then_1",
)

# 变量类型白名单（Graph Spec.variables[].type）。
VARIABLE_TYPES: tuple[str, ...] = (
    "bool", "float", "int32", "int", "int64", "double", "name", "string", "text",
    "vector", "rotator", "transform", "byte", "enum", "object",
)

SPEC_HINT = (
    "蓝图图（Blueprint Graph）Spec，JSON：{blueprint:/Game/Blueprints/BP_Door.BP_Door,"
    " graph:EventGraph, variables:[{name,type,default}],"
    " nodes:[{id,class,event?,function?,var?,defaults?}],"
    " edges:[{from:\"ev.exec\", to:\"set.exec\"}], verify:[...]}。"
    "class 只能取 K2Node 家族：" + "/".join(NODE_CLASSES) + "；"
    "edges 用 \"节点id.引脚名\"，引脚名常见后缀：" + "/".join(PIN_SUFFIXES) + "；"
    "同 id 幂等更新不重建；只描述结构与接线，不写游戏逻辑自由代码。"
)

# 写闸白名单：建节点/连线/变量/默认值/编译/保存等图写原语。
_WRITE_ALLOW = (
    "write_graph_dsl", "create_node", "connect_pins", "add_event", "set_pin_value",
    "add_variable", "compile_blueprint", "save_assets",
)


def _graph_ref(blueprint: str, graph: str) -> str:
    """蓝图 + 图名的 Graph refPath（实测形如 /Game/BP_Gun.BP_Gun:EventGraph）。"""
    bp = str(blueprint or "")
    g = str(graph or "EventGraph")
    return f"{bp}:{g}"


def _package(ref: str) -> str:
    return str(ref or "").split(".", 1)[0]


# ------------------------------------------------------------------ 校验
def validate_spec(spec: dict) -> list:
    errors: list = []
    if not isinstance(spec, dict):
        return ["spec 必须是对象"]

    bp = spec.get("blueprint")
    if not isinstance(bp, str) or not bp.strip():
        errors.append(sp.spec_error("bad_target", "缺 blueprint（/Game/.../BP_X.BP_X）",
                                    where="blueprint"))
    graph = spec.get("graph", "EventGraph")
    if graph is not None and (not isinstance(graph, str) or not str(graph).strip()):
        errors.append(sp.spec_error("bad_graph", "graph 必须是非空字符串", where="graph"))

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

    # 节点
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        errors.append(sp.spec_error("no_nodes", "nodes 必须是非空数组", where="nodes"))
        return errors
    ids: set[str] = set()
    node_class: dict[str, str] = {}
    for i, node in enumerate(nodes):
        where = f"nodes[{i}]"
        if not isinstance(node, dict):
            errors.append(sp.spec_error("bad_node", "节点必须是对象", where=where))
            continue
        nid = node.get("id")
        if not isinstance(nid, str) or not nid.strip():
            errors.append(sp.spec_error("bad_id", "节点缺 id", where=where))
            continue
        if nid in ids:
            errors.append(sp.spec_error("dup_id", f"节点 id 重复：{nid}", where=where))
        ids.add(nid)
        cls = node.get("class")
        if cls not in NODE_CLASSES:
            errors.append(sp.spec_error("unknown_class", f"{nid} 的类 {cls!r} 不在节点白名单",
                                        where=f"{where}.class",
                                        hint="可用：" + "/".join(NODE_CLASSES)))
        else:
            node_class[nid] = cls
        if cls == "K2Node_Event" and not node.get("event"):
            errors.append(sp.spec_error("missing_event", f"{nid} 是 K2Node_Event 但缺 event",
                                        where=f"{where}.event"))
        if cls in ("K2Node_VariableSet", "K2Node_VariableGet"):
            var = node.get("var")
            if not isinstance(var, str) or not var.strip():
                errors.append(sp.spec_error("missing_var", f"{nid} 缺 var", where=f"{where}.var"))
            elif var_names and var not in var_names:
                errors.append(sp.spec_error("unknown_var", f"{nid} 引用未声明变量 {var!r}",
                                            where=f"{where}.var"))
        if cls == "K2Node_CallFunction" and not node.get("function"):
            errors.append(sp.spec_error("missing_function", f"{nid} 是 K2Node_CallFunction 但缺 function",
                                        where=f"{where}.function"))
        defaults = node.get("defaults")
        if defaults is not None and not isinstance(defaults, dict):
            errors.append(sp.spec_error("bad_defaults", f"{nid}.defaults 必须是对象",
                                        where=f"{where}.defaults"))

    # 边（引脚级）
    edges = spec.get("edges")
    if edges is not None and not isinstance(edges, list):
        errors.append(sp.spec_error("bad_edges", "edges 必须是数组", where="edges"))
        edges = []
    adjacency: dict[str, list[str]] = {}
    for i, edge in enumerate(edges or []):
        where = f"edges[{i}]"
        if not isinstance(edge, dict):
            errors.append(sp.spec_error("bad_edge", "边必须是对象", where=where))
            continue
        src = edge.get("from")
        dst = edge.get("to")
        for end, val in (("from", src), ("to", dst)):
            if not isinstance(val, str) or "." not in val:
                errors.append(sp.spec_error("bad_edge",
                                            f"{where}.{end} 必须是 \"节点id.引脚名\"，实际 {val!r}",
                                            where=f"{where}.{end}"))
                continue
            node_id, pin = val.rsplit(".", 1)
            if node_id not in ids:
                errors.append(sp.spec_error("bad_edge",
                                            f"{where}.{end} 指向未知节点 {node_id!r}",
                                            where=f"{where}.{end}"))
            elif not pin.strip():
                errors.append(sp.spec_error("unknown_pin", f"{where}.{end} 缺引脚名",
                                            where=f"{where}.{end}"))
        if isinstance(src, str) and isinstance(dst, str) and "." in src and "." in dst:
            a = src.rsplit(".", 1)[0]
            b = dst.rsplit(".", 1)[0]
            if a in ids and b in ids:
                adjacency.setdefault(a, []).append(b)

    # 节点级成环（exec 图不允许环：环会被编译器判死或孤立）
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {nid: WHITE for nid in ids}

    def _dfs(start: str) -> str:
        stack = [(start, iter(adjacency.get(start, [])))]
        color[start] = GREY
        while stack:
            cur, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[cur] = BLACK
                stack.pop()
                continue
            if color.get(nxt) == GREY:
                return f"{cur}->{nxt}"
            if color.get(nxt) == WHITE:
                color[nxt] = GREY
                stack.append((nxt, iter(adjacency.get(nxt, []))))
        return ""

    for nid in ids:
        if color[nid] == WHITE:
            hit = _dfs(nid)
            if hit:
                errors.append(sp.spec_error("cycle", f"节点成环：{hit}", where="edges"))
                break

    return errors


def extract_spec(step: dict) -> dict | None:
    if not isinstance(step, dict):
        return None
    for key in ("spec", "graph_spec"):
        if isinstance(step.get(key), dict):
            return step[key]
    return None


def write_task(step: dict) -> dict:
    save = bool((step or {}).get("save", True))
    return sp.make_write_task(_WRITE_ALLOW, save=save, pie=False, allow_key="allow_blueprint")


# ------------------------------------------------------------------ 执行 plan
def plan_ops(spec: dict) -> list[dict]:
    """确定性、幂等、无时间/随机的抽象原语清单：snapshot→变量→节点→默认值→连线→compile→回读。"""
    bp = spec.get("blueprint")
    graph = spec.get("graph", "EventGraph")
    ops: list[dict] = [{"op": "snapshot", "target": bp, "graph": graph}]
    for var in spec.get("variables") or []:
        ops.append({"op": "add_variable", "target": bp, "var": var})
    for node in spec.get("nodes") or []:
        ops.append({"op": "add_node", "target": bp, "graph": graph, "node": node})
    for node in spec.get("nodes") or []:
        for prop, val in (node.get("defaults") or {}).items():
            ops.append({"op": "set_default", "target": bp, "graph": graph,
                        "node_id": node.get("id"), "prop": prop, "value": val})
    for edge in spec.get("edges") or []:
        ops.append({"op": "connect", "target": bp, "graph": graph,
                    "from": edge.get("from"), "to": edge.get("to")})
    ops.append({"op": "compile", "target": bp})
    ops.append({"op": "readback", "target": bp, "graph": graph})
    return ops


def _result(ok: bool, *, tools=None, readback=None, error: str = "") -> dict:
    return {"ok": ok, "tools": list(tools or []), "readback": dict(readback or {}), "error": error}


def _parse_readback(data) -> dict:
    """把 read_graph_dsl/get_node_infos 的返回归一成 {nodes, variables, edges}。

    容忍空 data（守卫 adapter 替身返回 ``{"ok": True, "data": {}}``）：解析不出就给空结构，
    绝不在这里抛，也绝不把缺失字段默认成「通过」（复核由 assertions 决定）。
    ⚠️ 待 P0 单点 describe：真实 read_graph_dsl 回的是文本 DSL，需再解析成结构；本层先按
    data 里若已有 nodes/variables/edges 就直取，否则空。"""
    nodes: dict[str, dict] = {}
    variables: dict[str, dict] = {}
    edges: list[str] = []
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
        raw_edges = data.get("edges")
        if isinstance(raw_edges, list):
            for e in raw_edges:
                if isinstance(e, str):
                    edges.append(e)
                elif isinstance(e, dict) and e.get("from") and e.get("to"):
                    edges.append(f"{e['from']}->{e['to']}")
    return {"nodes": nodes, "variables": variables, "edges": edges}


def exec_op(op: dict, call, write: dict) -> dict:
    """抽象原语 → 真实工具调用。读原语容忍空 data；写原语按 call 的 ok 判定。"""
    kind = op.get("op")
    bp = op.get("target") or ""
    graph = op.get("graph", "EventGraph")
    node = op.get("node") or {}

    if kind == "snapshot":
        got = call("editor_toolset.toolsets.blueprint.BlueprintTools", "read_graph_dsl",
                   {"graph": {"refPath": _graph_ref(bp, graph)}}, write)
        if not got.get("ok"):
            return _result(False, tools=["read_graph_dsl"],
                           error=str(got.get("error") or "读不到图"))
        parsed = _parse_readback(got.get("data"))
        return _result(True, tools=["read_graph_dsl"], readback={"__existing__": parsed})

    if kind == "readback":
        got = call("editor_toolset.toolsets.blueprint.BlueprintTools", "read_graph_dsl",
                   {"graph": {"refPath": _graph_ref(bp, graph)}}, write)
        if not got.get("ok"):
            return _result(False, tools=["read_graph_dsl"],
                           error=str(got.get("error") or "复核读不到图"))
        parsed = _parse_readback(got.get("data"))
        return _result(True, tools=["read_graph_dsl"], readback=parsed)

    if kind == "add_variable":
        var = op.get("var") or {}
        args = {"blueprint": {"refPath": bp},
                "name": var.get("name"), "type": var.get("type")}
        if "default" in var:
            args["default"] = var.get("default")
        r = call("editor_toolset.toolsets.blueprint.BlueprintTools", "add_variable", args, write)
        return _result(bool(r.get("ok")), tools=["add_variable"],
                       error=str(r.get("error") or f"建变量 {var.get('name')} 失败"))

    if kind == "add_node":
        cls = str(node.get("class") or "")
        pos = node.get("position") or {"x": 0, "y": 0}
        if cls in ("K2Node_Event", "K2Node_CustomEvent"):
            # 事件桩走 add_event（实测：add_event(blueprint, event_name=..., position=...)）。
            r = call("editor_toolset.toolsets.blueprint.BlueprintTools", "add_event",
                     {"blueprint": {"refPath": bp}, "graph_name": graph,
                      "event_name": node.get("event"), "position": pos}, write)
            return _result(bool(r.get("ok")), tools=["add_event"],
                           error=str(r.get("error") or f"建事件 {node.get('event')} 失败"))
        # ⚠️ 待 P0 单点 describe：create_node 期望的 node_type 是 type_id
        #    （如 "Utilities|FlowControl|Branch"），本层先原样传 K2Node 类名，落地前按 P0 结果映射。
        args = {"blueprint": {"refPath": bp}, "graph_name": graph,
                "node_type": cls, "position": pos, "node_id": node.get("id")}
        if node.get("var"):
            args["var"] = node.get("var")
        if node.get("function"):
            args["function"] = node.get("function")
        r = call("editor_toolset.toolsets.blueprint.BlueprintTools", "create_node", args, write)
        return _result(bool(r.get("ok")), tools=["create_node"],
                       error=str(r.get("error") or f"建节点 {node.get('id')} 失败"))

    if kind == "set_default":
        # ⚠️ 待 P0 单点 describe：真实 set_pin_value 的 pin 要 pin_id 对象；本层先传
        #    "节点id.引脚名" 抽象串，落地前按 get_node_infos 的 pin_id 解析。
        r = call("editor_toolset.toolsets.blueprint.BlueprintTools", "set_pin_value",
                 {"blueprint": {"refPath": bp}, "graph_name": graph,
                  "node": op.get("node_id"), "pin": op.get("prop"),
                  "value": op.get("value")}, write)
        return _result(bool(r.get("ok")), tools=["set_pin_value"],
                       error=str(r.get("error") or f"设默认值 {op.get('prop')} 失败"))

    if kind == "connect":
        # ⚠️ 待 P0 单点 describe：connect_pins 的 output_pin/input_pin 要 pin_id 对象
        #    （{direction,index_id,node}）；本层先传 "节点id.引脚名" 抽象串。
        r = call("editor_toolset.toolsets.blueprint.BlueprintTools", "connect_pins",
                 {"blueprint": {"refPath": bp}, "graph_name": graph,
                  "output_pin": op.get("from"), "input_pin": op.get("to")}, write)
        return _result(bool(r.get("ok")), tools=["connect_pins"],
                       error=str(r.get("error") or f"连 {op.get('from')}->{op.get('to')} 失败"))

    if kind == "compile":
        r = call("editor_toolset.toolsets.blueprint.BlueprintTools", "compile_blueprint",
                 {"blueprint": {"refPath": bp}}, write)
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
    for node in spec.get("nodes") or []:
        nid = node.get("id")
        out.append({"path": f"nodes.{nid}", "op": "exists"})
        if node.get("class"):
            out.append({"path": f"nodes.{nid}.class", "op": "contains", "value": node.get("class")})
    for edge in spec.get("edges") or []:
        out.append({"path": "edges", "op": "contains",
                    "value": f"{edge.get('from')}->{edge.get('to')}"})
    return out


def verify_spec(spec: dict, readback: dict) -> list:
    # 关键：不 setdefault("compiled", True)。空 readback 必须至少有一条断言不通过，
    # 否则「复核不过⇒暂停」的守卫会空转（见 eval/_spec_guard_lib 的 repair 测试）。
    return sp.eval_assertions(assertions(spec), readback or {})


def summarize(spec: dict) -> str:
    nodes = spec.get("nodes") or []
    edges = spec.get("edges") or []
    names = "、".join(str(n.get("id")) for n in nodes)
    return (f"{spec.get('blueprint')}:{spec.get('graph', 'EventGraph')} 落地 {len(nodes)} 个节点"
            f"（{names}）、{len(edges)} 条连线，变量 {len(spec.get('variables') or [])} 个，"
            f"已编译并复核通过")


# ------------------------------------------------------------------ 路由识别（宁缺毋滥）
# 稳定属于「蓝图图」域的词；命中这些 + 有蓝图上下文，且不像别的图域，才认。
_BP_CONTEXT = ("蓝图", "eventgraph", "event graph", "事件图", "beginplay", "receivebeginplay")
_GRAPH_WORDS = ("节点", "连线", "接线", "引脚", "连到", "连接", "beginplay", "receivebeginplay",
                "执行引脚", "执行线")
_EXCLUDE = ("动画蓝图", "animbp", "anim blueprint", "animgraph", "状态机", "材质图", "材质节点",
            "niagara", "控件图", "umg", "wbp_", "widget")


def recognize(text: str) -> bool:
    """一句话是否稳定属于蓝图图域。有蓝图上下文 + 图动作词，且不是别的图域。"""
    blob = str(text or "").lower().strip()
    if not blob:
        return False
    if any(tok in blob for tok in _EXCLUDE):
        return False
    if not any(tok in blob for tok in _BP_CONTEXT):
        return False
    return any(tok in blob for tok in _GRAPH_WORDS)


# ------------------------------------------------------------------ 对外入口
def build(step: dict, **kwargs) -> dict:
    """交付接口：跑完整闭环（无 fix_model 时离线/直接执行；调用方可注入修复口）。"""
    return sp.build_domain(sys.modules[__name__], step, **kwargs)
