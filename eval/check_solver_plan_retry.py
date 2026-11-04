"""方案层 JSON 稳定性（升级计划 §2 残差 4）的离线断言。

20261028 载具整句实测：模型偶尔不按 JSON 回（纯文本「我来规划…」），
`plan_steps` 当时只能诚实失败成「方案不是 JSON」。这里钉四件事：

1. 首次解析失败后有一次结构化重问：纯文本 → 重问 → 成功；
2. 两次都不成 → 诚实失败，plan_error 可读（说清是哪一类、重问过没有）；
3. 截断保护：finish_reason == "length" 或括号配不平，报「方案 JSON 被截断」，
   不许伪装成「不是 JSON」，也不许无限重问；
4. 彻底失败时写一条 failure_patterns 影子（successes=0，source_runs 带 task id），
   且重问始终受 budget.spend("model_calls") 约束，烧尽就如实说「没发出」。

全离线：complete / discover / exec 全是假件，不调真模型、不连编辑器、不联网。
产物（失败模式库）用 UE_FAILURE_PATTERNS 指到临时目录，与 check_solver_plan_parse
的 _distill_tmp 同口径保存/恢复。
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
    def __init__(self, content: str, finish_reason: str | None = None) -> None:
        self.message = _Msg(content)
        self.finish_reason = finish_reason


class _Response:
    """真机响应形状：choices[0].message.content 与 choices[0].finish_reason。"""

    def __init__(self, content: str, finish_reason: str | None = None) -> None:
        self.choices = [_Choice(content, finish_reason)]


def _live(content: str, finish_reason: str | None = None, error: str | None = None):
    """core.llm_call.complete 的形状：(response, record)。record 里有 error/finish_reason。"""
    record = {"caller": "solve", "error": error, "finish_reason": finish_reason}
    return (_Response(content, finish_reason), record)


def _inv():
    # list_actors 是只读原语：写闸不放行时也仍在盘点清单里（spawn_actor 需要
    # UE_ALLOW_MONOLITH_WRITE，不能拿来当离线默认件）。
    return {"actions": {"editor": ["list_actors", "load_level"]}, "automation_tests": []}


_GOOD_PLAN = json.dumps({"steps": [{"tool": "editor.list_actors", "args": {}}]}, ensure_ascii=False)


# ---------------------------------------------------------------- 重问：成功与失败

def check_plain_text_then_retry_succeeds(tmp: Path) -> None:
    """首次是纯文本 → 重问一次 → 拿到 JSON：steps 有货，恰好花 2 次模型预算。"""
    from agent.solver import Budget, plan_steps

    seen: list[list] = []

    def fake_complete(messages, **_kwargs):
        seen.append([dict(item) for item in messages])
        if len(seen) == 1:
            return _live("好的，我先把场景里的 Actor 列出来，然后放一个进去。")
        return _live(_GOOD_PLAN)

    budget = Budget({"editor_calls": 4, "build_rounds": 1, "model_calls": 6})
    with _distill_tmp(tmp):
        steps, automation, error = plan_steps("放一个 Actor 到关卡", _inv(), "", fake_complete, budget)

    assert error == "", f"重问成功后不该有 plan_error：{error!r}"
    assert [step["tool"] for step in steps] == ["editor.list_actors"], steps
    assert automation == "", automation
    assert budget.used["model_calls"] == 2, f"首轮 + 一次重问 = 2 次：{budget.used}"
    assert len(seen) == 2, f"模型只该被调 2 次：{len(seen)}"

    # 重问消息必须更严格：明确「只输出 JSON」、不许解释、给字段骨架。
    retry_msgs = seen[1]
    assert len(retry_msgs) > len(seen[0]), "重问要在原消息后追加追问，不能原地重发"
    retry_text = " ".join(str(item.get("content") or "") for item in retry_msgs[2:])
    assert "只输出" in retry_text and "JSON" in retry_text, retry_text[:200]
    assert "不要解释" in retry_text, retry_text[:200]
    assert "steps" in retry_text and "tool" in retry_text, f"要给字段骨架：{retry_text[:200]}"
    assert "上一轮的" in retry_text or "上一轮" in retry_text, "重问要指明上一轮哪错了"


def check_twice_plain_text_fails_honestly(tmp: Path) -> None:
    """两次都纯文本 → 诚实失败：可读 plan_error + 失败模式影子，不假成功。"""
    from agent.solver import Budget, plan_steps
    from agent.failure_memory import load_patterns, lookup_pattern

    calls = {"n": 0}

    def fake_complete(_messages, **_kwargs):
        calls["n"] += 1
        return _live("这个需求我建议分三步做，第一步先看看场景里有什么。")

    budget = Budget({"editor_calls": 4, "build_rounds": 1, "model_calls": 6})
    with _distill_tmp(tmp):
        steps, automation, error = plan_steps(
            "放一个 Actor 到关卡", _inv(), "", fake_complete, budget, task_id="W-RETRY")
        patterns = load_patterns()  # 必须在隔离窗口内读，否则会读到真实 data/skills
        # 影子不进 fix：lookup_pattern 永不返回它（同样在隔离窗口内查）。
        from_fix = lookup_pattern("solver-plan-not-json")

    assert steps == [] and automation == "", (steps, automation)
    assert calls["n"] == 2, f"重问恰好一次，不许循环：{calls}"
    assert budget.used["model_calls"] == 2, budget.used
    assert error, "失败必须有可读 plan_error"
    assert "方案不是 JSON" in error, f"分类要可读：{error!r}"
    assert "重问" in error, f"要说明重问过了：{error!r}"

    assert patterns, "解析彻底失败必须留一条失败模式影子"
    hit = next((item for item in patterns if item.get("symptom") == "solver-plan-not-json"), None)
    assert hit is not None, patterns
    assert hit.get("status") == "shadow", f"只写影子，不许 promote：{hit}"
    assert int(hit.get("successes") or 0) == 0, hit
    assert "W-RETRY" in (hit.get("source_runs") or []), f"source_runs 要带 task id：{hit}"
    assert from_fix is None, "影子不许被 fix 用上"


# ---------------------------------------------------------------- 截断保护

def check_truncated_by_finish_reason(tmp: Path) -> None:
    """finish_reason == "length"：报「方案 JSON 被截断」，且重问要求更短。"""
    from agent.solver import Budget, plan_steps
    from agent.failure_memory import load_patterns

    seen: list[list] = []
    # 括号看着是配平的，但 finish_reason=length 说明尾巴被砍了——仍算截断。
    chopped = '{"steps":[{"tool":"editor.list_actors","args":{}}]}'

    def fake_complete(messages, **_kwargs):
        seen.append([dict(item) for item in messages])
        if len(seen) == 1:
            return _live(chopped, finish_reason="length")
        return _live('{"steps":[{"tool":"editor.list_actors","args":{}}', finish_reason="length")

    budget = Budget({"editor_calls": 4, "build_rounds": 1, "model_calls": 6})
    with _distill_tmp(tmp):
        steps, automation, error = plan_steps("放一个 Actor", _inv(), "", fake_complete, budget,
                                              task_id="W-TRUNC")
        patterns = load_patterns()

    assert steps == [], steps
    assert len(seen) == 2, f"截断也只重问一次，不许无限重问：{len(seen)}"
    assert budget.used["model_calls"] == 2, budget.used
    assert "截断" in error, f"必须明说被截断：{error!r}"
    assert "方案不是 JSON" not in error, f"截断不许伪装成「不是 JSON」：{error!r}"

    retry_text = " ".join(str(item.get("content") or "") for item in seen[1][2:])
    assert "精简" in retry_text or "3 条" in retry_text, f"截断重问必须要求更短：{retry_text[:200]}"

    hit = next((item for item in patterns if item.get("symptom") == "solver-plan-truncated"), None)
    assert hit is not None, patterns
    assert int(hit.get("successes") or 0) == 0 and hit.get("status") == "shadow", hit
    assert "W-TRUNC" in (hit.get("source_runs") or []), hit


def check_unbalanced_braces_is_truncation(tmp: Path) -> None:
    """没有 finish_reason、但括号不配对：同样判定为截断，不当成格式错误。"""
    from agent.solver import Budget, plan_steps

    def fake_complete(_messages, **_kwargs):
        return _live('{"steps":[{"tool":"editor.list_actors","args":{"k":"v"')

    budget = Budget({"editor_calls": 4, "build_rounds": 1, "model_calls": 6})
    with _distill_tmp(tmp):
        steps, _automation, error = plan_steps("放一个 Actor", _inv(), "", fake_complete, budget)
    assert steps == [], steps
    assert "截断" in error, f"括号配不平 = 截断：{error!r}"
    assert "方案不是 JSON" not in error, error


def check_truncated_then_shrunk_json_succeeds(tmp: Path) -> None:
    """截断 → 重问给了精简 JSON → 成功。首轮的截断不许污染结果。"""
    from agent.solver import Budget, plan_steps

    def fake_complete(_messages, **_kwargs):
        if not getattr(fake_complete, "n", 0):
            fake_complete.n = 1
            return _live('{"steps":[{"tool":"editor.list_actors","args":{"x":', finish_reason="length")
        return _live(_GOOD_PLAN)

    budget = Budget({"editor_calls": 4, "build_rounds": 1, "model_calls": 6})
    with _distill_tmp(tmp):
        steps, _automation, error = plan_steps("放一个 Actor", _inv(), "", fake_complete, budget)
    assert error == "", error
    assert [step["tool"] for step in steps] == ["editor.list_actors"], steps
    assert budget.used["model_calls"] == 2, budget.used


# ---------------------------------------------------------------- 预算闸门

def check_retry_never_exceeds_model_budget(tmp: Path) -> None:
    """model_calls 只够一次：重问发不出去 → 如实说「没发出」，不谎报重问过。"""
    from agent.solver import Budget, plan_steps

    calls = {"n": 0}

    def fake_complete(_messages, **_kwargs):
        calls["n"] += 1
        return _live("纯文本，没有 JSON。")

    budget = Budget({"editor_calls": 4, "build_rounds": 1, "model_calls": 1})
    with _distill_tmp(tmp):
        steps, _automation, error = plan_steps("放一个 Actor", _inv(), "", fake_complete, budget,
                                               task_id="W-BUDGET")

    assert steps == [], steps
    assert calls["n"] == 1, f"预算只够一次，不许发第二次：{calls}"
    assert budget.used["model_calls"] == 1, budget.used
    assert "预算烧尽" in error, f"烧尽要诚实说明：{error!r}"
    assert "没发出" in error, f"要说明重问没发出去，不许谎报：{error!r}"
    assert "已结构化重问 1 次" not in error, f"没发出去就不许写「重问过」：{error!r}"


def check_zero_budget_spends_nothing() -> None:
    """model_calls=0：一次都不调模型，直接诚实失败。"""
    from agent.solver import Budget, plan_steps

    calls = {"n": 0}

    def fake_complete(_messages, **_kwargs):
        calls["n"] += 1
        return _live(_GOOD_PLAN)

    budget = Budget({"editor_calls": 4, "build_rounds": 1, "model_calls": 0})
    steps, _automation, error = plan_steps("放一个 Actor", _inv(), "", fake_complete, budget)
    assert steps == [] and calls["n"] == 0, calls
    assert "预算烧尽" in error, error


# ---------------------------------------------------------------- 端到端（solve 扇）

def check_solve_survives_first_plain_text(tmp: Path) -> None:
    """整条 solve：首次纯文本 → 重问拿到方案 → 执行到收口成功。"""
    from agent import solver

    calls = {"n": 0}

    def fake_complete(_messages, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _live("我来规划一下：先放一个 Actor，然后跑测试。")
        return _live(json.dumps({"steps": [
            {"tool": "editor.list_actors", "args": {}},
            {"automation": "AIAgent.Fps.Hit"},
        ]}, ensure_ascii=False))

    def fake_discover(namespace):
        return {"ok": True, "namespace": namespace, "actions": [{"name": "list_actors"}]}

    # list_actors 是只读原语：句子里不许出现「改/调整」这类动词，否则会撞
    # read_only_plan 守卫（本检查钉的是方案层重问，不是那条守卫）。
    task = {"id": "W1", "action": "solve", "query": "看看关卡里现在有哪些 Actor"}
    with _distill_tmp(tmp):
        out = solver.solve(
            task, complete=fake_complete, discover_fn=fake_discover,
            exec_fn=lambda ns, action, params, task: {"ok": True, "data": {"ok": True}},
            retrieve_fn=lambda _q, top_k=1: [],
            judge_fn=lambda spec, task=None: {"ok": True, "kind": "automation",
                                              "filter": spec["filter"], "summary": "失败数 0"},
        )
    assert out.get("ok") is True, out
    assert out.get("plan_error") in ("", None), out.get("plan_error")
    assert out.get("budget", {}).get("model_calls", {}).get("used") == 2, out.get("budget")


def check_solve_plan_failure_is_readable(tmp: Path) -> None:
    """重问也失败：solve 诚实返回 plan_failed，错误里带截断/JSON 分类，不假成功。"""
    from agent import solver

    def fake_complete(_messages, **_kwargs):
        return _live('{"steps":[{"tool":"editor.list_actors"', finish_reason="length")

    def fake_discover(namespace):
        return {"ok": True, "namespace": namespace, "actions": [{"name": "list_actors"}]}

    execs = {"n": 0}

    def fake_exec(namespace, action, params, task):
        execs["n"] += 1
        return {"ok": True, "data": {}}

    task = {"id": "W2", "action": "solve", "query": "往关卡里放一个 Actor"}
    with _distill_tmp(tmp):
        out = solver.solve(
            task, complete=fake_complete, discover_fn=fake_discover, exec_fn=fake_exec,
            retrieve_fn=lambda _q, top_k=1: [],
        )
    assert out.get("ok") is False, out
    assert out.get("outcome") == "plan_failed", out
    assert "截断" in str(out.get("error") or ""), out.get("error")
    assert execs["n"] == 0, "方案失败就一步都不许执行"


# ---------------------------------------------------------------- 临时目录隔离

def _distill_tmp(tmp: Path):
    """轨迹与蒸馏产物只落临时目录。保存/恢复写法与 check_solver_plan_parse 一致。"""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        saved = {
            key: os.environ.get(key)
            for key in ("UE_DISTILL_OUT", "UE_LEARNED_SKILLS", "UE_FAILURE_PATTERNS", "UE_LEXICON_CANDIDATES")
        }
        os.environ["UE_DISTILL_OUT"] = str(tmp / "distill")
        os.environ["UE_LEARNED_SKILLS"] = str(tmp / "learned_skills.json")
        os.environ["UE_FAILURE_PATTERNS"] = str(tmp / "failure_patterns.json")
        os.environ["UE_LEXICON_CANDIDATES"] = str(tmp / "lexicon_candidates.json")
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return _ctx()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    # 本机 .env 现在带 UE_SOLVER_ENABLED=true / UE_ALLOW_*=true；断言钉的是缺省，
    # 不封闭就会随机器漂移（与 check_solver_patch 同口径）。
    from eval._pin_offline import close_now

    close_now()
    check_zero_budget_spends_nothing()
    # 每个用例一个独立临时目录：失败模式按 id 覆盖，挤在一个目录里会互相踩。
    # 凡可能触发失败模式落库的用例都必须在隔离窗口内跑，绝不写真实 data/skills。
    for case in (
        check_plain_text_then_retry_succeeds,
        check_truncated_then_shrunk_json_succeeds,
        check_twice_plain_text_fails_honestly,
        check_truncated_by_finish_reason,
        check_unbalanced_braces_is_truncation,
        check_retry_never_exceeds_model_budget,
        check_solve_survives_first_plain_text,
        check_solve_plan_failure_is_readable,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            case(Path(tmp))
    print("solver plan retry ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


