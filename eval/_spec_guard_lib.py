# eval/_spec_guard_lib.py
"""各域离线守卫（check_<域>_spec.py）共用的断言电池。

只吃「一个域模块 + 一组样例」，不 import 编辑器、不调模型、不联网。它把总览 §5 要求的
四类守卫（boundary/spec/repair/route）里**可由数据驱动**的那几条钉死：

  1. validate：好 Spec 零错误；每个坏 Spec 命中预期错误码（表外类/参数/轨道/成环…）。
  2. kernel 确定性：plan_ops(spec) 两次完全一致，且原语里没有时间/随机等不确定源。
  3. adapter 覆盖：plan_ops 里出现的每种原语，exec_op 都能处理（返回 dict，带 ok）。
  4. verifier：good_readback 全过；bad_readback 逐条报出未通过（只读复核不是空转）。
  5. route 识别：正例句全 True，反例句全 False（宁缺毋滥、不误报兜住）。
  6. repair 上限：复核不过且模型救不回时 **暂停点名**，不假成功，且轮数有上限。

每个域的 check_<域>_spec.py 只提供一张 CASES 表 + 一行 run()。
"""
from __future__ import annotations

import copy
import json


class GuardFail(Exception):
    pass


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise GuardFail(msg)


def recording_fake(toolset: str, tool: str, args=None, task=None) -> dict:
    """默认替身：所有调用都成功、返回空 data。用于纯内核路径（不喂复核断言）。"""
    return {"ok": True, "data": {}}


def _plan_json(spec: dict, module) -> str:
    return json.dumps(module.plan_ops(copy.deepcopy(spec)), ensure_ascii=False, sort_keys=True, default=str)


def run(module, cases: dict) -> int:
    """跑标准电池。返回 0（全绿）/1（有一条红）。"""
    lines: list[str] = []
    try:
        good = cases["good_spec"]
        # 1) validate 好 Spec 零错误
        errors = [e if isinstance(e, dict) else {"code": "validate", "message": str(e)}
                  for e in (module.validate_spec(copy.deepcopy(good)) or [])]
        _check(not errors, f"好 Spec 被判错：{[e.get('message') for e in errors]}")
        lines.append("validate：好 Spec 零错误")

        # 1b) 坏 Spec 命中预期错误码
        for bad, code in cases.get("bad_specs") or []:
            errs = module.validate_spec(copy.deepcopy(bad)) or []
            codes = {e.get("code") if isinstance(e, dict) else "validate" for e in errs}
            _check(code in codes, f"坏 Spec 没命中 {code!r}，实际错误码 {codes}")
        lines.append(f"validate：{len(cases.get('bad_specs') or [])} 个坏 Spec 都被拒")

        # 2) 确定性：两次 plan 完全一致，且无时间/随机源
        p1 = _plan_json(good, module)
        p2 = _plan_json(good, module)
        _check(p1 == p2, "plan_ops 两次不一致（内核不确定）")
        ops = module.plan_ops(copy.deepcopy(good))
        _check(bool(ops), "plan_ops 产出为空")
        blob = p1.lower()
        for marker in ("time(", "random", "uuid", "now()"):
            _check(marker not in blob, f"plan_ops 含不确定源 {marker}")
        lines.append(f"kernel 确定性：{len(ops)} 个原语，两次一致且无时间/随机")

        # 3) adapter 覆盖：每种原语 exec_op 都能处理
        fake = cases.get("fake") or recording_fake
        seen_kinds: set = set()
        for op in ops:
            kind = str(op.get("op") or "")
            if kind in seen_kinds:
                continue
            seen_kinds.add(kind)
            out = module.exec_op(copy.deepcopy(op), fake, module.write_task(cases.get("step") or {}))
            _check(isinstance(out, dict) and "ok" in out, f"exec_op 对原语 {kind!r} 没返回标准结构")
        lines.append(f"adapter 覆盖：{len(seen_kinds)} 类原语都能处理")

        # 4) verifier：good 全过 / bad 逐条报
        fails_good = module.verify_spec(copy.deepcopy(good), copy.deepcopy(cases["good_readback"])) or []
        _check(not fails_good, f"good_readback 没复核通过：{fails_good}")
        bad_rb = cases.get("bad_readback")
        if bad_rb is not None:
            fails_bad = module.verify_spec(copy.deepcopy(good), copy.deepcopy(bad_rb)) or []
            _check(bool(fails_bad), "bad_readback 居然复核通过（verifier 空转）")
        lines.append("verifier：good 全过、bad 被逐条拦下")

        # 5) route 识别
        for text in cases.get("positive_texts") or []:
            _check(module.recognize(text) is True, f"正例句没识别为 {module.KIND}：{text!r}")
        for text in cases.get("negative_texts") or []:
            _check(module.recognize(text) is False, f"反例句被误报成 {module.KIND}：{text!r}")
        lines.append("route：正例全中、反例全不中")

        # 6) repair 上限：复核不过、模型救不回 ⇒ 暂停点名（budget 有上限）
        step = dict(cases.get("step") or {})
        step["spec"] = copy.deepcopy(good)
        # 用「永远喂空 readback」的 fake 让 verify 必不过；fix_model 恒返回 None。
        result = module.build(step, call=(lambda *a, **k: {"ok": True, "data": {}}),
                              fix_model=(lambda spec, errs: None), max_rounds=2)
        _check(result.get("ok") is not True, "复核不过却报成了成功（假成功）")
        _check(result.get("paused") is True, "复核不过且救不回时没有暂停点名")
        _check(int(result.get("rounds") or 0) <= 3, f"回灌轮数超上限：{result.get('rounds')}")
        lines.append("repair：复核不过且模型救不回 ⇒ 暂停点名，轮数有上限")
    except GuardFail as exc:
        print(f"FAIL {getattr(module, 'KIND', '?')}: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {getattr(module, 'KIND', '?')}: {type(exc).__name__}: {exc}")
        return 1
    for line in lines:
        print("  " + line)
    print(f"{getattr(module, 'KIND', '?')} spec ok（Spec/kernel/verifier/repair/route 全离线通过）")
    return 0
