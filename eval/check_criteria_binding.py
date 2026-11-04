# eval/check_criteria_binding.py
"""条款绑定回归（U3b，20261027-180955 的教训）。

那一次整句 run：4 扇 place_batch 全部 failed（monolith 断链），但 5 条 editor_assert
条款全部 passed=True。链条：理解器自由条款 → build_criteria 按 kind 宽绑定到全部
editor_assert 扇（launch/open_level/place_batch 都在）→ W1 launch_editor 的判词把
同 kind 条款全部翻绿。20261021-195220 的教训换了入口重现。

修法钉死三件事：
① 场景闸门在「设计图已被既有 place_batch 扇覆盖」时，把讲该实体的条款绑到那些具体扇；
② launch_editor 判词打 synthetic，apply_judge 只放行「证明窗口恰好只有它自己」的条款；
③ 本文件：用 20261027-180955 的真实形态做回归——窗口全败时条款必须保持 false。

纯函数，不调模型、不动编辑器。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# 20261027-180955 的真实形态（截取决策字段）：W1/W2 成功，4 扇 place_batch 全败，W5 排队
RUN_WINDOWS = [
    {"id": "W1", "action": "launch_editor", "slots": {}, "depends_on": []},
    {"id": "W2", "action": "open_level", "slots": {"level": "L_NewTest"}, "depends_on": []},
    {"id": "W3", "action": "place_batch", "slots": {"blueprint": "BP_Coin", "layout": "20261020-coins"}, "depends_on": []},
    {"id": "W4", "action": "place_batch", "slots": {"blueprint": "BP_Platform", "layout": "20261021-platforms"}, "depends_on": []},
    {"id": "W5", "action": "place_batch", "slots": {"blueprint": "BP_Platform", "layout": "20261020-platform"}, "depends_on": ["W4"]},
    {"id": "W6", "action": "place_batch", "slots": {"blueprint": "BP_Door", "layout": "20261020-door"}, "depends_on": []},
    {"id": "W7", "action": "place_batch", "slots": {"blueprint": "BP_Progress", "layout": "20261020-progress"}, "depends_on": []},
]

# 理解器那次自发的内容条款（原文截取，id/文本为真实值）
UNDERSTOOD_CRITERIA = [
    {"id": "c-coins-placed", "text": "关卡 L_NewTest 中已按设计图 20261020-coins 批量放置 BP_Coin，数量与设计图一致", "evidence_kind": "editor_assert", "passed": False},
    {"id": "c-platforms-placed", "text": "关卡 L_NewTest 中已按设计图 20261021-platforms 批量放置 BP_Platform，数量与设计图一致", "evidence_kind": "editor_assert", "passed": False},
    {"id": "c-platform-placed", "text": "关卡 L_NewTest 中已按设计图 20261020-platform 批量放置 BP_Platform，数量与设计图一致", "evidence_kind": "editor_assert", "passed": False},
    {"id": "c-door-placed", "text": "关卡 L_NewTest 中已按设计图 20261020-door 批量放置 BP_Door，数量与设计图一致", "evidence_kind": "editor_assert", "passed": False},
    {"id": "c-progress-placed", "text": "关卡 L_NewTest 中已按设计图 20261020-progress 批量放置 BP_Progress，数量与设计图一致", "evidence_kind": "editor_assert", "passed": False},
]


def check_real_shape_stays_false() -> None:
    """③ 主回归：真实形态下，窗口全败时条款必须保持 false。"""
    from agent.harness import build_criteria
    from agent.plan_windows import _gate_scene

    query = ("打开编辑器，打开关卡 L_NewTest，按设计图 20261020-coins 批量放置 BP_Coin，"
             "按设计图 20261021-platforms 批量放置 BP_Platform，"
             "按设计图 20261020-platform 批量放置 BP_Platform，"
             "按设计图 20261020-door 批量放置 BP_Door，"
             "按设计图 20261020-progress 批量放置 BP_Progress，然后保存关卡")
    gated = _gate_scene(query, RUN_WINDOWS, [dict(item) for item in UNDERSTOOD_CRITERIA])
    criteria = build_criteria(gated["criteria"], gated["windows"])
    by_id = {str(item.get("id")): item for item in criteria}

    assert by_id, "合同里一条条款都没有"
    for item in criteria:
        assert item.get("passed") is False, f"{item.get('id')} 起手就不是 false"
    # 每条内容条款都被重绑到自己的放置扇，不再宽绑
    for cid, wid in (("c-coins-placed", "W3"), ("c-platforms-placed", "W4"),
                     ("c-platform-placed", "W5"), ("c-door-placed", "W6"),
                     ("c-progress-placed", "W7")):
        owners = by_id[cid].get("windows")
        assert isinstance(owners, list) and owners == [wid], (
            f"{cid} 应该只绑 {wid}，实际 {owners}")

    # 窗口层判词回放：W1 launch 成功（synthetic）、W2 open 成功——条款一个都不许翻
    from agent.harness import apply_judge
    criteria = apply_judge(criteria, {"ok": True, "kind": "editor_assert", "window": "W1",
                                      "synthetic": True, "summary": "编辑器已在运行，MCP 已连通。"})
    criteria = apply_judge(criteria, {"ok": True, "kind": "editor_assert", "window": "W2",
                                      "summary": "已打开 /Game/Maps/L_NewTest；已成为当前关卡。"})
    for item in criteria:
        assert item.get("passed") is False, (
            f"四扇 place_batch 全败，{item.get('id')} 却被 {item.get('windows')} 翻绿")


def check_launch_only_still_flips() -> None:
    """synthetic 不能把「打开编辑器」这类单扇句误伤成假失败。"""
    from agent.harness import build_criteria

    windows = [{"id": "W1", "action": "launch_editor", "slots": {}, "depends_on": []}]
    criteria = build_criteria(None, windows)
    assert len(criteria) == 1 and criteria[0]["evidence_kind"] == "editor_assert"
    assert criteria[0]["windows"] == ["W1"], f"兜底条款应绑 W1，实际 {criteria[0].get('windows')}"
    from agent.harness import apply_judge
    flipped = apply_judge(criteria, {"ok": True, "kind": "editor_assert", "window": "W1",
                                     "synthetic": True, "summary": "编辑器已连通"})
    assert flipped[0]["passed"] is True, "证明窗口恰好只有 launch 自己的条款应能被翻"


def check_synthetic_never_flips_wide() -> None:
    """② synthetic 判词对宽绑定条款一律不动，哪怕 kind 对得上。"""
    from agent.harness import apply_judge

    rows = [{"id": "c-x", "text": "内容条款", "evidence_kind": "editor_assert", "passed": False,
             "windows": ["W1", "W2"]}]
    out = apply_judge(rows, {"ok": True, "kind": "editor_assert", "window": "W1", "synthetic": True})
    assert out[0]["passed"] is False
    out = apply_judge(rows, {"ok": True, "kind": "editor_assert", "window": "W1"})  # 无标记照旧
    assert out[0]["passed"] is True, "非 synthetic 判词行为不能变"


def check_gate_covered_rebind() -> None:
    """① 闸门对「已被既有放置扇接住」的设计图补出绑到具体扇的条款。"""
    from agent.scene_gate import ensure_scene_windows

    query = "打开关卡 L_NewTest，按设计图 20261020-coins 批量放置 BP_Coin"
    result = ensure_scene_windows(query, RUN_WINDOWS, [])
    assert result["added"] == [], "设计图已被 W3 接住，不该重复补扇"
    # BP_Progress 是 _IMPLIED：关卡句自动带上，W7 也是被既有扇接住的布局
    expect = {"c-scene-20261020-coins": ["W3"], "c-scene-20261020-progress": ["W7"]}
    by_id = {str(item.get("id")): item for item in result["criteria"]}
    for cid, wids in expect.items():
        row = by_id.get(cid)
        assert row is not None, f"覆盖已有放置扇时也要补出 {cid}"
        assert row.get("windows") == wids, f"{cid} 应绑 {wids}，实际 {row.get('windows')}"


def check_pickup_claim_not_rebound() -> None:
    """只提实体不提摆放的条款（可拾取）不被抢绑到放置扇。"""
    from agent.scene_gate import _rebind_covered

    wanted = [{"blueprint": "BP_Coin", "stem": "20261020-coins", "file": "20261020-coins.json",
               "names": ["Coin_1"]}]
    file_map = {"20261020-coins": ["W3"]}
    rows = [
        {"id": "c-place", "evidence_kind": "editor_assert", "text": "金币已批量放置", "passed": False},
        {"id": "c-pickup", "evidence_kind": "editor_assert", "text": "金币可拾取", "passed": False},
        {"id": "c-auto", "evidence_kind": "automation", "text": "金币已批量放置", "passed": False},
        {"id": "c-owned", "evidence_kind": "editor_assert", "text": "金币已批量放置", "passed": False, "windows": ["W9"]},
    ]
    _rebind_covered(rows, wanted, file_map, {})
    assert rows[0].get("windows") == ["W3"], "摆放条款应绑到放置扇"
    for row in rows[1:3]:
        assert not row.get("windows"), f"{row.get('id')} 不该被抢绑（{row.get('windows')}）"
    assert rows[3].get("windows") == ["W9"], "已有显式归属的条款不能被覆盖"


def main() -> None:
    check_real_shape_stays_false()
    check_launch_only_still_flips()
    check_synthetic_never_flips_wide()
    check_gate_covered_rebind()
    check_pickup_claim_not_rebound()
    print("criteria binding ok")


if __name__ == "__main__":
    main()
