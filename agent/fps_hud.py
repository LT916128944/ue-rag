# agent/fps_hud.py
"""FPS HUD 控件图的确定性落地（20261029 的第二个卡点）。

`edit_widget_blueprint` 在 monolith 下原本只有一条路：discover 猜一个 action、describe、exec
一次。这条路对「改控件图」根本不成立：

- `ui` 命名空间有 138 个 action，`compile` 这类针名同时命中 `compile_widget` 和
  `dump_blueprint_compile_log`，`_choose_action` 判不出唯一 → 3 次全灭，
  错误还写成「Monolith 不具备该能力」（20261029-013556 的 W3）；
- 就算挑中了 `compile_widget`，那也只是**编译**，一个控件都没加，
  却会让「控件图已改并编译通过」这条条款变成通过——那是假成功。

所以这里按仓库既有口径把合同写进代码：HUD 要哪六个控件、放在哪个角、父类是谁，
都由代码决定；模型只负责「这一扇要不要做」。执行一次就够，全程零模型调用。
不是 FPS HUD 的句子返回 None，交回原来的 repair 路径。
"""
from __future__ import annotations

import re

# 六个合同控件：名字与 UFpsHUDWidget 的 BindWidgetOptional 字段一一对应。
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
    ("txt_VehicleName", "top_left", {"x": 700, "y": 430}, {"x": 520, "y": 42}, "简易载具"),
    ("txt_EnterPrompt", "top_left", {"x": 700, "y": 480}, {"x": 520, "y": 42}, "按 E 上车"),
    ("txt_VehicleSpeed", "top_left", {"x": 700, "y": 530}, {"x": 520, "y": 42}, "0 km/h"),
)

HUD_PARENT = "/Script/AIAgentProject.FpsHUDWidget"
HUD_DEFAULT_REF = "/Game/UI/WBP_FpsHUD.WBP_FpsHUD"

# 句子里出现这些词，才认为它在要这六个读数里的东西。
_HUD_TOKENS = ("血条", "血量", "子弹", "弹药", "手雷", "载具", "时速", "上下车", "HUD")


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
    """把六个合同控件落进目标 WBP。不是 FPS HUD 请求 → None（交回原路径）。

    返回结构与 `repair_blueprint` 一致：{ok, attempts, exec_count, summary/error, tools}。
    `tools` 里记真实写过的原语，run_windows 的「只读不算完成」检查读它。
    """
    from backends.select import current_backend

    if current_backend() != "monolith":
        return None
    text = " ".join(str((step or {}).get(key) or "") for key in ("query", "title", "summary"))
    if not is_fps_hud_request(text):
        return None

    from core.editor_exec import exec_tool

    ref = widget_ref_from(step or {})
    package = _package(ref)
    write = {
        "allow_writes": ["AddWidget", "SetWidgetProperty", "RenameWidget",
                         "CompileWidgetBlueprint", "set_parent"],
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

    # ② 父类必须是 FpsHUDWidget，否则六个 BindWidgetOptional 绑不上，HUD 永远是空文案。
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

    # ③ 缺哪个补哪个；已有的控件不重建（幂等：重跑这一句不会叠出第二份）。
    root = existing[0] if existing else "CanvasPanel"
    added: list[str] = []
    for name, preset, position, size, text_value in HUD_LAYOUT:
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

    # ④ 编译 + 保存这一份资产（裁判是编译，保存由任务策略决定）。
    compiled = _call("UMGToolSet.UMGToolSet", "CompileWidgetBlueprint",
                     {"widgetBlueprint": {"refPath": ref}})
    if not compiled.get("ok"):
        return {
            "ok": False, "attempts": 1, "exec_count": len(tools), "tools": tools,
            "error": f"{ref} 编译失败：{compiled.get('error') or '未知错误'}",
        }
    _call("editor_toolset.toolsets.asset.AssetTools", "save_assets",
          {"asset_paths": [package]})

    # ⑤ 复查：六个名字必须真的都在（不信「调用成功」）。
    again = _call("UMGToolSet.UMGToolSet", "GetWidgets", {"widgetBlueprint": {"refPath": ref}})
    final = _names(again.get("data"))
    missing = [name for name, *_ in HUD_LAYOUT if name not in final]
    if missing:
        return {
            "ok": False, "attempts": 1, "exec_count": len(tools), "tools": tools,
            "error": f"{ref} 复查缺控件：{'、'.join(missing)}",
        }
    summary = (
        f"{ref} 六个合同控件齐（新增 {'、'.join(added) if added else '无，原本就在'}），"
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

    from agent.plan_windows import materialize

    ref = widget_ref_from({"query": text})
    asset = ref.rsplit("/", 1)[-1].split(".", 1)[0]
    folder = ref.rsplit("/", 1)[0]
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
    _add("edit_widget_blueprint",
         {"folder": folder, "name": asset},
         f"摆放 {asset} 的六个 HUD 控件")
    _add("check_fps_hud", {"widget": ref}, "只读核对 FPS HUD")
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
