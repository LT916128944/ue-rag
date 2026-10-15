# agent/catalog.py
"""规划者能用的动作。表外的 action 不执行。integrate_verify 只由代码追加，规划者写不出。"""

INTEGRATE_ACTION = "integrate_verify"

CATALOG = {
    "compile_project": {
        "slots": (),
        "summary": "编译整个工程，不改源码。这句话里要启动编辑器时，编译通过后再启动。",
    },
    "compile_blueprint": {
        "slots": ("blueprint",),
        "summary": "编译一张已经点名的蓝图。不改图。",
    },
    "edit_cpp": {
        "slots": ("class",),
        "summary": "只改一个 C++ 类。类不是唯一的一条就暂停。",
    },
    "bind_blueprint": {
        "slots": ("class", "blueprint"),
        "mode": "one",
        "summary": "解析唯一蓝图。没有资产、而且这句话里已经给了 /Game 路径时才新建，不改图。",
    },
    "place_actor": {
        "slots": ("blueprint",),
        "summary": "把蓝图放进当前关卡。不保存。",
    },
    "judge_pickup": {
        "slots": ("target", "picker"),
        "summary": "只读裁决能否拾取。不写标签。距离不够仍是裁决结果。",
    },
    "set_pickable": {
        "slots": ("target",),
        "summary": "让场景里的实例可拾取：bPickable 为真，并带 InteractableActor。没有这个实例就失败。",
    },
    "move_actor": {
        "slots": ("actor", "location"),
        "summary": "把一个已经点名的 Actor 移到给出的坐标。不保存。",
    },
    INTEGRATE_ACTION: {
        "slots": (),
        "summary": "集成矩阵验收：重复请求幂等、拒答路径、延迟窗口、断线重连、未知态恢复，各一个子判定；"
                   "产出触及文件哈希、token 汇总与归档。只在任务书确认后由代码追加，规划者不要写它。",
    },
}

DIRECT_ACTIONS = frozenset({
    "set_pickable",
    "move_actor",
    "probe_move",
    "probe_look",
    "probe_play",
    "judge_pickup",
    "check_pickup",
    "place_actor",
    "place_blueprint",
    "place_pair",
    "bind_blueprint",
    "bind_slot",
    "create_blueprint",
})

READ_ONLY_ACTIONS = frozenset({
    "judge_pickup",
    "check_pickup",
})

GRAPH_ACTIONS = frozenset({
    "compile_project",
    "compile_blueprint",
    "edit_cpp",
})

EDITOR_LOCK_ACTIONS = frozenset({
    "compile_project",
    "compile_blueprint",
    "edit_cpp",
    "set_pickable",
    "move_actor",
    "probe_move",
    "probe_look",
    "probe_play",
    "place_actor",
    "place_blueprint",
    "place_pair",
    "bind_blueprint",
    "bind_slot",
    "create_blueprint",
})


def system_prompt() -> str:
    """只有动作表。不放检索、技能或工具说明。"""
    lines = [
        "你只输出 JSON，把用户这一句话拆成窗口清单。",
        "action 只能取下面的名字，槽位名只能用这一行写出的。看不准就不要编新动作。",
        "每个窗口只有 id、title、action、slots、depends_on、max_retries。",
        "有依赖的窗口把 depends_on 写成上游 id。互不依赖的不要互相等待。不要解释。",
        "编译某一个 BP_ 用 compile_blueprint，槽位 blueprint 填那个名字。只有「编译工程」才用 compile_project。",
        "打可交互标签用 set_pickable。让另一个对象与它交互用 judge_pickup，depends_on 指向上一扇。",
        "移动用 move_actor，location 写成 x,y,z。",
        "",
    ]
    for name, spec in CATALOG.items():
        if name == INTEGRATE_ACTION:
            continue
        slots = "、".join(spec["slots"]) if spec["slots"] else "无"
        if spec.get("mode") == "one":
            slots += "（有一个即可）"
        lines.append(f"- {name}。槽位：{slots}。{spec['summary']}")
    lines.extend([
        "",
        "integrate_verify 由代码在任务书确认后自动追加，你不要写它。",
        "输出形状：",
        '{"windows":[{"id":"W1","title":"这一扇要做完的事","action":"set_pickable","slots":{"target":"BP_名字"},"depends_on":[],"max_retries":1}]}',
    ])
    return "\n".join(lines)
