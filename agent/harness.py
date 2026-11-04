# agent/harness.py
"""Harness 决策字段、验收合同、交接文件。

不调用模型，不改图。任务的 save / pie 缺省跟随 UE_DEFAULT_SAVE / UE_DEFAULT_PIE（缺省 true）。
合同的 passed 只由对得上的裁判改写。
"""
import json
import os
import re
from datetime import date
from pathlib import Path


def _actions_judge_by_action() -> dict:
    """P1-6：动作→裁判口径由 agent/actions.py 单一声明推导。"""
    from agent.actions import JUDGE_BY_ACTION

    return dict(JUDGE_BY_ACTION)


EVIDENCE_KINDS = ("cpp_build", "blueprint_compile", "editor_assert", "automation", "widget_graph", "manual")
STOP_REASONS = ("ok", "paused", "retry_exceeded", "read_stall", "judge_failed", "unresolved",
                "no_evidence")
_CLIP = 240
_CRITERIA_LIMIT = 12

# 复合动作扇：执行时自带关卡内容核对（editor_assert 副判词）与 automation 收口，
# 它们能当 editor_assert 条款的证据窗口（见 _window_ids_for）。
_COMPOUND_EVIDENCE_ACTIONS = frozenset({"implement_shooter", "implement_grenade", "solve"})

# 只记「动作类型 → 裁判口径」的对应关系，不含任何具体玩法。
# P1-6 起由 agent/actions.py 单一声明推导；create_level 的口径与 catalog._VERIFY 一致：
# 「/Game 下关卡资产存在、该关卡已成为当前关卡」，是编辑器只读断言（窗口自带
# test.judge_kind="editor_assert"）。漏了这行 ⇒ build_criteria 产不出条款、
# _window_ids_for 也绑不上，一个真建成关卡的 run 条款数会是 0，集成矩阵 checked 为空 ⇒ 假失败。
# T8 的两个缺口已在 U1（20261027 升级计划）补上：producer 侧先声明判词，映射两侧同时动——
# bind_blueprint 在 sequence._bind_evidence 用一次 find_assets 只读核对唯一蓝图路径（editor_assert）；
# create_widget_blueprint 在 _create_widget_blueprint 保存后跑 blueprint_compile 编译裁判。
# PENDING_JUDGE 的两条登记已按同一批移除（check_catalog_parity.py）。
# solve 扇只由代码补窗给出（brief.materialize_scopes），收口判据固定走 automation；
# 求解产不出过滤器时诚实失败，不编编辑器断言。apply_spec 自 P0-3 起写完强制编译，收口 cpp_build。
_KIND_BY_ACTION = dict(_actions_judge_by_action())

_KIND_TEXT = {
    "cpp_build": "C++ 编译通过，退出码 0 且没有 error C",
    "blueprint_compile": "已点名的蓝图能编过",
    "editor_assert": "编辑器读数与期望一致",
    "automation": "Automation 过滤器失败数为 0",
    "widget_graph": "控件图连线与事件绑定已落地",
    "manual": "闭环做不到的那部分，由人在 Plan/ 里完成",
}


def clip(text, limit: int = _CLIP) -> str:
    raw = str(text or "").replace("\n", " ").strip()
    if "read_source" in raw:
        return ""
    return raw[:limit]


def policy_snapshot(task: dict | None) -> dict:
    """拒绝时记下的当前开关。不发明新的策略语言。"""
    from config import allow_anim, allow_blueprint, allow_material, allow_niagara, allow_umg, task_flag

    task = task or {}
    allow = task.get("allow_writes") or []
    return {
        "save": task_flag(task, "save"),
        "pie": task_flag(task, "pie"),
        "allow_writes": [str(item) for item in allow],
        "UE_ALLOW_BLUEPRINT": bool(allow_blueprint()),
        "UE_ALLOW_MATERIAL": bool(allow_material()),
        "UE_ALLOW_UMG": bool(allow_umg()),
        "UE_ALLOW_ANIM": bool(allow_anim()),
        "UE_ALLOW_NIAGARA": bool(allow_niagara()),
    }


def _window_filter(windows: list | None) -> str:
    """窗口里写到的 Automation 过滤器。没有就空串，不猜。"""
    for window in windows or []:
        if not isinstance(window, dict):
            continue
        if str(window.get("action") or "") != "run_automation_test":
            continue
        slots = window.get("slots") if isinstance(window.get("slots"), dict) else {}
        found = str(window.get("automation_filter") or slots.get("filter") or "").strip()
        if found:
            return found
    return ""


def _criterion_row(item: dict, windows: list, seen_ids: set[str]) -> dict | None:
    """一条条款的格式校验：evidence_kind 必须在允许范围内，id 唯一，passed 一律从 false 起。"""
    kind = str(item.get("evidence_kind") or "").strip()
    if kind not in EVIDENCE_KINDS:
        return None
    ident = clip(item.get("id") or "", 40) or f"c-{kind}"
    ident = _unique_id(ident, seen_ids)
    text = clip(item.get("text") or "", 200) or _KIND_TEXT[kind]
    row = {"id": ident, "text": text, "evidence_kind": kind, "passed": False}
    owners = item.get("windows")
    if isinstance(owners, list) and owners:
        # 条款自带归属（场景内容闸门补的那种）：只认这几扇，别被同类裁判顺手勾上
        row["windows"] = [str(one) for one in owners if str(one).strip()]
    if kind == "automation":
        found = clip(item.get("filter") or "", 120) or _window_filter(windows)
        if found:
            row["filter"] = found
    return row


def _window_ids_for(kind: str, windows: list, filt: str = "") -> list[str]:
    """哪些窗口能证明这条 kind。空列表=本 run 没有任何窗口能证明它（20261021-195220：
    W5 的「失败数 0」勾到了讲关卡内容的条款，因为条款不记归属）。
    控件图条款的证据窗口是改控件图那些扇——它们按动作表归在 blueprint_compile 下，
    这里单独认一遍，否则 widget_graph 永远绑不上、只能靠 kind 全量覆盖。
    editor_assert 条款另认复合动作扇（implement_shooter / implement_grenade / solve）：
    它们跑的过程中真的做关卡内容核对并落 editor_assert 副判词
    （20261028-151850：L1 把「关卡里有 FpsGrenadePickup…」写成 editor_assert 条款，
    只绑到 launch_editor 扇，复合扇收口后条款永远 false）。"""
    out: list[str] = []
    for window in windows or []:
        if not isinstance(window, dict):
            continue
        wid = str(window.get("id") or "").strip()
        action = str(window.get("action") or "")
        if not wid:
            continue
        if kind == "widget_graph":
            if action != "edit_widget_blueprint":
                continue
        elif kind == "editor_assert" and action in _COMPOUND_EVIDENCE_ACTIONS:
            pass
        elif _KIND_BY_ACTION.get(action) != kind:
            continue
        if kind == "automation" and filt:
            slots = window.get("slots") if isinstance(window.get("slots"), dict) else {}
            found = str(window.get("automation_filter") or slots.get("filter") or "").strip()
            if found and found != filt:
                continue
        if wid not in out:
            out.append(wid)
    return out


def _bind_windows(rows: list[dict], windows: list) -> list[dict]:
    """把「能证明它的窗口 id」写进条款。调用方给了窗口清单就一律绑定：
    绑不上记空列表，等于这条没有任何窗口能证明，不会被别的窗口顺手勾上。
    窗口清单里一个 id 都没有（离线拆句）时不绑定，保持旧的按 kind 口径。
    条款自己已经写明 windows 的（场景内容闸门补的那种）不覆盖——它只认自己那一扇。"""
    if not windows:
        return rows
    if not any(str(item.get("id") or "").strip() for item in windows if isinstance(item, dict)):
        return rows
    for row in rows:
        if isinstance(row.get("windows"), list) and row["windows"]:
            continue
        row["windows"] = _window_ids_for(
            str(row.get("evidence_kind") or ""), windows, str(row.get("filter") or "")
        )
    return rows


def build_criteria(criteria: list | None = None, windows: list | None = None) -> list[dict]:
    """验收合同只从两处来：L1 理解器给的 criteria，或窗口动作类型推出的 evidence_kind。
    这里不读原句、不拼业务条款，只做格式校验。每条 passed 从 false 起，模型文本改不了。"""
    windows = [item for item in (windows or []) if isinstance(item, dict)]
    rows: list[dict] = []
    seen_ids: set[str] = set()
    for item in criteria or []:
        if not isinstance(item, dict) or len(rows) >= _CRITERIA_LIMIT:
            continue
        row = _criterion_row(item, windows, seen_ids)
        if row is not None:
            rows.append(row)
    if rows:
        # 复合动作扇（implement_* / solve）自带 automation_filter：L1 合同没给对应
        # automation 条款时补一行，否则复合扇的收口裁判没有条款可勾，
        # 整句会被误判「部分完成」（20261028-151850 的 W3 就会这样收场）。
        for window in windows:
            action = str(window.get("action") or "")
            if action not in _COMPOUND_EVIDENCE_ACTIONS:
                continue
            filt = str(window.get("automation_filter") or "").strip()
            wid = str(window.get("id") or "").strip()
            if not filt or not wid:
                continue
            covered = any(
                isinstance(item, dict)
                and str(item.get("evidence_kind") or "") == "automation"
                and (
                    wid in [str(one) for one in (item.get("windows") or [])]
                    or str(item.get("filter") or "") == filt
                )
                for item in rows
            )
            if covered:
                continue
            row = _criterion_row(
                {"evidence_kind": "automation", "filter": filt, "text": f"{filt} 失败数为 0"},
                windows,
                seen_ids,
            )
            if row is not None:
                rows.append(row)
        return _bind_windows(rows, windows)
    # 没有 L1 合同（规则 / 已学 / 老规划者路径）时，才按窗口动作类型兜底
    seen_kinds: set[str] = set()
    for window in windows:
        kind = _KIND_BY_ACTION.get(str(window.get("action") or ""))
        if not kind or kind in seen_kinds or kind not in EVIDENCE_KINDS:
            continue
        row = _criterion_row({"evidence_kind": kind}, windows, seen_ids)
        if row is None:
            continue
        seen_kinds.add(kind)
        rows.append(row)
    return _bind_windows(rows, windows)


def apply_judge(criteria: list | None, judge: dict | None) -> list[dict]:
    """只有 judge.ok 且 kind 等于 evidence_kind 才改成通过。manual 保持 false。
    judge 带了 window 时，只有条款的 windows 列了它才算证明得动；没列的条款不动。
    synthetic 判词（launch_editor：只证明「端口通了」）只放行证明窗口恰好只有它自己的条款——
    宽绑定的条款混着别的证据窗口，端口连通证明不了内容（20261027-180955 的假通过）。
    automation 还要求真正跑到了测试：跑了 0 个测试的「失败数 0」不证明任何条款。"""
    out = [dict(item) for item in (criteria or []) if isinstance(item, dict)]
    if not isinstance(judge, dict) or judge.get("ok") is not True:
        return out
    kind = str(judge.get("kind") or "")
    if kind not in EVIDENCE_KINDS:
        return out
    wid = str(judge.get("window") or "").strip()
    synthetic = judge.get("synthetic") is True
    tests = judge.get("tests")
    for item in out:
        if str(item.get("evidence_kind") or "") == "manual":
            continue
        if str(item.get("evidence_kind") or "") != kind:
            continue
        owners = item.get("windows")
        if wid and isinstance(owners, list) and wid not in [str(one) for one in owners]:
            continue
        if synthetic:
            # 只证明自己那一扇：条款的证明窗口必须恰好只有它，宽绑定一律不动
            if not wid or [str(one) for one in owners or []] != [wid]:
                continue
        if kind == "automation" and isinstance(tests, list):
            if not tests:
                continue
            want = str(item.get("filter") or "").strip()
            if want and not any(str(one).startswith(want) for one in tests):
                continue
        item["passed"] = True
    return out


def ignore_model_claim(criteria: list | None, text: str = "") -> list[dict]:
    """模型说「已完成」不改合同。text 只为了调用点显式带上这句话。"""
    del text
    return [dict(item) for item in (criteria or []) if isinstance(item, dict)]


def apply_judges(criteria: list | None, *judges) -> list[dict]:
    current = [dict(item) for item in (criteria or []) if isinstance(item, dict)]
    for judge in judges:
        if isinstance(judge, dict):
            current = apply_judge(current, judge)
    return current


def effective_verify_specs(task: dict | None) -> list[dict]:
    """这一扇跑裁判时真正会跑到的 spec：task.verify 里 kind 合法的那些，外加 automation_filter
    合成的一条（运行期 nodes._test_specs 就是这么拼的：过滤器写在 task 上，spec 不在 verify 里）。
    纯函数：不读文件、不调模型、不凭句子拼 spec，只把已有的 spec 摊平。"""
    task = task if isinstance(task, dict) else {}
    specs: list[dict] = []
    for item in task.get("verify") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "") not in EVIDENCE_KINDS:
            continue
        specs.append(dict(item))
    filt = str(task.get("automation_filter") or "").strip()
    if filt and not any(
        str(item.get("filter") or item.get("filterExpression") or "") == filt for item in specs
    ):
        specs.append({"kind": "automation", "filter": filt})
    return specs


def _scope_capability_gap(scope: dict) -> dict | None:
    """范围整段没有可执行窗口时 materialize_scopes 写下的结构化能力缺口。
    只认这个显式字段，不靠「窗口为空」反推：人工拼的范围（老用例、旧记录）
    零窗口仍走原来的口径，兼容优先。"""
    gap = scope.get("capability_gap")
    return gap if isinstance(gap, dict) and gap else None


def _gap_mark_reason(scope: dict, gap: dict) -> str:
    """能力缺口的文案必须说清：没有可执行的窗口，是能力缺口，不是待办。"""
    bits = ["没有可执行的窗口，是能力缺口，不是待办"]
    deliver = [
        str(item) for item in (gap.get("deliverables") or scope.get("deliverables") or [])
        if str(item).strip()
    ]
    if deliver:
        bits.append("没交付：" + "、".join(deliver[:3]))
    missing = [str(item) for item in (gap.get("missing_actions") or []) if str(item).strip()]
    if missing:
        bits.append("缺动作：" + "、".join(missing))
    note = str(gap.get("pending_note") or scope.get("pending_note") or "").strip()
    if note:
        bits.append("现状：" + note[:60])
    return "；".join(bits)


def scope_marks(scopes: list | None) -> list[dict]:
    """范围级完成判定，代码判，不看模型文本。一个范围算完成要同时满足：
    有交付清单（否则暂停）、至少一扇能跑的窗口（否则只记待办）、窗口终态全 done、
    不等外部、自己的条款全过。没窗口的范围永远不算完成——20261021-195220 的 B/D/E 就是零窗口，
    旧逻辑里它们对总状态没有任何影响。
    带 capability_gap 的范围回结构化 reason，mark 里带 gap=True（20261024-192858：
    B/C 交付清单非空却一扇窗口都没有，之前只回一句「只记待办」，整句照样过）。
    scope 里带 window_states 时（调用方从机读状态注入）一并检查窗口是否都跑完。"""
    marks: list[dict] = []
    for scope in scopes or []:
        if not isinstance(scope, dict):
            continue
        sid = str(scope.get("id") or "").strip()
        rows = [item for item in (scope.get("criteria") or []) if isinstance(item, dict)]
        open_rows = [str(item.get("id") or "") for item in rows if not item.get("passed")]
        window_ids = [str(wid) for wid in (scope.get("window_ids") or []) if str(wid).strip()]
        states = [str(one) for one in (scope.get("window_states") or [])]
        unfinished = [wid for wid, state in zip(window_ids, states) if state != "done"] if states else []
        gap = _scope_capability_gap(scope)
        reason = ""
        if scope.get("blocked"):
            reason = "交付清单为空，这一范围暂停"
        elif gap:
            reason = _gap_mark_reason(scope, gap)
        elif scope.get("external_dep"):
            # 等外部要排在零窗口前面：等外部的范围本来就没有能跑的窗口，但原因不是「没有可执行的
            # 窗口，只记待办」——那句正是本轮要根除的话术（20261024 T3 证伪：零窗口 + M-GO 说错）。
            reason = f"等待外部 {scope.get('external_dep')}"
        elif not window_ids:
            reason = "没有可执行的窗口，只记待办"
        elif unfinished:
            reason = "窗口没跑完：" + "、".join(unfinished[:4])
        elif open_rows:
            reason = "条款未过：" + "、".join(open_rows[:4])
        mark = {"id": sid, "ok": not reason, "reason": reason}
        if gap:
            mark["gap"] = True
            mark["gap_reason"] = str(gap.get("reason") or "")
            mark["missing_actions"] = [
                str(item) for item in (gap.get("missing_actions") or []) if str(item).strip()
            ]
            mark["deliverables"] = [
                str(item) for item in (gap.get("deliverables") or []) if str(item).strip()
            ]
        marks.append(mark)
    return marks


def contract_status(criteria: list | None, status: str, scopes: list | None = None) -> str:
    """自动条款过了、仍有 manual，或还有未通过的 Automation 残差时，整句记部分完成。别的状态保持原样。
    给了 scopes 时，任何一个范围没完成也记部分完成（范围口径优先于窗口口径）；
    但存在 capability_gap 范围（整段没交付）时记「未完成」——没交付不是「部分完成」。
    已经是失败/中断的不改，别把窗口级失败吞掉。"""
    marks = scope_marks(scopes)
    if any(item.get("gap") for item in marks):
        return "未完成" if status in ("成功", "部分完成") else status
    if status != "成功":
        return status
    open_scopes = [item for item in marks if not item.get("ok")]
    if open_scopes:
        return "部分完成"
    rows = [item for item in (criteria or []) if isinstance(item, dict)]
    manuals = [item for item in rows if item.get("evidence_kind") == "manual"]
    autos = [item for item in rows if item.get("evidence_kind") != "manual"]
    if any(item.get("evidence_kind") == "automation" and not item.get("passed") for item in autos):
        return "部分完成"
    if any(item.get("evidence_kind") == "widget_graph" and not item.get("passed") for item in rows):
        return "部分完成"
    if not manuals:
        return status
    if autos and not all(item.get("passed") for item in autos):
        return status
    if any(not item.get("passed") for item in manuals):
        return "部分完成"
    return status


def classify_stop(*, ok: bool = False, paused: bool = False, retries: int = 0,
                  max_retries: int = 0, summary: str = "", error: str = "",
                  unresolved=None) -> str:
    if unresolved:
        return "unresolved"
    text = f"{summary or ''}\n{error or ''}"
    if any(token in text for token in ("unresolved", "没着落", "推不出")) and (paused or not ok):
        return "unresolved"
    if ok and not paused:
        return "ok"
    if "打转" in text or "read_stall" in text:
        return "read_stall"
    if paused:
        return "paused"
    try:
        if int(max_retries) > 0 and int(retries) >= int(max_retries):
            return "retry_exceeded"
    except (TypeError, ValueError):
        pass
    return "judge_failed"


def run_stop(reasons: list, ok: bool) -> str:
    found = {str(item) for item in reasons if item}
    for name in ("unresolved", "read_stall", "retry_exceeded", "judge_failed", "no_evidence", "paused"):
        if name in found:
            return name
    return "ok" if ok else "judge_failed"


def handoff_root() -> Path:
    override = os.environ.get("UE_HANDOFF_DIR", "").strip()
    if override:
        return Path(override)
    from config import DATA_DIR

    return DATA_DIR / "handoffs"


def load_goal() -> str:
    """progress.json 的 goal。没有文件或字段就返回空串，不编一句新目标。"""
    try:
        from config import DATA_DIR

        data = json.loads((DATA_DIR / "harness" / "progress.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str((data or {}).get("goal") or "")


def _handoff_path(run_id: str, window_id: str, root: Path | None = None) -> Path:
    return (root or handoff_root()) / str(run_id) / f"{window_id}.json"


def _titles_in(directory: Path, day: str | None) -> list[str]:
    path = Path(directory) / f"{day or date.today().isoformat()}.md"
    if not path.is_file():
        return []
    titles = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## ") and "未完成" in stripped:
            titles.append(stripped[3:].strip())
        elif stripped.startswith("- [ ]"):
            titles.append(stripped[5:].strip())
    return [item for item in titles if item]


def unfinished_titles(directory: Path | None = None, day: str | None = None) -> list[str]:
    """当天任务记录已经存在才读未完成标题。没有文件就不编。

    未指定目录时，`.codex/memory/任务记录` 与 `.cursor/memory/任务记录` 都看，去重后最多 12 条。
    """
    if directory is not None:
        return _titles_in(Path(directory), day)[:12]
    from config import task_record_dirs

    found = []
    seen = set()
    for folder in task_record_dirs():
        for title in _titles_in(folder, day):
            if title in seen:
                continue
            seen.add(title)
            found.append(title)
            if len(found) >= 12:
                return found
    return found


def _short_artifacts(artifacts: dict | None) -> dict:
    out = {}
    for key, value in (artifacts or {}).items():
        if not isinstance(value, dict):
            continue
        out[str(key)] = {
            "ref": value.get("ref"),
            "in_level": value.get("in_level"),
        }
    return out


def build_handoff(*, did: str, judges: list | None = None, summary: str = "", error: str = "",
                  ok: bool = False, criteria: list | None = None, next_title: str = "",
                  artifacts: dict | None = None, record_dir: Path | None = None,
                  day: str | None = None, goal: str | None = None) -> dict:
    """用裁判和窗口结果拼。不收工具正文。goal 缺省取 progress.json，没有就空串。"""
    evidence = []
    for judge in judges or []:
        if not isinstance(judge, dict) or not judge.get("kind"):
            continue
        evidence.append({
            "kind": str(judge.get("kind")),
            "ok": bool(judge.get("ok")),
            "summary": clip(judge.get("summary") or ""),
        })
    unmet = []
    if not ok:
        text = clip(error or summary)
        if text:
            unmet.append(text)
    for item in criteria or []:
        if not isinstance(item, dict) or item.get("passed"):
            continue
        label = clip(item.get("text") or item.get("evidence_kind") or "")
        if label and label not in unmet:
            unmet.append(label)
    nxt = [clip(next_title)] if next_title else []
    for title in unfinished_titles(record_dir, day):
        if title not in nxt:
            nxt.append(title)
    unmet_ids = []
    for item in criteria or []:
        if not isinstance(item, dict) or item.get("passed"):
            continue
        ident = str(item.get("id") or "").strip()
        if ident and ident not in unmet_ids:
            unmet_ids.append(ident)
    shown = unmet[:8]
    total = len(unmet)
    return {
        "did": clip(did, 200),
        "evidence": evidence,
        "unmet": shown,
        "unmet_total": total,
        "unmet_omitted": total - len(shown),
        "unmet_ids": unmet_ids,
        "next": "；".join(item for item in nxt if item)[:_CLIP],
        "artifacts": _short_artifacts(artifacts),
        "goal": clip(goal if goal is not None else load_goal()),
    }


def write_handoff(run_id: str, window_id: str, payload: dict, root: Path | None = None) -> Path:
    path = _handoff_path(run_id, window_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_handoff(run_id: str, window_id: str, root: Path | None = None) -> dict | None:
    path = _handoff_path(run_id, window_id, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def handoff_note(run_id: str, window_ids: list | None, root: Path | None = None) -> str:
    """下一扇用户消息里的交接。文件不在就返回空串，续接仍走 checkpoint。"""
    parts = []
    for wid in window_ids or []:
        doc = read_handoff(run_id, str(wid), root=root)
        if not doc:
            continue
        brief = {
            "window": str(wid),
            "unmet": list(doc.get("unmet") or []),
            "unmet_total": int(doc.get("unmet_total") or 0),
            "unmet_omitted": int(doc.get("unmet_omitted") or 0),
            "evidence": [
                {"kind": item.get("kind"), "ok": item.get("ok")}
                for item in (doc.get("evidence") or [])
                if isinstance(item, dict)
            ],
            "next": doc.get("next") or "",
        }
        parts.append(json.dumps(brief, ensure_ascii=False))
    if not parts:
        return ""
    return "上一扇交接：\n" + "\n".join(parts)


ATTRIBUTION_LABELS = ("blind_spot", "out_of_contract", "equivalent")
_COVER_LABELS = ("out_of_contract", "equivalent")
_TESTS_RE = re.compile(r"tests=([^\s]+)")
_CLUSTER_LIMIT = 4


def _test_key(message: str) -> str:
    match = _TESTS_RE.search(message or "")
    if not match:
        return ""
    name = match.group(1).split(",")[0].strip()
    return name


def _unique_id(ident: str, seen: set[str]) -> str:
    if ident not in seen:
        seen.add(ident)
        return ident
    number = 2
    while f"{ident}#{number}" in seen:
        number += 1
    ident = f"{ident}#{number}"
    seen.add(ident)
    return ident


def cluster_residuals(errors) -> list[dict]:
    """按错误消息里的测试名分簇。抽不出名字的进 unnamed。超过 4 簇时第 4 簇 key 为 overflow。"""
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    seen: set[str] = set()
    for index, err in enumerate(errors or []):
        if not isinstance(err, dict):
            continue
        key = _test_key(str(err.get("message") or "")) or "unnamed"
        raw = str(err.get("id") or "").strip()
        if not raw:
            raw = key if key != "unnamed" else f"unnamed-{index}"
        ident = _unique_id(raw, seen)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ident)
    if len(order) <= _CLUSTER_LIMIT:
        return [{"key": key, "ids": groups[key]} for key in order]
    head = order[: _CLUSTER_LIMIT - 1]
    overflow: list[str] = []
    for key in order[_CLUSTER_LIMIT - 1 :]:
        overflow.extend(groups[key])
    return [{"key": key, "ids": groups[key]} for key in head] + [{"key": "overflow", "ids": overflow}]


def build_dispose_windows(parent_id: str, errors, criteria, unmet_ids=None) -> list[dict]:
    """一簇一扇 dispose_residual。每扇 unmet_ids 只含该簇。条款从上一扇未通过的 automation 复制，passed 仍是 false。"""
    clusters = cluster_residuals(errors)
    copied = []
    for item in criteria or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("evidence_kind") or "") != "automation" or item.get("passed"):
            continue
        row = dict(item)
        row["passed"] = False
        copied.append(row)
    by_id = {str(item.get("id")): item for item in copied if str(item.get("id") or "").strip()}
    if len(clusters) == 1 and by_id and not any(ident in by_id for ident in clusters[0]["ids"]):
        chosen = [str(ident) for ident in (unmet_ids or []) if str(ident) in by_id]
        clusters = [{"key": clusters[0]["key"], "ids": chosen or list(by_id)}]
    windows = []
    for index, cluster in enumerate(clusters, start=1):
        ids = [str(ident) for ident in cluster["ids"]]
        rows = [dict(by_id[ident]) for ident in ids if ident in by_id]
        if not rows and len(clusters) == 1:
            rows = [dict(item) for item in copied]
            ids = [str(item.get("id")) for item in rows if str(item.get("id") or "").strip()] or ids
        windows.append({
            "id": f"{parent_id}-D{index}",
            "title": f"处置残差 {cluster['key']}",
            "action": "dispose_residual",
            "slots": {},
            "depends_on": [str(parent_id)],
            "max_retries": 0,
            "realizable": True,
            "ok": True,
            "blocked": False,
            "save": False,
            "pie": False,
            "unmet_ids": ids,
            "criteria": rows,
            "cluster_key": cluster["key"],
        })
    return windows


def attribution_path(run_id: str, window_id: str, root: Path | None = None) -> Path:
    return (root or handoff_root()) / str(run_id) / f"{window_id}.attribution.json"


def load_attribution(run_id: str, window_id: str, root: Path | None = None) -> dict:
    """没有文件不算坏。文件在但读不出、缺 items 列表，记 malformed。"""
    path = attribution_path(run_id, window_id, root)
    if not path.is_file():
        return {"malformed": False, "error": "", "items": [], "key": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"malformed": True, "error": "归因文件读不出", "items": [], "key": ""}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"malformed": True, "error": "归因文件没有 items", "items": [], "key": ""}
    return {
        "malformed": False,
        "error": "",
        "items": data.get("items") or [],
        "key": str(data.get("key") or ""),
    }


def normalize_attribution(items, residual_ids) -> dict:
    """id 必须来自这扇残差，一条一次。标签只能是三个词。note 走 clip，含 read_source 当空串。"""
    residuals = [str(ident) for ident in (residual_ids or [])]
    allowed = set(residuals)
    seen: set[str] = set()
    cleaned = []
    if not isinstance(items, list):
        return {"ok": False, "error": "归因不是列表", "items": []}
    for item in items:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            return {"ok": False, "error": "缺 id", "items": []}
        ident = str(item.get("id"))
        if ident not in allowed:
            return {"ok": False, "error": f"id {ident} 不是这扇残差", "items": []}
        if ident in seen:
            return {"ok": False, "error": f"重复 id {ident}", "items": []}
        seen.add(ident)
        label = str(item.get("label") or "")
        if label not in ATTRIBUTION_LABELS:
            return {"ok": False, "error": f"未知标签 {label}", "items": []}
        cleaned.append({"id": ident, "label": label, "note": clip(item.get("note") or "", 240)})
    return {"ok": True, "error": "", "items": cleaned}


def judge_disposition(residual_ids, criteria, items) -> dict:
    """残差被同 kind 裁判标成 passed，或被 out_of_contract / equivalent 盖住，才算这扇成功。
    blind_spot 不算盖住。归因说明不改 passed。"""
    rows = [dict(item) for item in (criteria or []) if isinstance(item, dict)]
    for row in rows:
        if row.get("passed") is not True:
            row["passed"] = False
    checked = normalize_attribution(items, residual_ids)
    if not checked["ok"]:
        return {"ok": False, "summary": checked["error"], "error": checked["error"], "criteria": rows, "items": []}
    covered = {item["id"] for item in checked["items"] if item["label"] in _COVER_LABELS}
    passed_ids = {
        str(row.get("id"))
        for row in rows
        if row.get("passed") is True and str(row.get("evidence_kind") or "") == "automation"
    }
    open_ids = [str(ident) for ident in (residual_ids or []) if str(ident) not in passed_ids and str(ident) not in covered]
    ok = not open_ids
    summary = "残差都有着落" if ok else "未结案：" + "、".join(open_ids)
    return {
        "ok": ok,
        "summary": summary,
        "error": "" if ok else summary,
        "criteria": rows,
        "items": checked["items"],
    }


def assess_kill(test_judge: dict | None) -> dict:
    """注入之后 Automation 必须失败才算杀伤。仍是失败数 0 时记 blind_spot，不能单靠这次归因结案。"""
    judge = test_judge if isinstance(test_judge, dict) else {}
    summary = str(judge.get("summary") or "")
    still_green = judge.get("ok") is True or "failed=0" in summary or "失败数 0" in summary
    if still_green:
        return {"killed": False, "label": "blind_spot", "can_close": False}
    if judge.get("ok") is False:
        return {"killed": True, "label": "", "can_close": True}
    return {"killed": False, "label": "blind_spot", "can_close": False}
