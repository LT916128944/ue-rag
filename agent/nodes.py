# agent/nodes.py
import os
import sys
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import SKILL_CARDS_PATH
from agent.skill_prompt import load_agent_skill_prompt, project_rules_suffix, skill_card_line
from dotenv import load_dotenv
load_dotenv()

from core.tool_router import build_agent_prompt, retrieve_tools, get_local_tools, tool_schema

from agent.state import AgentState

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": (
            "任务完成、失败或明确无法继续时，必须调用此工具来结束 Agent 循环。"
            "调用后不再有后续节点。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean", "description": "任务是否成功完成"},
                "summary": {"type": "string", "description": "一句话总结最终结果（不超过 100 字）"},
            },
            "required": ["success", "summary"],
        },
    },
}

_BUILD_KINDS = {"cpp_build", "blueprint_compile"}
_TEST_KINDS = {"editor_assert", "automation"}
_skill_vectors = {"mtime": None, "matrix": None, "cards": None}




def _ok_from_payload(text: str) -> bool:
    try:
        data = json.loads(text)
    except Exception:
        return False
    if isinstance(data, dict) and "ok" in data:
        return bool(data["ok"])
    if isinstance(data, dict) and data.get("error"):
        return False
    return True


def _error_from_payload(text: str) -> str:
    """工具失败原文，截短到 200 字。给 trace、事件流和打转熔断用。"""
    try:
        data = json.loads(text)
    except Exception:
        return ""
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])[:200]
    return ""


def execute_ue_tool(tool_name: str, arguments: str, task: dict | None = None) -> str:
    args = json.loads(arguments) if arguments else {}
    task = task or {}

    from core.tool_router import get_full_name, split_full_name, LOCAL_TOOL_NAMES

    full_name = get_full_name(tool_name) or tool_name
    toolset_name, short_name = split_full_name(full_name)

    if short_name in LOCAL_TOOL_NAMES:
        print(f"\n  >> [Local] {short_name}")
        if short_name == "search_ue_project":
            from core.rag_core import retrieve as rag_retrieve
            where = args.get("where")
            if isinstance(where, str):
                try:
                    where = json.loads(where)
                except json.JSONDecodeError:
                    where = None
            result = rag_retrieve(query=args.get("query", ""), top_k=int(args.get("top_k", 5)), where=where)
            return json.dumps(result, ensure_ascii=False)
        if short_name == "get_project_summary":
            from core.rag_core import _get_collection
            col = _get_collection()
            return json.dumps({"collection": "ue_assets", "total": col.count()}, ensure_ascii=False)
        if short_name == "read_source":
            from core.source_edit import read_source
            result = read_source(args.get("path", ""), int(args.get("start_line", 1)), int(args.get("end_line", 1)))
            return json.dumps(result, ensure_ascii=False)
        if short_name == "apply_unified_diff":
            if not (task.get("verify") or []):
                from agent.harness import policy_snapshot

                return json.dumps({
                    "ok": False,
                    "error": "write refused。改 C++ 用 start.bat 菜单 3，确认后会编译；源码留下后启动编辑器。",
                    "policy": policy_snapshot(task),
                }, ensure_ascii=False)
            from core.source_edit import apply_unified_diff
            result = apply_unified_diff(args.get("path", ""), args.get("diff", ""))
            return json.dumps(result, ensure_ascii=False)
        return json.dumps({"ok": False, "error": f"Unknown local tool: {short_name}"}, ensure_ascii=False)

    from backends.select import current_backend

    if current_backend() == "monolith":
        namespace = ""
        action = ""
        if "__" in tool_name:
            namespace, action = tool_name.split("__", 1)
        elif toolset_name and "." not in toolset_name:
            namespace, action = toolset_name, short_name
        elif "." not in tool_name:
            namespace = str(args.pop("namespace", "") or "mesh")
            action = tool_name
        if namespace and action:
            from backends.monolith import exec as monolith_exec

            result = monolith_exec(namespace, action, args, task=task)
            preview = json.dumps(result, ensure_ascii=False)[:300]
            print(f"\n  >> [Monolith] {namespace}.{action} → {preview}")
            return json.dumps(result, ensure_ascii=False)

    from core.editor_exec import exec_tool
    result = exec_tool(toolset_name, short_name, args, task=task)
    preview = json.dumps(result, ensure_ascii=False)[:300]
    print(f"\n  >> [UE] {toolset_name or '<top>'}::{short_name} → {preview}")
    return json.dumps(result, ensure_ascii=False)


def _project_context(query: str) -> str:
    try:
        from core.rag_core import retrieve as rag_retrieve
        hits = rag_retrieve(query, top_k=5)
    except Exception as exc:
        return f"(检索失败: {type(exc).__name__})"
    if not hits:
        return "(检索无命中)"
    return "\n".join(hit.get("content") or "" for hit in hits)


def _nearest_skill(query: str) -> str:
    if not SKILL_CARDS_PATH.is_file():
        return ""
    try:
        cards = json.loads(SKILL_CARDS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not cards:
        return ""
    from core.retrieve_funnel import skill_names_for
    full_cards = cards
    wanted = skill_names_for(query)
    scoped = False
    if wanted:
        picked = [card for card in full_cards if card.get("name") in wanted]
        if picked:
            cards = picked
            scoped = True
    if len(cards) == 1:
        card = cards[0]
        return _attach_solution(query, _skill_context(card, scoped))
    try:
        import numpy as np
        from core.rag_core import _get_model
        model = _get_model()
        if scoped:
            texts = [card.get("description") or card.get("name") or "" for card in cards]
            matrix = model.encode(texts, normalize_embeddings=True)
            query_vec = model.encode([query], normalize_embeddings=True)[0]
            card = cards[int(np.asarray(matrix) @ np.asarray(query_vec)).argmax()]
        else:
            mtime = SKILL_CARDS_PATH.stat().st_mtime
            if _skill_vectors["mtime"] != mtime or len(_skill_vectors.get("cards") or []) != len(full_cards):
                texts = [card.get("description") or card.get("name") or "" for card in full_cards]
                _skill_vectors["matrix"] = model.encode(texts, normalize_embeddings=True)
                _skill_vectors["cards"] = full_cards
                _skill_vectors["mtime"] = mtime
                _skill_vectors["model"] = model
            model = _skill_vectors["model"]
            query_vec = model.encode([query], normalize_embeddings=True)[0]
            scores = np.asarray(_skill_vectors["matrix"]) @ np.asarray(query_vec)
            card = _skill_vectors["cards"][int(scores.argmax())]
    except Exception:
        card = cards[0]
    return _attach_solution(query, _skill_context(card, scoped))


def _skill_context(card: dict, scoped: bool) -> str:
    return skill_card_line(card) + project_rules_suffix(scoped, load_agent_skill_prompt())


def _attach_solution(query: str, line: str) -> str:
    from core.retrieve_funnel import matching_solution
    extra = matching_solution(query)
    if not extra:
        return line
    return line + "\n命中一条：\n" + extra


def _verify_specs(state: AgentState, kinds: set[str]) -> list[dict]:
    task = state.get("task") or {}
    return [item for item in (task.get("verify") or []) if item.get("kind") in kinds]


def _pin_tools(tools: list[dict], names: list[str]) -> list[dict]:
    have = {item["function"]["name"] for item in tools}
    for short_name in names:
        schema = tool_schema(short_name)
        if schema and schema["function"]["name"] not in have:
            tools.append(schema)
            have.add(schema["function"]["name"])
    return tools


def _monolith_pinned(names: list[str]) -> list[dict]:
    """backend=monolith 时只给任务允许的 namespace.action，不把官方工具塞进列表。"""
    tools = []
    for name in names:
        text = str(name or "").strip()
        if not text or "." in text:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": text,
                "description": "Monolith action. 不要调用官方 MCP。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actor_name": {"type": "string"},
                        "location": {"type": "array", "items": {"type": "number"}},
                        "asset_path": {"type": "string"},
                    },
                },
            },
        })
    return tools


def retrieve_node(state: AgentState):
    print("--- [Node] retrieve ---")
    from agent.roles import CPP_TOOLS, EDITOR_READS, plan_role
    from backends.select import current_backend

    task = state.get("task") or {}
    role = plan_role(task)
    if role == "cpp":
        tools = get_local_tools(CPP_TOOLS)
    elif role == "editor" and current_backend() == "monolith":
        tools = _monolith_pinned(list(task.get("allow_writes") or []))
    elif role == "editor":
        tools = _pin_tools([], list(task.get("allow_writes") or []) + sorted(EDITOR_READS))
    elif role == "mixed":
        tools = _pin_tools(get_local_tools(CPP_TOOLS), list(task.get("allow_writes") or []) + sorted(EDITOR_READS))
    else:
        tools = get_local_tools() + retrieve_tools(state["user_query"], top_k=8)
        pinned = list(task.get("allow_writes") or [])
        if any(item.get("kind") == "editor_assert" for item in (task.get("verify") or [])):
            pinned.extend(["find_actors", "get_actor_transform"])
        tools = _pin_tools(tools, pinned)
    if not state.get("require_build"):
        tools = tools + [FINISH_TOOL]
    return {"available_tools": tools}


def _role_constraints(state: AgentState, role: str) -> str:
    from config import project_module, source_rel_prefix

    task = state.get("task") or {}
    allow = ", ".join(task.get("allow_writes") or []) or "（无）"
    module = project_module()
    source_root = source_rel_prefix().rstrip("/") or f"Source/{module}"
    if role == "cpp":
        return f"""
【角色】C++。只能使用 read_source 和 apply_unified_diff。不要调用编辑器工具，不要调用 finish。
空工具调用会进入裁判。裁判失败后，下一条消息里会有 errors。
path 相对 {source_root}，例如 Private/类名.cpp。
diff 的旧行必须来自最近一次 read_source 返回的 text，不要带行号。写码成功后文件已变，旧行要用新读取重做。每段 diff 成功后行号会偏移：下一段 diff 前必须重新 read_source，@@ 旧行号用这次返回的 numbered。
"""
    if role == "editor":
        return f"""
【角色】编辑器。不要读或改 Source，不要调用 finish。
允许的写工具：{allow}。没列出的写操作会被拒绝。
没有需要调用的写工具时，不要调用工具。空工具调用会进入裁判。
修改 Actor 前必须先 find_actors。name、tag、collision_channels 都要传，空的也要显式给。
引擎类用 /Script/Engine.XXX，工程类用 /Script/{module}.XXX。不确定就不要臆造路径。
"""
    if role == "mixed":
        return f"""
【角色】这个任务同时改 C++ 和编辑器。C++ 只用 read_source 与 apply_unified_diff。
编辑器写工具只允许：{allow}。不要调用 finish。
"""
    if state.get("require_build"):
        return f"""
【UE 类路径规则】
- 引擎内置类：/Script/Engine.XXX
- 工程自定义类：/Script/{module}.XXX
- 不确定类路径时，不要臆造，改用 find_actors。name、tag、collision_channels 三个参数都要传，空的也要显式给。

【流程约束】
- 工具调用是唯一能改工程的方式。空工具调用会直接进入裁判。裁判失败后你会在下一条里看到 errors。
- 你没有 finish，不能自己宣布成功。
- 允许的编辑器写工具：{allow}。没列出的写操作会被拒绝。
- 改 C++ 只能 read_source 再 apply_unified_diff。path 相对 {source_root}，例如 Private/类名.cpp。
- diff 里的旧行必须来自最近一次 read_source 返回的 text，不要带行号。写码成功后文件已变，旧行要用新读取重做。每段 diff 成功后行号会偏移：下一段 diff 前必须重新 read_source，@@ 旧行号用这次返回的 numbered。
- 所有 diff 写完就停：不要再 read_source 复查，直接回复不带任何工具调用，进入编译裁判。裁判失败会把编译错误带给你再改。
"""
    return """

【流程约束 · 一般任务】
- 当任务完成、失败或无法继续时，必须调用 finish(success, summary)。
- 没有列入允许列表的写操作会被拒绝。
"""


_READ_DIFF_TOOLS = frozenset({"read_source", "apply_unified_diff"})
# 控件图和 Actor 查找。连续只读时下一轮提示若看不到结果，模型会把同一对调用打到 recursion_limit。
_EDITOR_LOOP_READS = frozenset({
    "find_actors",
    "get_actor_transform",
    "GetWidgets",
    "GetWidgetDescription",
    "GetWidgetTreeDepth",
    "GetNamedSlots",
    "ListWidgetClasses",
    "GetWidgetClassInfo",
})


def _pre_build_stall(state: AgentState) -> tuple[str, str]:
    """编译还没跑过时的打转诊断（对应 20261014-180852：34 轮 plan↔execute 后撞 recursion_limit=80）。
    返回 (诊断, 动作)；动作 ""=没在打转 / "build"=写码已完成只是不肯停读 → 借现有「空工具调用→build」路由直接进编译 /
    "stop"=一次都没写成功且失败过半 → 干净结束。build 已经跑过的打转由 graph._read_stall 管。"""
    if state.get("build_result") or not state.get("require_build"):
        return "", ""
    outcomes = state.get("tool_outcomes") or []
    if len(outcomes) < 8:
        return "", ""
    if state.get("source_written"):
        tail = outcomes[-8:]
        if all(item.get("tool") == "read_source" for item in tail):
            return (
                "写码成功后连续 8 次只在 read_source，没去编译。停止读取，直接进入编译裁判。",
                "build",
            )
    if len(outcomes) >= 12:
        recent = outcomes[-12:]
        if all(item.get("tool") in _READ_DIFF_TOOLS for item in recent):
            failures = [item for item in recent if not item.get("ok")]
            if len(failures) >= 6 and not any(
                item.get("tool") == "apply_unified_diff" and item.get("ok") for item in recent
            ):
                last_error = str(failures[-1].get("error") or "diff conflict")
                return (
                    "build 前打转：最近 12 次调用全是 read_source / apply_unified_diff，"
                    f"其中 {len(failures)} 次失败、没有一次写码成功（最近一次：{last_error}）。"
                    "停止这一轮，把这条诊断交回。",
                    "stop",
                )
    return "", ""


def _editor_read_stall(state: AgentState) -> tuple[str, str]:
    """控件 / Actor 只读打转（20261021-121520：GetWidgets 与 GetWidgetDescription 交替直到 recursion_limit=80）。

    返回 (诊断, 动作)。动作 ""=没在打转 / "build"=已经写过、只是不肯停读 /
    "stop"=一次都没写成功。没有写入时不能借空工具调用进编译，否则未改的控件会被判成通过。
    """
    outcomes = state.get("tool_outcomes") or []
    if len(outcomes) < 8:
        return "", ""
    if not all(item.get("tool") in _EDITOR_LOOP_READS for item in outcomes[-8:]):
        return "", ""
    wrote = any(
        item.get("ok") and item.get("tool") not in _EDITOR_LOOP_READS
        for item in outcomes
    )
    if wrote:
        return (
            "连续 8 次只在读控件或 Actor，停止读取，直接进入编译裁判。",
            "build",
        )
    return (
        "编辑器打转：最近 8 次调用全是读控件或查找 Actor，没有一次写入。停止这一轮。",
        "stop",
    )


def plan_node(state: AgentState):
    print("--- [Node] plan ---")
    from agent.roles import plan_role

    stall, action = _pre_build_stall(state)
    if not action:
        stall, action = _editor_read_stall(state)
    if action == "build":
        # 借现有路由：plan 不带工具调用 + require_build + 未编译 → build。
        # 编译过了就进 fix 的正常循环，没过就结束；不再把写完的代码判成失败还原掉。
        print(f"  [Plan] 停止读取，直接进编译：{stall}")
        return {
            "messages": [{"role": "assistant", "content": f"停止读取：{stall}"}],
            "tool_calls": [],
            "llm_usage": {},
        }
    if action == "stop":
        print(f"  [Plan] 熔断：{stall}")
        return {
            "messages": [{"role": "assistant", "content": f"停止：{stall}"}],
            "tool_calls": [],
            "llm_usage": {},
            "is_completed": True,
            "final_success": False,
            "last_error": stall,
        }

    task = state.get("task") or {}
    role = plan_role(task)
    if task.get("judge_first") and int(state.get("retry_count") or 0) == 0:
        return {
            "messages": [{"role": "assistant", "content": "先跑裁判，这一轮不改文件。"}],
            "tool_calls": [],
            "llm_usage": {},
        }

    context = _project_context(state["user_query"])
    skill = _nearest_skill(state["user_query"])
    if skill:
        context = context + "\n" + skill
    prompt = build_agent_prompt(state["user_query"], context, require_verify=bool(state.get("require_build")))
    prompt += _role_constraints(state, role)

    from agent.context import slice_counts, visible_messages
    from core.llm_call import complete, reasoning_content, usage_for_trace

    messages = [{"role": "system", "content": prompt}] + visible_messages(state["messages"], task)
    counts = slice_counts(state["messages"], task)
    response, record = complete(
        messages,
        tools=state["available_tools"],
        tool_choice="auto",
        caller="plan",
    )
    msg = response.choices[0].message
    tool_calls_dict = []
    if msg.tool_calls:
        for tc in msg.tool_calls:
            tool_calls_dict.append({
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            })
    msg_dict = {"role": "assistant", "content": msg.content or ""}
    reasoning = reasoning_content(msg)
    if reasoning:
        msg_dict["reasoning_content"] = reasoning
    if tool_calls_dict:
        msg_dict["tool_calls"] = tool_calls_dict
    thought = {"tool": "llm", "ok": True}
    content = str(msg.content or "").strip()
    if content:
        thought["summary"] = content[:240]
    if reasoning:
        thought["reasoning"] = reasoning[:800]
    if tool_calls_dict:
        thought["calls"] = [
            str((tc.get("function") or {}).get("name") or "")
            for tc in tool_calls_dict
            if str((tc.get("function") or {}).get("name") or "")
        ]
    step_log = [{"tool": "context_slice", "ok": True, "context_slice": counts}]
    if thought.get("summary") or thought.get("reasoning") or thought.get("calls"):
        step_log.append(thought)
    return {
        "messages": [msg_dict],
        "tool_calls": tool_calls_dict,
        "llm_usage": usage_for_trace(record),
        "step_log": step_log,
    }


def execute_node(state: AgentState):
    print("--- [Node] execute ---")
    results = []
    new_messages = []
    finish_signal = None
    step_log = []
    tools_used = list(state.get("tools_used") or [])
    tool_outcomes = list(state.get("tool_outcomes") or [])
    source_written = bool(state.get("source_written"))
    task = state.get("task") or {}

    for call in state["tool_calls"]:
        tool_name = call["function"]["name"]
        arguments = call["function"]["arguments"]
        call_id = call["id"]

        from agent.roles import tool_allowed
        from core.tool_router import get_full_name, split_full_name

        full_name = get_full_name(tool_name) or tool_name
        _, short_name = split_full_name(full_name)
        if tool_name != "finish" and not tool_allowed(short_name, task):
            from agent.harness import policy_snapshot

            policy = policy_snapshot(task)
            result = json.dumps({"ok": False, "error": "role refused", "policy": policy}, ensure_ascii=False)
            results.append(result)
            new_messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
            tools_used.append(short_name)
            tool_outcomes.append({"tool": short_name, "ok": False, "error": "role refused"})
            step_log.append({"tool": short_name, "ok": False, "error": "role refused", "policy": policy})
            continue

        if tool_name == "finish":
            try:
                args = json.loads(arguments)
            except Exception:
                args = {"success": False, "summary": "(finish 参数解析失败)"}
            finish_signal = args
            print(f"  >> [Finish] success={args.get('success')}, summary={args.get('summary')}")
            new_messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps({"acknowledged": True}),
            })
            new_messages.append({
                "role": "assistant",
                "content": f"{'✅' if args.get('success') else '❌'} {args.get('summary', '')}",
            })
            step_log.append({"tool": "finish", "ok": bool(args.get("success"))})
            continue

        result = execute_ue_tool(tool_name, arguments, task=task)
        results.append(result)
        new_messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
        ok = _ok_from_payload(result)
        err = "" if ok else _error_from_payload(result)
        tools_used.append(short_name)
        tool_outcomes.append({"tool": short_name, "ok": ok, "error": err})
        logged = {"tool": short_name, "ok": ok}
        if err:
            logged["error"] = err
        policy = _policy_from_payload(result)
        if policy:
            logged["policy"] = policy
        step_log.append(logged)
        if short_name == "apply_unified_diff" and ok:
            source_written = True

    update = {
        "messages": new_messages,
        "tool_results": results,
        "tool_calls": [],
        "step_log": step_log,
        "tools_used": tools_used,
        "tool_outcomes": tool_outcomes,
        "source_written": source_written,
    }
    if finish_signal is not None and not state.get("require_build"):
        update["is_completed"] = True
        update["final_success"] = bool(finish_signal.get("success", False))
    return update


def _merge_judges(kind: str, judges: list[dict]) -> dict:
    if not judges:
        return {"ok": True, "kind": "skip", "summary": "no verify", "errors": []}
    errors = []
    for item in judges:
        errors.extend(item.get("errors") or [])
    return {
        "ok": all(item.get("ok") for item in judges),
        "kind": kind if len(judges) == 1 else "multi",
        "summary": "; ".join(item.get("summary") or "" for item in judges),
        "errors": errors,
    }


def build_node(state: AgentState):
    print("--- [Node] build ---")
    from core.judge import judge
    specs = _verify_specs(state, _BUILD_KINDS)
    if not specs:
        verdict = {"ok": True, "kind": "skip", "summary": "no build verify", "errors": []}
    else:
        verdict = _merge_judges(specs[0]["kind"], [judge(spec, state.get("task")) for spec in specs])
    print(f"  [Build] ok={verdict['ok']} {verdict['summary']}")
    return {
        "build_judge": verdict,
        "build_result": json.dumps(verdict, ensure_ascii=False),
        "step_log": [{"tool": verdict.get("kind") or "build", "ok": verdict.get("ok") is True}],
    }


def _test_specs(state: AgentState) -> list[dict]:
    specs = _verify_specs(state, _TEST_KINDS)
    filt = str((state.get("task") or {}).get("automation_filter") or "").strip()
    if filt and not any((item.get("filter") or item.get("filterExpression")) == filt for item in specs):
        specs.append({"kind": "automation", "filter": filt})
    return specs


def test_node(state: AgentState):
    print("--- [Node] test ---")
    from core.judge import judge
    specs = _test_specs(state)
    if not specs:
        verdict = {"ok": True, "kind": "skip", "summary": "no test verify", "errors": []}
    else:
        verdict = _merge_judges(specs[0]["kind"], [judge(spec, state.get("task")) for spec in specs])
    print(f"  [Test] ok={verdict['ok']} {verdict['summary']}")
    return {
        "test_judge": verdict,
        "test_result": json.dumps(verdict, ensure_ascii=False),
        "step_log": [{
            "tool": verdict.get("kind") or "test",
            "ok": verdict.get("ok") is True,
            "judge_kind": verdict.get("kind") or "",
            "summary": verdict.get("summary") or "",
        }],
    }


def _policy_from_payload(result: str) -> dict | None:
    try:
        data = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return None
    policy = data.get("policy") if isinstance(data, dict) else None
    return policy if isinstance(policy, dict) else None


def _criteria_update(state: AgentState, *judges) -> dict | None:
    task = dict(state.get("task") or {})
    criteria = task.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        return None
    from agent.harness import apply_judges

    # 图内裁判也带窗口 id，和窗口层同一套归属口径：条款只认列出自己的窗口。
    wid = str(task.get("id") or "").strip()
    stamped = []
    for judge in judges:
        if isinstance(judge, dict):
            judge = dict(judge)
            if wid:
                judge["window"] = wid
        stamped.append(judge)
    task["criteria"] = apply_judges(criteria, *stamped)
    return task


def _format_errors(errors: list[dict]) -> str:
    if not errors:
        return "(没有结构化错误)"
    lines = []
    for err in errors:
        lines.append(f"文件: {err.get('file') or ''}\n行: {err.get('line') or 0}\n原文: {err.get('message') or ''}")
    return "\n\n".join(lines)


def _observe_fields(build_judge: dict, test_judge: dict) -> tuple[str, str]:
    failed = [item for item in (build_judge, test_judge) if item.get("ok") is False]
    chosen = failed[0] if failed else (test_judge if test_judge.get("kind") not in (None, "", "skip") else build_judge)
    kind = str(chosen.get("kind") or "")
    errors = chosen.get("errors") or []
    if errors:
        err = str(errors[0].get("message") or "")
    elif chosen.get("ok") is False:
        err = str(chosen.get("summary") or "")
    else:
        err = ""
    return kind, err[:200]


def observe_node(state: AgentState):
    print("--- [Node] observe ---")
    require = state.get("require_build")
    build_judge = state.get("build_judge") or {}
    test_judge = state.get("test_judge") or {}

    if require:
        kind, err = _observe_fields(build_judge, test_judge)
        if build_judge.get("ok") is False or test_judge.get("ok") is False:
            print(f"  [Observe] 裁判失败，retry: {state['retry_count']} -> {state['retry_count'] + 1}")
            failed = {
                "retry_count": state["retry_count"] + 1,
                "is_completed": False,
                "final_success": False,
                "step_log": [{"tool": "", "ok": False, "judge_kind": kind, "error": err}],
            }
            if (state.get("task") or {}).get("freeze_source"):
                failed["is_completed"] = True
                print("  [Observe] freeze_source，留下裁判原文，不进入改文件")
            elif (
                build_judge.get("ok") is True
                and str(test_judge.get("kind") or "") == "automation"
                and test_judge.get("ok") is False
            ):
                failed["is_completed"] = True
                print("  [Observe] Automation 残差，本扇结束，不进入改文件")
            criteria_task = _criteria_update(state, build_judge, test_judge)
            if criteria_task is not None:
                failed["task"] = criteria_task
            return failed
        if build_judge.get("ok") is True and test_judge.get("ok") is True:
            print("  [Observe] 裁判通过")
            summary = f"裁判通过\nBuild: {build_judge.get('summary')}\nTest: {test_judge.get('summary')}"
            passed = {
                "is_completed": True,
                "final_success": True,
                "messages": [{"role": "assistant", "content": summary}],
                "step_log": [{
                    "tool": "",
                    "ok": True,
                    "judge_kind": kind,
                    "summary": summary,
                }],
            }
            criteria_task = _criteria_update(state, build_judge, test_judge)
            if criteria_task is not None:
                passed["task"] = criteria_task
            return passed
        return {}

    for msg in reversed(state["messages"][-20:]):
        if not isinstance(msg, dict):
            continue
        for tc in (msg.get("tool_calls") or []):
            if tc["function"]["name"] == "finish":
                try:
                    args = json.loads(tc["function"]["arguments"])
                except Exception:
                    args = {"success": False, "summary": "(解析失败)"}
                success = bool(args.get("success", False))
                print(f"  [Observe] 兜底捕获 finish(success={success})")
                return {"is_completed": True, "final_success": success}
        if msg.get("role") == "user":
            break
    print("  [Observe] 无 finish 信号，继续循环")
    return {}


def fix_node(state: AgentState):
    print("--- [Node] fix ---")
    errors = []
    for judge in (state.get("build_judge") or {}, state.get("test_judge") or {}):
        if judge.get("ok") is False:
            errors.extend(judge.get("errors") or [])
    memory = ""
    message = errors[0]["message"] if errors else ""
    if message:
        # 症状对上的失败模式优先。对不上不编造，再查 ue_error。
        try:
            from agent.failure_memory import hint_text, lookup_pattern

            pattern = lookup_pattern(message)
        except Exception:
            pattern = None
        if pattern:
            memory = "\n\n失败模式：" + hint_text(pattern)
        try:
            from core.rag_core import retrieve as rag_retrieve
            hits = rag_retrieve(message, top_k=1, where={"source_type": "ue_error"})
        except Exception:
            hits = []
        if hits:
            memory += "\n\n以往同类错误：\n" + (hits[0].get("content") or "")
    text = "裁判失败。下一轮必须调用工具修改工程，不要只回复说明。\n\n" + _format_errors(errors) + memory
    failure_log = list(state.get("failure_log") or [])
    failure_log.append({"errors": errors})
    print(f"  [Fix] {len(errors)} errors")
    return {
        "messages": [{"role": "user", "content": text}],
        "fix_plan": text[:500],
        "build_result": "",
        "test_result": "",
        "build_judge": {},
        "test_judge": {},
        "failure_log": failure_log,
    }
