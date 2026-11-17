# agent/spec_pipeline.py
"""全域「模型接管 / 脚本兜住」的共享执行流水线（20261117 边界升级总览 §1/§3）。

这个模块只做一件事：把任意域的「Spec → 确定性执行 → 只读复核 → 回灌修复 → 沉淀」
这套**形状**收成一份通用内核。各域（UMG / 蓝图图 / 材质 / Niagara / 动画蓝图 /
动画&ControlRig / 后处理 / Sequencer / C++）各自只提供：

  - ``KIND``            该域标识（与 spec["kind"] 对应）
  - ``SPEC_HINT``      给理解器/规划者看的 Spec 结构说明（模型据此产 Spec）
  - ``WHITELIST``      该域可接受的类/属性/轨道/通道等白名单（数据，不是代码）
  - ``validate_spec``  Spec 结构 + 白名单 + 成环/越界校验（纯函数，返回错误清单）
  - ``plan_ops``       Spec → 有序「抽象原语」清单（确定性、幂等；不含时间/随机）
  - ``exec_op``        抽象原语 → 真实工具调用（经注入的 ``call``，离线可替身）
  - ``assertions``     Spec → 期望断言清单（只读复核读回什么）
  - ``verify_spec``    Spec + 读回 → 未通过的断言（纯函数）
  - ``recognize``      一句话是否稳定属于本域（路由识别，宁缺毋滥）
  - ``write_task``     写闸：allow_writes / save / pie / allow_*（该域的写策略）

本文件提供这些域共享的**机制**：断言求值、路径取值、有序执行、回灌闭环、沉淀候选。
它不 import 任何具体域，也不在 import 期连编辑器——``default_call`` 里才惰性 import
``core.editor_exec.exec_tool``。这样离线守卫可以在完全不碰编辑器的情况下把内核钉死。

边界纪律（与总览 §1 一致）：
- 模型只产 Spec，永不直接发写工具；
- 脚本按 Spec 确定性执行 + 只读复核；
- 复核不过 → 结构化错误回灌模型改 Spec（≤ max_rounds），仍不过 → 暂停点名，不假成功；
- 稳定可验收的方案才进沉淀（graduation），novel 永远回模型。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable


# ------------------------------------------------------------------ 结构化错误
def spec_error(code: str, message: str, *, where: str = "", hint: str = "") -> dict:
    """一条可回灌模型的结构化错误（不是裸字符串）。"""
    return {
        "code": str(code),
        "message": str(message),
        "where": str(where),
        "hint": str(hint),
    }


class SpecUnsupported(Exception):
    """该域今天在脚本侧确实做不到（如需人工连线、工具集缺失）：宁可点名暂停，不许假装做完。"""

    def __init__(self, message: str, *, code: str = "unsupported"):
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ 路径取值
_PATH_TOKEN = re.compile(r"[^.\[\]]+")


def resolve_path(data: Any, path: str) -> tuple[bool, Any]:
    """按 ``a.b.0.c`` 在 dict / list 里取值。取不到返回 (False, None)，不抛。"""
    if not path:
        return False, None
    cur = data
    for token in _PATH_TOKEN.findall(str(path)):
        if isinstance(cur, dict):
            if token not in cur:
                return False, None
            cur = cur[token]
        elif isinstance(cur, (list, tuple)):
            try:
                idx = int(token)
            except ValueError:
                return False, None
            if idx < 0 or idx >= len(cur):
                return False, None
            cur = cur[idx]
        else:
            return False, None
    return True, cur


def _parse_value(raw: Any) -> Any:
    """断言右值：能按 JSON 解就按 JSON，否则当普通字符串（去首尾引号）。"""
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# 字符串断言支持：``path == value`` / ``path != value`` / ``exists:path`` /
# ``count:path == n`` / ``path contains needle`` / ``path >= n``（>= <= > < 同理）。
_CMP_RE = re.compile(r"^(?P<path>.+?)\s*(?P<op>==|!=|>=|<=|>|<)\s*(?P<value>.*)$")
_CONTAINS_RE = re.compile(r"^(?P<path>.+?)\s+contains\s+(?P<value>.+)$")


def _normalize_assertion(assertion: Any) -> dict:
    """把一条断言（dict 或字符串）归一成 {path, op, value}。解析不了就抛 ValueError。"""
    if isinstance(assertion, dict):
        op = str(assertion.get("op") or "eq").strip() or "eq"
        return {"path": str(assertion.get("path") or ""), "op": op,
                "value": assertion.get("value"), "raw": assertion}
    text = str(assertion or "").strip()
    if not text:
        raise ValueError("空断言")
    if text.startswith("exists:"):
        return {"path": text[len("exists:"):].strip(), "op": "exists", "value": None, "raw": text}
    if text.startswith("count:"):
        rest = text[len("count:"):]
        m = _CMP_RE.match(rest)
        if not m:
            raise ValueError(f"count 断言缺比较：{text}")
        return {"path": m.group("path").strip(), "op": "count",
                "value": _parse_value(m.group("value")), "raw": text}
    if text.endswith("compile_ok"):
        return {"path": "compiled", "op": "eq", "value": True, "raw": text}
    if text.endswith("saved"):
        return {"path": "saved", "op": "eq", "value": True, "raw": text}
    m = _CONTAINS_RE.match(text)
    if m:
        return {"path": m.group("path").strip(), "op": "contains",
                "value": m.group("value").strip(), "raw": text}
    m = _CMP_RE.match(text)
    if m:
        raw = m.group("value").strip()
        op = m.group("op")
        return {"path": m.group("path").strip(),
                "op": "eq" if op == "==" else ("ne" if op == "!=" else op),
                "value": _parse_value(raw), "raw": text}
    # 裸路径：只要求存在且为真
    return {"path": text, "op": "exists", "value": None, "raw": text}


def eval_assertion(assertion: Any, readback: dict) -> dict:
    """单条断言求值。返回 {ok, detail}；不做 I/O，纯函数。"""
    try:
        norm = _normalize_assertion(assertion)
    except ValueError as exc:
        return {"ok": False, "detail": f"断言无法解析：{exc}"}
    path, op, want = norm["path"], norm["op"], norm["value"]
    found, got = resolve_path(readback, path)
    if op == "exists":
        ok = found and got is not None and got is not False
        return {"ok": ok, "detail": f"期望 {path} 存在{'且为真' if want is None else ''}，实际 {got!r}"}
    if op == "count":
        n = len(got) if isinstance(got, (list, tuple, dict, str)) else None
        wn = _num(want)
        ok = found and n is not None and wn is not None and n == int(wn)
        return {"ok": ok, "detail": f"期望 {path} 条目数={want}，实际 {n!r}"}
    if not found:
        return {"ok": False, "detail": f"{path} 读不到（readback 里没有这个路径）"}
    if op == "eq":
        ok = got == want
        return {"ok": ok, "detail": f"期望 {path}=={want!r}，实际 {got!r}"}
    if op == "ne":
        ok = got != want
        return {"ok": ok, "detail": f"期望 {path}!={want!r}，实际 {got!r}"}
    if op == "contains":
        ok = (want in got) if isinstance(got, (list, tuple, str, dict)) else False
        return {"ok": ok, "detail": f"期望 {path} 含 {want!r}，实际 {got!r}"}
    if op in (">", "<", ">=", "<="):
        gn, wn = _num(got), _num(want)
        if gn is None or wn is None:
            return {"ok": False, "detail": f"{path} 与 {want!r} 不是数字，无法比较"}
        ok = {">": gn > wn, "<": gn < wn, ">=": gn >= wn, "<=": gn <= wn}[op]
        return {"ok": ok, "detail": f"期望 {path}{op}{want}，实际 {got!r}"}
    return {"ok": False, "detail": f"未知断言运算符 {op!r}"}


def eval_assertions(assertions: Iterable[Any], readback: dict) -> list[str]:
    """批量求值，返回未通过断言的可读说明（空列表 = 全过）。"""
    failures: list[str] = []
    for item in assertions or []:
        result = eval_assertion(item, readback or {})
        if not result["ok"]:
            failures.append(result["detail"])
    return failures


# ------------------------------------------------------------------ 写闸
def make_write_task(allow_writes: Iterable[str], *, save: bool = False, pie: bool = False,
                    allow_key: str | None = None) -> dict:
    """构造 exec_tool 的 task（写白名单 + save/pie + 该域 allow_* 开关）。

    ``allow_key`` 取 "allow_umg" / "allow_material" / "allow_niagara" / "allow_anim" 之一；
    该域开关一律设 True——真正的放行由人在 session_task / .env 决定，内核只声明意图
    （见总览 §1「写编辑器前放行归脚本写闸」）。"""
    task: dict = {"allow_writes": list(allow_writes), "save": bool(save), "pie": bool(pie)}
    if allow_key:
        task[allow_key] = True
    return task


def default_call(toolset: str, tool: str, args: dict | None, task: dict | None = None) -> dict:
    """真实执行入口（惰性 import，离线守卫注入替身，永不触发这条）。

    ``toolset`` 若是 monolith 原生命名空间（ui/material/niagara/animation/level_sequence/
    blueprint/editor…，见 agent/spec_toolmap.inventory），直接走 ``backends.monolith.exec``；
    否则按 official/bridged 走标准 ``editor_exec.exec_tool``。这样内核既可用核实过的原生命名
    （spec_toolmap 单一事实源），也兼容 official-style 家族名。"""
    try:
        from agent import spec_toolmap

        native = spec_toolmap.is_namespace(toolset)
    except Exception:  # noqa: BLE001 —— toolmap 不可用时不挡住 "ui" 这条老路径
        native = (toolset == "ui")
    if native:
        from backends.monolith import exec as monolith_exec

        return monolith_exec(toolset, tool, args or {}, task=task or {})
    from core.editor_exec import exec_tool

    return exec_tool(toolset, tool, args or {}, task=task or {})


# ------------------------------------------------------------------ 有序执行
def run_ops(module, spec: dict, call: Callable | None, write: dict) -> dict:
    """按 ``module.plan_ops(spec)`` 顺序执行。返回 {ok, tools, readback, error, op}。

    - 每个 op 经 ``module.exec_op(op, call, write)`` 落地；该函数返回
      {ok, tools?, readback?, error?}。
    - 任一 op 失败立刻停（结构化失败，带 op 定位），不继续往下写。
    - readback 累积合并（后写覆盖同键），供随后的只读复核断言用。
    """
    call = call or default_call
    tools: list[str] = []
    readback: dict = {}
    for op in module.plan_ops(spec) or []:
        result = module.exec_op(op, call, write) or {}
        tools.extend(result.get("tools") or [])
        readback.update(result.get("readback") or {})
        if not result.get("ok"):
            return {
                "ok": False, "tools": tools, "readback": readback, "op": op,
                "error": spec_error("exec_failed",
                                    f"原语 {op.get('op')} 执行失败：{result.get('error') or '未知错误'}",
                                    where=str(op.get("target") or op.get("op") or "")),
            }
    return {"ok": True, "tools": tools, "readback": readback, "op": None, "error": None}


# ------------------------------------------------------------------ 主流水线
def build_domain(module, step: dict, *, call: Callable | None = None,
                 fix_model: Callable | None = None, max_rounds: int = 2) -> dict:
    """一个域的完整闭环：validate → plan_ops/exec → verify → 回灌修复(≤N) → 终态。

    ``step`` 至少带 ``spec``（模型产出的 Spec dict）；没有就按 ``module.extract_spec``
    从 step 里取（各域可覆盖，如从 slots 复原）。返回统一形状：

      {ok, phase, domain, judge_kind, spec, tools, exec_count, readback,
       rounds, errors, summary|paused/reason}

    ``fix_model(spec, errors) -> dict|None`` 是回灌修复口：拿到结构化错误返回**新 Spec**；
    返回 None 表示模型也救不回 ⇒ 暂停点名。离线守卫注入替身，不调真模型。
    """
    extract = getattr(module, "extract_spec", None)
    spec = step.get("spec") if isinstance(step, dict) else None
    if not isinstance(spec, dict) and extract is not None:
        spec = extract(step)
    domain = str(getattr(module, "KIND", "") or "")
    judge_kind = str(getattr(module, "JUDGE_KIND", "editor_assert") or "editor_assert")

    def _fail(phase: str, errors: list, *, paused: bool = False, reason: str = "",
              tools: list | None = None, rounds: int = 1, extra: dict | None = None) -> dict:
        out = {
            "ok": False, "phase": phase, "domain": domain, "judge_kind": judge_kind,
            "spec": spec, "tools": list(tools or []), "exec_count": len(tools or []),
            "rounds": rounds, "errors": errors,
        }
        if paused:
            out["paused"] = True
            out["reason"] = reason or (errors[0]["message"] if errors else "无法完成")
        if extra:
            out.update(extra)
        return out

    if not isinstance(spec, dict):
        return _fail("extract", [spec_error("no_spec", "没有拿到结构化 Spec（模型未产出）",
                                            hint="先让模型按 SPEC_HINT 产出 spec")])

    rounds = 0
    last_errors: list = []
    last_tools: list = []
    while True:
        rounds += 1
        errors = module.validate_spec(spec) or []
        if errors:
            norm = [e if isinstance(e, dict) else spec_error("validate", str(e)) for e in errors]
            last_errors = norm
        else:
            write = module.write_task(step) if hasattr(module, "write_task") else make_write_task([])
            built = run_ops(module, spec, call, write)
            last_tools = built["tools"]
            if not built["ok"]:
                last_errors = [built["error"]]
            else:
                fails = module.verify_spec(spec, built["readback"]) or []
                if not fails:
                    summary = module.summarize(spec) if hasattr(module, "summarize") else f"{domain} 已落地并复核通过"
                    return {
                        "ok": True, "phase": "done", "domain": domain, "judge_kind": judge_kind,
                        "spec": spec, "tools": last_tools, "exec_count": len(last_tools),
                        "readback": built["readback"], "rounds": rounds, "errors": [],
                        "summary": summary,
                    }
                last_errors = [spec_error("verify", str(f), where=domain) for f in fails]
        # 回灌修复：仅在还有预算且给了修复口时继续
        if fix_model is not None and rounds <= max_rounds:
            repaired = fix_model(spec, last_errors)
            if isinstance(repaired, dict):
                spec = repaired
                continue
        reason = "复核/执行未通过且回灌修复用完或模型救不回，暂停点名"
        return _fail("repair" if len(last_errors) and last_errors[0].get("code") == "verify" else "build",
                     last_errors, paused=True, reason=reason, tools=last_tools, rounds=rounds)


# ------------------------------------------------------------------ 沉淀（毕业候选）
def graduation_candidate(domain: str, spec: dict, result: dict, *, query: str = "") -> dict | None:
    """稳定可验收的 Spec 才能毕业成免模型快路。这里只**产出候选**，不直接写 learned 表
    （写表是 remember_route/distill 的职责，必须过 _clauses_accounted 与 candidate_aging）。

    返回 None 表示不值得毕业（没成功 / 有暂停 / 没有 spec）。"""
    if not isinstance(result, dict) or not result.get("ok") or result.get("paused"):
        return None
    if not isinstance(spec, dict):
        return None
    return {
        "kind": f"{domain}_spec",
        "domain": domain,
        "query": str(query or ""),
        "spec": spec,
        "judge_kind": str(result.get("judge_kind") or ""),
        "exec_count": int(result.get("exec_count") or 0),
        "note": "毕业候选：稳定可验收的 Spec，交 remember_route/distill 过 _clauses_accounted",
    }


# ------------------------------------------------------------------ 小工具
def as_error_list(errors: Iterable[Any]) -> list[dict]:
    """把 validate 返回的混合清单统一成结构化错误（字符串也行）。"""
    out: list[dict] = []
    for item in errors or []:
        out.append(item if isinstance(item, dict) else spec_error("validate", str(item)))
    return out


def unsupported_result(module, reason: str, *, code: str = "unsupported") -> dict:
    """统一的「该域今天脚本做不到 / 需人工」暂停结果（诚实失败，不假成功）。"""
    return {
        "ok": False, "phase": "unsupported", "domain": str(getattr(module, "KIND", "") or ""),
        "judge_kind": str(getattr(module, "JUDGE_KIND", "") or ""), "paused": True,
        "reason": str(reason), "errors": [spec_error(code, str(reason))], "tools": [],
        "exec_count": 0, "rounds": 0,
    }
