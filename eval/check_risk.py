# eval/check_risk.py
"""风险矩阵与熔断的离线检查。不连编辑器：裁决走 risk.decide，拒绝走 exec_tool 的既有分支。
末尾带 context-switch-drift 只读断言。阈值只在 core/fault.py，这里不另写一套。"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SEE = str(ROOT / "data" / "harness" / "risk_matrix.json")

from core import fault, risk  # noqa: E402
from core.editor_exec import exec_tool  # noqa: E402

ASSET_TOOLSET = "editor_toolset.toolsets.asset.AssetTools"
BLUEPRINT_TOOLSET = "editor_toolset.toolsets.blueprint.BlueprintTools"


def fail(msg: str, fix: str) -> None:
    print(f"❌ {msg}")
    print(f"✅ FIX: {fix}")
    print(f"📖 See: {SEE}")
    sys.exit(1)


def check_write_refusals() -> None:
    """任务显式写 save/pie=false 时 save_assets / StartPIE 被拒，error 精确等于 write refused。"""
    from eval._pin_backend import pinned_backend

    with pinned_backend("official"):
        for tool in ("save_assets", "StartPIE"):
            refused = exec_tool(ASSET_TOOLSET, tool, {}, task={"save": False, "pie": False, "allow_writes": []})
            if refused.get("error") != "write refused":
                fail(f"任务显式关掉 save/pie 时 {tool} 的 error={refused.get('error')!r}，应为 'write refused'",
                     f"core/editor_exec.py 里 {tool} 的拒绝分支改回 error='write refused'，policy 键不动")


def check_default_policy() -> None:
    """全闭环缺省：任务没写 save/pie 时按 UE_DEFAULT_SAVE / UE_DEFAULT_PIE 放行。
    只读配置，不调编辑器。"""
    from config import default_pie, default_save, task_flag, task_policy

    if not (default_save() and default_pie()):
        fail(f"缺省 save={default_save()} pie={default_pie()}，全闭环要求两个都开",
             "config.py 的 default_save/default_pie 缺省改回 True；要关在 .env 里写 UE_DEFAULT_SAVE=false")
    policy = task_policy()
    if policy != {"save": True, "pie": True}:
        fail(f"task_policy() 是 {policy}，应为 save/pie 都为 True",
             "config.py 的 task_policy 缺省跟随 default_save/default_pie")
    if not (task_flag({}, "save") and task_flag({}, "pie")):
        fail("空任务下 task_flag 没有放行 save/pie",
             "config.py 的 task_flag 在任务没写键时取缺省，不取 False")


def check_read_allowed() -> None:
    """query_actor 不在 allow_writes 时仍允许（未登记不当 high 拒掉）。"""
    verdict = risk.decide("query_actor", {"save": False, "pie": False, "allow_writes": []})
    if verdict["allow"] is not True or verdict["tier"] != "low":
        fail(f"query_actor 的裁决是 {verdict}",
             "core/risk.py 的 tier/decide 对矩阵 low 名字放行；未登记名字不得当成 high 拒绝")


def check_blueprint_switch() -> None:
    """UE_ALLOW_BLUEPRINT=false 时 create_node 被拒。测完恢复环境变量。"""
    from eval._pin_backend import pinned_backend

    previous = os.environ.get("UE_ALLOW_BLUEPRINT")
    try:
        os.environ["UE_ALLOW_BLUEPRINT"] = "false"
        with pinned_backend("official"):
            blocked = exec_tool(
                BLUEPRINT_TOOLSET,
                "create_node",
                {},
                task={"allow_writes": ["create_node"], "save": False, "pie": False},
            )
        if blocked.get("error") not in ("blueprint edit disabled", "write refused"):
            fail(f"UE_ALLOW_BLUEPRINT=false 时 create_node 的 error={blocked.get('error')!r}",
                 "core/editor_exec.py 里开关为 false 的改蓝图文案保持 'blueprint edit disabled'，decide 不再重复拒绝")
    finally:
        if previous is None:
            os.environ.pop("UE_ALLOW_BLUEPRINT", None)
        else:
            os.environ["UE_ALLOW_BLUEPRINT"] = previous


def check_high_tiers() -> None:
    """矩阵里每个 high 名字都被 tier() 认成 high，graph_write 展开也认。"""
    matrix = json.loads((ROOT / "data" / "harness" / "risk_matrix.json").read_text(encoding="utf-8"))
    for name in matrix["high"]:
        if risk.tier(name) != "high":
            fail(f"矩阵 high 名字 {name} 被 tier() 认成 {risk.tier(name)!r}",
                 f"data/harness/risk_matrix.json 的 high 名单与 core/risk.py 的 tier() 对齐")
    for name in risk.GRAPH_WRITE_TOOLS:
        if risk.tier(name) != "high":
            fail(f"graph_write 展开的名字 {name} 被 tier() 认成 {risk.tier(name)!r}",
                 "core/risk.py 的 GRAPH_WRITE_TOOLS 与矩阵 high 的 graph_write 展开对齐")


def check_circuit() -> None:
    """连续同短名失败达到阈值后 decide 拒绝且 reason 含 circuit；exec_tool 报 circuit open。"""
    previous = os.environ.get("UE_CIRCUIT_PATH")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["UE_CIRCUIT_PATH"] = str(Path(tmp) / "circuit.json")
            tool = "save_assets"
            count = 0
            while count < fault.CIRCUIT_THRESHOLD:
                count = fault.record_failure(tool)
            verdict = risk.decide(tool, {})
            if verdict["allow"] is not False or "circuit" not in str(verdict["reason"]):
                fail(f"第 {fault.CIRCUIT_THRESHOLD} 次失败后 decide 是 {verdict}",
                     "core/fault.py 的 record_failure 达到 CIRCUIT_THRESHOLD 后写 circuit.json；core/risk.py 的 decide 读它并带 circuit 拒绝")
            from eval._pin_backend import pinned_backend
            with pinned_backend("official"):
                refused = exec_tool(ASSET_TOOLSET, tool, {}, task={"save": True, "pie": False, "allow_writes": ["save_assets"]})
            if refused.get("error") != "circuit open":
                fail(f"熔断打开时 exec_tool 的 error={refused.get('error')!r}，应为 'circuit open'",
                     "core/editor_exec.py 的追加拒绝保持 error='circuit open'，policy 键不动")
            for key in ("save", "pie", "allow_writes", "UE_ALLOW_BLUEPRINT", "UE_ALLOW_MATERIAL", "UE_ALLOW_UMG", "UE_ALLOW_ANIM", "UE_ALLOW_NIAGARA"):
                if key not in (refused.get("policy") or {}):
                    fail(f"熔断拒绝的 policy 缺 {key}", "core/editor_exec.py 的熔断拒绝走 _refused，policy 键不动")
    finally:
        if previous is None:
            os.environ.pop("UE_CIRCUIT_PATH", None)
        else:
            os.environ["UE_CIRCUIT_PATH"] = previous


def check_policy_profiles() -> None:
    """同一份 task：menu 剖面与 task_policy 一致；dsh 在空目录全拒绝，且不读 task 里的 allow_writes。
    dsh_bridge 不再自己拼 allow_writes / save / pie 那份缺省字典。"""
    import ast

    from config import run_policy, task_policy

    sample = {"allow_writes": ["set_actor_transform"], "save": False, "note": "keep"}
    menu = run_policy("menu", sample)
    if menu["save"] != task_policy(save=False)["save"] or menu["pie"] != task_policy()["pie"]:
        fail(f"menu 剖面 {menu} 与 task_policy 不一致",
             "config.run_policy('menu') 的 save/pie 走 task_policy 同一套缺省")
    if menu["allow_writes"] != ["set_actor_transform"] or set(menu) != {"allow_writes", "save", "pie"}:
        fail(f"menu 剖面键或 allow_writes 不对：{menu}",
             "run_policy 只返回 allow_writes/save/pie，allow_writes 用 task 里已有的那份")
    bare = run_policy("menu", {})
    if (bare["save"], bare["pie"]) != (task_policy()["save"], task_policy()["pie"]):
        fail(f"空任务 menu 剖面是 {bare}，task_policy() 是 {task_policy()}",
             "没写 save/pie 时 menu 剖面跟随 default_save/default_pie")

    saved_data = os.environ.get("UE_DSH_DATA_DIR")
    saved_task = os.environ.get("UE_DSH_TASK_PATH")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["UE_DSH_DATA_DIR"] = tmp
            os.environ.pop("UE_DSH_TASK_PATH", None)
            denied = run_policy("dsh", {"allow_writes": ["set_actor_transform"], "save": True, "pie": True})
    finally:
        if saved_data is None:
            os.environ.pop("UE_DSH_DATA_DIR", None)
        else:
            os.environ["UE_DSH_DATA_DIR"] = saved_data
        if saved_task is None:
            os.environ.pop("UE_DSH_TASK_PATH", None)
        else:
            os.environ["UE_DSH_TASK_PATH"] = saved_task
    if denied != {"allow_writes": [], "save": False, "pie": False}:
        fail(f"空目录 dsh 剖面是 {denied}，应为全拒绝",
             "config.run_policy('dsh') 缺 session_task.json 时返回 allow_writes=[] save=false pie=false，不读 task 参数")

    tree = ast.parse((ROOT / "server" / "dsh_bridge.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [
            key.value for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
        if {"allow_writes", "save", "pie"} <= set(keys):
            fail("server/dsh_bridge.py 仍自己拼 allow_writes/save/pie",
                 "dsh_bridge._policy_task 改走 config.run_policy('dsh')，不要在网关里写那三份缺省")


def check_solver_statements() -> None:
    """求解开关三处表述。判断复用 check_solver_budget，不另写一套，也不改文档。"""
    import config
    from eval.check_solver_budget import check_enabled_defaults_off

    saved = config.ENV_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / ".env"
            empty.write_text("", encoding="utf-8")
            config.ENV_PATH = empty
            try:
                check_enabled_defaults_off()
            except AssertionError as exc:
                fail(f"环境变量未设置时求解开关不是关：{exc}",
                     "agent/solver.py 的 solver_enabled 在环境变量未设置时返回关")
    finally:
        config.ENV_PATH = saved

    project = ROOT.parent.parent / "AIAgentProject"
    workflow = project / "WORKFLOW.md"
    text = workflow.read_text(encoding="utf-8")
    start = text.find("## 当前系统")
    end = text.find("\n## ", start + 3) if start >= 0 else -1
    section = text[start:end] if start >= 0 and end >= 0 else ""
    if "缺省 false" not in section:
        print(f"❌ {workflow}")
        print("期望句：缺省 false")
        print("「当前系统」那一节应含这句")
        sys.exit(1)

    readme = project / "README.md"
    readme_text = readme.read_text(encoding="utf-8")
    if "本机已开" not in readme_text:
        return
    env_path = ROOT / ".env"
    if not env_path.is_file():
        print(f"❌ {readme}")
        print(f"期望：{env_path} 不存在时，README 不得写「本机已开」")
        sys.exit(1)
    enabled = ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() == "UE_SOLVER_ENABLED":
            enabled = value.strip().strip('"').strip("'").lower()
    if enabled not in ("1", "true", "yes", "on"):
        print(f"❌ {readme}")
        print(f"期望句：{env_path} 里 UE_SOLVER_ENABLED=true")
        print(f"实际：{enabled!r}")
        sys.exit(1)


def check_context_switch_drift() -> None:
    """context-switch-drift：project_context.json 的五个开关都要与 config 的实时读数一致。
    只比 UE_ALLOW_BLUEPRINT 一条时，其它四个开关漂了也查不出来（20261021 本机五个全开、
    JSON 里四个还是 false，check_routes 因此挂过一次）。"""
    import config

    context_path = ROOT / "data" / "project_context.json"
    recorded_switches = json.loads(context_path.read_text(encoding="utf-8")).get("switches") or {}
    live_readers = {
        "UE_ALLOW_BLUEPRINT": config.allow_blueprint,
        "UE_ALLOW_MATERIAL": config.allow_material,
        "UE_ALLOW_UMG": config.allow_umg,
        "UE_ALLOW_ANIM": config.allow_anim,
        "UE_ALLOW_NIAGARA": config.allow_niagara,
    }
    drifted = [
        f"{name}: JSON={bool(recorded_switches.get(name))} config={bool(reader())}"
        for name, reader in live_readers.items()
        if bool(recorded_switches.get(name)) != bool(reader())
    ]
    if drifted:
        print("❌ project_context.json 与 config 的开关漂移：" + "；".join(drifted))
        print("✅ FIX: 重跑 python pipelines/export_project_context.py 重新导出；不要手改 JSON，也不要改 .env")
        print(f"📖 See: {context_path}")
        sys.exit(1)


def check_cost_fields_are_not_secrets() -> None:
    """抽样 data/costs.jsonl 的字段名。形如密钥就失败。不读 .env，不扫磁盘。

    .gitignore 已忽略 .env，所以这条只看成本行，不把密钥文件当成样本。
    """
    import re

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    if not any(line.strip() == ".env" for line in gitignore.splitlines()):
        fail(".gitignore 没有单独忽略 .env", "在 RAG/ue-rag/.gitignore 写一行 .env。成本抽样不读 .env")
    from core.llm_call import _cost_line

    line = _cost_line({"caller": "check", "api_key": "drop-me", "prompt_tokens": 1})
    if "api_key" in line or "drop-me" in json.dumps(line, ensure_ascii=False):
        fail("append_cost 仍会写下 api_key", "core/llm_call.py 的 _cost_line 丢掉密钥字段")
    if line.get("prompt_tokens") != 1 or "ts" not in line:
        fail("成本行的用量或 ts 被误删", "密钥规则只匹配字段名，prompt_tokens 要留下")
    path = ROOT / "data" / "costs.jsonl"
    if not path.is_file():
        return
    bad: list[str] = []
    scanned = 0
    pattern = re.compile(
        r"(?i)(api[_-]?key|secret|password|authorization|credential|access[_-]?token)"
    )
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if scanned >= 500:
                break
            scanned += 1
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            for key in row:
                if pattern.search(str(key)):
                    bad.append(str(key))
    if bad:
        fail(
            "costs.jsonl 抽样出现密钥字段: " + ", ".join(sorted(set(bad))),
            "core/llm_call.py 的 append_cost 丢掉这些字段后再写",
        )


def _doc_project() -> Path:
    """AIAgentProject 目录。文档只读比对，这里不改文档。"""
    return ROOT.parent.parent / "AIAgentProject"


def _env_example_keys(path: Path) -> dict[str, list[tuple[int, str, bool]]]:
    """按 KEY 收 .env.example 的行：值、行号、是否被注释。同名字段全收，判真值时只看未注释那条。"""
    found: dict[str, list[tuple[int, str, bool]]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        commented = text.startswith("#")
        body = text.lstrip("#").strip() if commented else text
        if "=" not in body:
            continue
        key, value = body.split("=", 1)
        key = key.strip()
        if not key:
            continue
        found.setdefault(key, []).append((number, value.strip().strip('"').strip("'"), commented))
    return found


def _squash(line: str) -> str:
    """句式判据只看字面，不吃行内的反引号与空白。"""
    return line.replace("`", "").replace(" ", "")


def _check_solver_switch_in_env_example() -> tuple[list[str], Path]:
    """求解开关只认真值。返回（文档里的同句行，.env.example 路径）。"""
    example = ROOT / ".env.example"
    if not example.is_file():
        fail(f"缺 {example}", "RAG/ue-rag/.env.example 要进 git，求解开关那一行写在里面")
    hits = []
    for doc in (_doc_project() / "README.md", _doc_project() / "WORKFLOW.md"):
        text = doc.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            flat = _squash(line)
            if ".env.example" in flat and "UE_SOLVER_ENABLED" in flat:
                hits.append(f"{doc.name}:{number}「{line.strip()}」")
    rows = _env_example_keys(example).get("UE_SOLVER_ENABLED") or []
    live = [row for row in rows if not row[2] and row[1].lower() in ("1", "true", "yes", "on")]
    if hits and not live:
        listed = [
            f"第 {number} 行 {value!r}{'（注释）' if commented else ''}"
            for number, value, commented in rows
        ]
        actual = "；".join(listed) or "没有这一行"
        fail(
            "文档说 .env.example 里求解开关是 true，实际不是未注释的 UE_SOLVER_ENABLED=true："
            + "，".join(hits) + f" → 实际 {actual}",
            "把 RAG/ue-rag/.env.example 的 UE_SOLVER_ENABLED 改成未注释的 true（只改这一行，不碰 .env），"
            "或把两份文档那句改成「本机 .env 为 true」。不许只改文档不改这条断言",
        )
    return hits, example


def check_solver_switch_in_env_example() -> None:
    """P5-1 求解开关只认真值：README/WORKFLOW 里 `.env.example` 与 `UE_SOLVER_ENABLED` 同句时，
    .env.example 必须是未注释的 UE_SOLVER_ENABLED=true。写成注释行（`# UE_SOLVER_ENABLED=false`）就红。

    为什么：2026-11-11 之前 WORKFLOW.md 与 README.md 都写「.env.example 是 true / 这台机器的值」，
    而 .env.example 实际是注释的 false——照文档换机的人拿到的求解扇是关的，两份文档互相印证不出错。
    """
    _check_solver_switch_in_env_example()


def _setup_script_modes(script: Path) -> tuple[bool, bool]:
    """setup.ps1 是打印还是自己跑：(出现 Write-Output, 真的自己跑)。
    真的自己跑只认行首 `&` / `Start-Process` / `Start-Job` / `Invoke-Expression`；
    注释行不算，Here-String 里的文本行首不可能是这几个命令。"""
    lines = [
        text.strip() for text in script.read_text(encoding="utf-8").splitlines()
    ]
    lines = [text for text in lines if text and not text.startswith("#")]
    runs = any(text.split(None, 1)[0] in ("&", "Start-Process", "Start-Job", "Invoke-Expression", "iex") for text in lines)
    prints = any("Write-Output" in text for text in lines)
    return prints, runs


def check_setup_wording() -> None:
    """P5-2 setup.ps1 措辞：WORKFLOW.md 描述 setup.ps1 的句子含「自己跑」时必须同时含「打印」；
    shortcuts/setup.ps1 实际只是 Write-Output 打印时，句子说「自己跑」就红。

    为什么：setup.ps1 末尾三条是 Write-Output「下一步」，check_all / ingest.bat / 起入口服务都要人自己跑。
    文档写成脚本自己跑，换机的人会以为检查已经跑过。
    """
    script = ROOT / "shortcuts" / "setup.ps1"
    if not script.is_file():
        fail(f"缺 {script}", "RAG/ue-rag/shortcuts/setup.ps1 要留在盘上，这条断言要读它的末尾写法")
    prints, runs = _setup_script_modes(script)
    workflow = _doc_project() / "WORKFLOW.md"
    text = workflow.read_text(encoding="utf-8")
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        if "setup.ps1" not in line:
            continue
        # 中文没有空格，句子按行切：一行里说了 setup.ps1 就整行算这一句。
        hits.append((number, line))
    if not hits:
        return
    for number, line in hits:
        if "自己跑" in line and "打印" not in line:
            fail(
                f"WORKFLOW.md:{number} 描述 setup.ps1 说「自己跑」却没说「打印」：「{line.strip()}」",
                f"WORKFLOW.md:{number} 改成「脚本只打印下一步清单，不自己跑」（shortcuts/setup.ps1:78-81 是 Write-Output）",
            )
        if "自己跑" in line and prints and not runs:
            fail(
                f"WORKFLOW.md:{number} 说 setup.ps1「自己跑」，而 shortcuts/setup.ps1 只有 Write-Output 打印、没有 & / Start-Process：「{line.strip()}」",
                f"WORKFLOW.md:{number} 把「自己跑」改成「打印并列出」，脚本末尾仍是 Write-Output 三条「下一步」",
            )


def _machine_table_rows(workflow_text: str) -> list[str]:
    """「## 换机器」那一节表格里的每一行（含表头与分隔行，判据只看 `UE_ALLOW_*` 在前两格）。"""
    start = workflow_text.find("\n## 换机器")
    if start < 0:
        start = 0 if workflow_text.startswith("## 换机器") else -1
    if start < 0:
        fail("WORKFLOW.md 没有「## 换机器」这一节",
             "AIAgentProject/WORKFLOW.md 保留「## 换机器」标题与开关表；这条断言按那一节的表格判")
    end = workflow_text.find("\n## ", start + 3)
    section = workflow_text[start:end] if end >= 0 else workflow_text[start:]
    return [line for line in section.splitlines() if line.strip().startswith("|")]


def _table_key(row: str) -> str:
    cells = row.split("|")
    return cells[1].replace("`", "").strip() if len(cells) > 1 else ""


def check_env_switches_in_table() -> None:
    """P5-3 UE_ALLOW_* 全进开关表：.env.example 里出现的每一个 UE_ALLOW_* 键，
    都必须在 WORKFLOW.md「## 换机器」表格里有一行（行首第一格就是该键）。

    为什么：2026-11-11 之前表里缺 UE_ALLOW_PYTHON，而 .env.example 第 40 行未注释写着 true。
    照表换机的人不知道有个放行 editor.run_python 的开关，也不知道代码缺省是 false。
    """
    example = ROOT / ".env.example"
    keys = sorted(key for key in _env_example_keys(example) if key.startswith("UE_ALLOW_"))
    if not keys:
        fail(f"{example} 里没有 UE_ALLOW_* 键", "开关表按 .env.example 的 UE_ALLOW_* 判；这份文件要列这些键")
    table = {_table_key(row) for row in _machine_table_rows((_doc_project() / "WORKFLOW.md").read_text(encoding="utf-8"))}
    missing = [key for key in keys if key not in table]
    if missing:
        fail(
            "WORKFLOW.md「## 换机器」表缺这些 UE_ALLOW_*："
            + "、".join(missing)
            + "（.env.example 里出现了它们，表里没有）",
            "在 AIAgentProject/WORKFLOW.md 的「## 换机器」表格里为每个键加一行，"
            "第一格写 `UE_ALLOW_XXX`，第二格写代码缺省与放行条件",
        )


def _source_literal(node: object) -> str:
    """return {..., "source": "xxx", ...} 里 source 的字面值；不是这种形状返回空串。"""
    import ast

    if not isinstance(node, ast.Dict):
        return ""
    for key, item in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == "source" and isinstance(item, ast.Constant):
            return str(item.value)
    return ""


def _branch_guards(func: object) -> dict[str, tuple[bool, int]]:
    """一条函数里每个 `return {"source": ...}` 分支：{source 字面值: (return 之前有没有闸门, 行号)}。

    按函数体顺序走，扫到这个 return 时把「前面已经出现过的语句」拼起来找闸门——闸门写在
    if/elif 条件里、return 在块尾，所以看的是它之前那几行，不是 return 自己。
    rules 经 `_rules_cover` 间接过闸门，所以 rules 分支额外认 `_rules_cover`。
    """
    import ast

    guarded: dict[str, tuple[bool, int]] = {}

    def gate_text(literal: str, before: str) -> bool:
        if "_clauses_accounted" in before:
            return True
        return literal == "rules" and "_rules_cover" in before

    def walk(statements: list, before: list[str]) -> None:
        seen = list(before)
        for node in statements:
            literal = _source_literal(getattr(node, "value", None)) if isinstance(node, ast.Return) else ""
            if literal and literal not in guarded:
                guarded[literal] = (gate_text(literal, "\n".join(seen + [ast.unparse(node).strip()])), node.lineno)
            # 块里的 return 也带上门条件：闸门常常写在 if / elif 的条件里。
            inner_before = seen + ([ast.unparse(node.test).strip()] if isinstance(node, ast.If) else [])
            for field in ("body", "orelse"):
                inner = getattr(node, field, None)
                if isinstance(inner, list):
                    walk(inner, inner_before)
            seen.append(ast.unparse(node).strip())

    walk(list(getattr(func, "body", [])), [])
    return guarded


def check_gate_branches_documented() -> None:
    """P5-4 结构级：plan_windows 里 `rules` / `hud-rules` / `learned` 三条 L0 分支，
    没过 `_clauses_accounted` 闸门的那一条，WORKFLOW.md 里必须有对应例外说明。

    rules 走 `_rules_cover`（里面调 `_clauses_accounted`），learned 直接调；hud-rules 的
    `_gate_scene` 返回后直接 `return {"source": "hud-rules"}`，闸门在 agent/fps_hud.py 的
    `hud_windows` 里，分支本身看不见。三条一视同仁读会读成「hud-rules 也自己过一次闸门」。
    """
    import ast

    source = (ROOT / "agent" / "plan_windows.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    branch: dict[str, tuple[bool, int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for literal, guard in _branch_guards(node).items():
            if literal in ("rules", "hud-rules", "learned") and literal not in branch:
                branch[literal] = guard
        if all(name in branch for name in ("rules", "hud-rules", "learned")):
            break
    missing = [name for name in ("rules", "hud-rules", "learned") if name not in branch]
    if missing:
        fail(
            "agent/plan_windows.py 里找不到这些 L0 分支的 return：" + "、".join(missing),
            "三条分支的 return 字典里 source 仍是 'rules' / 'hud-rules' / 'learned'；改了名字就同步这条断言与 WORKFLOW.md",
        )
    workflow_text = (_doc_project() / "WORKFLOW.md").read_text(encoding="utf-8")
    ungated = [name for name in ("rules", "hud-rules", "learned") if not branch[name][0]]
    naked = [
        f"{name}（agent/plan_windows.py:{branch[name][1]}）"
        for name in ungated
        if not any(
            f"{name} 靠自身整句匹配" in line or f"{name} 的闸门" in line or f"{name} 例外" in line
            for line in workflow_text.splitlines()
        )
    ]
    if naked:
        fail(
            "这些 L0 分支的 return 前面没有 _clauses_accounted 闸门，WORKFLOW.md 也没有例外说明："
            + "、".join(naked)
            + "；WORKFLOW.md 却写「rules / hud-rules / learned 命中前先过 _clauses_accounted」",
            "在 AIAgentProject/WORKFLOW.md 的「门只认整句」那一节补一句例外，例如"
            "「hud-rules 靠自身整句匹配：闸门在 agent/fps_hud.py 的 hud_windows 里」，不要改 hud-rules 的直接 return",
        )


def main() -> int:
    check_write_refusals()
    check_default_policy()
    check_read_allowed()
    check_blueprint_switch()
    check_high_tiers()
    check_circuit()
    check_policy_profiles()
    check_solver_statements()
    check_context_switch_drift()
    check_cost_fields_are_not_secrets()
    # 20261111 P5：WORKFLOW.md 三处漂移 + 一条结构级断言。只读文档与 .env.example，不改它们。
    check_solver_switch_in_env_example()
    check_setup_wording()
    check_env_switches_in_table()
    check_gate_branches_documented()
    print("risk ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
