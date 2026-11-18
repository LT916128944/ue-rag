# agent/spec_toolmap.py
"""全域 Spec 内核的**单一事实源：真机核实的 monolith 原生工具清单 + 各域原语映射**。

20261117 边界升级 P0：此前各域 ``exec_op`` 的工具名要么是 official-style 家族名（经桥翻译），
要么逐处标注「待 P0 describe」。本文件把**当前后端（UE_EDITOR_BACKEND=monolith）真实存在的
动作清单**（用 ``backends.monolith.discover`` 对在线编辑器逐个命名空间 discover 得到，2026-11-17）
固化成数据，并为每个域的 kernel 原语指明对应动作——**能指到的就是 verified，指不到的如实登记为
缺口（pending）**，不再靠猜。

证据文件：``data/harness/monolith_tool_inventory.json``（8 个命名空间的全部动作名，真机 discover 快照）。

用法：
  - ``inventory()``            命名空间 → 动作集合；
  - ``has_action(ns, action)`` 该动作是否真实存在（离线比对快照）；
  - ``lookup(domain, op)``     某域某原语 → 核实过的 (namespace, action) 或缺口说明；
  - ``is_namespace(name)``     一个 toolset 串是不是 monolith 命名空间（default_call 据此路由）。

诚实边界：这里核实的是**动作名存在**；每个动作的**入参形状**仍需 ``--describe`` 单点确认
（见 ``PENDING_ARGS``）。名字对、参数没对，真机仍可能失败——所以真机冒烟（P1）仍需按任务
放开写权限后单独做。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

REF = "2026-11-17 backends.monolith.discover（在线编辑器逐个命名空间快照）"
_INVENTORY_FILE = Path(__file__).resolve().parents[1] / "data" / "harness" / "monolith_tool_inventory.json"


@lru_cache(maxsize=1)
def inventory() -> dict[str, frozenset[str]]:
    """命名空间 → 动作名集合。读不到就抛（不许静默空表）。"""
    data = json.loads(_INVENTORY_FILE.read_text(encoding="utf-8"))
    return {ns: frozenset(rows) for ns, rows in data.items()}


def is_namespace(name: str) -> bool:
    return str(name or "") in inventory()


def has_action(namespace: str, action: str) -> bool:
    return str(action or "") in inventory().get(str(namespace or ""), frozenset())


# ------------------------------------------------------------------ 各域原语 → 核实动作
# op: {"ns": 命名空间, "action": 动作名 或 None, "note": 说明}
# action 非 None 时必须能在 inventory 里查到（check_spec_toolmap 会离线断言）。
# action 为 None 表示「今天该后端没有对应写原语」——如实登记为缺口，不猜。
OPS: dict[str, dict[str, dict]] = {
    "umg": {
        "snapshot": {"ns": "ui", "action": "get_widget_tree", "note": "读控件树"},
        "readback": {"ns": "ui", "action": "get_widget_tree", "note": "复核控件树"},
        "ensure_widget": {"ns": "ui", "action": "add_widget", "note": "加控件（幂等：已在就跳过）"},
        "set_anchor": {"ns": "ui", "action": "set_anchor_preset", "note": "锚点"},
        "set_slot": {"ns": "ui", "action": "set_slot_property", "note": "槽位/位置尺寸"},
        "set_property": {"ns": "ui", "action": "set_widget_property", "note": "通用属性"},
        "set_variable": {"ns": "ui", "action": "set_widget_is_variable", "note": "暴露成命名变量（bIsVariable）"},
        "set_brush": {"ns": "ui", "action": "set_brush", "note": "画刷"},
        "compile": {"ns": "ui", "action": "compile_widget", "note": "编译控件图"},
        "list_props": {"ns": "ui", "action": "list_widget_properties", "note": "抽读属性"},
        "save": {"ns": "editor", "action": "save_packages", "note": "保存资产"},
    },
    "graph": {
        "snapshot": {"ns": "blueprint", "action": "get_graph_summary", "note": "读图（get_graph_data 需过滤，用 summary）"},
        "readback": {"ns": "blueprint", "action": "get_graph_summary", "note": "复核图"},
        "add_variable": {"ns": "blueprint", "action": "add_variable", "note": "成员变量"},
        "add_event": {"ns": "blueprint", "action": "add_event_node", "note": "事件桩"},
        "add_node": {"ns": "blueprint", "action": "add_node", "note": "按 type_id 建节点"},
        "set_pin_value": {"ns": "blueprint", "action": "set_pin_default", "note": "引脚默认值"},
        "connect": {"ns": "blueprint", "action": "connect_pins", "note": "引脚级连线"},
        "compile": {"ns": "blueprint", "action": "compile_blueprint", "note": "编译（硬裁判）"},
        "save": {"ns": "blueprint", "action": "save_asset", "note": "保存蓝图"},
    },
    "animgraph": {
        "snapshot": {"ns": "animation", "action": "get_graphs", "note": "读动画图"},
        "readback": {"ns": "animation", "action": "get_nodes", "note": "复核动画节点"},
        "add_variable": {"ns": "blueprint", "action": "add_variable", "note": "ABP 变量走蓝图命名空间"},
        "create_node": {"ns": "animation", "action": "add_anim_graph_node", "note": "动画图节点（如 SequencePlayer）"},
        "set_pin_value": {"ns": "animation", "action": "set_anim_graph_node_property", "note": "设动画节点属性"},
        "connect": {"ns": "animation", "action": "connect_anim_graph_pins", "note": "动画图连线"},
        "create_state_machine": {"ns": "animation", "action": "create_state_machine", "note": "状态机"},
        "add_state": {"ns": "animation", "action": "add_state_to_machine", "note": "加状态"},
        "add_transition": {"ns": "animation", "action": "add_transition", "note": "加转换"},
        "set_transition_rule": {"ns": "animation", "action": "set_transition_rule", "note": "转换规则"},
        "set_state_animation": {"ns": "animation", "action": "set_state_animation", "note": "状态引用动画"},
        "compile": {"ns": "blueprint", "action": "compile_blueprint", "note": "ABP 按蓝图编译"},
    },
    "material": {
        "readback": {"ns": "material", "action": "get_instance_parameters", "note": "读 MI 参数"},
        "get_params": {"ns": "material", "action": "get_material_parameters", "note": "读材质参数"},
        "set_param": {"ns": "material", "action": "set_instance_parameter", "note": "设 MI 参数（scalar/vector/texture）"},
        "recompile": {"ns": "material", "action": "recompile_material", "note": "重编译"},
        "graph_set_prop": {"ns": "material", "action": "set_expression_property", "note": "材质图节点属性"},
        "graph_connect": {"ns": "material", "action": "connect_expressions", "note": "材质图连线（名称已核实存在）"},
        "save": {"ns": "material", "action": "save_material", "note": "保存材质"},
    },
    "niagara": {
        "readback": {"ns": "niagara", "action": "get_parameter_value", "note": "读 User 参数"},
        "list_emitters": {"ns": "niagara", "action": "list_emitters", "note": "列发射器"},
        "add_emitter": {"ns": "niagara", "action": "add_emitter", "note": "加发射器"},
        "add_module": {"ns": "niagara", "action": "add_module", "note": "加模块"},
        "set_module_input": {"ns": "niagara", "action": "set_module_input_value", "note": "设模块输入"},
        "add_user_param": {"ns": "niagara", "action": "add_user_parameter", "note": "加 User 参数"},
        "set_param_default": {"ns": "niagara", "action": "set_parameter_default", "note": "设参数默认值"},
        "system_summary": {"ns": "niagara", "action": "get_system_summary", "note": "系统摘要"},
        "save": {"ns": "niagara", "action": "save_system", "note": "保存系统"},
        "add_component": {"ns": "blueprint", "action": "add_component", "note": "NiagaraComponent 挂在 Actor 蓝图上（非 niagara 命名空间）"},
    },
    "animation": {
        "readback": {"ns": "animation", "action": "get_bone_track_keys", "note": "读骨骼关键帧"},
        "list_tracks": {"ns": "animation", "action": "list_bone_tracks", "note": "列骨骼轨道"},
        "add_track": {"ns": "animation", "action": "add_bone_track", "note": "加骨骼轨道"},
        "set_keys": {"ns": "animation", "action": "set_bone_track_keys", "note": "打关键帧"},
        "create_sequence": {"ns": "animation", "action": "create_sequence", "note": "建序列"},
        "controlrig_info": {"ns": "animation", "action": "get_control_rig_info", "note": "读 ControlRig"},
        "add_control": {"ns": "animation", "action": "add_control_rig_element", "note": "加控件"},
        "add_rig_node": {"ns": "animation", "action": "add_control_rig_node", "note": "加 rig 图节点"},
        "connect_rig_pins": {"ns": "animation", "action": "connect_control_rig_pins", "note": "rig 图连线"},
        "save": {"ns": "editor", "action": "save_packages", "note": "保存"},
    },
    "sequencer": {
        "list_bindings": {"ns": "level_sequence", "action": "list_bindings", "note": "列绑定（只读）"},
        "director_info": {"ns": "level_sequence", "action": "get_director_info", "note": "序列信息（只读）"},
        "list_directors": {"ns": "level_sequence", "action": "list_directors", "note": "列序列（只读）"},
        "create_sequence": {"ns": "animation", "action": "create_sequence", "note": "建序列走 animation 命名空间"},
        "add_binding": {"ns": "level_sequence", "action": None, "note": "level_sequence 仅 8 个只读动作，无 AddBinding 写原语——待确认"},
        "add_track": {"ns": "level_sequence", "action": None, "note": "level_sequence 无 AddTrack；Transform/CameraCut 轨道写入原语待确认"},
        "add_keyframe": {"ns": "animation", "action": "set_bone_track_keys", "note": "仅骨骼轨道关键帧；通用 Transform 轨道待确认"},
        "set_range": {"ns": "level_sequence", "action": None, "note": "无 SetPlaybackRange 写原语——待确认"},
        "save": {"ns": "editor", "action": "save_packages", "note": "保存"},
    },
    "postprocess": {
        "find_volume": {"ns": "editor", "action": "author_map_settings", "note": "关卡设置；PostProcessVolume 定位/建需确认"},
        "get_props": {"ns": "blueprint", "action": "get_cdo_properties", "note": "CDO 属性读；放置实例属性路径待确认"},
        "set_props": {"ns": "blueprint", "action": "set_property_at_path", "note": "属性路径写（对象属性路径支持待确认）"},
        "save": {"ns": "editor", "action": "save_packages", "note": "保存"},
    },
    "cpp": {
        "build": {"ns": "editor", "action": "trigger_build", "note": "触发编译"},
        "build_status": {"ns": "editor", "action": "get_build_status", "note": "编译状态"},
        "build_errors": {"ns": "editor", "action": "get_build_errors", "note": "编译错误（回灌）"},
        "source_apply": {"ns": "", "action": None, "note": "源码读写在 core.source_edit，不经编辑器命名空间"},
        "read_source": {"ns": "", "action": None, "note": "同上"},
    },
}

# 参数形状已用 --describe 核实的动作（名字 + 入参都核实）。key = "namespace.action"。
# 真机 describe 2026-11-17 快照；required/optional 为顶层入参名。
VERIFIED_ARGS: dict[str, dict] = {
    "material.set_instance_parameter": {
        "required": ["asset_path", "parameter_name"],
        "optional": ["scalar_value", "vector_value", "texture_value", "switch_value"],
    },
    "blueprint.add_node": {
        "required": ["asset_path", "node_type"],
        "optional": ["graph_name", "position", "function_name", "target_class", "variable_name",
                     "event_name", "macro_name", "macro_blueprint", "cast_class", "actor_class",
                     "struct_type", "enum_type", "format", "num_entries", "replication", "reliable",
                     "component_name", "delegate_property_name"],
    },
    "blueprint.connect_pins": {
        "required": ["asset_path", "source_node", "source_pin", "target_node", "target_pin"],
        "optional": ["graph_name"],
    },
    "ui.add_widget": {
        "required": ["asset_path", "widget_class"],
        "optional": ["widget_name", "parent_name", "anchor_preset", "position", "size", "padding",
                     "h_align", "v_align", "auto_size", "compile"],
    },
    "animation.set_bone_track_keys": {
        "required": ["asset_path", "bone_name", "positions_json", "rotations_json", "scales_json"],
        "optional": [],
    },
    "niagara.add_user_parameter": {
        "required": ["asset_path", "name", "type"],
        "optional": ["default"],
    },
}

# 参数形状仍需 --describe 单点确认的动作（名字已核实，入参未核实）。
PENDING_ARGS: tuple[tuple[str, str], ...] = (
    ("animation", "add_anim_graph_node"),
    ("niagara", "set_module_input_value"),
)


def lookup(domain: str, op: str) -> dict:
    """某域某原语 → {ns, action, verified, note}。找不到就抛（强制登记，不许漏）。"""
    row = (OPS.get(domain) or {}).get(op)
    if row is None:
        raise KeyError(f"toolmap 未登记 {domain}.{op}")
    action = row.get("action")
    return {
        "ns": row.get("ns") or "",
        "action": action,
        "verified": bool(action) and has_action(row.get("ns") or "", action),
        "note": str(row.get("note") or ""),
    }


def domains() -> tuple[str, ...]:
    return tuple(OPS.keys())


def verified_ops() -> list[tuple[str, str, str, str]]:
    """[(domain, op, ns, action)]，只列能核实到真实动作的。"""
    out = []
    for domain, ops in OPS.items():
        for op, row in ops.items():
            if row.get("action") and has_action(row.get("ns") or "", row["action"]):
                out.append((domain, op, row["ns"], row["action"]))
    return out


def pending_ops() -> list[tuple[str, str, str]]:
    """[(domain, op, note)]，只列当前后端没有写原语的缺口。"""
    return [(domain, op, str(row.get("note") or ""))
            for domain, ops in OPS.items() for op, row in ops.items() if not row.get("action")]
