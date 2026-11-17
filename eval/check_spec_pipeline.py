# eval/check_spec_pipeline.py
"""共享内核 agent/spec_pipeline.py 的单元守卫。全离线：纯函数，不连编辑器、不调模型。

钉三件事：
  1. 断言语言（字符串/结构式）求值正确：eq/ne/exists/count/contains/比较、路径取值、缺失路径。
  2. 回灌闭环的**终态可解释**：复核不过且模型救不回 ⇒ 暂停点名；给对修复口 ⇒ 收敛成功；
     轮数有上限，不会无限重试。
  3. 沉淀候选：只有成功且非暂停才产出毕业候选；unsupported 走结构化暂停，不假成功。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import spec_pipeline as sp  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def check_assertions() -> str:
    rb = {"params": {"BaseColor": [1, 0, 0, 1], "Roughness": 0.4},
          "nodes": ["Multiply", "Constant"], "compiled": True, "saved": True, "count": 3}
    ok = sp.eval_assertions([
        {"path": "params.Roughness", "op": "eq", "value": 0.4},
        "params.BaseColor == [1,0,0,1]",
        "nodes contains Multiply",
        "count:nodes == 2",
        "exists:compiled",
        "params.Roughness <= 1",
        "params.Roughness != 0.9",
        "compile_ok",
        "saved",
    ], rb)
    _check(ok == [], f"应全过，实际未过：{ok}")
    bad = sp.eval_assertions([
        {"path": "params.Roughness", "op": "eq", "value": 0.9},
        {"path": "missing.deep", "op": "exists"},
        "count:nodes == 5",
    ], rb)
    _check(len(bad) == 3, f"应 3 条不过，实际 {len(bad)}：{bad}")
    # 路径取值
    found, val = sp.resolve_path(rb, "params.BaseColor.0")
    _check(found and val == 1, f"resolve_path 取 list[0] 失败：{found} {val}")
    found, _ = sp.resolve_path(rb, "params.Nope")
    _check(not found, "resolve_path 不存在路径应返回 False")
    return "断言语言：9 条正例全过、3 条反例逐条拦下，路径取值正确"


def check_repair_terminal() -> str:
    """用一个最小假域跑三种终态：收敛成功 / 救不回暂停 / 轮数有上限。"""
    mod = _FakeDomain()

    # ① 一次就过
    ok = sp.build_domain(mod, {"spec": {"ok": True}}, call=_ok_call, max_rounds=2)
    _check(ok.get("ok") is True and ok.get("rounds") == 1, f"应一次收敛成功：{ok}")
    _check(ok.get("judge_kind") == "editor_assert", "judge_kind 应透传模块声明")

    # ② 复核不过、模型救不回 ⇒ 暂停点名
    paused = sp.build_domain(mod, {"spec": {"ok": False}}, call=_ok_call,
                             fix_model=lambda spec, errs: None, max_rounds=2)
    _check(paused.get("ok") is not True and paused.get("paused") is True, f"应暂停点名：{paused}")
    _check(paused.get("errors"), "暂停必须带结构化错误")

    # ③ 修复口每次给对的 Spec ⇒ 收敛；轮数有上限（不会烧穿）
    calls = {"n": 0}

    def fix(spec, errs):
        calls["n"] += 1
        return {"ok": True}

    fixed = sp.build_domain(mod, {"spec": {"ok": False}}, call=_ok_call, fix_model=fix, max_rounds=3)
    _check(fixed.get("ok") is True, f"修复口给对 Spec 应收敛：{fixed}")
    _check(fixed.get("rounds") == 2, f"应在第 2 轮收敛：{fixed.get('rounds')}")

    # ④ 修复口一直给错的 ⇒ 仍暂停，且 fix 调用次数 ≤ max_rounds
    calls2 = {"n": 0}

    def bad_fix(spec, errs):
        calls2["n"] += 1
        return {"ok": False}

    stuck = sp.build_domain(mod, {"spec": {"ok": False}}, call=_ok_call, fix_model=bad_fix, max_rounds=2)
    _check(stuck.get("paused") is True, f"修不好应暂停：{stuck}")
    _check(calls2["n"] <= 2, f"回灌次数应 ≤ max_rounds，实际 {calls2['n']}")
    return "回灌闭环：收敛/暂停/轮数上限三种终态都可解释"


def check_graduation_and_unsupported() -> str:
    cand = sp.graduation_candidate("demo", {"x": 1}, {"ok": True, "judge_kind": "editor_assert", "exec_count": 3})
    _check(isinstance(cand, dict) and cand.get("domain") == "demo", f"成功应产出毕业候选：{cand}")
    _check(sp.graduation_candidate("demo", {"x": 1}, {"ok": False}) is None, "失败不该产出毕业候选")
    _check(sp.graduation_candidate("demo", {"x": 1}, {"ok": True, "paused": True}) is None,
           "暂停态不该产出毕业候选")
    un = sp.unsupported_result(_FakeDomain(), "材质图连线需人工")
    _check(un.get("paused") is True and un.get("phase") == "unsupported", f"unsupported 应结构化成暂停：{un}")
    return "沉淀：仅成功非暂停产出毕业候选；unsupported 结构化暂停不假成功"


# ------------------------------------------------------------------ 最小假域
class _FakeDomain:
    KIND = "demo"
    JUDGE_KIND = "editor_assert"

    def validate_spec(self, spec):
        return [] if isinstance(spec, dict) and "ok" in spec else ["缺 ok"]

    def plan_ops(self, spec):
        return [{"op": "write", "ok": bool(spec.get("ok"))}]

    def exec_op(self, op, call, write):
        call("t", "t", {}, write)
        return {"ok": True, "tools": ["t"], "readback": {"ok": bool(op.get("ok"))}}

    def verify_spec(self, spec, readback):
        return [] if readback.get("ok") else ["ok 不为真"]

    def write_task(self, step):
        return sp.make_write_task(["t"])

    def build(self, step, **kw):
        return sp.build_domain(self, step, **kw)


def _ok_call(toolset, tool, args=None, task=None):
    return {"ok": True, "data": {}}


def main() -> int:
    try:
        rows = [
            check_assertions(),
            check_repair_terminal(),
            check_graduation_and_unsupported(),
        ]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print("  " + row)
    print("spec pipeline ok（共享内核：断言/回灌/沉淀三块离线通过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
