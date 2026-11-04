"""离线检查：编辑器后端缺省 official；非法值和选错入口都拒绝。不连 8000，也不连 9316。"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SEE = "backends/select.py"


def fail(msg: str, fix: str) -> None:
    print(f"❌ {msg}")
    print(f"✅ FIX: {fix}")
    print(f"📖 See: {SEE}")
    sys.exit(1)


def _boom(*_args, **_kwargs):
    raise AssertionError("选错路径仍然去连编辑器")


def check_defaults() -> None:
    import config
    from core.graph_write_policy import editor_backend, env_value

    previous = os.environ.pop("UE_EDITOR_BACKEND", None)
    try:
        file_raw = env_value(ROOT / ".env", "UE_EDITOR_BACKEND")
        if file_raw is None or not str(file_raw).strip():
            if config.editor_backend() != "official":
                fail(f"未设置 UE_EDITOR_BACKEND 时 editor_backend()={config.editor_backend()!r}",
                     "config.editor_backend 在键缺失时返回 official")
        if config.monolith_url() != "http://127.0.0.1:9316/mcp" and env_value(ROOT / ".env", "UE_MONOLITH_URL") not in (None, ""):
            pass
        elif env_value(ROOT / ".env", "UE_MONOLITH_URL") in (None, ""):
            if os.getenv("UE_MONOLITH_URL"):
                pass
            elif config.monolith_url() != "http://127.0.0.1:9316/mcp":
                fail(f"monolith_url 缺省是 {config.monolith_url()!r}",
                     "config.monolith_url 缺省 http://127.0.0.1:9316/mcp")
    finally:
        if previous is not None:
            os.environ["UE_EDITOR_BACKEND"] = previous

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text("UE_EDITOR_BACKEND=\nUE_MONOLITH_URL=\n", encoding="utf-8")
        if editor_backend(path) != "official":
            fail("空的 UE_EDITOR_BACKEND 没有当成 official",
                 "graph_write_policy.normalize_backend 把空字符串收成 official")
        path.write_text('UE_EDITOR_BACKEND="Monolith"\n', encoding="utf-8")
        if editor_backend(path) != "monolith":
            fail(f"文件里的 Monolith 读成 {editor_backend(path)!r}",
                 "editor_backend 每次读 .env，并忽略大小写")
        path.write_text("UE_EDITOR_BACKEND=both\n", encoding="utf-8")
        if editor_backend(path) != "both":
            fail(f"非法值被收成 {editor_backend(path)!r}",
                 "非法拼写保留小写，不要偷偷改成 official")


def _use(name: str):
    import config
    from backends import select

    select.reset_lock()
    original = config.editor_backend
    config.editor_backend = lambda: name
    return original


def _restore(original) -> None:
    import config
    from backends import select

    config.editor_backend = original
    select.reset_lock()


def check_refusals() -> None:
    from backends import monolith, select

    original = _use("official")
    try:
        for call in (
            lambda: monolith.discover("blueprint", transport=_boom),
            lambda: monolith.describe("blueprint", "get_graph_summary", transport=_boom),
            lambda: monolith.exec("blueprint", "add_node", {}, transport=_boom),
        ):
            payload = call()
            if "UE_EDITOR_BACKEND=official" not in str(payload.get("error")):
                fail(f"official 时 Monolith 入口没有拒绝：{payload}",
                     "backends.monolith 三个函数在 official 时返回含 UE_EDITOR_BACKEND=official 的 error")
        if select.official_refusal():
            fail("official 时官方入口也被拒绝了",
                 "official_refusal 只在后端不是 official 时返回原文")
    finally:
        _restore(original)

    original = _use("monolith")
    try:
        text = select.official_refusal()
        if "UE_EDITOR_BACKEND=monolith" not in text:
            fail(f"monolith 时官方拒绝原文是 {text!r}",
                 "official_refusal 返回 UE_EDITOR_BACKEND=monolith，且不要改调 9316")
        seen = {}

        def transport(tool_name, arguments):
            seen["tool"] = tool_name
            seen["args"] = arguments
            return {"ok": True, "data": {"namespaces": [{"namespace": "blueprint"}]}}

        payload = monolith.discover("", transport=transport)
        if payload.get("namespaces") != ["blueprint"] or seen.get("tool") != "monolith_discover":
            fail(f"discover 结果是 {payload!r}，调用 {seen!r}",
                 "backend=monolith 时 discover 只调 monolith_discover，不扫描官方工具")
        if "detail" in (seen.get("args") or {}) or "verbose" in (seen.get("args") or {}):
            fail(f"discover 把 detail 传出去了：{seen!r}", "discover 禁止 detail=true")
    finally:
        _restore(original)

    original = _use("both")
    try:
        official = select.official_refusal()
        other = select.monolith_refusal()
        for text in (official, other):
            if "official" not in text or "monolith" not in text:
                fail(f"非法值的拒绝原文没有同时写出两个允许值：{text!r}",
                     "backend_error 写明只能是 official 或 monolith")
        payload = monolith.exec("blueprint", "get_graph_summary", {}, transport=_boom)
        if payload.get("ok") is not False or "official" not in str(payload.get("error")):
            fail(f"非法值仍然执行了 exec：{payload}",
                 "非法 UE_EDITOR_BACKEND 时 exec 直接拒绝，不调用 transport")
    finally:
        _restore(original)


def check_lock_and_event() -> None:
    import config
    from backends import select

    select.reset_lock()
    original = config.editor_backend
    events = []
    try:
        config.editor_backend = lambda: "official"
        if select.bind_run(lambda etype, **fields: events.append((etype, fields))) != "official":
            fail("bind_run 没有锁住入口读到的后端", "bind_run 调用 lock_backend 并 emit editor_backend")
        config.editor_backend = lambda: "monolith"
        if select.current_backend() != "official":
            fail("运行中改后端之后 current_backend 跟着变了",
                 "lock_backend 之后本进程保持入口那一次的值")
        if events != [("editor_backend", {"backend": "official"})]:
            fail(f"运行事件是 {events!r}", "bind_run 写一条 type=editor_backend 的事件")
    finally:
        config.editor_backend = original
        select.reset_lock()

    from agent import run_windows
    source = Path(run_windows.execute_windows.__code__.co_filename)
    text = source.read_text(encoding="utf-8")
    if "bind_run" not in text:
        fail("execute_windows 没有锁后端", "在 _EventBus 创建后调用 backends.select.bind_run")


def check_script_refusal() -> None:
    env = os.environ.copy()
    env["UE_EDITOR_BACKEND"] = "monolith"
    env.pop("UE_MONOLITH_URL", None)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "call_mcp_tool.py"), "--tool", "find_actors", "--args", "{}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        env=env,
    )
    if "UE_EDITOR_BACKEND=monolith" not in (proc.stdout or ""):
        fail(f"call_mcp_tool 拒绝原文是 stdout={proc.stdout!r} stderr={proc.stderr!r}",
             "scripts/call_mcp_tool.py 在连 8000 之前拒绝，原文含 UE_EDITOR_BACKEND=monolith")
    scan = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "scan_mcp_tools.py"), "--query", "actor"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        env=env,
    )
    if "UE_EDITOR_BACKEND=monolith" not in (scan.stdout or ""):
        fail(f"scan_mcp_tools 拒绝原文是 stdout={scan.stdout!r}",
             "scripts/scan_mcp_tools.py 在检索官方工具之前拒绝")


def check_write_policy() -> None:
    """exec 在连 9316 之前看写许可。这里不启动编辑器。"""
    from backends import monolith

    calls: list = []

    def transport(tool_name, arguments):
        calls.append((tool_name, arguments))
        return {"ok": True, "data": {"nodes": 2, "graph": "EventGraph"}}

    previous = {key: os.environ.get(key) for key in (
        "UE_ALLOW_BLUEPRINT", "UE_ALLOW_AI", "UE_ALLOW_MONOLITH_WRITE",
    )}
    original = _use("monolith")
    try:
        os.environ["UE_ALLOW_BLUEPRINT"] = "false"
        os.environ["UE_ALLOW_AI"] = "false"
        os.environ["UE_ALLOW_MONOLITH_WRITE"] = "false"
        denied = monolith.exec("blueprint", "add_node", {}, transport=transport)
        if "UE_ALLOW_BLUEPRINT" not in str(denied.get("error")) or calls:
            fail(f"add_node 拒绝是 {denied!r}，调用 {calls!r}",
                 "UE_ALLOW_BLUEPRINT=false 时 blueprint 非只读 action 拒绝，且不调用 transport")
        calls.clear()
        allowed = monolith.exec("blueprint", "get_graph_summary", {"asset_path": "/Game/BP_Gun"}, transport=transport)
        if not allowed.get("ok") or not calls:
            fail(f"get_graph_summary 被挡下：{allowed!r}",
                 "get_ 前缀是只读，UE_ALLOW_BLUEPRINT=false 时仍放行")
        calls.clear()
        ai = monolith.exec("ai", "add_bt_node", {}, transport=transport)
        if "UE_ALLOW_AI" not in str(ai.get("error")) or calls:
            fail(f"add_bt_node 拒绝是 {ai!r}", "UE_ALLOW_AI=false 时 ai 的非只读 action 拒绝")
        gas = monolith.exec("gas", "add_effect", {}, transport=transport)
        if "UE_ALLOW_MONOLITH_WRITE" not in str(gas.get("error")) or calls:
            fail(f"gas 写拒绝是 {gas!r}", "UE_ALLOW_MONOLITH_WRITE=false 时 gas 非只读拒绝")
        python = monolith.exec("editor", "run_python", {}, task={"allow_python": False}, transport=transport)
        if "allow_python" not in str(python.get("error")) or calls:
            fail(f"run_python 拒绝是 {python!r}", "任务没写 allow_python: true 时拒绝 run_python")
        os.environ["UE_ALLOW_MONOLITH_WRITE"] = "true"
        level = monolith.exec("editor", "load_level", {}, task={"save": False}, transport=transport)
        if level.get("ok") is not False or calls:
            fail(f"load_level 在 save=false 时放行了：{level!r}",
                 "load_level 不跟 UE_ALLOW_MONOLITH_WRITE，看任务书的 save")
        graph = monolith.exec("blueprint", "get_graph_data", {}, transport=transport)
        if "get_graph_summary" not in str(graph.get("error")) or calls:
            fail(f"无过滤 get_graph_data 是 {graph!r}",
                 "get_graph_data 没有 filter/graph_name/node_id 时拒绝")
        detail = monolith.exec("blueprint", "get_graph_summary", {"detail": True}, transport=transport)
        if "detail=true" not in str(detail.get("error")) or calls:
            fail(f"detail=true 没有被拒绝：{detail!r}", "exec 禁止把 detail=true 发给 9316")
        args = monolith.discover_arguments("blueprint")
        if "detail" in args or "verbose" in args or args.get("namespace") != "blueprint":
            fail(f"discover 参数是 {args!r}", "discover_arguments 只带 namespace，不带 detail")
        pcg = monolith.exec("pcg", "get_graph", {}, transport=transport)
        if pcg.get("error") != "Monolith 无此命名空间" or calls:
            fail(f"PCG 结果是 {pcg!r}", "backend=monolith 时 PCG 固定失败，不回退官方 MCP")
        compiled = monolith.exec("blueprint", "compile_blueprint", {}, transport=transport)
        if not compiled.get("ok") or not calls:
            fail(f"compile_blueprint 被写开关挡住：{compiled!r}",
                 "compile_blueprint 是编译裁判，开关为 false 时仍允许，且不保存")
    finally:
        _restore(original)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def check_live_official_shape() -> None:
    """backend 钉成 official 时，官方拒绝文案仍是原来的 blueprint edit disabled。"""
    from core.editor_exec import exec_tool

    from eval._pin_backend import pinned_backend

    previous = os.environ.get("UE_ALLOW_BLUEPRINT")
    try:
        os.environ["UE_ALLOW_BLUEPRINT"] = "false"
        with pinned_backend("official"):
            blocked = exec_tool(
                "editor_toolset.toolsets.blueprint.BlueprintTools",
                "create_node",
                {},
                task={"allow_writes": ["create_node"], "save": False, "pie": False},
            )
        if blocked.get("error") != "blueprint edit disabled":
            fail(f"official 路径的 create_node error={blocked.get('error')!r}",
                 "backend 缺省 official 时 exec_tool 先放行选择层，再走原来的蓝图开关")
    finally:
        if previous is None:
            os.environ.pop("UE_ALLOW_BLUEPRINT", None)
        else:
            os.environ["UE_ALLOW_BLUEPRINT"] = previous


def check_mcp_port_follows_backend() -> None:
    """连通检查跟本次后端走。两个端口都有响应就拒绝，不改连另一边。不探真端口。"""
    import core.editor_launch as launch

    original = launch._probe
    pinned = _use("monolith")
    try:
        seen: list[str] = []

        def only_monolith(url: str) -> bool:
            seen.append(url)
            return "9316" in url

        launch._probe = only_monolith
        if launch.mcp_up() is not True or any("8000" in url for url in seen):
            fail(f"monolith 时 mcp_up 探了 {seen!r}", "backend=monolith 时 mcp_up 只探 9316")
        launch._probe = lambda _url: True
        both = launch.ensure_editor()
        if both.get("ok") is not False or "同时在听" not in str(both.get("note")):
            fail(f"两边都在听时结果是 {both!r}", "两个端口都有响应时 ensure_editor 拒绝，并点名关掉 Unreal MCP")
        if "Unreal MCP" not in str(both.get("note")):
            fail(f"拒绝原文是 {both.get('note')!r}", "monolith 且两边都在听时，原文要求关掉 Unreal MCP")
    finally:
        launch._probe = original
        _restore(pinned)

    pinned = _use("official")
    try:
        seen = []

        def only_official(url: str) -> bool:
            seen.append(url)
            return "8000" in url

        launch._probe = only_official
        if launch.mcp_up() is not True or any("9316" in url for url in seen):
            fail(f"official 时 mcp_up 探了 {seen!r}", "backend=official 时 mcp_up 只探 8000")
        launch._probe = lambda _url: True
        both = launch.ensure_editor()
        if both.get("ok") is not False or "关掉 Monolith" not in str(both.get("note")):
            fail(f"official 且两边都在听时结果是 {both!r}", "本次后端是 official 时要求关掉 Monolith")
    finally:
        launch._probe = original
        _restore(pinned)


def check_launch_refusal_not_masked() -> None:
    """20261026-160659：cpp_build 已 exit 0，ensure_editor 因两边同时在听拒绝。
    窗口结果必须留下这句拒绝，不能改写成「图结束时没有完成」。不连端口。"""
    from agent.run_task import _unfinished_reason

    note = (
        "官方 MCP http://127.0.0.1:8000/mcp 与 Monolith http://127.0.0.1:9316/mcp 同时在听。"
        "本次后端是 monolith，请在插件里关掉 Unreal MCP（ModelContextProtocol）后重启编辑器。"
    )
    text = _unfinished_reason(
        {"ok": True, "kind": "cpp_build", "summary": "exit 0", "errors": []},
        {"final_success": True, "last_error": ""},
        editor_note=note,
        launch_ok=False,
    )
    if "同时在听" not in text or "图结束时没有完成" in text:
        fail(f"启动被拒绝后的失败句是 {text!r}", "编译已通过且 ensure_editor 拒绝时，窗口 error 用 editor 原文")
    if _unfinished_reason({"ok": True, "summary": "exit 0"}, {"last_error": ""}) != "图结束时没有完成":
        fail("没有启动拒绝时不再落到「图结束时没有完成」", "last_error 为空且没有 editor 拒绝时，仍用原来的那句")
    compile_fail = _unfinished_reason(
        {"ok": False, "summary": "error C1234", "errors": [{"message": "error C1234: x"}]},
        {},
        editor_note=note,
        launch_ok=False,
    )
    if not str(compile_fail).startswith("error C1234"):
        fail(f"编译失败句是 {compile_fail!r}", "build.ok 为 false 时仍先报编译错误")


def check_backend_plugins_are_exclusive() -> None:
    """UE_EDITOR_BACKEND 决定哪个插件听端口。monolith 关 ModelContextProtocol，official 关 Monolith。不启动编辑器。"""
    import tempfile

    from core.editor_launch import backend_plugin_args, launch_command, sync_backend_plugins
    from config import editor_backend

    monolith = backend_plugin_args("monolith")
    official = backend_plugin_args("official")
    if monolith != ["-EnablePlugins=Monolith", "-DisablePlugins=ModelContextProtocol"]:
        fail(f"monolith 启动参数是 {monolith!r}", "monolith 只启用 Monolith，并 DisablePlugins=ModelContextProtocol")
    if official != ["-EnablePlugins=ModelContextProtocol", "-DisablePlugins=Monolith"]:
        fail(f"official 启动参数是 {official!r}", "official 只启用 ModelContextProtocol，并 DisablePlugins=Monolith")
    if backend_plugin_args("both"):
        fail("非法后端仍带了插件参数", "不是 official 或 monolith 时不改插件参数")
    sample = """{
\t"Plugins": [
\t\t{
\t\t\t"Name": "ModelContextProtocol",
\t\t\t"Enabled": true
\t\t},
\t\t{
\t\t\t"Name": "Monolith",
\t\t\t"Enabled": true
\t\t}
\t]
}
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Sample.uproject"
        path.write_text(sample, encoding="utf-8", newline="")
        closed = sync_backend_plugins("monolith", path)
        text = path.read_text(encoding="utf-8")
        if not closed.get("ok") or '"Name": "ModelContextProtocol",\n\t\t\t"Enabled": false' not in text:
            fail(f"monolith 同步结果 {closed!r} 文件片段不符", "monolith 把 ModelContextProtocol.Enabled 写成 false")
        if '"Name": "Monolith",\n\t\t\t"Enabled": true' not in text:
            fail("monolith 同步后 Monolith 不是 true", "monolith 保持 Monolith.Enabled 为 true")
        opened = sync_backend_plugins("official", path)
        text = path.read_text(encoding="utf-8")
        if not opened.get("ok") or '"Name": "ModelContextProtocol",\n\t\t\t"Enabled": true' not in text:
            fail(f"official 同步结果 {opened!r}", "official 把 ModelContextProtocol.Enabled 写成 true")
        if '"Name": "Monolith",\n\t\t\t"Enabled": false' not in text:
            fail("official 同步后 Monolith 仍是 true", "official 把 Monolith.Enabled 写成 false")
        again = sync_backend_plugins("official", path)
        if path.read_text(encoding="utf-8") != text or not again.get("ok"):
            fail("第二次同步改写了已经对齐的文件", "开关已经符合后端时不要再写 .uproject")
    command = launch_command()
    if command[-1] != "-AutoDeclinePackageRecovery":
        fail(f"启动命令结尾是 {command[-1]!r}", "插件参数放在 -AutoDeclinePackageRecovery 前面")
    for arg in backend_plugin_args(editor_backend()):
        if arg not in command:
            fail(f"启动命令 {command!r} 缺少 {arg}", "launch_command 带上当前后端的互斥插件参数")


def check_monolith_automation_skips_official() -> None:
    """monolith 下 Automation 走 editor.run_automation_tests，不调用官方 DiscoverTests。"""
    import core.editor_exec as editor_exec
    import core.judge as judge
    from backends import monolith

    calls: list = []

    def fake_exec(namespace, action, params=None, task=None, transport=None):
        calls.append((namespace, action, dict(params or {})))
        return {"ok": True, "data": {
            "success": True,
            "total": 1,
            "passed": 1,
            "failed": 0,
            "results": [{"full_path": "AIAgent.Fps.Hit", "status": "passed"}],
        }}

    def boom(*_args, **_kwargs):
        raise AssertionError("monolith 验收不许走官方 DiscoverTests")

    original_exec = monolith.exec
    original_tool = editor_exec.exec_tool
    original_mode = judge.UE_EXEC_MODE
    original_discovered = judge._discovered
    pinned = _use("monolith")
    monolith.exec = fake_exec
    editor_exec.exec_tool = boom
    judge.UE_EXEC_MODE = "real"
    judge._discovered = False
    try:
        verdict = judge.judge(
            {"kind": "automation", "filter": "AIAgent.Fps.Hit"},
            {"allow_writes": [], "save": False, "pie": False},
        )
        if not verdict.get("ok"):
            fail(f"通过的自动化被判失败：{verdict!r}",
                 "monolith 的 success=true、failed=0、total>0 应判通过")
        if calls != [("editor", "run_automation_tests", {"prefix": "AIAgent.Fps.Hit"})]:
            fail(f"调用是 {calls!r}",
                 "filter 作为 editor.run_automation_tests 的 prefix，不调用 DiscoverTests")
        if verdict.get("tests") != ["AIAgent.Fps.Hit"] or "DiscoverTests" in str(verdict.get("summary")):
            fail(f"裁决是 {verdict!r}", "摘要写测试名，不写 DiscoverTests 失败")

        def fail_exec(namespace, action, params=None, task=None, transport=None):
            return {"ok": False, "error": "编辑器未连通"}

        monolith.exec = fail_exec
        denied = judge.judge({"kind": "automation", "filter": "AIAgent.Fps.Hit"})
        if denied.get("ok") or "编辑器未连通" not in str(denied.get("summary")):
            fail(f"连接失败的摘要是 {denied!r}",
                 "run_automation_tests 的 error 原文写入 summary，不要只写 DiscoverTests 失败")

        def empty_exec(namespace, action, params=None, task=None, transport=None):
            return {"ok": True, "data": {
                "success": False, "total": 0, "passed": 0, "failed": 0,
                "message": "No tests matching prefix 'AIAgent.Fps.Hit'",
            }}

        monolith.exec = empty_exec
        empty = judge.judge({"kind": "automation", "filter": "AIAgent.Fps.Hit"})
        if empty.get("ok") or "No tests matching prefix" not in str(empty.get("summary")):
            fail(f"零匹配的摘要是 {empty!r}", "success=false 时保留 monolith 的 message")

        def broken_exec(namespace, action, params=None, task=None, transport=None):
            return {"ok": True, "data": {
                "success": False, "total": 1, "passed": 0, "failed": 1,
                "results": [{
                    "full_path": "AIAgent.Fps.Hit",
                    "status": "failed",
                    "errors": ["命中数"],
                }],
            }}

        monolith.exec = broken_exec
        broken = judge.judge({"kind": "automation", "filter": "AIAgent.Fps.Hit"})
        text = str(broken.get("summary") or "")
        if broken.get("ok") or "failed=1" not in text or "命中数" not in text:
            fail(f"失败用例的摘要是 {broken!r}", "failed>0 时摘要带失败数和测试错误原文")
    finally:
        monolith.exec = original_exec
        editor_exec.exec_tool = original_tool
        judge.UE_EXEC_MODE = original_mode
        judge._discovered = original_discovered
        _restore(pinned)


def main() -> int:
    check_defaults()
    check_refusals()
    check_lock_and_event()
    check_script_refusal()
    check_write_policy()
    check_monolith_automation_skips_official()
    check_mcp_port_follows_backend()
    check_launch_refusal_not_masked()
    check_backend_plugins_are_exclusive()
    check_live_official_shape()
    print("backend ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
