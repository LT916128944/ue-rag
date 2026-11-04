"""受控求解循环的预算闸门与诚实失败（20261028 P0-1/P0-4）。

全部离线：模型 / discover / exec / 检索 / 编译 / 裁判全用假件注入。
断言四件事：
1. 预算烧完 → 诚实失败（不假成功），轨迹落进 UE_DISTILL_OUT 临时目录；
2. 方案编造盘点清单之外的原语 → 整条丢，方案失败；
3. 没有收口判据（automation 过滤器）→ 诚实失败，不编证据；
4. 正常路径：盘点 → 方案 → 执行 → automation 失败数 0 收口 → ok，轨迹 outcome=success。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _disable_solver_env(monkeypatch_os: bool = True) -> None:
    os.environ.pop("UE_SOLVER_ENABLED", None)


def check_budget_counts_are_enforced() -> None:
    from agent.solver import Budget

    budget = Budget({"editor_calls": 2, "build_rounds": 1, "model_calls": 1})
    assert budget.spend("editor_calls") is True
    assert budget.spend("editor_calls") is True
    assert budget.spend("editor_calls") is False, "第 3 次必须被闸住"
    assert budget.exhausted("editor_calls") is True
    assert budget.spend("build_rounds") is True
    assert budget.spend("build_rounds") is False
    assert budget.snapshot()["editor_calls"] == {"used": 2, "limit": 2}
    # meta_calls 与 editor_calls 分开记。旧构造没写 meta 时补缺省，花元数据不占执行额度。
    assert budget.limits.get("meta_calls", 0) >= 1
    assert budget.spend("meta_calls") is True
    assert budget.used["editor_calls"] == 2
    assert budget.snapshot()["meta_calls"]["used"] == 1


def check_enabled_defaults_off() -> None:
    """UE_SOLVER_ENABLED 缺省 false：开关没写就是关。"""
    from agent.solver import solver_enabled

    saved = os.environ.pop("UE_SOLVER_ENABLED", None)
    try:
        assert solver_enabled() is False
        os.environ["UE_SOLVER_ENABLED"] = "true"
        assert solver_enabled() is True
        os.environ["UE_SOLVER_ENABLED"] = "1"
        assert solver_enabled() is True
        os.environ["UE_SOLVER_ENABLED"] = ""
        assert solver_enabled() is False, "空串不许当开"
    finally:
        if saved is None:
            os.environ.pop("UE_SOLVER_ENABLED", None)
        else:
            os.environ["UE_SOLVER_ENABLED"] = saved


def check_budget_exhaustion_is_honest(tmp: Path) -> None:
    """编辑器预算只有 2 次，方案要 5 个原语：烧尽 → 诚实失败，不假成功。"""
    from agent import solver

    def fake_complete(_messages):
        return json.dumps({"steps": [
            {"tool": "editor.list_actors", "args": {}},
            {"tool": "niagara.create_system", "args": {}},
            {"tool": "niagara.add_module", "args": {}},
            {"tool": "editor.spawn_actor", "args": {}},
            {"tool": "editor.save_packages", "args": {}},
            {"automation": "AIAgent.Fps.Grenade"},
        ]}, ensure_ascii=False)

    def fake_discover(namespace):
        return {"ok": True, "namespace": namespace, "actions": [
            {"name": "list_actors"}, {"name": "spawn_actor"}, {"name": "save_packages"},
        ]}

    def fake_discover_niagara(namespace):
        assert namespace == "niagara"
        return {"ok": True, "namespace": namespace, "actions": [
            {"name": "create_system"}, {"name": "add_module"},
        ]}

    def discover_router(namespace):
        return fake_discover_niagara(namespace) if namespace == "niagara" else fake_discover(namespace)

    exec_calls = {"n": 0}

    def fake_exec(namespace, action, params, task):
        exec_calls["n"] += 1
        return {"ok": True, "data": {"ok": True}}

    task = {"id": "SBW-B-SOLVE", "action": "solve", "query": "给关卡加一个爆炸特效，粒子用 niagara"}
    with _distill_tmp(tmp):
        out = solver.solve(
            task,
            complete=fake_complete,
            discover_fn=discover_router,
            exec_fn=fake_exec,
            retrieve_fn=lambda _q, top_k=1: [],
            judge_fn=lambda spec, task=None: (_ for _ in ()).throw(AssertionError("不该跑裁判")),
            budget_limits={"editor_calls": 2, "build_rounds": 3, "model_calls": 6},
        )
    assert out.get("ok") is False, out
    assert "预算" in str(out.get("error") or ""), out
    assert out.get("outcome") in ("exec_failed", "inventory_failed"), out
    assert exec_calls["n"] <= 2, exec_calls
    assert out.get("budget", {}).get("editor_calls", {}).get("used", 0) <= 2, out
    assert out.get("capability_gap"), out  # 降级回缺口口径
    assert out.get("trajectory"), out


def check_fabricated_primitive_is_dropped(tmp: Path) -> None:
    """模型编造盘点清单之外的 action：整条丢，方案失败，不执行编造的原语。

    20261028 起 editor 永远在盘点内，编造件改成盘点里肯定没有的名字。"""
    from agent import solver

    def fake_complete(_messages):
        return json.dumps({"steps": [
            {"tool": "editor.not_a_real_action", "args": {}},
            {"tool": "niagara.does_not_exist", "args": {}},
            {"automation": "AIAgent.Fps.Grenade"},
        ]}, ensure_ascii=False)

    def fake_discover(namespace):
        return {"ok": True, "namespace": namespace, "actions": [{"name": "list_actors"}]}

    calls = {"n": 0}

    def fake_exec(namespace, action, params, task):
        calls["n"] += 1
        return {"ok": True, "data": {}}

    task = {"id": "W1", "action": "solve", "query": "加爆炸特效"}
    with _distill_tmp(tmp):
        out = solver.solve(
            task, complete=fake_complete, discover_fn=fake_discover,
            exec_fn=fake_exec, retrieve_fn=lambda _q, top_k=1: [],
        )
    assert out.get("ok") is False, out
    assert calls["n"] == 0, calls  # 编造的原语一个都不许执行
    assert "方案" in str(out.get("error") or "") or "步骤" in str(out.get("error") or ""), out


def check_no_criteria_is_honest(tmp: Path) -> None:
    """方案没给 automation 过滤器：执行完了也诚实失败，不编证据。"""
    from agent import solver

    def fake_complete(_messages):
        return json.dumps({"steps": [{"tool": "editor.list_actors", "args": {}}]}, ensure_ascii=False)

    def fake_discover(namespace):
        return {"ok": True, "namespace": namespace, "actions": [{"name": "list_actors"}]}

    def fake_exec(namespace, action, params, task):
        return {"ok": True, "data": {}}

    task = {"id": "W2", "action": "solve", "query": "看看场景"}
    with _distill_tmp(tmp):
        out = solver.solve(
            task, complete=fake_complete, discover_fn=fake_discover,
            exec_fn=fake_exec, retrieve_fn=lambda _q, top_k=1: [],
        )
    assert out.get("ok") is False, out
    assert out.get("outcome") == "no_criteria", out
    assert "判据" in str(out.get("error") or ""), out
    assert not out.get("test"), out  # 没有判据就不许带判词


def check_success_path_closes_with_automation(tmp: Path) -> None:
    """正常路径：盘点 → 方案 → 执行 → automation 失败数 0 收口。轨迹 outcome=success。"""
    from agent import solver

    def fake_complete(_messages):
        return json.dumps({"steps": [
            {"tool": "niagara.create_system", "args": {}},
            {"tool": "niagara.add_module", "args": {}},
            {"automation": "AIAgent.Fps.Grenade"},
        ]}, ensure_ascii=False)

    def discover_router(namespace):
        rows = {"niagara": [{"name": "create_system"}, {"name": "add_module"}]}
        return {"ok": True, "namespace": namespace, "actions": rows.get(namespace, [])}

    execs: list[str] = []

    def fake_exec(namespace, action, params, task):
        execs.append(f"{namespace}.{action}")
        return {"ok": True, "data": {"ok": True}}

    def fake_judge(spec, task=None):
        assert spec.get("filter") == "AIAgent.Fps.Grenade", spec
        return {"ok": True, "kind": "automation", "filter": spec["filter"],
                "summary": "失败数 0（1 个测试）", "tests": ["AIAgent.Fps.Grenade"]}

    task = {"id": "W3", "action": "solve", "query": "给关卡加爆炸特效（niagara 粒子）"}
    with _distill_tmp(tmp):
        out = solver.solve(
            task, complete=fake_complete, discover_fn=discover_router,
            exec_fn=fake_exec, retrieve_fn=lambda _q, top_k=1: ["AFpsBullet"],
            judge_fn=fake_judge,
        )
    assert out.get("ok") is True, out
    assert out.get("judge_kind") == "automation", out
    assert out["test"]["kind"] == "automation" and out["test"]["filter"] == "AIAgent.Fps.Grenade", out
    assert execs == ["niagara.create_system", "niagara.add_module"], execs
    trajectory = Path(out["trajectory"])
    assert trajectory.is_file(), trajectory
    rows = [json.loads(line) for line in trajectory.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows and rows[-1].get("outcome") == "success", rows[-1]


import contextlib


@contextlib.contextmanager
def _distill_tmp(tmp: Path):
    """轨迹与蒸馏产物只落进临时目录，不污染真实 data/。"""
    saved = {
        key: os.environ.get(key)
        for key in ("UE_DISTILL_OUT", "UE_LEARNED_SKILLS", "UE_FAILURE_PATTERNS", "UE_LEXICON_CANDIDATES")
    }
    os.environ["UE_DISTILL_OUT"] = str(tmp / "distill")
    os.environ["UE_LEARNED_SKILLS"] = str(tmp / "learned_skills.json")
    os.environ["UE_FAILURE_PATTERNS"] = str(tmp / "failure_patterns.json")
    os.environ["UE_LEXICON_CANDIDATES"] = str(tmp / "lexicon_candidates.json")
    try:
        yield tmp / "distill"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    # 20261028：eval 必须封闭——真实 .env 一旦写上 UE_SOLVER_ENABLED=true，
    # 「缺省关」的断言就随环境漂移。把 config.ENV_PATH 指到空临时文件。
    import tempfile

    import config

    saved_env_path = config.ENV_PATH
    with tempfile.TemporaryDirectory() as tmp:
        empty_env = Path(tmp) / ".env"
        empty_env.write_text("", encoding="utf-8")
        config.ENV_PATH = empty_env
        try:
            _disable_solver_env()
            check_budget_counts_are_enforced()
            check_enabled_defaults_off()
            with tempfile.TemporaryDirectory() as tmp2:
                check_budget_exhaustion_is_honest(Path(tmp2))
                check_fabricated_primitive_is_dropped(Path(tmp2))
                check_no_criteria_is_honest(Path(tmp2))
                check_success_path_closes_with_automation(Path(tmp2))
        finally:
            config.ENV_PATH = saved_env_path
    print("solver_budget ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())