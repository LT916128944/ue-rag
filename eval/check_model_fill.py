# eval/check_model_fill.py
"""缺信息暂停「先问模型、模型补不出再问人」（20261117）：离线验收。

目标：把「写不出唯一路径 / 蓝图 / 源文件 / 过滤器 → 停下等人 r 补」里**能问模型解决**的那部分
先交给模型，减少人工；模型补不出（或离线无模型）时，人工裁决这条路一个字不改。

本检查全离线（把 `plan_windows._complete_live` 换成替身，不联网、不调真模型、不碰编辑器），钉：
1. `model_fill.fill_missing` 的形状校验：合法/非法 path、source（存在性）、filter；抛异常→""；
2. `run_windows._decide_window_model_first`：
   a. 模型补得出 → 直接返回补值，**不调用 `_decide_window`（不问人）**；
   b. 模型补不出 → 退回 `_decide_window`（问人，行为不变）；
   c. 纯失败扇（无「只给一次：r」标记）不走模型 → 直接问人；
3. `run_multi._decide_model_first`：模型补得出→返回补值；补不出→退回 `_decide`。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _patch_complete(module_owner, fn):
    """替换 `agent.plan_windows._complete_live`，返回还原函数。"""
    from agent import plan_windows

    saved = plan_windows._complete_live
    plan_windows._complete_live = fn
    return lambda: setattr(plan_windows, "_complete_live", saved)


def check_fill_shapes() -> str:
    from agent import model_fill

    ok = model_fill.fill_missing("放个蓝图", "path", complete=lambda _m: "```\n/Game/Blueprints/BP_TestEnemy\n```")
    check(ok == "/Game/Blueprints/BP_TestEnemy", f"路径应剥掉代码块/引号取一条，得到 {ok!r}")

    bad = model_fill.fill_missing("放个蓝图", "path", complete=lambda _m: "Blueprints 目录里")
    check(bad == "", f"不是 /Game 路径必须返回空（不猜），得到 {bad!r}")

    # source：形状 + 存在性。把存在性替身成 True 验形状，False 验存在性拦截。
    saved = model_fill._source_exists
    try:
        model_fill._source_exists = lambda _rel: True
        src = model_fill.fill_missing("改射速", "source", complete=lambda _m: "Private/FpsPawn.cpp")
        check(src == "Private/FpsPawn.cpp", f"合法源文件应通过，得到 {src!r}")
        model_fill._source_exists = lambda _rel: False
        missing = model_fill.fill_missing("改射速", "source", complete=lambda _m: "Private/NoSuch.cpp")
        check(missing == "", f"源文件不存在必须返回空，得到 {missing!r}")
        bad_src = model_fill.fill_missing("改射速", "source", complete=lambda _m: "whatever.cpp")
        check(bad_src == "", f"不合 Public/Private 形状必须返回空，得到 {bad_src!r}")
    finally:
        model_fill._source_exists = saved

    flt = model_fill.fill_missing("跑测试", "filter", complete=lambda _m: "AIAgent.Fps.Hit")
    check(flt == "AIAgent.Fps.Hit", f"合法过滤器应通过，得到 {flt!r}")

    def _boom(_m):
        raise RuntimeError("no model")

    check(model_fill.fill_missing("x", "path", complete=_boom) == "", "模型抛异常必须返回空（交回人工）")
    return "fill_missing 形状校验：path/source(存在性)/filter + 异常→空"


WINDOW = {
    "id": "W1", "action": "place_actor", "slots": {},
    "error": "索引里没有这张蓝图，也没有 /Game 路径。只给一次：r /Game/某目录，或 r /Game/某目录/BP_名字。",
}
ENTRY = {"id": "W1", "action": "place_actor", "error": WINDOW["error"], "window": WINDOW}


def check_decide_window_model_first() -> str:
    from agent import run_windows as rw

    calls = {"human": 0}
    saved_human = rw._decide_window

    def _human(_w, _e):
        calls["human"] += 1
        return "HUMAN_DECISION"

    rw._decide_window = _human
    try:
        # a. 模型补得出 → 直接用补值，不问人
        saved = _patch_complete(rw, lambda _m: "/Game/Blueprints/BP_TestEnemy")
        try:
            got = rw._decide_window_model_first(WINDOW, ENTRY, "把 BP_TestEnemy 放进场景")
        finally:
            saved()
        check(got == "/Game/Blueprints/BP_TestEnemy", f"应直接用模型补的路径，得到 {got!r}")
        check(calls["human"] == 0, "模型补得出时不许再问人")

        # b. 模型补不出（抛异常）→ 退回问人
        saved = _patch_complete(rw, lambda _m: (_ for _ in ()).throw(RuntimeError("no model")))
        try:
            got = rw._decide_window_model_first(WINDOW, ENTRY, "把 BP_TestEnemy 放进场景")
        finally:
            saved()
        check(got == "HUMAN_DECISION", f"模型补不出应退回问人，得到 {got!r}")
        check(calls["human"] == 1, "模型补不出时必须问人一次")

        # c. 纯失败扇（无「只给一次：r」）不走模型，直接问人
        fail_entry = {"id": "W9", "action": "place_actor", "error": "放置失败：场景统计没 +1", "window": {}}
        saved = _patch_complete(rw, lambda _m: "/Game/ShouldNotBeUsed")
        try:
            got = rw._decide_window_model_first({"id": "W9", "action": "place_actor"}, fail_entry, "放个东西")
        finally:
            saved()
        check(got == "HUMAN_DECISION", f"纯失败扇应直接问人（不走模型），得到 {got!r}")
    finally:
        rw._decide_window = saved_human
    return "decide_window：模型补得出→不问人；补不出/纯失败→问人（行为不变）"


def check_decide_multi_model_first() -> str:
    from agent import run_multi as rm

    calls = {"human": 0}
    saved = rm._decide
    rm._decide = lambda _s, _m: calls.__setitem__("human", calls["human"] + 1) or "HUMAN_DECISION"
    try:
        step = {"action": "place_actor", "needs": "path", "error": "只给一次：r /Game/某目录"}
        saved_pc = _patch_complete(rm, lambda _m: "/Game/Blueprints/BP_Coin")
        try:
            got = rm._decide_model_first(step, None, "放个 BP_Coin")
        finally:
            saved_pc()
        check(got == "/Game/Blueprints/BP_Coin", f"多窗口应直接用模型补值，得到 {got!r}")
        check(calls["human"] == 0, "模型补得出时不该问人")

        saved_pc = _patch_complete(rm, lambda _m: (_ for _ in ()).throw(RuntimeError("no model")))
        try:
            got = rm._decide_model_first(step, None, "放个 BP_Coin")
        finally:
            saved_pc()
        check(got == "HUMAN_DECISION", f"模型补不出应退回问人，得到 {got!r}")
        check(calls["human"] == 1, "模型补不出时必须问人一次")
    finally:
        rm._decide = saved
    return "run_multi：模型补得出→不问人；补不出→问人"


def main() -> int:
    try:
        rows = [check_fill_shapes(), check_decide_window_model_first(), check_decide_multi_model_first()]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print("  " + row)
    print("model fill ok（缺信息先问模型，补不出才问人）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
