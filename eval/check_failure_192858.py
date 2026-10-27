# eval/check_failure_192858.py
"""T3 独立复验（20261024-192858）：失败链条的端到端离线回归 + 对抗性用例。

被验对象：run data/runs/20261024/20261024-192858.json（原句「打开编辑器，新建一个关卡，
名字为L_NewTest2，在这个关卡实现逻辑如下：需要实现简易的FPS射击游戏，内容你自由发挥」）。
当时只跑出 launch_editor 一扇：B（新建关卡）、C（FPS 玩法）两段需求交付清单非空、窗口为 0，
只记一句「没有可执行的窗口，只记录待办」就放行；集成矩阵 5 项全不适用 ⇒ checked 为空
⇒ 笼统 judge_failed，收尾记「部分完成」，B/C 的缺口一个字都没落地。

本文件是**独立写的**：不复用 T1/T2/T5/T6/T7/T8 自测文件的断言与假件，只复用它们的被验函数。
断言分四组：
  a) 规划/物化：B 段必须出现 realizable 的 create_level 窗口；C 段必须是结构化 capability_gap
     或可用窗口；任何范围都不许「0 窗口且无声无息」。
  b) 「只有 launch_editor 成功 + 集成扇全不适用」这类结局：综合状态必须是「未完成」
     （不是「部分完成」，更不是「成功」），说明里点名缺口范围。
  c) integrate_verify 无证据时给出第三态 verdict="no_evidence"，与笼统 judge_failed 可区分。
  d) 反例守卫两条：真做成（正常窗口全 done、条款全过、无缺口）仍必须是「成功」且 label 含
     「矩阵全部通过」；一条条款都没有且无缺口时，集成不许撑成功。
外加规划层检查（catalog.system_prompt / plan_windows / scope_windows）与对抗性用例：
幂等、不猜、四条失败路径、老记录兼容、三态互斥、归档保真、条款绑定两侧对齐。

**已知缺陷（T3 报给 lead，task-9 修）**：`_run_integrate` 的聚合口径只看 checked 是否全 True。
「交付验收」在有缺口时被强制成 ok=None（不投票），而「拒答路径」「延迟窗口」只要窗口有终态
就给 ok=True（延迟窗口连失败都算过），于是「有缺口 + 有一项运行期项为 True」也能凑出
「矩阵全部通过」+ 集成扇 ok=True + stop_reason=ok。本文件把这两条写成**严格断言**（证伪方
不该把已知缺陷写成当前行为）：修好之前本套件 exit=1 是正确的。

不联网、不调模型、不碰编辑器：run_task / scope_windows / exec_tool 全程换成假函数，
RUNS/EVENTS/ARCHIVE/SNAPSHOTS/BRIEFS/LEDGER/HANDOFFS 全部隔离到临时目录。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent
REAL_BRIEF = REPO / "data" / "briefs" / "20261024-192830-L_NewTest2-FPS.json"
REAL_RUN = REPO / "data" / "runs" / "20261024" / "20261024-192858.json"
REAL_ARCHIVE = REPO / "data" / "archive" / "20261024-192858" / "deliverables.json"

# run 20261024-192858 的原句（一字不差）
QUERY = ("打开编辑器，新建一个关卡，名字为L_NewTest2，在这个关卡实现逻辑如下："
         "需要实现简易的FPS射击游戏，内容你自由发挥")
TARGET = "/Game/Maps/L_NewTest2"


def _quiet(*_args, **_kwargs):
    return None


def _boom(_messages):
    """离线：任何模型调用都不许真的发出去。"""
    raise RuntimeError("离线检查不叫模型")


def _forbid_editor(*_args, **_kwargs):
    """离线：真编辑器一个工具都不许调。"""
    raise AssertionError("离线检查碰了真编辑器：core.editor_exec.exec_tool 被调用")


# ---------------------------------------------------------------- 真实件 / 替身

def _inline_brief() -> dict:
    """真实任务书 20261024-192830 的同形状替身（data/briefs 是 gitignore 的，CI 上没有）。
    原句、A/B/C 的交付清单、B/C 的旧 pending_note 与旧 block_note 都与真实文件一致。"""
    from agent.brief import _LEGACY_ZERO_WINDOW_NOTE

    return {
        "query": QUERY,
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器并等待 MCP 端口连通",
             "deliverables": ["UE 编辑器进程启动完成", "MCP 端口可连通，后续动作可下发"],
             "pending_note": "", "depends_on": [], "external_dep": "", "estimate_hours": [0.1, 0.5],
             "windows": [{"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
                          "slots": {}, "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "新建关卡 L_NewTest2",
             "deliverables": ["在 /Game 下创建关卡资产 L_NewTest2", "把 L_NewTest2 设为当前编辑关卡"],
             "pending_note": "动作表里没有建关卡的动作，需要在编辑器内完成；"
                             "代码/资产尚无，缺创建与切换当前关卡的接线和验证",
             "depends_on": ["A"], "external_dep": "", "estimate_hours": [2.0, 8.0],
             "windows": [], "block_note": _LEGACY_ZERO_WINDOW_NOTE},
            {"id": "C", "title": "在 L_NewTest2 内实现简易 FPS 射击玩法",
             "deliverables": ["玩家角色：移动、视角、开火输入", "射击命中判定与可击倒/可拾取目标",
                              "计分或结算反馈（HUD/日志择一）", "把上述内容放进 L_NewTest2 并完成接线",
                              "在关卡内跑通一次射击到命中的验证"],
             "pending_note": "原句只说“自由发挥”，未点名具体蓝图或资产；"
                             "实现清单、资产命名与接线都还没定，也没验证",
             "depends_on": ["B"], "external_dep": "", "estimate_hours": [5.0, 20.0],
             "windows": [], "block_note": _LEGACY_ZERO_WINDOW_NOTE},
        ],
        "integration": {"title": "累计集成、验证与交付",
                        "deliverables": ["按 A→B→C 的顺序串起，确认编辑器已连通后再操作关卡",
                                         "在 L_NewTest2 内完整跑一次：启动→移动→射击→命中反馈",
                                         "对修改做一次只读复验", "记录未接线/未验证的遗留条目"],
                        "estimate_hours": [2.0, 8.0]},
        "external_dependencies": [],
    }


def _real_brief() -> tuple[dict, bool]:
    """(原始任务书, 是否真读到仓库里的那份)。缺文件时用同形状替身，并在输出里说清楚。"""
    if REAL_BRIEF.is_file():
        return json.loads(REAL_BRIEF.read_text(encoding="utf-8")), True
    return _inline_brief(), False


def _inline_legacy_run() -> dict:
    """192858 那份老记录的同形状替身：没有 capability_gap、没有 verdict、criteria 只有一条。"""
    return {
        "run_id": "20261024-192858", "query": QUERY, "source": "understood",
        "status": "部分完成", "review": {"rounds": 1, "status": "通过"},
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器并等待 MCP 端口连通", "window_ids": ["W1"],
             "external_dep": "", "blocked": False, "deliverables": ["UE 编辑器进程启动完成"],
             "pending_note": "",
             "criteria": [{"id": "c-editor_assert", "text": "编辑器读数与期望一致",
                           "evidence_kind": "editor_assert", "passed": True, "windows": ["W1"]}]},
            {"id": "B", "title": "新建关卡 L_NewTest2", "window_ids": [], "external_dep": "",
             "blocked": False, "deliverables": ["在 /Game 下创建关卡资产 L_NewTest2"],
             "pending_note": "动作表里没有建关卡的动作，需要在编辑器内完成", "criteria": []},
            {"id": "C", "title": "在 L_NewTest2 内实现简易 FPS 射击玩法", "window_ids": [],
             "external_dep": "", "blocked": False, "deliverables": ["玩家角色：移动、视角、开火输入"],
             "pending_note": "原句只说“自由发挥”", "criteria": []},
            {"id": "INT", "title": "累计集成、验证与交付", "window_ids": ["W2"], "external_dep": "",
             "blocked": False, "deliverables": ["对修改做一次只读复验"], "pending_note": "",
             "kind": "integration", "criteria": []},
        ],
        "windows": {
            "W1": {"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor", "depends_on": [],
                   "state": "done", "ok": True, "result": {"id": "W1", "ok": True, "summary": "已启动"},
                   "detail": {"id": "W1", "ok": True, "judge_kind": "editor_assert"},
                   "thread_id": "t1", "elapsed": 0, "retries": 0, "summary": "已启动",
                   "snapshot": None, "realizable": True, "error": "", "external_dep": "",
                   "window": {"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
                              "slots": {}, "depends_on": [], "max_retries": 1, "realizable": True,
                              "ok": True, "blocked": False, "save": True, "pie": True}},
            "W2": {"id": "W2", "title": "累计集成、验证与交付", "action": "integrate_verify",
                   "depends_on": ["W1"], "state": "failed", "ok": False,
                   "result": {"id": "W2", "ok": False, "summary": "矩阵没有可验证的项，不算全过"},
                   "detail": {"id": "W2", "ok": False, "matrix": [], "hashes": [],
                              "summary": "矩阵没有可验证的项，不算全过"},
                   "thread_id": None, "elapsed": None, "retries": 0, "summary": "矩阵没有可验证的项",
                   "snapshot": None, "realizable": True, "error": "", "external_dep": "",
                   "window": {"id": "W2", "title": "累计集成、验证与交付",
                              "action": "integrate_verify", "slots": {}, "depends_on": ["W1"],
                              "max_retries": 1, "realizable": True, "ok": True, "blocked": False}},
        },
        "artifacts": {},
        "integration": {"ok": False, "status": "部分完成",
                        "summary": "完成 W1；失败 W2（无下游依赖，不阻塞别的范围）。"},
        "criteria": [{"id": "c-editor_assert", "text": "编辑器读数与期望一致",
                      "evidence_kind": "editor_assert", "passed": True, "windows": ["W1"]}],
    }


def _real_or_inline_run() -> tuple[dict, bool]:
    if REAL_RUN.is_file():
        return json.loads(REAL_RUN.read_text(encoding="utf-8")), True
    return _inline_legacy_run(), False


# ---------------------------------------------------------------- 隔离与假件

def _isolate(tmp: Path) -> dict:
    """把 run/事件/归档/快照/台账/交接全部指到临时目录；返回恢复用的原值。"""
    from agent import brief as brief_mod
    from agent import ledger as ledger_mod
    from agent import run_windows as rw
    from agent import snapshot as snapshot_mod

    saved = {
        "runs": rw.RUNS_DIR, "events": rw.EVENTS_DIR, "archive": rw.ARCHIVE_DIR,
        "src": rw.UE_SOURCE_ROOT, "project": rw.UE_PROJECT_DIR,
        "snap_src": snapshot_mod.UE_SOURCE_ROOT, "snaps": snapshot_mod.SNAPSHOTS_DIR,
        "briefs": brief_mod.BRIEFS_DIR, "ledger": ledger_mod.LEDGER_PATH,
    }
    rw.RUNS_DIR, rw.EVENTS_DIR, rw.ARCHIVE_DIR = tmp / "runs", tmp / "events", tmp / "archive"
    rw.UE_SOURCE_ROOT, rw.UE_PROJECT_DIR = tmp / "source", tmp
    snapshot_mod.UE_SOURCE_ROOT, snapshot_mod.SNAPSHOTS_DIR = tmp / "source", tmp / "snapshots"
    brief_mod.BRIEFS_DIR = tmp / "briefs"
    ledger_mod.LEDGER_PATH = tmp / "ledger.json"
    return saved


def _restore(saved: dict) -> None:
    from agent import brief as brief_mod
    from agent import ledger as ledger_mod
    from agent import run_windows as rw
    from agent import snapshot as snapshot_mod

    rw.RUNS_DIR, rw.EVENTS_DIR, rw.ARCHIVE_DIR = saved["runs"], saved["events"], saved["archive"]
    rw.UE_SOURCE_ROOT, rw.UE_PROJECT_DIR = saved["src"], saved["project"]
    snapshot_mod.UE_SOURCE_ROOT, snapshot_mod.SNAPSHOTS_DIR = saved["snap_src"], saved["snaps"]
    brief_mod.BRIEFS_DIR = saved["briefs"]
    ledger_mod.LEDGER_PATH = saved["ledger"]


def _patch_scope_windows(fake):
    """把 T1 的 scope_windows 换成替身（补不出窗的 192858 形状）。返回恢复句柄。"""
    from agent import plan_windows

    had = hasattr(plan_windows, "scope_windows")
    saved = getattr(plan_windows, "scope_windows", None)
    plan_windows.scope_windows = fake
    return saved, had


def _unpatch_scope_windows(saved, had) -> None:
    from agent import plan_windows

    if had:
        plan_windows.scope_windows = saved
        return
    try:
        delattr(plan_windows, "scope_windows")
    except AttributeError:
        pass


def _run(brief: dict, fake, *, windows=None, scopes=None, no_rescue: bool = False,
         resume_state=None):
    """假 execute 真跑一遍（不叫模型、不碰编辑器）。返回 (code, path, run json, windows, scopes, events)。"""
    from agent import run_task as run_task_mod
    from agent import run_windows as rw
    from agent.brief import materialize_scopes

    if windows is None and resume_state is None:
        if no_rescue:
            handle = _patch_scope_windows(lambda scope, query="": [])
            try:
                windows, scopes = materialize_scopes(brief)
            finally:
                _unpatch_scope_windows(*handle)
        else:
            windows, scopes = materialize_scopes(brief)
    events: list[dict] = []
    original = run_task_mod.run_task
    run_task_mod.run_task = fake
    try:
        if resume_state is not None:
            code, path = rw.execute_windows(brief["query"], resume_state=resume_state,
                                            echo=_quiet, emit=events.append)
        else:
            code, path = rw.execute_windows(
                brief["query"], planned={"source": "brief"}, windows=windows, brief=brief,
                scopes=scopes, echo=_quiet, emit=events.append,
            )
    finally:
        run_task_mod.run_task = original
    data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    return code, path, data, windows, scopes, events


def _integrate_view(data: dict) -> dict:
    """集成扇的三态视图：entry / detail / matrix / checked。"""
    wid = next((wid for wid, entry in (data.get("windows") or {}).items()
                if str(entry.get("action")) == "integrate_verify"), "")
    assert wid, ("run 里没有集成扇", list((data.get("windows") or {}).keys()))
    entry = data["windows"][wid]
    detail = entry.get("detail") or {}
    matrix = [item for item in (detail.get("matrix") or []) if isinstance(item, dict)]
    return {
        "id": wid, "entry": entry, "detail": detail, "matrix": matrix,
        "checked": [item for item in matrix if item.get("ok") is not None],
        "label": str(detail.get("summary") or ""),
        "verdict": str(detail.get("verdict") or ""),
        "stop_reason": str(entry.get("stop_reason") or ""),
        "item": next((item for item in matrix if item.get("item") == "交付验收"), {}),
    }


def _fake_judged(fail_once: tuple = (), fail_actions: tuple = ()):
    """所有动作都给一条通过的 editor_assert 判词；fail_once 里的动作第一次失败、第二次成功。"""
    seen: dict[str, int] = {}

    def fake(task, trace_path=None, thread_id=None, on_row=None):
        action = str(task.get("action") or "")
        seen[action] = seen.get(action, 0) + 1
        if action in fail_actions:
            return {"id": task.get("id"), "ok": False, "error": f"{action} 失败",
                    "summary": f"{action} 失败",
                    "test": {"ok": False, "kind": "editor_assert", "summary": "失败", "errors": []},
                    "thread_id": thread_id}
        if action in fail_once and seen[action] == 1:
            return {"id": task.get("id"), "ok": False, "error": "第一次没连上",
                    "summary": "第一次没连上",
                    "test": {"ok": False, "kind": "editor_assert", "summary": "没连上", "errors": []},
                    "thread_id": thread_id}
        return {"id": task.get("id"), "ok": True, "summary": "做完了",
                "judge_kind": "editor_assert",
                "test": {"ok": True, "kind": "editor_assert", "summary": "读数与期望一致", "errors": []},
                "thread_id": thread_id}

    return fake


def _fake_unjudged(task, trace_path=None, thread_id=None, on_row=None):
    """窗口跑完但一条判词都不给（条款留在 passed=False）。"""
    return {"id": task.get("id"), "ok": True, "summary": "做完了，但没跑裁判",
            "test": {"ok": True, "kind": "skip", "summary": "没有可跑的裁判", "errors": []},
            "thread_id": thread_id}


class FakeEditor:
    """假编辑器：只认 create_level 链上的五个原语，别的一律报错，防止偷偷连真的。"""

    def __init__(self, *, exists=False, duplicate_error="", save_error="", load_error="",
                 current_error="", current_level=None):
        self.exists = exists
        self.duplicate_error = duplicate_error
        self.save_error = save_error
        self.load_error = load_error
        self.current_error = current_error
        # current_level 给定时 get_current_level 永远回这个值（模拟载入没生效）
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
        assert found is not None, ("这一步没被调用", tool, self.tools)
        return found


def _with_fake_editor(fake: FakeEditor, step: dict) -> dict:
    import config
    import backends.select as select
    import core.editor_exec as editor_exec
    from agent.sequence import _create_level

    original = editor_exec.exec_tool
    editor_exec.exec_tool = fake
    select.reset_lock()
    previous = config.editor_backend
    config.editor_backend = lambda: "official"
    try:
        return _create_level(step)
    finally:
        config.editor_backend = previous
        select.reset_lock()
        editor_exec.exec_tool = original
        assert editor_exec.exec_tool is original


# ---------------------------------------------------------------- 任务书样板

def _brief_launch_and_level() -> dict:
    """真做成的一整句：A 启动编辑器 + B 新建关卡，无缺口。"""
    return {
        "query": "打开编辑器，新建一个关卡 L_NewTest2",
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器", "deliverables": ["编辑器进程在"],
             "pending_note": "", "depends_on": [], "external_dep": "",
             "windows": [{"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
                          "slots": {}, "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "新建关卡 L_NewTest2",
             "deliverables": ["/Game 下有关卡 L_NewTest2", "该关卡已成为当前关卡"],
             "pending_note": "", "depends_on": ["A"], "external_dep": "",
             "windows": [{"id": "W2", "title": "新建关卡 L_NewTest2", "action": "create_level",
                          "slots": {"level": "L_NewTest2"}, "depends_on": [], "max_retries": 1}]},
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["只读复验"]},
        "external_dependencies": [],
    }


def _brief_gap() -> dict:
    """A 启动 + B 新建关卡 + C 仍补不出窗的范围（巡逻，不是蓝图）。蓝图句不再当成零窗口。"""
    return {
        "query": "打开编辑器，新建一个关卡 L_NewTest2，再做一套巡逻行为树",
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器", "deliverables": ["编辑器进程在"],
             "pending_note": "", "depends_on": [], "external_dep": "",
             "windows": [{"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
                          "slots": {}, "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "新建关卡 L_NewTest2", "deliverables": ["/Game 下有关卡 L_NewTest2"],
             "pending_note": "", "depends_on": ["A"], "external_dep": "",
             "windows": [{"id": "W2", "title": "新建关卡 L_NewTest2", "action": "create_level",
                          "slots": {"level": "L_NewTest2"}, "depends_on": [], "max_retries": 1}]},
            {"id": "C", "title": "做一套巡逻行为树", "deliverables": ["敌人沿路径巡逻"],
             "pending_note": "工具面没有巡逻动作",
             "depends_on": ["B"], "external_dep": "", "windows": []},
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["只读复验"]},
        "external_dependencies": [],
    }


def _brief_no_name() -> dict:
    """原句没给关卡名：补窗必须暂停，不许乱建，也不许静默 0 窗口。"""
    return {
        "query": "打开编辑器，新建一个关卡，然后实现 FPS 射击玩法",
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器", "deliverables": ["编辑器进程在"],
             "pending_note": "", "depends_on": [], "external_dep": "",
             "windows": [{"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
                          "slots": {}, "depends_on": [], "max_retries": 0}]},
            {"id": "B", "title": "新建一个关卡", "deliverables": ["/Game 下有关卡"],
             "pending_note": "", "depends_on": ["A"], "external_dep": "", "windows": []},
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["只读复验"]},
        "external_dependencies": [],
    }


# ================================================================ 规划层

def check_catalog_prompt() -> None:
    """动作表与两处提示词都必须有 create_level：规划者/任务书生成器不知道它，B 段就还会蒸发。"""
    from agent.brief import brief_system_prompt
    from agent.catalog import (
        DIRECT_ACTIONS, EDITOR_LOCK_ACTIONS, action_meta, allows, spec_of, system_prompt,
    )

    spec = spec_of("create_level")
    assert isinstance(spec, dict), spec
    assert spec["slots"] == ("level",), spec
    assert allows("create_level") is True
    assert "create_level" in DIRECT_ACTIONS, "create_level 应进 DIRECT_ACTIONS"
    assert "create_level" in EDITOR_LOCK_ACTIONS, "create_level 应进 EDITOR_LOCK_ACTIONS"
    assert any("当前关卡" in str(item) for item in action_meta("create_level")["verification"])

    prompt = system_prompt()
    assert "create_level" in prompt, prompt[:400]
    assert "新建一个关卡用 create_level" in prompt, "提示词里少了「新建关卡」这句路由指令"
    assert "槽位：level" in prompt, prompt[:400]
    brief_prompt = brief_system_prompt()
    assert "create_level" in brief_prompt, "任务书生成器的动作表里没有 create_level"


def check_plan_windows() -> None:
    """「新建一个关卡，名字为 L_NewTest2」必须拆出 realizable=True 的 create_level 窗口。"""
    from agent.plan_windows import plan_windows

    planned = plan_windows("新建一个关卡，名字为 L_NewTest2", complete=_boom)
    assert planned.get("source") == "rules", planned.get("source")
    windows = planned.get("windows") or []
    assert [window.get("action") for window in windows] == ["create_level"], windows
    window = windows[0]
    assert window.get("realizable") is True, window
    assert window.get("blocked") in (None, False), window
    # 归地路径：窗口自己的 level_path 或 task 里的都算（_prepare_task 的白名单会截掉窗口级字段）
    resolved = window.get("level_path") or (window.get("task") or {}).get("level_path")
    assert resolved == TARGET, window

    # 理解器只拆出别的动作时，代码要把建关卡补在后面（句子级兜底）
    def other_goals(_messages):
        return json.dumps({
            "query": QUERY,
            "goals": [{"action": "launch_editor", "slots": {}},
                      {"action": "place_actor", "slots": {"blueprint": "BP_Coin"}}],
            "criteria": [], "constraints": {"editor": True}, "unresolved": [],
        })

    filled = plan_windows(QUERY, complete=other_goals)
    actions = [window.get("action") for window in filled.get("windows") or []]
    assert "create_level" in actions, ("理解器漏拆时没有补建关卡扇", actions)
    created = [window for window in filled["windows"] if window.get("action") == "create_level"]
    assert len(created) == 1, created
    assert created[0].get("realizable") is True, created
    # 建关卡排在启动之后（launch_editor 先起来，建关卡才有地方落）
    assert actions.index("launch_editor") < actions.index("create_level"), actions
    ids = [str(window.get("id")) for window in filled["windows"]]
    position = ids.index(str(created[0]["id"]))
    assert created[0].get("depends_on") == [ids[position - 1]], (
        "补出来的建关卡扇要接在上一扇后面", created, ids)
    # 幂等：已经有一扇 create_level 就不再补
    from agent.plan_windows import _ensure_requested_create_level

    assert _ensure_requested_create_level(QUERY, filled["windows"]) is filled["windows"]


def check_scope_windows_unrelated() -> None:
    """无关范围必须返回 []：不许把别的范围/别的句子里的「新建关卡」挪进来。"""
    from agent.plan_windows import scope_windows

    assert scope_windows({"id": "A", "title": "移动 Cube 到 x=100"}, "移动 Cube 到 x=100") == []
    assert scope_windows({"id": "A", "title": "移动 Cube 到 x=100",
                          "deliverables": ["Cube 在 x=100"]}, QUERY) == []
    assert scope_windows({"id": "A", "title": "启动 UE 编辑器并等待 MCP 端口连通"}, QUERY) == []
    # 没有分解回调时，不把 FPS 硬收成 compile_blueprint 或 implement_shooter
    fps = scope_windows({
        "id": "C", "title": "在 L_NewTest2 内实现简易 FPS 射击玩法",
        "deliverables": ["玩家角色：移动、视角、开火输入", "射击命中判定与可击倒/可拾取目标"],
        "pending_note": "原句只说“自由发挥”，未点名具体蓝图或资产",
    }, QUERY)
    assert fps == [], fps
    # 「做一个平台跳跃关卡」中间夹了别的词，不算「新建关卡」
    assert scope_windows({"id": "A", "title": "做一个平台跳跃关卡"}, "做一个平台跳跃关卡") == []
    assert scope_windows({}, "") == []
    # 脏输入不许抛异常
    for bad in (None, "不是字典", 42, {"id": None, "title": None, "deliverables": 7}):
        assert isinstance(scope_windows(bad, QUERY), list), bad


def check_scope_windows_no_guess() -> None:
    """名字缺失/多义时补出的是**暂停窗**，不是空列表（空列表会退化成缺口）、更不是猜一个名字。"""
    from agent.plan_windows import scope_windows

    for scope, label in (
        ({"id": "B", "title": "新建一个关卡"}, "缺名"),
        ({"id": "B", "title": "新建一个关卡 L_A 和 L_B"}, "多义"),
    ):
        made = scope_windows(scope, "")
        assert made, (label, "补窗不许静默返回空列表")
        window = made[0]
        assert window.get("action") == "create_level", (label, window)
        assert window.get("realizable") is False, (label, "名字不唯一/缺失必须暂停", window)
        assert window.get("blocked") is True, (label, window)
        assert "不猜" in str(window.get("error") or ""), (label, window)
        assert not window.get("level_path"), (label, "不许猜一个关卡路径出来", window)
        # id 契约：S<范围 id>W<序号>
        assert window.get("id") == "SBW1", (label, window)
        for dep in window.get("depends_on") or []:
            assert dep in [item["id"] for item in made], (label, dep)


# ================================================================ 断言 a

def check_brief_gate_wording() -> None:
    """人审门必须把零窗口范围写成「能力缺口」：旧任务书原文里的「只记录待办」要被换掉。"""
    from agent.brief import (
        _zero_window_scope, capability_gap_note, render_brief_md, validate_brief,
    )

    raw, is_real = _real_brief()
    brief = validate_brief(json.loads(json.dumps(raw, ensure_ascii=False)), raw["query"])
    for view, label in ((brief, "validate 后"), (raw, "旧任务书原文")):
        md = render_brief_md(view)
        assert "能力缺口" in md, (label, md)
        assert "只记录待办" not in md, (label, "旧文案还在放行", md)
        for sid in ("B", "C"):
            scope = next(item for item in view["scopes"] if item["id"] == sid)
            assert _zero_window_scope(scope) is True, (label, sid, scope)
            assert "能力缺口" in capability_gap_note(scope), (label, sid)
    if not is_real:
        print("  人审门用内联替身验的（data/briefs 不在）")


def check_materialize_no_silent_zero_window() -> None:
    """断言 a：B 段必须出现 create_level 窗口；C 段必须是结构化缺口或可用窗口；不许静默。"""
    from agent.brief import materialize_scopes, validate_brief

    raw, is_real = _real_brief()
    brief = validate_brief(json.loads(json.dumps(raw, ensure_ascii=False)), raw["query"])
    windows, scopes = materialize_scopes(brief)
    by_scope = {str(item.get("id")): item for item in scopes}
    by_action = {}
    for window in windows:
        by_action.setdefault(str(window.get("action")), []).append(window)

    # B 段：create_level 窗口，realizable，路径归到 /Game/Maps/L_NewTest2，且排在启动之后
    assert len(by_action.get("create_level") or []) == 1, (
        "B 段（新建关卡 L_NewTest2）没有出现 create_level 窗口", [w.get("action") for w in windows])
    created = by_action["create_level"][0]
    assert created.get("realizable") is True and created.get("blocked") in (None, False), created
    assert created.get("level_path") == TARGET, created
    assert created.get("goal") is True, ("补出来的窗口要标 goal", created)
    assert created.get("depends_on") == [by_action["launch_editor"][0]["id"]], (
        "建关卡必须等启动编辑器那一扇", created)
    assert by_scope["B"]["window_ids"] == [created["id"]], by_scope["B"]
    assert not by_scope["B"].get("capability_gap"), ("B 已经有窗口了，不该再记缺口", by_scope["B"])

    # C 段：补不出窗口 ⇒ 结构化 capability_gap；补得出 ⇒ 有窗口。两种都行，就是不许静默
    scope_c = by_scope["C"]
    gap = scope_c.get("capability_gap")
    assert scope_c["window_ids"] or (isinstance(gap, dict) and gap), (
        "范围 C 既没补出窗口也没记能力缺口：又静默了", scope_c)
    if gap:
        assert "能力缺口" in str(gap.get("reason") or ""), gap
        assert gap.get("deliverables"), ("缺口要带交付清单", gap)

    # 任何非集成、非暂停、不等外部的范围：窗口或缺口，二者必居其一
    for scope in scopes:
        if scope.get("kind") == "integration" or str(scope.get("id")) == "INT":
            continue
        if scope.get("blocked") or str(scope.get("external_dep") or "").strip():
            continue
        assert scope.get("window_ids") or isinstance(scope.get("capability_gap"), dict), (
            f"范围 {scope.get('id')} 既没窗口也没缺口", scope)

    # 集成扇必须在最后，且等所有窗口
    assert windows[-1]["action"] == "integrate_verify", windows[-1]
    assert windows[-1]["depends_on"] == [window["id"] for window in windows[:-1]], windows[-1]
    return is_real


# ================================================================ 断言 b

def check_gap_run_is_not_done() -> None:
    """断言 b：192858 形状（补不出窗 ⇒ B/C 都是缺口）⇒ 综合状态「未完成」，不是「部分完成」。"""
    from agent.brief import validate_brief

    raw, _is_real = _real_brief()
    brief = validate_brief(json.loads(json.dumps(raw, ensure_ascii=False)), raw["query"])
    code, path, data, windows, _scopes, events = _run(
        brief, _fake_judged(), no_rescue=True)
    assert code == 1, ("有整段没交付的 run 不能返回 0", code)
    assert data["status"] == "未完成", (data["status"], "不是「部分完成」，更不能是「成功」")
    integrated = data["integration"]
    assert integrated["ok"] is False and integrated["status"] == "未完成", integrated
    note = str(integrated.get("summary") or "")
    assert "能力缺口" in note, note
    for sid in ("B", "C"):
        assert sid in note, (sid, "收尾说明必须点名缺口范围", note)
    text = path.read_text(encoding="utf-8")
    assert "- 状态：未完成" in text, text
    assert "未完成（能力缺口）" in text, text

    view = _integrate_view(data)
    assert view["entry"]["state"] == "failed" and view["entry"]["ok"] is False, view["entry"]
    assert view["stop_reason"] == "no_evidence", view["stop_reason"]
    assert view["verdict"] == "no_evidence", view["verdict"]
    # 本次确实一件都没验过：矩阵里不许有任何一项被记成「通过」
    assert not any(item.get("ok") is True for item in view["matrix"]), view["matrix"]
    assert view["item"].get("ok") is not True, ("交付验收不许在有缺口时判通过", view["item"])
    assert "能力缺口" in view["label"], view["label"]
    run_end = [event for event in events if event.get("type") == "run_end"]
    assert run_end and run_end[-1].get("stop_reason") == "no_evidence", run_end[-1:]
    assert run_end[-1].get("status") == "未完成", run_end[-1]


def check_real_brief_run_is_not_done() -> None:
    """没有分解回调时，C 保持能力缺口，不再假收成 compile_blueprint。"""
    from agent.brief import validate_brief

    raw, _is_real = _real_brief()
    brief = validate_brief(json.loads(json.dumps(raw, ensure_ascii=False)), raw["query"])
    code, _path, data, windows, _scopes, _events = _run(brief, _fake_judged())

    actions = [(wid, entry["action"], entry["state"]) for wid, entry in data["windows"].items()]
    kinds = [action for _wid, action, _state in actions]
    assert kinds[0] == "launch_editor" and "create_level" in kinds, actions
    assert "compile_blueprint" not in kinds, actions
    assert "implement_shooter" not in kinds, actions
    scopes = {str(scope.get("id")): scope for scope in data.get("scopes") or []}
    assert scopes["B"]["window_ids"] and not scopes["B"].get("capability_gap"), scopes["B"]
    assert isinstance(scopes["C"].get("capability_gap"), dict), scopes["C"]
    assert code == 1, code
    assert data["status"] != "成功", data["status"]


# ================================================================ 断言 c / d / 三态

def check_three_state_exclusion() -> None:
    """断言 c + d + 三态互斥：no_evidence / 矩阵未全过 / 矩阵全部通过 / 全不适用 不许互相冒充。

    四种结局各跑一条真 run（假 run_task）：
      1 缺口（补不出窗）            ⇒ no_evidence，label 点名能力缺口；
      2 无缺口但条款真失败          ⇒ 矩阵未全过，verdict 空，停因 judge_failed；
      3 无缺口且条款全过            ⇒ 矩阵全部通过，状态「成功」，退出码 0；
      4 无缺口且一条条款都没有      ⇒ 矩阵没有可验证的项，不许撑成功。
    """
    from agent.plan_windows import materialize_goal

    # ---- 1 缺口
    _code, _path, gap_data, _w, _s, _e = _run(_brief_gap(), _fake_judged())
    gap = _integrate_view(gap_data)

    # ---- 2 条款真失败（窗口 done，但判词没过）
    _code, _path, bad_data, _w, _s, _e = _run(_brief_launch_and_level(), _fake_unjudged)
    bad = _integrate_view(bad_data)

    # ---- 3 条款全过
    ok_code, _path, ok_data, _w, _s, _e = _run(_brief_launch_and_level(), _fake_judged())
    good = _integrate_view(ok_data)

    # ---- 4 一条条款都没有且无缺口（只有集成扇）
    integrate_only = materialize_goal("integrate_verify", {}, title="累计集成、验证与交付",
                                     wid="W1", depends=[], retries=1)[0]
    empty_brief = {"query": "只跑集成扇", "scopes": [], "integration": {},
                   "external_dependencies": []}
    _code, _path, empty_data, _w, _s, _e = _run(
        empty_brief, _fake_unjudged, windows=[integrate_only], scopes=[])
    empty = _integrate_view(empty_data)

    # 三态各自成立
    assert gap["verdict"] == "no_evidence" and gap["stop_reason"] == "no_evidence", gap
    assert "能力缺口" in gap["label"], gap["label"]
    assert gap["entry"]["ok"] is False, gap["entry"]
    assert gap_data["status"] == "未完成", gap_data["status"]

    assert bad["verdict"] != "no_evidence", ("条款真失败不是 no_evidence", bad["verdict"])
    assert "矩阵未全过" in bad["label"], bad["label"]
    assert bad["entry"]["ok"] is False and bad["item"].get("ok") is False, bad["item"]
    assert bad["checked"], bad["matrix"]
    assert bad_data["status"] != "成功", bad_data["status"]

    assert good["verdict"] == "" and good["stop_reason"] == "ok", good
    assert "矩阵全部通过" in good["label"], good["label"]
    assert good["entry"]["ok"] is True and good["entry"]["state"] == "done", good["entry"]
    assert good["item"].get("ok") is True, good["item"]
    assert ok_code == 0 and ok_data["status"] == "成功", (ok_code, ok_data["status"])

    assert empty["verdict"] != "no_evidence", empty
    assert empty["checked"] == [], ("没有条款时不许有 checked 项", empty["matrix"])
    assert "矩阵全部通过" not in empty["label"], ("一条条款都没有却撑成功", empty["label"])
    assert "没有可验证的项" in empty["label"], empty["label"]
    assert empty["item"].get("ok") is not True, empty["item"]
    assert empty["entry"]["ok"] is False, empty["entry"]
    assert empty_data["status"] != "成功", empty_data["status"]

    # 互斥：三种「标签」只许出现在自己那种结局里；「矩阵未全过」可以和缺口同时出现（那是对的）
    exclusive = {
        "能力缺口": gap["label"],
        "矩阵全部通过": good["label"],
        "没有可验证的项": empty["label"],
    }
    all_labels = {"缺口": gap["label"], "条款真失败": bad["label"],
                  "全过": good["label"], "一条条款都没有": empty["label"]}
    for tag, own in exclusive.items():
        assert tag in own, (tag, own)
        for name, label in all_labels.items():
            if label is own:
                continue
            assert tag not in label, (tag, "冒充了别的结局", name, label)
    assert "矩阵未全过" not in gap["label"] or "能力缺口" in gap["label"], gap["label"]

    # 缺口 vs 条款真失败：verdict 与停因必须能分开（这就是「第三态」的意义）
    assert (gap["verdict"], gap["stop_reason"]) != (bad["verdict"], bad["stop_reason"]), (
        gap["verdict"], bad["verdict"])


def check_known_defect_gap_with_delayed_item() -> None:
    """**已知缺陷（T3 报给 lead / task-9）**：缺口 + 一次成功重试 ⇒ 现在会假报「矩阵全部通过」。

    根因：`_run_integrate` 的 ok/label 只看 checked（ok is not None 的矩阵项）是否全 True；
    「交付验收」在有缺口时被强制 ok=None（不投票），而「延迟窗口」只要窗口有终态就给 True。
    于是 checked=[延迟窗口 True] ⇒ label「矩阵全部通过」+ 集成扇 ok=True + stop_reason=ok，
    即使范围 C 整段没交付。run 级 status/退出码仍是对的（未完成 / 1）。
    修好后这条自动转绿（T9 口径：gaps 非空 ⇒ ok=False 且 verdict=no_evidence）。"""
    _code, _path, data, _w, _s, _e = _run(
        _brief_gap(), _fake_judged(fail_once=("launch_editor",)))
    view = _integrate_view(data)
    delayed = next((item for item in view["matrix"] if item.get("item") == "延迟窗口"), {})
    assert delayed.get("ok") is True, ("这条用例的前提是「延迟窗口」真的判过", view["matrix"])
    assert data["status"] == "未完成", data["status"]
    assert "矩阵全部通过" not in view["label"], (
        "有范围整段没交付，矩阵不许报「矩阵全部通过」", view["label"])
    assert view["entry"]["ok"] is False, ("有缺口时集成扇不许 ok=True", view["entry"])
    assert view["verdict"] == "no_evidence", view["verdict"]
    assert view["stop_reason"] != "ok", view["stop_reason"]


def check_known_defect_paused_window() -> None:
    """**已知缺陷（T3 报给 lead / task-9）**：暂停扇会让集成扇假报全过，且收尾 note 不点名它。

    构造：原句没给关卡名 ⇒ 补出的是暂停窗（不猜，行为正确）；但它让「拒答路径」=True，
    集成扇于是报「矩阵全部通过」；收尾 note 却是「完成 W1、W3」，一个字都没提暂停的 W2。
    修好后这条自动转绿（T9 口径：有 paused 扇 ⇒ ok=False 点名；note 有 open_scopes 就点名）。"""
    from agent.brief import materialize_scopes

    brief = _brief_no_name()
    windows, scopes = materialize_scopes(brief)
    by_action = {str(window.get("action")): window for window in windows}
    assert by_action.get("create_level", {}).get("realizable") is False, (
        "缺关卡名时必须补出暂停窗，不许乱建", windows)

    called: list[str] = []

    def fake(task, trace_path=None, thread_id=None, on_row=None):
        called.append(str(task.get("action") or ""))
        return _fake_judged()(task, trace_path, thread_id, on_row)

    code, _path, data, _w, _s, _e = _run(brief, fake, windows=windows, scopes=scopes)
    assert "create_level" not in called, ("名字缺失时不许真去建关卡", called)
    paused = [wid for wid, entry in data["windows"].items() if entry["state"] == "paused"]
    assert paused, ("缺关卡名的那一扇应当停在暂停", data["windows"])
    assert code == 1 and data["status"] != "成功", (code, data["status"])

    note = str(data["integration"].get("summary") or "")
    assert any(token in note for token in ("W2", "范围 B", "B（", "暂停")), (
        "收尾说明必须点名暂停的那一扇/那个范围", note)
    view = _integrate_view(data)
    assert "矩阵全部通过" not in view["label"], (
        "有窗口停在暂停，矩阵不许报「矩阵全部通过」", view["label"])
    assert view["entry"]["ok"] is False, ("有暂停扇时集成扇不许 ok=True", view["entry"])


def check_known_defect_stuck_window_passes() -> None:
    """**已知缺陷 A（T3 复验第二轮发现）**：矩阵第 5 项「未知态恢复」把语义反了。

    HEAD 里是 `"ok": not stuck`（有窗口卡住 ⇒ False，那是这一项在查的东西）；
    现在写成 `"ok": True if stuck else None`：**没有任何窗口卡住** 时才给 None（不适用，对），
    **有窗口卡在 running/retry/revising/interrupted** 时反而给 True ⇒ 它会占一个 checked 名额
    去撑「矩阵全部通过」。最小复现是纯单测（不跑 run，不碰编辑器）。"""
    from agent import run_windows as rw

    state = rw._RunState("20260909-160000", "中断形态", "brief", [])
    state.data["windows"] = {
        "W1": {"id": "W1", "action": "launch_editor", "state": "done", "ok": True,
               "result": {"ok": True, "summary": "ok"}, "window": {}, "detail": {}},
        "W2": {"id": "W2", "action": "create_level", "state": "running", "ok": None,
               "result": None, "window": {}},
        "W3": {"id": "W3", "action": "integrate_verify", "state": "running", "ok": None,
               "result": None, "window": {}},
    }
    state.data["criteria"] = [{"id": "c-editor_assert", "evidence_kind": "editor_assert",
                               "passed": True, "windows": ["W1"]}]
    out = rw._run_integrate(state.data["windows"]["W3"], state, "q", {},
                            rw._EventBus("20260909-160000"))
    matrix = [item for item in (out.get("matrix") or []) if isinstance(item, dict)]
    stuck = next((item for item in matrix if item.get("item") == "未知态恢复"), {})
    assert "W2=running" in str(stuck.get("note") or ""), (
        "前提：这一项真的看见了卡住的窗口", stuck)
    assert stuck.get("ok") is False, (
        "「未知态恢复」在真卡住时必须是 False（有窗口没有明确终态），"
        "现在是 True ⇒ 会去撑「矩阵全部通过」", stuck)
    # 这一项一旦如实记 False，它就进 checked 并把 all(checked) 拉成 False ⇒ 聚合层不会再落到
    # 「全过」分支（见 _run_integrate: checked / matrix_ok / label 三处）。这里一并钉住。
    assert "矩阵全部通过" not in str(out.get("summary") or ""), (
        "有窗口悬在半路，矩阵不许报「矩阵全部通过」", out.get("summary"))
    assert out.get("ok") is False, ("有窗口悬在半路，集成扇不许 ok=True", out.get("ok"))


def check_known_defect_external_scope_wording() -> None:
    """**已知缺陷 B（T3 复验第二轮发现）**：等外部的零窗口范围被说成「只记待办」，
    并且等外部的 run 会同时出现「矩阵全部通过」与「有范围没完成」。

    `agent/harness.py` 的 scope_marks 里 `elif not window_ids:` 排在
    `elif scope.get("external_dep"):` 之前，于是「零窗口 + 等外部」落到「只记录待办」那句
    本轮要根除的话术（它有窗口时才能正确说出「等待外部 M-GO」）。
    注意：这条**不是**在要求「等外部也算失败」——run 级 部分完成 / exit 1 是对的；
    要的是「话术别错 + label 别在同一份记录里跟 note 打架」。"""
    from agent.brief import materialize_scopes
    from agent.harness import scope_marks

    # (1) 判定口径：零窗口 + 等外部 不许说成「只记待办」
    marks = scope_marks([{
        "id": "C", "window_ids": [], "external_dep": "M-GO", "criteria": [],
        "deliverables": ["接口恢复后核对"], "window_states": [],
    }])
    reason = str((marks[0] if marks else {}).get("reason") or "")
    assert "等待外部" in reason, (
        "零窗口 + 等外部的范围应当说「等待外部 M-GO」，现在是", reason)
    assert "只记待办" not in reason, ("等外部不是「只记待办」", reason)

    # (2) 一份记录里的话术不许自相矛盾
    brief = {
        "query": "打开编辑器，新建一个关卡 L_NewTest2，再等 M-GO 接口",
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器", "deliverables": ["编辑器进程在"],
             "pending_note": "", "depends_on": [], "external_dep": "",
             "windows": [{"id": "W1", "title": "启动", "action": "launch_editor", "slots": {},
                          "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "新建关卡 L_NewTest2", "deliverables": ["关卡在"],
             "pending_note": "", "depends_on": ["A"], "external_dep": "",
             "windows": [{"id": "W2", "title": "建关卡", "action": "create_level",
                          "slots": {"level": "L_NewTest2"}, "depends_on": [], "max_retries": 1}]},
            {"id": "C", "title": "等 M-GO 接口", "deliverables": ["接口恢复后核对"],
             "pending_note": "", "depends_on": ["B"], "external_dep": "M-GO", "windows": []},
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["只读复验"]},
        "external_dependencies": [{"name": "M-GO", "provides": "接口", "status": "未恢复"}],
    }
    windows, scopes = materialize_scopes(brief)
    assert any(str(scope.get("external_dep") or "") for scope in scopes), (
        "前提：任务书里有一个等外部的范围", scopes)
    code, _path, data, _w, _s, _e = _run(brief, _fake_judged(), windows=windows, scopes=scopes)
    note = str(data["integration"].get("summary") or "")
    view = _integrate_view(data)
    # 等外部不该被 veto 成失败：run 仍是「部分完成」+ exit 1，这是既有正确口径
    assert code == 1 and data["status"] == "部分完成", (code, data["status"])
    assert "有范围没完成" in note or "等待外部" in note, ("收尾要点名等外部/未完成的范围", note)
    # 话术护栏：同一份记录里说「有范围没完成」，集成 label 就不许把结论写成「全过」。
    # 允许「矩阵全部通过（另有范围未完成：C）」这类补充写法，不许只写「矩阵全部通过」。
    label = view["label"]
    if "矩阵全部通过" in label:
        assert "另有范围" in label or "未完成" in label or "等外部" in label, (
            "收尾 note 说「有范围没完成」，集成 label 却不加限定地说「矩阵全部通过」", label)


# ================================================================ 对抗：create_level 执行链

def check_create_level_idempotent() -> None:
    """幂等：目标关卡已存在时不重复 duplicate；duplicate 报「已存在」也算幂等命中。"""
    step = {"id": "W2", "action": "create_level", "slots": {"level": "L_NewTest2"}}

    same = FakeEditor(exists=True)
    done = _with_fake_editor(same, dict(step))
    assert done.get("ok") is True, done
    assert "duplicate" not in same.tools, ("已存在却还去复制了", same.tools)
    assert same.tools == ["exists", "save_assets", "load_level", "get_current_level"], same.tools
    assert "已存在，不重复新建" in str(done.get("index_note")), done

    raced = FakeEditor(exists=False, duplicate_error="Asset already exists at path")
    racy = _with_fake_editor(raced, dict(step))
    assert racy.get("ok") is True, ("复制报「已存在」应当按幂等命中，不报失败", racy)
    assert "已存在，不重复新建" in str(racy.get("index_note")), racy
    assert raced.tools == ["exists", "duplicate", "save_assets", "load_level",
                           "get_current_level"], raced.tools


def check_create_level_failures() -> None:
    """失败路径：duplicate / save_assets / load_level / get_current_level / 复查不符
    都必须 ok=False 且 error 指到具体步骤，不许假成功。"""
    step = {"id": "W2", "action": "create_level", "slots": {"level": "L_NewTest2"}}
    cases = [
        (FakeEditor(exists=False, duplicate_error="拒绝连接"), "复制关卡", "duplicate"),
        (FakeEditor(exists=True, save_error="write refused"), "落盘失败", "save_assets"),
        (FakeEditor(exists=True, load_error="关卡打不开"), "加载失败", "load_level"),
        (FakeEditor(exists=True, current_error="读不出"), "复查当前关卡失败", "get_current_level"),
        # 复查读到的不是目标关卡：L_NewTest20 不许被 L_NewTest2 顶掉（词边界）
        (FakeEditor(exists=True, current_level="/Game/Maps/L_NewTest20"), "不是", "get_current_level"),
        (FakeEditor(exists=True, current_level="/Game/Maps/L_NewTest"), "不是", "get_current_level"),
    ]
    for fake, needle, last in cases:
        out = _with_fake_editor(fake, dict(step))
        assert out.get("ok") is False, (needle, out)
        assert needle in str(out.get("error")), (needle, out.get("error"))
        assert fake.tools[-1] == last, (needle, fake.tools)


def check_create_level_no_guess() -> None:
    """不猜：名字为空/多义时一个工具都不调，直接报不猜。"""
    for bad in ("", "L_A 和 L_B"):
        fake = FakeEditor(exists=True)
        out = _with_fake_editor(fake, {"id": "W2", "action": "create_level",
                                      "slots": {"level": bad}})
        assert out.get("ok") is False, (bad, out)
        assert "不猜" in str(out.get("error")), (bad, out)
        assert fake.tools == [], ("名字不唯一/缺失时不许碰编辑器", bad, fake.tools)


def check_create_level_write_authorization() -> None:
    """写授权：duplicate / save_assets / load_level 必须自带 allow_writes，只读原语不许带。"""
    from config import default_save

    fake = FakeEditor(exists=False)
    out = _with_fake_editor(fake, {"id": "W2", "action": "create_level",
                                  "slots": {"level": "L_NewTest2"}})
    assert out.get("ok") is True, out
    for tool, allowed in (("duplicate", "duplicate"), ("save_assets", "save_assets"),
                          ("load_level", "load_level")):
        task = fake.call(tool)["task"]
        assert allowed in (task.get("allow_writes") or []), (tool, task)
        if default_save():
            assert task.get("save") is True, (tool, "落盘是这一步的交付，save 必须是 True", task)
    for tool in ("exists", "get_current_level"):
        assert (fake.call(tool)["task"].get("allow_writes") or []) == [], (
            tool, "只读原语不许带写授权", fake.call(tool)["task"])
    assert fake.call("duplicate")["args"] == {
        "path": "/Game/Maps/L_NewTest", "new_path": TARGET}, fake.call("duplicate")["args"]


def check_window_to_judge_chain() -> None:
    """整链：任务书 → materialize → 窗口 → _prepare_task → sequence._create_level → 判词 → 条款 passed。

    T1/T2/T6 各自只验一段；这里把它们串起来，钉住「真建成了关卡，条款就该真的过」。"""
    from agent.brief import materialize_scopes, validate_brief
    from agent.harness import apply_judge
    from agent.run_windows import _prepare_task, _stamp_judge
    from agent.sequence import _create_level

    raw, _is_real = _real_brief()
    brief = validate_brief(json.loads(json.dumps(raw, ensure_ascii=False)), raw["query"])
    windows, scopes = materialize_scopes(brief)
    created = next(window for window in windows if window.get("action") == "create_level")
    task = _prepare_task(created, brief["query"], {}, run_id="check-192858")
    assert task.get("action") == "create_level", task
    assert task.get("slots") == {"level": "L_NewTest2"}, task
    # 字段白名单会截掉 level_path：执行分支必须靠 slots 自己解析（陷阱钉死）
    assert not task.get("level_path"), task

    fake = FakeEditor(exists=False)
    out = _with_fake_editor(fake, task)
    assert out.get("ok") is True, out
    assert fake.tools == ["exists", "duplicate", "save_assets", "load_level",
                          "get_current_level"], fake.tools
    assert out.get("judge_kind") == "editor_assert" and out["test"]["ok"] is True, out
    assert out.get("level_path") == TARGET, out

    scope_b = next(scope for scope in scopes if str(scope.get("id")) == "B")
    rows = [dict(row) for row in (scope_b.get("criteria") or [])]
    assert rows, ("B 段没有条款，建成了也不会被验", scope_b)
    stamped = apply_judge(rows, _stamp_judge(out.get("test"), str(created["id"])))
    assert [row.get("passed") for row in stamped] == [True], (
        "真建成关卡后，B 段的条款必须能被这一扇的判词勾上", stamped)


def check_binding_parity() -> None:
    """T8 的两侧对齐守卫（保留在复验清单里）：producer 没声明判词 ⇒ 表里不许先有映射。

    方向是「证据源先于声明点」：先加映射会造出一条永远 passed=False 的条款，
    把「没判过」说成「做坏了」。"""
    import inspect

    from agent import sequence
    from agent.harness import _KIND_BY_ACTION, _window_ids_for, build_criteria

    def _declares_judge(func) -> bool | None:
        try:
            return "judge_kind" in inspect.getsource(func)
        except (OSError, TypeError):
            return None

    for action, producer in (("create_widget_blueprint", sequence._create_widget_blueprint),
                             ("bind_blueprint", sequence._run_action)):
        declared = _declares_judge(producer)
        if declared is None:
            continue  # 读不到 producer 源码（冻结/exec 部署）：不猜，别制造假红
        rows = build_criteria(windows=[{"id": "W1", "action": action, "slots": {}}])
        if declared:
            assert action in _KIND_BY_ACTION, (
                f"{action} 已经会报判词，_KIND_BY_ACTION 该补映射", action)
        else:
            assert action not in _KIND_BY_ACTION, (
                f"{action} 还没在 sequence 里声明判词，先加映射会造出永远过不了的条款", action)
            assert rows == [], (action, rows)

    # create_level 反过来：producer 声明了判词，映射必须在，条款必须绑得上
    assert _declares_judge(sequence._create_level) is True
    assert _KIND_BY_ACTION.get("create_level") == "editor_assert", _KIND_BY_ACTION.get("create_level")
    rows = build_criteria(windows=[{"id": "W2", "action": "create_level",
                                    "slots": {"level": "L_NewTest2"}}])
    assert rows and rows[0]["windows"] == ["W2"], rows
    assert _window_ids_for("editor_assert", [{"id": "W2", "action": "create_level"}]) == ["W2"]
    # 认不出的动作仍不产条款、不绑定（别把默认值放宽成「认不出也绑」）
    other = {"id": "W9", "action": "no_such_action", "slots": {}}
    assert build_criteria(windows=[other]) == []
    assert _window_ids_for("editor_assert", [other]) == []


# ================================================================ 兼容 / 归档 / 历史坑

def check_legacy_compat() -> None:
    """老记录（没有 capability_gap、没有 verdict、criteria 缺失）读回与续跑都不炸。

    兼容取舍（刻意保留，不报为缺陷）：老记录的范围没有 capability_gap 字段，harness 侧
    scope_marks 只认显式字段（check_harness.py:731-737 钉住了老口径），所以续跑时状态词仍是
    「部分完成」；而 run_windows 侧按结构认缺口，集成 verdict 是 no_evidence。两边口径不同，
    这里只钉「不炸 + 缺口被点名 + 归档一致」。"""
    from agent.harness import contract_status, scope_marks
    from agent.run_windows import _RunState, _capability_gap_scopes

    raw, is_real = _real_or_inline_run()
    state = _RunState.from_resume(raw)
    marks = scope_marks(state.data.get("scopes"))
    assert [mark["id"] for mark in marks] == [str(scope.get("id")) for scope in raw["scopes"]], marks
    gaps = [str(scope.get("id")) for scope in _capability_gap_scopes(state)]
    assert gaps == ["B", "C"], ("老记录的结构性零窗口要按缺口认", gaps)
    # 缺字段不炸：criteria / capability_gap / window_ids / verdict 全缺
    assert contract_status(None, "成功", [{"id": "B", "window_ids": [], "criteria": []}]) == "部分完成"
    assert contract_status(None, "成功", None) == "成功"
    assert contract_status(None, "成功", [{"id": "X"}]) in ("成功", "部分完成")

    code, path, data, _w, _s, events = _run(raw, _fake_judged(), resume_state=raw)
    assert code == 1, code
    assert data["status"] in ("部分完成", "未完成"), data["status"]
    note = str(data["integration"].get("summary") or "")
    # 老记录的范围没有 capability_gap 字段，harness 侧保留老口径（「没有可执行的窗口，只记待办」），
    # 所以这里只要求「点名 + 说清没交付」，不要求新文案「能力缺口」。
    assert "能力缺口" in note or "没有可执行的窗口" in note, ("老记录的缺口也要点名", note)
    for sid in gaps:
        assert sid in note, (sid, "老记录的缺口范围必须被点名", note)
    view = _integrate_view(data)
    assert view["verdict"] == "no_evidence", view["verdict"]
    # 归档件与 runs 最终态一致
    from agent import run_windows as rw

    archived = json.loads((rw.ARCHIVE_DIR / path.stem / f"{path.stem}.json").read_text(encoding="utf-8"))
    assert archived["status"] == data["status"], (archived["status"], data["status"])
    assert archived["status"] != "running", archived["status"]
    deliv = json.loads((rw.ARCHIVE_DIR / path.stem / "deliverables.json").read_text(encoding="utf-8"))
    assert len(deliv.get("matrix") or []) == len(view["matrix"]), ("归档丢了矩阵", deliv)
    assert deliv.get("written_by") == "finalize", deliv
    if is_real:
        print("  老记录用仓库里真实那份 data/runs/20261024/20261024-192858.json（只读）")


def check_archive_fidelity(tmp: Path) -> None:
    """归档保真：二次 finalize 归档不许把 matrix/hashes 清空；run json 的 status 与 runs 一致。"""
    from agent import run_windows as rw

    state = rw._RunState("20260909-120000", "归档保真", "brief", [{
        "id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
        "depends_on": [], "max_retries": 1,
    }])
    matrix = [{"item": "交付验收", "ok": None, "note": "有范围没交付，交付验收不适用（见缺口）：C"}]
    hashes = [{"path": "Source/AIAgentProject/TestPawn.cpp", "sha256": "cd" * 32, "note": "已修改"}]
    folder = rw._archive_run(state, hashes=hashes, matrix=matrix, written_by="integrate")
    first = json.loads((folder / "deliverables.json").read_text(encoding="utf-8"))
    assert first["matrix"] == matrix and first["hashes"] == hashes, first
    assert first["written_by"] == "integrate", first

    md = tmp / f"{state.run_id}.md"
    md.write_text("# 归档保真\n\n## 集成\n- 状态：未完成\n", encoding="utf-8")
    rw._archive_run(state, md_path=md, written_by="finalize")
    second = json.loads((folder / "deliverables.json").read_text(encoding="utf-8"))
    assert second["matrix"] == matrix, ("收尾归档把矩阵清了", second)
    assert second["hashes"] == hashes, ("收尾归档把哈希清了", second)
    assert second["written_by"] == "finalize", second
    assert (folder / md.name).is_file(), "最终 md 仍要进归档"

    # 显式传空是「明确要给空」，不被合并逻辑吃掉
    rw._archive_run(state, hashes=[], matrix=[], written_by="finalize")
    third = json.loads((folder / "deliverables.json").read_text(encoding="utf-8"))
    assert third["matrix"] == [] and third["hashes"] == [], ("显式给空被合并逻辑吃掉了", third)

    # 老归档件（没有 written_by）读回不炸、证据留着
    (folder / "deliverables.json").write_text(
        json.dumps({"run_id": state.run_id, "hashes": hashes, "matrix": matrix},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    rw._archive_run(state, md_path=md, written_by="finalize")
    fourth = json.loads((folder / "deliverables.json").read_text(encoding="utf-8"))
    assert fourth["matrix"] == matrix and fourth["hashes"] == hashes, fourth

    # 端到端：正常 run 的归档件 matrix == run json 集成扇 matrix，且状态一致
    _code, path, data, _w, _s, _e = _run(_brief_launch_and_level(), _fake_judged())
    run_id = path.stem
    archived = json.loads((rw.ARCHIVE_DIR / run_id / f"{run_id}.json").read_text(encoding="utf-8"))
    assert archived["status"] == data["status"], (archived["status"], data["status"])
    view = _integrate_view(data)
    deliv = json.loads((rw.ARCHIVE_DIR / run_id / "deliverables.json").read_text(encoding="utf-8"))
    assert [item.get("item") for item in deliv.get("matrix") or []] == [
        item.get("item") for item in view["matrix"]], ("端到端归档丢了矩阵", deliv)
    assert deliv.get("written_by") == "finalize", deliv
    assert (rw.ARCHIVE_DIR / run_id / f"{run_id}.md").is_file(), "最终 md 要进归档"


def check_historical_guards() -> None:
    """两条历史坑必须继续成立：
    1) 矩阵项 ok=None 是「不适用」，不算通过也不撑成功（20261021-195220 五项空转全绿）；
    2) eval/check_harness.py 里「只有不适用项的矩阵不能是「全过」」那条断言还在。"""
    _code, _path, data, _w, _s, _e = _run(
        _brief_gap(), _fake_judged(fail_once=("launch_editor",)))
    view = _integrate_view(data)
    assert any(item.get("ok") is None for item in view["matrix"]), (
        "本次没发生的项要记不适用", view["matrix"])
    assert "项不适用" in view["label"], view["label"]
    assert view["item"].get("ok") is not True, view["item"]
    # 只有不适用项的矩阵永远不是「全过」：run 里集成扇不能因为「没东西可验」而 ok
    empty_only = [item for item in view["matrix"] if item.get("ok") is not None]
    assert empty_only, view["matrix"]
    _c, _p, hollow_data, _w, _s, _e = _run(_brief_launch_and_level(), _fake_unjudged)
    hollow = _integrate_view(hollow_data)
    assert hollow["entry"]["ok"] is False, ("只有不适用/未过项的矩阵不能是「全过」", hollow["entry"])

    harness_src = (REPO / "eval" / "check_harness.py").read_text(encoding="utf-8")
    assert "只有不适用项的矩阵不能是「全过」" in harness_src, (
        "check_harness.py 里那条老断言被删了/改写了")
    assert "没有可执行的窗口" in harness_src, "check_harness.py 的零窗口老用例被删了"


def check_real_archive_untouched(before: bytes | None) -> None:
    """红线：data/archive/20261024-192858/ 下既有文件是历史证据，一个字节都不许动。"""
    if before is None:
        return
    assert REAL_ARCHIVE.is_file(), "真实归档件被删了"
    assert REAL_ARCHIVE.read_bytes() == before, "真实归档件被改了（历史证据只读）"


# ---------------------------------------------------------------- 主流程

def main() -> int:
    from agent import run_task as run_task_mod

    real_before = REAL_ARCHIVE.read_bytes() if REAL_ARCHIVE.is_file() else None
    if not REAL_BRIEF.is_file():
        print("data/briefs 里没有真实任务书（data/ 是 gitignore 的，CI 上不会有）：用同形状内联替身")
    if not REAL_RUN.is_file():
        print("data/runs 里没有真实 run 记录：用同形状内联替身")

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        saved = _isolate(tmp)
        prev_env = {
            key: os.environ.get(key)
            for key in ("UE_ACTION_CANDIDATES", "UE_LEARNED_ROUTES", "UE_HANDOFF_DIR")
        }
        os.environ["UE_ACTION_CANDIDATES"] = str(tmp / "action_candidates.json")
        os.environ["UE_LEARNED_ROUTES"] = str(tmp / "learned_routes.json")
        os.environ["UE_HANDOFF_DIR"] = str(tmp / "handoffs")

        import agent.understand as understand_mod
        import agent.umg_manual as umg_manual_mod
        import core.editor_exec as editor_exec

        saved_titles = understand_mod._titles
        saved_hud = umg_manual_mod.maybe_write_hud_plan
        saved_editor = editor_exec.exec_tool
        # 离线：检索标题不查（别加载检索模型）；HUD 人工步骤文档不写（别落到真工程 Plan/）；
        # 真编辑器全程封死，谁碰谁红。
        understand_mod._titles = lambda _query: []
        umg_manual_mod.maybe_write_hud_plan = lambda _query: ""
        editor_exec.exec_tool = _forbid_editor
        try:
            check_catalog_prompt()
            check_plan_windows()
            check_scope_windows_unrelated()
            check_scope_windows_no_guess()
            check_brief_gate_wording()
            check_materialize_no_silent_zero_window()
            check_gap_run_is_not_done()
            check_real_brief_run_is_not_done()
            check_three_state_exclusion()
            check_known_defect_gap_with_delayed_item()
            check_known_defect_paused_window()
            check_known_defect_stuck_window_passes()
            check_known_defect_external_scope_wording()
            check_create_level_idempotent()
            check_create_level_failures()
            check_create_level_no_guess()
            check_create_level_write_authorization()
            check_window_to_judge_chain()
            check_binding_parity()
            check_legacy_compat()
            check_archive_fidelity(tmp)
            check_historical_guards()
            check_real_archive_untouched(real_before)
        finally:
            understand_mod._titles = saved_titles
            umg_manual_mod.maybe_write_hud_plan = saved_hud
            editor_exec.exec_tool = saved_editor
            assert run_task_mod.run_task is not None
            _restore(saved)
            for key, value in prev_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    print("failure 192858 ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
