# eval/check_spec_boundary.py
"""全域边界守卫（20261117 边界升级总览 §5）：登记表骨架 + 路由不误报 + 诚实失败。

只钉「跨域共享的那几条不变式」，不重复各域自己的 check_<域>_spec：
  1. 九个域都能惰性导入、都有 SPEC_HINT、judge_kind 都落在 EVIDENCE_KINDS 内。
  2. 登记表**不贪心**：一句明显无关的话不归任何域（recognize → None）。
  3. 任一域在“没有 Spec”时 build 绝不报成功（诚实失败，不假成功）。
  4. 未知域 build → 结构化暂停（不猜）。
  5. all_hints 覆盖九个域（模型据此产 Spec 的入口存在）。

全部离线：不连编辑器、不调模型。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import spec_registry as registry  # noqa: E402

EXPECTED_DOMAINS = (
    "umg", "graph", "animgraph", "material", "niagara", "animation", "sequencer",
    "postprocess", "cpp",
)
UNRELATED = (
    "把一盏点光源放进当前关卡，然后编译整个工程",
    "今天天气不错，泡杯咖啡",
    "打开编辑器并新建一个空关卡 L_Test",
)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def check_registry_complete() -> str:
    from agent.harness import EVIDENCE_KINDS

    desc = registry.describe()
    missing = [d for d in EXPECTED_DOMAINS if d not in desc]
    _check(not missing, f"登记表缺域：{missing}")
    for name in EXPECTED_DOMAINS:
        row = desc[name]
        _check(row.get("ok") is True, f"{name} 域导入失败：{row.get('error')}")
        _check(bool(row.get("kind")), f"{name} 没声明 KIND")
        _check(bool(row.get("has_hint")), f"{name} 没写 SPEC_HINT")
        _check(row.get("judge_kind") in set(EVIDENCE_KINDS),
               f"{name} 的 judge_kind={row.get('judge_kind')!r} 不在 EVIDENCE_KINDS={EVIDENCE_KINDS}")
    hints = registry.all_hints()
    covered = {name for name, _ in hints}
    _check(set(EXPECTED_DOMAINS) <= covered, f"all_hints 没覆盖：{set(EXPECTED_DOMAINS) - covered}")
    return f"九域齐全：都导入、都有 SPEC_HINT、judge_kind 合法（{len(hints)} 条 hint）"


def check_recognize_not_greedy() -> str:
    for text in UNRELATED:
        got = registry.recognize(text)
        _check(got is None, f"无关句被误报成域 {got!r}：{text!r}")
    return "路由不贪心：3 句无关请求都不归任何域"


def check_no_spec_never_succeeds() -> str:
    for name in EXPECTED_DOMAINS:
        result = registry.build(name, {})
        _check(isinstance(result, dict), f"{name} build(空 step) 没返回 dict")
        _check(result.get("ok") is not True, f"{name} 在没有 Spec 时居然报成功（假成功）")
    return "诚实失败：九域在没有 Spec 时 build 全都不报成功"


def check_unknown_domain_pauses() -> str:
    result = registry.build("__not_a_domain__", {"spec": {"x": 1}})
    _check(result.get("paused") is True, f"未知域应结构化暂停：{result}")
    return "未知域 build → 结构化暂停（不猜）"


def main() -> int:
    try:
        rows = [
            check_registry_complete(),
            check_recognize_not_greedy(),
            check_no_spec_never_succeeds(),
            check_unknown_domain_pauses(),
        ]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print("  " + row)
    print("spec boundary ok（登记表骨架 + 路由不误报 + 诚实失败 全离线通过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
