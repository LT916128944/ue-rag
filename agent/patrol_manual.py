# agent/patrol_manual.py
"""巡逻敌人的蓝图人工步骤。C++ 半边已经在工程里，这里只写给人绑的步骤。

不改蓝图 Graph。保存与 PIE 跟随任务策略，缺省都开。"""
from pathlib import Path


def patrol_manual_text() -> str:
    return """# 巡逻敌人蓝图人工步骤

C++ 半边是 `UTestEnemyPatrolComponent` 和 `ATestEnemy`。组件绕出生点巡逻，发现玩家后追击，近距离调用 `OnMeleeAttack`。行为树、动画、受击表现由人绑。本文不改图。

## 人绑

1. 打开 `BP_TestEnemy` 的事件图。父类是 `ATestEnemy`，组件是 `UTestEnemyPatrolComponent`。
2. 从组件事件 `OnPatrol` 的白色执行引脚接到巡逻表现。不要在这里写追击或伤害。
3. 从组件事件 `OnChase` 的白色执行引脚接到追击表现。发现距离看组件上的 `DetectRadius`。
4. 从组件事件 `OnMeleeAttack` 的白色执行引脚接到近战表现。这个事件不造成伤害，不播动画，次数在 `MeleeAttackCount`。
5. 行为树、动画蓝图、受击受击盒由人在编辑器里接。闭环不自动绑。

## 只读核对

- `StartPatrol` 之后应进入巡逻。`bEnableChase` 为真且玩家进入 `DetectRadius` 后应追击。
- 距离进入 `AttackRange` 后 `MeleeAttackCount` 增加，间隔不短于 `AttackInterval`。
- 编译看 `cpp_build`。蓝图只编译已有资产，不改 Graph。
- 保存与 PIE 跟随任务策略（缺省都开），除非这句话单独要求关掉。

## 不做

- 不自动连接行为树、动画通知、受击判定。
- 不按类名凭空生成实例。场景里没有敌人时，放置扇单独失败。
"""


def write_patrol_plan(directory: Path, name: str = "20261017-1507-巡逻敌人蓝图人工步骤.md") -> Path:
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(patrol_manual_text(), encoding="utf-8")
    return path
