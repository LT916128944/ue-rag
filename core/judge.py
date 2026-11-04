# core/judge.py
"""真裁判。返回 {ok, kind, summary, errors:[{file, line, message}]}。"""
import contextlib
import json
import re

from config import UE_ENGINE_ROOT, UE_EXEC_MODE, UE_MCP_TIMEOUT, UE_PROJECT
from core import editor_exec
from core.editor_exec import MOCK_ACTORS

_ERROR_C = re.compile(r"error C\d+")
_FATAL = re.compile(r"fatal error", re.I)
_FILE_LINE = re.compile(r"((?:[A-Za-z]:\\)?[^\s()]+\.(?:h|cpp|hpp|cs))\((\d+)")

_ACTOR_TOOLSET = "editor_toolset.toolsets.actor.ActorTools"
_SCENE_TOOLSET = "editor_toolset.toolsets.scene.SceneTools"
_BP_TOOLSET = "editor_toolset.toolsets.blueprint.BlueprintTools"
_ASSET_TOOLSET = "editor_toolset.toolsets.asset.AssetTools"
_WIDGET_COMPILE = "CompileWidgetBlueprint"
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


def _actor_names(spec: dict) -> list[str]:
    names = []
    for item in spec.get("actor_names") or []:
        text = str(item or "").strip()
        if text and text not in names:
            names.append(text)
    one = str(spec.get("actor_name") or "").strip()
    if one and one not in names:
        names.insert(0, one)
    return names


def _location_text(name: str, loc: dict) -> str:
    return f"{name} x={loc.get('x')} y={loc.get('y')} z={loc.get('z')}"


def _box_mismatches(loc: dict, box: dict, tolerance: float = 1.0) -> list[str]:
    """点落在盒子内。容差 1 uu：出界不超过 1 uu 仍算在里面。"""
    bounds = box or {}
    low = bounds.get("min") or {}
    high = bounds.get("max") or {}
    mismatches = []
    for axis in ("x", "y", "z"):
        try:
            actual = float(loc[axis])
            lo = float(low[axis])
            hi = float(high[axis])
        except (KeyError, TypeError, ValueError):
            mismatches.append(f"{axis} 缺失")
            continue
        if actual < lo - tolerance or actual > hi + tolerance:
            mismatches.append(f"{axis}={actual} 不在 [{lo}, {hi}]")
    return mismatches


def _find_unique_actor(name: str, task: dict) -> tuple[str, dict | None]:
    found = editor_exec.exec_tool(
        _SCENE_TOOLSET,
        "find_actors",
        {"name": name, "tag": "", "collision_channels": []},
        task=task,
    )
    if not found.get("ok"):
        return "", _result(False, "editor_assert", "find_actors 失败", [{"file": "", "line": 0, "message": str(found.get("error"))}])
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
        return "", _result(False, "editor_assert", "Actor 不唯一", [{"file": "", "line": 0, "message": f"{name} 候选: {listed}"}])
    return paths[0], None


def _read_actor_location(spec: dict, ref: str, task: dict) -> tuple[dict | None, dict | None]:
    tool = spec.get("tool") or "get_actor_transform"
    toolset = spec.get("toolset") or _ACTOR_TOOLSET
    args = dict(spec.get("args") or {})
    args["actor"] = {"refPath": ref}
    called = editor_exec.exec_tool(toolset, tool, args, task=task)
    if not called.get("ok"):
        return None, _result(False, "editor_assert", "读工具失败", [{"file": "", "line": 0, "message": str(called.get("error"))}])
    loc = _location(called.get("data"))
    if loc is None:
        return None, _result(False, "editor_assert", "没有 location", [{"file": "", "line": 0, "message": "editor 返回里没有 location"}])
    return loc, None


def _using_monolith() -> bool:
    from backends.select import current_backend

    return current_backend() == "monolith"


def _monolith_location(data) -> dict | None:
    """2026-10-26 实读 mesh.get_actor_info：transform.location 是 [x, y, z]。"""
    obj = _unwrap(data)
    if not isinstance(obj, dict):
        return None
    transform = obj.get("transform")
    if not isinstance(transform, dict):
        return None
    raw = transform.get("location")
    if not isinstance(raw, list) or len(raw) < 3:
        return None
    try:
        return {"x": float(raw[0]), "y": float(raw[1]), "z": float(raw[2])}
    except (TypeError, ValueError):
        return None


def _monolith_editor_assert(spec: dict, task: dict) -> dict:
    from backends.monolith import exec as monolith_exec

    names = _actor_names(spec)
    if len(names) != 1:
        return _result(False, "editor_assert", "缺少 actor", [{"file": "", "line": 0, "message": "editor_assert 没有唯一的 actor_name"}])
    called = monolith_exec("mesh", "get_actor_info", {"actor_name": names[0]}, task=task)
    if not called.get("ok"):
        return _result(False, "editor_assert", "读工具失败", [{"file": "", "line": 0, "message": str(called.get("error"))}])
    return _compare_location(spec, _monolith_location(called.get("data")), "get_actor_info")


def _monolith_blueprint_compile(spec: dict, task: dict) -> dict:
    """2026-10-26 实读 blueprint.compile_blueprint：success 为 true 且 error_count 为 0。不保存。"""
    from backends.monolith import exec as monolith_exec

    ref = spec.get("refPath") or ""
    if not ref:
        return _result(False, "blueprint_compile", "缺少 refPath", [{"file": "", "line": 0, "message": "blueprint_compile 没有 refPath"}])
    tool = str(spec.get("tool") or "compile_blueprint")
    if tool in (_WIDGET_COMPILE, "compile_widget"):
        called = monolith_exec("ui", "compile_widget", {"asset_path": ref}, task=task)
    else:
        called = monolith_exec("blueprint", "compile_blueprint", {"asset_path": ref}, task=task)
    if not called.get("ok"):
        return _result(False, "blueprint_compile", "编译失败", [{"file": ref, "line": 0, "message": str(called.get("error"))}])
    data = _unwrap(called.get("data"))
    if isinstance(data, dict) and data.get("success") is True and int(data.get("error_count") or 0) == 0:
        return _result(True, "blueprint_compile", "compile_blueprint success")
    message = json.dumps(data, ensure_ascii=False)
    return _result(False, "blueprint_compile", "编译未成功", [{"file": ref, "line": 0, "message": message}])


def _editor_assert(spec: dict, task: dict) -> dict:
    names = _actor_names(spec)
    box = ((spec.get("expect") or {}).get("box"))
    if UE_EXEC_MODE == "mock":
        if not names:
            names = [spec.get("actor_name") or (spec.get("args") or {}).get("actor", {}).get("refPath", "")]
        if box:
            readings = []
            mismatches = []
            for name in names:
                loc = (MOCK_ACTORS.get(name) or {}).get("location")
                if loc is None:
                    return _result(False, "editor_assert", "没有 location", [{"file": "", "line": 0, "message": f"mock 没有 {name}"}])
                readings.append(_location_text(name, loc))
                mismatches.extend(_box_mismatches(loc, box))
            summary = "; ".join(readings)
            if mismatches:
                message = f"{summary} {'; '.join(mismatches)}".strip()
                return _result(False, "editor_assert", message, [{"file": "", "line": 0, "message": message}])
            return _result(True, "editor_assert", f"在盒子内 {summary}")
        name = names[0] if names else ""
        loc = (MOCK_ACTORS.get(name) or {}).get("location")
        return _compare_location(spec, loc, "mock")

    if _using_monolith():
        return _monolith_editor_assert(spec, task)

    if box:
        if not names:
            return _result(False, "editor_assert", "缺少 actor", [{"file": "", "line": 0, "message": "editor_assert 没有 actor"}])
        readings = []
        mismatches = []
        for name in names:
            ref, error = _find_unique_actor(name, task)
            if error:
                return error
            loc, error = _read_actor_location(spec, ref, task)
            if error:
                return error
            readings.append(_location_text(name, loc))
            mismatches.extend(_box_mismatches(loc, box))
        summary = "; ".join(readings)
        if mismatches:
            message = f"{summary} {'; '.join(mismatches)}".strip()
            return _result(False, "editor_assert", message, [{"file": "", "line": 0, "message": message}])
        return _result(True, "editor_assert", f"在盒子内 {summary}")

    tool = spec.get("tool") or "get_actor_transform"
    toolset = spec.get("toolset") or _ACTOR_TOOLSET
    args = dict(spec.get("args") or {})
    if tool == "get_actor_transform" and "actor" not in args:
        if len(names) != 1:
            return _result(False, "editor_assert", "缺少 actor", [{"file": "", "line": 0, "message": "editor_assert 没有 actor"}])
        ref, error = _find_unique_actor(names[0], task)
        if error:
            return error
        args["actor"] = {"refPath": ref}

    called = editor_exec.exec_tool(toolset, tool, args, task=task)
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
    shown = _location_text(spec.get("actor_name") or "actor", loc)
    if mismatches:
        message = f"{shown} " + "; ".join(mismatches)
        return _result(False, "editor_assert", message, [{"file": "", "line": 0, "message": message}])
    return _result(True, "editor_assert", f"location 匹配 {shown}")


def _widget_compiled(ref: str, data) -> dict:
    """控件图编译成功后保存这份资产。返回 false 或带 error 算失败。"""
    raw = data if isinstance(data, dict) else {}
    flat = _unwrap(data)
    if flat is False or raw.get("error") or raw.get("success") is False or raw.get("returnValue") is False:
        message = json.dumps(data, ensure_ascii=False) if data not in (None, "") else "CompileWidgetBlueprint 返回失败"
        return _result(False, "blueprint_compile", "控件图编译失败", [{"file": ref, "line": 0, "message": message}])
    package = str(ref).split(".", 1)[0]
    saved = editor_exec.exec_tool(
        _ASSET_TOOLSET,
        "save_assets",
        {"asset_paths": [package]},
        task={"allow_writes": [], "save": True, "pie": False},
        timeout=UE_MCP_TIMEOUT,
    )
    if not saved.get("ok"):
        message = str(saved.get("error") or "未存上")
        return _result(False, "blueprint_compile", "控件图已编译，保存这份资产失败", [{"file": ref, "line": 0, "message": message}])
    return _result(True, "blueprint_compile", "控件图已编译并保存这份资产")


def _blueprint_compile(spec: dict, task: dict) -> dict:
    if UE_EXEC_MODE == "mock":
        return _result(True, "blueprint_compile", "mock")
    if _using_monolith():
        return _monolith_blueprint_compile(spec, task)
    ref = spec.get("refPath") or ""
    if not ref:
        return _result(False, "blueprint_compile", "缺少 refPath", [{"file": "", "line": 0, "message": "blueprint_compile 没有 refPath"}])
    tool = str(spec.get("tool") or "compile_blueprint")
    toolset = str(spec.get("toolset") or _BP_TOOLSET)
    arg = "widgetBlueprint" if tool == _WIDGET_COMPILE else "blueprint"
    called = editor_exec.exec_tool(
        toolset,
        tool,
        {arg: {"refPath": ref}},
        task=task,
        timeout=UE_MCP_TIMEOUT,
    )
    if not called.get("ok"):
        message = str(called.get("error"))
        return _result(False, "blueprint_compile", "编译失败", [{"file": ref, "line": 0, "message": message}])
    if tool == _WIDGET_COMPILE:
        return _widget_compiled(ref, called.get("data"))
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


def cpp_build_verdict(outcome: dict) -> dict:
    """超时和停止先记失败。不看退出码，避免杀掉进程后被写成编译成功。"""
    reason = str(outcome.get("reason") or "")
    if reason == "timeout":
        message = f"timeout {outcome.get('timeout_s')}s"
        return _result(False, "cpp_build", "编译超时", [{"file": "", "line": 0, "message": message}])
    if reason == "stopped":
        return _result(False, "cpp_build", "编译已停止", [{"file": "", "line": 0, "message": "stopped"}])
    log = (outcome.get("stdout") or "") + "\n" + (outcome.get("stderr") or "")
    errors = _parse_cpp_errors(log)
    code = outcome.get("returncode")
    if code == 0 and not errors:
        return _result(True, "cpp_build", "exit 0")
    if not errors:
        tail = "\n".join(log.strip().splitlines()[-5:])
        errors = [{"file": "", "line": 0, "message": f"exit {code} {tail}".strip()}]
    return _result(False, "cpp_build", errors[0]["message"], errors)


def _cpp_build() -> dict:
    if UE_EXEC_MODE == "mock":
        return _result(True, "cpp_build", "mock")
    # 全局编译锁：UBT 并行必崩（20261028-171355：两扇 solve 并行编译 → exit 6
    # Unhandled aggregate exceptions）。同一时刻只许一个编译进程，等锁有上限。
    with _build_lock():
        from core.editor_launch import close_editor
        closed = close_editor()
        if not closed.get("ok"):
            message = str(closed.get("note") or "编辑器未能关闭")
            return _result(False, "cpp_build", message, [{"file": "", "line": 0, "message": message}])
        try:
            argv = cpp_build_argv()
        except RuntimeError as exc:
            return _result(False, "cpp_build", str(exc), [{"file": "", "line": 0, "message": str(exc)}])
        from core.build_job import run_command
        try:
            outcome = run_command(argv, UE_MCP_TIMEOUT)
        except OSError as exc:
            verdict = _result(False, "cpp_build", str(exc), [{"file": "", "line": 0, "message": str(exc)}])
            _record_build(verdict)
            return verdict
        verdict = cpp_build_verdict(outcome)
        _record_build(verdict)
        return verdict


@contextlib.contextmanager
def _build_lock(timeout_seconds: float = 1800.0):
    """跨进程互斥的编译锁。拿不到就等（编译一跑几分钟，等待是正常态），超时抛 RuntimeError。"""
    import time

    from config import DATA_DIR

    path = DATA_DIR / "build.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    locked = False
    deadline = time.time() + timeout_seconds
    try:
        while True:
            try:
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                if time.time() >= deadline:
                    raise RuntimeError("编译锁等待超时（1800s）：另一场编译还没结束")
                time.sleep(5.0)
        yield
    finally:
        if locked:
            try:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()


def _record_build(verdict: dict) -> None:
    """执行面每次真编译都落 last_cpp_build.json：get_build_error 才读得到（20261028 P0-3）。
    mock 模式不落，别把「mock」当成真实编译记录。"""
    if UE_EXEC_MODE == "mock":
        return
    try:
        from core.contract_tools import record

        record(verdict)
    except Exception:
        pass


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


def _test_names(data) -> list[str]:
    """跑到的测试名。0 个也要能看出来：0 个测试的 failed=0 不等于条款被证明。"""
    names: list[str] = []
    for item in _walk_tests(data):
        if not isinstance(item, dict):
            continue
        name = (
            item.get("fullTestPath") or item.get("testName") or item.get("name")
            or item.get("path") or item.get("test") or ""
        )
        if name and str(name) not in names:
            names.append(str(name))
    return names


def _test_total(data) -> int | None:
    """这次过滤器匹配到多少个测试。字段读不到时按 tests 列表条目数算；都没有返回 None。"""
    data = _unwrap(data)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if isinstance(data, dict) and "returnValue" in data:
        inner = _test_total(data["returnValue"])
        if inner is not None:
            return inner
    if isinstance(data, dict):
        for key in ("totalTests", "totalCount", "numTests", "testsRun", "runCount", "matched"):
            value = data.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return int(value)
    tests = _walk_tests(data)
    return len(tests) if tests else None


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


def _note_text(entry) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        for key in ("message", "text", "warning", "error"):
            value = entry.get(key)
            if value:
                return str(value).strip()
        return json.dumps(entry, ensure_ascii=False)
    return str(entry or "").strip()


def _result_notes(data) -> list[str]:
    notes = []
    for item in _walk_tests(data):
        for key in ("warnings", "messages", "entries"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                notes.append(value.strip())
            elif isinstance(value, list):
                for entry in value:
                    text = _note_text(entry)
                    if text:
                        notes.append(text)
    return notes[:8]


def _monolith_names(data: dict) -> tuple[list[str], list[str]]:
    names: list[str] = []
    notes: list[str] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("full_path") or item.get("test_name") or "").strip()
        if name and name not in names:
            names.append(name)
        for entry in item.get("errors") or []:
            text = str(entry).strip()
            if text:
                notes.append(text)
    return names, notes[:8]


def _monolith_automation(spec: dict, task: dict) -> dict:
    """monolith 不走官方 DiscoverTests。editor.run_automation_tests 按前缀同步跑完。"""
    from backends.monolith import exec as monolith_exec

    filt = str(spec.get("filter") or spec.get("filterExpression") or "").strip()
    if not filt:
        return _result(False, "automation", "缺少 filter", [{"file": "", "line": 0, "message": "automation 没有 filter"}])
    called = monolith_exec("editor", "run_automation_tests", {"prefix": filt}, task=task)
    if not called.get("ok"):
        message = str(called.get("error") or "run_automation_tests 失败")
        return _result(False, "automation", message, [{"file": "", "line": 0, "message": message}])
    data = _unwrap(called.get("data"))
    if not isinstance(data, dict):
        message = "run_automation_tests 没有结果"
        return _result(False, "automation", message, [{"file": "", "line": 0, "message": message}])
    failed = data.get("failed")
    total = data.get("total")
    if isinstance(failed, bool) or not isinstance(failed, (int, float)):
        message = "run_automation_tests 里没有失败数"
        return _result(False, "automation", message, [{"file": "", "line": 0, "message": message}])
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        message = "run_automation_tests 里没有测试数"
        return _result(False, "automation", message, [{"file": "", "line": 0, "message": message}])
    failed_n = int(failed)
    total_n = int(total)
    names, notes = _monolith_names(data)
    passed = data.get("passed")
    passed_n = int(passed) if isinstance(passed, (int, float)) and not isinstance(passed, bool) else None
    if data.get("success") is not True or failed_n != 0 or total_n == 0 or passed_n == 0:
        message = str(data.get("message") or "").strip()
        shown = ",".join(names[:8])
        if not message:
            if total_n == 0 or not names:
                message = f"failed={failed_n} 但匹配到 0 个测试，filter={filt}"
            elif passed_n == 0 and failed_n == 0:
                message = f"failed=0 但没有通过的测试，filter={filt} tests={shown}".strip()
            else:
                message = f"failed={failed_n} filter={filt} tests={shown}".strip()
        if notes:
            message = f"{message} {' '.join(notes)}".strip()
        message = message[:500]
        return _result(False, "automation", message, [{"file": "", "line": 0, "message": message}])
    summary = f"失败数 0（{total_n} 个测试）"
    shown = ",".join(names[:8])
    if shown:
        summary = f"{summary} tests={shown}"
    verdict = _result(True, "automation", summary[:500])
    verdict["tests"] = names[:16]
    verdict["filter"] = filt
    return verdict


def _automation(spec: dict, task: dict) -> dict:
    global _discovered
    if UE_EXEC_MODE == "mock":
        return _result(True, "automation", "mock")
    if _using_monolith():
        return _monolith_automation(spec, task)
    if not _discovered:
        discovered = editor_exec.exec_tool(_AUTO_TOOLSET, "DiscoverTests", {}, task=task, timeout=UE_MCP_TIMEOUT)
        if not discovered.get("ok"):
            message = str(discovered.get("error") or "")
            summary = "DiscoverTests 失败" + (f"：{message}" if message else "")
            return _result(False, "automation", summary, [{"file": "", "line": 0, "message": message or summary}])
        _discovered = True
    filt = spec.get("filter") or spec.get("filterExpression") or ""
    if not filt:
        return _result(False, "automation", "缺少 filter", [{"file": "", "line": 0, "message": "automation 没有 filter"}])
    ran = editor_exec.exec_tool(
        _AUTO_TOOLSET,
        "RunTestsByFilter",
        {"filterExpression": filt},
        task=task,
        timeout=UE_MCP_TIMEOUT,
    )
    if not ran.get("ok"):
        message = str(ran.get("error"))
        return _result(False, "automation", "RunTestsByFilter 失败", [{"file": "", "line": 0, "message": message}])
    results = editor_exec.exec_tool(_AUTO_TOOLSET, "GetTestResults", {}, task=task, timeout=UE_MCP_TIMEOUT)
    if not results.get("ok"):
        message = str(results.get("error"))
        return _result(False, "automation", "GetTestResults 失败", [{"file": "", "line": 0, "message": message}])
    failed = _fail_count(results.get("data"))
    if failed is None:
        return _result(False, "automation", "无法解析失败数", [{"file": "", "line": 0, "message": "GetTestResults 里没有失败数字段"}])
    total = _test_total(results.get("data"))
    names = _test_names(results.get("data")) or _failed_names(results.get("data"))
    if failed != 0:
        shown = ",".join(_failed_names(results.get("data"))[:8])
        message = f"failed={failed} filter={filt} tests={shown}".strip()
        notes = _result_notes(results.get("data"))
        if notes:
            message = f"{message} {' '.join(notes)}".strip()
        return _result(False, "automation", message, [{"file": "", "line": 0, "message": message}])
    # 0 失败必须配上「至少真跑了一个测试」。一条都没匹配到时 failed=0 同样成立，
    # 拿它当通过等于没验（20261021-195220 的 W5 就是这样勾上了讲关卡内容的条款）。
    if total == 0 or (total is None and not names):
        message = f"failed=0 但匹配到 0 个测试，filter={filt}".strip()
        return _result(False, "automation", message, [{"file": "", "line": 0, "message": message}])
    notes = _result_notes(results.get("data"))
    summary = f"失败数 0（{total if total is not None else len(names)} 个测试）"
    shown = ",".join(names[:8])
    if shown:
        summary = f"{summary} tests={shown}".strip()
    if notes:
        summary = f"{summary} {' '.join(notes)}".strip()
    verdict = _result(True, "automation", summary[:500])
    verdict["tests"] = names[:16]
    verdict["filter"] = filt
    return verdict


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

