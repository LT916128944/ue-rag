"""求解方案层的三个真机卡壳（20261028 载具整句实测）的离线断言。

20261028-153623 run：solve 扇走到 plan_steps 必挂。差分出三处：
1. `_text_of` 拿到的是 core.llm_call.complete 返回的 (response, record) 元组，
   取 .choices 失败后回退 str(raw)，把元组 repr 喂给 JSON 解析 → 永远解析失败。
   离线 eval 全用返回 str 的假件，测不出来。钉法：元组解包后能解析。
2. `inventory` 截到 8 个 action：editor 有 49 个，前 8 个全是 build/log 类，
   放 Actor 的原语永远进不了方案。钉法：49 个全保留。
3. `pick_namespaces`：范围文本里「开火」含「火」，niagara 误命中，
   editor（放 Actor 兜底）反而落选。钉法：editor 永远在内，「火」单字不再当关键词。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Response:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


def check_text_of_unwraps_complete_tuple() -> None:
    """complete 返回 (response, record)：第一件才是响应，repr 不许进解析。"""
    from agent.solver import _text_of

    content = '{"steps":[{"tool":"editor.list_actors","args":{}}]}'
    raw = (_Response(content), {"caller": "solve", "error": None})
    assert _text_of(raw) == content, "元组第一件是响应本体，必须解包"
    assert _text_of(_Response(content)) == content, "已经是响应也照常取"
    assert _text_of(content) == content, "裸字符串照常返回"
    assert _text_of(None) == "", "None 照常空串"
    assert _text_of((None, {"error": "x"})) == "", "元组里响应为 None 也不许炸"


def check_inventory_keeps_all_action_names() -> None:
    """editor 49 个 action 全保留：截到 8 个，模型看不见放 Actor 的原语。"""
    from agent.solver import Budget, inventory

    def fake_discover(namespace):
        return {"ok": True, "namespace": namespace, "actions": [
            {"name": f"editor.action_{i:02d}"} for i in range(49)
        ]}

    out = inventory("放一个 Actor 到关卡", Budget({"editor_calls": 4, "build_rounds": 1, "model_calls": 1}),
                    discover_fn=fake_discover)
    names = (out.get("actions") or {}).get("editor") or []
    assert len(names) == 49, f"49 个 action 应全保留，实得 {len(names)}"
    assert "editor.action_48" in names, "尾部 action 不许被截掉"


def check_pick_namespaces_editor_always_in() -> None:
    """editor 永远在内；「开火」不许再借单字「火」把 niagara 拽进来。"""
    from agent.solver import pick_namespaces

    picked = pick_namespaces("简易载具：上车、驾驶、边开车边射击，开火输入仍生效")
    assert "editor" in picked, f"场景类任务 editor 必须在内：{picked}"
    assert "niagara" not in picked, f"「开火」不许误命中 niagara：{picked}"

    picked_fire = pick_namespaces("加一个爆炸特效，粒子拖尾")
    assert "niagara" in picked_fire, f"真爆炸需求仍要挑中 niagara：{picked_fire}"
    assert "editor" in picked_fire, f"editor 仍然要在内：{picked_fire}"


def check_plan_parses_with_live_shaped_complete(tmp: Path) -> None:
    """端到端：complete 返回 (response, record) 形状，方案能产出并执行到收口。"""
    from agent import solver

    def fake_complete(_messages):
        plan = {"steps": [
            {"tool": "editor.spawn_actor", "args": {}},
            {"automation": "AIAgent.Vehicle.Drive"},
        ]}
        # 20261028 真机形状：complete_logged 返回元组，solver 必须自己解包。
        return (_Response(json.dumps(plan, ensure_ascii=False)), {"caller": "solve", "error": None})

    def fake_discover(namespace):
        return {"ok": True, "namespace": namespace, "actions": [{"name": "spawn_actor"}]}

    execs: list[str] = []

    def fake_exec(namespace, action, params, task):
        execs.append(f"{namespace}.{action}")
        return {"ok": True, "data": {"ok": True}}

    def fake_judge(spec, task=None):
        return {"ok": True, "kind": "automation", "filter": spec["filter"], "summary": "失败数 0"}

    task = {"id": "W1", "action": "solve", "query": "做一个简易载具放进关卡"}
    with _distill_tmp(tmp):
        out = solver.solve(
            task, complete=fake_complete, discover_fn=fake_discover,
            exec_fn=fake_exec, retrieve_fn=lambda _q, top_k=1: [], judge_fn=fake_judge,
        )
    assert out.get("ok") is True, out
    assert execs == ["editor.spawn_actor"], execs


def check_inline_build_order_and_editor_reconnect(tmp: Path) -> None:
    """执行序：apply_spec 就地 编译 → 编辑器回连 → 后面的放 Actor 才执行。

    20261028 第三轮实测的预判缺陷：apply_spec 攒到最后统一编译时，
    要么放 Actor 时新类还没编译（放置必败），要么编译时编辑器断连。
    钉法：build 先于后续 editor 步骤，ensure_editor 恰好隔在中间；
    编译失败则诚实 build_failed，后面的步骤一步都不跑。"""
    from agent import solver

    order: list[str] = []

    def fake_complete(_messages):
        plan = {"steps": [
            {"apply_spec": {"files": [
                {"path": "Public/SimpleVehiclePawn.h", "content": "#pragma once\n"},
                {"path": "Private/SimpleVehiclePawn.cpp", "content": "#include \"SimpleVehiclePawn.h\"\n"},
            ]}},
            {"tool": "editor.spawn_actor", "args": {}},
            {"automation": "AIAgent.Vehicle.Drive"},
        ]}
        return (_Response(json.dumps(plan, ensure_ascii=False)), {"caller": "solve", "error": None})

    def fake_discover(namespace):
        return {"ok": True, "namespace": namespace, "actions": [{"name": "spawn_actor"}]}

    def fake_exec(namespace, action, params, task):
        order.append(f"exec:{namespace}.{action}")
        return {"ok": True, "data": {"ok": True}}

    def fake_build(_files=None):
        order.append("build")
        return {"ok": True, "kind": "cpp_build", "build_rounds": 1, "summary": "ok", "fixes": []}

    def fake_ensure():
        order.append("ensure_editor")
        return {"ok": True, "started": False, "note": "MCP 已连通"}

    def fake_judge(spec, task=None):
        return {"ok": True, "kind": "automation", "filter": spec["filter"], "summary": "失败数 0"}

    task = {"id": "W1", "action": "solve", "query": "做一个简易载具放进关卡"}
    with _distill_tmp(tmp):
        out = solver.solve(
            task, complete=fake_complete, discover_fn=fake_discover,
            exec_fn=fake_exec, retrieve_fn=lambda _q, top_k=1: [],
            build=fake_build, judge_fn=fake_judge, ensure_editor_fn=fake_ensure,
        )
    assert out.get("ok") is True, out
    build_at = order.index("build")
    ensure_at = order.index("ensure_editor")
    spawn_at = order.index("exec:editor.spawn_actor")
    assert build_at < ensure_at < spawn_at, f"次序必须 编译→回连→放置：{order}"

    # 编译失败：诚实 build_failed，后续 editor 步骤一步不跑。
    order.clear()

    def bad_build(_files=None):
        order.append("build")
        return {"ok": False, "kind": "cpp_build", "build_rounds": 1, "summary": "error C2039",
                "errors": [{"file": "a.cpp", "line": 1, "message": "error C2039"}], "fixes": []}

    with _distill_tmp(tmp), _no_network_fix(None) as fixes:
        out = solver.solve(
            task, complete=fake_complete, discover_fn=fake_discover,
            exec_fn=fake_exec, retrieve_fn=lambda _q, top_k=1: [],
            build=bad_build, judge_fn=fake_judge, ensure_editor_fn=fake_ensure,
        )
    assert out.get("ok") is False, out
    assert out.get("outcome") == "build_failed", out
    builds = [item for item in order if item == "build"]
    assert 1 <= len(builds) <= 3, f"apply_spec_loop 回灌最多 3 轮编译：{order}"
    assert not any(item.startswith("exec:") for item in order), \
        f"编译失败后不许再跑编辑器步骤：{order}"
    # 20261028-163746 起的语义：编译会关编辑器，成败都要回连，下一扇不该陪葬。
    assert "ensure_editor" in order, f"编译后必须回连编辑器：{order}"
    assert order.index("ensure_editor") > order.index("build"), order
    # 20261029：修复器必须被替身拦住。真跑 run_fix 会调模型 + 加载 embedding，
    # 这条断言钉住「本检查不联网」，且证明 build_failed 确实走到了修复轮。
    assert fixes, "build_failed 分支应经过修复轮（run_fix 替身一次都没被调用）"


def _no_network_fix(monkeypatch_target):
    """把 `agent.build_error.run_fix` 换成不联网的假件（上下文管理器）。

    为什么要加这个：`solver._apply_spec_now` 调 `apply_spec_loop(..., fix=None)`，
    而 `fix is None` 会让 apply_spec_loop 落到默认修复器 `run_fix`，它真跑一轮图任务
    ——真调 DeepSeek、真加载 sentence-transformers。本检查本来想钉的是「编译失败后
    不再执行编辑器步骤」，与修复器无关；替身只保证 build_failed 分支不去联网。
    （20261029 发现：本机有 key 时会卡 32 秒到 3 分钟，CI 上等于依赖网络。）
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        from agent import build_error

        saved = build_error.run_fix
        calls: list[dict] = []

        def fake_run_fix(errors, *, wid="", query_hint=""):
            calls.append({"errors": len(errors or []), "wid": wid})
            # 修复轮「没改成」：循环据此再编译一轮，撞上限后停，正是 build_failed 的路径。
            return {"ok": False, "error": "离线检查：修复器被替身拦住，不联网"}

        build_error.run_fix = fake_run_fix
        try:
            yield calls
        finally:
            build_error.run_fix = saved

    return _ctx()


def _distill_tmp(tmp: Path):
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
    import contextlib
    import tempfile

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    check_text_of_unwraps_complete_tuple()
    check_inventory_keeps_all_action_names()
    check_pick_namespaces_editor_always_in()
    with tempfile.TemporaryDirectory() as tmp:
        check_plan_parses_with_live_shaped_complete(Path(tmp))
        check_inline_build_order_and_editor_reconnect(Path(tmp))
    print("solver_plan_parse ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())