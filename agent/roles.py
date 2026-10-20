# agent/roles.py
"""同一张图上的角色。不新增边，只限制这一轮能看见的工具。

任务 JSON 可带 harness_role：research / plan / review / execute。
没写时行为与原来完全一致（cpp / editor / mixed / loop）。
L1 理解器仍从全动作目录选动作，research / review 的写动作在执行时被拒（可能多一次模型调用）。
"""

CPP_TOOLS = {"read_source", "apply_unified_diff"}
EDITOR_READS = {"find_actors", "get_actor_transform"}

HARNESS_ROLES = ("research", "plan", "review", "execute")

# research / review 只看得见只读动作：源码、检索、只读裁决、只读测试与查找。
# 写前缀、保存、PIE、改图一律不在其中，allow_writes 写了也无效（收紧）。
RESEARCH_TOOLS = {
    "read_source",
    "search_ue_project",
    "query_actor",
    "judge_pickup",
    "check_pickup",
    "run_automation_test",
    "find_actors",
    "get_actor_transform",
}


def harness_role(task: dict | None) -> str:
    role = str((task or {}).get("harness_role") or "").strip()
    return role if role in HARNESS_ROLES else ""


def task_kinds(task: dict | None) -> set[str]:
    kinds = set()
    for item in (task or {}).get("verify") or []:
        if isinstance(item, dict) and item.get("kind"):
            kinds.add(item["kind"])
    return kinds


def plan_role(task: dict | None) -> str:
    """loop 是单 Agent。有裁判种类时才拆成 cpp / editor。harness_role 命中时优先。"""
    hrole = harness_role(task)
    if hrole in ("research", "plan", "review"):
        return hrole
    if not task or not (task.get("verify") or []):
        return "loop"
    kinds = task_kinds(task)
    has_cpp = "cpp_build" in kinds
    has_editor = bool(kinds & {"editor_assert", "blueprint_compile"})
    if has_cpp and has_editor:
        return "mixed"
    if has_cpp:
        return "cpp"
    if has_editor:
        return "editor"
    return "loop"


def trace_role(node: str, task: dict | None) -> str:
    if node == "supervise":
        return "supervisor"
    if node in ("build", "test", "precheck"):
        return "judge"
    if node == "retrieve":
        return "retrieve"
    return plan_role(task)


def tool_allowed(short_name: str, task: dict | None) -> bool:
    hrole = harness_role(task)
    if hrole in ("research", "review"):
        # 只读白名单。写工具一律拒绝，即使 allow_writes 里写了它们。
        return short_name in RESEARCH_TOOLS
    if hrole == "plan":
        # 角色只返回窗口清单，不执行任何工具。
        return False
    role = plan_role(task)
    if role == "loop":
        return True
    if short_name in CPP_TOOLS:
        return role in ("cpp", "mixed")
    allow = set((task or {}).get("allow_writes") or [])
    if short_name in allow or short_name in EDITOR_READS:
        return role in ("editor", "mixed")
    if role == "cpp":
        return False
    if role == "editor":
        return False
    return False
