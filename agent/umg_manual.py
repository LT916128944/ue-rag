# agent/umg_manual.py
"""平台跳跃 HUD 的 UMG 人工步骤。UE_ALLOW_UMG=false 时自动产出 Plan/ 文档，
开关翻 true 前不改任何 Widget 图。不改图，不保存关卡，不 PIE。"""
from datetime import datetime
from pathlib import Path


def hud_manual_text() -> str:
    return """# 平台跳跃 HUD · UMG 人工步骤

`UE_ALLOW_UMG=false`：闭环不改任何 Widget 图。本文档把「左上角计数、90 秒倒计时、
收齐开门后的通关显示、超时失败显示」拆给人做，全部三步半，按序即可。
C++ 半边已就位：进度与倒计时都在 `UPlatformProgressComponent`（挂在 `APlatformProgressActor`）。

## 前置

1. 关卡里放一个 `APlatformProgressActor`（C++ 类，Place Actor 面板搜 PlatformProgress）。
   金币 `ACoin`、门 `APlatformDoor`、移动平台 `AMovingPlatform` 同面板可放；
   金币一进关卡就会自注册到总数，门会在收齐时自己开。

## 人做（新建 WBP_HUD）

1. Content Browser 新建目录 `/Game/UI`，右键 → User Interface → Widget Blueprint，
   父类选 `User Widget`，命名 `WBP_HUD`。
2. 打开 Designer：拖一个 Canvas Panel，再拖两个 Text Block：
   - `txt_CoinCount`：Anchors 选左上（Anchor 0,0），Position X=10, Y=10，字号 20。
   - `txt_Timer`：放 `txt_CoinCount` 正下方（X=10, Y=44），字号 16。
3. 通关/失败覆盖层：再拖两个 Text Block `txt_Clear`、`txt_Fail`，居中，
   `Visibility = Hidden`。`txt_Clear` 写「通关」，`txt_Fail` 写「时间到」。
4. 切到 Graph，Event Tick 不接；用委托只动两个 Text：
   - 自定义事件 `BindProgress`：Get All Actors of Class → APlatformProgressActor →
     Get Progress（蓝图纯函数），返回的组件转下去。
   - 接 `OnCoinCollectedDyn`（BlueprintAssignable 委托）：Set Text (txt_CoinCount) =
     `金币 <CollectedCount>/<TotalCoins>`。
   - 接 `OnAllCollectedDyn`：Set Visibility (txt_Clear) = Visible。
   - 接 `OnTimeOutDyn`：Set Visibility (txt_Fail) = Visible。
5. 倒计时读数仍每帧变，单独用 Event Tick：Get Progress → Get RemainingSeconds
   （BlueprintReadOnly）→ Ceil → 格式化 0:00 分秒 → Set Text (txt_Timer)。
   只有这一个 Tick，别往里加别的。
6. 编译保存 `WBP_HUD`。

## 人做（挂进视口）

7. Level Blueprint 的 BeginPlay：Create Widget (WBP_HUD) → Add to Viewport。
   或在 BP_GameMode 的 BeginPlay 里做，二选一，别两头挂。

## 只读核对（编译与 Automation 已由闭环负责）

- 放两枚金币，PIE 走近一枚：txt_CoinCount 变 1/2；剩余时间递减；收齐后门升 300uu，txt_Clear 可见。
- 倒计时归零没收齐：txt_Fail 可见，门不动。
- 裁判口径：C++ 半边 `cpp_build`，玩法 `RunTestsByFilter("AIAgent.Platform")` 失败数 0。
  HUD 视觉本条不在裁判里，本文档即人工条款。

## 不做

- 闭环不自动绑任何 Widget 事件，不改 WBP_HUD 图。
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


def maybe_write_hud_plan(query: str) -> str:
    """平台跳跃又要 HUD 语义、且 UE_ALLOW_UMG=false 时产出文档。幂等：当天同名覆盖。"""
    from config import allow_umg

    from agent.harness import wants_platform

    text = query or ""
    if not wants_platform(text):
        return ""
    if not any(token in text for token in ("HUD", "计数", "倒计时", "通关", "左上")):
        return ""
    if allow_umg():
        return ""
    from config import UE_PROJECT_DIR

    # 文档落在 UE 工程的 Plan/，人从这里开工
    path = write_hud_plan(UE_PROJECT_DIR / "Plan")
    return str(path)