# agent/fps_hud.py
"""FPS HUD 控件图的确定性落地（20261029 的第二个卡点）。

`edit_widget_blueprint` 在 monolith 下原本只有一条路：discover 猜一个 action、describe、exec
一次。这条路对「改控件图」根本不成立：

- `ui` 命名空间有 138 个 action，`compile` 这类针名同时命中 `compile_widget` 和
  `dump_blueprint_compile_log`，`_choose_action` 判不出唯一 → 3 次全灭，
  错误还写成「Monolith 不具备该能力」（20261029-013556 的 W3）；
- 就算挑中了 `compile_widget`，那也只是**编译**，一个控件都没加，
  却会让「控件图已改并编译通过」这条条款变成通过——那是假成功。

所以这里按仓库既有口径把合同写进代码：HUD 要哪些控件、放在哪个角、父类是谁，
都由代码决定；模型只负责「这一扇要不要做」。执行一次就够，全程零模型调用。
不是 FPS HUD 的句子返回 None，交回原来的 repair 路径。
"""
from __future__ import annotations

import re

# 合同控件：名字与 UFpsHUDWidget 的 BindWidgetOptional 字段一一对应。
# 左下血条 / 右下子弹 / 右手雷 / 右中载具名字、上下车提示、时速
#
# 坐标口径（20261029 修正）：CanvasPanel 的 offset 是**相对锚点**的，不是屏幕绝对坐标。
# 所以这里统一用 left/top 锚点 + 绝对屏幕位置，摆放确定、不看设计分辨率。
# 早先一版配了 bottom_left/bottom_right/center 预设却仍写 980/1620 这类绝对偏移，
# 控件会被推到视口外（锚点已经吃掉了一整屏）。
HUD_LAYOUT: tuple[tuple[str, str, dict, dict, str], ...] = (
    # (控件名, 锚点预设, position, size, 占位文案)
    ("txt_Health", "top_left", {"x": 40, "y": 1000}, {"x": 220, "y": 52}, "100"),
    ("txt_Ammo", "top_left", {"x": 1660, "y": 1000}, {"x": 220, "y": 46}, "12"),
    ("txt_Grenade", "top_left", {"x": 1660, "y": 950}, {"x": 220, "y": 46}, "0"),
    # 火箭筒弹药：AFpsPawn::GetRocketAmmoText 直接给 "火箭弹 2/3"，WBP 不用再拼。
    ("txt_RocketAmmo", "top_left", {"x": 1660, "y": 1060}, {"x": 280, "y": 46}, "火箭弹 3/3"),
    ("txt_VehicleName", "top_left", {"x": 700, "y": 430}, {"x": 520, "y": 42}, "简易载具"),
    ("txt_EnterPrompt", "top_left", {"x": 700, "y": 480}, {"x": 520, "y": 42}, "按 E 上车"),
    ("txt_VehicleSpeed", "top_left", {"x": 700, "y": 530}, {"x": 520, "y": 42}, "0 km/h"),
)

HUD_PARENT = "/Script/AIAgentProject.FpsHUDWidget"
HUD_DEFAULT_REF = "/Game/UI/WBP_FpsHUD.WBP_FpsHUD"

# 图形血条（20261117）：「血条」不能只是一个文本 "100"。系统在左下角给它配一套
# 背景底条(Border·半透明黑) + 填充条(ProgressBar·满血绿) + 数值(复用 txt_Health 叠在上面)。
# 这两个是展示层附加控件，不进 FPS_HUD_WIDGETS 合同（那是 C++ BindWidgetOptional 的文本名，
# 与 sequence.FPS_HUD_WIDGETS 对齐）；合同名一个字都不动。
HEALTH_BAR_BG = "HealthBarBG"
HEALTH_BAR_FILL = "HealthBarFill"
HEALTH_BAR_VISUALS: tuple[str, ...] = (HEALTH_BAR_BG, HEALTH_BAR_FILL)
_HB_POS = {"x": 40, "y": 1000}
_HB_SIZE = {"x": 280, "y": 44}
_HB_FILL_POS = {"x": 46, "y": 1006}
_HB_FILL_SIZE = {"x": 268, "y": 32}
_HB_BG_COLOR = {"R": 0.0, "G": 0.0, "B": 0.0, "A": 0.55}
_HB_FILL_COLOR = {"R": 0.12, "G": 0.80, "B": 0.25, "A": 1.0}

# 句子里出现这些词，才认为它在要这些读数里的东西。
_HUD_TOKENS = ("血条", "血量", "子弹", "弹药", "手雷", "载具", "时速", "上下车", "HUD")

# 「编译代码/编译工程」——点名要编译。排除「编译蓝图/编译控件」，那是另一扇。
_CODE_COMPILE_RE = re.compile(r"(?<!图)(?<!蓝图)编译(?!蓝图|控件|图|BP_|WBP_)")

# 点名要一段玩法实现（不是只要 HUD）：动词 + 玩法词。只留确定性强的几种说法，
# 太宽会把「右中部显示载具信息」这类纯 HUD 句子误判成要改 C++。
_GAMEPLAY_VERBS = r"(实现|做|加|加个|加上|新增|增加|改成|改成个)"
_GAMEPLAY_TOKENS = r"(T\s*键|按\s*T|射击|开火|火箭筒|火箭弹|发射|游戏玩法|玩法)"
_GAMEPLAY_RE = re.compile(_GAMEPLAY_VERBS + r"[^，,。；;]{0,20}?" + _GAMEPLAY_TOKENS)

# 玩法落在哪个 C++ 类上：本项目 FPS 玩法的落点是 AFpsPawn（人 + 枪 + HUD 挂载）。
# 句子点名别的 A 前缀类就照点名的那个走；认不出退回 AFpsPawn（合同判据会兜住）。
_CLASS_RE = re.compile(r"\b(A[A-Z][A-Za-z0-9_]{2,})\b")


def wants_code_compile(text: str) -> bool:
    """这句点名要编译代码/编译工程。"""
    return bool(_CODE_COMPILE_RE.search(str(text or "")))


def wants_gameplay(text: str) -> bool:
    """这句点名要一段玩法实现（T 键/射击/火箭筒/玩法 + 实现类动词）。"""
    return bool(_GAMEPLAY_RE.search(str(text or "")))


def gameplay_class(text: str) -> str:
    """玩法落在哪个 C++ 类：句子点名的唯一 A 类，否则本项目 FPS 玩法的落点 AFpsPawn。"""
    names = {name for name in _CLASS_RE.findall(str(text or ""))}
    if len(names) == 1:
        return names.pop()
    return "AFpsPawn"



def is_fps_hud_request(text: str) -> bool:
    blob = str(text or "")
    if not any(token in blob for token in _HUD_TOKENS):
        return False
    # 点了控件名就更确定：这些名字只有本合同用。
    if any(name in blob for name, *_ in HUD_LAYOUT):
        return True
    # 或者同时说了两类以上读数（血条 + 子弹/手雷/载具），才当成完整 HUD 需求。
    hits = {token for token in _HUD_TOKENS if token in blob}
    return len(hits) >= 2


# 读数词 → 合同控件。用于「只加一条血条」这类**单读数**请求：只落地它要的那几块，
# 不把整套 HUD 都塞进去（20261117-140229：「给 WBP_FpsHUD 左下角加一个血条」曾整体掉进
# 通用受控求解、用无关的 AIAgent.Fps.Hit automation 收口——需求根本没被拆成控件动作）。
_HUD_READOUT: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("血条", "血量", "生命值"), ("txt_Health",)),
    (("子弹", "弹药"), ("txt_Ammo",)),
    (("手雷", "手榴弹"), ("txt_Grenade",)),
    (("火箭弹", "火箭筒"), ("txt_RocketAmmo",)),
    (("载具", "车辆"), ("txt_VehicleName", "txt_EnterPrompt", "txt_VehicleSpeed")),
    (("上下车", "上车", "下车"), ("txt_EnterPrompt",)),
    (("时速", "车速"), ("txt_VehicleSpeed",)),
)


def requested_hud_controls(text: str) -> list[str]:
    """这句话要点哪几块读数 → 需要的合同控件名（按 HUD_LAYOUT 顺序、去重）。

    口径：
    - 命中两类及以上读数，或只提「HUD / 控件 / 视口」这种泛称 → **整份合同**（保持既有行为，
      整句 FPS HUD 仍摆齐七块）；
    - 只命中一类读数（如只说「血条」）→ **只落地这一类**（单读数请求不越权补别的读数）；
    - 句子里直接点了控件名（txt_Health…）→ 就按点的那个来。
    """
    blob = str(text or "")
    all_names = [name for name, *_ in HUD_LAYOUT]
    categories: list[tuple[str, ...]] = []
    for tokens, names in _HUD_READOUT:
        if any(token in blob for token in tokens):
            categories.append(names)
    explicit = [name for name in all_names if name in blob]
    if len(categories) >= 2:
        return all_names
    picked: list[str] = []
    for names in categories:
        for name in names:
            if name not in picked:
                picked.append(name)
    for name in explicit:
        if name not in picked:
            picked.append(name)
    if not picked:
        return all_names
    return [name for name in all_names if name in picked]


# 删除/新增意图：一句「先删血条再加一条」要两件事都做，不能只当「补控件」。
_REMOVE_RE = re.compile(r"(删除|删掉|移除|去掉|清除|删了|抹掉|清掉|拿下)")
_ADD_RE = re.compile(r"(添加|加上|新增|增加|加一个|加个|做一个|新建|摆放|放一个|放入|显示|补上|补一|补|摆)")


def wants_remove_controls(text: str) -> bool:
    """这句要删/移除合同控件吗（删除…血条 / 移除…控件）。"""
    return bool(_REMOVE_RE.search(str(text or "")))


def wants_add_controls(text: str) -> bool:
    """这句要把合同控件摆上去吗。没写删除动作时默认按「补/摆」处理（保持原行为）。"""
    blob = str(text or "")
    if _ADD_RE.search(blob):
        return True
    return not _REMOVE_RE.search(blob)


def hud_expectation(text: str) -> str:
    """复核口径：先删后加 / 只加 ⇒ present；只删 ⇒ absent。"""
    if wants_remove_controls(text) and not wants_add_controls(text):
        return "absent"
    return "present"


def widget_ref_from(step: dict) -> str:
    """从窗口/任务里取控件路径：显式 ref_path/asset_name 优先，其次句子，最后默认合同路径。"""
    step = step or {}
    ref = str(step.get("ref_path") or "").strip()
    if ref:
        # 已经是 /Game/UI/WBP_X.WBP_X 就原样；只给了 /Game/UI/WBP_X 就补对象名。
        package, _, obj = ref.partition(".")
        if obj:
            return ref
        name = package.rsplit("/", 1)[-1]
        return f"{package}.{name}"
    asset = str(step.get("asset_name") or "").strip()
    if asset.startswith("WBP_"):
        folder = str(step.get("folder_path") or "/Game/UI").rstrip("/")
        return f"{folder}/{asset}.{asset}"
    blob = str(step.get("query") or step.get("title") or "")
    paths = [
        item for item in dict.fromkeys(re.findall(r"/Game(?:/[A-Za-z0-9_]+)+", blob))
        if item.rsplit("/", 1)[-1].startswith("WBP_")
    ]
    if len(paths) == 1:
        package = paths[0].split(".", 1)[0]
        return f"{package}.{package.rsplit('/', 1)[-1]}"
    names = list(dict.fromkeys(re.findall(r"(?<![A-Za-z0-9_])(WBP_[A-Za-z0-9_]+)", blob)))
    if len(names) == 1:
        return f"/Game/UI/{names[0]}.{names[0]}"
    return HUD_DEFAULT_REF


def _package(ref: str) -> str:
    return str(ref or "").split(".", 1)[0]


def _names(tree: list) -> list[str]:
    out = []
    for row in tree or []:
        if isinstance(row, dict):
            name = str(row.get("widgetName") or row.get("name") or "").strip()
            if name:
                out.append(name)
    return out


def build_fps_hud(step: dict) -> dict | None:
    """把合同控件落进目标 WBP。不是 FPS HUD 请求 → None（交回原路径）。

    返回结构与 `repair_blueprint` 一致：{ok, attempts, exec_count, summary/error, tools}。
    `tools` 里记真实写过的原语，run_windows 的「只读不算完成」检查读它。
    """
    from backends.select import current_backend

    if current_backend() != "monolith":
        return None
    text = " ".join(str((step or {}).get(key) or "") for key in ("query", "title", "summary"))
    if not is_fps_hud_request(text):
        return None
    # 只落地这句话要的那几块读数；单读数（如「加一个血条」）不越权补齐整套 HUD。
    wanted = requested_hud_controls(text)
    remove_intent = wants_remove_controls(text)
    add_intent = wants_add_controls(text)
    # 20261117：一句话里「先删旧血条、再加一条」是两个有序动作，规划层会把它们拆成
    # 「删除扇」「摆放扇」两扇（见 hud_windows）。每扇用 slots.hud_op 指明自己只做哪一半，
    # 不再都按整句文本同时删+加——否则删扇会顺手把加回来的又删了，两扇都错。
    slots = step.get("slots") if isinstance(step, dict) else {}
    hud_op = str((slots or {}).get("hud_op") or "").strip()
    if hud_op == "remove":
        remove_intent, add_intent = True, False
    elif hud_op == "add":
        remove_intent, add_intent = False, True

    from core.editor_exec import exec_tool

    ref = widget_ref_from(step or {})
    package = _package(ref)
    write = {
        "allow_writes": ["AddWidget", "SetWidgetProperty", "RenameWidget", "RemoveWidget",
                         "set_widget_property", "CompileWidgetBlueprint", "set_parent"],
        "save": True, "pie": False, "allow_umg": True,
    }
    tools: list[str] = []

    def _call(toolset: str, tool: str, args: dict) -> dict:
        out = exec_tool(toolset, tool, args, task=write)
        tools.append(tool)
        return out

    # ① 资产必须已经在（上一扇 create_widget_blueprint 负责建）。没有就诚实失败。
    tree = _call("UMGToolSet.UMGToolSet", "GetWidgets", {"widgetBlueprint": {"refPath": ref}})
    if not tree.get("ok"):
        return {
            "ok": False, "attempts": 1, "exec_count": 1, "tools": tools,
            "error": f"读不到控件树 {ref}：{tree.get('error') or '没有返回'}。先建这张 Widget Blueprint。",
        }
    existing = _names(tree.get("data"))

    # ② 父类必须是 FpsHUDWidget，否则 BindWidgetOptional 绑不上，HUD 永远是空文案。
    parent = _call("editor_toolset.toolsets.blueprint.BlueprintTools", "get_parent",
                   {"blueprint": {"refPath": ref}})
    parent_ref = ""
    data = parent.get("data")
    if isinstance(data, dict) and isinstance(data.get("returnValue"), dict):
        parent_ref = str(data["returnValue"].get("refPath") or "")
    if parent_ref and parent_ref != HUD_PARENT:
        fixed = _call("editor_toolset.toolsets.blueprint.BlueprintTools", "set_parent",
                      {"blueprint": {"refPath": ref}, "parent_class": {"refPath": HUD_PARENT}})
        if not fixed.get("ok"):
            return {
                "ok": False, "attempts": 1, "exec_count": len(tools), "tools": tools,
                "error": f"父类没设成 FpsHUDWidget：{fixed.get('error') or '未知错误'}",
            }

    # ③ 删除意图：句子里说「删除/移除…血条」时，先把这些控件 RemoveWidget 掉。
    #    「删了再加」= 先删后重建一条新的；纯删除则只删不加。
    removed: list[str] = []
    if remove_intent:
        for name in wanted:
            if name not in existing:
                continue
            gone = _call("UMGToolSet.UMGToolSet", "RemoveWidget", {
                "widgetBlueprint": {"refPath": ref}, "widget": {"refPath": name},
            })
            if not gone.get("ok"):
                return {
                    "ok": False, "attempts": 1, "exec_count": len(tools), "tools": tools,
                    "error": f"删除控件 {name} 失败：{gone.get('error') or '未知错误'}",
                }
            removed.append(name)
        existing = [name for name in existing if name not in removed]

    # ④ 缺哪个补哪个；已有的控件不重建（幂等：重跑这一句不会叠出第二份）。
    root = existing[0] if existing else "CanvasPanel"
    added: list[str] = []
    for name, preset, position, size, text_value in HUD_LAYOUT:
        if name not in wanted or not add_intent:
            continue
        if name in existing:
            # 已经在了也把锚点摆正：老文件里可能是上一版错位的偏移。
            _monolith_ui("set_anchor_preset", {
                "asset_path": package, "widget_name": name, "preset": preset,
            }, write, tools)
            _monolith_ui("set_slot_property", {
                "asset_path": package, "widget_name": name,
                "position": position, "size": size,
            }, write, tools)
            continue
        made = _call("UMGToolSet.UMGToolSet", "AddWidget", {
            "widgetBlueprint": {"refPath": ref},
            "widgetClass": {"refPath": "/Script/UMG.TextBlock"},
            "widgetDisplayName": name,
            "parentWidget": {"refPath": f"WidgetTree.{root}"},
            "position": position, "size": size,
            "h_align": "Left", "v_align": "Top",
        })
        if not made.get("ok"):
            return {
                "ok": False, "attempts": 1, "exec_count": len(tools), "tools": tools,
                "error": f"加控件 {name} 失败：{made.get('error') or '未知错误'}",
            }
        added.append(name)
        # 锚点与占位文案：monolith 的一等 action 只有 ui 命名空间有，走它们自己的名字。
        _monolith_ui("set_anchor_preset", {
            "asset_path": package, "widget_name": name, "preset": preset,
        }, write, tools)
        _monolith_ui("set_text", {
            "asset_path": package, "widget_name": name, "text": text_value,
            "font_size": 28, "text_color": "#FFFFFF",
        }, write, tools)

    # ⑤ 图形血条：要「血条」就把 背景底条(Border) + 填充条(ProgressBar) + 数值 一起做出来，
    #    不是只放一个文本 "100"。这就是「样式你自己定义、仿照常见 FPS」被系统兜住的地方。
    present = set(existing) | set(added)
    health_wanted = "txt_Health" in wanted
    if health_wanted and remove_intent:
        for vname in HEALTH_BAR_VISUALS:
            if vname not in present:
                continue
            gone = _call("UMGToolSet.UMGToolSet", "RemoveWidget", {
                "widgetBlueprint": {"refPath": ref}, "widget": {"refPath": vname},
            })
            if not gone.get("ok"):
                return {
                    "ok": False, "attempts": 1, "exec_count": len(tools), "tools": tools,
                    "error": f"删除血条图形控件 {vname} 失败：{gone.get('error') or '未知错误'}",
                }
            removed.append(vname)
            present.discard(vname)
    if health_wanted and add_intent:
        def _prop(wid: str, prop: str, val) -> None:
            _monolith_ui("set_widget_property", {
                "asset_path": package, "widget_name": wid,
                "property_name": prop, "value": val,
            }, write, tools)

        def _slot(wid: str, pos: dict, size: dict, z: int | None = None) -> None:
            params = {"asset_path": package, "widget_name": wid, "position": pos, "size": size}
            if z is not None:
                params["z_order"] = z
            _monolith_ui("set_slot_property", params, write, tools)

        def _add_widget(wname: str, wclass: str, pos: dict, size: dict) -> dict:
            return _call("UMGToolSet.UMGToolSet", "AddWidget", {
                "widgetBlueprint": {"refPath": ref},
                "widgetClass": {"refPath": wclass},
                "widgetDisplayName": wname,
                "parentWidget": {"refPath": f"WidgetTree.{root}"},
                "position": pos, "size": size, "h_align": "Left", "v_align": "Top",
            })

        if HEALTH_BAR_BG not in present:
            made_bg = _add_widget(HEALTH_BAR_BG, "/Script/UMG.Border", _HB_POS, _HB_SIZE)
            if not made_bg.get("ok"):
                return {
                    "ok": False, "attempts": 1, "exec_count": len(tools), "tools": tools,
                    "error": f"加血条底条失败：{made_bg.get('error') or '未知错误'}",
                }
            added.append(HEALTH_BAR_BG)
            present.add(HEALTH_BAR_BG)
        _monolith_ui("set_anchor_preset", {
            "asset_path": package, "widget_name": HEALTH_BAR_BG, "preset": "top_left"}, write, tools)
        _prop(HEALTH_BAR_BG, "BrushColor", _HB_BG_COLOR)
        _slot(HEALTH_BAR_BG, _HB_POS, _HB_SIZE, z=0)

        if HEALTH_BAR_FILL not in present:
            made_fill = _add_widget(HEALTH_BAR_FILL, "/Script/UMG.ProgressBar", _HB_FILL_POS, _HB_FILL_SIZE)
            if not made_fill.get("ok"):
                return {
                    "ok": False, "attempts": 1, "exec_count": len(tools), "tools": tools,
                    "error": f"加血条填充条失败：{made_fill.get('error') or '未知错误'}",
                }
            added.append(HEALTH_BAR_FILL)
            present.add(HEALTH_BAR_FILL)
        _monolith_ui("set_anchor_preset", {
            "asset_path": package, "widget_name": HEALTH_BAR_FILL, "preset": "top_left"}, write, tools)
        _prop(HEALTH_BAR_FILL, "Percent", 1.0)
        _prop(HEALTH_BAR_FILL, "FillColorAndOpacity", _HB_FILL_COLOR)
        _slot(HEALTH_BAR_FILL, _HB_FILL_POS, _HB_FILL_SIZE, z=1)

        # 数值叠在血条上，居中白字（C++ 运行期仍会刷新 txt_Health 的文本值）。
        _prop("txt_Health", "Justification", "Center")
        _slot("txt_Health", _HB_FILL_POS, _HB_FILL_SIZE, z=2)
        _monolith_ui("set_text", {
            "asset_path": package, "widget_name": "txt_Health", "text": "100",
            "font_size": 22, "text_color": "#FFFFFF",
        }, write, tools)

    # ⑥ 编译 + 保存这一份资产（裁判是编译，保存由任务策略决定）。
    compiled = _call("UMGToolSet.UMGToolSet", "CompileWidgetBlueprint",
                     {"widgetBlueprint": {"refPath": ref}})
    if not compiled.get("ok"):
        return {
            "ok": False, "attempts": 1, "exec_count": len(tools), "tools": tools,
            "error": f"{ref} 编译失败：{compiled.get('error') or '未知错误'}",
        }
    _call("editor_toolset.toolsets.asset.AssetTools", "save_assets",
          {"asset_paths": [package]})

    # ⑤ 复查：按意图核对（先删后加/只加 ⇒ 必须在；只删 ⇒ 必须不在）。
    again = _call("UMGToolSet.UMGToolSet", "GetWidgets", {"widgetBlueprint": {"refPath": ref}})
    final = _names(again.get("data"))
    expectation = "absent" if hud_op == "remove" else ("present" if hud_op == "add" else hud_expectation(text))
    if expectation == "absent":
        still = [name for name in wanted if name in final]
        if still:
            return {
                "ok": False, "attempts": 1, "exec_count": len(tools), "tools": tools,
                "error": f"{ref} 删除后仍在：{'、'.join(still)}",
            }
        summary = (
            f"{ref} 已删除控件 {'、'.join(removed) if removed else '（原本就没有）'}，"
            f"已编译并保存这份资产。"
        )
    else:
        missing = [name for name in wanted if name not in final]
        if missing:
            return {
                "ok": False, "attempts": 1, "exec_count": len(tools), "tools": tools,
                "error": f"{ref} 复查缺控件：{'、'.join(missing)}",
            }
        bits = []
        if removed:
            bits.append("删除 " + "、".join(removed))
        bits.append("新增 " + ("、".join(added) if added else "无，原本就在"))
        if health_wanted and add_intent:
            bits.append("图形血条（底条+填充条+数值）已就位")
        summary = (
            f"{ref} 合同控件齐（{'；'.join(bits)}），"
            f"父类 FpsHUDWidget，已编译并保存这份资产。"
        )
    return {
        "ok": True, "attempts": 1, "exec_count": len(tools), "tools": tools,
        "summary": summary, "index_note": summary, "ref": ref,
        "judge_kind": "widget_graph",
        "test": {"ok": True, "kind": "widget_graph", "summary": summary, "errors": []},
    }


def hud_windows(query: str) -> list[dict] | None:
    """整句 FPS HUD 需求的确定性窗口清单（零模型调用）。

    为什么要有这一条：这句话结构固定（打开编辑器 → 开关卡 → 建/改控件蓝图 → 只读复核），
    但此前必须先叫一次理解器。理解器是老模型单点，20261029-115122 / 115302 两次
    返回纯文本，整句就直接掉进求解扇并失败——需求根本没被拆开。
    拆句是纯规则，不该依赖模型可用性。

    返回 None 表示「这句不是完整的 FPS HUD 需求」，交回 L0/L1 原路径。
    """
    text = str(query or "")
    if not is_fps_hud_request(text):
        return None

    # 门只认整句（20261111）：这条链只认领得住整句的纯 HUD 需求。
    # 句子里还有这条链接不住的半句（滑铲/冲刺/改速度…）时返回 None，交回 L1/求解，
    # 不把整句领走后让后半句静默丢掉（混句实测：HUD 摆了，玩法半句蒸发）。
    planned_actions: list[str] = []
    if "打开编辑器" in text or "启动编辑器" in text:
        planned_actions.append("launch_editor")
    if _level_token(text):
        planned_actions.append("open_level")
    planned_actions += ["create_widget_blueprint", "edit_widget_blueprint", "check_fps_hud"]
    from agent.plan_windows import _clauses_accounted

    if not _clauses_accounted(text, [{"action": action} for action in planned_actions]):
        return None

    from agent.plan_windows import materialize

    ref = widget_ref_from({"query": text})
    asset = ref.rsplit("/", 1)[-1].split(".", 1)[0]
    folder = ref.rsplit("/", 1)[0]
    # 只加一条血条时，复核扇也只看这一块（否则「缺 txt_Ammo」会把单读数请求判成失败）。
    controls = requested_hud_controls(text)
    expect = hud_expectation(text)
    windows: list[dict] = []

    def _add(action: str, slots: dict, title: str) -> None:
        """顺序链：每一扇依赖上一扇。这里是硬因果（先有资产才有控件树，才有复核），
        不是可选优化。20261029-123341 的 W5 就是没连依赖、在 W4 补控件之前就跑，
        于是判「缺五个控件」——检查扇抢跑等于把正确结果报成失败。"""
        deps = [windows[-1]["id"]] if windows else []
        windows.append(materialize(action, slots, title=title,
                                   wid=f"W{len(windows) + 1}", depends=deps,
                                   retries=1, text=text))


    if "打开编辑器" in text or "启动编辑器" in text:
        _add("launch_editor", {}, "启动 UE 编辑器")
    level = _level_token(text)
    if level:
        _add("open_level", {"level": level}, f"打开关卡 {level}")
    _add("create_widget_blueprint",
         {"folder": folder, "name": asset, "parent": "UserWidget"},
         f"新建/定位 {asset}")
    # 20261117：把「删除旧血条」与「新增血条」拆成两扇独立窗口，删除动作在窗口图上看得见。
    # 之前删+加都塞进同一扇「摆放 HUD 控件」，运行时确实删了（RemoveWidget），但面板上
    # 只显示「摆放…」，用户看不到「删除」这一步（185234 反馈）。删除扇只删、摆放扇只加，
    # 各自带 slots.hud_op 让 build_fps_hud 只做一半。
    edit_slots = {"folder": folder, "name": asset, "hud_controls": controls}
    remove_intent = wants_remove_controls(text)
    add_intent = wants_add_controls(text)
    if remove_intent and add_intent:
        _add("edit_widget_blueprint",
             {**edit_slots, "hud_op": "remove"},
             f"删除 {asset} 的旧血条控件")
        _add("edit_widget_blueprint",
             {**edit_slots, "hud_op": "add"},
             f"摆放 {asset} 的 HUD 控件（新增血条）")
    elif remove_intent:
        _add("edit_widget_blueprint",
             {**edit_slots, "hud_op": "remove"},
             f"删除 {asset} 的血条控件")
    else:
        _add("edit_widget_blueprint",
             {**edit_slots, "hud_op": "add"},
             f"摆放 {asset} 的 HUD 控件")
    _add("check_fps_hud",
         {"widget": ref, "hud_controls": controls, "hud_expect": expect},
         ("只读核对 FPS HUD（应已删除）：" if expect == "absent" else "只读核对 FPS HUD：")
         + "、".join(controls))
    return windows


def _level_token(text: str) -> str:
    """句中的 L_ 关卡名；没有就空串（不猜一个默认关卡）。"""
    found = re.findall(r"(L_[A-Za-z0-9_]+)", str(text or ""))
    return found[0] if len(set(found)) == 1 else ""


def _monolith_ui(action: str, params: dict, task: dict, tools: list[str]) -> dict:
    """monolith 原生 ui action（没有 official 对应原语的那几个）。失败不中断主链。"""
    from backends.monolith import exec as monolith_exec

    tools.append(action)
    try:
        return monolith_exec("ui", action, params, task=task)
    except Exception as exc:  # 锚点/文案是表现层，失败不阻断「控件在不在」这条硬合同
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
