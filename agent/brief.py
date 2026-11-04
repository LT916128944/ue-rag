# agent/brief.py
"""任务书：一句话 → 范围 / 尚未完成内容清单 / 估时区间 / 依赖 / 外部依赖。
估算与免责只是记录字段，执行层不读、不作为预算。表外内容由代码丢弃。
人审门：确认 / 修改意见 / 放弃；意见会带进下一轮并记审阅轮次。"""
import contextlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from agent.catalog import CATALOG, DISPOSE_ACTION, INTEGRATE_ACTION
from agent.ledger import ledger_summary_text
from config import PROJECT_ROOT

BRIEFS_DIR = PROJECT_ROOT / "data" / "briefs"
BRIEF_WINDOW_THRESHOLD = 3
_BRIEF_KEYWORD_RE = re.compile(r"新功能|重构")

_DISCLAIMER = ("任务之间有重叠", "估算是粗的，不构成承诺", "不含等待外部依赖的时间")

# 估时按动作类型给区间（小时）：新功能宽、只读裁决窄。只进文档。
# P1-6 起由 agent/actions.py 单一声明推导，这里不再手抄一份。
from agent.actions import ESTIMATES as _ACTION_ESTIMATE

_SCOPE_KEYS = ("id", "title", "deliverables", "pending_note", "estimate_hours", "depends_on",
               "external_dep", "windows", "blocked", "block_note")
_WINDOW_KEYS = ("id", "title", "action", "slots", "depends_on", "max_retries")
_INTEGRATION_KEYS = ("title", "deliverables", "estimate_hours")
_EXT_KEYS = ("name", "provides", "status")

# 零窗口范围的话术（20261024-192858：B/C 整段蒸发在那句「只记录待办」上）。
# 交付清单非空却没有一扇能跑的窗口，这不是「待办」，是能力缺口：确认后先试着补窗，
# 补不出就不执行，并且要在人审门和收尾里显式说出来。
# 「能力缺口」四个字放最前面：人审门的表格很窄，rich 会截到 20 字左右，
# 放在句尾就等于没显示（md 里也一样，先说的先被看见）。
_GAP_NOTE = "能力缺口：没有可执行的窗口（不是待办）。确认后先按同一路径试补窗，补不出就不执行。"
_LEGACY_ZERO_WINDOW_NOTE = "没有可执行的窗口，只记录待办，不进执行。"
# 「动作表里没有建关卡的动作」→ 抠出「建关卡」。抠不到就空着，不瞎编动作名。
_GAP_ACTION_RE = re.compile(r"(?:没有|缺少|缺|没)([^，。；、\s“”\"']{2,12}?)的动作")


# 任务书里模型给范围加「等外部」依赖：只有代码能核实的外部服务才允许它拦执行。
# 模型编出来的「UE_EDITOR_BACKEND 需等待连通」在编辑器明明连通时会把整句钉死
# （20261029-015933 的 W1/W2 就是被这一句拦住，一次都没跑）。
_VERIFIABLE_EXTERNAL = ("UE_EDITOR_BACKEND",)


def external_dep_satisfied(dep: str) -> bool:
    """「等外部」依赖现在是否其实已经满足。满足就不许再拦窗口。

    只认代码能自己探的依赖；其余一律返回 False（保持原来的「等外部」语义，
    不凭一句猜测放行）。"""
    name = str(dep or "").strip()
    if not name:
        return False
    if name in _VERIFIABLE_EXTERNAL:
        try:
            from core.editor_launch import mcp_up

            return bool(mcp_up())
        except Exception:
            return False
    return False


def _zero_window_scope(scope: dict) -> bool:
    """结构性零窗口：交付清单非空、不等外部、也不是集成末行，却没有一扇窗口。
    人审门的集成行（没有 id、没有 windows）不走这条。"""
    if not isinstance(scope, dict):
        return False
    if not str(scope.get("id") or "").strip():
        return False
    if scope.get("blocked") or str(scope.get("external_dep") or "").strip():
        return False
    if str(scope.get("kind") or "") == "integration" or str(scope.get("id") or "") == "INT":
        return False
    return not [item for item in (scope.get("windows") or []) if isinstance(item, dict)]


def capability_gap_note(scope: dict) -> str:
    """零窗口范围给人看的话：说清是能力缺口，不是「只记录待办」。
    老任务书文件里存的旧文案在这里被换掉，免得人审门照着旧句放行。
    D6：光说「缺口」不够，人审要看见「缺什么」，所以把抠得出的动作词缀在后面
    （抠不到就不缀，不瞎编——`_gap_missing_actions` 的口径）。"""
    text = str((scope or {}).get("block_note") or "").replace(_LEGACY_ZERO_WINDOW_NOTE, "").strip()
    if "能力缺口" not in text:
        text = (text + " " if text else "") + _GAP_NOTE
    missing = _gap_missing_actions(scope)
    if missing and "缺：" not in text:
        text += " 缺：" + "、".join(missing) + "。"
    return text


def _gap_missing_actions(scope: dict) -> list[str]:
    """从 pending_note/交付清单里抠「缺的动作词」。抠不到就空着——不瞎编动作名。"""
    text = " ".join(
        [str(scope.get("pending_note") or "")]
        + [str(item) for item in (scope.get("deliverables") or [])]
    )
    found: list[str] = []
    for match in _GAP_ACTION_RE.finditer(text):
        name = match.group(1).strip()
        if name and name not in found:
            found.append(name)
    return found[:3]


def _capability_gap_record(scope: dict) -> dict:
    """范围整段没有可执行窗口时的结构化记录：原因 + 交付清单 + 缺的动作词 + 现状原话。"""
    return {
        "reason": _GAP_NOTE,
        "deliverables": [str(item) for item in (scope.get("deliverables") or [])],
        "missing_actions": _gap_missing_actions(scope),
        "pending_note": str(scope.get("pending_note") or ""),
    }


def _note_gap_candidate(scope: dict, gap: dict) -> None:
    """真能提名一个动作时才记候选表；抠不到动作名就只留在 capability_gap 里。"""
    names = [str(item) for item in (gap.get("missing_actions") or []) if str(item).strip()]
    if not names:
        return
    try:
        from agent.catalog import note_candidate

        for name in names:
            note_candidate(
                name,
                source="capability_gap",
                summary=(
                    f"范围 {scope.get('id')} 没有可执行的窗口，缺这个动作："
                    + str(scope.get("pending_note") or "")[:80]
                ),
            )
    except Exception:
        pass


def _rescue_scope_windows(scope: dict, query: str, complete=None, titles=None) -> list[dict]:
    """零窗口范围先补救：调 T1 的 agent.plan_windows.scope_windows 按同一 materialize 路径补窗。
    那个函数还没落地、导入失败、或它自己抛异常，都只当补不出（返回 []），不能把物化整段打断。"""
    try:
        from agent.plan_windows import scope_windows
    except ImportError:
        return []
    except Exception:
        return []
    if not callable(scope_windows):
        return []
    use_titles = titles
    if complete is not None and use_titles is None:
        try:
            use_titles = _retrieval_titles(str(query or "")[:200], 3)
        except Exception:
            use_titles = []
    try:
        try:
            made = scope_windows(scope, query, complete=complete, titles=use_titles)
        except TypeError:
            made = scope_windows(scope, query)
    except Exception:
        return []
    out: list[dict] = []
    sid = str(scope.get("id") or "S")
    for index, item in enumerate(made or [], 1):
        if not isinstance(item, dict) or not str(item.get("action") or "").strip():
            continue
        row = dict(item)
        if not str(row.get("id") or "").strip():
            # T1 的补窗 id 形如 SBW1；只兜底无 id 的，带上范围 id 防跨范围撞号
            row["id"] = f"SBW-{sid}-{index}"
        out.append(row)
    return out


def _solver_scope_windows(scope: dict, query: str) -> list[dict]:
    """补窗补不出时，先试受控求解窗（20261028 P0-2）：零窗口 ≠ 缺口。

    UE_SOLVER_ENABLED 缺省 false：开关关着返回 []，行为与今天完全一致
    （check_solve_gap_fallback 钉这条）。开着也只在补窗真的失败后给一扇。"""
    try:
        from agent.solver import solve_window
    except ImportError:
        return []
    except Exception:
        return []
    if not callable(solve_window):
        return []
    try:
        made = solve_window(scope, query)
    except Exception:
        return []
    return [item for item in (made or []) if isinstance(item, dict) and str(item.get("action") or "") == "solve"]


def _solver_on() -> bool:
    """求解开关的统一读口。读不到（包坏了）按关算，不放开路径。"""
    try:
        from agent.solver import solver_enabled

        return bool(solver_enabled())
    except Exception:
        return False


# ---------------------------------------------------------------- 交付项覆盖对账
# 20261028-155214 假成功教训：范围「有窗」≠「交付被覆盖」。载具范围的 5 条交付里
# 4 条是载具，唯一窗口 implement_shooter 只接得住最后一条（射击主体就位），
# 其余 4 条被静默丢弃，集成矩阵还全绿。守卫方向要从「整段零窗口」细化到「逐条交付」。

def _content_bigrams(text: str) -> set[str]:
    """内容字二元组：去掉标点空白后相邻两字一组。离线可算，不引分词依赖。"""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text or ""))
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


# 验收类交付的识别词。命中即认「验收类窗口（run_automation_test / integrate_verify /
# editor_assert）执行了它就是接住了」——见 _uncovered_deliverables 的 20261028 注。
_VERIFY_DELIVERABLE_RE = re.compile(r"检查|验证|复验|结论|确认|实测|测试|跑通")


def _uncovered_deliverables(scope: dict, windows: list[dict], threshold: float = 0.2) -> list[str]:
    """没被任何窗口接住的交付项清单。

    判法刻意保守：拿交付项的字二元组去对窗口文本（title/action/goal/scope_text/slots），
    重叠率 ≥threshold 就算接住——错放行只是退回今天的现状，错拦截会制造假缺口，
    后者的代价更高。窗口文本拿不到（老记录）时按「覆盖」处理，不误伤。

    20261028 补一条（check_implement_shooter 的存量漂移）：验收类交付
    （「给出一份只读检查结论」这类）由验收类窗口（run_automation_test /
    integrate_verify / editor_assert）执行时就是被接住了——它们的裁判产出正是
    那份结论。字二元组对不上不代表没接住；错放行最多是少一扇冗余窗口，
    错拦截会给已绑了验收窗的范围硬记「人审已确认不执行」，口径反而错。"""
    parts: list[str] = []
    verify_only_actions: set[str] = set()
    for win in windows or []:
        if not isinstance(win, dict):
            continue
        action = str(win.get("action") or "").strip()
        if action in ("run_automation_test", "run_automation_tests", "integrate_verify", "editor_assert"):
            verify_only_actions.add(action)
        for key in ("title", "action", "goal", "scope_text"):
            parts.append(str(win.get(key) or ""))
        slots = win.get("slots")
        if isinstance(slots, dict):
            parts.extend(str(value) for value in slots.values())
        # 动作的声明能力文本也算窗口语义：implement_shooter 的 summary 写明了
        # 它只负责 FpsPawn/靶/地面/灯，载具交付对不上它的能力面（20261028-155214）。
        if action:
            try:
                from agent.catalog import spec_of

                spec = spec_of(action)
                if isinstance(spec, dict):
                    parts.append(str(spec.get("summary") or ""))
                    parts.extend(str(item) for item in (spec.get("verify") or []))
            except Exception:
                pass
    bundle = _content_bigrams(" ".join(part for part in parts if part.strip()))
    if not bundle:
        return []
    uncovered: list[str] = []
    for item in scope.get("deliverables") or []:
        grams = _content_bigrams(item)
        if not grams:
            continue
        if len(grams & bundle) / len(grams) >= threshold:
            continue
        if verify_only_actions and _VERIFY_DELIVERABLE_RE.search(str(item)):
            continue  # 验收类交付 + 验收类窗口：这就是被接住，不算缺口
        uncovered.append(str(item))
    return uncovered


# ---------------------------------------------------------------- D6 覆盖预演
# 人审发生在物化之前（[事实] agent/run_windows.py:2458 brief_flow → :2470 materialize_scopes），
# 所以人审那一刻 scope["windows"] 可能本来就是空的——那是模型没给窗口，不是我们没物化。
# 只看声明字段，人审门看到的是「模型写了什么」；人审要判的是「跑起来有没有东西跑」，
# 那是「声明窗口 + 补窗救援」之后的结果。两者差在零窗口范围上，所以人审前做一次预演。

# 缺口显式确认协议（§7 不做清单第 7 条：不引入「缺省回车即通过」）。
# 只有存在能力缺口范围时才生效；正常任务书仍是一句「确认」或直接回车通过（反例守卫）。
GAP_ACK_PHRASE = "确认缺口"
GAP_ACK_ESCALATE_ENV = "UE_ESCALATE_GAPS"
_LEGACY_CONFIRM_WORDS = ("确认", "confirm", "y", "yes", "ok", "通过")
_CONFIRM_WORD_RE = re.compile(r"(?:确认|confirm|yes|ok)", re.I)


def _escalate_gaps() -> bool:
    """缺口是否阻塞人审门。默认开；只有显式写成 false/0/no/off 才旁路（§3.2 允许）。
    [事实] docs/upgrade/next-upgrade-plan.md:204「可用环境变量旁路（UE_ESCALATE_GAPS=false
    时只记录不阻塞）——但不允许它在真实 run 里默认关闭」。"""
    raw = str(os.environ.get(GAP_ACK_ESCALATE_ENV, "") or "").strip().lower()
    return raw not in ("false", "0", "no", "off")


@contextlib.contextmanager
def _no_candidate_writes():
    """dry-run 期间把「记能力候选」这条写盘腿临时掐掉。

    为什么需要：预演走的是 `_rescue_scope_windows` → `agent.plan_windows.scope_windows`，
    里面会 `materialize`/`materialize_goal`；而 `materialize` 在表外动作分支会调
    `note_candidate(...)`（[事实] agent/plan_windows.py:570），后者 `_write_rows` 真写
    `data/skills/action_candidates.json`（[事实] agent/catalog.py:327）。人审前先写盘等于
    「还没确认就先改了项目状态」，而且人审可能被放弃/重出。

    为什么不能用别的办法：`materialize_scopes` 的补窗路径是同一个 `_rescue_scope_windows`，
    但在人审阶段物化会把 `brief["scopes"][i]["windows"]` 真的填上，人审看到的就是物化后的
    任务书 —— 那是另一个对象、另一件事（且候选窗口继承会吃掉 candidates）。这里的职责只是
    「算一算会有几扇窗口」，所以只掐写盘腿。

    为什么安全：`note_candidate` 只往候选表追加/累加命中次数（source="planner"），它不参与
    `allows()` 判定、不改 CATALOG、不影响 `materialize` 的返回值（[事实] agent/catalog.py:329-355
    只读写候选文件；`allows()` 读的是 CATALOG 与 promoted，`catalog.py:368` 的硬门不提候选表）。
    也就是说掐掉它只丢一次「这个表外动作被提过一次」的计数，不改变任何决策。
    而且 `_rescue_scope_windows` 内部调的是模块级 `from agent.plan_windows import scope_windows`，
    那个模块在 import 时就把 `note_candidate` 绑成了局部名（[事实] agent/plan_windows.py:8），
    所以必须**同时**替换 `agent.plan_windows.note_candidate` 与 `agent.catalog.note_candidate`。
    两个名字都在 `finally` 里原样恢复；恢复失败也只可能是 AttributeError，不吞别的异常。"""
    from agent import catalog as catalog_mod

    try:
        from agent import plan_windows as plan_windows_mod
    except Exception:
        plan_windows_mod = None

    saved: list[tuple[object, str, object]] = []
    targets = [("note_candidate", catalog_mod)]
    if plan_windows_mod is not None:
        targets.append(("note_candidate", plan_windows_mod))
    try:
        for name, module in targets:
            if hasattr(module, name):
                saved.append((module, name, getattr(module, name)))
                setattr(module, name, _noop_candidate)
            else:
                saved.append((module, name, None))
                setattr(module, name, _noop_candidate)
        yield
    finally:
        for module, name, original in reversed(saved):
            if original is None:
                try:
                    delattr(module, name)
                except AttributeError:
                    pass
            else:
                setattr(module, name, original)


def _noop_candidate(*_args, **_kwargs):
    """`note_candidate` 的干跑替身：不写盘、不返回记录。"""
    return None


def _planned_scope_windows(scope: dict, query: str, acked: set | None = None) -> list[dict]:
    """预演单个范围的覆盖：模型声明了就照声明，没声明就按同一救援路径补一次，
    补不出且求解开关开着再试一扇 solve 扇（与 materialize_scopes 的判定顺序一致）。
    blocked / 等外部 / 人审已确认缺口的范围不补窗，也不计入缺口。"""
    declared = [raw for raw in (scope.get("windows") or []) if isinstance(raw, dict)]
    if declared:
        return declared
    if scope.get("blocked") or str(scope.get("external_dep") or "").strip():
        return []
    if str(scope.get("id") or "") in set(acked or ()):
        return []
    planned = _rescue_scope_windows(scope, query)
    if planned:
        return planned
    return _solver_scope_windows(scope, query)


def _window_runnable(window: dict) -> bool:
    """这一扇预演窗口是否真能跑。`materialize` 造出来的窗口带 blocked/realizable
    （[事实] agent/plan_windows.py:552-598：表外动作、缺槽位、归地失败都会 blocked=True，
    返回前才 realizable=True）；任务书自己声明的 raw 窗口没有这两个字段，
    按「不知道 = 先算能跑」处理，免得把模型写的窗口一律当成暂停窗。"""
    if not isinstance(window, dict):
        return False
    if window.get("blocked") is True or window.get("realizable") is False:
        return False
    return bool(str(window.get("action") or "").strip())


def preview_coverage(brief: dict) -> dict:
    """人审用的覆盖预演（dry-run，不写盘）：算出「若现在物化，各范围会有几扇窗口」。

    返回 {"scopes": [{"id","title","count","runnable","actions","declared","rescued","gap"}],
          "total": 业务窗口总数, "gaps": [{"id","title","missing_actions","pending_note","note"}]}。
    结果同时写回 `brief["coverage"]`（新增字段，供 run_windows 侧与断言读）。

    `count` 是窗口扇数，`runnable` 是其中真能跑的扇数。两个数分开：
    「补窗补出一扇暂停窗」与「补出可跑窗口」在人审眼里完全不同，前者仍是洞，
    只显示「窗口：1 扇」等于拿一个跑不起来的窗口冒充覆盖。
    """
    query = str(brief.get("query") or "")
    rows: list[dict] = []
    gaps: list[dict] = []
    total = 0
    for scope in brief.get("scopes") or []:
        if not isinstance(scope, dict):
            continue
        declared = [raw for raw in (scope.get("windows") or []) if isinstance(raw, dict)]
        with _no_candidate_writes():
            planned = _planned_scope_windows(scope, query, acked=_acked_gap_ids(brief))
        actions = [str(item.get("action") or "") for item in planned]
        runnable = [item for item in planned if _window_runnable(item)]
        blocked = bool(scope.get("blocked"))
        external = str(scope.get("external_dep") or "").strip()
        # 整段没窗口 ⇒ 缺口；有窗口但一扇都跑不起来且不等外部 ⇒ 同样是缺口
        # （交付清单非空却没有任何可执行路径，这与「0 扇窗口」在人审眼里是同一件事）
        gap = _zero_window_scope({**scope, "windows": planned})
        if not gap and planned and not runnable and not blocked and not external:
            gap = True
        row = {
            "id": str(scope.get("id") or ""),
            "title": str(scope.get("title") or ""),
            "count": len(planned),
            "runnable": len(runnable),
            "actions": [name for name in actions if name],
            "declared": len(declared),
            "rescued": len(planned) - len(declared),
            "gap": bool(gap),
            "blocked": blocked,
            "external_dep": external,
        }
        rows.append(row)
        total += len(planned)
        # 20261028-155214：有窗范围的交付项也要对账，没接住的进人审表态清单。
        # 求解开着时不升级（solve 扇就是缺口的第二条路，不打扰人审），只做提示。
        if not gap and planned and not blocked and not external:
            uncovered = _uncovered_deliverables(scope, planned)
            if uncovered:
                row["uncovered"] = uncovered
                if not _solver_on():
                    gaps.append({
                        "id": row["id"],
                        "title": row["title"],
                        "missing_actions": [],
                        "pending_note": str(scope.get("pending_note") or ""),
                        "partial": True,
                        "deliverables": list(uncovered),
                        "note": "有 " + str(len(uncovered)) + " 条交付项没被任何窗口接住："
                                + "；".join(str(item)[:40] for item in uncovered[:3]),
                    })
        if gap:
            gaps.append({
                "id": row["id"],
                "title": row["title"],
                "missing_actions": _gap_missing_actions(scope),
                "pending_note": str(scope.get("pending_note") or ""),
                "note": capability_gap_note(scope),
            })
    coverage = {"scopes": rows, "total": total, "gaps": gaps, "dry_run": True}
    brief["coverage"] = coverage
    return coverage


def _acked_gap_ids(brief: dict) -> set[str]:
    """人审门已经显式确认「不执行」的范围 id。没确认过就是空集。"""
    if not isinstance(brief, dict):
        return set()
    ack = brief.get("gap_ack")
    if not isinstance(ack, dict) or not ack.get("acknowledged"):
        return set()
    return {str(item) for item in (ack.get("scopes") or []) if str(item).strip()}


def gap_scopes(brief: dict) -> list[dict]:
    """人审门认的缺口范围。优先用预演结果（已算过补窗），没预演就退回声明字段判定。
    [事实] 结构化 capability_gap 只在 materialize_scopes（:906/:997）写，那是人审之后，
    所以人审阶段不能读它。"""
    coverage = brief.get("coverage")
    if isinstance(coverage, dict) and isinstance(coverage.get("gaps"), list):
        return [item for item in coverage["gaps"] if isinstance(item, dict) and item.get("id")]
    return [
        {"id": str(scope.get("id")), "title": str(scope.get("title") or ""),
         "missing_actions": _gap_missing_actions(scope),
         "pending_note": str(scope.get("pending_note") or ""),
         "note": capability_gap_note(scope)}
        for scope in brief.get("scopes") or []
        if isinstance(scope, dict) and _zero_window_scope(scope)
    ]


def _coverage_of(scope: dict, coverage: dict | None) -> dict | None:
    """从预演结果里取这一行的覆盖（集成末行没有对应行，返回 None）。"""
    if not isinstance(coverage, dict):
        return None
    sid = str((scope or {}).get("id") or "")
    if not sid:
        return None
    for row in coverage.get("scopes") or []:
        if str((row or {}).get("id") or "") == sid:
            return row
    return None


def _coverage_text(row: dict | None) -> str:
    """覆盖列的一句话：`窗口：N 扇`，能列出动作就列出来（给人审看能不能真跑）。

    有窗口但一扇都跑不起来时写出 `可跑 0 扇`：否则「窗口：1 扇（no_such_action）」
    在人眼里与「有一扇能跑」无法区分，等于用一个暂停窗冒充覆盖。"""
    if not isinstance(row, dict):
        return ""
    count = int(row.get("count") or 0)
    runnable = int(row.get("runnable", count) or 0)
    text = f"窗口：{count} 扇"
    actions = [str(name) for name in (row.get("actions") or []) if str(name).strip()]
    if actions:
        text += f"（{'、'.join(actions)}）"
    if count and not runnable:
        text += " ※可跑 0 扇（全是暂停窗）"
    if int(row.get("rescued") or 0) > 0:
        text += f" ※含补窗 {int(row['rescued'])} 扇"
    return text


def _review_state(scope: dict, coverage: dict | None) -> str:
    """这一行在人审眼里算什么：""（正常）/ "gap"（真缺口）/ "rescued"（模型没给但补得出）。

    [事实] 有预演结果时以预演为准：B 段这种「模型给了 0 扇、代码按同一路径补得出」的范围
    如果还照着声明字段喊「能力缺口」，人审会同时看到「缺口」和「窗口 1 扇」两个说法，
    比空白更糟。没有预演（直接 render 的任务书文件）则退回声明判定，保持旧口径。
    """
    if isinstance(scope, dict) and scope.get("blocked"):
        return ""
    row = _coverage_of(scope, coverage) if isinstance(coverage, dict) else None
    if row is not None:
        if row.get("gap"):
            return "gap"
        if int(row.get("rescued") or 0) > 0:
            return "rescued"
        return ""
    return "gap" if _zero_window_scope(scope) else ""


def _rescued_note(row: dict) -> str:
    """补窗预告的措辞：说清是预演、没写盘、真补不出来才会变成缺口。"""
    return (
        f"模型没给窗口，预演按同一路径补出 {int(row.get('rescued') or 0)} 扇"
        "（dry-run 未写盘；补不出来才算能力缺口）"
    )


def _gap_ack_prompt(gaps: list[dict]) -> str:
    """缺口确认的提示语：把要人表态的东西逐条摆出来，并写清协议。
    §7 不做清单第 7 条禁止「缺省回车即通过」，所以这里必须明说回车不算。"""
    ids = "、".join(f"{item.get('id')}" for item in gaps) or "?"
    lines = [
        f"存在能力缺口（范围 {ids}）：这些范围有交付清单，但没有一扇能跑的窗口。",
    ]
    for item in gaps:
        if item.get("note"):
            lines.append(f"  · {item.get('id')} {item.get('title') or ''}".rstrip()
                         + "：" + str(item["note"]))
            continue
        missing = [str(name) for name in (item.get("missing_actions") or []) if str(name).strip()]
        lines.append(
            f"  · {item.get('id')} {item.get('title') or ''}".rstrip()
            + "：" + ("缺动作 " + "、".join(missing) if missing else "缺可落地的动作（抠不出动作名）")
        )
    lines.append(
        f"    输入「{GAP_ACK_PHRASE}」= 承认缺口、只跑有窗口的范围（缺的部分不执行）；"
        "带「确认/ok/yes」的其它说法 = 同样承认；"
        "「放弃/q」= 这次不执行。"
    )
    lines.append("    直接回车不算确认（缺口必须显式表态）。")
    return "\n".join(lines)


def _ack_is_explicit(answer: str) -> bool:
    """缺口任务的确认词：必须真的写了确认词（回车、空串都不算）。
    「确认缺口」与「确认」「ok」都认，但必须是敲进来的字。"""
    text = str(answer or "").strip()
    if not text:
        return False
    if text == GAP_ACK_PHRASE:
        return True
    return _CONFIRM_WORD_RE.search(text) is not None


def needs_brief(query: str, windows: list[dict] | None, intent: dict | None = None) -> bool:
    """大任务判定：说了新功能/重构，或拆出的窗口超过 3 扇，或理解器把半句扔进了
    unresolved（20261015-1705：没着落的半句正是「尚未完成的全部主要内容」该记的）。
    窄任务走快路径，不出任务书。"""
    if _BRIEF_KEYWORD_RE.search(query or ""):
        return True
    if isinstance(intent, dict) and intent.get("unresolved"):
        return True
    return len(windows or []) > BRIEF_WINDOW_THRESHOLD


def _retrieval_titles(query: str, k: int = 3) -> list[str]:
    """search_ue_project 同一漏斗的 3 条标题级摘要。只要标题，不进正文。失败就空。"""
    try:
        from core.rag_core import retrieve

        rows = retrieve(query, top_k=k)
    except Exception:
        return []
    titles: list[str] = []
    for row in rows or []:
        meta = row.get("metadata") or {}
        title = str(meta.get("display_name") or meta.get("asset_name") or meta.get("class_name") or "").strip()
        if not title:
            title = str(row.get("content") or "").strip()[:30]
        if title and title not in titles:
            titles.append(title)
    return titles[:k]


def brief_system_prompt() -> str:
    """任务书生成器只看动作表和任务书 schema。工具 schema、检索正文一律不给。"""
    lines = [
        "你只输出 JSON 任务书，不解释。",
        "把用户一句话拆成范围（A、B、C…），每个范围列「尚未完成」的交付条目，最后带一行累计集成。",
        "deliverables 枚举还没完成的内容，包括「代码已有但未接线、未验证」这类现状，写进 pending_note。",
        "台账摘要只用来标出原句里已经做过的部分。原句没提到的台账条目不要写进 scopes，也不要写进 integration。",
        "windows 只能用动作表里的动作和槽位名；看不准就不要编。windows 的 depends_on 只引用先出现的窗口 id。",
        "候选窗口给了 id 时，范围 windows 必须原样引用候选的 id 和 action，不要自己编动作、槽位或路径，也不要漏掉候选窗口。",
        "只能等、不能跑的外部服务写进 external_dependencies。范围等外部服务时，把服务名写进该范围的 external_dep。",
        "estimate_hours 是记录字段，不是执行预算；给不出就不写，代码会按动作表估。免责由代码自动带，不要写。",
        "",
        "输出形状：",
        json.dumps({
            "query": "原句",
            "scopes": [{
                "id": "A",
                "title": "范围名",
                "deliverables": ["尚未完成的条目"],
                "pending_note": "代码已有，缺接线和验证",
                "estimate_hours": [4, 16],
                "depends_on": [],
                "external_dep": "",
                "windows": [{"id": "W1", "title": "这一扇要做完的事", "action": "set_pickable",
                             "slots": {"target": "BP_名字"}, "depends_on": []}],
            }],
            "integration": {"title": "累计集成、验证与交付", "deliverables": ["对修改做一次只读复验"]},
            "external_dependencies": [{"name": "Main/M-UE", "provides": "权威初始化", "status": "服务未恢复"}],
        }, ensure_ascii=False),
        "",
        "动作表：",
    ]
    for name, spec in CATALOG.items():
        if name in (INTEGRATE_ACTION, DISPOSE_ACTION):
            continue
        slots = "、".join(spec["slots"]) if spec["slots"] else "无"
        if spec.get("mode") == "one":
            slots += "（有一个即可）"
        lines.append(f"- {name}。槽位：{slots}。{spec['summary']}")
    return "\n".join(lines)


def brief_user_prompt(query: str, windows: list[dict] | None, ledger_text: str, titles: list[str]) -> str:
    """请求体控制在 1KB 上下：原句 + 台账摘要 + 检索标题 + 候选窗口。"""
    candidates = [
        {"id": str(w.get("id") or ""), "title": str(w.get("title") or ""),
         "action": str(w.get("action") or ""), "slots": w.get("slots") or {}}
        for w in (windows or [])
    ]
    payload = {
        "原句": query,
        "台账摘要": ledger_text,
        "检索标题": titles,
        "候选窗口": candidates,
    }
    return json.dumps(payload, ensure_ascii=False)


def parse_brief_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("scopes"), list) or not data["scopes"]:
        return None
    return data


def _clamp_hours(value) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        pair = (float(value[0]), float(value[1]))
    except (TypeError, ValueError):
        return None
    low, high = min(pair), max(pair)
    if low <= 0 or high > 999:
        return None
    return [max(0.1, low), min(999.0, high)]


def compute_estimate(scope: dict) -> list[float]:
    """代码侧估算：按窗口动作类型给区间；没有窗口就按交付条目数。只进文档。"""
    windows = scope.get("windows") or []
    if windows:
        low = high = 0.0
        for window in windows:
            pair = _ACTION_ESTIMATE.get(str(window.get("action") or "")) or (1.0, 4.0)
            low += pair[0]
            high += pair[1]
        return [round(low, 1), round(high, 1)]
    count = max(1, len(scope.get("deliverables") or []))
    return [round(1.0 * count, 1), round(4.0 * count, 1)]


def validate_brief(data: dict, query: str) -> dict:
    """schema 由代码验收：未知字段丢弃、估时超界丢弃改用代码估算、交付清单为空的范围暂停。"""
    dropped: list[str] = []
    brief: dict = {"query": str(data.get("query") or query)}
    scopes: list[dict] = []
    seen: set[str] = set()
    for raw in data.get("scopes") or []:
        if not isinstance(raw, dict):
            dropped.append("一条范围不是对象，已丢弃")
            continue
        unknown = [k for k in raw if k not in _SCOPE_KEYS]
        if unknown:
            dropped.append("范围 " + str(raw.get("id") or "?") + " 丢弃未知字段：" + "、".join(sorted(set(map(str, unknown)))))
        sid = str(raw.get("id") or "").strip()
        if not sid:
            dropped.append("无 id 的范围已丢弃")
            continue
        if sid in seen:
            dropped.append(f"范围 {sid} 重复，后一条已丢弃")
            continue
        seen.add(sid)
        deliverables = [str(x).strip() for x in raw.get("deliverables") or [] if str(x).strip()]
        raw_windows = raw.get("windows") if isinstance(raw.get("windows"), list) else []
        wins: list[dict] = []
        for rw in raw_windows:
            if not isinstance(rw, dict):
                dropped.append(f"范围 {sid} 有一条窗口不是对象，已丢弃")
                continue
            unknown_w = [k for k in rw if k not in _WINDOW_KEYS]
            if unknown_w:
                dropped.append("窗口 " + str(rw.get("id") or rw.get("action") or "?") + " 丢弃未知字段：" + "、".join(sorted(set(map(str, unknown_w)))))
            try:
                retries = max(0, int(rw.get("max_retries") or 1))
            except (TypeError, ValueError):
                retries = 1
            wins.append({
                "id": str(rw.get("id") or "").strip(),
                "title": str(rw.get("title") or "").strip(),
                "action": str(rw.get("action") or "").strip(),
                "slots": rw.get("slots") if isinstance(rw.get("slots"), dict) else {},
                "depends_on": [str(x) for x in rw.get("depends_on") or [] if str(x).strip()],
                "max_retries": retries,
            })
        scope = {
            "id": sid,
            "title": str(raw.get("title") or "").strip(),
            "deliverables": deliverables,
            "pending_note": str(raw.get("pending_note") or "").strip(),
            "depends_on": [str(x) for x in raw.get("depends_on") or [] if str(x).strip()],
            "external_dep": str(raw.get("external_dep") or "").strip(),
            "windows": wins,
        }
        hours = _clamp_hours(raw.get("estimate_hours"))
        scope["estimate_hours"] = hours if hours is not None else compute_estimate(scope)
        if not deliverables:
            scope["blocked"] = True
            scope["block_note"] = "交付清单为空，这一范围暂停，不执行它的窗口。"
        if not wins and not scope.get("blocked"):
            scope["block_note"] = _GAP_NOTE
        scopes.append(scope)
    brief["scopes"] = scopes

    raw_integration = data.get("integration")
    integration: dict = {}
    if isinstance(raw_integration, dict):
        unknown_i = [k for k in raw_integration if k not in _INTEGRATION_KEYS]
        if unknown_i:
            dropped.append("集成行丢弃未知字段：" + "、".join(sorted(set(map(str, unknown_i)))))
        integration = {
            "title": str(raw_integration.get("title") or "累计集成、验证与交付").strip(),
            "deliverables": [str(x).strip() for x in raw_integration.get("deliverables") or [] if str(x).strip()],
        }
        hours = _clamp_hours(raw_integration.get("estimate_hours"))
        integration["estimate_hours"] = hours if hours is not None else compute_estimate({"windows": [{"action": INTEGRATE_ACTION}]})
    brief["integration"] = integration

    ext: list[dict] = []
    for raw_ext in data.get("external_dependencies") or []:
        if not isinstance(raw_ext, dict):
            dropped.append("一条外部依赖不是对象，已丢弃")
            continue
        unknown_e = [k for k in raw_ext if k not in _EXT_KEYS]
        if unknown_e:
            dropped.append("外部依赖 " + str(raw_ext.get("name") or "?") + " 丢弃未知字段：" + "、".join(sorted(set(map(str, unknown_e)))))
        name = str(raw_ext.get("name") or "").strip()
        if not name:
            dropped.append("无名的外部依赖已丢弃")
            continue
        ext.append({
            "name": name,
            "provides": str(raw_ext.get("provides") or "").strip(),
            "status": str(raw_ext.get("status") or "未登记").strip(),
        })
    brief["external_dependencies"] = ext

    low = sum(float(scope["estimate_hours"][0]) for scope in scopes)
    high = sum(float(scope["estimate_hours"][1]) for scope in scopes)
    if integration:
        low += float(integration["estimate_hours"][0])
        high += float(integration["estimate_hours"][1])
    total = data.get("total_estimate")
    if isinstance(total, dict):
        clamp = _clamp_hours([total.get("min_hours"), total.get("max_hours")])
        if clamp is not None:
            brief["total_estimate"] = {"min_hours": clamp[0], "max_hours": clamp[1]}
    brief.setdefault("total_estimate", {"min_hours": round(low, 1), "max_hours": round(high, 1)})
    brief["disclaimer"] = list(_DISCLAIMER)
    brief["dropped"] = dropped
    return brief


_SCOPE_CITE_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z])(?![A-Za-z0-9_])")


def _cited_scope_ids(text: str) -> list[str]:
    found: list[str] = []
    for match in _SCOPE_CITE_RE.finditer(text or ""):
        sid = match.group(1)
        if sid not in found:
            found.append(sid)
    return found


def _remap_scope_ids(text: str, mapping: dict[str, str]) -> str:
    def repl(match):
        old = match.group(1)
        if old not in mapping:
            return match.group(0)
        return "\u0000" + mapping[old]

    return _SCOPE_CITE_RE.sub(repl, text or "").replace("\u0000", "")


def _recompute_total(brief: dict) -> None:
    low = high = 0.0
    for scope in brief.get("scopes") or []:
        hours = scope.get("estimate_hours") or [0, 0]
        low += float(hours[0])
        high += float(hours[1])
    hours = (brief.get("integration") or {}).get("estimate_hours") or []
    if len(hours) == 2:
        low += float(hours[0])
        high += float(hours[1])
    brief["total_estimate"] = {"min_hours": round(low, 1), "max_hours": round(high, 1)}


def strip_unrelated_carryover(brief: dict, query: str, ledger_path: Path | None = None) -> dict:
    """原句没提到的台账条目，模型若照抄进范围，这里丢掉并重新编号。"""
    from agent.ledger import overlap_size, read_ledger, same_subject, scope_text

    unrelated = []
    for old in read_ledger(ledger_path).get("scopes") or []:
        if isinstance(old, dict) and old.get("id") and not same_subject(scope_text(old), query):
            unrelated.append(old)
    if not unrelated:
        return brief
    kept = []
    dropped_ids: list[str] = []
    notes = list(brief.get("dropped") or [])
    for scope in brief.get("scopes") or []:
        blob = scope_text(scope)
        if same_subject(blob, query):
            kept.append(scope)
            continue
        matched = None
        matched_n = 0
        for old in unrelated:
            score = overlap_size(blob, scope_text(old))
            if score > matched_n:
                matched = old
                matched_n = score
        if matched is None:
            kept.append(scope)
            continue
        sid = str(scope.get("id") or "")
        dropped_ids.append(sid)
        notes.append(
            f"范围 {sid}「{scope.get('title') or sid}」沿用了台账里原句没提到的"
            f"「{matched.get('title') or matched.get('id')}」，已从本次任务书去掉"
        )
    if not dropped_ids:
        return brief
    integration = brief.get("integration") if isinstance(brief.get("integration"), dict) else {}
    deliverables: list[str] = []
    for item in integration.get("deliverables") or []:
        text = str(item)
        cited = _cited_scope_ids(text)
        if cited and all(sid in dropped_ids for sid in cited):
            notes.append(f"集成条目只引用已去掉的范围 {'、'.join(cited)}，已去掉：{text}")
            continue
        deliverables.append(text)
    dropped_wids = {
        str(window.get("id"))
        for scope in brief.get("scopes") or []
        if str(scope.get("id") or "") in dropped_ids
        for window in scope.get("windows") or []
        if window.get("id")
    }
    mapping: dict[str, str] = {}
    for index, scope in enumerate(kept):
        old = str(scope.get("id") or "")
        mapping[old] = chr(ord("A") + index) if index < 26 else old
    for scope in kept:
        for window in scope.get("windows") or []:
            window["depends_on"] = [
                str(dep) for dep in (window.get("depends_on") or []) if str(dep) not in dropped_wids
            ]
        scope["depends_on"] = [
            mapping[str(dep)] for dep in (scope.get("depends_on") or []) if str(dep) in mapping
        ]
        if scope.get("pending_note"):
            scope["pending_note"] = _remap_scope_ids(str(scope["pending_note"]), mapping)
        old_id = str(scope.get("id") or "")
        scope["id"] = mapping.get(old_id, old_id)
    if integration:
        integration["deliverables"] = [_remap_scope_ids(item, mapping) for item in deliverables]
        brief["integration"] = integration
    for item in brief.get("external_dependencies") or []:
        if not isinstance(item, dict):
            continue
        for key in ("status", "provides"):
            if item.get(key):
                item[key] = _remap_scope_ids(str(item[key]), mapping)
    brief["scopes"] = kept
    brief["dropped"] = notes
    _recompute_total(brief)
    return brief


def _unresolved_list(unresolved) -> list[str]:
    """理解器的 unresolved → 任务书要落的字符串列表。非列表、空串都丢掉，不做补全。"""
    if not isinstance(unresolved, (list, tuple)):
        return []
    out: list[str] = []
    for item in unresolved:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def generate_brief(query: str, windows: list[dict] | None = None, complete=None,
                   feedback: str = "", ledger_text: str | None = None,
                   unresolved=None) -> dict | None:
    """生成器只看：原句、动作表、任务书 schema、台账摘要、3 条检索标题、候选窗口。

    D9：`unresolved`（理解器扔掉的原句半句）原样写进 `brief["unresolved"]`，
    render_brief_md 的「尚未完成清单」据此渲染。**不传时行为与今天完全一致**
    （不带这个键，章节不渲染）。[事实] 20261024-192858 的任务书 md 只有 21 行、
    没有这个章节，因为 brief_flow 不接收 intent、generate_brief 签名里没有 unresolved。"""
    from agent.plan_windows import _complete_live

    try:
        from agent.cli_log import status

        status("正在写任务书")
    except Exception:
        pass
    titles = _retrieval_titles(query)
    ledger = ledger_text if ledger_text is not None else ledger_summary_text(query=query)
    user = brief_user_prompt(query, windows, ledger, titles)
    if feedback:
        user += "\n上一轮审阅意见（据此修改）：" + str(feedback)[:600]
    messages = [
        {"role": "system", "content": brief_system_prompt()},
        {"role": "user", "content": user},
    ]
    try:
        text = complete(messages) if complete is not None else _complete_live(messages)
    except Exception:
        return None
    data = parse_brief_json(text)
    if data is None:
        return None
    brief = strip_unrelated_carryover(validate_brief(data, query), query)
    pending = _unresolved_list(unresolved)
    if pending:
        brief["unresolved"] = pending
    return brief


def _scope_coverage_row(scope: dict, coverage: dict | None) -> dict | None:
    """这一行该显示的覆盖。有预演结果就用预演（已含补窗），否则退回声明窗口。
    集成末行（无 id）人审时还没有 integrate_verify，不画覆盖，免得编一个数出来。"""
    row = _coverage_of(scope, coverage)
    if row is not None:
        return row
    if not str((scope or {}).get("id") or "").strip():
        return None
    declared = [raw for raw in (scope.get("windows") or []) if isinstance(raw, dict)]
    return {
        "id": str(scope.get("id")),
        "count": len(declared),
        "actions": [str(item.get("action") or "") for item in declared if str(item.get("action") or "")],
        "declared": len(declared),
        "rescued": 0,
    }


def render_brief_md(brief: dict) -> str:
    """渲染成模版样式：三列表 + 依赖 + 外部依赖 + 总工作量与免责。

    D6：表从三列变四列（多一列「窗口覆盖」），原三列语义不动。**只有存在能力缺口范围时
    才加这一列与缺口小节**：覆盖齐全的任务书必须与旧版逐字一致（反例守卫），
    不能给正常任务加噪音。
    """
    coverage = brief.get("coverage") if isinstance(brief.get("coverage"), dict) else None
    gaps = gap_scopes(brief)
    show_coverage = bool(gaps)
    lines: list[str] = ["# 任务书" + (" " + str(brief["stamp"]) if brief.get("stamp") else ""), ""]
    lines.append(f"- 原句：{brief.get('query')}")
    if brief.get("json_path"):
        lines.append(f"- JSON：{brief['json_path']}")
    review = brief.get("review") or {}
    if review.get("rounds"):
        lines.append(f"- 审阅：{review['rounds']}（{review.get('status') or '待确认'}）")
    lines.append("")
    if show_coverage:
        lines.append("| 任务范围 | 尚未完成的全部主要内容 | 估时区间（小时） | 窗口覆盖 |")
        lines.append("| --- | --- | --- | --- |")
    else:
        lines.append("| 任务范围 | 尚未完成的全部主要内容 | 估时区间（小时） |")
        lines.append("| --- | --- | --- |")
    for scope in brief.get("scopes") or []:
        items = "<br>".join("- " + d for d in scope.get("deliverables") or []) or "（空）"
        if scope.get("pending_note"):
            items += f"<br>现状：{scope['pending_note']}"
        state = _review_state(scope, coverage)
        if scope.get("blocked"):
            items += f"<br>※ {scope.get('block_note') or '这一范围暂停'}"
        elif state == "gap":
            items += f"<br>※ {capability_gap_note(scope)}"
        deps = "、".join(scope.get("depends_on") or []) or "无"
        items += f"<br>依赖：{deps}"
        if scope.get("external_dep"):
            items += f"<br>等外部：{scope['external_dep']}"
        hours = scope.get("estimate_hours") or [0, 0]
        row = f"| {scope['id']} {scope.get('title') or ''} | {items} | {hours[0]:g}–{hours[1]:g} |"
        if show_coverage:
            # 零窗口范围写「窗口：0 扇」，与「一切正常」的空白区分开
            cover = _coverage_text(_scope_coverage_row(scope, coverage)) or "窗口：0 扇"
            if state == "rescued":
                cover += " ※" + _rescued_note(_scope_coverage_row(scope, coverage) or {})
            row += f" {cover} |"
        lines.append(row)
    integration = brief.get("integration") or {}
    if integration:
        items = "<br>".join("- " + d for d in integration.get("deliverables") or [])
        hours = integration.get("estimate_hours") or [0, 0]
        row = f"| （末行）{integration.get('title') or '累计集成、验证与交付'} | {items} | {hours[0]:g}–{hours[1]:g} |"
        if show_coverage:
            row += " 物化后追加 |"
        lines.append(row)
    if show_coverage:
        lines += ["", "## 能力缺口（人审必须逐条表态）", ""]
        for gap in gaps:
            missing = [str(name) for name in (gap.get("missing_actions") or []) if str(name).strip()]
            row = _coverage_of({"id": gap.get("id")}, coverage) or {}
            count = int(row.get("count") or 0)
            runnable = int(row.get("runnable", count) or 0)
            line = f"- 范围 {gap.get('id')}「{gap.get('title') or ''}」："
            line += f"窗口：{count} 扇，可跑 {runnable} 扇，" if count else "窗口：0 扇，"
            line += ("缺动作 " + "、".join(missing)) if missing else "缺可落地的动作（抠不出动作名）"
            if gap.get("pending_note"):
                line += f"。现状：{str(gap['pending_note'])[:120]}"
            lines.append(line)
        lines.append("")
        lines.append(
            "- 这些范围有交付清单却没有可跑的窗口：确认后只跑有窗口的范围，缺口部分不执行。"
            f"人审门必须输入「{GAP_ACK_PHRASE}」或明确确认词（直接回车不算）。"
        )
    lines += ["", "## 外部依赖（目前依赖）", ""]
    ext = brief.get("external_dependencies") or []
    if ext:
        for item in ext:
            lines.append(f"- {item.get('name')}：{item.get('provides') or ''}。状态：{item.get('status') or '未登记'}")
    else:
        lines.append("- 无")
    total = brief.get("total_estimate") or {"min_hours": 0, "max_hours": 0}
    lines += [
        "",
        "## 总工作量",
        "",
        f"- 合计：{total.get('min_hours', 0):g}–{total.get('max_hours', 0):g} 小时（估算不作为执行预算）",
        "- 免责：" + "；".join(_DISCLAIMER) + "。",
    ]
    if brief.get("dropped"):
        lines += ["", "## 代码验收丢弃的内容", ""]
        lines.extend("- " + str(x) for x in brief["dropped"])
    pending = unfinished_text(
        unresolved=brief.get("unresolved") if isinstance(brief.get("unresolved"), list) else None,
        query=str(brief.get("query") or ""),
    )
    if pending:
        lines += ["", "## 尚未完成清单", "", pending]
    return "\n".join(lines) + "\n"


def unfinished_text(ledger_path: Path | None = None, unresolved=None, query: str | None = None) -> str:
    """台账范围、未提升技能、unresolved 合成尚未完成清单。人不审不执行。

    带了 query 时，台账里和这句话不是同一件事的范围不列进这份任务书。
    """
    from agent.ledger import read_ledger, same_subject, scope_text

    bits = []
    try:
        data = read_ledger(ledger_path)
    except Exception:
        data = {"scopes": []}
    for scope in data.get("scopes") or []:
        if not isinstance(scope, dict) or not scope.get("id"):
            continue
        if query and not same_subject(scope_text(scope), query):
            continue
        status = str(scope.get("status") or "pending")
        label = {"done": "完成", "partial": "部分完成", "pending": "未完成"}.get(status, status)
        title = str(scope.get("title") or "").strip()
        piece = f"{scope.get('id')} {label}"
        if title:
            piece += f"（{title}）"
        bits.append(piece)
    lines = []
    if bits:
        lines.append("；".join(bits))
    try:
        from agent.skill_memory import unfinished_skills

        for skill in unfinished_skills():
            lines.append(f"技能 {skill.get('id')} 尚未提升")
    except Exception:
        pass
    for item in unresolved or []:
        text = str(item or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def write_brief(brief: dict, directory: Path | None = None) -> tuple[Path, Path]:
    stamp = str(brief.get("stamp") or datetime.now().strftime("%Y%m%d-%H%M%S"))
    slug = re.sub(r"[^0-9A-Za-z_-]+", "-", str(brief.get("query") or ""))[:24].strip("-") or "brief"
    folder = Path(directory) if directory is not None else BRIEFS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    md = folder / f"{stamp}-{slug}.md"
    js = folder / f"{stamp}-{slug}.json"
    brief.update(stamp=stamp, json_path=str(js))
    js.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    md.write_text(render_brief_md(brief), encoding="utf-8")
    return md, js


_EDIT_HELP = (
    "就地编辑（每条改完立即保存进同一对 .md/.json，元任务是 INT 一行）：\n"
    "  e B.title 新标题        改范围标题\n"
    "  e B.pending 现状说明     改 pending_note（现状）\n"
    "  e B.del+ 新交付条目     加一条「尚未完成」交付\n"
    "  e B.del- 2              删第 2 条交付\n"
    "  e B.est 2 8             改估时区间（小时）\n"
    "  e B.dep A,C             改范围依赖，留空(e B.dep) = 无\n"
    "  e INT.title 新末行       改累计集成行的标题\n"
    "  e INT.del+ 新交付条目    改累计集成行的交付\n"
    "想大改范围结构或窗口，就写一句修改意见交给生成器；手改只动文字不动窗口。"
)
_EDIT_KEY_RE = re.compile(r"^e\s+([A-Za-z]\w*)\.([A-Za-z+-]+)\s*(.*)$")


def _edit_target(brief: dict, tid: str) -> dict | None:
    if tid.upper() == "INT":
        return brief.get("integration") or None
    for scope in brief.get("scopes") or []:
        if str(scope.get("id") or "") == tid:
            return scope
    return None


def _refresh_total(brief: dict) -> None:
    """手改估时或交付后合计跟着变。手改不再走 validate 的重算。"""
    low = high = 0.0
    for scope in (brief.get("scopes") or []) + [brief.get("integration") or {}]:
        hours = scope.get("estimate_hours") or [0, 0]
        low += float(hours[0])
        high += float(hours[1])
    brief["total_estimate"] = {"min_hours": round(low, 1), "max_hours": round(high, 1)}


def _apply_manual_edit(brief: dict, answer: str) -> tuple[bool, str]:
    """e 命令的就地编辑。成功即改在 brief 上（落盘由调用方做），返回 (ok, note)。
    窗口与动作不在手改范围——那要过动作表和槽位校验，写修改意见走生成器。"""
    match = _EDIT_KEY_RE.match((answer or "").strip())
    if not match:
        return False, "写法：e <范围>.<字段> 内容，如 e B.title 接线逻辑。输入 e help 看全部写法。"
    tid, key, value = match.group(1), match.group(2).lower(), match.group(3).strip()
    target = _edit_target(brief, tid)
    if target is None:
        ids = [str(s.get("id")) for s in brief.get("scopes") or []]
        if brief.get("integration"):
            ids.append("INT")
        return False, f"没有范围 {tid}。可用：" + "、".join(ids)
    if key == "title":
        if not value:
            return False, "标题不能为空。"
        target["title"] = value
        return True, f"{tid}.title 已改。"
    if key == "pending":
        target["pending_note"] = value
        return True, f"{tid}.pending 已改。"
    if key in ("del+", "del-"):
        items = list(target.get("deliverables") or [])
        if key == "del+":
            if not value:
                return False, "要写上新条目内容，如 e B.del+ 验证受击表现。"
            items.append(value)
        else:
            if not value.isdigit() or not items or not (1 <= int(value) <= len(items)):
                return False, f"要删第几条：e {tid}.del- 1 到 {len(items)}。"
            items = items[: int(value) - 1] + items[int(value):]
        target["deliverables"] = items
        return True, f"{tid} 交付现在 {len(items)} 条。"
    if key == "est":
        nums = []
        for piece in value.split():
            try:
                nums.append(float(piece))
            except ValueError:
                nums.append(-1.0)
        if len(nums) != 2 or min(nums) < 0 or nums[0] > nums[1]:
            return False, "估时写两个数：e " + tid + ".est 2 8"
        target["estimate_hours"] = [round(nums[0], 1), round(nums[1], 1)]
        _refresh_total(brief)
        return True, f"{tid}.est 已改。"
    if key == "dep":
        if tid.upper() == "INT":
            return False, "末行不接依赖。"
        wanted = [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]
        known = {str(s.get("id")) for s in brief.get("scopes") or []}
        bad = [x for x in wanted if x not in known or x == tid]
        if bad:
            return False, f"依赖里不认识 {('、'.join(bad))}，或指了自己。可用：" + "、".join(sorted(known - {tid}))
        target["depends_on"] = wanted
        return True, (f"{tid}.dep 已改：" + ("、".join(wanted) if wanted else "无"))
    return False, "字段只有 title / pending / del+ / del- / est / dep。输入 e help 看写法。"


def _print_brief_pretty(brief: dict, printer=print) -> None:
    """人审门里给眼睛看的表。文件里落的仍是 render_brief_md 的 markdown。

    D6：有缺口范围时每行都显式写「窗口：N 扇」（零窗口写「窗口：0 扇」+ 能力缺口），
    空白的行与「一切正常」在人眼里本来无法区分。覆盖齐全的任务书仍走旧版画法
    （只在有窗口时画「窗口：…」行），逐字一致。
    """
    coverage = brief.get("coverage") if isinstance(brief.get("coverage"), dict) else None
    gaps = gap_scopes(brief)
    show_coverage = bool(gaps)
    try:
        from rich.console import Console
        from rich.table import Table

        table = Table(title="任务书·人审门", show_lines=True, expand=True)
        table.add_column("范围", no_wrap=True, style="bold cyan")
        table.add_column("尚未完成的全部主要内容", ratio=1)
        table.add_column("估时(小时)", no_wrap=True, justify="right")

        def _cell(scope: dict) -> str:
            bits = ["• " + item for item in scope.get("deliverables") or []] or ["（空）"]
            if scope.get("pending_note"):
                bits.append(f"[dim]现状：{scope['pending_note']}[/dim]")
            state = _review_state(scope, coverage) if show_coverage else (
                "gap" if _zero_window_scope(scope) else ""
            )
            if scope.get("blocked"):
                bits.append(f"[yellow]※ {scope.get('block_note') or '这一范围暂停'}[/yellow]")
            elif state == "gap":
                bits.append(f"[yellow]※ {capability_gap_note(scope)}[/yellow]")
            deps = "、".join(scope.get("depends_on") or []) or "无"
            bits.append(f"[dim]依赖：{deps}[/dim]")
            if scope.get("external_dep"):
                bits.append(f"[yellow]等外部：{scope['external_dep']}[/yellow]")
            wins = scope.get("windows") or []
            if wins:
                marks = "、".join(f"{w.get('id') or ''} {w.get('action') or ''}".strip() for w in wins)
                bits.append(f"[dim]窗口：{marks}[/dim]")
            row = _scope_coverage_row(scope, coverage) if show_coverage else None
            if row is not None:
                text = _coverage_text(row) or "窗口：0 扇"
                style = "yellow" if not int(row.get("count") or 0) else "dim"
                bits.append(f"[{style}]{text}[/{style}]")
                if state == "rescued":
                    bits.append(f"[cyan]{_rescued_note(row)}[/cyan]")
            return "\n".join(bits)

        for scope in brief.get("scopes") or []:
            hours = scope.get("estimate_hours") or [0, 0]
            table.add_row(f"{scope['id']} {scope.get('title') or ''}", _cell(scope), f"{hours[0]:g}–{hours[1]:g}")
        integration = brief.get("integration") or {}
        if integration:
            hours = integration.get("estimate_hours") or [0, 0]
            table.add_row(f"[INT] {integration.get('title') or '累计集成、验证与交付'}",
                          _cell(integration), f"{hours[0]:g}–{hours[1]:g}")
        Console().print(table)
    except Exception:
        printer(render_brief_md(brief))
        return
    ext = brief.get("external_dependencies") or []
    total = brief.get("total_estimate") or {"min_hours": 0, "max_hours": 0}
    printer(f"外部依赖：{'；'.join(f"{x.get('name')}（{x.get('status') or '未登记'}）" for x in ext) or '无'}")
    printer(f"合计：{total.get('min_hours', 0):g}–{total.get('max_hours', 0):g} 小时（估算只记录，不作为执行预算）")
    if show_coverage and _escalate_gaps():
        # 缺口必须在人审门里出声，且说清「怎么才算表态」——否则人会照着旧习惯直接回车
        printer(_gap_ack_prompt(gaps))


def brief_flow(query: str, windows: list[dict] | None = None, complete=None,
               reader=input, printer=print, directory: Path | None = None, unresolved=None):
    """生成任务书并走人审门。确认键是人按；e 命令就地编辑并实时落盘同一对文件。
    返回 (status, brief, md, js)；status ∈ confirm / abort。生成失败返回 None（调用方按窄任务继续）。

    D6/D9：① 显示给人之前先做一次 dry-run 覆盖预演（不写盘），人审看得到每范围的窗口数；
    ② `unresolved` 透传进任务书（「尚未完成清单」章节），不传时行为与旧版完全一致；
    ③ 存在能力缺口范围时空输入不再等于确认（§7 不做清单第 7 条），必须显式表态，
       表态结果写进 `brief["gap_ack"]`。"""
    brief = generate_brief(query, windows=windows, complete=complete, unresolved=unresolved)
    if brief is None:
        printer("写不出任务书：生成器没有给出可验收的 JSON。这次按窄任务直接执行。")
        return None
    rounds = 0
    edits = 0
    md = None
    js = None
    while True:
        if md is None:
            rounds += 1
            brief["review"] = {"rounds": rounds, "status": "待确认"}
            # 人审前预演：物化之后各范围会有几扇窗口。必须不写盘（见 _no_candidate_writes）。
            preview_coverage(brief)
            md, js = write_brief(brief, directory)
        printer(f"任务书第 {rounds} 版：{md}")
        _print_brief_pretty(brief, printer)
        if edits:
            printer(f"人工编辑 {edits} 次，已实时保存进上面这对 .md/.json。")
        gaps = gap_scopes(brief)
        escalate = bool(gaps) and _escalate_gaps()
        # 缺口说明已在上面 _print_brief_pretty 里逐条摆过（见 _gap_ack_prompt），
        # 这里只给一行提示，不重复第二遍整段。
        try:
            answer = str(reader(
                "任务书审阅（输入「" + GAP_ACK_PHRASE + "」或确认词才算表态 / 放弃 / "
                "e 就地编辑，e help 看写法 / 写修改意见交生成器）: "
                if escalate else
                "任务书审阅（确认 / e 就地编辑，e help 看写法 / 写修改意见交生成器 / 放弃）: "
            )).strip()
        except (EOFError, KeyboardInterrupt):
            printer("\n放弃")
            return ("abort", brief, md, js)
        if escalate and not answer:
            # 缺口 + 空输入：不是确认。只记录「问过一次但没表态」，继续循环。
            printer(
                "存在能力缺口，直接回车不算确认：请显式输入「" + GAP_ACK_PHRASE
                + "」或确认词（放弃就输入「放弃」）。"
            )
            continue
        if not answer or answer in _LEGACY_CONFIRM_WORDS or answer == GAP_ACK_PHRASE:
            # 缺口任务书：只有敲进来的确认词才算（`_ack_is_explicit` 对空串返回 False）。
            # 正常任务书仍是「空输入 = 通过」，与旧版一致（反例守卫）。
            if escalate and not _ack_is_explicit(answer):
                continue
            brief["review"]["status"] = "通过" if (rounds == 1 and not edits) else "通过，附修改"
            brief["manual_edits"] = edits
            if gaps:
                # 结构化表态：run_windows 侧与断言都读它。旁路时也记，只是不阻塞。
                brief["gap_ack"] = {
                    "acknowledged": True,
                    "scopes": [str(item.get("id")) for item in gaps],
                    "by": "人",
                    "answer": answer,
                    "escalated": escalate,
                    "acknowledged_at": datetime.now().isoformat(timespec="seconds"),
                }
            write_brief(brief, directory)
            return ("confirm", brief, md, js)
        if answer in ("放弃", "abort", "q", "quit", "停止"):
            if gaps:
                brief["gap_ack"] = {
                    "acknowledged": False,
                    "scopes": [str(item.get("id")) for item in gaps],
                    "by": "人",
                    "answer": answer,
                    "escalated": escalate,
                    "acknowledged_at": datetime.now().isoformat(timespec="seconds"),
                }
            return ("abort", brief, md, js)
        if answer in ("e", "e help"):
            printer(_EDIT_HELP)
            continue
        if answer.split()[0] == "e":
            ok, note = _apply_manual_edit(brief, answer)
            if not ok:
                printer(note)
                continue
            edits += 1
            brief["review"] = {"rounds": rounds, "status": "待确认"}
            write_brief(brief, directory)
            printer(f"已保存：{note}")
            continue
        revised = generate_brief(query, windows=windows, complete=complete, feedback=answer)
        if revised is None:
            printer("按意见重出失败，回到上一版继续审。")
            continue
        brief = revised
        md = None


def materialize_scopes(brief: dict, candidates: list[dict] | None = None, complete=None,
                       titles=None) -> tuple[list[dict], list[dict]]:
    """确认后把范围物化成窗口。范围窗口引用了候选窗口 id 的，原样继承候选
    （快路径已解析的路径、父类、task 一并保留，不叫生成器重编）；引用不上才
    按动作表 materialize，表外照旧暂停。asset 槽位动作统一走归地 materialize_goal
    （20261015-1705 P2）：索引没有但父类可推时，自动插「创建蓝图」前置扇，前置扇
    id 用本扇 id 加 c 后缀，依赖反向挂到本扇。任务书没归类的候选窗口不丢，按原顺序补跑。
    依赖换算分两遍：候选可能排到最后才补跑，先把新旧 id 对照表填全，再统一
    重映射并禁止依赖指向自己，避免撞出自环（2026-10-15 环：W2→W2 修的）。
    等外部的范围标 waiting_external；末尾由代码追加一扇 integrate_verify。
    返回 (windows, scopes_meta)。scopes_meta 供集成报告与台账用。"""
    from agent.plan_windows import materialize, materialize_goal

    candidates = [cand for cand in (candidates or []) if isinstance(cand, dict)]
    used: list[dict] = []
    windows: list[dict] = []
    scopes_meta: list[dict] = []
    old_to_new: dict[str, str] = {}
    counter = 0
    raw_scopes = list(brief.get("scopes") or [])
    win_by_id: dict[str, dict] = {}
    leftover_ids: list[str] = []
    leftover_titles: list[str] = []
    from agent.plan_windows import _intent_query_paths

    origin_paths = _intent_query_paths(str(brief.get("query") or ""))

    def _match_candidate(raw: dict) -> dict | None:
        rid = str(raw.get("id") or "").strip()
        act = str(raw.get("action") or "").strip()
        if rid:
            for cand in candidates:
                if cand not in used and str(cand.get("id") or "") == rid:
                    return cand
        if act:
            same = [
                cand for cand in candidates
                if cand not in used and str(cand.get("action") or "") == act
            ]
            if len(same) == 1:
                return same[0]
        return None

    # 第一遍：定下每扇的新 id 和它引用的候选，把新旧 id 对照表先填全。
    # 候选可能排到最后才被补跑，提前换算会把还没进表的候选 id 错当成新 id，撞出自环。
    plan: list[tuple[int, dict | None, dict | None, str]] = []
    scope_wids: dict[int, list[str]] = {}
    query = str(brief.get("query") or "")
    declared_windows: dict[int, list[dict]] = {}
    solve_windows: dict[int, list[dict]] = {}
    scope_gaps: dict[int, dict] = {}
    # 人审已经确认「这段不执行」的范围。预演补窗不带模型；这里再把 complete 传进去，
    # 会在确认之后另拆出任务书里没有的窗口（20261027-142031：拆出没有 name 的
    # query_actor，执行器只能拿 /Game 目录或头文件来问）。确认过的范围只走预演那条。
    acked_gaps = _acked_gap_ids(brief)
    zero_needs: list[tuple[int, str, dict]] = []
    for si, scope in enumerate(raw_scopes):
        declared = [raw for raw in (scope.get("windows") or []) if isinstance(raw, dict)]
        solve_by_scope: dict[int, list[dict]] = {}
        if not declared and not scope.get("blocked") and not str(scope.get("external_dep") or "").strip():
            # 零窗口范围先补救（20261024-192858：B/C 在这里整段蒸发）。补窗 id 形如 SBW1，
            # 天然不撞 W<n>；补不出窗口才往下走。人审已确认缺口的范围不叫模型，也不给 solve 扇。
            acked = str(scope.get("id") or "") in acked_gaps
            rescue_complete = None if acked else complete
            declared = _rescue_scope_windows(
                scope, query, complete=rescue_complete, titles=titles,
            )
            if not declared and not acked and _solver_on():
                # 20261028 P0-2：零窗口 ≠ 缺口，先给一扇受控求解扇（UE_SOLVER_ENABLED 开着才有）。
                # 20261028-171355 起并进合并队列：零窗口与覆盖对账共用一扇，见下。
                zero_needs.append((si, str(scope.get("id") or ""), scope))
        declared_windows[si] = declared
        solve_windows[si] = solve_by_scope.get(si) or []
        in_zero_queue = any(zsi == si for zsi, _zsid, _zscope in zero_needs)
        if not declared and not solve_windows.get(si) and not in_zero_queue \
                and not scope.get("blocked") and not str(scope.get("external_dep") or "").strip():
            scope_gaps[si] = _capability_gap_record(scope)

    # 覆盖对账（20261028-155214 假成功教训）：有窗范围 ≠ 交付被覆盖。
    # 声明窗接不住的交付项：求解开着给一扇只装这些交付的 solve 扇；求解关着且
    # 人审没表态过 → partial_gap，交付验收要真失败，不许静默丢；人审已表态 →
    # 按能力缺口口径记，走「已确认不执行」。
    # 20261028-165939 教训：多个范围各派一扇 solve 会各自写一套同名类，后扇撞
    # 前扇的重复定义，编译必炸——同一任务书里全部未覆盖交付**合并成一扇**，
    # 挂在第一个需要的范围下，依赖取全部相关范围的声明窗。
    partial_gaps: dict[int, list[str]] = {}
    shared_solve: dict[int, bool] = {}
    # 待求解需求统一队列：零窗口范围（整段没窗口）+ 有窗范围里没被窗口接住的交付项。
    # needs: (si, sid, 待求解文本行列表)。20261028-171355 起**一个任务书最多一扇
    # solve 扇**——多扇 solve 并行会各自写一套同名类（撞重复定义）并互抢 Build.bat
    # （UBT exit 6 aggregate exceptions），两败俱伤。
    needs: list[tuple[int, str, list[str]]] = []
    for zsi, zsid, zscope in zero_needs:
        parts = [str(zscope.get("title") or ""), str(zscope.get("pending_note") or "")]
        parts.extend(str(item) for item in (zscope.get("deliverables") or []))
        text = "；".join(part for part in parts if part.strip())
        needs.append((zsi, zsid, [text] if text else []))
    for si, scope in enumerate(raw_scopes):
        wins = declared_windows.get(si) or []
        if not wins or scope.get("blocked") or str(scope.get("external_dep") or "").strip():
            continue
        sid = str(scope.get("id") or "")
        uncovered = _uncovered_deliverables(scope, wins)
        if uncovered:
            needs.append((si, sid, uncovered))
    if needs:
        first_si, first_sid, _first_items = needs[0]
        merged: list[str] = []
        seen_items: set[str] = set()
        for _si, _sid, items in needs:
            for item in items:
                if item and item not in seen_items:
                    seen_items.add(item)
                    merged.append(item)
        solved = False
        if _solver_on() and first_sid not in acked_gaps and merged:
            sub = {
                "id": f"{first_sid or 'S'}cov",
                "title": str(raw_scopes[first_si].get("title") or ""),
                "pending_note": "以下交付项没有窗口接住，交给受控求解（全部合并为一扇，只写一套代码）",
                "deliverables": merged,
                "depends_on": [],
                "external_dep": "",
                "windows": [],
            }
            extra = _solver_scope_windows(sub, query)
            if extra:
                raw_ids = []
                for need_si, _need_sid, _items in needs:
                    for raw in (declared_windows.get(need_si) or []):
                        rid = str(raw.get("id") or "").strip()
                        if rid and rid not in raw_ids:
                            raw_ids.append(rid)
                for cand in extra:
                    cand["depends_on"] = list(raw_ids)
                solve_windows[first_si] = list(solve_windows.get(first_si) or []) + extra
                for need_si, _need_sid, _items in needs[1:]:
                    shared_solve[need_si] = True
                solved = True
        if not solved:
            for need_si, need_sid, items in needs:
                if need_sid in acked_gaps:
                    scope_gaps[need_si] = {
                        "reason": "以下交付项人审已确认不执行（窗口接不住且未求解）："
                                  + "；".join(str(item)[:60] for item in items[:4]),
                        "missing_actions": [],
                        "deliverables": items,
                    }
                else:
                    partial_gaps[need_si] = items
    for si, scope in enumerate(raw_scopes):
        for raw in declared_windows.get(si) or []:
            counter += 1
            wid = f"W{counter}"
            if raw.get("id"):
                old_to_new[str(raw["id"])] = wid
            cand = _match_candidate(raw)
            if cand is not None:
                used.append(cand)
            plan.append((si, raw, cand, wid))
        # solve 扇走候选同一条物化路（dict(cand) 原样保留 task / scope_text）：
        # 不经 materialize——它不进动作表，规划者写不出它，只有代码补窗给得出。
        for cand in solve_windows.get(si) or []:
            counter += 1
            wid = f"W{counter}"
            if cand.get("id"):
                old_to_new[str(cand["id"])] = wid
            plan.append((si, None, cand, wid))
    for cand in candidates:
        if cand in used:
            continue
        counter += 1
        wid = f"W{counter}"
        old_id = str(cand.get("id") or "")
        if old_id:
            old_to_new[old_id] = wid
        plan.append((-1, None, cand, wid))

    # 第二遍：物化窗口。依赖用完整对照表重映射，且不允许指向自己。
    # 20261028-191514 教训：任务书生成器写出 W1→W2→W1 的环（范围 A 依赖后面的
    # 范围 B，B 的窗口又引用 W1），整个 run 被环诊断卡死，一扇都没跑。这里强制
    # 「只许引用先出现的窗口」：自引用与后向引用一律砍掉并打印一句，不让环进调度。
    plan_order = {entry_wid: idx for idx, (_si, _raw, _cand, entry_wid) in enumerate(plan)}
    for si, raw, cand, wid in plan:
        deps: list[str] = []
        for dep in ((raw or {}).get("depends_on") or []):
            mapped = old_to_new.get(str(dep), str(dep))
            if mapped == wid:
                continue
            if plan_order.get(mapped, -1) >= plan_order.get(wid, 0):
                print(f"任务书依赖环已砍：{wid} ← {mapped}（只许引用先出现的窗口）", flush=True)
                continue
            if mapped not in deps:
                deps.append(mapped)
        for dep in ((cand or {}).get("depends_on") or []):
            mapped = old_to_new.get(str(dep), str(dep))
            if mapped == wid:
                continue
            if plan_order.get(mapped, -1) >= plan_order.get(wid, 0):
                print(f"任务书依赖环已砍：{wid} ← {mapped}（只许引用先出现的窗口）", flush=True)
                continue
            if mapped not in deps:
                deps.append(mapped)
        if cand is not None:
            window = dict(cand)
            window["id"] = wid
            if raw and raw.get("title"):
                window["title"] = str(raw["title"])
            window["depends_on"] = deps
        else:
            made = materialize_goal(
                raw["action"],
                raw.get("slots") or {},
                title=raw.get("title"),
                wid=wid,
                depends=deps,
                retries=int(raw.get("max_retries") or 1),
                paths=origin_paths,
            )
            window = made[0]
            for extra_index, extra in enumerate(made[1:], 1):
                # 归地插出的 create 前置扇：id 加 c 后缀，依赖与本扇同源，本扇回头等它
                extra_id = f"{wid}c{extra_index}"
                extra["id"] = extra_id
                extra["depends_on"] = list(deps)
                merged = list(window.get("depends_on") or [])
                if extra_id not in merged:
                    merged.append(extra_id)
                window["depends_on"] = merged
                windows.append(extra)
                win_by_id[extra_id] = extra
        if si >= 0:
            scope = raw_scopes[si]
            blocked = bool(scope.get("blocked"))
            external = str(scope.get("external_dep") or "").strip()
            if blocked:
                window["realizable"] = False
                window["ok"] = False
                window["blocked"] = True
                window["error"] = f"范围 {scope['id']} 的交付清单为空，这一窗暂停。"
            elif external:
                if external_dep_satisfied(external):
                    # 依赖已经被满足（编辑器/MCP 现在连通）：不拦执行，只把话留下。
                    window["external_dep_checked"] = external
                else:
                    window["waiting_external"] = True
                    window["external_dep"] = external
            scope_wids.setdefault(si, []).append(wid)
        else:
            leftover_ids.append(wid)
            leftover_titles.append(str(cand.get("title") or cand.get("action") or ""))
        windows.append(window)
        win_by_id[wid] = window

    for si, scope in enumerate(raw_scopes):
        row = {
            "id": str(scope.get("id") or ""),
            "title": str(scope.get("title") or ""),
            "window_ids": list(scope_wids.get(si) or []),
            "external_dep": str(scope.get("external_dep") or "").strip(),
            "blocked": bool(scope.get("blocked")),
            "deliverables": list(scope.get("deliverables") or []),
            "pending_note": str(scope.get("pending_note") or ""),
        }
        gap = scope_gaps.get(si)
        if gap:
            row["capability_gap"] = gap
            _note_gap_candidate(scope, gap)
        uncovered = partial_gaps.get(si)
        if uncovered:
            row["partial_gap"] = {"deliverables": uncovered}
        # 共享 solve 扇：合并的未覆盖交付挂在第一个范围下，其余范围把这扇
        # 记进自己的 window_ids（覆盖统计与判据都算它一份）。
        if shared_solve.get(si):
            solved_wid = old_to_new.get(f"SBW-{first_sid}cov-SOLVE")
            if solved_wid and solved_wid not in row["window_ids"]:
                row["window_ids"].append(solved_wid)
        scopes_meta.append(row)

    # 范围级依赖：本范围第一扇接在依赖范围的最后一扇后面。
    # 与第二遍同一条环规则（20261028-194400：范围 A 依赖排在后面的 B，注入后成
    # 环 W1→W2→W1，整 run 暂停一扇没跑）：只许接在「先出现」的窗口后面，后向的砍掉并打印。
    for si, scope in enumerate(raw_scopes):
        ids = scope_wids.get(si) or []
        if not ids:
            continue
        first = win_by_id.get(ids[0])
        if not first:
            continue
        for dep_scope in scope.get("depends_on") or []:
            last = None
            for other in scopes_meta:
                if other is scopes_meta[si]:
                    continue
                if other["id"] == str(dep_scope) and other.get("window_ids"):
                    last = other["window_ids"][-1]
            if not last or last == first["id"]:
                continue
            if plan_order.get(last, -1) >= plan_order.get(first["id"], 0):
                print(f"任务书依赖环已砍：{first['id']} ← {last}（范围级依赖只许接在先出现的窗口后）", flush=True)
                continue
            merged = list(first.get("depends_on") or [])
            if last not in merged:
                merged.append(last)
            first["depends_on"] = merged
    if leftover_ids:
        scopes_meta.append({
            "id": "EX",
            "title": "任务书未归类的窗口（快路径已拆好，照跑不丢）",
            "window_ids": leftover_ids,
            "external_dep": "",
            "blocked": False,
            "deliverables": leftover_titles,
            "pending_note": "生成器没有把这些窗口归进任何范围，按原候补跑。",
        })
    if brief.get("integration"):
        counter += 1
        wid = f"W{counter}"
        deps = [w["id"] for w in windows]
        window = materialize(
            INTEGRATE_ACTION,
            {},
            title=str(brief["integration"].get("title") or "累计集成、验证与交付"),
            wid=wid,
            depends=deps,
            retries=1,
        )
        windows.append(window)
        scopes_meta.append({
            "id": "INT",
            "title": str(brief["integration"].get("title") or "累计集成、验证与交付"),
            "window_ids": [wid],
            "external_dep": "",
            "blocked": False,
            "deliverables": list(brief["integration"].get("deliverables") or []),
            "pending_note": "",
            "kind": "integration",
        })
    try:
        from agent.harness import build_criteria

        for meta in scopes_meta:
            scoped = [win_by_id[wid] for wid in (meta.get("window_ids") or []) if wid in win_by_id]
            meta["criteria"] = build_criteria(windows=scoped)
    except Exception:
        pass
    return windows, scopes_meta
