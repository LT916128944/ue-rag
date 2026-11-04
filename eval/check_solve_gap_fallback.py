"""solve 扇的开关行为（20261028 P0-2 / P0-4 验收基准）：

- UE_SOLVER_ENABLED 缺省 false：零窗口范围的物化行为必须与今天完全一致
  （capability_gap，诚实缺口），一个字节都不许漂移；
- 开着：补窗失败后给一扇 solve 扇（action=solve、带 task、realizable），
  不再标 capability_gap；人审已确认缺口的范围不给 solve 扇（尊重表态）。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _brief() -> dict:
    return {
        "query": "打开编辑器，新建一个关卡，名字为L_NewTest2，在这个关卡实现逻辑如下：需要实现简易的FPS射击游戏，内容你自由发挥",
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器并等待 MCP 端口连通",
             "deliverables": ["UE 编辑器进程启动完成"],
             "pending_note": "", "depends_on": [], "external_dep": "",
             "windows": [{"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
                          "slots": {}, "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "新建关卡 L_NewTest2",
             "deliverables": ["在 /Game 下创建关卡资产 L_NewTest2", "把 L_NewTest2 设为当前编辑关卡"],
             "pending_note": "动作表里没有建关卡的动作，需要在编辑器内完成",
             "depends_on": ["A"], "external_dep": "", "windows": []},
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["对修改做一次只读复验"]},
        "external_dependencies": [],
    }


def _patch_rescue_empty():
    from agent import brief

    saved = brief._rescue_scope_windows
    brief._rescue_scope_windows = lambda scope, query, complete=None, titles=None: []
    return saved


def _set_solver(on: bool) -> str | None:
    saved = os.environ.get("UE_SOLVER_ENABLED")
    if on:
        os.environ["UE_SOLVER_ENABLED"] = "true"
    else:
        os.environ.pop("UE_SOLVER_ENABLED", None)
    return saved


def _restore_env(saved) -> None:
    if saved is None:
        os.environ.pop("UE_SOLVER_ENABLED", None)
    else:
        os.environ["UE_SOLVER_ENABLED"] = saved


def check_off_matches_today() -> None:
    """开关关着（缺省）：零窗口范围照旧 capability_gap，没有 solve 扇。"""
    from agent.brief import materialize_scopes

    saved = _patch_rescue_empty()
    env = _set_solver(False)
    try:
        windows, meta = materialize_scopes(_brief())
        actions = [item.get("action") for item in windows]
        assert "solve" not in actions, actions
        by_id = {item["id"]: item for item in meta}
        gap = by_id["B"].get("capability_gap")
        assert isinstance(gap, dict) and gap, by_id["B"]
        assert "能力缺口" in str(gap.get("reason") or ""), gap
        assert gap.get("deliverables"), gap
        assert by_id["B"]["window_ids"] == [], by_id["B"]
    finally:
        from agent import brief

        brief._rescue_scope_windows = saved
        _restore_env(env)


def check_on_gives_solve_window() -> None:
    """开关开着：补窗失败 → 一扇 solve 扇（带 task、realizable），不再标缺口。"""
    from agent.brief import materialize_scopes

    saved = _patch_rescue_empty()
    env = _set_solver(True)
    try:
        windows, meta = materialize_scopes(_brief())
        solves = [item for item in windows if item.get("action") == "solve"]
        assert len(solves) == 1, [item.get("action") for item in windows]
        window = solves[0]
        assert window.get("realizable") is True and window.get("blocked") is False, window
        task = window.get("task")
        assert isinstance(task, dict) and task.get("action") == "solve", window
        assert str(task.get("query") or "").strip(), window  # 范围话术进了任务句
        by_id = {item["id"]: item for item in meta}
        assert by_id["B"]["window_ids"], by_id["B"]
        assert not by_id["B"].get("capability_gap"), by_id["B"]
        assert window["id"] in by_id["B"]["window_ids"], (window["id"], by_id["B"])
        # 集成窗仍按全量窗口收口
        last = windows[-1]
        assert last.get("action") == "integrate_verify", last
        assert window["id"] in (last.get("depends_on") or []), last
    finally:
        from agent import brief

        brief._rescue_scope_windows = saved
        _restore_env(env)


def check_acked_gap_still_acknowledged() -> None:
    """人审已确认缺口：即使开关开着也不给 solve 扇（不推翻人的表态）。"""
    from agent.brief import materialize_scopes

    brief = _brief()
    brief["gap_ack"] = {"acknowledged": True, "scopes": ["B"], "answer": "确认缺口"}
    saved = _patch_rescue_empty()
    env = _set_solver(True)
    try:
        windows, meta = materialize_scopes(brief)
        assert not [item for item in windows if item.get("action") == "solve"], windows
        by_id = {item["id"]: item for item in meta}
        assert by_id["B"].get("capability_gap"), by_id["B"]
    finally:
        from agent import brief

        brief._rescue_scope_windows = saved
        _restore_env(env)


def check_preview_matches_materialize() -> None:
    """预演与物化同口径：开关开着时预演能看到 solve 扇，关着时预演仍是缺口。"""
    from agent.brief import materialize_scopes, preview_coverage

    saved = _patch_rescue_empty()
    env = _set_solver(True)
    try:
        brief = _brief()
        coverage = preview_coverage(brief)
        row_b = next(row for row in coverage["scopes"] if row["id"] == "B")
        assert row_b["count"] == 1 and "solve" in row_b["actions"], row_b
        assert coverage["gaps"] == [], coverage["gaps"]
        windows, meta = materialize_scopes(brief)
        assert any(item.get("action") == "solve" for item in windows), windows
    finally:
        from agent import brief

        brief._rescue_scope_windows = saved
        _restore_env(env)
    env = _set_solver(False)
    try:
        saved2 = _patch_rescue_empty()
        try:
            brief2 = _brief()
            coverage2 = preview_coverage(brief2)
            row_b2 = next(row for row in coverage2["scopes"] if row["id"] == "B")
            assert row_b2["count"] == 0 and row_b2["gap"] is True, row_b2
        finally:
            from agent import brief

            brief._rescue_scope_windows = saved
    finally:
        _restore_env(env)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    # 20261028：eval 必须封闭——真实 .env 写上 UE_SOLVER_ENABLED=true 后，
    # `_set_solver(False)` 只摘环境变量挡不住 .env 里的键。指到空临时 .env。
    import tempfile

    import config

    saved_env_path = config.ENV_PATH
    with tempfile.TemporaryDirectory() as tmp:
        empty_env = Path(tmp) / ".env"
        empty_env.write_text("", encoding="utf-8")
        config.ENV_PATH = empty_env
        try:
            check_off_matches_today()
            check_on_gives_solve_window()
            check_acked_gap_still_acknowledged()
            check_preview_matches_materialize()
        finally:
            config.ENV_PATH = saved_env_path
    print("solve_gap_fallback ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())