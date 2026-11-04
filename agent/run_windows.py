# agent/run_windows.py
"""多窗口：每扇单独跑现有任务。写场景/编译/PIE 的窗口过编辑器闸门，只读窗口并行。
集成不叫模型；任务书的集成扇跑矩阵验收。事件流落 data/events/<run_id>.jsonl，
机读 run 状态落 data/runs/YYYYMMDD/<run_id>.json（老 run 平铺兼容），可 --resume 续接。
子 CLI 的脚本、思考和 print 另落同目录 <run_id>.cli.jsonl，用来回放，不用再跑。
写源码的窗口先拍字节快照，失败且无 keep_source 时还原。估算只进任务书，不作为执行预算。"""
import concurrent.futures
import hashlib
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.catalog import (
    BLUEPRINT_ATTEMPT_ACTIONS, DIRECT_ACTIONS, DISPOSE_ACTION, EDITOR_LOCK_ACTIONS, GRAPH_ACTIONS,
    INTEGRATE_ACTION, READ_ONLY_ACTIONS,
)
from agent.harness import effective_verify_specs
from agent.plan_windows import find_cycle, mark_dangling, plan_windows
from agent.sequence import RUNS_DIR
from config import (
    PROJECT_ROOT, UE_PROJECT_DIR, UE_SOURCE_ROOT, default_pie, default_save, project_module,
    task_flag, task_policy,
)
from agent.snapshot import (
    restore_source as _restore_source,
    sha256_file as _sha256_file,
    snapshot_source as _snapshot_source,
)

EVENTS_DIR = PROJECT_ROOT / "data" / "events"
ARCHIVE_DIR = PROJECT_ROOT / "data" / "archive"


def _runs_day(run_id: str) -> str:
    """run_id 形如 YYYYMMDD-HHMMSS，日期做子目录。对不上（外来 id）留平铺。"""
    head = str(run_id or "").split("-", 1)[0]
    return head if len(head) == 8 and head.isdigit() else ""


def runs_md_path(run_id: str) -> Path:
    """给人读的 run 记录：data/runs/YYYYMMDD/<run_id>.md（老 run 平铺兼容读写）。"""
    day = _runs_day(run_id)
    return (RUNS_DIR / day if day else RUNS_DIR) / f"{run_id}.md"


def runs_json_path(run_id: str) -> Path:
    """机读 run 状态：data/runs/YYYYMMDD/<run_id>.json（老 run 平铺兼容读写）。"""
    day = _runs_day(run_id)
    return (RUNS_DIR / day if day else RUNS_DIR) / f"{run_id}.json"


def runs_cli_path(run_id: str) -> Path:
    """子 CLI 日志：data/runs/YYYYMMDD/<run_id>.cli.jsonl。一天一个目录，和机读记录放一起。"""
    day = _runs_day(run_id)
    return (RUNS_DIR / day if day else RUNS_DIR) / f"{run_id}.cli.jsonl"


def append_cli_log(run_id: str, record: dict) -> None:
    """追加一行子 CLI 日志。写失败不影响这一扇继续跑。"""
    if not run_id or not isinstance(record, dict) or not record.get("text"):
        return
    from agent.cli_log import append_record

    try:
        append_record(runs_cli_path(run_id), record)
    except OSError:
        pass

_SOURCE_LABEL = {"rules": "手写规则", "learned": "已学路径", "understood": "理解器",
                 "planner": "规划者", "brief": "任务书", "resume": "续接"}
_MD_STATUS = {
    "queued": "未开始", "running": "运行中", "retry": "重试", "done": "成功", "failed": "失败",
    "paused": "暂停", "skipped": "跳过", "waiting_external": "等待外部", "revising": "返工", "interrupted": "中断",
}
_TERMINAL_STATES = frozenset({"done", "failed", "paused", "skipped", "waiting_external"})
_RESUMABLE_STATES = frozenset({"queued", "running", "retry", "failed", "paused", "waiting_external", "revising", "interrupted"})
_SCENE_WRITE_ACTIONS = frozenset({
    "set_pickable", "move_actor", "place_actor", "place_blueprint", "place_pair",
    "bind_blueprint", "bind_slot", "create_blueprint", "create_widget_blueprint",
})
_SOURCE_WRITE_ACTIONS = frozenset({"edit_cpp"})
_DISCONNECT_RE = re.compile(r"连不上|10061|refused", re.I)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def confirm_fields(decision, path: str = "") -> tuple[str, str]:
    """人的选择：s 停止 / c 跳过 / r 补路径或重试。路径只在补路径时有字。"""
    text = decision if isinstance(decision, str) else ""
    if text == "skip":
        return "c", ""
    if text == "retry":
        return "r", ""
    if text in ("", "stop"):
        return "s", ""
    return "r", path or text


# ---------- 编辑器闸门：读并行、写独占 ----------

class _EditorGate:
    """编辑器闸门。只读裁决并行（读锁），写场景/编译/PIE 独占（写锁）。
    UE 编辑器单进程，cpp_build 还要关编辑器，写窗口之间永远不重叠。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False

    def _enter_read(self) -> None:
        with self._cond:
            while self._writer:
                self._cond.wait()
            self._readers += 1

    def _exit_read(self) -> None:
        with self._cond:
            self._readers -= 1
            self._cond.notify_all()

    def _enter_write(self) -> None:
        with self._cond:
            while self._writer or self._readers:
                self._cond.wait()
            self._writer = True

    def _exit_write(self) -> None:
        with self._cond:
            self._writer = False
            self._cond.notify_all()

    def enter(self, action: str) -> None:
        if action in READ_ONLY_ACTIONS:
            self._enter_read()
        elif action in EDITOR_LOCK_ACTIONS:
            self._enter_write()

    def exit(self, action: str) -> None:
        if action in READ_ONLY_ACTIONS:
            self._exit_read()
        elif action in EDITOR_LOCK_ACTIONS:
            self._exit_write()


_GATE = _EditorGate()


class _Pool:
    """极小线程池：daemon 线程，Ctrl+C 不等在途窗口，进程可立即退出。"""

    _SENTINEL = object()

    def __init__(self, max_workers: int = 3) -> None:
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._threads = [
            threading.Thread(target=self._work, name=f"window-{index}", daemon=True)
            for index in range(max(1, int(max_workers)))
        ]

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def _work(self) -> None:
        while True:
            item = self._q.get()
            if item is _Pool._SENTINEL:
                return
            fn, future = item
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn())
                except BaseException as exc:  # noqa: BLE001 - 交回主线程记账
                    future.set_exception(exc)

    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        self._q.put((lambda: fn(*args, **kwargs), future))
        return future

    def close(self) -> None:
        for _thread in self._threads:
            self._q.put(_Pool._SENTINEL)


# ---------- 机读 run 状态与事件流 ----------

_TASK_SNAPSHOT_KEYS = (
    "id", "action", "title", "verify", "allow_writes", "save", "pie",
    "automation_filter", "allow_blueprint_ref", "max_retries",
)


def _task_snapshot(task: dict | None) -> dict | None:
    """把这一扇真正拿去跑的任务摊成可落盘的快照：验什么（verify spec 原件）+
    按什么策略跑（allow_writes / save / pie）。

    run 记录的 entry["window"] 是规划窗口定义，没有 task.verify；detail.build /
    detail.test 记的是裁判结论（kind: skip 那种），不是 spec 原件。DSH 的 verify
    需要 spec 才能重跑裁判，所以 spec 必须单独留一份（20261024-011917 的 W3/W8）。
    detail.* 的语义不动，这里只加一份 spec 副本。"""
    if not isinstance(task, dict) or not task:
        return None
    snap: dict = {}
    for key in _TASK_SNAPSHOT_KEYS:
        if key not in task:
            continue
        value = task[key]
        if key == "verify":
            continue
        if key == "allow_writes":
            value = [str(item) for item in (value or [])]
        elif key in ("save", "pie"):
            value = task_flag(task, key)
        snap[key] = value
    # 验什么一律按运行期口径摊平（verify 原件 + automation_filter 合成的那条）：
    # run_automation_test 这类窗口的 spec 只写在 automation_filter 上，verify 是空的。
    snap["verify"] = effective_verify_specs(task)
    if "allow_writes" not in snap:
        snap["allow_writes"] = []
    if "save" not in snap:
        snap["save"] = task_flag(task, "save")
    if "pie" not in snap:
        snap["pie"] = task_flag(task, "pie")
    return snap


def legacy_task_snapshot(window: dict, origin: str = "", run_id: str = "") -> dict | None:
    """旧 run 记录没有 entry["task"] 时，按当时的确定性规则把它复原出来。

    只调用运行期本来就会调的那条路（_prepare_task：规划窗口 → 任务），不拼句子、
    不叫模型、不发明 spec：原来没 spec 的窗口复原出来仍然没有 spec（place_actor
    运行期就只跑 _run_action，never 有 verify）。恢复不了就返回 None，让人看见
    「这扇本来就没留证据」，而不是猜一条。"""
    if not isinstance(window, dict) or not window:
        return None
    try:
        task = _prepare_task(dict(window), str(origin or ""), {}, str(run_id or ""))
    except Exception:
        return None
    if not isinstance(task, dict) or task.get("ok") is False:
        return None
    return _task_snapshot(task)


class _RunState:
    """data/runs/<run_id>.json 的内存镜像，每次变化写穿。窗口定义存进 entry["window"]，续接不用重规划。"""

    def __init__(self, run_id: str, query: str, source: str, windows: list[dict],
                 *, review: dict | None = None, brief: dict | None = None, scopes=None) -> None:
        self.run_id = run_id
        self.path = runs_json_path(run_id)
        self.data = {
            "run_id": run_id, "query": query, "source": source,
            "started_at": _now(), "updated_at": _now(), "status": "running",
            "pid": os.getpid(), "heartbeat_at": _now(),
            "review": review or {}, "brief": brief, "scopes": scopes or [],
            "windows": {}, "artifacts": {}, "integration": {},
        }
        for window in windows:
            self.data["windows"][str(window["id"])] = self._entry(window)

    def _entry(self, window: dict) -> dict:
        action = str(window.get("action") or (window.get("task") or {}).get("action") or "")
        realizable = bool(window.get("realizable", True))
        state = "waiting_external" if window.get("waiting_external") else ("paused" if not realizable else "queued")
        entry = {
            "id": str(window["id"]),
            "title": str(window.get("title") or window["id"]),
            "action": action,
            "depends_on": [str(dep) for dep in window.get("depends_on") or []],
            "state": state,
            "ok": None, "result": None, "detail": None, "thread_id": None,
            "elapsed": None, "retries": 0, "summary": "",
            "snapshot": None, "realizable": realizable,
            "error": str(window.get("error") or ""),
            "external_dep": str(window.get("external_dep") or ""),
            "window": dict(window),
        }
        # 规划期就带 task 的窗口（任务书/人工补的）先把 spec 落一份：这一扇即使没跑到，
        # 记录里也能看出它本来要验什么。真跑起来后由 _absorb_result 用运行期的覆盖。
        planned = _task_snapshot(window.get("task"))
        if planned is not None:
            entry["task"] = planned
        return entry

    @classmethod
    def from_resume(cls, data: dict) -> "_RunState":
        """续接：done 复用结果；running/interrupted 用原 thread_id 重跑；failed 重新排队；
        paused 且有动作的仍等人工裁决；paused 但没绑上动作的旧死桩直接跳过——裁决不问它，
        r 也填不活，重拆那句话才会走新路由。"""
        state = cls.__new__(cls)
        state.run_id = str(data.get("run_id") or datetime.now().strftime("%Y%m%d-%H%M%S"))
        state.path = runs_json_path(state.run_id)
        fresh = dict(data)
        fresh["status"] = "running"
        fresh["started_at"] = fresh.get("started_at") or _now()
        fresh["pid"] = os.getpid()
        fresh["heartbeat_at"] = _now()
        windows = fresh.get("windows") or {}
        for entry in windows.values():
            if not isinstance(entry, dict):
                continue
            current = str(entry.get("state") or "")
            if current in ("running", "interrupted"):
                entry["state"] = "queued"
                entry["resume_thread"] = bool(entry.get("thread_id"))
                entry["ok"] = None
                entry["result"] = None
            elif current in ("failed", "queued", "retry", "revising"):
                entry["state"] = "queued"
                entry["ok"] = None
                entry["result"] = None
            elif current == "paused" and not str(entry.get("action") or ""):
                # 旧规则拆出的死桩：人工裁决只问有动作的暂停扇，r 填进去也没有可跑的东西。
                entry["state"] = "skipped"
                entry["ok"] = False
                entry["summary"] = "旧规则拆出的死桩（没绑上动作），续接时跳过。重拆那句话会走新路由。"
        fresh["windows"] = {key: value for key, value in windows.items() if isinstance(value, dict)}
        fresh.setdefault("artifacts", {})
        fresh.setdefault("scopes", [])
        fresh["updated_at"] = _now()
        state.data = fresh
        return state

    def entries(self) -> list[dict]:
        return list(self.data["windows"].values())

    def window(self, wid: str) -> dict:
        return self.data["windows"][wid]

    def save(self) -> None:
        self.data["updated_at"] = _now()
        self.data["pid"] = os.getpid()
        self.data["heartbeat_at"] = self.data["updated_at"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            tmp.replace(self.path)
        except PermissionError:
            time.sleep(0.1)  # Windows：目标刚被别的线程读过，稍候再换
            tmp.replace(self.path)


class _EventBus:
    """一行一事件写 data/events/<run_id>.jsonl；调用方的 emit 回调（面板）是第二个订阅者。"""

    def __init__(self, run_id: str, emit=None) -> None:
        self.run_id = run_id
        self.path = EVENTS_DIR / f"{run_id}.jsonl"
        self._lock = threading.Lock()
        self._extra = emit

    def emit(self, etype: str, **fields) -> None:
        event = {"ts": _now(), "run_id": self.run_id, "type": etype}
        event.update({key: value for key, value in fields.items() if value is not None})
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        if self._extra is not None:
            try:
                self._extra(event)
            except Exception:
                pass
        try:
            from agent.cli_log import cli_record

            rec = cli_record(event)
            if rec:
                append_cli_log(self.run_id, rec)
        except Exception:
            pass


# ---------- 窗口 → 任务 ----------

def _build_graph_task(window: dict, origin: str) -> dict:
    action = window.get("action")
    title = str(window.get("title") or action or "")
    if action == "compile_project":
        from agent.supervise import _LAUNCH_EDITOR_RE, _project_compile_task

        sentence = title or "编译工程"
        if _LAUNCH_EDITOR_RE.search(origin) and not _LAUNCH_EDITOR_RE.search(sentence):
            sentence += "。启动 UE 编辑器"
        task = _project_compile_task(sentence)
        task["id"] = window["id"]
        task["title"] = title or "编译工程"
        return task
    if action == "edit_cpp":
        from agent.supervise import (
            _FILE_RE, _resolve_class, _try_cpp, cpp_task_for_class, named_file_class,
        )

        class_name = str(window.get("class_name") or (window.get("slots") or {}).get("class") or "")
        # 判据顺序（20261024-0215 的 WF-1 + WF-2），按「谁更权威」排。规划者给的 title 只是扇名
        # （「改 C++ 并编译」），拿它重建任务句等于把原句需求整段丢掉（20261024-011917 的 W1
        # 只剩标题与类名，模型只好猜，改了个 bCanBePossessed 编译 exit 0 就报成功）。
        # ① 原句唯一点名一个 .h/.cpp → 目标就是它，不看 class 槽位。
        #    该文件解析出 UCLASS 就走按类的任务体（shortcuts=False：这扇要真改源码，
        #    不能被「移动已实现就只编译」改写成只编译）；解析不出就把文件本身钉死。
        #    class 槽位写什么都不许因此换类——WF-2 的换类病只是入口挪了过来，不是没了。
        # ② 原句没点名文件、class 槽位有值 → 按类钉死。原句提到多个类名、又没点名文件时也走这条，
        #    交给 class 槽位/唯一性判断，不让 _try_cpp 去猜。
        # ③ 都没有 → 回落旧写法。
        task = None
        named = _FILE_RE.findall(origin or "")
        if len(named) == 1:
            bare = class_name[1:] if class_name.startswith("A") and len(class_name) > 1 else class_name
            slot_ok = not class_name or bool(_resolve_class(class_name)) or named[0] == bare
            if not slot_ok:
                # 槽位点了一个索引里没有、又不是原句点名那一个的类：这扇窗口自身不自洽，不猜
                return {"ok": False, "error": "写不出唯一的 C++ 类，不猜。"}
            target = named_file_class(origin)
            task = cpp_task_for_class(origin, target) if target else _try_cpp(origin)
            if task is None:
                return {
                    "ok": False,
                    "error": "写不出唯一的 C++ 路径，不猜。只给一次：r Private/名字.cpp。",
                    "needs": "source",
                    "blocked": True,
                }
        elif class_name:
            task = cpp_task_for_class(origin, class_name)
        if not task:
            task = _try_cpp(f"{title}。类 {class_name}。编译")
        if not task or task.get("ok") is False:
            return task or {"ok": False, "error": "写不出唯一的 C++ 类，不猜。"}
        # 规划给的是「改 C++」这一扇，这扇就该改源码。judge_first 是给「只编译」用的，
        # 拿它跑改动扇会变成一行都不改、编译 exit 0 就算通过（20261021-195220 的 W1）。
        task["judge_first"] = False
        task["expect_source_write"] = True
        task["id"] = window["id"]
        task["title"] = title or "改 C++ 并编译"
        return task
    if action == "edit_widget_blueprint":
        from agent.plan_windows import widget_edit_task

        task = widget_edit_task(origin or title, window["id"])
        if task.get("ok") is False:
            return task
        task["id"] = window["id"]
        task["title"] = title or task["title"]
        return task
    if action == "compile_blueprint":
        from agent.supervise import _base

        ref = str(window.get("ref_path") or "")
        if not ref:
            return {"ok": False, "error": "写不出唯一的蓝图 refPath，不猜。"}
        task = _base(title)
        task["id"] = window["id"]
        task["title"] = title or "编译蓝图"
        task["query"] = f"{title}\n不要改蓝图 Graph。不要调用工具，让裁判编译 {ref}。"
        task["verify"] = [{"kind": "blueprint_compile", "refPath": ref}]
        return task
    if action == "move_actor":
        from agent.supervise import _base

        name = str(window.get("actor_name") or "")
        loc = window.get("location") or {}
        if not name or not loc:
            return {"ok": False, "error": "写不出唯一的 Actor 或坐标，不猜。"}
        x, y, z = float(loc["x"]), float(loc["y"]), float(loc["z"])
        task = _base(title)
        task["id"] = window["id"]
        task["title"] = title or "移动 Actor"
        task["query"] = (
            f"{title}\n"
            f"把名为 {name} 的 Actor 移动到世界坐标 x={x:g} y={y:g} z={z:g}。"
            "先用 find_actors（name、tag、collision_channels 都要传，tag 传空字符串，collision_channels 传空数组）"
            f"拿到 refPath，再调用 set_actor_transform，只改 location，worldspace 为 true。"
            "改完保存当前关卡。"
        )
        task["verify"] = [{
            "kind": "editor_assert",
            "toolset": "editor_toolset.toolsets.actor.ActorTools",
            "tool": "get_actor_transform",
            "actor_name": name,
            "expect": {"location": {"x": x, "y": y, "z": z}},
        }]
        task["allow_writes"] = ["set_actor_transform"]
        task.update(task_policy())
        return task
    return {"ok": False, "error": f"未知动作 {action}"}


def _publish_artifacts(artifacts: dict, name: str, summary: dict) -> None:
    """成功扇按名字发布工件 {ref, in_level}；最后一个发布者同时当默认目标。"""
    if not summary.get("ref"):
        return
    entry = {"ref": str(summary["ref"]), "in_level": bool(summary.get("in_level", True))}
    if name:
        artifacts.setdefault("by_name", {})[str(name)] = entry
    artifacts["target"] = entry


def _lookup_artifact(artifacts: dict, name: str) -> dict | None:
    if not isinstance(artifacts, dict):
        return None
    by_name = artifacts.get("by_name") or {}
    if name and by_name.get(str(name)):
        return by_name[str(name)]
    return artifacts.get("target")


def _prepare_task(window: dict, origin: str, artifacts: dict, run_id: str = "") -> dict:
    if window.get("task"):
        task = dict(window["task"])
        task["id"] = window["id"]
        task.setdefault("save", default_save())
        task.setdefault("pie", default_pie())
    else:
        task = {
            "id": window["id"],
            "action": window.get("action"),
            "title": window.get("title"),
            "query": window.get("title") or "",
            "slots": dict(window.get("slots") or {}),
            **task_policy(),
            "allow_writes": [],
            "max_retries": int(window.get("max_retries") or 0),
        }
        for key in (
            "actor_name", "picker_name", "ref_path", "asset_path", "folder_path",
            "asset_name", "class_name", "parent", "from_path", "location",
        ):
            if window.get(key) not in (None, ""):
                task[key] = window[key]
        if isinstance(window.get("spec"), dict):
            task["spec"] = window["spec"]
    action = str(task.get("action") or window.get("action") or "")
    if action in GRAPH_ACTIONS and not task.get("verify"):
        built = _build_graph_task(window, origin)
        if built.get("ok") is False and not built.get("verify"):
            return built
        built["id"] = window["id"]
        task = built
    for key in ("wait_mcp", "launch_editor", "launch_without_source", "judge_first"):
        if window.get(key):
            task[key] = True
    if window.get("pie"):
        task["pie"] = True
    # 窗口显式写了 save / pie 就按写的走，没写跟随 UE_DEFAULT_SAVE / UE_DEFAULT_PIE
    if isinstance(window.get("save"), bool):
        task["save"] = window["save"]
    if isinstance(window.get("pie"), bool):
        task["pie"] = window["pie"]
    if action in ("judge_pickup", "check_pickup"):
        entry_ref = _lookup_artifact(artifacts, str(task.get("actor_name") or window.get("actor_name") or ""))
        if entry_ref:
            task["target_ref"] = entry_ref["ref"]
            task["target_in_level"] = bool(entry_ref.get("in_level", True))
    task.setdefault("save", default_save())
    task.setdefault("pie", default_pie())
    if run_id:
        task["run_id"] = run_id
    if window.get("criteria"):
        task["criteria"] = list(window.get("criteria") or [])
    if window.get("automation_filter"):
        task["automation_filter"] = window.get("automation_filter")
    try:
        from agent.harness import handoff_note

        note = handoff_note(run_id, window.get("depends_on") or [])
    except Exception:
        note = ""
    if note:
        task["query"] = note + "\n" + str(task.get("query") or window.get("title") or origin)
    return task


def _brief(summary: dict) -> dict:
    text = summary.get("summary") or summary.get("index_note") or summary.get("error") or ""
    out = {"id": summary.get("id"), "ok": bool(summary.get("ok")), "summary": str(text)[:500]}
    judge = summary.get("judge") or summary.get("verdict")
    if judge:
        out["judge"] = judge
    return out


def _run_one(task: dict, thread_id: str, action: str, on_row=None, under_gate=None) -> dict:
    from agent.cli_log import bind_window, reset_window
    from agent.run_task import run_task

    token = bind_window(str(task.get("id") or ""))
    _GATE.enter(action)
    try:
        if under_gate is not None:
            under_gate()
        return run_task(task, thread_id=thread_id, on_row=on_row)
    finally:
        _GATE.exit(action)
        reset_window(token)


def _make_on_row(events: _EventBus, wid: str):
    """把一扇的 trace 行转发成 run 级事件：build/test/precheck 算 judge，其余算 tool_call。"""

    def on_row(row: dict) -> None:
        if row.get("context_slice") or str(row.get("tool") or "") == "context_slice":
            counts = row.get("context_slice") if isinstance(row.get("context_slice"), dict) else {}
            events.emit(
                "context_slice",
                window=wid,
                user=int(counts.get("user") or 0),
                judge_fail=int(counts.get("judge_fail") or 0),
                tool_kept=int(counts.get("tool_kept") or 0),
                tool_dropped=int(counts.get("tool_dropped") or 0),
            )
            return
        node = str(row.get("node") or "")
        etype = "judge" if node in ("build", "test", "precheck") else "tool_call"
        calls = row.get("calls")
        if isinstance(calls, list):
            calls = [str(name) for name in calls[:8]] or None
        else:
            calls = None
        reasoning = str(row.get("reasoning") or "").strip()[:800] or None
        summary = str(row.get("summary") or "").strip()[:240] or None
        events.emit(
            etype, window=wid, node=node, tool=str(row.get("tool") or ""),
            ok=bool(row.get("ok")), note=str(row.get("error") or "")[:120] or None,
            elapsed=row.get("elapsed"),
            policy=row.get("policy"),
            summary=summary,
            reasoning=reasoning,
            calls=calls,
        )

    return on_row


# ---------- 源码快照与还原（实现见 agent/snapshot.py）----------

def _task_writes_source(window: dict) -> bool:
    task = window.get("task") or {}
    if task.get("inject_typo") or task.get("freeze_source"):
        return False
    return any(
        isinstance(item, dict) and item.get("kind") == "cpp_build"
        for item in task.get("verify") or []
    )


# ---------- 一扇的执行 ----------

def _run_window(entry: dict, state: _RunState, query: str, artifacts: dict, events: _EventBus, echo) -> dict:
    from agent.run_task import new_thread_id

    window = dict(entry.get("window") or {})
    if state.data.get("criteria") and not window.get("criteria"):
        window["criteria"] = list(state.data.get("criteria") or [])
    action = str(entry.get("action") or window.get("action") or "")
    if action == DISPOSE_ACTION:
        return _run_dispose(entry, state)
    if action == INTEGRATE_ACTION:
        return _run_integrate(entry, state, query, artifacts, events)
    on_row = _make_on_row(events, entry["id"])
    from backends.select import current_backend

    monolith_blueprint = action in BLUEPRINT_ATTEMPT_ACTIONS and current_backend() == "monolith"
    # 蓝图和图动作在 monolith 下固定 3 次，不再被 GRAPH_ACTIONS 压成 1 次。
    # compile_project / edit_cpp 仍是一次图任务，不占这 3 次。
    if monolith_blueprint:
        attempts = 3
    elif action in GRAPH_ACTIONS:
        attempts = 1
    else:
        attempts = int(window.get("max_retries") or 0) + 1
    snap_box: dict = {}

    def _snapshot_once() -> None:
        if not snap_box and (_task_writes_source(window) or action in _SOURCE_WRITE_ACTIONS):
            snap_box["snap"] = _snapshot_source(state.run_id, entry["id"])

    summary = {"id": entry["id"], "ok": False, "summary": "没有执行"}
    attempt = 0
    task: dict = {}
    started = time.perf_counter()
    for attempt in range(1, attempts + 1):
        if monolith_blueprint:
            from agent.sequence import repair_blueprint

            events.emit("window_state", window=entry["id"], state="running", attempt=1)
            summary = repair_blueprint(
                {**window, "action": action, "query": query or window.get("title") or ""},
                attempts=3,
            )
            summary["id"] = entry["id"]
            attempt = int(summary.get("attempts") or 0)
            break
        events.emit("window_state", window=entry["id"], state="running" if attempt == 1 else "retry", attempt=attempt)
        task = _prepare_task(window, query, artifacts, state.run_id)
        if task.get("ok") is False and not task.get("verify") and task.get("action") not in DIRECT_ACTIONS:
            snap = _task_snapshot(task)
            if snap is not None:
                entry["task"] = snap  # 没跑也要留底：这扇本来要验什么、按什么策略跑
            summary = {
                "id": entry["id"], "ok": False,
                "summary": str(task.get("error") or "这一窗暂停"),
                "error": task.get("error"), "paused": True,
            }
            break
        if attempt == 1 and entry.get("resume_thread") and entry.get("thread_id"):
            thread_id = str(entry["thread_id"])  # 断点续跑：图内对话从 checkpoint 续
        else:
            thread_id = new_thread_id()
        # 运行期任务才是真凭据：spec 原件与 save/pie/allow_writes 按这一份落到 entry["task"]，
        # DSH 的 verify 之后从这里取回 spec 重跑裁判。落盘在 _absorb_result 的 state.save()。
        snap = _task_snapshot(task)
        if snap is not None:
            entry["task"] = snap
        try:
            summary = _run_one(task, thread_id, action, on_row=on_row, under_gate=_snapshot_once)
        except Exception as exc:
            summary = {"id": entry["id"], "ok": False, "summary": f"{type(exc).__name__}: {exc}", "error": str(exc)}
        if not summary.get("thread_id"):
            summary["thread_id"] = thread_id
        if summary.get("ok"):
            break
        if attempt < attempts:
            echo(f"\n{entry['id']} 失败，重试这一扇。")
    retries = max(0, attempt - 1)
    elapsed = round(time.perf_counter() - started, 3)
    snapshot = snap_box.get("snap")
    keep = bool((window.get("task") or {}).get("keep_source"))
    if snapshot is not None and not summary.get("ok") and not keep:
        restored = _restore_source(state.run_id, entry["id"], snapshot)
        if restored:
            summary["restored"] = restored
    if not summary.get("ok") and not keep and action in ("set_pickable", "move_actor", "place_batch", "set_scalar_param"):
        try:
            from agent.prop_snapshot import restore_props

            props = restore_props(state.run_id)
            if props:
                summary["props_restored"] = props
        except Exception:
            pass
    out = dict(summary)
    if out.get("elapsed") is None:
        out["elapsed"] = elapsed
    out["retries"] = retries
    out["snapshot"] = snapshot
    # 「改 C++」这扇的活儿是改源码。judge_first 的扇只跑裁判、一行都没写时，
    # 编译 exit 0 什么都不证明（20261021-195220 的 W1：plan 0.01s 空转，30 个源文件零改动，
    # 仍然记成功）。没有 source_written 就不算完成，原因写明，交给人或上游重拆。
    # 明确写成只编译（judge_first / freeze_source / compile_project）的扇不在此列：
    # 它们的活儿本来就是核对现状，不是改。
    compile_only = bool(task.get("judge_first") or task.get("freeze_source"))
    must_write = bool(task.get("expect_source_write")) or (
        action == "edit_cpp" and not compile_only
    )
    if must_write and out.get("ok") and not out.get("source_written"):
        text = "这一扇要求改源码，但没有一次 apply_unified_diff 成功，源码零改动，不算完成。"
        out["ok"] = False
        out["error"] = text
        out["summary"] = text
    # 控件图扇同理：只读了一遍控件树、一处没改就编译过，是空转。
    if action == "edit_widget_blueprint" and out.get("ok"):
        touched = [
            str(name) for name in (summary.get("tools") or [])
            if name and not str(name).lower().startswith(("get", "list", "find", "read", "search", "discover"))
        ]
        if not touched:
            text = "这一扇要求改控件图，但只读了控件树、没有一次写工具调用，不算完成。"
            out["ok"] = False
            out["error"] = text
            out["summary"] = text
        else:
            out["widget_written"] = True
    return out


def _dispatch_window(entry: dict, state: _RunState, query: str, events: _EventBus, echo) -> dict:
    return _run_window(entry, state, query, state.data["artifacts"], events, echo)


def _stamp_judge(judge, wid: str) -> dict | None:
    """裁判结构体带上窗口 id。不带 id 时条款的归属校验会退化成按 kind 全量覆盖，
    正是 20261021-195220 里 W5 勾动整句条款的原因。"""
    if not isinstance(judge, dict):
        return None
    out = dict(judge)
    if wid:
        out["window"] = wid
    return out


def _automation_residual(summary: dict) -> bool:
    build = summary.get("build") if isinstance(summary.get("build"), dict) else {}
    test = summary.get("test") if isinstance(summary.get("test"), dict) else {}
    return build.get("ok") is True and str(test.get("kind") or "") == "automation" and test.get("ok") is False


def _apply_run_stitch(state: _RunState, query: str, events: _EventBus, echo) -> bool:
    """中断先用脚本接。有改动就先不派下一扇，避免集成带着没人认领的条款收尾。"""
    from agent.stitch import apply_stitch

    changed = apply_stitch(state, query)
    if not changed:
        return False
    note = str(state.data.get("stitch_note") or "已接上中断")
    echo(f"\n{note}")
    events.emit("stitch", note=note)
    return True


def _append_dispose_windows(state: _RunState, entry: dict, summary: dict, payload: dict | None) -> None:
    """编译过了、Automation 没过：按测试名追加处置扇。规划者写不出这个动作。"""
    if str(entry.get("action") or "") == DISPOSE_ACTION:
        return
    if not _automation_residual(summary):
        return
    from agent.harness import build_dispose_windows

    test = summary.get("test") if isinstance(summary.get("test"), dict) else {}
    errors = [item for item in (test.get("errors") or []) if isinstance(item, dict)]
    if not errors and str(test.get("summary") or "").strip():
        errors = [{"message": str(test.get("summary"))}]
    criteria = [
        item for item in (state.data.get("criteria") or [])
        if isinstance(item, dict) and str(item.get("evidence_kind") or "") == "automation" and not item.get("passed")
    ]
    windows = build_dispose_windows(
        str(entry.get("id") or ""),
        errors,
        criteria,
        list((payload or {}).get("unmet_ids") or []),
    )
    added = False
    for window in windows:
        if window["id"] in state.data["windows"]:
            continue
        state.data["windows"][window["id"]] = state._entry(window)
        added = True
    if added:
        state.save()


def _run_dispose(entry: dict, state: _RunState) -> dict:
    """读归因文件并结案。不调用模型，不把归因说明写成 passed。"""
    from agent.harness import judge_disposition, load_attribution
    from agent.run_task import new_thread_id

    window = entry.get("window") or {}
    residual_ids = list(window.get("unmet_ids") or [])
    criteria = [dict(item) for item in (window.get("criteria") or []) if isinstance(item, dict)]
    loaded = load_attribution(state.run_id, entry["id"])
    if loaded.get("malformed"):
        summary = str(loaded.get("error") or "归因文件无效")
        ok = False
    else:
        verdict = judge_disposition(residual_ids, criteria, loaded.get("items") or [])
        summary = str(verdict.get("summary") or "")
        ok = bool(verdict.get("ok"))
    return {
        "id": entry["id"],
        "ok": ok,
        "summary": summary,
        "error": "" if ok else summary,
        "thread_id": new_thread_id(),
        "build": {"ok": True, "kind": "skip", "summary": "处置扇不编译", "errors": []},
        "test": {"ok": False, "kind": "skip", "summary": summary, "errors": []},
    }


def _absorb_result(state: _RunState, events: _EventBus, echo, wid: str, summary: dict) -> None:
    entry = state.window(wid)
    ok = bool(summary.get("ok"))
    paused = bool(summary.get("paused"))
    entry["state"] = "paused" if paused else ("done" if ok else "failed")
    entry["ok"] = ok
    entry["result"] = _brief(summary)
    entry["detail"] = {key: value for key, value in summary.items() if value is not None}
    if summary.get("thread_id"):
        entry["thread_id"] = summary["thread_id"]
    if summary.get("elapsed") is not None:
        entry["elapsed"] = summary["elapsed"]
    if summary.get("retries") is not None:
        entry["retries"] = summary["retries"]
    if summary.get("snapshot"):
        entry["snapshot"] = summary["snapshot"]
    entry["summary"] = str(summary.get("summary") or "")[:200]
    criteria = list(state.data.get("criteria") or [])
    try:
        from agent.harness import apply_judges, classify_stop

        action = str(entry.get("action") or (entry.get("window") or {}).get("action") or "")
        # 裁判带上窗口 id：条款只认列出这扇窗口的证明，别的窗口编译过了勾不动它。
        judges = [_stamp_judge(summary.get("build"), wid), _stamp_judge(summary.get("test"), wid)]
        if ok and action == "edit_widget_blueprint":
            # 控件图扇跑完并编译通过，就是 widget_graph 条款的证据：控件图没有单独的裁判。
            # 但它只证明「控件图改过且编过」，条款里写的事件绑定、Tick、挂视口不在证据里。
            judges.append({"ok": True, "kind": "widget_graph", "window": wid, "summary": "控件图已改并编译通过"})
        if isinstance(summary.get("criteria"), list):
            criteria = apply_judges(criteria, *judges)
            by_id = {str(item.get("id")): item for item in criteria if isinstance(item, dict)}
            for item in summary["criteria"]:
                if not isinstance(item, dict) or not item.get("passed"):
                    continue
                target = by_id.get(str(item.get("id")))
                if target is None:
                    continue
                owners = target.get("windows")
                if isinstance(owners, list) and wid not in [str(one) for one in owners]:
                    continue  # 这扇不在条款的证明窗口里，图内说通过也不算
                target["passed"] = True
            criteria = list(by_id.values())
        else:
            criteria = apply_judges(criteria, *judges)
        state.data["criteria"] = criteria
        for scope in state.data.get("scopes") or []:
            if isinstance(scope, dict) and wid in (scope.get("window_ids") or []):
                # 范围条款只用自己的那一份。用全局兜底会让「本范围没条款」变成
                # 「继承整句条款」，范围完成判定就失去意义。
                own = scope.get("criteria")
                scope["criteria"] = apply_judges(own if isinstance(own, list) else [], *judges)
        reason = classify_stop(
            ok=ok,
            paused=paused,
            retries=int(summary.get("retries") or entry.get("retries") or 0),
            max_retries=int((entry.get("window") or {}).get("max_retries") or 0),
            summary=str(summary.get("summary") or ""),
            error=str(summary.get("error") or ""),
            unresolved=(entry.get("window") or {}).get("unresolved"),
        )
    except Exception:
        reason = "ok" if ok else ("paused" if paused else "judge_failed")
    if str(summary.get("verdict") or "").strip() == "no_evidence":
        # 集成扇的「一条都没验」不是裁判失败，是整段没交付：停因要能区分开
        reason = "no_evidence"
    entry["stop_reason"] = reason
    if ok and summary.get("ref"):
        _publish_artifacts(
            state.data["artifacts"],
            str((entry.get("window") or {}).get("actor_name") or summary.get("id") or ""),
            summary,
        )
    if ok and (summary.get("source_written") or str(entry.get("action")) in _SCENE_WRITE_ACTIONS):
        events.emit("files_modified", window=wid, kinds=("source" if summary.get("source_written") else "scene"))
    state.save()
    events.emit("window_state", window=wid, state=entry["state"])
    events.emit("window_result", window=wid, ok=ok, state=entry["state"], summary=entry["summary"],
                elapsed=entry.get("elapsed"), retries=entry.get("retries"), stop_reason=entry.get("stop_reason"))
    payload = None
    try:
        from agent.harness import build_handoff, write_handoff

        nxt = ""
        for other in state.entries():
            if wid in (other.get("depends_on") or []):
                nxt = str(other.get("title") or other.get("id") or "")
                break
        payload = build_handoff(
            did=str(entry.get("title") or entry.get("action") or wid),
            judges=[item for item in (summary.get("build"), summary.get("test")) if isinstance(item, dict)],
            summary=str(summary.get("summary") or ""),
            error=str(summary.get("error") or ""),
            ok=ok,
            criteria=state.data.get("criteria") or [],
            next_title=nxt,
            artifacts=state.data.get("artifacts") or {},
        )
        write_handoff(state.run_id, wid, payload)
        if payload.get("unmet"):
            events.emit(
                "handoff", window=wid, unmet=payload.get("unmet"),
                unmet_total=payload.get("unmet_total"), next=payload.get("next"),
            )
    except Exception:
        pass
    try:
        _append_dispose_windows(state, entry, summary, payload)
    except Exception:
        pass
    echo(f"\n{wid} {_MD_STATUS.get(entry['state'], entry['state'])}。{json.dumps(entry['result'], ensure_ascii=False)}")


# ---------- 集成矩阵验收 ----------

def _looks_disconnected(entry: dict) -> bool:
    result = entry.get("result") or {}
    detail = entry.get("detail") or {}
    text = " ".join(
        str(part) for part in (result.get("summary"), result.get("error"), detail.get("error"), detail.get("editor"))
        if part
    )
    return bool(_DISCONNECT_RE.search(text))


def _capability_gap_scope(scope: dict) -> bool:
    """整段没交付的范围：materialize_scopes 标了 capability_gap；老记录没这个字段，
    但「没暂停、不等外部、也不是集成末行，却一扇窗口都没有」按同一口径认。"""
    if not isinstance(scope, dict):
        return False
    gap = scope.get("capability_gap")
    if isinstance(gap, dict) and gap:
        return True
    if bool(scope.get("blocked")) or str(scope.get("external_dep") or "").strip():
        return False
    if str(scope.get("kind") or "") == "integration" or str(scope.get("id") or "") == "INT":
        return False
    return not [wid for wid in (scope.get("window_ids") or []) if str(wid).strip()]


def _capability_gap_scopes(state: _RunState) -> list[dict]:
    return [scope for scope in (state.data.get("scopes") or []) if _capability_gap_scope(scope)]


def _capability_gap_text(scope: dict) -> str:
    """缺口的一句话：缺什么。集成 summary 与收尾 note 都用它点名。"""
    gap = scope.get("capability_gap") if isinstance(scope.get("capability_gap"), dict) else {}
    reason = str(gap.get("reason") or "").strip() or "没有可执行的窗口，是能力缺口"
    bits = [reason]
    missing = [str(item) for item in (gap.get("missing_actions") or []) if str(item).strip()]
    if missing:
        bits.append("缺动作：" + "、".join(missing))
    deliver = [
        str(item) for item in (gap.get("deliverables") or scope.get("deliverables") or [])
        if str(item).strip()
    ]
    if deliver:
        bits.append("没交付：" + "、".join(deliver[:3]))
    return "；".join(bits)


def _delivery_criteria(state: _RunState) -> list[dict]:
    """「交付验收」看的条款：优先 run 级 state.data["criteria"]，没有才回落各窗口 window["criteria"]。
    只算「所属窗口都跑完（done）」的条款：一扇失败的窗口可能已经带着通过的 build 判词
    （20261021-195220 那个空转扇：编译 exit 0 但源码零改动），那种条款不能给集成矩阵撑出
    ok=True —— check_harness 的老口径「只有不适用项的矩阵永远不算全过」靠的就是它保持 None。"""
    rows: list[dict] = [item for item in (state.data.get("criteria") or []) if isinstance(item, dict)]
    if not rows:
        for entry in state.entries():
            rows.extend(
                item for item in ((entry.get("window") or {}).get("criteria") or [])
                if isinstance(item, dict)
            )
    done = {str(entry.get("id") or "") for entry in state.entries() if entry.get("state") == "done"}
    out: list[dict] = []
    for item in rows:
        owners = [str(wid) for wid in (item.get("windows") or []) if str(wid).strip()]
        if owners and not all(wid in done for wid in owners):
            continue
        out.append(item)
    return out


def _all_criteria(state: _RunState) -> list[dict]:
    """run 级全部条款，不做「所属窗口都跑完」过滤。证据计数用它，判定口径仍用 _delivery_criteria。

    两处口径必须分开：计数问的是「这条条款有没有证据」，判定问的是「这一扇算不算交付」。
    混用会让一扇失败窗口带着通过的 build 判词，把证据数抬高（20261021-195220 空转扇）。"""
    rows: list[dict] = [item for item in (state.data.get("criteria") or []) if isinstance(item, dict)]
    if not rows:
        for entry in state.entries():
            rows.extend(
                item for item in ((entry.get("window") or {}).get("criteria") or [])
                if isinstance(item, dict)
            )
    return rows


def _scope_index(state: _RunState) -> tuple[dict[str, str], list[dict]]:
    """窗口 id → 范围 id，外加范围清单。矩阵归因用：一个矩阵项要能说出它属于哪些范围。"""
    scopes = [scope for scope in (state.data.get("scopes") or []) if isinstance(scope, dict)]
    owner: dict[str, str] = {}
    for scope in scopes:
        sid = str(scope.get("id") or "").strip()
        if not sid:
            continue
        for wid in scope.get("window_ids") or []:
            text = str(wid).strip()
            if text:
                owner.setdefault(text, sid)
    return owner, scopes


def _scopes_of_windows(owner: dict[str, str], window_ids) -> list[str]:
    """窗口集合 → 去重排序的范围 id 集合。没归到任何范围的窗口不贡献范围（不瞎猜）。"""
    out: list[str] = []
    for wid in window_ids or []:
        sid = owner.get(str(wid).strip())
        if sid and sid not in out:
            out.append(sid)
    return sorted(out)


def _evidence_count_for(criteria: list[dict], window_ids) -> int:
    """这个矩阵项背后的「已通过条款」条数。按条款绑定的窗口取交集，不另起一套统计。"""
    wanted = {str(wid) for wid in (window_ids or []) if str(wid).strip()}
    if not wanted:
        return 0
    count = 0
    for item in criteria:
        if not item.get("passed"):
            continue
        owners = {str(wid) for wid in (item.get("windows") or []) if str(wid).strip()}
        if owners & wanted:
            count += 1
    return count


def _item_verdict(item: dict, *, has_gap: bool = False) -> str:
    """矩阵项四态（D4）：passed / failed / not_applicable / no_evidence，互斥且穷尽。

    注意这是**加法**：`ok` 的语义一个字都不动（None 仍是「不适用」、仍不撑成功）。
    四态只多给读者一个「为什么是 None」的出口——「本次没发生」与「该交付却没有任何证据」
    在 `ok` 里都写作 None，但前者不需要人管，后者必须有人管。

    `no_evidence` 只给「交付验收」：有缺口范围时那一项记 None（不投票，见
    _delivery_acceptance_item），但它的 None 语义是**整段没交付**，不是「本次没发生」。"""
    if item.get("ok") is True:
        return "passed"
    if item.get("ok") is False:
        return "failed"
    if has_gap and str(item.get("item") or "") == "交付验收":
        return "no_evidence"
    return "not_applicable"


def _attribute_matrix(state: _RunState, matrix: list[dict]) -> list[dict]:
    """给矩阵每项补 evidence_count 与 scope_ids，并把内部用的 _windows 摘掉。

    D5：矩阵项原本只有 {item, ok, note}，没有「这条结论由哪些证据撑着、撑了几条」。
    没有计数就只能靠布尔推断，两个历史极端（全不适用⇒通过 / 全不适用⇒失败）都是从
    同一个空洞里长出来的。计数一律从条款派生（§7 不做清单第 10 条：不许另起一套真值）。

    交付验收项的范围归因要额外并入缺口范围：缺口范围一扇窗口都没有，它不可能是任何
    窗口的 owner，但它正是「没交付」的当事人（本 run 的 B/C）。"""
    owner, _scopes = _scope_index(state)
    criteria = _all_criteria(state)
    gaps = [str(scope.get("id") or "") for scope in _capability_gap_scopes(state)]
    gaps = [sid for sid in gaps if sid]
    out: list[dict] = []
    for item in matrix:
        row = dict(item)
        windows = [str(wid) for wid in (row.pop("_windows", None) or []) if str(wid).strip()]
        if str(row.get("item") or "") == "交付验收":
            rows = _delivery_criteria(state)
            same = [one for one in criteria if one in rows]
            row["evidence_count"] = len([one for one in same if one.get("passed")])
            scope_ids = _scopes_of_windows(owner, windows)
            for sid in gaps + _failed_scope_ids(state):
                if sid not in scope_ids:
                    scope_ids.append(sid)
            row["scope_ids"] = sorted(scope_ids)
        else:
            row["evidence_count"] = _evidence_count_for(criteria, windows)
            row["scope_ids"] = _scopes_of_windows(owner, windows)
        out.append(row)
    return out


def _undelivered_failure_note(state: _RunState) -> str:
    """失败窗口所属范围。没有范围 id 时用窗口 id，避免交付验收把它们当成已交付。"""
    owner, _scopes = _scope_index(state)
    failed_now, _paused = _undelivered_entries(state, {})
    labels: list[str] = []
    for entry in failed_now:
        wid = str(entry.get("id") or "")
        sid = owner.get(wid, "")
        label = f"{sid}（{wid}）" if sid else wid
        if label and label not in labels:
            labels.append(label)
    if not labels:
        return ""
    return "有范围没交付：" + "、".join(labels[:4])


def _failed_scope_ids(state: _RunState) -> list[str]:
    owner, _scopes = _scope_index(state)
    failed_now, _paused = _undelivered_entries(state, {})
    found: list[str] = []
    for entry in failed_now:
        sid = owner.get(str(entry.get("id") or ""), "")
        if sid and sid not in found:
            found.append(sid)
    return found


def _delivery_acceptance_item(state: _RunState) -> dict:
    """集成矩阵第六项「交付验收」四态（20261024-192858 的镜像缺陷：真做成了却被记假失败）。

    顺序：缺口 → 没条款 → 有未过条款 → 有失败窗口的范围 → 全过。
    缺口范围（整段没交付）优先判 None：那是「没做」，已由 contract_status=未完成 与集成
    verdict=no_evidence 点名；这里判 False 会把「没做」和「做坏了」混一起，也会吃掉 no_evidence 三态。
    没条款判 None：一条都没声称的东西不算验过，不许撑成功（五项空转全绿那条教训）。
    有条款 passed=False 判 False：真失败要看得见。全过且无缺口判 True：真做成的不许被记假失败。"""
    gaps = _capability_gap_scopes(state)
    rows = _delivery_criteria(state)
    if gaps:
        shown = "、".join(str(scope.get("id") or "") for scope in gaps[:4])
        return {"item": "交付验收", "ok": None,
                "note": f"有范围没交付，交付验收不适用（见缺口）：{shown}"}
    # 20261028-155214 假成功守卫：范围有窗但部分交付项没被任何窗口接住——
    # 这是「声称了却没交付」，判 False，不许静默丢给全绿矩阵。
    partial = [
        scope for scope in (state.data.get("scopes") or [])
        if isinstance(scope.get("partial_gap"), dict) and scope.get("partial_gap", {}).get("deliverables")
    ]
    if partial:
        bits = []
        for scope in partial[:4]:
            items = [str(one) for one in (scope["partial_gap"].get("deliverables") or [])][:2]
            bits.append(f"{scope.get('id')}（{'；'.join(items)}）")
        return {"item": "交付验收", "ok": False,
                "note": "有交付项没被任何窗口接住：" + "；".join(bits)}
    if not rows:
        return {"item": "交付验收", "ok": None, "note": "本次没有产出任何条款，交付验收不适用"}
    failed = [str(item.get("id") or "") for item in rows if not item.get("passed")]
    if failed:
        return {"item": "交付验收", "ok": False, "note": "条款未过：" + "、".join(failed[:4])}
    # 已有条款可以全过，同时另一个范围的窗口失败且没有自己的条款（20261026-162919：
    # 编译和建关卡过了，FPS 那一扇失败，交付验收仍写「没有整段没交付的范围」）。
    undelivered = _undelivered_failure_note(state)
    if undelivered:
        return {"item": "交付验收", "ok": False, "note": undelivered}
    return {"item": "交付验收", "ok": True,
            "note": f"{len(rows)} 条条款全部通过，且没有整段没交付的范围"}


def _note_with_named(note: str, bits: list[str]) -> str:
    """把点名片段插进收尾 note（在「集成不调用模型。」之前），原来的「完成 W1、W2」「等待外部」
    这些信息保留不动。"""
    text = str(note or "")
    extra = "；".join(str(bit) for bit in bits if str(bit).strip()) + "。"
    tail = "集成不调用模型。"
    if text.endswith(tail):
        return text[: -len(tail)] + extra + tail
    return text + extra


def _entry_note(entry: dict) -> str:
    """一扇没交付的原因：取结果/错误里最短的那句，一句话点名用。"""
    result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
    text = str(
        result.get("summary") or result.get("error")
        or entry.get("error") or entry.get("summary") or ""
    ).strip()
    return (text or "没有说明")[:60]


def _undelivered_entries(state: _RunState, self_entry: dict) -> tuple[list[dict], list[dict]]:
    """真失败扇与暂停扇（都排除集成扇自己；失败扇再排除被处置扇豁免的那些）。"""
    entries = state.entries()
    integrate_ids = {
        str(item.get("id")) for item in entries if str(item.get("action")) == INTEGRATE_ACTION
    }
    integrate_ids.add(str((self_entry or {}).get("id") or ""))
    excused = excused_dispose_parents(entries)
    failed = [
        item for item in entries
        if item.get("state") == "failed"
        and str(item.get("id")) not in integrate_ids
        and str(item.get("id")) not in excused
    ]
    paused = [item for item in entries if item.get("state") == "paused"]
    return failed, paused


def _scope_qualifier(state: _RunState) -> str:
    """「矩阵全部通过」后面补一句范围限定，免得和收尾 note 打架。

    只改文案，不投票：等外部/waiting_external **不进**交付否决（run 级记部分完成 / exit 1 的
    既有口径是对的）。矩阵项只说明运行期鲁棒性行为对，范围没完成照样要说清楚。
    （20261024 T3 证伪：一份记录里同时出现「矩阵全部通过」与「有范围没完成」。）"""
    try:
        from agent.harness import scope_marks

        rows = [scope for scope in (state.data.get("scopes") or []) if isinstance(scope, dict)]
        marks = [item for item in scope_marks(rows) if not item.get("ok")]
    except Exception:
        return ""
    if not marks:
        return ""
    waiting = [item for item in marks if "等待外部" in str(item.get("reason") or "")]
    others = [item for item in marks if item not in waiting]
    bits: list[str] = []
    if waiting:
        bits.append("另有范围等待外部：" + "、".join(str(item.get("id") or "") for item in waiting[:6]))
    if others:
        bits.append("另有范围未完成：" + "、".join(str(item.get("id") or "") for item in others[:6]))
    return "；" + "；".join(bits) if bits else ""


def _undelivered_rows(scope_view: list[dict], failed_now: list[dict], paused_now: list[dict]) -> list[dict]:
    """D9：未交付清单的机读出口。用户看到的不该只是「部分完成」四个字。

    真值源只有一个：`scope_marks` 的 mark（缺口/没完成/等外部/条款未过都由它给 reason），
    窗口级失败与暂停补在对应范围后面。这里**不新增统计**（§7 不做清单第 10 条），
    只把已经算出来的东西排成「哪个范围、缺什么、为什么」。

    归到范围：靠 scope_view 的 window_ids；窗口没归到任何范围时单独出一条 scope=""。"""
    marks = []
    try:
        from agent.harness import scope_marks

        marks = scope_marks(scope_view)
    except Exception:
        marks = []
    by_id = {str(item.get("id") or ""): item for item in marks if str(item.get("id") or "")}
    rows: list[dict] = []
    for scope in scope_view or []:
        sid = str(scope.get("id") or "").strip()
        if not sid:
            continue
        mark = by_id.get(sid) or {}
        if mark.get("ok"):
            continue
        deliver = [str(item) for item in (scope.get("deliverables") or []) if str(item).strip()]
        row = {
            "scope": sid,
            "title": str(scope.get("title") or ""),
            "deliverables": deliver,
            "reason": str(mark.get("reason") or "没有可执行的窗口，只记待办"),
            "gap": bool(mark.get("gap")),
            "windows": [],
        }
        if mark.get("gap"):
            row["missing_actions"] = [
                str(item) for item in (mark.get("missing_actions") or []) if str(item).strip()
            ]
        rows.append(row)
    index = {str(row.get("scope") or ""): row for row in rows}
    owner: dict[str, str] = {}
    for scope in scope_view or []:
        sid = str(scope.get("id") or "").strip()
        for wid in scope.get("window_ids") or []:
            if sid and str(wid).strip():
                owner.setdefault(str(wid).strip(), sid)
    for entry in failed_now + paused_now:
        wid = str(entry.get("id") or "")
        sid = owner.get(wid, "")
        text = _entry_note(entry)
        if sid and sid in index:
            index[sid]["windows"].append({"id": wid, "note": text})
            continue
        rows.append({
            "scope": sid, "title": "", "deliverables": [], "reason": text,
            "gap": False, "windows": [{"id": wid, "note": text}],
        })
    return rows


def _undelivered_text(rows: list[dict]) -> str:
    """人读的「未交付」段：一行一个范围，写清缺什么与为什么。"""
    bits: list[str] = []
    for row in rows or []:
        sid = str(row.get("scope") or "").strip()
        head = f"{sid}（未交付）" if sid else "（未归属范围的窗口）"
        why = str(row.get("reason") or "")
        deliver = [str(item) for item in (row.get("deliverables") or []) if str(item).strip()]
        piece = head + "：" + why
        if deliver:
            piece += "；缺：" + "、".join(deliver[:4])
        missing = [str(item) for item in (row.get("missing_actions") or []) if str(item).strip()]
        if missing:
            piece += "；缺动作：" + "、".join(missing[:4])
        for win in row.get("windows") or []:
            piece += f"；{win.get('id')}：{win.get('note')}"
        bits.append(piece)
    return "\n".join("- " + bit for bit in bits)


def _run_integrate(entry: dict, state: _RunState, query: str, artifacts: dict, events: _EventBus) -> dict:
    """集成矩阵验收：代码判定，不叫模型。子判定一项一个；产出哈希、汇总与归档。"""
    from agent.run_task import new_thread_id

    entries = state.entries()
    matrix: list[dict] = []

    # 矩阵口径（20261021-195220 修）：只有「真的验过且通过」才算 ok。
    # 「本次没发生」记 ok=None（不适用），不再当成通过——五项空转全绿曾给一句什么都没做的 run 加了「矩阵全部通过」。
    # 1 重复请求幂等：重跑最近一扇成功的只读裁决，结论不变才算过
    judges = [
        item for item in entries
        if str(item.get("action")) in ("judge_pickup", "check_pickup") and item.get("state") == "done"
    ]
    item = {"item": "重复请求幂等", "ok": None, "note": "", "_windows": []}
    if judges:
        last = judges[-1]
        # 归因（D5）：这一项的证据只能来自被复验的那一扇，别的窗口的条款不算
        item["_windows"] = [str(last.get("id") or "")]
        try:
            task = _prepare_task(last.get("window") or {}, query, artifacts)
            task["id"] = f"{last['id']}-reverify"
            result = _run_one(task, new_thread_id(), str(last.get("action") or "judge_pickup"))
            before = (last.get("result") or {}).get("judge")
            after = result.get("judge")
            if result.get("ok") and after == before:
                item["ok"] = True
                item["note"] = f"复验一致：{after or str(result.get('summary') or '')[:40]}"
            else:
                item["ok"] = False
                item["note"] = "复验不一致或失败：" + str(result.get("summary") or result.get("error") or "")[:80]
        except Exception as exc:
            item["ok"] = False
            item["note"] = f"复验异常：{exc}"[:100]
    else:
        item["note"] = "没有可复验的只读裁决，本次不适用"
    matrix.append(item)

    # 2 拒答路径：被拒/表外/暂停的窗口要停在暂停，不带成功结果
    refused = [item for item in entries if item.get("state") == "paused"]
    matrix.append({
        "item": "拒答路径",
        "ok": True if refused else None,
        "note": (
            "；".join(item["id"] for item in refused)
            if refused and all(not ((item.get("result") or {}).get("ok")) for item in refused)
            else ("；".join(item["id"] for item in refused) + " 带了成功结果" if refused else "本次没有拒答窗口，不适用")
        ),
        "_windows": [str(item.get("id") or "") for item in refused],
    })

    # 3 延迟窗口：带重试的窗口最终要有明确终态
    delayed = [item for item in entries if int(item.get("retries") or 0) > 0]
    matrix.append({
        "item": "延迟窗口",
        "ok": (all(item.get("state") in ("done", "failed", "paused", "skipped") for item in delayed)
               if delayed else None),
        "note": "；".join(f"{item['id']}×{item.get('retries')}" for item in delayed) or "本次没有重试，不适用",
        "_windows": [str(item.get("id") or "") for item in delayed],
    })

    # 4 断线重连：出现过断线的窗口最终要跑完
    hit = [item for item in entries if _looks_disconnected(item)]
    if not hit:
        matrix.append({"item": "断线重连", "ok": None, "note": "本次无断线，不适用", "_windows": []})
    else:
        recovered = all(item.get("state") == "done" for item in hit)
        matrix.append({
            "item": "断线重连", "ok": recovered,
            "note": ("断线后重连成功：" if recovered else "断线未恢复：") + "；".join(item["id"] for item in hit),
            "_windows": [str(item.get("id") or "") for item in hit],
        })

    # 5 未知态恢复：run 结束时每扇都有明确状态，没有悬在半路的
    # 集成扇自检时自己是 running，除名自己，别把自己判成悬在半路（2026-10-15 未过 W5=running 修的）
    stuck = [
        item for item in entries
        if item["id"] != entry["id"]
        and item.get("state") not in _TERMINAL_STATES
        and item.get("state") != "queued"
    ]
    matrix.append({
        "item": "未知态恢复",
        # 这一项查的是「每扇都有明确终态」：真卡住(running/retry/revising/interrupted) ⇒ False；
        # 没有卡住的 ⇒ None（不适用，不占 checked 名额）。写成 True if stuck else None 会把语义
        # 反成「卡住就算过」，还占一个 checked 名额去撑「矩阵全部通过」（20261024 T3 证伪）。
        "ok": False if stuck else None,
        "note": "；".join(f"{item['id']}={item.get('state')}" for item in stuck) or "每扇都有明确终态或未开始",
        "_windows": [str(item.get("id") or "") for item in stuck],
    })

    # 6 交付验收（第六项，20261024-192858 的镜像缺陷）：前五项全是运行期鲁棒性行为，
    # 一个只有 launch_editor + create_level、条款全过的 run 五项全 None ⇒ checked 为空 ⇒
    # 真做成了却被记假失败。这一项四态见 _delivery_acceptance_item。
    matrix.append(_delivery_acceptance_item(state))

    hashes = _touched_hashes(state)
    totals = _run_totals(state)
    # D5：归档前先补归因字段，归档件与 run json 才是同一份矩阵（归档保真是 D10 的既有断言）
    matrix = _attribute_matrix(state, matrix)
    archive = _archive_run(state, hashes=hashes, matrix=matrix, written_by="integrate")
    # ok=None 是「不适用」，不算失败也不撑成功。一条实打实的都没验时整句不能是「全过」。
    # 聚合层再加「交付否决」（20261024 T9 / T3 证伪）：五项里「拒答路径」「延迟窗口」只说明
    # 鲁棒性行为对（拒答得住、重试有终态），**不代表交付完成**——失败扇也算「有终态」。
    # 所以缺口 / 真失败扇 / 暂停扇任一存在就否决「矩阵全部通过」，不能让它们凑出假通过。
    checked = [one for one in matrix if one.get("ok") is not None]
    matrix_ok = all(one.get("ok") for one in checked) if checked else False
    gaps = _capability_gap_scopes(state)
    failed_now, paused_now = _undelivered_entries(state, entry)
    veto: list[str] = []
    if gaps:
        veto.append(
            "有范围整段没交付（能力缺口）——"
            + "、".join(f"{scope.get('id')}（{_capability_gap_text(scope)}）" for scope in gaps[:6])
        )
    if failed_now:
        veto.append("有窗口失败：" + "、".join(
            f"{item.get('id')}（{_entry_note(item)}）" for item in failed_now[:6]
        ))
    if paused_now:
        veto.append("有窗口暂停：" + "、".join(
            f"{item.get('id')}（{_entry_note(item)}）" for item in paused_now[:6]
        ))
    verdict = ""
    if veto:
        ok = False
        label = "矩阵未全过：" + "；".join(veto)
        if gaps:
            # 缺口不再要求 checked 为空：只要整段没交付就是 no_evidence（T2 三态口径不变）
            verdict = "no_evidence"
    elif not checked:
        ok = False
        label = "矩阵没有可验证的项，不算全过"
    elif matrix_ok:
        ok = True
        label = "矩阵全部通过" + _scope_qualifier(state)
    else:
        ok = False
        label = "矩阵未全过"
    skipped = len(matrix) - len(checked)
    # D4：逐项四态（加法，不动 ok 语义）。缺口范围存在时「交付验收」的 None 记 no_evidence，
    # 其余 None 一律 not_applicable —— 这样读者不用看 ok 布尔去猜「为什么是 None」。
    gaps_now = bool(gaps)
    for one in matrix:
        one["verdict"] = _item_verdict(one, has_gap=gaps_now)
    evidence_total = sum(int(one.get("evidence_count") or 0) for one in matrix)
    no_evidence_ids = [
        one.get("item") for one in matrix if one.get("verdict") == "no_evidence"
    ]
    return {
        "id": entry["id"],
        "ok": ok,
        "verdict": verdict,
        "item_verdicts": {str(one.get("item") or ""): str(one.get("verdict") or "") for one in matrix},
        "no_evidence_items": no_evidence_ids,
        "evidence_total": evidence_total,
        "summary": label + f"；{len(checked)} 项通过口径、{skipped} 项不适用；触及文件 {len(hashes)} 个；归档 {archive}",
        "matrix": matrix,
        "hashes": hashes,
        "totals": totals,
        "archive": str(archive),
    }


def _touched_hashes(state: _RunState) -> list[dict]:
    """触及文件哈希清单：快照前后字节不一样的文件，外加编辑器 DLL。只输出哈希，不读内容进上下文。"""
    out: list[dict] = []
    seen: set[str] = set()
    for entry in state.entries():
        snap = entry.get("snapshot") or {}
        for rel, sha in (snap.get("files") or {}).items():
            if rel in seen:
                continue
            seen.add(rel)
            path = UE_SOURCE_ROOT / rel
            if not path.is_file():
                out.append({"path": rel, "sha256": None, "note": "文件已不在"})
                continue
            current = _sha256_file(path)
            if current != sha:
                out.append({"path": rel, "sha256": current, "note": "已修改"})
    if any(
        str(entry.get("action")) in ("edit_cpp", "compile_project") and entry.get("state") == "done"
        for entry in state.entries()
    ):
        dll = UE_PROJECT_DIR / "Binaries" / "Win64" / f"UnrealEditor-{project_module()}.dll"
        if dll.is_file():
            out.append({
                "path": dll.relative_to(UE_PROJECT_DIR).as_posix(),
                "sha256": _sha256_file(dll),
                "note": "二进制",
            })
    return out


def _run_totals(state: _RunState) -> dict:
    """窗口 elapsed/retry/token 汇总。token 从每扇的 trace 文件合计，没有就记 0。"""
    elapsed = 0.0
    retries = 0
    prompt = 0
    completion = 0
    for entry in state.entries():
        try:
            elapsed += float(entry.get("elapsed") or 0)
        except (TypeError, ValueError):
            pass
        try:
            retries += int(entry.get("retries") or 0)
        except (TypeError, ValueError):
            pass
        trace = (entry.get("detail") or {}).get("trace")
        if trace and Path(str(trace)).is_file():
            for line in Path(str(trace)).read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    prompt += int(row.get("prompt_tokens") or 0)
                    completion += int(row.get("completion_tokens") or 0)
                except (TypeError, ValueError):
                    pass
    return {"elapsed": round(elapsed, 1), "retries": retries, "prompt_tokens": prompt, "completion_tokens": completion}


def _copy_file_quietly(src: Path, dst: Path) -> None:
    for attempt in range(5):
        try:
            shutil.copyfile(src, dst)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    shutil.copyfile(src, dst)


def _archive_run(state: _RunState, hashes=None, matrix=None, md_path=None, *, written_by: str = "") -> Path:
    """日志与报告归档：run json、事件流、md 记录、哈希与矩阵都收进 data/archive/<run_id>/。

    证据合并语义（20261024-192858：集成归档里的 matrix/hashes 被收尾那次归档覆盖成空）：
    hashes/matrix 传 None 表示「这一次没给证据」，读回已存在的 deliverables.json 保住原有内容；
    显式传 [] 才是「明确要给空」，不被合并逻辑吃掉。written_by 只记本次写入来源
    （integrate / finalize）；老归档件没有这个字段也能读回，未知字段一并保留，不再静默丢。"""
    folder = ARCHIVE_DIR / state.run_id
    folder.mkdir(parents=True, exist_ok=True)
    if state.path.is_file():
        _copy_file_quietly(state.path, folder / state.path.name)
    events = EVENTS_DIR / f"{state.run_id}.jsonl"
    if events.is_file():
        _copy_file_quietly(events, folder / events.name)
    if md_path is not None and Path(md_path).is_file():
        _copy_file_quietly(Path(md_path), folder / Path(md_path).name)
    target = folder / "deliverables.json"
    previous: dict = {}
    if target.is_file():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            previous = loaded  # 老归档件可能是任意旧形状：不是对象就当没有
    kept_hashes = hashes if hashes is not None else previous.get("hashes")
    kept_matrix = matrix if matrix is not None else previous.get("matrix")
    payload = dict(previous)
    payload["run_id"] = state.run_id
    payload["hashes"] = list(kept_hashes) if isinstance(kept_hashes, (list, tuple)) else []
    payload["matrix"] = list(kept_matrix) if isinstance(kept_matrix, (list, tuple)) else []
    payload["written_by"] = str(written_by or previous.get("written_by") or "")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return folder


def _index_stale_hint(entries: list[dict]) -> str:
    """P5 索引一致性（20261015-1705）：本 run 真新建了蓝图资产时给一句提示。
    已有资产（「不重复新建」）不算新建，不提示。只提示不自动跑：
    shortcuts\\asset.bat 走无界面编辑器，要先关编辑器，由人执行。"""
    for entry in entries:
        if str(entry.get("action") or "") != "create_blueprint" or entry.get("state") != "done":
            continue
        text = str((entry.get("result") or {}).get("summary") or "")
        if "不重复新建" in text:
            continue
        return (
            "本 run 新建了蓝图资产，资产索引还是旧的（L2 归地的 linked 分支依赖索引新鲜度）。"
            "关掉编辑器后运行 shortcuts\\asset.bat 重建资产索引；要关编辑器，由人执行。"
        )
    return ""


# ---------- 人读记录 ----------

def _write(path, query: str, source: str, rows: list[dict], integrated: dict, *, pie: bool, extra=None) -> None:
    lines = [
        f"# {path.stem}",
        "",
        f"原话：{query}",
        f"来源：{_SOURCE_LABEL.get(source, source)}",
        f"策略：保存关卡={'开' if default_save() else '关'}；本 run PIE={'进入' if pie else '未进入'}。",
        "",
    ]
    for row in rows:
        lines.append(f"## {row['id']}. {row['title']}")
        lines.append(f"- 动作：{row.get('action') or '无'}")
        if row.get("supplement"):
            lines.append("- 来源：补扇")
        deps = row.get("depends_on") or []
        lines.append("- 依赖：" + ("、".join(deps) if deps else "无"))
        lines.append(
            "- 策略：保存关卡=" + ("是" if row.get("save") else "否")
            + "；PIE=" + ("是" if row.get("pie") else "否")
        )
        lines.append(f"- 状态：{row['status']}")
        if row.get("result"):
            lines.append("- 结果：" + json.dumps(row["result"], ensure_ascii=False))
        lines.append("")
    lines.append("## 集成")
    lines.append(f"- 状态：{integrated.get('status') or ('成功' if integrated.get('ok') else '失败')}")
    lines.append(f"- 说明：{integrated.get('summary')}")
    for line in extra or []:
        lines.append(str(line))
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _rows(state: _RunState) -> list[dict]:
    rows = []
    for entry in state.entries():
        result = entry.get("result")
        if entry.get("state") == "queued" and entry.get("summary"):
            result = {"id": entry["id"], "ok": False, "summary": entry["summary"]}
        elif entry.get("state") == "paused" and result is None:
            text = str(entry.get("error") or entry.get("summary") or "")
            if text:
                result = {"id": entry["id"], "ok": False, "summary": text}
        rows.append({
            "id": entry["id"],
            "title": entry["title"],
            "action": entry.get("action") or "无",
            "depends_on": list(entry.get("depends_on") or []),
            "save": bool((entry.get("window") or {}).get("save", default_save())),
            "pie": bool((entry.get("window") or {}).get("pie", default_pie())),
            "status": _MD_STATUS.get(entry.get("state"), str(entry.get("state"))),
            "result": result,
            "supplement": bool(
                entry.get("supplement") or (entry.get("window") or {}).get("supplement")
            ),
        })
    return rows


# ---------- 主执行 ----------

def _emit_action_candidates(events, planned: dict, windows: list) -> None:
    """表外动作只记候选事件，不交给执行器。"""
    names = []
    for name in (planned or {}).get("action_candidates") or []:
        text = str(name or "").strip()
        if text and text not in names:
            names.append(text)
    for window in windows or []:
        if not isinstance(window, dict):
            continue
        text = str(window.get("action_candidate") or "").strip()
        if text and text not in names:
            names.append(text)
    for name in names:
        events.emit("action_candidate", action=name, status="review")


def _start_windows(entries) -> list[dict]:
    """run_start 带上已经算好的状态。不带的话面板会把暂停扇画成排队。"""
    return [{
        "id": entry["id"],
        "title": entry["title"],
        "action": entry.get("action"),
        "depends_on": list(entry.get("depends_on") or []),
        "state": entry.get("state") or "queued",
    } for entry in entries]


def _dependency_met(dep_state: str, action: str) -> bool:
    """上游完成才算满足。只读 Automation 在上游等待外部或已跳过时仍可跑，不跟着停。
    处置扇的上游是已经结束的 Automation 残差，失败也算交接到了。"""
    if dep_state == "done":
        return True
    if action == DISPOSE_ACTION and dep_state == "failed":
        return True
    return action == "run_automation_test" and dep_state in ("waiting_external", "skipped")


def _empty_deliverable_text(entry: dict) -> bool:
    blob = " ".join(str(entry.get(key) or "") for key in ("error", "summary"))
    return "交付清单为空" in blob


def _empty_skip_satisfies(entry: dict) -> bool:
    """交付清单为空被自动跳过：没有新工作，下游不用等，也不问人补路径。"""
    return str(entry.get("state") or "") == "skipped" and _empty_deliverable_text(entry)


def _unfilled_slot_text(entry: dict) -> bool:
    blob = " ".join(str(entry.get(key) or "") for key in ("error", "summary"))
    window = entry.get("window") if isinstance(entry.get("window"), dict) else {}
    blob += " " + str(window.get("error") or "")
    return "缺少槽位" in blob


def _path_reply_can_fill(entry: dict) -> bool:
    """这扇暂停能不能被 r /Game 或 r Public/类名.h 解开。不能的话问路径只是空转。"""
    window = entry.get("window") if isinstance(entry.get("window"), dict) else {}
    if window.get("task"):
        return True
    action = str(entry.get("action") or window.get("action") or "")
    if action in (
        "bind_blueprint", "place_actor", "place_blueprint", "compile_blueprint",
        "create_blueprint", "edit_cpp", "create_level", "open_level",
    ):
        return True
    err = str(entry.get("error") or window.get("error") or "")
    return "只给一次：r " in err or "请用 r " in err


def _unfilled_skip_satisfies(entry: dict) -> bool:
    """缺槽位被跳过：路径填不进去，下游不用等这扇。"""
    return (
        str(entry.get("state") or "") == "skipped"
        and _unfilled_slot_text(entry)
        and "跳过" in str(entry.get("summary") or "")
    )


def release_empty_deliverables(entries: list[dict]) -> list[str]:
    """这些暂停不是缺路径。改成跳过并写明原因，调用方不要再弹出 s/c/r。"""
    notes = []
    for entry in entries or []:
        if entry.get("state") != "paused" or not entry.get("action"):
            continue
        if not _empty_deliverable_text(entry):
            continue
        note = "交付清单为空，没有新工作，跳过。不用补路径。"
        entry.update(state="skipped", ok=False, summary=note)
        notes.append(f"{entry.get('id')} {note}")
    return notes


_FILTER_NAME_MARK = "只给一次：r 过滤器全名"
_PATH_PAUSE_HINT = "s 停止 / c 跳过 / r /Game/目录 或 r Public/类名.h"
_FILTER_PAUSE_HINT = "s 停止 / c 跳过 / r 过滤器全名"


def _filter_name_pause(*items: dict) -> bool:
    """这扇暂停要的是过滤器全名，不是 /Game 或头文件。"""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        parts.append(str(item.get("error") or ""))
        parts.append(str(item.get("summary") or ""))
        nested = item.get("window")
        if isinstance(nested, dict):
            parts.append(str(nested.get("error") or ""))
    return _FILTER_NAME_MARK in " ".join(parts)


def _pause_hint(*items: dict) -> str:
    if _filter_name_pause(*items):
        return _FILTER_PAUSE_HINT
    return _PATH_PAUSE_HINT


def release_unfilled_slots(entries: list[dict]) -> list[str]:
    """缺的是动作槽位，不是 /Game 或源文件。问 r 路径写不回去，直接跳过。
    「过滤器不是唯一」要的是全名，留在人工裁决里，不算这一类。"""
    notes = []
    for entry in entries or []:
        if entry.get("state") != "paused" or not entry.get("action"):
            continue
        if _filter_name_pause(entry):
            continue
        if not _unfilled_slot_text(entry) or _path_reply_can_fill(entry):
            continue
        note = str(entry.get("error") or "缺少槽位") + " 补路径填不进去，跳过。不用填 /Game 或源文件。"
        entry.update(state="skipped", ok=False, summary=note)
        notes.append(f"{entry.get('id')} {note}")
    return notes


def excused_dispose_parents(entries) -> set[str]:
    """处置扇全部成功时，被它接走的失败扇不再把整句打成失败。"""
    children: dict[str, list] = {}
    for entry in entries or []:
        if str(entry.get("action") or "") != DISPOSE_ACTION:
            continue
        for dep in entry.get("depends_on") or []:
            children.setdefault(str(dep), []).append(entry)
    return {
        dep for dep, kids in children.items()
        if kids and all(item.get("state") == "done" and item.get("ok") for item in kids)
    }


_EVIDENCE_SKIP_ACTIONS = frozenset({
    "launch_editor", "query_actor", "query_material", "run_automation_test",
    "run_automation_tests", "judge_pickup", "check_pickup", "integrate_verify",
    "dispose_residual", "compile_project", "open_level",
})
_WRITE_EDITOR_KINDS = frozenset({"editor_assert", "blueprint_compile", "widget_graph"})


def _window_change_evidence(entry: dict) -> bool:
    """这一扇有没有留下改动证据。

    spec_files 非空且编译裁判 ok，或写类编辑器裁判 {ok, kind} 对上。
    solve 扇只认前者：automation 通过不算改过东西。
    只读/观察类动作的判词也不算。
    """
    detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
    action = str(entry.get("action") or "")
    spec_files = [item for item in (detail.get("spec_files") or []) if isinstance(item, dict)]
    build = detail.get("build") if isinstance(detail.get("build"), dict) else {}
    if spec_files and build.get("ok") is True and str(build.get("kind") or "cpp_build") == "cpp_build":
        return True
    if action == "solve" or action in _EVIDENCE_SKIP_ACTIONS:
        return False
    for judge in (build, detail.get("test")):
        if not isinstance(judge, dict):
            continue
        if judge.get("ok") is True and str(judge.get("kind") or "") in _WRITE_EDITOR_KINDS:
            return True
    return False


def _scope_has_change_evidence(state: _RunState, scope: dict) -> bool:
    for wid in scope.get("window_ids") or []:
        entry = state.data["windows"].get(str(wid))
        if isinstance(entry, dict) and _window_change_evidence(entry):
            return True
    return False


def execute_windows(query: str, complete=None, echo=print, emit=None, planned=None, windows=None,
                    brief=None, scopes=None, resume_state=None, decide=None, max_workers=3):
    """拆窗口 → 事件流与机读状态 → 只读并行调度 → 矩阵集成 → 台账与归档。
    兼容旧调用：execute_windows(query, complete=..., echo=...)。集成不叫模型。"""
    if resume_state is not None:
        query = str(resume_state.get("query") or query)
        state = _RunState.from_resume(resume_state)
        run_id = state.run_id
        source = str(state.data.get("source") or "resume")
        if brief is None and isinstance(resume_state.get("brief"), dict):
            brief = resume_state["brief"]
        if scopes is None and resume_state.get("scopes"):
            scopes = resume_state.get("scopes")
    else:
        if planned is None:
            planned = plan_windows(query, complete=complete)
        if windows is None:
            windows = planned.get("windows") or []
        windows = [dict(window) for window in windows]
        if planned.get("planner_text"):
            echo("规划者原文：" + str(planned["planner_text"]).replace("\n", " ")[:500])
        source = str(planned.get("source") or ("brief" if brief else ""))
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        mark_dangling(windows)
        state = _RunState(
            run_id, query, source, windows,
            review=(brief or {}).get("review") or {}, brief=brief, scopes=scopes,
        )

    path = runs_md_path(run_id)
    events = _EventBus(run_id, emit=emit)
    from backends.select import bind_run

    bind_run(events.emit)
    if resume_state is None:
        from agent.umg_manual import maybe_write_hud_plan

        manual_note = maybe_write_hud_plan(query)
        if manual_note:
            events.emit("manual_doc", window="", path=manual_note)
            echo(f"UMG 人工步骤已写出：{manual_note}")
    if resume_state is None:
        review = (brief or {}).get("review") or {}
        if str(review.get("status") or "").startswith("通过"):
            events.emit("confirm", window="", option="确认", path="")
    # P4 观测：理解器一开口就记一条 intent 事件（只存决策字段）；run_start 的 source 带 understood
    if isinstance(planned, dict) and planned.get("intent"):
        fields = {key: value for key, value in dict(planned["intent"]).items()}
        fields.pop("source", None)
        events.emit("intent", source=str(planned.get("source") or source), **fields)
    if resume_state is None:
        _emit_action_candidates(events, planned if isinstance(planned, dict) else {}, windows)
    entries = state.entries()

    # 环依赖前置诊断：不拖到集成段
    cycle = find_cycle([
        {"id": entry["id"], "depends_on": list(entry.get("depends_on") or [])}
        for entry in entries
    ])
    if cycle:
        note = "环：" + "→".join(cycle)
        members = set(cycle[:-1])
        for entry in entries:
            if entry["id"] in members:
                entry.update(state="paused", error=note, summary=note, realizable=False)
        events.emit("run_start", query=query, source=source, windows=_start_windows(entries))
        for entry in entries:
            if entry["id"] in members:
                events.emit("window_state", window=entry["id"], state="paused")
        integrated = {"ok": False, "status": "失败", "summary": note + "。环上的窗口暂停，依赖它们的窗口不跑。"}
        state.data["integration"] = integrated
        state.data["status"] = "失败"
        state.save()
        _write(path, query, source, _rows(state), integrated, pie=False)
        events.emit("integration", ok=False, status="失败", summary=integrated["summary"])
        events.emit("run_end", status="失败", ok=False, stop_reason="paused")
        echo(f"\n拆成 {len(entries)} 扇。来源：{_SOURCE_LABEL.get(source, source)}。记录写在 {path}")
        echo(f"\n集成：{integrated['summary']}")
        return 1, path

    if not state.data.get("criteria") and isinstance(planned, dict) and planned.get("criteria"):
        # L1 给的合同随 planned 进来，不能被窗口类型兜底覆盖
        state.data["criteria"] = [dict(item) for item in planned["criteria"] if isinstance(item, dict)]
    if not state.data.get("criteria"):
        try:
            from agent.harness import build_criteria

            state.data["criteria"] = build_criteria(windows=[entry.get("window") or {} for entry in entries])
        except Exception:
            state.data["criteria"] = []
    for scope in state.data.get("scopes") or []:
        if isinstance(scope, dict) and not scope.get("criteria"):
            scoped = [
                (state.window(wid).get("window") or {})
                for wid in (scope.get("window_ids") or [])
                if wid in state.data["windows"]
            ]
            try:
                from agent.harness import build_criteria

                scope["criteria"] = build_criteria(windows=scoped)
            except Exception:
                scope["criteria"] = []
    events.emit("run_start", query=query, source=source, windows=_start_windows(entries))
    if resume_state is not None:
        try:
            from agent.harness import read_handoff

            for entry in entries:
                doc = read_handoff(state.run_id, entry["id"])
                if doc and doc.get("unmet"):
                    events.emit(
                        "handoff", window=entry["id"], unmet=doc.get("unmet"),
                        unmet_total=doc.get("unmet_total"), next=doc.get("next"),
                    )
                    echo(f"\n{entry['id']} 交接未完成：{'；'.join(str(item) for item in doc.get('unmet') or [])}")
        except Exception:
            pass
    state.save()
    echo(f"拆成 {len(entries)} 扇。来源：{_SOURCE_LABEL.get(source, source)}。记录写在 {path}")

    # 暂停窗口的人工裁决：对齐单循环的 s/c/r，只问一次
    stop_all = False
    if decide is not None:
        for entry in entries:
            if entry.get("state") != "paused" or not entry.get("action"):
                continue
            if "未知窗口 id" in str(entry.get("error") or ""):
                continue
            err_text = str(entry.get("error") or entry.get("summary") or "")
            # 交付清单为空：不是缺 /Game 或源文件。问 s/c/r 会让人以为要补路径。
            if _empty_deliverable_text(entry):
                release_empty_deliverables([entry])
                state.save()
                events.emit("window_state", window=entry["id"], state="skipped")
                echo(f"\n{entry['id']} {entry.get('summary')}")
                continue
            # 缺的是 name 这类槽位：提示要的是 /Game 或头文件，填进去也写不回这扇。
            if _unfilled_slot_text(entry) and not _path_reply_can_fill(entry):
                release_unfilled_slots([entry])
                state.save()
                events.emit("window_state", window=entry["id"], state="skipped")
                echo(f"\n{entry['id']} {entry.get('summary')}")
                continue
            # 代码自己否掉的编译扇：不问 s/c/r。那句提示要的是路径，填进去也变不成编译。
            if "没有要编译整个工程" in err_text:
                entry.update(state="skipped", ok=False, summary=err_text)
                state.save()
                events.emit("window_state", window=entry["id"], state="skipped")
                echo(f"\n{entry['id']} 跳过。{err_text}")
                continue
            echo(f"\n{entry['id']} 暂停：{err_text}")
            echo(f"{entry['id']} 等人工裁决，在终端里输入：{_pause_hint(entry)}")
            decision = decide(entry.get("window") or {}, entry)
            option, supplied = confirm_fields(decision)
            events.emit("confirm", window=entry["id"], option=option, path=supplied)
            note = str(decision) if isinstance(decision, str) else ""
            echo(f"{entry['id']} 裁决收到：{note if note else '停止'}")
            if decision == "skip":
                entry.update(state="skipped", summary="人工跳过")
                state.save()
                events.emit("window_state", window=entry["id"], state="skipped")
            elif decision == "stop":
                entry.update(state="paused", summary="人工裁决停止")
                state.save()
                events.emit("window_state", window=entry["id"], state="paused")
                stop_all = True
                break
            else:
                updated = _apply_path(entry.get("window") or {}, decision, query)
                if updated is None:
                    entry.update(state="paused", summary="补路径无效，这一窗仍暂停")
                    state.save()
                else:
                    entry["window"] = updated
                    entry.update(state="queued", realizable=True, error="", summary="")
                    state.save()
                    events.emit("window_state", window=entry["id"], state="revising")
                    events.emit("window_state", window=entry["id"], state="queued")
    for entry in entries:
        if entry.get("state") == "paused":
            echo(f"\n{entry['id']} 暂停。{entry.get('error') or entry.get('summary') or ''}")

    # 续接时先还原上次中断窗口的旧快照，避免半截改动被当成前提
    if resume_state is not None:
        for entry in entries:
            if entry.get("resume_thread") and entry.get("snapshot"):
                _restore_source(run_id, entry["id"], entry["snapshot"])

    pool = _Pool(max_workers)
    pool.start()
    running: dict[str, concurrent.futures.Future] = {}
    interrupted = False
    try:
        while not stop_all:
            if _apply_run_stitch(state, query, events, echo):
                continue
            dispatched = 0
            for entry in state.entries():
                wid = entry["id"]
                if wid in running or entry.get("state") != "queued":
                    continue
                unmet = []
                for dep in entry.get("depends_on") or []:
                    dep_entry = state.data["windows"].get(dep)
                    if not isinstance(dep_entry, dict):
                        unmet.append(dep)
                        continue
                    dep_state = str(dep_entry.get("state") or "")
                    if _dependency_met(dep_state, str(entry.get("action") or "")):
                        continue
                    if _empty_skip_satisfies(dep_entry) or _unfilled_skip_satisfies(dep_entry):
                        continue
                    unmet.append(dep)
                if unmet:
                    note = "依赖 " + "、".join(unmet) + " 未完成，不跑"
                    if entry.get("summary") != note:
                        entry["summary"] = note
                        state.save()
                        echo(f"\n{wid} 未开始。{note}")
                    continue
                if entry.get("realizable") is False:
                    text = str(entry.get("error") or "这一窗暂停")
                    entry.update(state="paused", summary=text)
                    state.save()
                    echo(f"\n{wid} 暂停。{text}")
                    events.emit("window_state", window=wid, state="paused")
                    continue
                running[wid] = pool.submit(_dispatch_window, entry, state, query, events, echo)
                entry.update(state="running", summary="")
                state.save()
                events.emit("window_state", window=wid, state="running")
                dispatched += 1
            if not running and dispatched == 0:
                # 只剩集成扇时放行：矩阵本来就是验失败面的
                bypassed = 0
                for entry in state.entries():
                    if entry.get("state") == "queued" and str(entry.get("action")) == INTEGRATE_ACTION:
                        entry.update(state="running")
                        state.save()
                        running[entry["id"]] = pool.submit(_dispatch_window, entry, state, query, events, echo)
                        events.emit("window_state", window=entry["id"], state="running")
                        bypassed += 1
                if bypassed == 0:
                    break
            if running:
                done, _pending = concurrent.futures.wait(
                    list(running.values()), timeout=0.3,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    wid = next(key for key, value in running.items() if value is future)
                    del running[wid]
                    try:
                        summary = future.result()
                    except BaseException as exc:  # noqa: BLE001 - 记账后按失败处理
                        summary = {"id": wid, "ok": False, "summary": f"{type(exc).__name__}: {exc}", "error": str(exc)}
                    _absorb_result(state, events, echo, wid, summary)
    except KeyboardInterrupt:
        from core.build_job import request_stop

        request_stop()
        interrupted = True
        state.data["status"] = "interrupted"
        state.save()
        echo("\n中断。已完成的窗口保留在机读记录里，可 --resume latest 续跑。")
    finally:
        pool.close()

    # 集成：全 done 才成功；失败扇无下游依赖记部分完成；等待外部不阻塞兄弟范围
    entries = state.entries()
    done_ids = [entry["id"] for entry in entries if entry.get("state") == "done"]
    failed = [entry for entry in entries if entry.get("state") == "failed"]
    waiting = [entry for entry in entries if entry.get("state") == "waiting_external"]
    ok = bool(entries) and len(done_ids) == len(entries)
    dependents: set[str] = set()
    for entry in entries:
        if str(entry.get("action")) == INTEGRATE_ACTION:
            continue
        dependents.update(entry.get("depends_on") or [])
    excused = excused_dispose_parents(entries)
    blocking = [entry for entry in failed if entry["id"] in dependents and entry["id"] not in excused]
    if ok:
        status, note = "成功", "全部窗口完成。集成不调用模型。"
    elif not entries:
        status, note = "失败", "没有窗口。"
    elif interrupted:
        status, note = "中断", "有窗口没跑完。状态已落盘，可 --resume 续跑。"
    elif stop_all:
        status, note = "失败", "人工裁决停止，未跑完的窗口记未开始。集成不调用模型。"
    elif not blocking and (done_ids or waiting):
        status = "部分完成"
        note = "完成 " + ("、".join(done_ids) or "无")
        if failed:
            note += "；失败 " + "、".join(entry["id"] for entry in failed) + "（无下游依赖，不阻塞别的范围）"
        if waiting:
            note += "；等待外部 " + "、".join(
                f"{entry['id']}({entry.get('external_dep') or '?'})" for entry in waiting
            )
        note += "。集成不调用模型。"
    else:
        status, note = "失败", "有窗口没完成。集成不调用模型。"
    try:
        from agent.harness import contract_status, scope_marks

        # 把窗口终态喂进范围判定：窗口没跑完的范围不算完成，和零窗口范围一并算进总状态。
        scope_view = []
        for scope in state.data.get("scopes") or []:
            row = dict(scope) if isinstance(scope, dict) else {}
            ids = [wid for wid in row.get("window_ids") or [] if wid in state.data["windows"]]
            row["window_states"] = [state.data["windows"][wid].get("state") for wid in ids]
            scope_view.append(row)
        adjusted = contract_status(state.data.get("criteria") or [], status, scope_view)
        open_scopes = [item for item in scope_marks(scope_view) if not item.get("ok")]
    except Exception:
        adjusted = status
        scope_view = []
        open_scopes = []
    gap_scopes = [item for item in open_scopes if item.get("gap")]
    others_open = [item for item in open_scopes if not item.get("gap")]
    integrate_ids = {
        str(item.get("id")) for item in entries if str(item.get("action")) == INTEGRATE_ACTION
    }
    failed_now = [
        item for item in entries
        if item.get("state") == "failed"
        and str(item.get("id")) not in integrate_ids
        and str(item.get("id")) not in excused
    ]
    paused_now = [item for item in entries if item.get("state") == "paused"]
    named: list[str] = []
    if gap_scopes:
        named.append("有范围没交付（能力缺口）：" + "、".join(
            f"{item.get('id')}（{item.get('reason')}）" for item in gap_scopes[:6]
        ))
    if others_open:
        named.append("有范围没完成：" + "、".join(
            f"{item.get('id')}（{item.get('reason')}）" for item in others_open[:6]
        ))
    if failed_now:
        named.append("有窗口失败：" + "、".join(
            f"{item.get('id')}（{_entry_note(item)}）" for item in failed_now[:6]
        ))
    if paused_now:
        named.append("有窗口暂停：" + "、".join(
            f"{item.get('id')}（{_entry_note(item)}）" for item in paused_now[:6]
        ))
    stitch_why = str(state.data.get("stitch_unresolved") or "")
    if "不补" in stitch_why:
        named.append(stitch_why)
    if named:
        # 只要有没交付/没完成/失败/暂停，就一律点名（不再被 status == "成功" 挡住）：
        # 缺口范围无论先前 status 是什么都要把 note 补上，暂停扇也不许一个字不提。
        if adjusted in ("未完成", "部分完成"):
            status = adjusted
        ok = False
        note = _note_with_named(note, named)
    elif adjusted == "部分完成" and status == "成功":
        status = adjusted
        ok = False
        note = "自动条款之外仍有人工条款。集成不调用模型。"
    integrated = {"ok": ok, "status": status, "summary": note}

    # 范围行（任务书 run 才有）+ 台账回写
    extra: list[str] = []
    ledger_rows: list[dict] = []
    if scopes:
        marks = {item.get("id"): item for item in scope_marks(scope_view or scopes)}
        bits = []
        for scope in scopes:
            ids = [wid for wid in scope.get("window_ids") or [] if wid in state.data["windows"]]
            states = [state.data["windows"][wid].get("state") for wid in ids]
            verdict = marks.get(str(scope.get("id") or "")) or {}
            if states and all(item == "done" for item in states):
                mark, ledger_status = "完成", "done"
            elif any(item == "done" for item in states):
                mark, ledger_status = "部分完成", "partial"
            elif states and all(item == "waiting_external" for item in states):
                mark, ledger_status = "等待外部", "pending"
            elif any(item == "failed" for item in states):
                mark, ledger_status = "失败", "pending"
            else:
                mark, ledger_status = "未开始", "pending"
            if verdict.get("gap"):
                # 零窗口范围整段没交付：记「未完成（能力缺口）」，别记成「未开始」让人以为还没轮到
                mark, ledger_status = "未完成（能力缺口）", "pending"
            # 窗口都 done 但范围还是没完成（零窗口范围、条款没过、等外部）：
            # 不许记 done，否则台账把「只记待办」的范围读成已经做完。
            if verdict and not verdict.get("ok"):
                ledger_status = "pending"
                if mark == "完成":
                    mark = "未完成"
                if verdict.get("reason") and verdict.get("reason") not in str(scope.get("pending_note") or ""):
                    scope = dict(scope)
                    scope["pending_note"] = (
                        (str(scope.get("pending_note") or "") + " " if scope.get("pending_note") else "")
                        + str(verdict.get("reason"))
                    )
            evidence_gap = ""
            if ledger_status == "done" and not _scope_has_change_evidence(state, scope):
                ledger_status = "partial"
                if mark == "完成":
                    mark = "部分完成"
                evidence_gap = "没有改动证据（源码未落盘编译，也没有写类编辑器裁判）"
            bits.append(f"{scope.get('id')} {mark}")
            pending = [] if ledger_status == "done" else [str(item) for item in scope.get("deliverables") or []]
            if evidence_gap:
                pending.append(evidence_gap)
            note_scope = str(scope.get("pending_note") or "")
            if evidence_gap:
                note_scope = (note_scope + " " if note_scope else "") + evidence_gap
            if scope.get("external_dep"):
                note_scope = (note_scope + " " if note_scope else "") + f"等待外部 {scope.get('external_dep')}"
            ledger_rows.append({
                "id": scope.get("id"), "title": scope.get("title"),
                "status": ledger_status, "pending": pending, "note": note_scope,
            })
        extra.append("- 范围：" + "；".join(bits))
        try:
            from agent import ledger as ledger_mod

            ledger_mod.update_ledger(run_id, ledger_rows)
        except Exception as exc:
            extra.append(f"- 台账回写失败：{exc}")

    # D9：未交付清单。只对「有范围的任务书 run」生成——窄任务没有范围口径，
    # 一行都没归到范围的窗口也照样列出来（scope 为空串），免得再出现「部分完成但看不出少了什么」。
    undelivered: list[dict] = []
    try:
        undelivered = _undelivered_rows(scope_view, failed_now, paused_now)
    except Exception as exc:
        extra.append(f"- 未交付清单生成失败：{exc}")
    if undelivered:
        state.data["undelivered"] = undelivered
        extra.append("- 未交付清单（机读字段 undelivered）：")
        extra.extend(_undelivered_text(undelivered).splitlines())

    # D8：候选表闭环的出声腿。缺口被记成候选却没人管时，收尾必须说一句——
    # 「记了」不等于「有人管」。只出声不阻塞：候选表不许变成阻塞真实 run 的开关。
    try:
        from agent.catalog import CANDIDATE_STALE_DAYS, stale_candidates

        stale = stale_candidates()
    except Exception:
        stale = []
    if stale:
        state.data["stale_candidates"] = [
            {"name": item.get("name"), "age_days": item.get("age_days"), "hits": item.get("hits")}
            for item in stale
        ]
        detail_text = "、".join(
            f"{item.get('name')}（{item.get('age_days')} 天，{item.get('hits') or 1} 次）" for item in stale[:6]
        )
        extra.append(
            f"- 候选动作待处置：{detail_text}。超过 {CANDIDATE_STALE_DAYS} 天仍是 review，"
            "要么 promote（需人审 + verification），要么 dismiss。"
        )
        events.emit("action_candidate_stale", candidates=[
            {"name": item.get("name"), "age_days": item.get("age_days"), "hits": item.get("hits")}
            for item in stale
        ])

    integrate_entry = next(
        (entry for entry in entries if str(entry.get("action")) == INTEGRATE_ACTION), None
    )
    if integrate_entry is not None and isinstance(integrate_entry.get("detail"), dict):
        detail = integrate_entry["detail"]
        for item in detail.get("matrix") or []:
            mark = "不适用" if item.get("ok") is None else ("通过" if item.get("ok") else "未过")
            extra.append(f"- 矩阵 {item.get('item')}：{mark}。{item.get('note') or ''}")
        totals = detail.get("totals") or {}
        if totals:
            extra.append(
                f"- 汇总：elapsed={totals.get('elapsed')} retries={totals.get('retries')} "
                f"prompt_tokens={totals.get('prompt_tokens')} completion_tokens={totals.get('completion_tokens')}"
            )
        hashes = detail.get("hashes") or []
        if hashes:
            extra.append("- 哈希：" + "；".join(
                f"{item.get('path')}={str(item.get('sha256'))[:12]}" for item in hashes[:8]
            ))
        if detail.get("archive"):
            extra.append(f"- 归档：{detail['archive']}")
    # P5 索引一致性（20261015-1705）：真新建了蓝图资产的 run，收尾提示重建资产索引。
    # 只提示不自动跑——shortcuts\asset.bat 要关编辑器，由人执行。续接的 run 不重复提示。
    stale_hint = "" if resume_state is not None else _index_stale_hint(entries)
    if stale_hint:
        extra.append("- " + stale_hint)
        state.data["index_hint"] = stale_hint
    entered_pie = any((entry.get("window") or {}).get("pie") for entry in entries) or "PIE" in query.upper()
    _write(path, query, source, _rows(state), integrated, pie=entered_pie, extra=extra)
    events.emit("integration", ok=ok, status=status, summary=note, scopes=extra or None)
    if stale_hint:
        events.emit("index_stale", note=stale_hint)
        echo(f"\n{stale_hint}")
        try:
            from agent.index_refresh import mark_stale, schedule_index_refresh

            try:
                from core.editor_launch import editor_running

                editor_open = bool(editor_running())
            except Exception:
                editor_open = True
            mark_stale(stale_hint, path=EVENTS_DIR.parent / "index_status.json")
            info = schedule_index_refresh(
                editor_open=editor_open,
                queue_path=EVENTS_DIR.parent / "index_queue.jsonl",
            )
            events.emit(
                "index_auto_refresh",
                mode=info.get("mode"),
                editor_open=editor_open,
                command=info.get("command"),
                note=stale_hint,
            )
        except Exception:
            pass
    if ok and isinstance(planned, dict) and planned.get("intent_full"):
        # P3 L3：整句集成成功才把意图归一成句式模板写回 learned_routes（unresolved 非空不回写）
        learned_id = ""
        try:
            from agent.plan_windows import remember_intent_route

            learned_id = remember_intent_route(query, planned["intent_full"], planned.get("windows") or [], source_run=run_id) or ""
        except Exception:
            learned_id = ""
        if learned_id:
            version = 1
            skill_status = "shadow"
            try:
                from agent.skill_memory import load_skills

                for item in load_skills():
                    if str(item.get("id") or "") == learned_id:
                        version = int(item.get("version") or 1)
                        skill_status = str(item.get("status") or "shadow")
                        break
            except Exception:
                skill_status = "shadow"
            events.emit(
                "learned_route", route_id=learned_id, skill_id=learned_id,
                version=version, status=skill_status, query=query[:120],
            )
            if skill_status == "promoted":
                events.emit(
                    "skill_promoted", skill_id=learned_id, route_id=learned_id,
                    version=version, status="promoted", query=query[:120],
                )
    reasons = [entry.get("stop_reason") for entry in entries if entry.get("stop_reason")]
    try:
        from agent.harness import run_stop

        stop = run_stop(reasons, ok)
    except Exception:
        stop = "ok" if ok else "judge_failed"
    events.emit("run_end", status=status, ok=ok, stop_reason=stop)
    if ok:
        # U6：每次成功 run 收尾做一次零模型蒸馏计数（dry_run 只统计、不落盘、不调教师），
        # distill_candidate 事件让统计页看得到候选池在随真实 run 增长。失败不追尾。
        try:
            from agent.distill import run_distill

            report = run_distill(dry_run=True)
            events.emit(
                "distill_candidate", status="shadow", dry_run=True,
                events=report.get("events"), runs=report.get("runs"),
                counts=json.dumps(report.get("counts") or {}, ensure_ascii=False)[:200],
            )
        except Exception:
            pass
    state.data["integration"] = integrated
    state.data["status"] = "interrupted" if interrupted else status
    state.save()
    if integrate_entry is not None:
        # 这次归档只为把刚写完的最终 md 和最终 run json/jsonl 复制进归档：必须排在 state.save()
        # 之后，否则归档副本停在 status=running（20261024 那次就是，runs 目录里已是最终态）。
        # 不带 hashes/matrix ⇒ 按合并语义保住集成那次落的证据，不再覆盖成空（20261024-192858）。
        _archive_run(state, md_path=path, written_by="finalize")
    echo(f"\n集成：{note}")
    if interrupted:
        return 130, path
    return (0 if ok else 1), path


# ---------- 人工裁决补路径 ----------

def _apply_path(window: dict, raw: str, query: str) -> dict | None:
    """把 r 补的路径写回窗口：规则步骤沿用单循环的填法；规划窗口按槽位重解析。无效返回 None。
    过滤器不唯一时，回复必须与源码里声明的全名逐字一致，不猜大小写或最后一段。"""
    from agent.plan_windows import resolve_name
    from agent.sequence import apply_supplied_path

    window = dict(window)
    if _filter_name_pause(window):
        from agent.supervise import declared_automation_filters

        reply = str(raw or "").strip()
        if reply not in declared_automation_filters():
            return None
        slots = dict(window.get("slots") or {})
        slots["filter"] = reply
        window["slots"] = slots
        window["automation_filter"] = reply
        window["realizable"] = True
        window["ok"] = True
        window["blocked"] = False
        window.pop("error", None)
        return window
    if window.get("task"):
        filled = apply_supplied_path(query, dict(window["task"]), raw)
        if filled is None:
            return None
        window["task"] = filled
        for key in ("actor_name", "ref_path", "asset_name", "folder_path", "class_name"):
            if filled.get(key):
                window[key] = filled[key]
    else:
        action = str(window.get("action") or "")
        if raw.startswith("/Game") and action in ("bind_blueprint", "place_actor", "place_blueprint", "compile_blueprint"):
            found = resolve_name(raw)
            if not found.get("ok"):
                return None
            for key, value in found.items():
                if key != "ok" and value is not None:
                    window[key] = value
        elif (".h" in raw or ".cpp" in raw) and action == "edit_cpp":
            from agent.supervise import cpp_task_for_file

            task = cpp_task_for_file(str(window.get("title") or query), raw)
            if not task:
                return None
            window["task"] = task
            if task.get("actor_name"):
                window["actor_name"] = task["actor_name"]
        else:
            return None
    window["realizable"] = True
    window["ok"] = True
    window["blocked"] = False
    window.pop("error", None)
    return window


# ---------- REPL ----------

def _parse_args(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="多窗口")
    parser.add_argument("--plain", action="store_true", help="纯文本模式，不开面板")
    parser.add_argument("--board", action="store_true", help="终端面板（默认）")
    parser.add_argument("--resume", default="", help="续接的 run id，或 latest")
    return parser.parse_args(argv)


def _resumable_runs(limit: int = 5) -> list[str]:
    """可续接 = 存在未终态且有动作的窗口的 run。规划者伪窗口（无 action）不算——它没有可续的东西。
    面板启动时提示。"""
    out: list[str] = []
    if not RUNS_DIR.is_dir():
        return out
    # 新布局按日期分目录，老 run 平铺在根上；两边都扫，按 run_id 排最近的在前
    paths = sorted(
        list(RUNS_DIR.glob("*.json")) + list(RUNS_DIR.glob("*/*.json")),
        key=lambda p: p.name, reverse=True,
    )
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        windows = data.get("windows")
        if not isinstance(windows, dict):
            continue
        if any(
            isinstance(item, dict) and item.get("state") in _RESUMABLE_STATES and str(item.get("action") or "").strip()
            for item in windows.values()
        ):
            out.append(str(data.get("run_id") or path.stem))
            if len(out) >= limit:
                break
    return out


def _load_run(run_ref: str) -> dict | None:
    if run_ref in ("latest", "l", ""):
        ids = _resumable_runs(limit=1)
        if not ids:
            return None
        run_ref = ids[0]
    # 先按日期子目录找，再回落到平铺的老文件
    name = run_ref if run_ref.endswith(".json") else f"{run_ref}.json"
    stem = name[:-5] if name.endswith(".json") else name
    candidates = []
    day = _runs_day(stem)
    if day:
        candidates.append(RUNS_DIR / day / name)
    candidates.append(RUNS_DIR / name)
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _decide_window(window: dict, entry: dict) -> str:
    """多窗口的人工裁决，对齐单循环：s 停止 / c 跳过 / r 路径。只问一次。"""
    from agent.cli_log import pause_live

    prompt = "裁决：" + _pause_hint(window, entry) + ": "
    try:
        with pause_live():
            print(f"\n{window.get('id')} 暂停：{entry.get('error') or entry.get('summary') or ''}", flush=True)
            answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n停止")
        return "stop"
    low = answer.lower()
    if low == "c":
        return "skip"
    if low in ("", "s"):
        return "stop"
    if low.startswith("r ") or low.startswith("r\t"):
        return answer.split(None, 1)[1].strip()
    action = str(window.get("action") or "")
    if answer.startswith("/Game") and action in (
        "bind_blueprint", "place_actor", "place_blueprint", "compile_blueprint", "create_blueprint",
    ):
        return answer
    if (".h" in answer or ".cpp" in answer) and action == "edit_cpp":
        return answer
    print("路径格式不对，这次不记成补路径。")
    return "stop"


def _resume_command(query: str) -> str | None:
    """REPL 里的续接指令：:resume / :r / 误敲的 --resume / 裸的 resume，后接 run id 或 latest。
    裸 resume 只在后跟 latest 或 run id 形状（YYYYMMDD-HHMMSS）时才算指令，普通句子不受影响。
    返回引用；None 表示不是。"""
    text = (query or "").strip()
    if not text:
        return None
    low = text.lower()
    parts = low.split()
    bare = parts[0] == "resume" and len(parts) <= 2 and (
        len(parts) == 1 or parts[1] == "latest" or re.fullmatch(r"\d{8}-\d{6}", parts[1])
    )
    for prefix in (":resume", "--resume", ":r"):
        if low == prefix:
            return "latest"
        if low.startswith(prefix + " "):
            rest = text[len(prefix):].strip()
            return rest or "latest"
    if bare:
        return parts[1] if len(parts) == 2 else "latest"
    return None


def _do_resume(data: dict, board, echo, emit) -> None:
    print(f"续接 {data.get('run_id')}：{data.get('query')}")
    if board is not None:
        board.reset(str(data.get("query") or ""))
        board.start()
    try:
        code, path = execute_windows(
            str(data.get("query") or ""), resume_state=data, echo=echo, emit=emit, decide=_decide_window,
        )
    finally:
        if board is not None:
            board.stop()
    print(f"\n续接结束，退出码 {code}。记录：{path}\n")


def _print_last_run(path) -> None:
    if path is None:
        print("还没有 run。")
        return
    json_path = Path(str(path)).with_suffix(".json")
    if not json_path.is_file():
        print(f"没有机读记录：{json_path}")
        return
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"机读记录读不了：{exc}")
        return
    windows = data.get("windows")
    if not isinstance(windows, dict):
        print(f"{json_path} 不是机读 run 记录（可能是任务书）。")
        return
    print(f"run {data.get('run_id')}：{data.get('query')}")
    for entry in (data.get("windows") or {}).values():
        if not isinstance(entry, dict):
            continue
        print(
            f"  {entry.get('id')} {_MD_STATUS.get(entry.get('state'), entry.get('state'))}"
            f" ok={entry.get('ok')} elapsed={entry.get('elapsed')} retries={entry.get('retries')}"
        )
        if entry.get("summary"):
            print(f"    {str(entry['summary'])[:120]}")
    integrated = data.get("integration") or {}
    print(f"  集成：{integrated.get('status') or ''} {integrated.get('summary') or ''}")


def _escalate_gaps_enabled() -> bool:
    """缺口是否必须人工确认。默认开；`UE_ESCALATE_GAPS=false` 只记录不阻塞（§3.2 允许的旁路）。

    默认值不许是关：那等于把「缺口必须改变流程走向」这条规则又退化回一句文案。"""
    import os

    return str(os.environ.get("UE_ESCALATE_GAPS", "")).strip().lower() not in ("false", "0", "no", "off")


def _gap_scopes_of(scopes) -> list[dict]:
    """物化后仍然整段没交付的范围（与集成/收尾同一口径，别另起一套判定）。"""
    return [scope for scope in (scopes or []) if _capability_gap_scope(scope)]


def _gap_ack_phrase() -> str:
    """缺口确认词只有**一个**：以 brief 侧常量为准，避免两侧各要一个词、把人问两遍。"""
    try:
        from agent.brief import GAP_ACK_PHRASE

        return str(GAP_ACK_PHRASE)
    except Exception:
        return "确认缺口"


def _already_acknowledged(brief: dict | None) -> list[str]:
    """人审门里已经显式确认过的缺口范围。空列表 = 还没确认过。

    D2 有两道闸，位置不同、职责不同：
    - **人审门那道**（`brief.brief_flow`）在**物化前**，用的是 dry-run 预演结论，问的是
      「模型给 0 扇、补窗也补不出来，这段我不做，行吗」；
    - **这道**（执行前）在**物化后**，用的是权威结论，问的是同样的事，但结论以真物化为准。
    两道都必要（预演可能乐观，物化才是真值），但**不许把人问两遍**：
    人审门已经显式确认过同一批范围时，这道只做复核、不再提问。"""
    if not isinstance(brief, dict):
        return []
    ack = brief.get("gap_ack")
    if not isinstance(ack, dict) or not ack.get("acknowledged"):
        return []
    return [str(item) for item in (ack.get("scopes") or []) if str(item).strip()]


def _escalate_gaps(scopes, ask, show, brief: dict | None = None) -> tuple[bool, list[dict]]:
    """D2 第 4 步：缺口范围必须有人显式表态「这段我不做」，本句才许继续执行。

    为什么放在**物化之后**：人审发生在 `brief_flow` 里，那时还没补窗，模型给的
    `windows: []` 究竟是「缺口」还是「代码能补」尚未确定——在人审前就问，问的是错的东西。
    物化跑完仍是零窗口，才是真缺口。

    返回 (允许继续?, 缺口范围)。被拒绝时调用方必须**不执行**，不许把它降级成「部分完成」。"""
    gaps = _gap_scopes_of(scopes)
    if not gaps:
        return True, []
    listing = "、".join(
        f"{scope.get('id')}（{_capability_gap_text(scope)}）" for scope in gaps
    )
    if not _escalate_gaps_enabled():
        show(f"能力缺口（只记录不阻塞，UE_ESCALATE_GAPS 已关）：{listing}")
        return True, gaps
    acked = set(_already_acknowledged(brief))
    pending = [scope for scope in gaps if str(scope.get("id") or "") not in acked]
    if not pending:
        # 人审门已经逐条确认过这些范围：复核通过，不再问第二遍。
        show(f"能力缺口已由人审确认，不执行这些范围：{listing}")
        return True, gaps
    show(
        "能力缺口：下面这些范围物化后仍没有可执行的窗口。"
        "它们不会被执行，也不会被算成「部分完成」。\n"
        f"  {listing}"
    )
    phrase = _gap_ack_phrase()
    try:
        answer = str(ask(f"确认不做这些范围，只执行其余部分？输入 {phrase} 确认，其它任何输入都取消: ")).strip()
    except (EOFError, KeyboardInterrupt):
        return False, gaps
    if answer == phrase:
        return True, gaps
    show("缺口未被确认，这次不执行。")
    return False, gaps


def _decompose_complete(messages):
    """表外需求的那一次调用。返回纯文本，不把工具 schema 带进去。"""
    from core.llm_call import complete

    response, _record = complete(messages, temperature=0, caller="decompose")
    if response is None:
        return ""
    return response.choices[0].message.content or ""


def _needs_brief(planned: dict, query: str) -> bool:
    """要不要出任务书。确定性拆句（hud-rules 这类纯规则链）跳过人审门。

    人审门的存在意义是审「模型决策的范围」。规则链的窗口是代码写死的固定因果链
    （上面 launch_editor → open_level → create → edit → check），既没有模型裁量，
    也没有未着落的半句可审；再叫一次生成器只是白烧 token，还会在生成器返回非 JSON 时
    多一条「写不出任务书」的噪声（20261029-123341/123344 两次实测）。
    只对已知的确定性来源生效，其余来源一字不动。
    """
    source = str((planned or {}).get("source") or "")
    if source in _DETERMINISTIC_PLAN_SOURCES:
        windows = (planned or {}).get("windows") or []
        # 只有每扇都真实可跑才跳过；有暂停/不可实现的扇仍走人审门。
        if windows and all(w.get("realizable") and not w.get("waiting_external") for w in windows):
            print("确定性拆句（" + source + "）：窗口是固定因果链，跳过任务书人审门。", flush=True)
            return False
    from agent.brief import needs_brief

    return needs_brief(query, (planned or {}).get("windows") or [], (planned or {}).get("intent_full"))


# 只列「窗口由代码写死的固定因果链」这一种来源。手写规则（rules）仍按原口径走人审门：
# 它们可能拼出跨域的多扇组合，保留人审不动。
_DETERMINISTIC_PLAN_SOURCES = frozenset({"hud-rules"})


def _run_query(query: str, board, echo, emit) -> tuple[int, Path | None]:
    """一句话入口：先看要不要任务书；窄任务直接进多窗口执行。
    面板在拆窗口之前就打开。检索模型的权重条不再占着最后一行，阶段写在主代理上。"""
    from agent.brief import brief_flow, materialize_scopes, needs_brief

    if board is not None:
        board.reset(query)
        board.set_phase("正在拆窗口")
        board.start()
    try:
        planned = plan_windows(query)
        windows = planned.get("windows") or []
        if _needs_brief(planned, query):
            if board is not None:
                board.set_phase("正在写任务书")
                board.set_review("审阅 1：任务书生成中")
            print("这句话像大任务：先出任务书，人审通过后才执行。", flush=True)

            def _show(text) -> None:
                if board is not None and board.running():
                    board.stop()
                print(text, flush=True)

            def _ask(prompt="") -> str:
                if board is not None and board.running():
                    board.stop()
                return input(prompt)

            # D9：理解器没着落的半句必须进任务书，不许只在事件流里留一句。
            # 用签名探测传参：老签名没有 unresolved 时保持原行为，不炸。
            unresolved = None
            intent = planned.get("intent_full")
            if isinstance(intent, dict) and intent.get("unresolved"):
                unresolved = [str(item) for item in intent["unresolved"] if str(item).strip()] or None
            flow_kwargs = {"windows": windows, "reader": _ask, "printer": _show}
            if unresolved:
                try:
                    import inspect

                    if "unresolved" in inspect.signature(brief_flow).parameters:
                        flow_kwargs["unresolved"] = unresolved
                except (TypeError, ValueError):
                    pass
            outcome = brief_flow(query, **flow_kwargs)
            if outcome is None:
                print("任务书写不出，按窄任务继续。", flush=True)
            else:
                status, brief, md, _js = outcome
                rounds = (brief.get("review") or {}).get("rounds") or 1
                review_text = f"审阅 {rounds}：通过" + ("，附修改" if rounds > 1 else "")
                # D6 同源病灶：缺口被确认时面板还写「通过」，等于把「这段我不做」藏进一个好词里。
                # 人审门已经记下 gap_ack，这里必须把它换成能看见缺口的文案。
                acked = _already_acknowledged(brief)
                if acked:
                    review_text = (
                        f"审阅 {rounds}：通过（已确认能力缺口 " + "、".join(acked[:4]) + "，不执行）"
                    )
                if board is not None:
                    board.set_review(review_text)
                if status == "abort":
                    print("任务书被放弃，这次不执行。", flush=True)
                    return 1, md
                windows, scopes = materialize_scopes(
                    brief, candidates=windows, complete=_decompose_complete,
                )
                # D2：物化后仍是缺口 ⇒ 必须人工显式确认，否则这句不许进执行。
                # 人审门已确认过同一批范围时不重复提问（见 _already_acknowledged）。
                allowed, gaps = _escalate_gaps(scopes, _ask, _show, brief)
                if not allowed:
                    if board is not None:
                        board.set_review("审阅：缺口未确认，未执行")
                    # 缺口要落盘：人拒绝之后的记录仍须说清「哪段没做、为什么」
                    _record_declined_gaps(query, md, brief, scopes, gaps, emit)
                    return 1, md
                if board is not None:
                    board.set_phase("执行")
                    board.set_review(review_text)
                    if not board.running():
                        board.start()
                return execute_windows(
                    query, planned=planned, windows=windows, brief=brief, scopes=scopes,
                    echo=echo, emit=emit, decide=_decide_window,
                )
        if board is not None:
            board.set_phase("执行")
            if not board.running():
                board.start()
        windows = planned.get("windows") or []
        if _only_paused_planner(windows):
            # 20261028-185100 实测：理解器推不出 goal（「动作表里没有改射速的动作」——
            # 这句话本身是对的）、规划者返回空清单，plan_windows 只留一扇暂停窗，
            # 句子在快路径上撞死胡同（续接也是同一条路）。求解开着就把它交给受控
            # 求解扇，与任务书物化路径（brief.materialize_scopes 的 zero_needs）同一条
            # 兜底；求解关着保持原样。
            fan = _fast_path_solve_windows(query)
            if fan:
                if board is not None and board.running():
                    board.stop()
                print("快路径没有窗口：交给受控求解扇（UE_SOLVER_ENABLED）。", flush=True)
                planned = dict(planned)
                planned["windows"] = fan
                planned["source"] = "solver-fallback"
        return execute_windows(query, planned=planned, echo=echo, emit=emit, decide=_decide_window)
    finally:
        if board is not None and board.running():
            board.stop()


def _only_paused_planner(windows: list) -> bool:
    """规划者空清单时 plan_windows 只塞一扇 _paused 合成窗（action 空、blocked）。
    这扇不是交付，是死胡同的牌子。"""
    if not windows:
        return True
    return all(
        isinstance(win, dict) and not str(win.get("action") or "").strip()
        for win in windows
    )


def _fast_path_solve_windows(query: str) -> list[dict]:
    """快路径零窗口的 solve 兜底（20261028-185100）。理解器与规划者都给不出窗口时，
    求解开着就不许把句子停在「暂停」死胡同——交给受控求解扇，预算与写闸照旧。"""
    try:
        from agent.solver import solve_window, solver_enabled
    except Exception:
        return []
    if not solver_enabled():
        return []
    scope = {
        "id": "A",
        "title": str(query or "").strip()[:80],
        "deliverables": [],
        "pending_note": "",
        "depends_on": [],
        "external_dep": "",
        "windows": [],
    }
    try:
        return [item for item in (solve_window(scope, query) or []) if isinstance(item, dict)]
    except Exception:
        return []


def _record_declined_gaps(query: str, md, brief: dict, scopes, gaps: list[dict], emit) -> None:
    """缺口被拒绝执行时的落盘：事件 + 一行给人看的话。不伪造 status=成功，也不留空。

    这里**不调 execute_windows**：没有窗口就没有 run json，所以缺口清单落进事件流与台账附注。
    「人在审阅里拒绝」和「系统静默蒸发」的区别就在于这一条记录有没有。"""
    rows = [
        {
            "scope": str(scope.get("id") or ""),
            "title": str(scope.get("title") or ""),
            "deliverables": [str(x) for x in (scope.get("deliverables") or []) if str(x).strip()],
            "reason": _capability_gap_text(scope),
            "missing_actions": [
                str(x) for x in (
                    (scope.get("capability_gap") or {}).get("missing_actions") or []
                ) if str(x).strip()
            ],
        }
        for scope in gaps
    ]
    if callable(emit):
        try:
            emit("capability_gap_declined", query=query[:120], scopes=rows, brief=str(md or ""))
        except Exception:
            pass
    print("未执行。缺口清单：", flush=True)
    for row in rows:
        print(f"  - {row['scope']}：{row['reason']}", flush=True)
        for item in row["deliverables"][:4]:
            print(f"      缺：{item}", flush=True)
    print(f"任务书仍在：{md}", flush=True)


def main(argv=None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    from agent.run_guard import attach_claimed_stdin, sweep_orphan_runs

    if attach_claimed_stdin() == "busy":
        print("输入已被另一个进程占用")
        raise SystemExit(1)
    args = _parse_args(argv)
    print("多窗口。规划者只在快路径拆不开时开口。每扇单独跑，最后集成。")
    print("记录在 data/runs（含 .cli.jsonl 子窗口日志），事件在 data/events。"
          "保存关卡与 PIE 跟随 UE_DEFAULT_SAVE / UE_DEFAULT_PIE（现缺省开）。")
    print("输入 :quit 退出。提示符下输入 E 查看上一个 run；运行中按 E 切换简略/详细。:resume latest 续接。\n")
    board = None
    if not args.plain:
        try:
            from agent.board import Board

            def _save_cli(record: dict) -> None:
                append_cli_log(str(record.get("run_id") or ""), record)

            board = Board(on_log=_save_cli)

            def _status(text: str) -> None:
                board.set_phase(text)
                board.echo(text)

            def _toggle_live(on: bool) -> None:
                if on:
                    if not board.running():
                        board.start()
                elif board.running():
                    board.stop()

            from agent.cli_log import set_pause_live, set_status_sink

            set_status_sink(_status)
            set_pause_live(_toggle_live)
        except Exception as exc:
            print(f"面板不可用（{exc}），改用纯文本。")
            board = None
    resumable = _resumable_runs()
    if resumable:
        print(f"可续接的 run：{'、'.join(resumable)}（:resume latest 取最近一个）")
    if board is not None:
        board.set_resumable(resumable)

    def echo(text) -> None:
        if board is not None:
            board.echo(text)
        else:
            print(text)

    def emit(event: dict) -> None:
        if board is not None:
            board.event(event)

    if args.resume:
        data = _load_run(args.resume)
        if data is None:
            print("没有可续接的 run。")
        else:
            _do_resume(data, board, echo, emit)

    orphans = sweep_orphan_runs(RUNS_DIR)
    if orphans:
        print("无主 run 已留名：" + "、".join(orphans))

    last_run = None
    last_code = 0
    while True:
        try:
            query = input("多窗口 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            raise SystemExit(last_code)
        if not query:
            continue
        if query in (":quit", ":q", "exit", "quit"):
            print("退出")
            raise SystemExit(last_code)
        if query in ("e", "E", ":expand"):
            _print_last_run(last_run)
            continue
        resume_ref = _resume_command(query)
        if resume_ref is not None:
            data = _load_run(resume_ref)
            if data is None:
                available = "、".join(_resumable_runs()) or "无"
                print(f"没有可续接的 run：{resume_ref}（可续接的：{available}）")
            else:
                _do_resume(data, board, echo, emit)
                last_run = runs_md_path(str(data.get("run_id") or ""))
            continue
        if query.startswith("--"):
            print("REPL 里 --xxx 是命令行参数的写法，不当任务执行。续接用 :resume latest，退出用 :quit。")
            continue
        code, path = _run_query(query, board, echo, emit)
        last_run = path
        last_code = code
        print(f"\n窗口结束，退出码 {code}。记录：{path}\n")


if __name__ == "__main__":
    main()
