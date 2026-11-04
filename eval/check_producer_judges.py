# -*- coding: utf-8 -*-
"""U1（20261027 升级计划）：bind_blueprint / create_widget_blueprint 的 producer 侧判词。

钉住四件事：
1. bind_blueprint 成功时窗口自带 editor_assert 判词（find_assets 只读核对唯一蓝图路径）；
2. 资产核对不到 / 编辑器连不上时 bind 失败，不编造成功；
3. create_widget_blueprint 成功时窗口自带 blueprint_compile 判词（保存后真编译一次）；
4. 编译失败时 create 也失败，judge_kind 仍在——「判过了且没过」，不是「没判过」。
全离线：exec_tool 用假实现，后端钉 official，不连编辑器、不调模型。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ASSET_TOOLS = "editor_toolset.toolsets.asset.AssetTools"
BP_TOOLS = "editor_toolset.toolsets.blueprint.BlueprintTools"
REF = "/Game/Props/BP_Gun.BP_Gun"
WREF = "/Game/UI/WBP_Test.WBP_Test"


def _refs(*refs: str) -> dict:
    return {"ok": True, "data": {"returnValue": list(refs)}}


def check_bind_success_carries_editor_assert() -> None:
    from agent import sequence
    import core.editor_exec as editor_exec
    from eval._pin_backend import pinned_backend

    def fake(toolset, tool, args=None, *, task=None, timeout=None):
        assert (toolset, tool) == (ASSET_TOOLS, "find_assets"), (toolset, tool)
        return _refs(REF)

    original = editor_exec.exec_tool
    editor_exec.exec_tool = fake
    try:
        with pinned_backend("official"):
            out = sequence._run_action({"action": "bind_blueprint", "ref_path": REF})
    finally:
        editor_exec.exec_tool = original
    assert out.get("ok") is True, out
    assert out.get("ref") == REF, out
    assert out.get("judge_kind") == "editor_assert", out
    assert out.get("test", {}).get("ok") is True, out
    print("ok: bind_blueprint 成功自带 editor_assert 判词")


def check_bind_missing_asset_fails() -> None:
    from agent import sequence
    import core.editor_exec as editor_exec
    from eval._pin_backend import pinned_backend

    def fake(toolset, tool, args=None, *, task=None, timeout=None):
        return _refs()

    original = editor_exec.exec_tool
    editor_exec.exec_tool = fake
    try:
        with pinned_backend("official"):
            out = sequence._run_action({"action": "bind_blueprint", "ref_path": REF})
    finally:
        editor_exec.exec_tool = original
    assert out.get("ok") is False, out
    assert "找不到" in str(out.get("error") or ""), out
    print("ok: 唯一蓝图路径核对不到时 bind 失败，不编造成功")


def check_bind_editor_down_fails() -> None:
    from agent import sequence
    import core.editor_exec as editor_exec
    from eval._pin_backend import pinned_backend

    def fake(toolset, tool, args=None, *, task=None, timeout=None):
        return {"ok": False, "error": "ConnectError: [WinError 10061]"}

    original = editor_exec.exec_tool
    editor_exec.exec_tool = fake
    try:
        with pinned_backend("official"):
            out = sequence._run_action({"action": "bind_blueprint", "ref_path": REF})
    finally:
        editor_exec.exec_tool = original
    assert out.get("ok") is False, out
    assert "核对失败" in str(out.get("error") or ""), out
    print("ok: 编辑器连不上时 bind 失败并写明原因")


def _widget_env() -> dict:
    previous = os.environ.get("UE_ALLOW_UMG")
    os.environ["UE_ALLOW_UMG"] = "true"
    return {"previous": previous}


def _restore_widget_env(saved: dict) -> None:
    if saved["previous"] is None:
        os.environ.pop("UE_ALLOW_UMG", None)
    else:
        os.environ["UE_ALLOW_UMG"] = saved["previous"]


def check_create_widget_compiles() -> None:
    from agent import sequence
    import core.editor_exec as editor_exec
    from eval._pin_backend import pinned_backend

    calls: list[tuple[str, str]] = []

    def fake(toolset, tool, args=None, *, task=None, timeout=None):
        calls.append((toolset, tool))
        if tool == "find_assets":
            if len([c for c in calls if c[1] == "find_assets"]) == 1:
                return _refs()  # 第一次：还没有这份资产
            return _refs(WREF)  # 创建后复查：在
        if tool == "create":
            return {"ok": True, "data": None}
        if tool == "set_parent":
            return {"ok": True, "data": None}
        if tool == "save_assets":
            return {"ok": True, "data": {"saved": 1}}
        if tool == "compile_blueprint":
            return {"ok": True, "data": None}  # 官方口径：编译成功返回空
        raise AssertionError(f"不该调用 {toolset}.{tool}")

    saved = _widget_env()
    original = editor_exec.exec_tool
    editor_exec.exec_tool = fake
    try:
        with pinned_backend("official"):
            out = sequence._run_action({
                "action": "create_widget_blueprint",
                "folder_path": "/Game/UI",
                "asset_name": "WBP_Test",
                "parent": "/Script/AIAgentProject.PlatformHUDWidget",
            })
    finally:
        editor_exec.exec_tool = original
        _restore_widget_env(saved)
    assert out.get("ok") is True, out
    assert out.get("judge_kind") == "blueprint_compile", out
    assert out.get("test", {}).get("ok") is True, out
    assert any(tool == "compile_blueprint" for _t, tool in calls), calls
    print("ok: create_widget_blueprint 成功自带 blueprint_compile 判词")


def check_create_widget_compile_failure_fails_loudly() -> None:
    from agent import sequence
    import core.editor_exec as editor_exec
    from eval._pin_backend import pinned_backend

    seen: list[str] = []

    def fake(toolset, tool, args=None, *, task=None, timeout=None):
        if tool == "find_assets":
            seen.append(tool)
            if len(seen) == 1:
                return _refs()  # 创建前：还没有这份资产
            return _refs(WREF)  # 创建后复查：在
        if tool in ("create", "set_parent"):
            return {"ok": True, "data": None}
        if tool == "save_assets":
            return {"ok": True, "data": {"saved": 1}}
        if tool == "compile_blueprint":
            return {"ok": False, "error": "error C9999: 编译失败"}
        raise AssertionError(f"不该调用 {toolset}.{tool}")

    saved = _widget_env()
    original = editor_exec.exec_tool
    editor_exec.exec_tool = fake
    try:
        with pinned_backend("official"):
            out = sequence._run_action({
                "action": "create_widget_blueprint",
                "folder_path": "/Game/UI",
                "asset_name": "WBP_Test",
                "parent": "/Script/AIAgentProject.PlatformHUDWidget",
            })
    finally:
        editor_exec.exec_tool = original
        _restore_widget_env(saved)
    assert out.get("ok") is False, out
    assert out.get("judge_kind") == "blueprint_compile", out
    assert "error C9999" in str(out.get("error") or "") or "编译" in str(out.get("error") or ""), out
    print("ok: 编译失败时 create 也失败，判词仍在（判过了且没过）")


def main() -> int:
    check_bind_success_carries_editor_assert()
    check_bind_missing_asset_fails()
    check_bind_editor_down_fails()
    check_create_widget_compiles()
    check_create_widget_compile_failure_fails_loudly()
    print("producer judges ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


