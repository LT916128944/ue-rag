# eval/check_candidate_aging.py
"""D8 离线验收：能力候选表必须有闭环（记次数、算年龄、出声、能关掉）。

## 缺陷
`action_candidates.json` 只有**写方**（`brief._note_gap_candidate`、`plan_windows.materialize`
表外分支、`intent.validate_intent`）与**一个读方** `iter_prompt_specs()`（只输出 approved），
中间没有人审队列、没有到期、没有优先级。实测：失败 run 时代 `data/skills/action_candidates.json`
**根本不存在** —— 缺口既没被记成候选，也没有到期与优先级，等于「记了」和「没记」一样。
`run_windows._emit_action_candidates` 发出的 `action_candidate` 事件**没有任何消费方**。

## 口径（不许放宽的部分）
1. **候选表不是垃圾桶**：阈值只控制「出声」，不控制「入库」。入库永远发生。
2. **`promote_candidate` 的口径一个字都不改**：仍要求 `reviewed=True` + 非空 `verification`。
   不许加默认值、不许把 `reviewed` 默认成真（§7 不做清单第 3 条）。
3. **不许倒填假年龄**：`first_seen` 缺失时用 `last_seen` 回落，两者都没有就**不算过期**。
   宁可少报一条 stale，也不许凭猜出来的年龄去催人。
4. `dismissed` 与 `approved` 都不算 stale：人已经表过态的不再催。

不连编辑器、不调模型、不联网；候选文件隔离到临时目录。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _isolation(tmp: Path):
    """把候选/已提升表都隔离到临时目录，返回恢复句柄。

    两张表都要隔离：`promote_candidate` 写的是 `promoted_actions.json`，
    只隔离候选表会让「人审通过的假动作」漏进真实数据目录，下次
    `eval/check_catalog_parity.py` 的 I5 会因为 planner 多出一个真实 CATALOG 里没有的名字而红。
    这不是理论风险：本文件第一版就漏了这条，被 parity 守卫当场抓到。"""
    prev = {
        "candidates": os.environ.get("UE_ACTION_CANDIDATES"),
        "promoted": os.environ.get("UE_PROMOTED_ACTIONS"),
    }
    os.environ["UE_ACTION_CANDIDATES"] = str(tmp / "action_candidates.json")
    os.environ["UE_PROMOTED_ACTIONS"] = str(tmp / "promoted_actions.json")
    return prev


def _restore(prev) -> None:
    for key, env in (("candidates", "UE_ACTION_CANDIDATES"),
                     ("promoted", "UE_PROMOTED_ACTIONS")):
        value = prev.get(key)
        if value is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = value


def check_first_hit_records_aging_fields() -> None:
    """D8-1：第一次命中就写下 first_seen / last_seen / hits / sources。"""
    from agent import catalog

    rows = catalog.load_candidates()
    assert rows == [], ("前提：临时目录里的候选表应当是空的", rows)
    found = catalog.note_candidate("no_such_action_alpha", source="test", summary="测试候选")
    assert found is not None, "note_candidate 没记下新动作"
    for key in ("first_seen", "last_seen", "hits", "sources"):
        assert key in found, (f"候选记录缺 {key}", found)
    assert found["hits"] == 1, found
    assert found["first_seen"] == found["last_seen"], found
    assert found["sources"] == ["test"], found
    assert str(found.get("status")) == "review", found

    # 落盘件必须和返回值一致（不许只在内存里对）
    on_disk = _read(catalog.candidates_path())["candidates"]
    assert len(on_disk) == 1, on_disk
    assert on_disk[0]["name"] == "no_such_action_alpha", on_disk


def check_repeat_hit_accumulates() -> None:
    """D8-2：重复命中累计 hits、刷新 last_seen、不改 first_seen、来源去重。"""
    from agent import catalog

    catalog.note_candidate("no_such_action_alpha", source="test")
    before = next(row for row in catalog.load_candidates() if row["name"] == "no_such_action_alpha")
    seen_before = before["first_seen"]
    base_hits = int(before["hits"])
    second = catalog.note_candidate("no_such_action_alpha", source="planner")
    assert second is not None
    assert second["hits"] == base_hits + 1, ("重复命中没有累计次数", second, base_hits)
    assert second["first_seen"] == seen_before, ("first_seen 被改写了", second, seen_before)
    assert "planner" in second["sources"] and "test" in second["sources"], second
    third = catalog.note_candidate("no_such_action_alpha", source="planner")
    assert third["hits"] == second["hits"] + 1, third
    assert third["sources"] == second["sources"], ("同一来源不许重复计数", third)


def check_builtin_action_not_recorded() -> None:
    """反例守卫：内置动作、代码专用扇都不进候选表（候选表不是第二张动作表）。"""
    from agent import catalog

    assert catalog.note_candidate("launch_editor") is None, "内置动作不该进候选表"
    assert catalog.note_candidate(catalog.INTEGRATE_ACTION) is None, "代码专用扇不该进候选表"
    assert catalog.note_candidate(catalog.DISPOSE_ACTION) is None
    assert catalog.note_candidate("  ") is None, "空名字不该进候选表"
    names = {str(row.get("name")) for row in catalog.load_candidates()}
    assert "launch_editor" not in names, names


def check_stale_detection() -> None:
    """D8-3：超过阈值仍是 review ⇒ stale；正好到阈值当天就算；未到阈值不算。"""
    from agent import catalog

    path = catalog.candidates_path()
    payload = _read(path)
    for row in payload["candidates"]:
        if row["name"] == "no_such_action_alpha":
            row["first_seen"] = "2026-01-01"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    stale = catalog.stale_candidates()
    names = [str(item.get("name")) for item in stale]
    assert "no_such_action_alpha" in names, ("超期 review 候选没被认成 stale", names)
    item = next(one for one in stale if one.get("name") == "no_such_action_alpha")
    assert int(item.get("age_days") or 0) >= catalog.CANDIDATE_STALE_DAYS, item

    # 阈值边界：把它改成「今天」，立刻不再 stale
    payload = _read(path)
    today = catalog._today()
    for row in payload["candidates"]:
        if row["name"] == "no_such_action_alpha":
            row["first_seen"] = today
            row["last_seen"] = today
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    names = [str(item.get("name")) for item in catalog.stale_candidates()]
    assert "no_such_action_alpha" not in names, ("刚记下的候选不该立刻 stale", names)


def check_no_fake_age() -> None:
    """D8-4 反例守卫：first_seen / last_seen 都缺失或坏掉时**不许**算过期。"""
    from agent import catalog

    path = catalog.candidates_path()
    payload = _read(path)
    payload["candidates"].append({
        "name": "no_such_action_gamma", "status": "review",
        "first_seen": "", "last_seen": "不是日期",
    })
    payload["candidates"].append({
        "name": "no_such_action_delta", "status": "review",
    })
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    names = {str(item.get("name")) for item in catalog.stale_candidates()}
    assert "no_such_action_gamma" not in names, ("坏日期被当成过期了", names)
    assert "no_such_action_delta" not in names, ("没有日期被当成过期了", names)


def check_dismiss_stops_nagging() -> None:
    """D8-5：dismiss 后不再 stale，且永不自动 promote。"""
    from agent import catalog

    path = catalog.candidates_path()
    payload = _read(path)
    for row in payload["candidates"]:
        if row["name"] == "no_such_action_alpha":
            row["first_seen"] = "2026-01-01"
            row["status"] = "review"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert [x for x in catalog.stale_candidates() if x.get("name") == "no_such_action_alpha"], (
        "前提：它此刻应当是 stale")

    dismissed = catalog.dismiss_candidate("no_such_action_alpha", note="本期不做")
    assert dismissed is not None and dismissed["status"] == "dismissed", dismissed
    assert dismissed.get("dismissed_at"), dismissed
    names = {str(item.get("name")) for item in catalog.stale_candidates()}
    assert "no_such_action_alpha" not in names, ("dismiss 之后不该再催", names)
    # dismiss 不等于 approve：它仍然进不了提示
    assert "no_such_action_alpha" not in {name for name, _ in catalog.iter_prompt_specs()}
    assert "no_such_action_alpha" not in catalog.CATALOG


def check_promote_guard_unchanged() -> None:
    """D8-6：promote 的口径一个字都不许放宽（§7 不做清单第 3 条）。"""
    from agent import catalog

    catalog.note_candidate("no_such_action_beta", source="test")
    assert catalog.promote_candidate("no_such_action_beta", reviewed=False, verification=["x"]) is None, (
        "reviewed=False 竟然 promote 成功了")
    assert catalog.promote_candidate("no_such_action_beta", reviewed=True, verification=[]) is None, (
        "verification 为空竟然 promote 成功了")
    assert catalog.promote_candidate("no_such_action_beta", reviewed=True, verification=["真验过"]) is not None, (
        "人审 + 验证都齐了应当能 promote")
    # 内置动作永远不许被 promote 顶掉
    assert catalog.promote_candidate("launch_editor", reviewed=True, verification=["x"]) is None
    # 提升表必须落在临时目录里，不许污染 data/skills（parity 守卫的 I5 会因此变红）
    promoted = catalog.promoted_path()
    assert "no_such_action_beta" not in catalog.CATALOG, (
        "候选提升不许改 CATALOG 常量（执行器只认 allows()）")
    on_disk = _read(promoted)["actions"] if promoted.is_file() else []
    assert any(row.get("name") == "no_such_action_beta" for row in on_disk), on_disk


def check_legacy_record_upgraded() -> None:
    """兼容性：老记录（没有 first_seen/hits）被再次命中时要补字段，不许倒填更早日期。"""
    from agent import catalog

    path = catalog.candidates_path()
    payload = _read(path)
    payload["candidates"].append({
        "name": "建关卡",  # 真实失败 run 里写下的形状：没有 first_seen / hits
        "version": 1, "conflicts": [], "verification": [],
        "status": "review", "source": "capability_gap", "summary": "老记录",
        "spec": {"slots": [], "summary": "老记录"},
    })
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    before = next(row for row in catalog.load_candidates() if row["name"] == "建关卡")
    assert "first_seen" not in before, ("前提：这应当是一条老记录", before)

    after = catalog.note_candidate("建关卡", source="capability_gap")
    assert after is not None
    assert after["first_seen"] == catalog._today(), (
        "老记录没有 first_seen 时不许倒填假日期，应当从今天算起", after)
    assert after["hits"] == 1, after
    assert "capability_gap" in after["sources"], after


def main() -> int:
    from agent import catalog

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        prev = _isolation(tmp)
        try:
            check_first_hit_records_aging_fields()
            check_repeat_hit_accumulates()
            check_builtin_action_not_recorded()
            check_stale_detection()
            check_no_fake_age()
            check_dismiss_stops_nagging()
            check_promote_guard_unchanged()
            check_legacy_record_upgraded()
        finally:
            _restore(prev)
    print("candidate aging ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
