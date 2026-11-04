"""蒸馏闭环（20261028 P2 第 7 条）的离线断言：

- 成功轨迹 → learned_skills 影子条目（代码永不提升）+ lexicon 提名行（人审队列，
  同一轨迹 sha 只一行、重复命中累计 hits）；
- 失败轨迹 → 失败模式库影子条目（successes=0，fix 不会用它）；
- 影子技能不在 promoted_as_routes / match_learned 的执行链里。
"""
import json
import os
import sys
import tempfile
import contextlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@contextlib.contextmanager
def _isolated(tmp: Path):
    saved = {
        key: os.environ.get(key)
        for key in ("UE_DISTILL_OUT", "UE_LEARNED_SKILLS", "UE_FAILURE_PATTERNS", "UE_LEXICON_CANDIDATES")
    }
    os.environ["UE_DISTILL_OUT"] = str(tmp / "distill")
    os.environ["UE_LEARNED_SKILLS"] = str(tmp / "learned_skills.json")
    os.environ["UE_FAILURE_PATTERNS"] = str(tmp / "failure_patterns.json")
    os.environ["UE_LEXICON_CANDIDATES"] = str(tmp / "lexicon_candidates.json")
    try:
        yield tmp
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def check_success_distills_to_shadow_skill_and_lexicon(tmp: Path) -> None:
    from agent import solver
    from agent.skill_memory import load_skills, promoted_as_routes

    trace = {
        "task_id": "W7",
        "scope_text": "给 level3 的 fps 游戏加手榴弹（爆炸表现）",
        "outcome": "success",
        "executions": [
            {"tool": "niagara.create_system", "ok": True},
            {"tool": "editor.spawn_actor", "ok": True},
        ],
        "build": {"rounds": 2, "ok": True},
        "verdict": {"ok": True, "kind": "automation", "filter": "AIAgent.Fps.Grenade",
                    "summary": "失败数 0（1 个测试）"},
    }
    result = solver.distill_trajectory(trace)
    assert result.get("bucket") == "success", result
    skills = load_skills()
    assert skills and skills[0].get("status") == "shadow", skills
    assert skills[0]["action_sequence"] == ["niagara.create_system", "editor.spawn_actor", "apply_spec+build"], skills[0]
    assert skills[0]["source_runs"] == ["W7"], skills[0]
    # 影子永不进执行链
    assert promoted_as_routes() == [], promoted_as_routes()

    # lexicon 提名行：人审队列，重复命中累计 hits
    solver.distill_trajectory(trace)
    path = Path(os.environ["UE_LEXICON_CANDIDATES"])
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    assert len(rows) == 1, rows
    assert rows[0]["hits"] == 2, rows
    assert rows[0]["status"] == "review", rows
    assert rows[0]["automation_filter"] == "AIAgent.Fps.Grenade", rows
    assert rows[0]["verbs"] == [], rows  # verbs 由人审补，代码不编动词


def check_failure_distills_to_failure_pattern(tmp: Path) -> None:
    from agent import solver
    from agent.failure_memory import load_patterns, lookup_pattern

    trace = {
        "task_id": "W8",
        "scope_text": "给关卡加爆炸特效",
        "outcome": "build_failed",
        "executions": [{"tool": "niagara.create_system", "ok": True}],
        "build": {"rounds": 3, "ok": False,
                  "fixes": [{"round": 2, "ok": True, "error": "error C2039: 'ThrowGrenade': is not a member"}]},
    }
    result = solver.distill_trajectory(trace)
    assert result.get("bucket") == "failure", result
    patterns = load_patterns()
    assert patterns and patterns[0].get("status") == "shadow", patterns
    assert "C2039" in str(patterns[0].get("symptom") or ""), patterns[0]
    # successes=0：lookup_pattern 永不返回影子条目，fix 不会用它
    assert lookup_pattern("error C2039: 'ThrowGrenade'") is None, "影子不许进 fix"


def check_read_only_plan_lands_in_failure_shadow(tmp: Path) -> None:
    """read_only_plan 收一条固定症状的影子。词表不在这条路径上扩。"""
    from agent import solver
    from agent.failure_memory import load_patterns

    result = solver.distill_trajectory({
        "task_id": "W-ro",
        "scope_text": "把门打开",
        "outcome": "read_only_plan",
        "executions": [{"tool": "editor.load_level", "ok": True}],
    })
    assert result.get("bucket") == "failure", result
    patterns = load_patterns()
    hit = next((item for item in patterns if item.get("symptom") == "solver-read_only_plan"), None)
    assert hit is not None, patterns
    assert hit.get("status") == "shadow", hit
    assert "_CHANGE_IMPLYING_RE" in str(hit.get("note") or ""), hit
    assert "打开" not in solver._CHANGE_IMPLYING_RE.pattern


def check_real_solve_run_distills(tmp: Path) -> None:
    """真跑一次 solve（假件）：成功轨迹的蒸馏产物真的落库。"""
    from agent import solver

    def fake_complete(_messages):
        return json.dumps({"steps": [
            {"tool": "niagara.create_system", "args": {}},
            {"automation": "AIAgent.Fps.Grenade"},
        ]}, ensure_ascii=False)

    out = solver.solve(
        {"id": "W9", "action": "solve", "query": "给关卡加 niagara 爆炸特效"},
        complete=fake_complete,
        discover_fn=lambda ns: {"ok": True, "namespace": ns, "actions": [{"name": "create_system"}]},
        exec_fn=lambda ns, action, params, task: {"ok": True, "data": {}},
        retrieve_fn=lambda _q, top_k=1: [],
        judge_fn=lambda spec, task=None: {"ok": True, "kind": "automation",
                                          "filter": spec["filter"], "summary": "失败数 0",
                                          "tests": ["AIAgent.Fps.Grenade"]},
    )
    assert out.get("ok") is True, out
    assert out.get("distill", {}).get("bucket") == "success", out
    skills = json.loads(Path(os.environ["UE_LEARNED_SKILLS"]).read_text(encoding="utf-8"))["skills"]
    assert skills and skills[0]["status"] == "shadow", skills
    trajectory = Path(out["trajectory"])
    rows = [json.loads(line) for line in trajectory.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows and rows[-1]["outcome"] == "success", rows[-1]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        with _isolated(Path(tmp)):
            check_success_distills_to_shadow_skill_and_lexicon(Path(tmp))
            check_failure_distills_to_failure_pattern(Path(tmp))
            check_read_only_plan_lands_in_failure_shadow(Path(tmp))
            check_real_solve_run_distills(Path(tmp))
    print("solver_distill ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())