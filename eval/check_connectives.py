# eval/check_connectives.py
"""连接词层（20261117）：`agent/connectives.py` + `data/skills/connectives.json` 的离线验收。

要治的问题：`plan_windows._clause_rows` 原先只按标点 `[，,；;。]` 切半句，连接词（然后 /
并且 / 顺便…）留在半句里，`_strip_owned_spans` 剥完只剩一个「然 / 且」字残渣 → 半句被判
「没盖住」→ 整句掉给理解器（叫模型）。加了连接词层后这些半句能在零模型下被接住。

本检查全离线（`complete` 替身 + `_complete_live` 也禁），钉：
1. split 只按标点切时与旧行为逐字一致（词表缺省/空表时退化）；
2. split 把「然后/并且」当切点并剥掉，link 类别正确；连接词不当切点时不误伤正文（不超过 / 和平）；
3. strip 抹连接词、不动正文；has_seq / has_parallel / has_disjunction 判得对；
4. connectives.verify() == []；
5. 端到端：「在关卡里放一个点光源，然后/并且编译工程」在零模型下落到 **rules**，
   动作 [place_light, compile_project]（顺序依赖不自动串，理由见该用例 docstring）；
6. 回归守卫：「编译工程」「打开编辑器」等单位句仍单扇、不被连接词层拆坏。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_disjunction_not_guessed() -> str:
    """选择类连接词（或者/还是/要么）不拆、不剥：二选一不许被当成「两个都做」，回模型/问人。"""
    from agent import connectives
    from agent.plan_windows import _clauses_accounted

    rows = connectives.split("在关卡里放一个点光源，或者编译工程")
    check(any("或者" in row["clause"] for row in rows),
          f"「或者」不该被切掉（否则会两个都做），得到 {rows}")
    check("或者" in connectives.strip("在关卡里放一个点光源或者编译工程"),
          "strip 不该剥掉「或者」")
    check(connectives.has_disjunction("A或者B"), "「或者」应被识别为 disjunction")
    # 二选一整句不能被判「已覆盖」（判 covered 就会同时执行两边）；留在 uncovered → 交模型/问人。
    covered = _clauses_accounted("在关卡里放一个点光源，或者编译工程",
                                 [{"action": "place_light"}, {"action": "compile_project"}])
    check(not covered, "「或者」二选一不该被判 covered（会两个都做），应回模型/问人")
    return "或者/还是/要么 不拆不剥；二选一整句不判 covered（回模型/问人，不替人决定）"


def check_split_basics() -> str:
    from agent import connectives

    one = connectives.split("编译工程")
    check(one == [{"clause": "编译工程", "link": ""}], f"纯单位句 split 应原样，得到 {one}")

    two = connectives.split("打开关卡 L_NewTest3，然后编译工程")
    check([row["clause"] for row in two] == ["打开关卡 L_NewTest3", "编译工程"],
          f"「，然后」应切成两句并剥掉 然后，得到 {two}")
    check([row["link"] for row in two] == ["", "seq"], f"第二句 link 应是 seq，得到 {two}")

    para = connectives.split("在关卡里放个灯并且编译工程")
    check([row["clause"] for row in para] == ["在关卡里放个灯", "编译工程"], f"「并且」应切句，得到 {para}")
    check(para[1]["link"] == "parallel", f"「并且」后应是 parallel，得到 {para}")

    # 连接词在句首也要被剥掉，且前半句为空就不产出空句
    lead = connectives.split("顺便放个光源")
    check(lead == [{"clause": "放个光源", "link": "parallel"}], f"句首 顺便 应剥掉，得到 {lead}")
    tail = connectives.split("加个灯，然后")
    check([row["clause"] for row in tail] == ["加个灯"], f"连接词收尾不应产出空半句，得到 {tail}")
    check(connectives.split("然后") == [], f"纯连接词应什么都不产出，得到 {connectives.split('然后')}")
    return "split：标点+连接词切句、剥词、link 类别正确，空半句不产出"


def check_no_false_positive() -> str:
    from agent import connectives

    # 「不超过」「和平」不在词表里，绝不能被当成连接词切开
    for text in ("不超过三个", "和平精英", "再来一次"):
        rows = connectives.split(text)
        check(len(rows) == 1 and rows[0]["clause"] == text, f"「{text}」不该被切开，得到 {rows}")
        check(connectives.strip(text) == text, f"「{text}」strip 不该动正文，得到 {connectives.strip(text)!r}")
    return "不误伤正文：不超过 / 和平 / 再来 都不被当成连接词"


def check_predicates() -> str:
    from agent import connectives

    check(connectives.has_seq("A，然后B"), "「然后」应是 seq")
    check(not connectives.has_seq("编译工程"), "「编译工程」不该被判 seq")
    check(connectives.has_parallel("A并且B"), "「并且」应是 parallel")
    check(connectives.has_disjunction("A或者B"), "「或者」应是 disjunction")
    check(connectives.strip("然后编译工程") == "编译工程", "strip 应剥掉句首 然后")
    check(connectives.verify() == [], f"connectives.verify() 应为空，得到 {connectives.verify()}")
    return "has_seq / has_parallel / has_disjunction / strip / verify 全部正确"


def check_degrade_without_table() -> str:
    """词表缺失时 split 退化为「只按标点切」，与旧行为一致（不改变拆句）。"""
    from agent import connectives

    saved = os.environ.get("UE_CONNECTIVES")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UE_CONNECTIVES"] = str(Path(tmp) / "nope.json")
        try:
            rows = connectives.split("A，然后B")
            check([row["clause"] for row in rows] == ["A", "然后B"],
                  f"空表时应只按标点切（然后留在半句里），得到 {rows}")
            check(connectives.strip("然后编译工程") == "然后编译工程", "空表时 strip 不该动文本")
        finally:
            if saved is None:
                os.environ.pop("UE_CONNECTIVES", None)
            else:
                os.environ["UE_CONNECTIVES"] = saved
    return "词表缺失时退化为只按标点切（旧行为不变）"


def _offline_plan(text: str) -> dict:
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
    return planned or {}


def _actions(planned: dict) -> list[str]:
    return [str(w.get("action") or "") for w in (planned.get("windows") or []) if isinstance(w, dict)]


def check_sequential_zero_model() -> str:
    """核心收益：带「然后 / 并且」的放灯+编译整句，零模型落到 rules、动作齐。

    注意：本检查**不断言**窗口 depends_on。`_plan_rules` 会把 place_light 插到
    compile_project 之前，句子若是「编译工程，然后放灯」则窗口顺序与句子顺序不一致，
    按窗口顺序自动接依赖会把因果接反；所以连接词层只负责「拆句 + 覆盖」（省模型调用），
    顺序语义用 `connectives.has_seq` 暴露给未来按子句序排序时再用，不在这里自动串依赖。
    """
    for text in ("在关卡里放一个点光源，然后编译工程", "在关卡里放一个点光源，并且编译工程"):
        planned = _offline_plan(text)
        check(str(planned.get("source")) == "rules",
              f"「{text}」应零模型落到 rules，实际 source={planned.get('source')!r}")
        check(_actions(planned) == ["place_light", "compile_project"],
              f"「{text}」动作应为 [place_light, compile_project]，实际 {_actions(planned)}")
    from agent import connectives

    check(connectives.has_seq("在关卡里放一个点光源，然后编译工程"), "「然后」应被识别为 seq")
    check(connectives.has_parallel("在关卡里放一个点光源，并且编译工程"), "「并且」应被识别为 parallel")
    return "「放灯，然后/并且编译工程」→ rules，[place_light, compile_project]；seq/parallel 分类正确"


def check_unit_sentences_intact() -> str:
    """回归守卫：没有连接词的单位句仍是单扇、不被拆坏。"""
    for text, action in (("编译工程", "compile_project"), ("打开编辑器", "launch_editor")):
        planned = _offline_plan(text)
        check(str(planned.get("source")) == "rules", f"「{text}」应走 rules，实际 {planned.get('source')!r}")
        check(_actions(planned) == [action], f"「{text}」应只一扇 {action}，实际 {_actions(planned)}")
    return "编译工程 / 打开编辑器 仍单扇（连接词层没拆坏单位句）"


def main() -> int:
    try:
        rows = [
            check_split_basics(),
            check_no_false_positive(),
            check_predicates(),
            check_disjunction_not_guessed(),
            check_degrade_without_table(),
            check_sequential_zero_model(),
            check_unit_sentences_intact(),
        ]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print("  " + row)
    print("connectives ok（然后/并且/顺便在零模型下被接住，且不误伤正文）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
