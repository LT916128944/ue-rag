# eval/check_create_level.py
"""新建关卡动作（create_level）的离线验收（20261024-192858 的整改）：
1) 动作表：slots / DIRECT_ACTIONS / EDITOR_LOCK_ACTIONS / _VERIFY；
2) 归地：resolve_level（裸名 → /Game/Maps，/Game 路径原样，不唯一就暂停）；
3) 代码补窗：scope_windows（T2 接口，id 契约 S<范围 id>W<序号>）与 plan_windows 的句子级补窗；
4) 执行：sequence._create_level 的 exists → duplicate → save_assets → load_level → get_current_level，
   「已存在」幂等与「不存在」两条路径的调用序列，以及每一步失败时的明确 error；
5) 真实失败 run 的范围清单只救 B；窗口 → task 的字段白名单会截掉 level_path，
   执行分支必须能靠 slots 自己解析；条款绑定接口（动作 → evidence_kind 映射在 harness，本条只钉接口）。

不连真编辑器：exec_tool 全程换成假函数；不叫模型：complete 全是假函数。"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 真实失败 run（data/runs/20261024/20261024-192858.json）的原句
QUERY = "打开编辑器，新建一个关卡，名字为L_NewTest2，在这个关卡实现逻辑如下：需要实现简易的FPS射击游戏"
SCOPE_B = {
    "id": "B",
    "title": "新建关卡 L_NewTest2",
    "deliverables": ["在 /Game 下创建关卡资产 L_NewTest2", "把 L_NewTest2 设为当前编辑关卡"],
    "pending_note": "动作表里没有建关卡的动作，需要在编辑器内完成",
}
TARGET = "/Game/Maps/L_NewTest2"


def _boom(_messages):
    """离线：任何模型调用都不许真的发出去。"""
    raise RuntimeError("离线检查不叫模型")


def check_catalog() -> None:
    """动作表里必须有 create_level，且被判成直连 + 编辑器独占 + 有验收口径。"""
    from agent.catalog import (
        DIRECT_ACTIONS, EDITOR_LOCK_ACTIONS, action_meta, allows, spec_of, system_prompt,
    )

    spec = spec_of("create_level")
    assert isinstance(spec, dict), spec
    assert spec["slots"] == ("level",), spec
    assert allows("create_level") is True
    assert "create_level" in DIRECT_ACTIONS, "create_level 应进 DIRECT_ACTIONS"
    assert "create_level" in EDITOR_LOCK_ACTIONS, "create_level 应进 EDITOR_LOCK_ACTIONS"
    verification = action_meta("create_level")["verification"]
    assert verification, verification
    assert any("当前关卡" in item for item in verification), verification
    prompt = system_prompt()
    assert "create_level" in prompt and "槽位：level" in prompt, prompt[:400]


def check_resolve() -> None:
    """槽位归一与归地：裸名、/Game 路径、.Object 后缀；空/不唯一一律不猜。"""
    from agent.plan_windows import (
        _default_title, _missing_slots, _normalize_slots, materialize_goal, resolve_level,
    )

    bare = resolve_level("L_NewTest2")
    assert bare.get("ok") is True and bare["level_path"] == TARGET, bare
    assert bare["ref_path"] == f"{TARGET}.L_NewTest2", bare
    assert resolve_level(TARGET)["level_path"] == TARGET
    assert resolve_level(f"{TARGET}.L_NewTest2")["level_path"] == TARGET
    # 规划者只写裸名（没带 L_ 前缀）也认，落到 /Game/Maps
    assert resolve_level("MyMap")["level_path"] == "/Game/Maps/MyMap"
    # 名字与路径对不上、名字多于一条、空：三种都暂停，不猜
    assert resolve_level("L_A 和 L_B").get("ok") is False
    assert "不是唯一" in resolve_level("L_A 和 L_B")["error"]
    assert resolve_level("").get("ok") is False
    assert resolve_level("/Game/Maps/L_A 和 L_B").get("ok") is False
    assert resolve_level("新建一个关卡").get("ok") is False
    # 槽位别名：只认一个名字，别把整句当路径
    assert _normalize_slots("create_level", {"名字": "L_X2"})["level"] == "L_X2"
    assert _normalize_slots("create_level", {"level_name": "L_X2"})["level"] == "L_X2"
    assert "level" in _missing_slots("create_level", {}), _missing_slots("create_level", {})
    assert _default_title("create_level", {"level": "L_X2"}) == "新建关卡 L_X2"
    # materialize 主链：realizable=True，并带上解析好的路径
    made = materialize_goal("create_level", {"level": "L_NewTest2"}, title="", wid="W1",
                            depends=[], retries=1)
    assert len(made) == 1 and made[0]["realizable"] is True, made
    assert made[0]["goal"] is True and made[0]["level_path"] == TARGET, made
    assert made[0]["action"] == "create_level", made
    paused = materialize_goal("create_level", {"level": "L_A 和 L_B"}, title="", wid="W1",
                              depends=[], retries=1)
    assert paused[0]["realizable"] is False and "不是唯一" in paused[0]["error"], paused


def check_scope_windows() -> None:
    """T2 接口：范围补窗的 id 契约、依赖闭包、不抛异常。"""
    from agent.plan_windows import scope_windows

    made = scope_windows(SCOPE_B, QUERY)
    assert len(made) == 1, made
    window = made[0]
    assert window["id"] == "SBW1", window
    assert window["action"] == "create_level", window
    assert window["goal"] is True and window["realizable"] is True, window
    assert window["level_path"] == TARGET, window
    assert window["depends_on"] == [], window
    for item in made:
        assert item["id"].startswith("SBW"), item
        for dep in item["depends_on"]:
            assert dep in [other["id"] for other in made], (dep, made)
    # 范围自己没写名字、也没写标题时，名字从原句借一次
    borrowed = scope_windows({"id": "B", "title": "新建一个关卡"}, QUERY)
    assert borrowed and borrowed[0]["level_path"] == TARGET, borrowed
    # 前缀跟着范围 id 走：范围 C → SCW1
    named_c = scope_windows({"id": "C", "title": "新建一个关卡 L_C1"}, "")
    assert named_c[0]["id"] == "SCW1", named_c
    assert named_c[0]["level_path"] == "/Game/Maps/L_C1", named_c
    # 范围没写 id（缺、空串、全空白）→ 退成 SW 前缀，id 仍只在本调用内唯一
    for no_id in ({"title": "新建一个关卡 L_NoId"}, {"id": "", "title": "新建一个关卡 L_NoId"},
                  {"id": "   ", "title": "新建一个关卡 L_NoId"}):
        made = scope_windows(no_id, "")
        assert [window["id"] for window in made] == ["SW1"], (no_id, made)
        assert made[0]["action"] == "create_level" and made[0]["goal"] is True, made
        for dep in made[0]["depends_on"]:
            assert dep in ["SW1"], (dep, made)
    # 一调用多扇：序号递增，依赖闭包只在本调用内
    pair = scope_windows({"id": "D", "title": "新建蓝图 BP_Gun 并放进场景"},
                         "新建蓝图 BP_Gun 并放进场景 /Game/BP/BP_Gun")
    assert [window["id"] for window in pair] == ["SDW1", "SDW2"], pair
    assert [window["action"] for window in pair] == ["create_blueprint", "place_blueprint"], pair
    assert pair[1]["depends_on"] == ["SDW1"], pair
    assert all(window["goal"] is True for window in pair), pair
    # 蓝图那条复用已有 create_blueprint 归地（路径从原句借）
    blue = scope_windows({"id": "D", "title": "新建蓝图 BP_Gun"}, "新建蓝图 /Game/BP/BP_Gun")
    assert blue and blue[0]["action"] == "create_blueprint", blue
    assert blue[0]["id"] == "SDW1" and blue[0]["goal"] is True, blue
    assert blue[0]["realizable"] is True, blue
    # 归地字段在 task 里（窗口带 task 时 _prepare_task 直接用这一份，不会被键白名单截掉）
    assert blue[0]["task"]["folder_path"] == "/Game/BP", blue
    assert blue[0]["task"]["ref_path"] == "/Game/BP/BP_Gun.BP_Gun", blue
    # 匹配不上就是空列表，而且别的范围不会被原句的需求污染
    assert scope_windows({"id": "A", "title": "启动 UE 编辑器并等待 MCP 端口连通"}, QUERY) == []
    # 标题对不上已有脚本时不硬收成蓝图窗。要拆开得把 complete 传进来。
    fps = scope_windows({"id": "C", "title": "在 L_NewTest2 内实现简易 FPS 射击玩法"}, QUERY)
    assert fps == [], fps
    assert scope_windows({}, "") == []
    # 任何脏输入都不许抛异常
    for bad in (None, "不是字典", 42, {"id": None, "title": None, "deliverables": 7},
                {"id": 1, "deliverables": [None, {"x": 1}], "title": []}):
        got = scope_windows(bad, QUERY)
        assert isinstance(got, list), (bad, got)


def check_plan_fill() -> None:
    """句子级补窗：L0 规则路径直接接住；理解器/规划者漏拆时由代码补一扇。"""
    from agent.plan_windows import _ensure_requested_create_level, plan_windows
    from agent.supervise import _plan_rules, _wants_create_level

    # 没有需求半句的句子 → L0 强命中，直接拆出 create_level，不叫模型
    simple = "新建一个关卡 L_Test2"
    planned = plan_windows(simple, complete=_boom)
    actions = [window.get("action") for window in planned["windows"]]
    assert planned["source"] == "rules", planned["source"]
    assert actions == ["create_level"], actions
    assert planned["windows"][0]["realizable"] is True, planned["windows"][0]
    # 规则路径的窗口把整步放在 task 里（_prepare_task 直接用这一份），路径字段在 task 上
    assert planned["windows"][0]["task"]["level_path"] == "/Game/Maps/L_Test2", planned["windows"][0]

    # 原句那句：即使理解器只拆出 launch_editor，代码也要把建关卡补上，且排在启动之后
    def one_launch(_messages):
        return json.dumps({"query": QUERY, "goals": [{"action": "launch_editor", "slots": {}}],
                           "criteria": [], "constraints": {"editor": True}, "unresolved": []})

    understood = plan_windows(QUERY, complete=one_launch)
    assert understood["source"] == "understood", understood["source"]
    kinds = [window.get("action") for window in understood["windows"]]
    assert "create_level" in kinds, kinds
    assert kinds.index("launch_editor") < kinds.index("create_level"), kinds
    created = [window for window in understood["windows"] if window.get("action") == "create_level"]
    assert len(created) == 1 and created[0]["goal"] is True, created
    assert created[0]["realizable"] is True and created[0]["level_path"] == TARGET, created

    # 老规划者路径：规划者只写 launch_editor 一扇，也要补出 create_level
    def planner_only(_messages):
        return json.dumps({"windows": [{
            "id": "W1", "title": "启动编辑器", "action": "launch_editor",
            "slots": {}, "depends_on": [], "max_retries": 0,
        }]}, ensure_ascii=False)

    fallback = plan_windows(QUERY, complete=planner_only)
    assert fallback["source"] == "planner", fallback["source"]
    kinds = [window.get("action") for window in fallback["windows"]]
    assert kinds == ["launch_editor", "create_level"], kinds
    assert fallback["windows"][1]["depends_on"] == ["W1"], fallback["windows"][1]

    # 补窗是幂等的：已经有一扇 create_level 就不再补
    existing = plan_windows(QUERY, complete=one_launch)["windows"]
    assert _ensure_requested_create_level(QUERY, existing) is existing
    # 句子里没有「建关卡」时一扇都不补
    assert _wants_create_level("在 L_NewTest 里做一个平台跳跃关卡") is False
    untouched = [{"id": "W1", "action": "place_actor", "slots": {"blueprint": "BP_Coin"}}]
    assert _ensure_requested_create_level("做一个平台跳跃关卡，左上角计数", untouched) is untouched
    rules = _plan_rules(QUERY)
    assert [step.get("action") for step in rules] == ["launch_editor", "create_level"], rules


def check_supervise() -> None:
    """句子级纯函数：要不要建关卡、名字是什么、缺名字就暂停。"""
    from agent.supervise import _create_level_task, _level_token, _plan_rules, _wants_create_level

    assert _wants_create_level(QUERY) is True
    assert _wants_create_level("新建一个地图 L_M1") is True
    assert _wants_create_level("打开编辑器") is False
    # 中间夹了别的词不算「新建关卡」
    assert _wants_create_level("在 L_NewTest 里做一个平台跳跃关卡") is False
    assert _level_token(QUERY) == "L_NewTest2"
    assert _level_token("新建关卡 /Game/Maps/L_Solo") == "L_Solo"
    assert _level_token("新建关卡 L_A 和 L_B") == ""
    task = _create_level_task(QUERY)
    assert task["action"] == "create_level" and task.get("ok") is not False, task
    assert task["level_path"] == TARGET, task
    assert "duplicate" in task["allow_writes"] and "load_level" in task["allow_writes"], task
    blocked = _create_level_task("新建一个关卡")
    assert blocked.get("ok") is False and blocked.get("blocked") is True, blocked
    assert blocked.get("error"), blocked
    # 只有建关卡、没要求启动编辑器时，不硬塞 launch_editor
    assert [step.get("action") for step in _plan_rules("新建一个关卡 L_Test2")] == ["create_level"]


class FakeEditor:
    """假编辑器：只认 create_level 链上的五个原语，别的一律报错，防止偷偷连真的。"""

    def __init__(self, *, exists=False, duplicate_error="", save_error="", load_error="",
                 current_error="", current_level=None):
        self.exists = exists
        self.duplicate_error = duplicate_error
        self.save_error = save_error
        self.load_error = load_error
        self.current_error = current_error
        # current_level 给定时，get_current_level 永远回这个值（模拟载入没生效）；
        # 不给定时跟随 load_level 的入参。
        self.current_level = current_level
        self.current = current_level if current_level is not None else "/Game/Maps/L_NewTest"
        self.calls: list[dict] = []

    def __call__(self, toolset, tool, args=None, *, task=None, timeout=None):
        del toolset, timeout
        args = dict(args or {})
        self.calls.append({"tool": tool, "args": args, "task": dict(task or {})})
        if tool == "exists":
            return {"ok": True, "data": self.exists}
        if tool == "duplicate":
            if self.duplicate_error:
                return {"ok": False, "error": self.duplicate_error}
            self.exists = True
            return {"ok": True, "data": True}
        if tool == "save_assets":
            if self.save_error:
                return {"ok": False, "error": self.save_error}
            return {"ok": True, "data": True}
        if tool == "load_level":
            if self.load_error:
                return {"ok": False, "error": self.load_error}
            if self.current_level is None:
                self.current = str(args.get("level_path") or "")
            return {"ok": True, "data": True}
        if tool == "get_current_level":
            if self.current_error:
                return {"ok": False, "error": self.current_error}
            return {"ok": True, "data": {"returnValue": self.current}}
        return {"ok": False, "error": f"假编辑器没实现 {tool}"}

    @property
    def tools(self) -> list[str]:
        return [call["tool"] for call in self.calls]

    def call(self, tool: str) -> dict:
        found = next((call for call in self.calls if call["tool"] == tool), None)
        assert found is not None, self.tools
        return found


def _pin_backend(name: str):
    """这组检查钉死一条后端。本机 .env 是 monolith 时，不钉的话官方五步假编辑器不会被走到。"""
    import config
    import backends.select as select

    select.reset_lock()
    original = config.editor_backend
    config.editor_backend = lambda: name
    return original


def _unpin_backend(original) -> None:
    import config
    import backends.select as select

    config.editor_backend = original
    select.reset_lock()


def _with_fake_editor(fake: FakeEditor, step: dict) -> dict:
    import core.editor_exec as editor_exec
    from agent.sequence import _create_level

    original = editor_exec.exec_tool
    editor_exec.exec_tool = fake
    pinned = _pin_backend("official")
    try:
        return _create_level(step)
    finally:
        _unpin_backend(pinned)
        editor_exec.exec_tool = original
        assert editor_exec.exec_tool is original


class FakeMonolith:
    """假 Monolith：只认建关卡这条链，别的 action 报错。不连 9316。"""

    def __init__(self, *, found=False, probe_error="", create_error="", opened=True,
                 current_world=None, load_error="", loaded=True, load_path=None):
        self.found = found
        self.probe_error = probe_error
        self.create_error = create_error
        self.opened = opened
        self.current_world = current_world
        self.load_error = load_error
        self.loaded = loaded
        self.load_path = load_path
        self.calls: list[dict] = []

    def __call__(self, namespace, action, params=None, task=None, transport=None):
        del transport
        params = dict(params or {})
        self.calls.append({
            "namespace": namespace, "action": action, "params": params, "task": dict(task or {}),
        })
        if action == "get_asset_details":
            if self.probe_error:
                return {"ok": False, "error": self.probe_error}
            if not self.found:
                return {"ok": False, "error": "Asset not found in index"}
            return {"ok": True, "data": {"success": True, "asset": {"asset_name": params.get("asset_path")}}}
        if action == "create_empty_map":
            if self.create_error:
                return {"ok": False, "error": self.create_error}
            self.found = True
            world = self.current_world if self.current_world is not None else str(params.get("path") or "")
            if not self.opened:
                world = "/Game/Maps/L_NewTest"
            return {"ok": True, "data": {
                "ok": True, "path": params.get("path"), "opened": self.opened, "current_world": world,
            }}
        if action == "load_level":
            if self.load_error:
                return {"ok": False, "error": self.load_error}
            path = self.load_path if self.load_path is not None else params.get("path")
            return {"ok": True, "data": {"ok": self.loaded, "loaded": self.loaded, "path": path,
                                         "message": "" if self.loaded else "LoadLevel returned false"}}
        return {"ok": False, "error": f"假 Monolith 没实现 {namespace}.{action}"}

    @property
    def actions(self) -> list[str]:
        return [call["action"] for call in self.calls]

    def call(self, action: str) -> dict:
        found = next((call for call in self.calls if call["action"] == action), None)
        assert found is not None, self.actions
        return found


def _with_fake_monolith(fake: FakeMonolith, step: dict) -> dict:
    import backends.monolith as monolith
    import core.editor_exec as editor_exec
    from agent.sequence import _create_level

    def _boom(*_args, **_kwargs):
        raise AssertionError("monolith 建关卡不许走官方 exec_tool")

    original_exec = monolith.exec
    original_tool = editor_exec.exec_tool
    monolith.exec = fake
    editor_exec.exec_tool = _boom
    pinned = _pin_backend("monolith")
    try:
        return _create_level(step)
    finally:
        _unpin_backend(pinned)
        monolith.exec = original_exec
        editor_exec.exec_tool = original_tool


def check_monolith_runner() -> None:
    """UE_EDITOR_BACKEND=monolith 时建关卡走 project/editor action，不碰官方 exists。"""
    step = {"id": "W2", "action": "create_level", "slots": {"level": "L_NewTest2"}}
    previous = os.environ.get("UE_LEVEL_TEMPLATE")
    os.environ.pop("UE_LEVEL_TEMPLATE", None)
    try:
        _check_monolith_cases(step)
    finally:
        if previous is None:
            os.environ.pop("UE_LEVEL_TEMPLATE", None)
        else:
            os.environ["UE_LEVEL_TEMPLATE"] = previous


def _check_monolith_cases(step: dict) -> None:
    fresh = FakeMonolith(found=False)
    done = _with_fake_monolith(fresh, step)
    assert done.get("ok") is True, done
    assert fresh.actions == ["get_asset_details", "create_empty_map"], fresh.actions
    assert fresh.call("create_empty_map")["params"] == {
        "path": TARGET, "open": True, "dirty_policy": "discard",
    }, fresh.calls
    assert fresh.call("get_asset_details")["task"]["allow_writes"] == [], fresh.calls
    assert fresh.call("create_empty_map")["task"]["save"] is True, fresh.calls
    assert "空白关卡" in str(done.get("index_note")), done
    assert done.get("judge_kind") == "editor_assert", done

    same = FakeMonolith(found=True)
    again = _with_fake_monolith(same, step)
    assert again.get("ok") is True, again
    assert same.actions == ["get_asset_details", "load_level"], same.actions
    assert same.call("load_level")["params"]["path"] == TARGET, same.calls
    assert "已存在，不重复新建" in str(again.get("index_note")), again

    raced = FakeMonolith(found=False, create_error="Asset already exists at /Game/Maps/L_NewTest2; delete first.")
    racy = _with_fake_monolith(raced, step)
    assert racy.get("ok") is True, racy
    assert raced.actions == ["get_asset_details", "create_empty_map", "load_level"], raced.actions
    assert "已存在，不重复新建" in str(racy.get("index_note")), racy

    closed = FakeMonolith(found=False, opened=False)
    opened = _with_fake_monolith(closed, step)
    assert opened.get("ok") is True, opened
    assert closed.actions == ["get_asset_details", "create_empty_map", "load_level"], closed.actions

    cases = [
        (FakeMonolith(found=False, probe_error="UE MCP 调用失败: timeout"), "查关卡", ["get_asset_details"]),
        (FakeMonolith(found=False, create_error="Failed to construct UWorldFactory"), "新建关卡",
         ["get_asset_details", "create_empty_map"]),
        (FakeMonolith(found=True, load_error="Refusing load_level"), "加载失败",
         ["get_asset_details", "load_level"]),
        (FakeMonolith(found=True, loaded=False), "加载失败", ["get_asset_details", "load_level"]),
        (FakeMonolith(found=True, load_path="/Game/Maps/L_Other"), "不是",
         ["get_asset_details", "load_level"]),
    ]
    for fake, needle, actions in cases:
        failed = _with_fake_monolith(fake, step)
        assert failed.get("ok") is False, (needle, failed)
        assert needle in str(failed.get("error")), (needle, failed)
        assert "UE_EDITOR_BACKEND=monolith" not in str(failed.get("error")), failed
        assert fake.actions == actions, (needle, fake.actions)

    blocked = FakeMonolith(found=False)
    naive = _with_fake_monolith(blocked, {"id": "W2", "action": "create_level", "slots": {"level": "L_NewTest"}})
    assert naive.get("ok") is False and "模板关卡" in str(naive.get("error")), naive
    assert blocked.actions == ["get_asset_details"], blocked.actions


def check_runner() -> None:
    """执行分支：两条路径的调用序列、幂等、以及每一步失败时的明确 error。"""
    from agent.sequence import _level_matches, _level_template_path

    # 复查口径：路径按包路径比、名字按词边界比（L_NewTest 不能被 L_NewTest2 顶上）
    assert _level_matches("/Game/Maps/L_NewTest2", TARGET, "L_NewTest2") is True
    assert _level_matches("/Game/Maps/L_NewTest2.L_NewTest2", TARGET, "L_NewTest2") is True
    assert _level_matches("/Game/Maps/L_NewTest2:PersistentLevel.SkyLight_0", TARGET, "L_NewTest2") is True
    assert _level_matches("L_NewTest2", TARGET, "L_NewTest2") is True
    assert _level_matches("/Game/Maps/L_NewTest", TARGET, "L_NewTest2") is False
    assert _level_matches("/Game/Maps/L_NewTest2", "/Game/Maps/L_NewTest", "L_NewTest") is False
    assert _level_matches("", TARGET, "L_NewTest2") is False

    step = {"id": "W2", "action": "create_level", "slots": {"level": "L_NewTest2"}}
    previous = os.environ.get("UE_LEVEL_TEMPLATE")
    os.environ.pop("UE_LEVEL_TEMPLATE", None)
    try:
        assert _level_template_path() == "/Game/Maps/L_NewTest", _level_template_path()
        os.environ["UE_LEVEL_TEMPLATE"] = "/Game/Maps/L_Base"

        # 不存在 → 复制模板，落盘、载入、复查四条原语按序执行
        fresh = FakeEditor(exists=False)
        done = _with_fake_editor(fresh, step)
        assert done.get("ok") is True, done
        assert fresh.tools == ["exists", "duplicate", "save_assets", "load_level", "get_current_level"], fresh.tools
        assert fresh.call("duplicate")["args"] == {"path": "/Game/Maps/L_Base", "new_path": TARGET}, fresh.calls
        assert fresh.call("duplicate")["task"]["save"] is True, fresh.calls
        assert "duplicate" in fresh.call("duplicate")["task"]["allow_writes"], fresh.calls
        assert "load_level" in fresh.call("load_level")["task"]["allow_writes"], fresh.calls
        assert fresh.call("save_assets")["args"] == {"asset_paths": [TARGET]}, fresh.calls
        # 只读原语（exists / get_current_level）不许带写授权
        assert fresh.call("exists")["task"]["allow_writes"] == [], fresh.calls
        assert fresh.call("get_current_level")["task"]["allow_writes"] == [], fresh.calls
        assert done.get("judge_kind") == "editor_assert" and done["test"]["ok"] is True, done
        assert "复制" in str(done.get("index_note")), done

        # 已存在 → 跳过复制（幂等），仍落盘 + 载入 + 复查，note 说明不重复新建
        same = FakeEditor(exists=True)
        again = _with_fake_editor(same, step)
        assert again.get("ok") is True, again
        assert same.tools == ["exists", "save_assets", "load_level", "get_current_level"], same.tools
        assert "已存在，不重复新建" in str(again.get("index_note")), again

        # 复制报「已存在」时也算幂等命中，不报失败
        raced = FakeEditor(exists=False, duplicate_error="Asset already exists at path")
        racy = _with_fake_editor(raced, step)
        assert racy.get("ok") is True, racy
        assert "已存在，不重复新建" in str(racy.get("index_note")), racy

        # 每一步失败都要说清楚错在哪一步，不猜、不静默
        cases = [
            (FakeEditor(exists=False, duplicate_error="拒绝连接"), "复制关卡", "duplicate"),
            (FakeEditor(exists=True, save_error="write refused"), "落盘失败", "save_assets"),
            (FakeEditor(exists=True, load_error="关卡打不开"), "加载失败", "load_level"),
            (FakeEditor(exists=True, current_error="读不出"), "复查当前关卡失败", "get_current_level"),
        ]
        for fake, needle, last in cases:
            failed = _with_fake_editor(fake, step)
            assert failed.get("ok") is False, (needle, failed)
            assert needle in str(failed.get("error")), (needle, failed)
            assert fake.tools[-1] == last, (needle, fake.tools)

        # 载入后当前关卡对不上 → 明确失败（L_NewTest 不能被 L_NewTest2 顶上）
        mismatch = FakeEditor(exists=True, current_level="/Game/Maps/L_Other")
        wrong = _with_fake_editor(mismatch, step)
        assert wrong.get("ok") is False and "不是" in str(wrong.get("error")), wrong

        # 模板就是目标、又不存在 → 没有可复制的新关卡
        sametpl = FakeEditor(exists=False)
        naive = _with_fake_editor(sametpl, {"id": "W2", "action": "create_level",
                                           "slots": {"level": "L_Base"}})
        assert naive.get("ok") is False and "模板关卡" in str(naive.get("error")), naive

        # 槽位空 / 不唯一 → 一个工具都不调，直接报不猜
        for bad in ("", "L_A 和 L_B"):
            untouched = FakeEditor(exists=True)
            refused = _with_fake_editor(untouched, {"id": "W2", "action": "create_level",
                                                   "slots": {"level": bad}})
            assert refused.get("ok") is False, refused
            assert untouched.tools == [], untouched.tools

        # 复查的匹配是按词边界，不是子串：目标是 L_NewTest 时 L_NewTest2 不算命中
        boundary = FakeEditor(exists=True, current_level="/Game/Maps/L_NewTest2")
        off = _with_fake_editor(boundary, {"id": "W2", "action": "create_level",
                                          "slots": {"level": "L_NewTest"}})
        assert off.get("ok") is False, off
    finally:
        if previous is None:
            os.environ.pop("UE_LEVEL_TEMPLATE", None)
        else:
            os.environ["UE_LEVEL_TEMPLATE"] = previous


def check_real_run() -> None:
    """真实失败 run 的范围清单：B 补出建关卡。C 的标题对不上已有脚本，离线不硬补。"""
    from agent.plan_windows import scope_windows

    run_path = (Path(__file__).resolve().parent.parent / "data" / "runs" / "20261024"
                / "20261024-192858.json")
    if not run_path.is_file():
        return
    run = json.loads(run_path.read_text(encoding="utf-8"))
    query = str(run.get("query") or "")
    found = {str(scope.get("id")): scope_windows(scope, query) for scope in run.get("scopes") or []}
    assert [window["action"] for window in found.get("B") or []] == ["create_level"], found
    assert found["B"][0]["level_path"] == TARGET, found["B"]
    assert found["B"][0]["goal"] is True and found["B"][0]["realizable"] is True, found["B"]
    assert found["B"][0]["id"] == "SBW1", found["B"]
    assert found.get("A") == [] and found.get("INT") == [], found
    assert found.get("C") == [], found.get("C")
    assert [sid for sid, made in found.items() if made] == ["B"], found


def check_window_to_task() -> None:
    """窗口 → 任务字典的字段白名单会截掉 level_path；slots 还在，执行分支必须能自己解析。"""
    from agent.plan_windows import materialize_goal
    from agent.run_windows import _prepare_task

    window = materialize_goal("create_level", {"level": "L_NewTest2"}, title="", wid="W2",
                              depends=["W1"], retries=1)[0]
    task = _prepare_task(window, "原句", {})
    assert task["action"] == "create_level" and task["slots"] == {"level": "L_NewTest2"}, task
    # 白名单不带 level_path（run_windows.py 不在本次写入范围）：这条断言把陷阱钉住
    assert not task.get("level_path"), task
    fake = FakeEditor(exists=False)
    done = _with_fake_editor(fake, task)
    assert done.get("ok") is True, done
    assert fake.tools == ["exists", "duplicate", "save_assets", "load_level", "get_current_level"], fake.tools
    assert fake.call("duplicate")["args"]["new_path"] == TARGET, fake.calls


def check_criteria_binding() -> None:
    """条款绑定：动作 → evidence_kind 的映射在 agent.harness（不在本次写入范围，task-6 负责）。
    这条只钉接口、不写 harness：映射补上后 build_criteria 必须产出一条条款并绑到那一扇；
    还没补上时不许报错、也不许伪造条款（窗口自身的 test 仍带 editor_assert 判词）。"""
    from agent.harness import _KIND_BY_ACTION, build_criteria
    from agent.plan_windows import materialize_goal

    window = materialize_goal("create_level", {"level": "L_NewTest2"}, title="", wid="W2",
                              depends=["W1"], retries=1)[0]
    rows = build_criteria(None, [window])
    if "create_level" in _KIND_BY_ACTION:
        assert rows, "映射补上后必须产出一条条款"
        assert rows[0]["evidence_kind"] == _KIND_BY_ACTION["create_level"], rows
        assert rows[0]["windows"] == ["W2"], rows
    else:
        assert rows == [], rows


def check_no_editor() -> None:
    """这次检查不许碰真编辑器：实时 exec_tool 一次都没被调用过。"""
    from core.editor_exec import exec_tool, is_write_tool

    # duplicate / load_level 不在 WRITE_HINTS 里，不会被自动拦；所以 allow_writes 必须自己声明。
    assert is_write_tool("duplicate") is False
    assert is_write_tool("load_level") is False
    assert is_write_tool("save_assets") is False  # save 走 SAVE_TOOLS 那条分支
    assert callable(exec_tool)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("UE_LEARNED_ROUTES")
        # 规划回写不许污染仓库里的 learned_routes.json
        os.environ["UE_LEARNED_ROUTES"] = str(Path(tmp) / "learned_routes.json")
        # 离线：理解器的检索标题不查，别加载检索模型
        import agent.understand as understand_mod

        saved_titles = understand_mod._titles
        understand_mod._titles = lambda _query: []
        try:
            check_catalog()
            check_resolve()
            check_scope_windows()
            check_plan_fill()
            check_supervise()
            check_real_run()
            check_runner()
            check_monolith_runner()
            check_window_to_task()
            check_criteria_binding()
            check_no_editor()
        finally:
            understand_mod._titles = saved_titles
            if previous is None:
                os.environ.pop("UE_LEARNED_ROUTES", None)
            else:
                os.environ["UE_LEARNED_ROUTES"] = previous
    print("create level ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())