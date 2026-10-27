# eval/check_blueprint_stop.py
"""蓝图 Monolith 三次止损。假 exec，不连 9316，也不调用官方 8000。

1. 未见过的路径，第 1 次复查通过 → ok，exec 1 次。
2. 前两次失败，第 3 次通过 → ok，exec 正好 3 次。
3. 三次都失败 → ok false，止损句 + 最后一次错误原文，没有第 4 次 exec，没有官方工具。
4. UE_ALLOW_BLUEPRINT=false → 立刻拒绝，exec 0 次，文案里没有止损句。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STOP = "已尝试 3 次，Monolith 不具备该能力"
PATH = "/Game/Blueprints/BP_Unseen"


def _step() -> dict:
    return {
        "action": "compile_blueprint",
        "title": "编译未见过的蓝图",
        "query": f"编译 {PATH}",
        "slots": {"blueprint": PATH},
    }


def _use(name: str):
    import config
    from backends import select

    select.reset_lock()
    original = config.editor_backend
    config.editor_backend = lambda: name
    return original


def _restore(original) -> None:
    import config
    from backends import select

    config.editor_backend = original
    select.reset_lock()


def _install(exec_fn):
    from backends import monolith
    import core.editor_exec as editor_exec

    saved = {
        "exec": monolith.exec,
        "describe": monolith.describe,
        "discover": monolith.discover,
        "official": editor_exec.exec_tool,
    }
    official_calls = []

    def discover(namespace="", transport=None):
        return {
            "ok": True,
            "namespace": namespace,
            "actions": [{"name": "compile_blueprint", "description": "编译"}],
        }

    def describe(namespace, action, transport=None):
        return {"ok": True, "namespace": namespace, "action": action, "params": {}}

    def official(*_args, **_kwargs):
        official_calls.append(True)
        return {"ok": False, "error": "测试不许调用官方工具"}

    monolith.discover = discover
    monolith.describe = describe
    monolith.exec = exec_fn
    editor_exec.exec_tool = official
    return saved, official_calls


def _uninstall(saved) -> None:
    from backends import monolith
    import core.editor_exec as editor_exec

    monolith.exec = saved["exec"]
    monolith.describe = saved["describe"]
    monolith.discover = saved["discover"]
    editor_exec.exec_tool = saved["official"]


def _run(exec_fn, *, allow="true"):
    from agent.sequence import repair_blueprint

    previous = os.environ.get("UE_ALLOW_BLUEPRINT")
    os.environ["UE_ALLOW_BLUEPRINT"] = allow
    original = _use("monolith")
    saved, official_calls = _install(exec_fn)
    try:
        result = repair_blueprint(_step())
    finally:
        _uninstall(saved)
        _restore(original)
        if previous is None:
            os.environ.pop("UE_ALLOW_BLUEPRINT", None)
        else:
            os.environ["UE_ALLOW_BLUEPRINT"] = previous
    return result, official_calls


def check_first_pass() -> None:
    seen = []

    def exec_fn(*_args, **_kwargs):
        seen.append(1)
        return {"ok": True, "data": {"compiled": True}}

    result, official = _run(exec_fn)
    assert result.get("ok") is True, result
    assert len(seen) == 1, seen
    assert result.get("exec_count") == 1, result
    assert not official, official


def check_third_pass() -> None:
    seen = []

    def exec_fn(*_args, **_kwargs):
        seen.append(len(seen) + 1)
        if len(seen) < 3:
            return {"ok": False, "error": f"第 {len(seen)} 次编译失败"}
        return {"ok": True, "data": {"compiled": True}}

    result, official = _run(exec_fn)
    assert result.get("ok") is True, result
    assert len(seen) == 3, seen
    assert result.get("exec_count") == 3, result
    assert not official, official


def check_three_fail() -> None:
    seen = []
    last = "引脚对不上"

    def exec_fn(*_args, **_kwargs):
        seen.append(len(seen) + 1)
        if len(seen) > 3:
            raise AssertionError("出现了第 4 次 exec")
        if len(seen) == 3:
            return {"ok": False, "error": last}
        return {"ok": False, "error": f"第 {len(seen)} 次编译失败"}

    result, official = _run(exec_fn)
    assert result.get("ok") is False, result
    text = str(result.get("error") or "")
    assert STOP in text, text
    assert last in text, text
    assert len(seen) == 3, seen
    assert result.get("exec_count") == 3, result
    assert not official, official


def check_missing_path_once() -> None:
    """没有唯一路径：1 次、exec 0 次，不套止损句，也不去 discover。"""
    from agent.sequence import repair_blueprint

    seen = []

    def exec_fn(*_args, **_kwargs):
        seen.append(1)
        return {"ok": True, "data": {"compiled": True}}

    previous = os.environ.get("UE_ALLOW_BLUEPRINT")
    os.environ["UE_ALLOW_BLUEPRINT"] = "true"
    original = _use("monolith")
    saved, official_calls = _install(exec_fn)
    try:
        result = repair_blueprint({
            "action": "compile_blueprint",
            "title": "在关卡内实现简易 FPS 射击玩法",
            "query": "需要实现简易的FPS射击游戏，内容你自由发挥",
            "slots": {},
        })
    finally:
        _uninstall(saved)
        _restore(original)
        if previous is None:
            os.environ.pop("UE_ALLOW_BLUEPRINT", None)
        else:
            os.environ["UE_ALLOW_BLUEPRINT"] = previous
    assert result.get("ok") is False, result
    assert result.get("attempts") == 1, result
    assert result.get("exec_count") == 0, result
    assert result.get("error") == "没有唯一的蓝图路径", result
    assert STOP not in str(result.get("error") or ""), result
    assert not seen, seen
    assert not official_calls, official_calls


def check_switch_off() -> None:
    seen = []

    def exec_fn(*_args, **_kwargs):
        seen.append(1)
        return {"ok": True, "data": {}}

    result, official = _run(exec_fn, allow="false")
    assert result.get("ok") is False, result
    assert result.get("exec_count") == 0, result
    assert result.get("attempts") == 0, result
    assert not seen, seen
    assert STOP not in str(result.get("error") or ""), result
    assert "UE_ALLOW_BLUEPRINT" in str(result.get("error") or ""), result
    assert not official, official


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    check_first_pass()
    check_third_pass()
    check_three_fail()
    check_missing_path_once()
    check_switch_off()
    print("blueprint stop ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
