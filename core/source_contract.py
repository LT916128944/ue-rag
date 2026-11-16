"""源码契约：只用源码文本回答「这句话点名的玩法有没有真的写进 C++」。

为什么要有这一条：`cpp_build` 只证明「能编译」。20261104-201829 与 20261105-155615
两次实测里，一句话点名「实现 T 键射击圆柱体火箭筒的玩法、一共三发弹药」却被拆成五扇
HUD 链，编译和玩法一个字都没进窗口，最后仍报「全部窗口完成」——编译 exit 0 不说明
任何玩法存在。判词要么给得出「T 键绑到哪、扣几发、控件写没写」，要么就不许报成功。

契约写成「文件 + 正则 + 一句人话」：命中全部才算成立，缺哪条就逐条列出来。契约是
**显式约定**，不是模糊检索：换了实现方式就得同时改这里，宁可红也不许静默放行。
"""
from __future__ import annotations

import re
from pathlib import Path

# 规则形如 (文件或文件元组, 正则, 人话)。正则在整份文件文本上跑（DOTALL），
# 命中不到就记一条缺失；文件不在就记「读不到」，同样算缺失。
CONTRACTS: dict[str, dict] = {
    "rocket-gameplay": {
        "title": "T 键射击圆柱体火箭筒（三发弹药）",
        "rules": (
            ("Public/FpsPawn.h", r"int32\s+RocketAmmo\s*=\s*3\s*;",
             "弹匣默认三发：RocketAmmo = 3"),
            ("Public/FpsPawn.h", r"\bvoid\s+FireRocket\s*\(",
             "有开火入口 FireRocket()"),
            ("Public/FpsPawn.h", r"\bGetRocketAmmoText\b",
             "有给 HUD 读的弹药文本 GetRocketAmmoText()"),
            ("Private/FpsPawn.cpp", r"EKeys::T\b[\s\S]{0,160}?FireRocket",
             "T 键绑到 FireRocket（BindKey(EKeys::T, IE_Pressed, ...)）"),
            ("Private/FpsPawn.cpp", r"RocketAmmo\s*<=\s*0",
             "没弹药就停火：RocketAmmo <= 0 早退"),
            ("Private/FpsPawn.cpp", r"RocketAmmo\s*-=\s*1",
             "每发真的扣数：RocketAmmo -= 1"),
            ("Private/FpsPawn.cpp", r"BasicShapes/Cylinder",
             "火箭筒是圆柱体网格（/Engine/BasicShapes/Cylinder）"),
            ("Private/FpsPawn.cpp", r"RocketLauncher",
             "火箭筒组件挂在 Pawn 上（RocketLauncher）"),
            ("Private/FpsPawn.cpp", r"GetRocketAmmoText\s*\(\s*\)",
             "弹药文本在 C++ 里拼好，WBP 不用自己算"),
        ),
    },
    "rocket-hud": {
        "title": "UI 上真的显示火箭弹发数",
        "rules": (
            ("Public/FpsHUDWidget.h", r"\btxt_RocketAmmo\b",
             "HUD 头里有 txt_RocketAmmo 绑定字段"),
            ("Private/FpsHUDWidget.cpp", r"\btxt_RocketAmmo\b",
             "HUD 里真的往 txt_RocketAmmo 写文本"),
            ("Private/FpsHUDWidget.cpp", r"GetRocketAmmoText",
             "写进控件的内容来自 Pawn 的弹药读数"),
        ),
    },
}


def names() -> list[str]:
    return list(CONTRACTS)


def _source_root(root=None) -> Path:
    if root is not None:
        return Path(root)
    from config import UE_SOURCE_ROOT

    return Path(UE_SOURCE_ROOT)


def _files_of(rule_paths) -> tuple[str, ...]:
    if isinstance(rule_paths, (str, Path)):
        return (str(rule_paths),)
    return tuple(str(item) for item in rule_paths or ())


def _line_of(text: str, found: re.Match | None) -> int:
    if found is None:
        return 0
    return text.count("\n", 0, found.start()) + 1


def check(name: str, root=None) -> dict:
    """跑一条契约。返回和裁判同形状的判词（kind=source_contract），永不抛。"""
    spec = CONTRACTS.get(str(name or ""))
    if not spec:
        return {
            "ok": False,
            "kind": "source_contract",
            "contract": str(name or ""),
            "summary": f"未知契约 {name or '空'}，不猜。",
            "errors": [{"file": "", "line": 0, "message": f"unknown contract {name or ''}"}],
        }
    base = _source_root(root)
    errors: list[dict] = []
    for rule_paths, pattern, note in spec["rules"]:
        hit = False
        for rel in _files_of(rule_paths):
            path = base / rel
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                errors.append({"file": rel, "line": 0, "message": f"读不到 {rel}，无法核对「{note}」"})
                continue
            found = re.search(pattern, text, re.S)
            if found:
                hit = True
                break
        if hit:
            continue
        if any(item["file"] in _files_of(rule_paths) and item["line"] == 0 for item in errors):
            continue  # 已经记过「读不到」，不再重复记同一条
        rel = _files_of(rule_paths)[0]
        errors.append({
            "file": rel,
            "line": 0,
            "message": f"源码里没有「{note}」（{rel}）",
        })
    if errors:
        summary = f"{spec['title']} 未落进源码：" + "；".join(item["message"] for item in errors[:4])
        return {
            "ok": False,
            "kind": "source_contract",
            "contract": str(name),
            "summary": summary,
            "errors": errors,
        }
    return {
        "ok": True,
        "kind": "source_contract",
        "contract": str(name),
        "summary": f"{spec['title']}：源码契约 {len(spec['rules'])} 条全部命中",
        "errors": [],
    }
