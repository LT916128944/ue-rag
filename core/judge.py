# core/judge.py
"""真裁判。返回 {ok, kind, summary, errors:[{file, line, message}]}。"""
import json
import re
import subprocess

from config import UE_ENGINE_ROOT, UE_EXEC_MODE, UE_MCP_TIMEOUT, UE_PROJECT
from core.editor_exec import MOCK_ACTORS, exec_tool

_ERROR_C = re.compile(r"error C\d+")
_FATAL = re.compile(r"fatal error", re.I)
_FILE_LINE = re.compile(r"((?:[A-Za-z]:\\)?[^\s()]+\.(?:h|cpp|hpp|cs))\((\d+)")

_ACTOR_TOOLSET = "editor_toolset.toolsets.actor.ActorTools"
_SCENE_TOOLSET = "editor_toolset.toolsets.scene.SceneTools"
_BP_TOOLSET = "editor_toolset.toolsets.blueprint.BlueprintTools"
_AUTO_TOOLSET = "AutomationTestToolset.AutomationTestToolset"

_discovered = False


def _result(ok: bool, kind: str, summary: str, errors: list | None = None) -> dict:
    return {"ok": ok, "kind": kind, "summary": summary, "errors": errors or []}


def _unwrap(data):
    if isinstance(data, str):
        text = data.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return data
    if isinstance(data, dict) and "returnValue" in data and len(data) == 1:
        return _unwrap(data["returnValue"])
    return data


def _empty_success(data) -> bool:
    data = _unwrap(data)
    if data is None or data == "" or data == {} or data == []:
        return True
    if isinstance(data, dict):
        if "error" in data:
            return False
        if "returnValue" in data:
            return _empty_success(data.get("returnValue"))
    return False


def _location(data) -> dict | None:
    data = _unwrap(data)
    if isinstance(data, dict) and "location" in data and isinstance(data["location"], dict):
        return data["location"]
    if isinstance(data, dict):
        for key in ("x", "y", "z"):
            if key not in data:
                break
        else:
            return data
    return None


def _read_task(task: dict | None) -> dict:
    task = dict(task or {})
    task.setdefault("allow_writes", [])
    task.setdefault("save", False)
    task.setdefault("pie", False)
    return task


def _editor_assert(spec: dict, task: dict) -> dict:
    if UE_EXEC_MODE == "mock":
        name = spec.get("actor_name") or (spec.get("args") or {}).get("actor", {}).get("refPath", "")
        loc = (MOCK_ACTORS.get(name) or {}).get("location")
        return _compare_location(spec, loc, "mock")

    tool = spec.get("tool") or "get_actor_transform"
    toolset = spec.get("toolset") or _ACTOR_TOOLSET
    args = dict(spec.get("args") or {})
    if tool == "get_actor_transform" and "actor" not in args:
        name = spec.get("actor_name")
        if not name:
            return _result(False, "editor_assert", "缺少 actor", [{"file": "", "line": 0, "message": "editor_assert 没有 actor"}])
        found = exec_tool(
            _SCENE_TOOLSET,
            "find_actors",
            {"name": name, "tag": "", "collision_channels": []},
            task=task,
        )
        if not found.get("ok"):
            return _result(False, "editor_assert", "find_actors 失败", [{"file": "", "line": 0, "message": str(found.get("error"))}])
        actors = _unwrap(found.get("data"))
        if isinstance(actors, dict):
            actors = actors.get("returnValue") or actors.get("actors") or []
        if not isinstance(actors, list):
            actors = [actors]
        paths = []
        for actor in actors:
            if isinstance(actor, dict):
                paths.append(actor.get("refPath") or actor.get("path") or json.dumps(actor, ensure_ascii=False))
            else:
                paths.append(str(actor))
        if len(paths) != 1:
            listed = ", ".join(paths) or "(空)"
            return _result(False, "editor_assert", "Actor 不唯一", [{"file": "", "line": 0, "message": f"候选: {listed}"}])
        args["actor"] = {"refPath": paths[0]}

    called = exec_tool(toolset, tool, args, task=task)
    if not called.get("ok"):
        return _result(False, "editor_assert", "读工具失败", [{"file": "", "line": 0, "message": str(called.get("error"))}])
    return _compare_location(spec, _location(called.get("data")), "editor")


def _compare_location(spec: dict, loc: dict | None, source: str) -> dict:
    expect = ((spec.get("expect") or {}).get("location")) or {}
    if loc is None:
        return _result(False, "editor_assert", "没有 location", [{"file": "", "line": 0, "message": f"{source} 返回里没有 location"}])
    mismatches = []
    for axis in ("x", "y", "z"):
        if axis not in expect:
            continue
        try:
            actual = float(loc[axis])
            wanted = float(expect[axis])
        except (KeyError, TypeError, ValueError):
            mismatches.append(f"{axis} 缺失")
            continue
        if abs(actual - wanted) > 1.0:
            mismatches.append(f"{axis}={actual} 期望 {wanted}")
    if mismatches:
        message = "; ".join(mismatches)
        return _result(False, "editor_assert", message, [{"file": "", "line": 0, "message": message}])
    return _result(True, "editor_assert", "location 匹配")


def _blueprint_compile(spec: dict, task: dict) -> dict:
    if UE_EXEC_MODE == "mock":
        return _result(True, "blueprint_compile", "mock")
    ref = spec.get("refPath") or ""
    if not ref:
        return _result(False, "blueprint_compile", "缺少 refPath", [{"file": "", "line": 0, "message": "blueprint_compile 没有 refPath"}])
    called = exec_tool(
        _BP_TOOLSET,
        "compile_blueprint",
        {"blueprint": {"refPath": ref}},
        task=task,
        timeout=UE_MCP_TIMEOUT,
    )
    if not called.get("ok"):
        message = str(called.get("error"))
        return _result(False, "blueprint_compile", "编译失败", [{"file": ref, "line": 0, "message": message}])
    if _empty_success(called.get("data")):
        return _result(True, "blueprint_compile", "compile_blueprint 返回空")
    message = json.dumps(called.get("data"), ensure_ascii=False)
    return _result(False, "blueprint_compile", "返回不是空成功", [{"file": ref, "line": 0, "message": message}])


def cpp_build_argv() -> list[str]:
    """编译编辑器目标。先关编辑器再编，启动后的 UnrealEditor 才会带上新的 UPROPERTY。

    游戏目标 AIAgentProject 编出来的 DLL 编辑器不加载。编辑器开着时 Live Coding
    会拒绝 AIAgentProjectEditor，所以调用方必须先关掉编辑器。
    """
    build_bat = UE_ENGINE_ROOT / "Engine" / "Build" / "BatchFiles" / "Build.bat"
    return [
        "cmd.exe", "/d", "/c", str(build_bat),
        "AIAgentProjectEditor", "Win64", "Development",
        f"-Project={UE_PROJECT}", "-WaitMutex",
    ]


def _parse_cpp_errors(log: str) -> list[dict]:
    found = []
    for line in log.splitlines():
        if not (_ERROR_C.search(line) or _FATAL.search(line)):
            continue
        file_name = ""
        line_no = 0
        match = _FILE_LINE.search(line)
        if match:
            file_name = match.group(1)
            line_no = int(match.group(2))
        found.append({"file": file_name, "line": line_no, "message": line.strip()})
        if len(found) >= 20:
            break
    return found


def _cpp_build() -> dict:
    if UE_EXEC_MODE == "mock":
        return _result(True, "cpp_build", "mock")
    from core.editor_launch import close_editor
    closed = close_editor()
    if not closed.get("ok"):
        message = str(closed.get("note") or "编辑器未能关闭")
        return _result(False, "cpp_build", message, [{"file": "", "line": 0, "message": message}])
    try:
        argv = cpp_build_argv()
    except RuntimeError as exc:
        return _result(False, "cpp_build", str(exc), [{"file": "", "line": 0, "message": str(exc)}])
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=UE_MCP_TIMEOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return _result(False, "cpp_build", "编译超时", [{"file": "", "line": 0, "message": f"timeout {UE_MCP_TIMEOUT}s"}])
    log = (proc.stdout or "") + "\n" + (proc.stderr or "")
    errors = _parse_cpp_errors(log)
    if proc.returncode == 0 and not errors:
        return _result(True, "cpp_build", "exit 0")
    if not errors:
        tail = "\n".join(log.strip().splitlines()[-5:])
        errors = [{"file": "", "line": 0, "message": f"exit {proc.returncode} {tail}".strip()}]
    return _result(False, "cpp_build", errors[0]["message"], errors)


def _fail_count(data) -> int | None:
    data = _unwrap(data)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    if "returnValue" in data:
        inner = _fail_count(data["returnValue"])
        if inner is not None:
            return inner
    for key in ("failCount", "failedCount", "numFailed", "failed"):
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    tests = data.get("tests") or data.get("results") or data.get("testResults")
    if isinstance(tests, list):
        count = 0
        saw = False
        for item in tests:
            if not isinstance(item, dict):
                continue
            state = str(item.get("state") or item.get("result") or item.get("status") or "")
            if not state:
                continue
            saw = True
            if "fail" in state.lower():
                count += 1
        if saw:
            return count
    return None


def _failed_names(data) -> list[str]:
    names = []
    for item in _walk_tests(data):
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or item.get("result") or item.get("status") or "")
        if "fail" not in state.lower():
            continue
        name = item.get("fullTestPath") or item.get("testName") or item.get("name") or item.get("path") or item.get("test") or ""
        if name:
            names.append(str(name))
    return names


def _walk_tests(data) -> list:
    data = _unwrap(data)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    found = []
    for key in ("tests", "results", "testResults"):
        value = data.get(key)
        if isinstance(value, list):
            found.extend(item for item in value if isinstance(item, dict))
    if "returnValue" in data:
        found.extend(_walk_tests(data.get("returnValue")))
    return found


def _automation(spec: dict, task: dict) -> dict:
    global _discovered
    if UE_EXEC_MODE == "mock":
        return _result(True, "automation", "mock")
    if not _discovered:
        discovered = exec_tool(_AUTO_TOOLSET, "DiscoverTests", {}, task=task, timeout=UE_MCP_TIMEOUT)
        if not discovered.get("ok"):
            message = str(discovered.get("error"))
            return _result(False, "automation", "DiscoverTests 失败", [{"file": "", "line": 0, "message": message}])
        _discovered = True
    filt = spec.get("filter") or spec.get("filterExpression") or ""
    if not filt:
        return _result(False, "automation", "缺少 filter", [{"file": "", "line": 0, "message": "automation 没有 filter"}])
    ran = exec_tool(
        _AUTO_TOOLSET,
        "RunTestsByFilter",
        {"filterExpression": filt},
        task=task,
        timeout=UE_MCP_TIMEOUT,
    )
    if not ran.get("ok"):
        message = str(ran.get("error"))
        return _result(False, "automation", "RunTestsByFilter 失败", [{"file": "", "line": 0, "message": message}])
    results = exec_tool(_AUTO_TOOLSET, "GetTestResults", {}, task=task, timeout=UE_MCP_TIMEOUT)
    if not results.get("ok"):
        message = str(results.get("error"))
        return _result(False, "automation", "GetTestResults 失败", [{"file": "", "line": 0, "message": message}])
    failed = _fail_count(results.get("data"))
    if failed is None:
        return _result(False, "automation", "无法解析失败数", [{"file": "", "line": 0, "message": "GetTestResults 里没有失败数字段"}])
    if failed != 0:
        names = _failed_names(results.get("data"))
        shown = ",".join(names[:8])
        message = f"failed={failed} filter={filt} tests={shown}".strip()
        return _result(False, "automation", message, [{"file": "", "line": 0, "message": message}])
    return _result(True, "automation", "失败数 0")


def judge(spec: dict, task: dict | None = None) -> dict:
    kind = (spec or {}).get("kind") or ""
    task = _read_task(task)
    if kind == "editor_assert":
        return _editor_assert(spec, task)
    if kind == "blueprint_compile":
        return _blueprint_compile(spec, task)
    if kind == "cpp_build":
        return _cpp_build()
    if kind == "automation":
        return _automation(spec, task)
    return _result(False, kind or "unknown", "未知裁判", [{"file": "", "line": 0, "message": f"unknown kind {kind}"}])
