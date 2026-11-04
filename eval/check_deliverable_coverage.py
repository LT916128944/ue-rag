"""交付项覆盖对账（20261028-155214 假成功的守卫）。

155214 run：载具范围的 5 条交付里 4 条是载具，唯一声明窗 implement_shooter
只接得住最后一条（射击主体就位）；其余 4 条被静默丢弃，集成矩阵交付验收还全绿。
守卫三件事：
1. 求解关着：未接住的交付项进 partial_gap，交付验收判 False（假成功必须拦住）；
2. 人审已确认缺口的范围：按能力缺口口径记（确认过 = 这段不做），不再问第二遍；
3. 求解开着：未接住的交付项自动接一扇只装它们的 solve 扇，跟在本范围声明窗后面，
   不出 partial_gap、不打扰人审。
全部离线：不碰编辑器、不调模型（eval 重定向空 .env，求解开关显式控制）。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _brief_vehicle() -> dict:
    """155214 任务书的真实形状（范围 A 原样照抄，窗口只有 implement_shooter）。"""
    return {
        "query": "在关卡L_NewTest3中做一个简易载具，人可以上载具边开车边射击，也可以选择投掷手榴弹",
        "scopes": [
            {
                "id": "A",
                "title": "简易载具与车载射击（L_NewTest3）",
                "deliverables": [
                    "载具 Actor 与蓝图资产：车体网格、物理与可驾驶基础",
                    "上车与下车交互：玩家靠近载具就能进入驾驶座，也能离开驾驶座",
                    "载具驾驶输入与移动逻辑（前后左右、转向）",
                    "驾驶状态下开火输入仍然生效，并接到射击主体与命中判定",
                    "在当前关卡里确认射击主体（FpsPawn、FpsTarget、地面与灯）已经就位且可以命中",
                ],
                "pending_note": "载具、驾驶与车载射击的代码还没有接入关卡",
                "depends_on": [],
                "external_dep": "",
                "windows": [
                    {"id": "W1", "title": "实现射击玩法 L_NewTest3", "action": "implement_shooter",
                     "slots": {"level": "L_NewTest3"}, "depends_on": [], "max_retries": 1},
                ],
            },
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["对本次修改做一次只读复验"]},
        "external_dependencies": [],
    }


def _set_solver(on: bool, config):
    """两层都钉死：.env（solver 优先读）+ os.environ（.env 缺键时的回落）。
    20261028 实测：只钉 .env 挡不住 shell 环境变量残留，eval 会随环境漂移。"""
    saved_env_path = config.ENV_PATH
    saved_env_var = os.environ.get("UE_SOLVER_ENABLED")
    tmp = tempfile.TemporaryDirectory()
    env = Path(tmp.name) / ".env"
    env.write_text("" if not on else "UE_SOLVER_ENABLED=true\n", encoding="utf-8")
    config.ENV_PATH = env
    os.environ["UE_SOLVER_ENABLED"] = "true" if on else "false"
    return saved_env_path, saved_env_var, tmp


def _restore_solver(saved, config):
    saved_env_path, saved_env_var, tmp = saved
    config.ENV_PATH = saved_env_path
    if saved_env_var is None:
        os.environ.pop("UE_SOLVER_ENABLED", None)
    else:
        os.environ["UE_SOLVER_ENABLED"] = saved_env_var
    tmp.cleanup()


def _patch_rescue_empty():
    from agent import brief

    saved = brief._rescue_scope_windows
    brief._rescue_scope_windows = lambda scope, query, complete=None, titles=None: []
    return saved


def check_solver_off_partial_gap_fails_delivery() -> None:
    import config

    from agent import brief

    saved_rescue = _patch_rescue_empty()
    saved = _set_solver(False, config)
    try:
        b = _brief_vehicle()
        coverage = brief.preview_coverage(b)
        rows = {row["id"]: row for row in coverage["scopes"]}
        uncovered = rows["A"].get("uncovered") or []
        assert uncovered, f"4 条载具交付必须被点名：{rows['A']}"
        assert all("载具" in item or "驾驶" in item or "上车" in item for item in uncovered), uncovered
        shooter_like = [item for item in uncovered if "FpsPawn" in item]
        assert not shooter_like, "射击主体的那条交付 implement_shooter 接得住，不许误伤"

        windows, meta = brief.materialize_scopes(b)
        by_id = {row["id"]: row for row in meta}
        assert "partial_gap" in by_id["A"], by_id["A"]
        assert len(by_id["A"]["partial_gap"]["deliverables"]) >= 3, by_id["A"]

        # 集成矩阵「交付验收」必须判 False，note 点名没接住的交付。
        class _State:
            data = {"scopes": meta}

            def entries(self):
                return []

        item = brief  # 占位，真正断言走 run_windows 的条款函数
        from agent.run_windows import _delivery_acceptance_item

        verdict = _delivery_acceptance_item(_State())
        assert verdict.get("ok") is False, verdict
        assert "没被任何窗口接住" in str(verdict.get("note") or ""), verdict
        assert "A" in str(verdict.get("note") or ""), verdict
    finally:
        from agent import brief

        brief._rescue_scope_windows = saved_rescue
        _restore_solver(saved, config)


def check_acked_partial_is_capability_gap() -> None:
    import config

    from agent import brief

    saved_rescue = _patch_rescue_empty()
    saved = _set_solver(False, config)
    try:
        b = _brief_vehicle()
        b["gap_ack"] = {"acknowledged": True, "scopes": ["A"]}
        windows, meta = brief.materialize_scopes(b)
        by_id = {row["id"]: row for row in meta}
        gap = by_id["A"].get("capability_gap")
        assert isinstance(gap, dict) and gap, by_id["A"]
        assert "人审已确认" in str(gap.get("reason") or ""), gap
        assert "partial_gap" not in by_id["A"], by_id["A"]
    finally:
        from agent import brief

        brief._rescue_scope_windows = saved_rescue
        _restore_solver(saved, config)


def check_solver_on_covers_uncovered_with_solve_window() -> None:
    import config

    from agent import brief

    saved_rescue = _patch_rescue_empty()
    saved = _set_solver(True, config)
    try:
        b = _brief_vehicle()
        windows, meta = brief.materialize_scopes(b)
        by_id = {row["id"]: row for row in meta}
        solve_wids = by_id["A"]["window_ids"][1:]  # 第一扇是声明窗
        assert solve_wids, f"未接住的交付必须有 solve 扇接：{by_id['A']}"
        win = next(item for item in windows if item.get("id") == solve_wids[0])
        assert win.get("action") == "solve", win
        assert "partial_gap" not in by_id["A"], by_id["A"]
        # solve 扇要跟在声明窗后面：依赖重映射到声明窗的新 id。
        deps = win.get("depends_on") or []
        assert deps and deps[0] == by_id["A"]["window_ids"][0], (deps, by_id["A"]["window_ids"])
        # solve 扇的文本只装没接住的交付，不把已接住的再算一遍。
        text = str(win.get("scope_text") or "")
        assert "载具" in text and "FpsPawn" not in text, text[:200]
    finally:
        from agent import brief

        brief._rescue_scope_windows = saved_rescue
        _restore_solver(saved, config)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    check_solver_off_partial_gap_fails_delivery()
    check_acked_partial_is_capability_gap()
    check_solver_on_covers_uncovered_with_solve_window()
    print("deliverable_coverage ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())