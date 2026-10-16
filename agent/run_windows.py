# agent/run_windows.py
"""多窗口：每扇单独跑现有任务。写场景/编译/PIE 的窗口过编辑器闸门，只读窗口并行。
集成不叫模型；任务书的集成扇跑矩阵验收。事件流落 data/events/<run_id>.jsonl，
机读 run 状态落 data/runs/YYYYMMDD/<run_id>.json（老 run 平铺兼容），可 --resume 续接。
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

from agent.catalog import DIRECT_ACTIONS, EDITOR_LOCK_ACTIONS, GRAPH_ACTIONS, INTEGRATE_ACTION, READ_ONLY_ACTIONS
from agent.plan_windows import find_cycle, mark_dangling, plan_windows
from agent.sequence import RUNS_DIR
from config import PROJECT_ROOT, UE_PROJECT_DIR, UE_SOURCE_ROOT

EVENTS_DIR = PROJECT_ROOT / "data" / "events"
SNAPSHOTS_DIR = PROJECT_ROOT / "data" / "snapshots"
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
    "bind_blueprint", "bind_slot", "create_blueprint",
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

class _RunState:
    """data/runs/<run_id>.json 的内存镜像，每次变化写穿。窗口定义存进 entry["window"]，续接不用重规划。"""

    def __init__(self, run_id: str, query: str, source: str, windows: list[dict],
                 *, review: dict | None = None, brief: dict | None = None, scopes=None) -> None:
        self.run_id = run_id
        self.path = runs_json_path(run_id)
        self.data = {
            "run_id": run_id, "query": query, "source": source,
            "started_at": _now(), "updated_at": _now(), "status": "running",
            "review": review or {}, "brief": brief, "scopes": scopes or [],
            "windows": {}, "artifacts": {}, "integration": {},
        }
        for window in windows:
            self.data["windows"][str(window["id"])] = self._entry(window)

    def _entry(self, window: dict) -> dict:
        action = str(window.get("action") or (window.get("task") or {}).get("action") or "")
        realizable = bool(window.get("realizable", True))
        state = "waiting_external" if window.get("waiting_external") else ("paused" if not realizable else "queued")
        return {
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
        from agent.supervise import _try_cpp

        class_name = window.get("class_name") or (window.get("slots") or {}).get("class") or ""
        task = _try_cpp(f"{title}。类 {class_name}。编译")
        if not task or task.get("ok") is False:
            return task or {"ok": False, "error": "写不出唯一的 C++ 类，不猜。"}
        task["id"] = window["id"]
        task["title"] = title or "改 C++ 并编译"
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
            f"拿到 refPath，再调用 set_actor_transform，只改 location，worldspace 为 true。不要保存，不要启动 PIE。"
        )
        task["verify"] = [{
            "kind": "editor_assert",
            "toolset": "editor_toolset.toolsets.actor.ActorTools",
            "tool": "get_actor_transform",
            "actor_name": name,
            "expect": {"location": {"x": x, "y": y, "z": z}},
        }]
        task["allow_writes"] = ["set_actor_transform"]
        task["save"] = False
        task["pie"] = False
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


def _prepare_task(window: dict, origin: str, artifacts: dict) -> dict:
    if window.get("task"):
        task = dict(window["task"])
        task["id"] = window["id"]
        task.setdefault("save", False)
        task.setdefault("pie", False)
    else:
        task = {
            "id": window["id"],
            "action": window.get("action"),
            "title": window.get("title"),
            "query": window.get("title") or "",
            "slots": dict(window.get("slots") or {}),
            "save": False,
            "pie": False,
            "allow_writes": [],
            "max_retries": int(window.get("max_retries") or 0),
        }
        for key in (
            "actor_name", "picker_name", "ref_path", "asset_path", "folder_path",
            "asset_name", "class_name", "parent", "from_path", "location",
        ):
            if window.get(key) not in (None, ""):
                task[key] = window[key]
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
    if action in ("judge_pickup", "check_pickup"):
        entry_ref = _lookup_artifact(artifacts, str(task.get("actor_name") or window.get("actor_name") or ""))
        if entry_ref:
            task["target_ref"] = entry_ref["ref"]
            task["target_in_level"] = bool(entry_ref.get("in_level", True))
    task.setdefault("save", False)
    task.setdefault("pie", False)
    return task


def _brief(summary: dict) -> dict:
    text = summary.get("summary") or summary.get("index_note") or summary.get("error") or ""
    out = {"id": summary.get("id"), "ok": bool(summary.get("ok")), "summary": str(text)[:500]}
    judge = summary.get("judge") or summary.get("verdict")
    if judge:
        out["judge"] = judge
    return out


def _run_one(task: dict, thread_id: str, action: str, on_row=None, under_gate=None) -> dict:
    from agent.run_task import run_task

    _GATE.enter(action)
    try:
        if under_gate is not None:
            under_gate()
        return run_task(task, thread_id=thread_id, on_row=on_row)
    finally:
        _GATE.exit(action)


def _make_on_row(events: _EventBus, wid: str):
    """把一扇的 trace 行转发成 run 级事件：build/test/precheck 算 judge，其余算 tool_call。"""

    def on_row(row: dict) -> None:
        node = str(row.get("node") or "")
        etype = "judge" if node in ("build", "test", "precheck") else "tool_call"
        events.emit(
            etype, window=wid, node=node, tool=str(row.get("tool") or ""),
            ok=bool(row.get("ok")), note=str(row.get("error") or "")[:120] or None,
            elapsed=row.get("elapsed"),
        )

    return on_row


# ---------- 源码快照与还原 ----------

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _task_writes_source(window: dict) -> bool:
    task = window.get("task") or {}
    if task.get("inject_typo") or task.get("freeze_source"):
        return False
    return any(
        isinstance(item, dict) and item.get("kind") == "cpp_build"
        for item in task.get("verify") or []
    )


def _snapshot_source(run_id: str, wid: str) -> dict:
    """会写源码的窗口开始前拍字节快照到 data/snapshots/<run_id>/。在编辑器闸门内执行。"""
    if not UE_SOURCE_ROOT.is_dir():
        return {"dir": "", "files": {}}
    out = SNAPSHOTS_DIR / run_id / wid
    if out.exists():
        shutil.rmtree(out)
    files: dict[str, str] = {}
    for path in sorted(UE_SOURCE_ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".h", ".cpp", ".cs", ".ini"):
            continue
        rel = path.relative_to(UE_SOURCE_ROOT).as_posix()
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        files[rel] = _sha256_file(path)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps({"files": files}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"dir": str(out), "files": files}


def _restore_source(run_id: str, wid: str, snapshot: dict) -> list[str]:
    """窗口失败且无 keep_source 时还原：只回滚这次窗口改过的文件字节。"""
    base = Path(str(snapshot.get("dir") or ""))
    restored: list[str] = []
    for rel, sha in (snapshot.get("files") or {}).items():
        path = UE_SOURCE_ROOT / rel
        if path.is_file() and _sha256_file(path) == sha:
            continue
        src = base / rel
        if src.is_file():
            shutil.copyfile(src, path)
            restored.append(rel)
    return restored


# ---------- 一扇的执行 ----------

def _run_window(entry: dict, state: _RunState, query: str, artifacts: dict, events: _EventBus, echo) -> dict:
    from agent.run_task import new_thread_id

    window = entry.get("window") or {}
    action = str(entry.get("action") or window.get("action") or "")
    if action == INTEGRATE_ACTION:
        return _run_integrate(entry, state, query, artifacts, events)
    on_row = _make_on_row(events, entry["id"])
    attempts = 1 if action in GRAPH_ACTIONS else int(window.get("max_retries") or 0) + 1
    snap_box: dict = {}

    def _snapshot_once() -> None:
        if not snap_box and (_task_writes_source(window) or action in _SOURCE_WRITE_ACTIONS):
            snap_box["snap"] = _snapshot_source(state.run_id, entry["id"])

    summary = {"id": entry["id"], "ok": False, "summary": "没有执行"}
    attempt = 0
    started = time.perf_counter()
    for attempt in range(1, attempts + 1):
        events.emit("window_state", window=entry["id"], state="running" if attempt == 1 else "retry", attempt=attempt)
        task = _prepare_task(window, query, artifacts)
        if task.get("ok") is False and not task.get("verify") and task.get("action") not in DIRECT_ACTIONS:
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
    if snapshot is not None and not summary.get("ok"):
        keep = bool((window.get("task") or {}).get("keep_source"))
        if not keep:
            restored = _restore_source(state.run_id, entry["id"], snapshot)
            if restored:
                summary["restored"] = restored
    out = dict(summary)
    if out.get("elapsed") is None:
        out["elapsed"] = elapsed
    out["retries"] = retries
    out["snapshot"] = snapshot
    return out


def _dispatch_window(entry: dict, state: _RunState, query: str, events: _EventBus, echo) -> dict:
    return _run_window(entry, state, query, state.data["artifacts"], events, echo)


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
                elapsed=entry.get("elapsed"), retries=entry.get("retries"))
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


def _run_integrate(entry: dict, state: _RunState, query: str, artifacts: dict, events: _EventBus) -> dict:
    """集成矩阵验收：代码判定，不叫模型。子判定一项一个；产出哈希、汇总与归档。"""
    from agent.run_task import new_thread_id

    entries = state.entries()
    matrix: list[dict] = []

    # 1 重复请求幂等：重跑最近一扇成功的只读裁决，结论不变才算过
    judges = [
        item for item in entries
        if str(item.get("action")) in ("judge_pickup", "check_pickup") and item.get("state") == "done"
    ]
    item = {"item": "重复请求幂等", "ok": True, "note": ""}
    if judges:
        last = judges[-1]
        try:
            task = _prepare_task(last.get("window") or {}, query, artifacts)
            task["id"] = f"{last['id']}-reverify"
            result = _run_one(task, new_thread_id(), str(last.get("action") or "judge_pickup"))
            before = (last.get("result") or {}).get("judge")
            after = result.get("judge")
            if result.get("ok") and after == before:
                item["note"] = f"复验一致：{after or str(result.get('summary') or '')[:40]}"
            else:
                item["ok"] = False
                item["note"] = "复验不一致或失败：" + str(result.get("summary") or result.get("error") or "")[:80]
        except Exception as exc:
            item["ok"] = False
            item["note"] = f"复验异常：{exc}"[:100]
    else:
        item["note"] = "没有可复验的只读裁决，未复验"
    matrix.append(item)

    # 2 拒答路径：被拒/表外/暂停的窗口要停在暂停，不带成功结果
    refused = [item for item in entries if item.get("state") == "paused"]
    matrix.append({
        "item": "拒答路径",
        "ok": all(not ((item.get("result") or {}).get("ok")) for item in refused),
        "note": "；".join(item["id"] for item in refused) or "本次没有拒答窗口",
    })

    # 3 延迟窗口：带重试的窗口最终要有明确终态
    delayed = [item for item in entries if int(item.get("retries") or 0) > 0]
    matrix.append({
        "item": "延迟窗口",
        "ok": all(item.get("state") in ("done", "failed", "paused", "skipped") for item in delayed),
        "note": "；".join(f"{item['id']}×{item.get('retries')}" for item in delayed) or "本次没有重试",
    })

    # 4 断线重连：出现过断线的窗口最终要跑完
    hit = [item for item in entries if _looks_disconnected(item)]
    if not hit:
        matrix.append({"item": "断线重连", "ok": True, "note": "本次无断线"})
    else:
        recovered = all(item.get("state") == "done" for item in hit)
        matrix.append({
            "item": "断线重连", "ok": recovered,
            "note": ("断线后重连成功：" if recovered else "断线未恢复：") + "；".join(item["id"] for item in hit),
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
        "ok": not stuck,
        "note": "；".join(f"{item['id']}={item.get('state')}" for item in stuck) or "每扇都有明确终态或未开始",
    })

    hashes = _touched_hashes(state)
    totals = _run_totals(state)
    archive = _archive_run(state, hashes=hashes, matrix=matrix)
    ok = all(one.get("ok") for one in matrix)
    return {
        "id": entry["id"],
        "ok": ok,
        "summary": ("矩阵全部通过" if ok else "矩阵未全过") + f"；{len(matrix)} 项；触及文件 {len(hashes)} 个；归档 {archive}",
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
        dll = UE_PROJECT_DIR / "Binaries" / "Win64" / "UnrealEditor-AIAgentProject.dll"
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


def _archive_run(state: _RunState, hashes=None, matrix=None, md_path=None) -> Path:
    """日志与报告归档：run json、事件流、md 记录、哈希与矩阵都收进 data/archive/<run_id>/。"""
    folder = ARCHIVE_DIR / state.run_id
    folder.mkdir(parents=True, exist_ok=True)
    if state.path.is_file():
        _copy_file_quietly(state.path, folder / state.path.name)
    events = EVENTS_DIR / f"{state.run_id}.jsonl"
    if events.is_file():
        _copy_file_quietly(events, folder / events.name)
    if md_path is not None and Path(md_path).is_file():
        _copy_file_quietly(Path(md_path), folder / Path(md_path).name)
    payload = {"run_id": state.run_id, "hashes": hashes or [], "matrix": matrix or []}
    (folder / "deliverables.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
        "进入 PIE，不保存关卡。" if pie else "不保存关卡，不 PIE。",
        "",
    ]
    for row in rows:
        lines.append(f"## {row['id']}. {row['title']}")
        lines.append(f"- 动作：{row.get('action') or '无'}")
        deps = row.get("depends_on") or []
        lines.append("- 依赖：" + ("、".join(deps) if deps else "无"))
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
            "status": _MD_STATUS.get(entry.get("state"), str(entry.get("state"))),
            "result": result,
        })
    return rows


# ---------- 主执行 ----------

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
    if resume_state is None:
        review = (brief or {}).get("review") or {}
        if str(review.get("status") or "").startswith("通过"):
            events.emit("confirm", window="", option="确认", path="")
    # P4 观测：理解器一开口就记一条 intent 事件（只存决策字段）；run_start 的 source 带 understood
    if isinstance(planned, dict) and planned.get("intent"):
        fields = {key: value for key, value in dict(planned["intent"]).items()}
        fields.pop("source", None)
        events.emit("intent", source=str(planned.get("source") or source), **fields)
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
        events.emit("run_start", query=query, source=source,
                    windows=[{"id": e["id"], "title": e["title"], "action": e["action"],
                              "depends_on": list(e.get("depends_on") or [])} for e in entries])
        for entry in entries:
            if entry["id"] in members:
                events.emit("window_state", window=entry["id"], state="paused")
        integrated = {"ok": False, "status": "失败", "summary": note + "。环上的窗口暂停，依赖它们的窗口不跑。"}
        state.data["integration"] = integrated
        state.data["status"] = "失败"
        state.save()
        _write(path, query, source, _rows(state), integrated, pie=False)
        events.emit("integration", ok=False, status="失败", summary=integrated["summary"])
        events.emit("run_end", status="失败", ok=False)
        echo(f"\n拆成 {len(entries)} 扇。来源：{_SOURCE_LABEL.get(source, source)}。记录写在 {path}")
        echo(f"\n集成：{integrated['summary']}")
        return 1, path

    events.emit("run_start", query=query, source=source,
                windows=[{"id": e["id"], "title": e["title"], "action": e["action"],
                          "depends_on": list(e.get("depends_on") or [])} for e in entries])
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
            echo(f"\n{entry['id']} 暂停：{err_text}")
            echo(f"{entry['id']} 等人工裁决，在终端里输入：s 停止 / c 跳过 / r /Game/目录 或 r Public/类名.h")
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
            dispatched = 0
            for entry in state.entries():
                wid = entry["id"]
                if wid in running or entry.get("state") != "queued":
                    continue
                unmet = [
                    dep for dep in entry.get("depends_on") or []
                    if dep not in state.data["windows"] or state.data["windows"][dep].get("state") != "done"
                ]
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
    blocking = [entry for entry in failed if entry["id"] in dependents]
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
    integrated = {"ok": ok, "status": status, "summary": note}

    # 范围行（任务书 run 才有）+ 台账回写
    extra: list[str] = []
    ledger_rows: list[dict] = []
    if scopes:
        bits = []
        for scope in scopes:
            ids = [wid for wid in scope.get("window_ids") or [] if wid in state.data["windows"]]
            states = [state.data["windows"][wid].get("state") for wid in ids]
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
            bits.append(f"{scope.get('id')} {mark}")
            pending = [] if ledger_status == "done" else [str(item) for item in scope.get("deliverables") or []]
            note_scope = str(scope.get("pending_note") or "")
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

    integrate_entry = next(
        (entry for entry in entries if str(entry.get("action")) == INTEGRATE_ACTION), None
    )
    if integrate_entry is not None and isinstance(integrate_entry.get("detail"), dict):
        detail = integrate_entry["detail"]
        for item in detail.get("matrix") or []:
            extra.append(f"- 矩阵 {item.get('item')}：{'通过' if item.get('ok') else '未过'}。{item.get('note') or ''}")
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
    if integrate_entry is not None:
        _archive_run(state, md_path=path)
    events.emit("integration", ok=ok, status=status, summary=note, scopes=extra or None)
    if stale_hint:
        events.emit("index_stale", note=stale_hint)
        echo(f"\n{stale_hint}")
    if ok and isinstance(planned, dict) and planned.get("intent_full"):
        # P3 L3：整句集成成功才把意图归一成句式模板写回 learned_routes（unresolved 非空不回写）
        learned_id = ""
        try:
            from agent.plan_windows import remember_intent_route

            learned_id = remember_intent_route(query, planned["intent_full"], planned.get("windows") or []) or ""
        except Exception:
            learned_id = ""
        if learned_id:
            events.emit("learned_route", route_id=learned_id, query=query[:120])
    events.emit("run_end", status=status, ok=ok)
    state.data["integration"] = integrated
    state.data["status"] = "interrupted" if interrupted else status
    state.save()
    echo(f"\n集成：{note}")
    if interrupted:
        return 130, path
    return (0 if ok else 1), path


# ---------- 人工裁决补路径 ----------

def _apply_path(window: dict, raw: str, query: str) -> dict | None:
    """把 r 补的路径写回窗口：规则步骤沿用单循环的填法；规划窗口按槽位重解析。无效返回 None。"""
    from agent.plan_windows import resolve_name
    from agent.sequence import apply_supplied_path

    window = dict(window)
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
    print(f"\n{window.get('id')} 暂停：{entry.get('error') or entry.get('summary') or ''}")
    prompt = "裁决：s 停止 / c 跳过 / r /Game/目录 或 r Public/类名.h: "
    try:
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


def _run_query(query: str, board, echo, emit) -> tuple[int, Path | None]:
    """一句话入口：先看要不要任务书；窄任务直接进多窗口执行。"""
    from agent.brief import brief_flow, materialize_scopes, needs_brief

    planned = plan_windows(query)
    windows = planned.get("windows") or []
    if needs_brief(query, windows, planned.get("intent_full")):
        print("这句话像大任务：先出任务书，人审通过后才执行。")
        if board is not None:
            board.set_review("审阅 1：任务书生成中")
        outcome = brief_flow(query, windows=windows, reader=input, printer=print)
        if outcome is None:
            print("任务书写不出，按窄任务继续。")
        else:
            status, brief, md, _js = outcome
            rounds = (brief.get("review") or {}).get("rounds") or 1
            review_text = f"审阅 {rounds}：通过" + ("，附修改" if rounds > 1 else "")
            if board is not None:
                board.set_review(review_text)
            if status == "abort":
                print("任务书被放弃，这次不执行。")
                return 1, md
            windows, scopes = materialize_scopes(brief, candidates=windows)
            if board is not None:
                board.reset(query)
                board.set_review(review_text)
                board.start()
            try:
                return execute_windows(
                    query, planned=planned, windows=windows, brief=brief, scopes=scopes,
                    echo=echo, emit=emit, decide=_decide_window,
                )
            finally:
                if board is not None:
                    board.stop()
    if board is not None:
        board.reset(query)
        board.start()
    try:
        return execute_windows(query, planned=planned, echo=echo, emit=emit, decide=_decide_window)
    finally:
        if board is not None:
            board.stop()


def main(argv=None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    args = _parse_args(argv)
    print("多窗口。规划者只在快路径拆不开时开口。每扇单独跑，最后集成。")
    print("记录在 data/runs，事件在 data/events。不保存关卡，不 PIE。")
    print("输入 :quit 退出，E 展开上一个 run，:resume latest（或 :resume <run_id>）续接。\n")
    board = None
    if not args.plain:
        try:
            from agent.board import Board

            board = Board()
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

    last_run = None
    while True:
        try:
            query = input("多窗口 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            return
        if not query:
            continue
        if query in (":quit", ":q", "exit", "quit"):
            print("退出")
            return
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
        print(f"\n窗口结束，退出码 {code}。记录：{path}\n")


if __name__ == "__main__":
    main()
