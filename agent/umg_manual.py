# agent/umg_manual.py
"""平台跳跃 HUD 的 UMG 人工步骤。平台整句写出 Plan/ 文档，不改 Widget 图。
控件图编译不算完成。"""
import json
import re
from datetime import datetime
from pathlib import Path


def hud_manual_text() -> str:
    from agent.supervise import unique_automation_filter
    from config import script_object_path

    parent = script_object_path("PlatformHUDWidget")
    platform_filter = unique_automation_filter("platform") or "源码里没有唯一的平台测试"
    return f"""# 平台跳跃 HUD · 现状与人工步骤

HUD 的**逻辑已在 C++**：`UPlatformHUDWidget`（父类路径 `{parent}`）
按名字绑 `txt_CoinCount`、`txt_Timer`、`txt_Clear`、`txt_Fail` 四个文本，读
`UPlatformProgressComponent` 的计数、剩余秒数、收齐与超时标记；`ATestPawn::EnsureHud`
在 `BeginPlay` / `PossessedBy` / `PawnClientRestart` 各试一次，把 `WBP_HUD` 挂进视口。
**不需要再在控件图里连委托或写 Event Tick。**

`WBP_HUD` 的父类必须指到 `UPlatformHUDWidget`（2026-10-21 已用 `set_parent` 改好并编译），
否则四个文本绑不上，HUD 会一直显示默认文案。

## 前置

1. 关卡里放一个 `APlatformProgressActor`（C++ 类，Place Actor 面板搜 PlatformProgress）。
   金币 `ACoin`、门 `APlatformDoor`、移动平台 `AMovingPlatform` 同面板可放；
   金币一进关卡就会自注册到总数，门会在收齐时自己开。

## 人做（只在资产被删或重建时才需要）

1. 控件树保持四个 `TextBlock`，名字一字不差：`txt_CoinCount`、`txt_Timer`、`txt_Clear`、`txt_Fail`，
   且勾上 `Is Variable`。
2. `txt_Clear` / `txt_Fail` 放居中覆盖层，初始 `Visibility = Hidden`（C++ 开场也会收起一次）。
3. 父类设成 `PlatformHUDWidget`，编译保存。
4. 文案不用人写：C++ 会写「金币 N/M」「1:30」「通关」「时间到」。

## 只读核对（闭环自动做）

- `check_platform_hud`：四个控件名都在 **且** 父类是 `PlatformHUDWidget`，两条都过才算 HUD 合格。
- 裁判口径：C++ 半边 `cpp_build`，玩法 `RunTestsByFilter("{platform_filter}")` 失败数 0。

## 不做

- 闭环不改 `WBP_HUD` 的控件树与 EventGraph；改父类只走 `set_parent` + `compile_blueprint`。
- 不因为做 HUD 去开 PIE、保存关卡，除非这句话单独要求。
"""


def hud_plan_name(day: str) -> str:
    return f"{day}-平台跳跃HUD-UMG人工步骤.md"


def write_hud_plan(directory: Path, name: str | None = None) -> Path:
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    fname = name or hud_plan_name(datetime.now().strftime("%Y%m%d"))
    path = folder / fname
    path.write_text(hud_manual_text(), encoding="utf-8")
    return path


# 点名了 HUD，但还要改表现。只新建资产接不住，不能当成这句已经做完。
_WIDGET_GRAPH_RE = re.compile(
    r"得分|分数|加分|计数|倒计时|左上角|"
    r"绿色|红色|颜色|"
    r"碰到|碰撞|"
    r"开门|"
    r"加一分|"
    r"连线|节点|委托|控件树|控件图|Anchor|Event\s*Tick|"
    r"(增加|添加|加上).{0,16}(UI|界面|文本|按钮|Text)",
    re.I,
)
WIDGET_GRAPH_MANUAL = "WBP_HUD 控件图由人做（得分、颜色、开门提示）。闭环只建资产，不改控件图。"


def names_hud_widget(query: str) -> bool:
    """点名 WBP_HUD，或 Widget Blueprint 放到 /Game/UI。"""
    text = query or ""
    if "WBP_HUD" in text:
        return True
    lowered = text.lower()
    return "widget blueprint" in lowered and "/game/ui" in lowered


def wants_widget_graph(query: str) -> bool:
    """要改控件图或 HUD 表现。只新建 WBP_HUD 不算。"""
    return _WIDGET_GRAPH_RE.search(query or "") is not None


def wants_hud_asset(query: str) -> bool:
    """点名新建 WBP_HUD，或 Widget Blueprint 放到 /Game/UI。
    句里还在改源码、编译工程、放进关卡、改控件图时不接管。"""
    text = query or ""
    if re.search(r"编译工程|编译项目|编译一下工程|编译代码", text):
        return False
    if re.search(r"放进|放到|放入", text):
        return False
    if wants_widget_graph(text):
        return False
    return names_hud_widget(text)


PLATFORM_HUD_NAMES = ("txt_CoinCount", "txt_Timer", "txt_Clear", "txt_Fail")


def platform_hud_names(data) -> set[str]:
    """从 GetWidgets 的返回里收集 widgetName。字符串包一层 JSON 时拆开再走。"""
    found: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            name = node.get("widgetName")
            if isinstance(name, str) and name.strip():
                found.add(name.strip())
            for value in node.values():
                walk(value)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, str):
            text = node.strip()
            if not text or text[0] not in "[{":
                return
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return
            walk(parsed)

    walk(data)
    return found


def missing_platform_hud(data) -> list[str]:
    found = platform_hud_names(data)
    return [name for name in PLATFORM_HUD_NAMES if name not in found]


_PLATFORM_RE = re.compile(r"平台跳跃|平台關卡|关卡.*金币|金币.*(倒计时|收齐|通关|计数)", re.I)
_COIN_RE = re.compile(r"金币|coin", re.I)
_HUD_TOKENS = ("HUD", "计数", "倒计时", "通关", "左上", "UMG")


def wants_platform_hud(query: str) -> bool:
    """平台 HUD 人工步骤文档的唯一归属就在这个模块。业务口径留在这里，裁判（harness）不看它。"""
    text = query or ""
    if "平台跳跃" in text:
        return True
    if not (_COIN_RE.search(text) and _PLATFORM_RE.search(text)):
        return False
    return any(token in text for token in _HUD_TOKENS)


def maybe_write_hud_plan(query: str) -> str:
    """UE_ALLOW_UMG 不是 true 时写出人工步骤。开关打开时闭环自己做控件图，不再写这份文档。
    幂等：当天同名覆盖。"""
    from config import allow_umg

    if allow_umg():
        return ""
    text = query or ""
    if not wants_platform_hud(text) and not wants_hud_asset(text):
        return ""
    from config import UE_PROJECT_DIR

    # 文档落在 UE 工程的 Plan/，人从这里开工
    path = write_hud_plan(UE_PROJECT_DIR / "Plan")
    return str(path)