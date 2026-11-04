"""求解扇的 patch 路线与写闸预检（20261028-183641 卡壳验收基准）：

那次卡壳：射速×100 需求进 solve 扇，模型只能编 editor.run_python（它不知道
射速定义在哪个文件），而 run_python 没放行必被写闸拒绝 → 一次失败 + 一次无谓
重试 + 整扇失败。修复后三条行为要钉住：

- 写闸感知盘点：run_python 不在放行清单里就不进盘点结果，规划者根本看不见；
  任务显式 allow_python: true 时才出现（.env UE_ALLOW_PYTHON 只给缺省）。
- 写闸预检不重试：执行层遇到被拒调用立即停，错误原文带「写闸拒绝」，预算不烧。
- patch 路线：对已有源文件一处替换 → 物化成整文件内容走 apply_spec 管线；
  文件不存在 / old 不命中 / old 不唯一 / 替换无变化都要诚实失败。

全离线：discover / exec / build 全是替身，UE_SOURCE_ROOT 指到临时目录。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Resp:
    """llm_call.complete 返回形状的最小替身（.choices[0].message.content）。"""

    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]



def _fake_discover(actions_by_ns: dict):
    def discover(namespace: str) -> dict:
        if namespace in actions_by_ns:
            return {"ok": True, "actions": [{"name": name} for name in actions_by_ns[namespace]]}
        return {"ok": False, "error": f"Unknown namespace: {namespace}"}
    return discover


def check_inventory_filters_by_policy() -> None:
    from agent.solver import Budget, inventory

    disc = _fake_discover({"editor": ["get_recent_logs", "run_python", "save_packages"]})
    budget = Budget({"editor_calls": 8, "build_rounds": 3, "model_calls": 6})
    # 任务没写 allow_python（None）+ .env 缺省 false（本检查把 ENV_PATH 指到空文件）：
    # run_python 必须从清单里消失。
    inv = inventory("打开关卡", budget, discover_fn=disc, task={"id": "W1", "save": True})
    assert "run_python" not in inv["actions"]["editor"], inv["actions"]
    assert "get_recent_logs" in inv["actions"]["editor"], inv["actions"]
    # save 在任务旗标 save: true 下放行。
    assert "save_packages" in inv["actions"]["editor"], inv["actions"]
    # 任务显式 allow_python: true：run_python 回到清单。
    budget2 = Budget({"editor_calls": 8, "build_rounds": 3, "model_calls": 6})
    inv2 = inventory("打开关卡", budget2, discover_fn=disc, task={"id": "W1", "allow_python": True})
    assert "run_python" in inv2["actions"]["editor"], inv2["actions"]


def check_discover_failure_continues() -> None:
    from agent.solver import Budget, inventory

    # asset 失败（monolith 没这个 namespace）不该把 editor 的盘点结果一并丢掉。
    disc = _fake_discover({"editor": ["get_recent_logs"]})
    budget = Budget({"editor_calls": 8, "build_rounds": 3, "model_calls": 6})
    os.environ["UE_SOLVER_NAMESPACES"] = "editor,asset"
    try:
        inv = inventory("资产目录", budget, discover_fn=disc, task={"id": "W1"})
        assert inv["actions"].get("editor"), inv
        assert "discover asset 失败" in str(inv.get("error") or ""), inv
    finally:
        os.environ.pop("UE_SOLVER_NAMESPACES", None)


def check_policy_block_no_retry() -> None:
    from agent.solver import Budget, execute

    calls: list[str] = []

    def exec_fn(ns, action, params, task):
        calls.append(f"{ns}.{action}")
        return {"ok": True}

    steps = [
        {"tool": "editor.run_python", "namespace": "editor", "action": "run_python", "args": {}},
        {"tool": "editor.get_recent_logs", "namespace": "editor", "action": "get_recent_logs", "args": {}},
    ]
    budget = Budget({"editor_calls": 8, "build_rounds": 3, "model_calls": 6})
    results, _specs, error, _build = execute(
        steps, {"id": "W1", "save": True}, budget, exec_fn=exec_fn)
    assert calls == [], calls  # 被拒调用根本不该发出去
    assert "写闸拒绝" in error, error
    assert "run_python" in error, error
    assert budget.used["editor_calls"] == 0, budget.used  # 预算零消耗
    # 显式放行后：第一步过闸执行，不触发预检拒绝。
    calls.clear()
    budget2 = Budget({"editor_calls": 8, "build_rounds": 3, "model_calls": 6})
    results2, _s, error2, _b = execute(
        steps, {"id": "W1", "allow_python": True}, budget2, exec_fn=exec_fn)
    assert calls == ["editor.run_python", "editor.get_recent_logs"], calls
    assert error2 == "", error2


def check_patch_materialize() -> None:
    from agent import solver

    with tempfile.TemporaryDirectory() as tmp:
        import config

        saved_root = config.UE_SOURCE_ROOT
        root = Path(tmp)
        (root / "Public").mkdir()
        (root / "Public" / "FpsBullet.h").write_text(
            "class AFpsBullet {\n\tstatic constexpr float Speed = 900.f;\n};\n", encoding="utf-8")
        (root / "Public" / "Twin.h").write_text(
            "static constexpr float Speed = 900.f;\nstatic constexpr float Speed = 900.f;\n", encoding="utf-8")
        config.UE_SOURCE_ROOT = root
        try:
            files, error = solver._materialize_patch(
                {"path": "Public/FpsBullet.h", "old": "Speed = 900.f;", "new": "Speed = 90000.f;"})
            assert error == "", error
            assert len(files) == 1 and files[0]["path"] == "Public/FpsBullet.h", files
            assert "Speed = 90000.f;" in files[0]["content"], files[0]["content"]
            assert "900.f;" not in files[0]["content"], files[0]["content"]

            _f, err_missing = solver._materialize_patch(
                {"path": "Public/Nope.h", "old": "a", "new": "b"})
            assert "文件不存在" in err_missing, err_missing
            _f, err_nohit = solver._materialize_patch(
                {"path": "Public/FpsBullet.h", "old": "Speed = 1.f;", "new": "x"})
            assert "找不到" in err_nohit, err_nohit
            _f, err_multi = solver._materialize_patch(
                {"path": "Public/Twin.h", "old": "Speed = 900.f;", "new": "x"})
            assert "唯一" in err_multi, err_multi
            _f, err_same = solver._materialize_patch(
                {"path": "Public/FpsBullet.h", "old": "Speed = 900.f;", "new": "Speed = 900.f;"})
            assert err_same, err_same
        finally:
            config.UE_SOURCE_ROOT = saved_root


def check_patch_step_runs_build() -> None:
    from agent import solver

    builds: list[int] = []

    def build(files=None):
        builds.append(1)
        return {"ok": True, "kind": "cpp_build", "summary": "编译通过", "build_rounds": 1}

    with tempfile.TemporaryDirectory() as tmp:
        import config

        saved_root = config.UE_SOURCE_ROOT
        root = Path(tmp)
        (root / "Public").mkdir()
        (root / "Public" / "FpsBullet.h").write_text("float Speed = 900.f;\n", encoding="utf-8")
        config.UE_SOURCE_ROOT = root
        try:
            steps = [{"patch": {"path": "Public/FpsBullet.h", "old": "900.f", "new": "90000.f"}}]
            results, spec_files, error, build_info = solver.execute(
                steps, {"id": "W1"}, solver.Budget({"editor_calls": 8, "build_rounds": 3, "model_calls": 6}),
                exec_fn=lambda *a, **k: {"ok": True}, build=build)
            assert error == "", error
            assert builds == [1], builds
            assert results and results[0]["tool"] == "apply_spec+build" and results[0]["ok"], results
            assert spec_files and spec_files[0]["path"] == "Public/FpsBullet.h", spec_files
            assert "90000.f" in spec_files[0]["content"], spec_files[0]
        finally:
            config.UE_SOURCE_ROOT = saved_root


def check_validate_steps_patch() -> None:
    from agent.solver import _validate_steps

    inv = {"actions": {"editor": ["get_recent_logs"]}, "automation_tests": ["AIAgent.Fps.Hit"]}
    steps, automation, error = _validate_steps(
        [{"patch": {"path": "Public/FpsBullet.h", "old": "900.f", "new": "90000.f"}}], inv)
    assert error == "" and steps and "patch" in steps[0], (steps, error)
    assert automation == "", automation
    _s, _a, err_bad_path = _validate_steps(
        [{"patch": {"path": "../evil.h", "old": "a", "new": "b"}}], inv)
    assert "路径不收" in err_bad_path, err_bad_path
    _s, _a, err_same = _validate_steps(
        [{"patch": {"path": "Public/A.h", "old": "a", "new": "a"}}], inv)
    assert "相同" in err_same, err_same
    _s, _a, err_empty = _validate_steps(
        [{"patch": {"path": "Public/A.h", "old": "", "new": "b"}}], inv)
    assert "old" in err_empty, err_empty


def check_locate_code() -> None:
    from agent import solver

    with tempfile.TemporaryDirectory() as tmp:
        import config

        saved_root = config.UE_SOURCE_ROOT
        root = Path(tmp)
        root.mkdir(exist_ok=True)
        (root / "FpsBullet.h").write_text(
            "// 注释行不算命中\n\tstatic constexpr float Speed = 900.f;\n", encoding="utf-8")
        config.UE_SOURCE_ROOT = root
        try:
            hits = solver.locate_code("把子弹射速改到100倍")
            assert hits, hits
            assert hits[0]["file"] == "FpsBullet.h", hits
            assert hits[0]["line"] == 2, hits
            assert "Speed = 900.f" in hits[0]["text"], hits
            assert solver.locate_code("无关需求没有词") == []
        finally:
            config.UE_SOURCE_ROOT = saved_root


def check_read_only_plan_rejected() -> None:
    """20261028-185716 的假成功：方案 0 改动（只有验收步骤），拿改动前就绿的
    AIAgent.Fps.Hit 报「求解收口成功」。改需求的原句 + 只读方案 ⇒ 必须诚实失败。"""
    from agent import solver

    calls: list[str] = []

    def complete(messages, **_kw):
        calls.append("plan")
        return _Resp(json.dumps({
            "steps": [
                {"tool": "editor.run_automation_tests", "args": {"prefix": "AIAgent.Fps"}},
                {"automation": "AIAgent.Fps.Hit"},
            ]
        }))

    def exec_fn(ns, action, params, task):
        if action == "list_automation_tests":
            return {"ok": True, "data": {"tests": [{"full_path": "AIAgent.Fps.Hit"}]}}
        return {"ok": True}

    budget = {"editor_calls": 8, "build_rounds": 3, "model_calls": 6}
    # 轨迹与蒸馏产物隔离到临时目录，不污染 data/distill 与 data/skills。
    # UE_ALLOW_MONOLITH_WRITE=true：让 run_automation_tests 过写闸进盘点清单。
    import tempfile as _tfd

    from eval._pin_offline import closed_env

    with _tfd.TemporaryDirectory() as dt, closed_env(UE_ALLOW_MONOLITH_WRITE="true"):
        os.environ["UE_DISTILL_OUT"] = str(Path(dt) / "distill")
        os.environ["UE_LEARNED_SKILLS"] = str(Path(dt) / "learned_skills.json")
        os.environ["UE_FAILURE_PATTERNS"] = str(Path(dt) / "failure_patterns.json")
        os.environ["UE_LEXICON_CANDIDATES"] = str(Path(dt) / "lexicon_candidates.json")
        try:
            out = solver.solve(
                {"id": "W1", "query": "把子弹射速调整到原先100倍"},
                complete=complete, discover_fn=_fake_discover({"editor": ["run_automation_tests", "list_automation_tests"]}),
                exec_fn=exec_fn, retrieve_fn=lambda q, top_k=1: [],
                automation_list_fn=lambda ns, action, params, task: {"ok": True, "data": {"tests": [{"full_path": "AIAgent.Fps.Hit"}]}},
                budget_limits=budget,
            )
        finally:
            for key in ("UE_DISTILL_OUT", "UE_LEARNED_SKILLS", "UE_FAILURE_PATTERNS", "UE_LEXICON_CANDIDATES"):
                os.environ.pop(key, None)
    assert not out.get("ok"), out
    assert out.get("outcome") == "read_only_plan", out
    assert "什么都没改" in str(out.get("summary") or ""), out


def check_locate_code_header_first() -> None:
    """20261028-185716 的第二个病灶：字母序 Private/ 在 Public/ 前，命中上限被
    .cpp 用法行占满，头文件定义行进不了计划载荷。定义行必须排前、头文件优先。"""
    from agent import solver

    with tempfile.TemporaryDirectory() as tmp:
        import config

        saved_root = config.UE_SOURCE_ROOT
        root = Path(tmp)
        (root / "Private").mkdir()
        (root / "Public").mkdir()
        for i in range(1, 7):
            (root / "Private" / f"Use{i}.cpp").write_text(
                f"void F{i}() {{ float x = Speed; }}\n", encoding="utf-8")
        (root / "Public" / "FpsBullet.h").write_text(
            "\tstatic constexpr float Speed = 900.f;\n", encoding="utf-8")
        config.UE_SOURCE_ROOT = root
        try:
            hits = solver.locate_code("把子弹射速改到100倍", max_hits=8)
            assert hits, hits
            first = hits[0]
            assert first["file"].endswith("FpsBullet.h"), hits  # 定义行排第一
            assert "Speed = 900.f" in first["text"], first
            assert len(hits) >= 2, hits  # 用法行仍在，但排在定义行后
        finally:
            config.UE_SOURCE_ROOT = saved_root


def check_calibrate_args_fixes_param_names() -> None:
    """20261028-190109 的卡壳：load_level 给了 level，monolith 要 path——盘点只有
    action 名，参数名靠猜。describe 校准必须能发现形状对不上，并用一次模型调用修好。"""
    from agent import solver

    inv = {"actions": {"editor": ["load_level"]}, "automation_tests": ["AIAgent.Fps.Hit"]}
    steps = [{"tool": "editor.load_level", "namespace": "editor", "action": "load_level",
              "args": {"level": "/Game/Maps/L_NewTest3"}}]
    schemas = {"editor.load_level": {"path": {"type": "string", "required": True}}}

    def complete(messages, **_kw):
        return _Resp(json.dumps({"steps": [
            {"tool": "editor.load_level", "args": {"path": "/Game/Maps/L_NewTest3"}},
        ]}))

    budget = solver.Budget({"editor_calls": 8, "build_rounds": 3, "model_calls": 6})
    fixed, error = solver._calibrate_args(steps, schemas, "打开关卡 L_NewTest3",
                                          complete, budget, inv=inv)
    assert error == "", error
    assert fixed[0]["args"] == {"path": "/Game/Maps/L_NewTest3"}, fixed
    assert budget.used["model_calls"] == 1, budget.used

    # 形状本来就对：不花模型预算。
    steps_ok = [{"tool": "editor.load_level", "namespace": "editor", "action": "load_level",
                 "args": {"path": "/Game/Maps/L_NewTest3"}}]
    budget2 = solver.Budget({"editor_calls": 8, "build_rounds": 3, "model_calls": 6})
    same, error2 = solver._calibrate_args(steps_ok, schemas, "q", complete, budget2, inv=inv)
    assert error2 == "" and same == steps_ok, (same, error2)
    assert budget2.used["model_calls"] == 0, budget2.used

    # 修不出来（模型给空）：保留原方案 + 带错误说明，不放宽执行。
    def complete_empty(messages, **_kw):
        return _Resp("{}")
    budget3 = solver.Budget({"editor_calls": 8, "build_rounds": 3, "model_calls": 6})
    kept, error3 = solver._calibrate_args(steps, schemas, "q", complete_empty, budget3, inv=inv)
    assert kept == steps and "校准输出没有 steps" in error3, (kept, error3)


def _criteria_complete(pick: str):
    def complete(messages, **_kw):
        system = str((messages or [{}])[0].get("content") or "")
        if "补选收口" in system:
            return _Resp(json.dumps({"filter": pick}))
        return _Resp(json.dumps({"steps": [{"tool": "editor.spawn_actor", "args": {}}]}))

    return complete


def _criteria_solve(complete, judge):
    from agent import solver

    return solver.solve(
        {"id": "W1", "query": "把子弹射速调整到原先的100倍"},
        complete=complete,
        discover_fn=_fake_discover({"editor": ["spawn_actor"]}),
        exec_fn=lambda ns, action, params, task: {"ok": True},
        retrieve_fn=lambda q, top_k=1: [],
        automation_list_fn=lambda ns, action, params, task: {
            "ok": True, "data": {"tests": [{"full_path": "AIAgent.Fps.Hit"}]},
        },
        judge_fn=judge,
        budget_limits={"editor_calls": 8, "build_rounds": 3, "model_calls": 6},
    )


def check_criteria_backfill_uses_real_filter() -> None:
    """方案省略 automation，补选命中真清单 → 收口走这个过滤器。"""
    from eval._pin_offline import closed_env

    seen = {}

    def judge(spec, task=None):
        seen["filter"] = spec.get("filter")
        return {"ok": True, "kind": "automation", "filter": spec.get("filter"),
                "summary": "失败数 0"}

    with tempfile.TemporaryDirectory() as dt, closed_env(UE_ALLOW_MONOLITH_WRITE="true"):
        os.environ["UE_DISTILL_OUT"] = str(Path(dt) / "distill")
        os.environ["UE_LEARNED_SKILLS"] = str(Path(dt) / "learned_skills.json")
        os.environ["UE_FAILURE_PATTERNS"] = str(Path(dt) / "failure_patterns.json")
        os.environ["UE_LEXICON_CANDIDATES"] = str(Path(dt) / "lexicon_candidates.json")
        try:
            out = _criteria_solve(_criteria_complete("AIAgent.Fps.Hit"), judge)
        finally:
            for key in ("UE_DISTILL_OUT", "UE_LEARNED_SKILLS", "UE_FAILURE_PATTERNS", "UE_LEXICON_CANDIDATES"):
                os.environ.pop(key, None)
    assert out.get("ok") is True, out
    assert seen.get("filter") == "AIAgent.Fps.Hit", seen
    assert out.get("test", {}).get("filter") == "AIAgent.Fps.Hit", out
    assert out.get("budget", {}).get("model_calls", {}).get("used") == 2, out.get("budget")


def check_criteria_backfill_drops_fabricated() -> None:
    """补选编造清单里没有的过滤器 → 整条丢，维持 no_criteria，不跑裁判。"""
    from eval._pin_offline import closed_env

    def judge(spec, task=None):
        raise AssertionError("编造的过滤器不许进裁判")

    with tempfile.TemporaryDirectory() as dt, closed_env(UE_ALLOW_MONOLITH_WRITE="true"):
        os.environ["UE_DISTILL_OUT"] = str(Path(dt) / "distill")
        os.environ["UE_LEARNED_SKILLS"] = str(Path(dt) / "learned_skills.json")
        os.environ["UE_FAILURE_PATTERNS"] = str(Path(dt) / "failure_patterns.json")
        os.environ["UE_LEXICON_CANDIDATES"] = str(Path(dt) / "lexicon_candidates.json")
        try:
            out = _criteria_solve(_criteria_complete("AIAgent.Vehicle.Nope"), judge)
        finally:
            for key in ("UE_DISTILL_OUT", "UE_LEARNED_SKILLS", "UE_FAILURE_PATTERNS", "UE_LEXICON_CANDIDATES"):
                os.environ.pop(key, None)
    assert out.get("ok") is False, out
    assert out.get("outcome") == "no_criteria", out


def check_describe_cache_does_not_spend_budget() -> None:
    """缓存命中不烧预算；未命中只花 meta_calls，不占 editor_calls。指纹对不上当没有缓存。"""
    from agent import solver

    steps = [{"tool": "editor.load_level", "namespace": "editor", "action": "load_level", "args": {}}]
    calls = {"n": 0}

    def describe_fn(ns, action):
        calls["n"] += 1
        return {"ok": True, "params": {"path": {"type": "string", "required": True}}}

    cache = {"editor.load_level": {"path": {"type": "string", "required": True}}}
    budget = solver.Budget({"editor_calls": 2, "build_rounds": 1, "model_calls": 1, "meta_calls": 4})
    out = solver.describe_actions(steps, budget, describe_fn=describe_fn, cache=cache)
    assert calls["n"] == 0, calls
    assert budget.used["editor_calls"] == 0 and budget.used["meta_calls"] == 0, budget.used
    assert out["editor.load_level"]["path"]["type"] == "string"

    budget2 = solver.Budget({"editor_calls": 2, "build_rounds": 1, "model_calls": 1, "meta_calls": 4})
    solver.describe_actions(steps, budget2, describe_fn=describe_fn, cache={})
    assert calls["n"] == 1, calls
    assert budget2.used["editor_calls"] == 0, budget2.used
    assert budget2.used["meta_calls"] == 1, budget2.used

    with tempfile.TemporaryDirectory() as tmp:
        surface = Path(tmp) / "surface.json"
        surface.write_text(json.dumps({
            "backend": "official",
            "namespaces": {"editor": [{"name": "load_level", "params": {"path": {"type": "string"}}}]},
        }), encoding="utf-8")
        os.environ["UE_CAPABILITY_SURFACE"] = str(surface)
        os.environ["UE_EDITOR_BACKEND"] = "monolith"
        try:
            assert solver.load_describe_cache() == {}
            os.environ["UE_EDITOR_BACKEND"] = "official"
            assert "editor.load_level" in solver.load_describe_cache()
        finally:
            os.environ.pop("UE_CAPABILITY_SURFACE", None)
            os.environ.pop("UE_EDITOR_BACKEND", None)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    import config

    saved_env_path = config.ENV_PATH
    # 20261028：封闭改用 close_now——本机 .env 现在带 UE_ALLOW_PYTHON=true，
    # dotenv 在 config import 时已把它灌进 os.environ，只换 ENV_PATH 挡不住。
    from eval._pin_offline import close_now

    close_now()
    try:
        check_inventory_filters_by_policy()
        check_discover_failure_continues()
        check_policy_block_no_retry()
        check_patch_materialize()
        check_patch_step_runs_build()
        check_validate_steps_patch()
        check_locate_code()
        check_read_only_plan_rejected()
        check_locate_code_header_first()
        check_calibrate_args_fixes_param_names()
        check_criteria_backfill_uses_real_filter()
        check_criteria_backfill_drops_fabricated()
        check_describe_cache_does_not_spend_budget()
    finally:
        config.ENV_PATH = saved_env_path
    print("solver_patch ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())