# -*- coding: utf-8 -*-
"""U4（20261027 升级计划）：编辑器写回滚扩面。

钉住四件事：
1. scalar 快照：set_scalar_param 覆写前记旧值，restore 写回旧值（写回走 set_scalar_parameter）；
2. created 快照：place_batch 这扇新建的 Actor 登记进 props.json，restore 按名找到唯一实例后删除；
3. created 重复登记安全：同一实例记两次也只删一次；字段类重复 ref 仍按旧规则跳过不写回；
4. created 删除的保守边界：名字命中不是恰好一个就不删（宁可留待人工）。
全离线：快照目录指到临时目录，exec_tool 用假实现或 writer 间谍，不连编辑器、不调模型。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SCENE = "editor_toolset.toolsets.scene.SceneTools"
_MI = "editor_toolset.toolsets.material_instance.MaterialInstanceTools"


def _with_snapshot_dir():
    import os

    from agent import prop_snapshot

    tmp = tempfile.TemporaryDirectory()
    previous = os.environ.get("UE_SNAPSHOT_DIR")
    os.environ["UE_SNAPSHOT_DIR"] = str(Path(tmp.name) / "snaps")

    class _World:
        def __enter__(self):
            return prop_snapshot

        def __exit__(self, *_args):
            if previous is None:
                os.environ.pop("UE_SNAPSHOT_DIR", None)
            else:
                os.environ["UE_SNAPSHOT_DIR"] = previous
            tmp.cleanup()
            return False

    return _World()


def check_scalar_snapshot_write_back() -> None:
    import core.editor_exec as editor_exec

    calls: list[tuple[str, str, dict]] = []

    def fake(toolset, tool, args=None, *, task=None, timeout=None):
        calls.append((toolset, tool, dict(args or {})))
        return {"ok": True, "data": None}

    with _with_snapshot_dir() as ps:
        ps.capture_props("run-u4", "/Game/M/MI_Coin.MI_Coin",
                         {"parameter": "Emissive", "value": 1.5}, kind="scalar")
        original = editor_exec.exec_tool
        # restore_props 收尾会 _save_level：U3a 后它有 monolith 直连分支（list_dirty_packages），
        # 不钉 backend 会随 .env 真连 9316 绕开假 exec_tool。钉 official 走假实现。
        from eval._pin_backend import pinned_backend

        editor_exec.exec_tool = fake
        try:
            with pinned_backend("official"):
                restored = ps.restore_props("run-u4")
        finally:
            editor_exec.exec_tool = original
    assert restored == ["/Game/M/MI_Coin.MI_Coin"], restored
    writes = [c for c in calls if c[1] == "set_scalar_parameter"]
    assert len(writes) == 1, calls
    assert writes[0][2].get("name") == "Emissive" and writes[0][2].get("value") == 1.5, writes
    print("ok: scalar 快照还原按旧值调 set_scalar_parameter")


def check_scalar_capture_skipped_when_old_unreadable() -> None:
    """旧值读不出（参数从未设置）就不登记——没有可信的「之前」，不编造。"""
    from agent import sequence
    import core.editor_exec as editor_exec

    calls: list[tuple[str, str]] = []

    def fake(toolset, tool, args=None, *, task=None, timeout=None):
        calls.append((toolset, tool))
        if tool == "get_scalar_parameter":
            return {"ok": False, "error": "参数未设置"}
        if tool == "set_scalar_parameter":
            return {"ok": False, "error": "覆写失败"}  # 让这一扇走失败路径
        raise AssertionError(f"不该调用 {tool}")

    with _with_snapshot_dir():
        step = {"action": "set_scalar_param", "run_id": "run-u4b",
                "slots": {"target": "MI_Coin", "param": "Emissive", "value": "5"}}
        original = editor_exec.exec_tool
        editor_exec.exec_tool = fake
        try:
            from eval._pin_backend import pinned_backend

            with pinned_backend("official"):
                out = sequence._run_action(step)
        finally:
            editor_exec.exec_tool = original
        rows = json.loads((None or _props_file("run-u4b")).read_text(encoding="utf-8")) if _props_file("run-u4b").is_file() else {"items": []}
    assert out.get("ok") is False, out
    assert rows.get("items") == [], rows
    print("ok: 旧值读不出时不登记 scalar 快照")


def _props_file(run_id: str) -> Path:
    import os

    from agent import prop_snapshot

    return Path(os.environ["UE_SNAPSHOT_DIR"]) / run_id / "props.json"


def check_created_rows_removed_once() -> None:
    with _with_snapshot_dir() as ps:
        ps.capture_props("run-u4c", "Coin_1", {}, kind="created")
        ps.capture_props("run-u4c", "Coin_1", {}, kind="created")  # 重复登记安全
        ps.capture_props("run-u4c", "Actor_B", {"bPickable": True})  # 字段类
        ps.capture_props("run-u4c", "Actor_B", {"bPickable": False})  # 字段类重复 → 跳过
        seen: list[tuple[str, dict]] = []

        def writer(ref: str, item: dict) -> None:
            seen.append((ref, dict(item)))

        restored = ps.restore_props("run-u4c", writer=writer)
    created_calls = [ref for ref, item in seen if item.get("kind") == "created"]
    field_calls = [ref for ref, item in seen if item.get("kind") != "created"]
    assert created_calls.count("Coin_1") == 1, seen
    assert field_calls == [], field_calls  # 字段类重复 ref 不写回
    assert "Coin_1" in restored, restored
    print("ok: created 重复登记只删一次；字段类重复 ref 仍跳过")


def check_created_removal_is_conservative() -> None:
    import core.editor_exec as editor_exec
    from agent.prop_snapshot import _write_back

    calls: list[tuple[str, str]] = []

    def fake(toolset, tool, args=None, *, task=None, timeout=None):
        calls.append((toolset, tool))
        if tool == "find_actors":
            return {"ok": True, "data": {"returnValue": [
                {"refPath": "/Game/Maps/L_NewTest.Coin_1"},
                {"refPath": "/Game/Maps/L_NewTest.Coin_2"},
            ]}}
        if tool == "remove_from_scene":
            return {"ok": True, "data": None}
        raise AssertionError(f"不该调用 {tool}")

    original = editor_exec.exec_tool
    editor_exec.exec_tool = fake
    try:
        _write_back("Coin_1", {"kind": "created", "fields": {}})
    finally:
        editor_exec.exec_tool = original
    assert [c[1] for c in calls] == ["find_actors"], calls  # 命中两个：一个都不删
    print("ok: created 删除保守——名字命中不是恰好一个就不动")


def check_monolith_save_reads_dirty_packages() -> None:
    """U3 重跑（20261027-191740）发现的形状钉子：list_dirty_packages 的行在
    dirty_packages 键下。读错键 = 「没有未保存的关卡改动」静默跳过保存——
    20261021-221446「已保存没真落盘」的教训换入口重现。"""
    import agent.sequence as sequence
    from eval._pin_backend import pinned_backend

    calls: list[tuple] = []

    def fake_transport(tool_name, arguments):
        calls.append((tool_name, arguments))
        body = arguments or {}
        if body.get("action") == "list_dirty_packages":
            return {"ok": True, "data": {"ok": True, "count": 1, "dirty_packages": [
                {"package": "/Game/Maps/L_NewTest", "is_map": True, "transient": False}]}}
        return {"ok": True, "data": {"saved": [{"package": "/Game/Maps/L_NewTest", "saved": True}]}}

    original = sequence._monolith_transport
    sequence._monolith_transport = fake_transport
    try:
        with pinned_backend("monolith"):
            out = sequence._save_level({"save": True})
    finally:
        sequence._monolith_transport = original
    assert out.get("ok") is True and out.get("saved") is True, out
    saved_calls = [args for _name, args in calls if (args or {}).get("action") == "save_packages"]
    assert saved_calls, calls
    assert saved_calls[0]["params"]["packages"] == ["/Game/Maps/L_NewTest"], saved_calls

    def bad_transport(tool_name, arguments):
        return {"ok": True, "data": {"ok": True, "count": 0}}  # 键整个不在：形状漂移

    sequence._monolith_transport = bad_transport
    try:
        with pinned_backend("monolith"):
            broken = sequence._save_level({"save": True})
    finally:
        sequence._monolith_transport = original
    assert broken.get("ok") is False and "dirty_packages" in str(broken.get("error")), broken
    print("ok: monolith 保存读 dirty_packages 键，形状漂移时响亮失败")


def check_restore_triggers_cover_new_actions() -> None:
    """run_windows 的失败还原触发范围必须覆盖 place_batch / set_scalar_param。"""
    source = (ROOT / "agent" / "run_windows.py").read_text(encoding="utf-8")
    marker = 'action in ("set_pickable", "move_actor", "place_batch", "set_scalar_param")'
    assert marker in source, "还原触发范围没扩到 place_batch / set_scalar_param"
    print("ok: run_windows 失败还原覆盖 place_batch / set_scalar_param")


def main() -> int:
    check_scalar_snapshot_write_back()
    check_scalar_capture_skipped_when_old_unreadable()
    check_created_rows_removed_once()
    check_created_removal_is_conservative()
    check_monolith_save_reads_dirty_packages()
    check_restore_triggers_cover_new_actions()
    print("write rollback ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
