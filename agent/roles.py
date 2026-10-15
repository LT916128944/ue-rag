# agent/roles.py
"""同一张图上的角色。不新增边，只限制这一轮能看见的工具。"""

CPP_TOOLS = {"read_source", "apply_unified_diff"}
EDITOR_READS = {"find_actors", "get_actor_transform"}


def task_kinds(task: dict | None) -> set[str]:
    kinds = set()
    for item in (task or {}).get("verify") or []:
        if isinstance(item, dict) and item.get("kind"):
            kinds.add(item["kind"])
    return kinds


def plan_role(task: dict | None) -> str:
    """loop 是单 Agent。有裁判种类时才拆成 cpp / editor。"""
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
