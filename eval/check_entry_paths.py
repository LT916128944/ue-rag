# eval/check_entry_paths.py
"""A-1 / A-2（20261116-1530 HTML 入口层计划 §8）：附件不许改拆句。

两组断言，全离线（不连编辑器、不调模型）：

A-1（同源）—— 带附件与不带附件，`plan_windows` 的 `source` 与窗口数必须一致。
    这是 `agent/plan_windows.py::_is_reference_clause` 的守卫。没有它，UI 追加的
    「。参考资产：/Game/...」会让原本强命中的规则链掉进理解器（20261116 实测：
    `编译工程。参考文件：Public/FpsPawn.h` 由 rules 变 planner，多一次模型调用）。
    另钉「只认三种形状」：带别的字（例如 `参考资产：/Game/UI/WBP_HUD 和 BP_Gun`）不算引用。

A-2 —— `server/prompt_builder.build()` 的 A1–A6：
    A1 原句一字不动放最前（不 trim、不改标点）
    A2 每个附件恰好一条 `。` 半句，顺序 = chip 顺序
    A4 全篇 `/Game` 最多一条（多则 ok=False，`multi_game_path`）
    A5 源码绝对路径相对化；非源码根拒发
    A6 外部文件不进 prompt

`prompt_builder` 只 import config，本检查直接 import 它，不起子进程。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A-1 的样本：五句走规则链的话，每句配一个附件句。
# 附件句一律按 prompt_builder 的产物形状写（`。参考X：token`）。
A1_SAMPLES = [
    ("编译工程", "。参考文件：Public/FpsPawn.h"),
    ("编译工程", "。参考文档：documents/harness/boundaries.md"),
    ("打开关卡 L_NewTest3", "。参考资产：/Game/UI/WBP_HUD"),
    ("打开关卡 L_NewTest3", "。参考关卡：/Game/Maps/L_NewTest3"),
    ("打开关卡 L_NewTest3", "。参考路径：/Game/Blueprints"),
]

# 必须**不**算引用的形状：多带一个字就不是纯路径，或者根本不是路径。
NOT_REFERENCE = [
    "参考资产：/Game/UI/WBP_HUD 和 BP_Gun",
    "把 HUD 改成显示载具信息",
    "参考文件：Public",
    "参考文档：documents/",
    "编译工程再打开关卡",
]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _offline_plan(text: str) -> dict:
    """封闭环境里跑真规划链；`complete` 是抛异常的替身（一次模型调用都不许发生）。"""
    from eval._pin_offline import closed_env

    def _stub(_messages):
        raise RuntimeError("离线检查不许叫模型（plan_windows 的 complete 替身）")

    with closed_env(UE_SOLVER_ENABLED="true"):
        from agent import plan_windows as module

        saved = module._complete_live

        def _forbid(_messages):
            raise AssertionError("离线检查里不许走 _complete_live（它会真的调模型）")

        module._complete_live = _forbid
        try:
            planned = module.plan_windows(text, complete=_stub)
        finally:
            module._complete_live = saved
    return {
        "source": str((planned or {}).get("source") or ""),
        "windows": len((planned or {}).get("windows") or []),
    }


def check_reference_clause_shape() -> None:
    from agent.plan_windows import _is_reference_clause

    for candidate in ("/Game/Maps/L_NewTest3", "参考资产：/Game/UI/WBP_HUD",
                      "参考文件：Public/FpsPawn.h", "Private/FpsPawn.cpp",
                      "documents/harness/boundaries.md", " 路径: /Game/Blueprints  "):
        check(_is_reference_clause(candidate), f"「{candidate}」是纯路径半句，应判成引用")
    for candidate in NOT_REFERENCE:
        check(not _is_reference_clause(candidate), f"「{candidate}」不是纯路径半句，不该判成引用")


def check_parity() -> list[str]:
    """A-1：带/不带附件，source 与窗口数一致。返回人读的对照行。"""
    rows = []
    for text, att in A1_SAMPLES:
        bare = _offline_plan(text)
        with_att = _offline_plan(text + att)
        rows.append(
            f"  {text} + {att[:28]:<28} {bare['source']}({bare['windows']}) → "
            f"{with_att['source']}({with_att['windows']})"
        )
        check(
            bare == with_att,
            f"A3 失败：附件改了拆句结果——「{text}」是 {bare['source']}/{bare['windows']} 扇，"
            f"加「{att}」后变成 {with_att['source']}/{with_att['windows']} 扇。"
            "该半句应被 `_is_reference_clause` 认成 constraint",
        )
    return rows


def check_build_rules() -> list[str]:
    """A-2：A1 / A2 / A4 / A5 / A6。返回人读的对照行。"""
    from server import prompt_builder as pb

    rows = []
    text = "在关卡 L_NewTest3 里放一个 BP_TestActor，然后编译工程"
    res = pb.build(text, [
        {"id": "a1", "path": "Content/Maps/L_NewTest3.umap"},
        {"id": "a2", "path": "Source/AIAgentProject/Public/FpsPawn.h"},
    ])
    rows.append("  " + res["prompt"])

    # A1：原句一字不动放最前（含内部双空格与中文句号）。
    tricky = "把  FpsPawn 的射速改成每秒 10 发。 别动血量"
    a1 = pb.build(tricky, [])
    check(a1["prompt"].startswith(tricky), f"A1 失败：原句没被原样放最前，得到 {a1['prompt']!r}")
    check(a1["prompt"] == tricky, f"A1 失败：无附件时 prompt 必须等于原句，得到 {a1['prompt']!r}")

    # A2：每个附件恰好一条 `。` 半句，顺序 = chip 顺序。
    check(
        res["prompt"] == text + "。参考关卡：/Game/Maps/L_NewTest3。参考文件：Public/FpsPawn.h",
        f"A2 失败：附件句形状不对，得到 {res['prompt']!r}",
    )
    att_lines = [line for line in res["lines"] if line["kind"] == "att"]
    check(len(att_lines) == 2, f"A2 失败：两个附件应产出两条半句，得到 {len(att_lines)} 条")
    check(all(line["text"].startswith("。") for line in att_lines), "A2 失败：附件句必须 `。` 开头")
    check(text not in att_lines[0]["text"], "A2 失败：附件句不许吞原句")

    # A4：两条 `/Game` → 拒发，且只保留第一条。
    multi = pb.build("把 HUD 改成显示载具信息", [
        {"id": "a1", "path": "Content/UI/WBP_HUD.uasset"},
        {"id": "a2", "path": "Content/Maps/L_NewTest3.umap"},
    ])
    codes = {w["code"] for w in multi["warnings"]}
    check(not multi["ok"], f"A4 失败：两条 /Game 路径必须拒发，警告 {sorted(codes)}")
    check("multi_game_path" in codes, f"A4 失败：缺 multi_game_path 警告，得到 {sorted(codes)}")
    check(
        multi["prompt"].count("/Game/") == 1,
        f"A4 失败：拒发后 prompt 仍带两条 /Game：{multi['prompt']!r}",
    )

    # A4：原句已占名额，附件再带一条 ⇒ 拒发（不能静默放行）。
    both = pb.build("在 /Game/Maps/L_NewTest3 里放 BP_Coin", [
        {"id": "a1", "path": "Content/UI/WBP_HUD.uasset"},
    ])
    check(not both["ok"], "A4 失败：原句已有一条 /Game、附件再带一条，必须拒发")

    # A4：同一个 token 重复不追加，只提醒。
    dup = pb.build("在 /Game/Maps/L_NewTest3 里放 BP_Coin", [
        {"id": "a1", "path": "Content/Maps/L_NewTest3.umap"},
    ])
    check(
        dup["prompt"].count("/Game/Maps/L_NewTest3") == 1,
        f"A4 失败：同一条路径不该重复追加，得到 {dup['prompt']!r}",
    )
    check("token_already_in_text" in {w["code"] for w in dup["warnings"]},
          "A4 失败：重复 token 应给 token_already_in_text 提醒")
    check(dup["ok"], f"A4 失败：重复不拒发，警告 {sorted({w['code'] for w in dup['warnings']})}")

    # A5：源码绝对路径相对化，且只收 UE_SOURCE_ROOT 下的。
    from config import UE_PROJECT_DIR

    abs_h = str(Path(UE_PROJECT_DIR) / "Source" / "AIAgentProject" / "Public" / "FpsPawn.h")
    src = pb.build("把 FpsPawn 的射速改成每秒 10 发", [{"id": "a1", "path": abs_h}])
    check(
        src["prompt"].endswith("。参考文件：Public/FpsPawn.h"),
        f"A5 失败：绝对源码路径没有相对化，得到 {src['prompt']!r}",
    )
    check("source_relativized" in {w["code"] for w in src["warnings"]}, "A5 失败：缺 source_relativized 信息")
    check("absolute" not in src["prompt"].lower(), "A5 失败：prompt 里不许留绝对路径")
    outside = pb.build("改一下这个文件", [{"id": "a1", "path": "E:/Temp/FpsPawn.h", "role": "source"}])
    check(not outside["ok"], "A5 失败：不在源码根下的路径必须拒发")

    # A6：外部文件不进 prompt。
    ext = pb.build("看看这个文件里写了什么", [
        {"id": "a1", "path": "设计草图.txt", "name": "设计草图.txt", "external": True},
    ])
    check("设计草图" not in ext["prompt"], f"A6 失败：外部文件名进了 prompt：{ext['prompt']!r}")
    check("external_file" in {w["code"] for w in ext["warnings"]}, "A6 失败：缺 external_file 提醒")

    # 屏蔽目录：铁律 2 的同栏目录不给 token。
    blocked = pb.build("编译工程", [{"id": "a1", "path": "Intermediate/Build/x.h", "role": "source"}])
    check(not blocked["ok"], "屏蔽目录附件必须拒发")
    check("att_bad_path" in {w["code"] for w in blocked["warnings"]}, "屏蔽目录应报 att_bad_path")

    # 空正文只有附件：能装配但必须提醒。
    empty = pb.build("", [{"id": "a1", "path": "Content/Maps/L_NewTest3.umap"}])
    check("empty_text" in {w["code"] for w in empty["warnings"]}, "空正文应报 empty_text")

    rows.append(f"  拒发样本：{sorted({w['code'] for w in multi['warnings']})}")
    return rows


def main() -> int:
    try:
        check_reference_clause_shape()
        rows = check_parity()
        build_rows = check_build_rules()
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # 装配器或规划链本身炸了，也要说清是哪一步
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    print("A-1 带/不带附件同源（source 与扇数一致）：")
    for row in rows:
        print(row)
    print("A-2 装配器 A1–A6：")
    for row in build_rows:
        print(row)
    print("entry paths ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
