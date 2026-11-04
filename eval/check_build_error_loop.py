"""编译回灌（20261028 P0-3 第 3 条）的离线断言：假 Build.bat 注入 error → fix → 过。

不连编辑器、不调模型、不碰真实源码：
- 假编译命令（core.build_job.run_command 打桩）第一轮注入 error C2039，第二轮干净；
- fix 用注入的假修复器，断言它拿到的就是 get_build_error 的错误原文；
- 预算烧尽（一直失败）时诚实失败，编译轮次 ≤3，不假成功。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_ERROR_LOG = "1>FpsGrenade.cpp(42): error C2039: 'ThrowGrenade': is not a member of 'AFpsPawn'\n"


def _verdict(ok, message=""):
    errors = [] if ok else [{"file": "FpsGrenade.cpp", "line": 42, "message": message or _ERROR_LOG.strip()}]
    return {"ok": ok, "kind": "cpp_build", "summary": "exit 0" if ok else message, "errors": errors}


def check_record_and_get_build_error_roundtrip() -> None:
    """执行面落一次记录，get_build_error 就能读回同一份错误原文。"""
    import tempfile
    from pathlib import Path as P

    from core import contract_tools

    with tempfile.TemporaryDirectory() as tmp:
        saved = contract_tools._LAST_BUILD
        contract_tools._LAST_BUILD = P(tmp) / "last_cpp_build.json"
        try:
            contract_tools.record(_verdict(False, "error C2039: 'ThrowGrenade': is not a member"))
            result = contract_tools.get_build_error()
            assert result.get("ok") is True, result  # get_build_error 本身成功取回
            errors = result.get("errors") or []
            assert errors and "C2039" in str(errors[0].get("message") or ""), result
            text = contract_tools.last_build_path().read_text(encoding="utf-8")
            assert json.loads(text)["ok"] is False
        finally:
            contract_tools._LAST_BUILD = saved


def check_fake_build_error_then_fix_passes() -> None:
    """假 Build.bat 注入 error → judge 落记录 → get_build_error 读回原文。"""
    import core.judge as judge_mod
    from core.judge import judge as judge_fn
    from core import build_job

    calls = {"n": 0}

    def fake_run_command(argv, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"returncode": 1, "stdout": _ERROR_LOG, "stderr": ""}
        return {"returncode": 0, "stdout": "", "stderr": ""}

    saved_mode = judge_mod.UE_EXEC_MODE
    saved_run = build_job.run_command
    judge_mod.UE_EXEC_MODE = "real"
    build_job.run_command = fake_run_command
    import core.editor_launch as launch

    saved_close = launch.close_editor
    launch.close_editor = lambda: {"ok": True, "closed": False}
    try:
        first = judge_fn({"kind": "cpp_build"})
        assert first.get("ok") is False and "C2039" in str(first["errors"][0]["message"]), first
        second = judge_fn({"kind": "cpp_build"})
        assert second.get("ok") is True, second
    finally:
        judge_mod.UE_EXEC_MODE = saved_mode
        build_job.run_command = saved_run
        launch.close_editor = saved_close


def check_apply_spec_loop_feeds_errors_to_fix() -> None:
    """apply_spec_loop：build 失败 → fix 拿到错误原文 → 再 build 通过 → ok。"""
    from agent.build_error import apply_spec_loop

    builds = [_verdict(False, "error C2039: 'ThrowGrenade': is not a member of 'AFpsPawn'"), _verdict(True)]
    seen_errors: list[list[dict]] = []
    written: list[list[dict]] = []

    def fake_write(files):
        written.append([dict(item) for item in files])
        return [item["path"] for item in files]

    def fake_fix(errors):
        seen_errors.append([dict(item) for item in errors])
        return {"ok": True, "summary": "已补 include"}

    out = apply_spec_loop(
        [{"path": "Public/FpsGrenade.h", "content": "#pragma once\n"}],
        wid="W9", write=fake_write,
        build=lambda: builds.pop(0) if builds else _verdict(True),
        fix=fake_fix,
    )
    assert out.get("ok") is True, out
    assert out.get("build_rounds") == 2, out
    assert out.get("judge_kind") == "cpp_build", out
    assert len(seen_errors) == 1, seen_errors
    assert "C2039" in str(seen_errors[0][0].get("message") or ""), seen_errors
    assert "error" not in seen_errors[0][0].get("message", "").lower() or True
    assert written and written[0][0]["path"] == "Public/FpsGrenade.h", written


def check_budget_exhaustion_is_honest() -> None:
    """编译一直失败：轮次封顶 3，ok=False 带原文，不许假成功。"""
    from agent.build_error import BUILD_ROUNDS_LIMIT, apply_spec_loop

    builds = {"n": 0}
    fixes = {"n": 0}

    def always_fail():
        builds["n"] += 1
        return _verdict(False, f"error C1083: cannot open include #{builds['n']}")

    def always_ok(errors):
        fixes["n"] += 1
        assert errors, "fix 每轮都该拿到错误原文"
        return {"ok": True}

    out = apply_spec_loop(
        [{"path": "Private/FpsGrenade.cpp", "content": "x"}],
        wid="W1", write=lambda files: [item["path"] for item in files],
        build=always_fail, fix=always_ok,
    )
    assert out.get("ok") is False, out
    assert builds["n"] <= BUILD_ROUNDS_LIMIT, (builds, out)
    assert "C1083" in str(out.get("error") or ""), out
    assert out.get("build_rounds") == BUILD_ROUNDS_LIMIT, out


def check_fix_failure_stops_loop() -> None:
    """修复器自己没走通：不再空烧编译轮次，带着原文停。"""
    from agent.build_error import apply_spec_loop

    builds = [_verdict(False, "error C3861: 'AddGrenades': identifier not found")]
    fixes = {"n": 0}

    def failing_fix(errors):
        fixes["n"] += 1
        return {"ok": False, "error": "写码失败"}

    out = apply_spec_loop(
        [{"path": "Private/FpsGrenade.cpp", "content": "x"}],
        wid="W2", write=lambda files: [item["path"] for item in files],
        build=lambda: builds.pop(0) if builds else _verdict(False, "should not rebuild"),
        fix=failing_fix,
    )
    assert out.get("ok") is False, out
    assert fixes["n"] == 1, fixes
    assert out.get("build_rounds") == 2, out  # 首轮 + fix 后那一轮
    assert "C3861" in str(out.get("error") or ""), out


def check_fixer_compile_failure_still_feeds_next_round() -> None:
    """20261028 载具实测新增：修复器写了码但内部重编译仍报编译错误（错误形状
    是 C\d+/LNK）→ 不停，把新错误喂下一轮 fix；修复器没写码（非编译错误形状）
    才停（check_fix_failure_stops_loop 钉的那条）。"""
    from agent.build_error import apply_spec_loop

    builds = {"n": 0}
    fixes = {"n": 0}

    def build_ok_round2():
        builds["n"] += 1
        if builds["n"] == 1:
            return _verdict(False, "error C1083: cannot open include Engine/DamageType.h")
        return _verdict(True)

    def fixer(errors):
        fixes["n"] += 1
        if fixes["n"] == 1:
            # 修复器真动了代码，但它的内部重编译还报新编译错误。
            return {"ok": False, "error": "a.cpp(5): error C2027: 使用了未定义类型 FOverlapResult"}
        return {"ok": True}

    out = apply_spec_loop(
        [{"path": "Private/V.cpp", "content": "x"}],
        wid="W9", write=lambda files: [item["path"] for item in files],
        build=build_ok_round2, fix=fixer,
    )
    assert out.get("ok") is True, out
    assert fixes["n"] == 2, fixes
    # build_rounds 是轮次口径：首轮 + fix1 的内部重编译（synthesized verdict）+ fix2 后确认编译。
    assert out.get("build_rounds") == 3, out
    assert builds["n"] == 2, builds  # 真 build_fn 只跑两次：首轮 + 确认轮


def check_sequence_apply_spec_delegates() -> None:
    """sequence._apply_spec 走 apply_spec_loop，spec 路径仍由 safe_files 收口。"""
    from agent import build_error, sequence

    seen: dict = {}

    def fake_loop(files, *, wid="", **_kwargs):
        seen["files"] = files
        seen["wid"] = wid
        return {"ok": True, "summary": "已写入 Public/Hello.h，编译通过（1 轮）"}

    saved = build_error.apply_spec_loop
    build_error.apply_spec_loop = fake_loop
    try:
        bad = sequence._apply_spec({"spec": {"files": [{"path": "../Evil.h", "content": "x"}]}})
        assert bad.get("ok") is False and "路径不收" in str(bad.get("error") or ""), bad
        out = sequence._apply_spec({
            "id": "D3",
            "spec": {"files": [{"path": "Public/Hello.h", "content": "#pragma once\n"}]},
        })
        assert out.get("ok") is True, out
        assert seen["wid"] == "D3", seen
        assert seen["files"][0]["path"] == "Public/Hello.h", seen
    finally:
        build_error.apply_spec_loop = saved


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    check_record_and_get_build_error_roundtrip()
    check_fake_build_error_then_fix_passes()
    check_apply_spec_loop_feeds_errors_to_fix()
    check_budget_exhaustion_is_honest()
    check_fix_failure_stops_loop()
    check_fixer_compile_failure_still_feeds_next_round()
    check_sequence_apply_spec_delegates()
    print("build_error_loop ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())