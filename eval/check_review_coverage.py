# eval/check_review_coverage.py
"""D6 人审覆盖校验 + D2（brief 侧）缺口 fail-fast + D9 未交付清单出口。

真事（20261024-192858）：13 条交付条目、3 个范围，只有 A（启动编辑器）真的有窗口；
B（新建关卡 L_NewTest2）/ C（FPS 玩法）是 `windows: []`，而人审记录是「审阅 1：通过」
（data/runs/20261024/20261024-192858.json:8-11）。三个成因叠加：

1. 顺序：人审（run_windows.py:2458 brief_flow）在物化（:2470 materialize_scopes）之前，
   人审看到的是未物化的任务书，窗口缺失要等确认之后才暴露；
2. render_brief_md 是固定三列（任务范围 / 尚未完成 / 估时），没有覆盖列；
3. _print_brief_pretty 只在 `wins` 非空时画「窗口：…」行 ⇒ 缺口的视觉表现是**空白**，
   而空白与「一切正常」在人眼里无法区分。

本用例钉五件事：
1) 失败 run 形状的任务书渲染 ⇒ 含「窗口：0 扇」或「能力缺口」，B/C 两行的覆盖信息非空；
2) 存在零窗口范围时 brief_flow 空输入**不得**返回 confirm（必须循环要显式表态），
   喂正确确认词后通过且 gap_ack.acknowledged 为真；
3) dry-run 预演不写盘（data/skills/action_candidates.json 内容不变）；
4) 反例守卫：每范围都有窗口的正常任务书，人审文本与旧版**逐字一致**，空输入仍 = 通过；
5) D9：intent.unresolved 进任务书 md 的「尚未完成清单」，两条原文都在。

不打开编辑器、不调模型、不联网：生成器是假的 complete，补窗路径按需打桩，
目录全部用 tempfile.TemporaryDirectory() 隔离（briefs / ledger / skills 候选文件）。
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
REAL_RUN = ROOT / "data" / "runs" / "20261024" / "20261024-192858.json"
QUERY = "打开编辑器，新建一个关卡，名字为L_NewTest2，在这个关卡实现逻辑如下：需要实现简易的FPS射击游戏，内容你自由发挥"

# data/events/20261024-192858.jsonl 第 2 行的两条 unresolved 原文（[事实]）。
# 文件不在时用同一份原文：这两条是冻结证据，不是从模型现场生成的东西。
UNRESOLVED = [
    "新建一个关卡，名字为L_NewTest2（动作表无新建关卡动作）",
    "在这个关卡实现简易的FPS射击游戏逻辑（内容自由发挥，无点名类/蓝图/资产，无法落到具体动作）",
]

# 反例守卫的基线：**改动前**（T2 之后、D6 之前）的 render_brief_md / _print_brief_pretty
# 对本文件 NORMAL_BRIEF 的逐字输出。改人审门时不许给正常任务加噪音，这份字面值就是标尺。
OLD_MD_REPR = (
    '# 任务书\\n\\n- 原句：给项目做新功能：血量系统\\n\\n'
    '| 任务范围 | 尚未完成的全部主要内容 | 估时区间（小时） |\\n| --- | --- | --- |\\n'
    '| A 局外客户端 | - 登录、选角<br>- 钱包与背包<br>现状：代码已有，缺接线<br>依赖：无 | 16–40 |\\n'
    '| B 外部等待 | - 等 M-GO 接口<br>依赖：A<br>等外部：M-GO | 0.5–2 |\\n'
    '| （末行）累计集成、验证与交付 | - 对修改做一次只读复验 | 2–8 |\\n\\n'
    '## 外部依赖（目前依赖）\\n\\n- M-GO：正式契约。状态：服务未恢复\\n\\n'
    '## 总工作量\\n\\n- 合计：18.5–50 小时（估算不作为执行预算）\\n'
    '- 免责：任务之间有重叠；估算是粗的，不构成承诺；不含等待外部依赖的时间。\\n'
)
OLD_PRETTY_REPR = (
    '                                 任务书·人审门                                 \\n'
    '┌────────────────────────────┬───────────────────────────────────┬────────────┐\\n'
    '│ 范围                       │ 尚未完成的全部主要内容            │ 估时(小时) │\\n'
    '├────────────────────────────┼───────────────────────────────────┼────────────┤\\n'
    '│ A 局外客户端               │ • 登录、选角                      │      16–40 │\\n'
    '│                            │ • 钱包与背包                      │            │\\n'
    '│                            │ 现状：代码已有，缺接线            │            │\\n'
    '│                            │ 依赖：无                          │            │\\n'
    '│                            │ 窗口：W1 set_pickable             │            │\\n'
    '├────────────────────────────┼───────────────────────────────────┼────────────┤\\n'
    '│ B 外部等待                 │ • 等 M-GO 接口                    │      0.5–2 │\\n'
    '│                            │ 依赖：A                           │            │\\n'
    '│                            │ 等外部：M-GO                      │            │\\n'
    '│                            │ 窗口：W2 judge_pickup             │            │\\n'
    '├────────────────────────────┼───────────────────────────────────┼────────────┤\\n'
    '│ [INT] 累计集成、验证与交付 │ • 对修改做一次只读复验            │        2–8 │\\n'
    '│                            │ 依赖：无                          │            │\\n'
    '└────────────────────────────┴───────────────────────────────────┴────────────┘\\n'
    '外部依赖：M-GO（服务未恢复）\\n'
    '合计：18.5–50 小时（估算只记录，不作为执行预算）\\n'
)

NORMAL_BRIEF = {
    "query": "给项目做新功能：血量系统",
    "scopes": [
        {"id": "A", "title": "局外客户端", "deliverables": ["登录、选角", "钱包与背包"],
         "pending_note": "代码已有，缺接线", "estimate_hours": [16, 40], "depends_on": [],
         "external_dep": "",
         "windows": [{"id": "W1", "title": "把 BP_TestActor 设为可拾取", "action": "set_pickable",
                      "slots": {"target": "BP_TestActor"}, "depends_on": [], "max_retries": 1}]},
        {"id": "B", "title": "外部等待", "deliverables": ["等 M-GO 接口"], "depends_on": ["A"],
         "external_dep": "M-GO",
         "windows": [{"id": "W2", "title": "裁决", "action": "judge_pickup",
                      "slots": {"target": "BP_TestActor", "picker": "BP_TestPawn"},
                      "depends_on": ["W1"], "max_retries": 0}]},
    ],
    "integration": {"title": "累计集成、验证与交付", "deliverables": ["对修改做一次只读复验"]},
    "external_dependencies": [{"name": "M-GO", "provides": "正式契约", "status": "服务未恢复"}],
}


def _quiet(*_args, **_kwargs):
    return None


@contextlib.contextmanager
def _silent():
    """把富文本表格（rich 的 Console 直接写 stdout，绕过 printer）与阶段日志一起吞掉，
    只留断言自己的输出。断言失败时异常照常抛出，不会被这里吞。"""
    from core import console as console_mod

    buf = io.StringIO()
    saved_sink = getattr(console_mod, "_status_sink", None)
    console_mod.set_status_sink(lambda _text: None)  # 「正在写任务书」这类一行进度
    try:
        with contextlib.redirect_stdout(buf):
            yield buf
    finally:
        console_mod.set_status_sink(saved_sink)


def _brief_flow_silent(*args, **kwargs):
    """brief_flow 跑一遍，控制台噪音屏蔽掉（人审表格本身不落盘，断言只看文件/返回结构）。"""
    from agent.brief import brief_flow

    with _silent():
        return brief_flow(*args, **kwargs)


# ================================================================ 夹具

def _inline_failed_brief() -> dict:
    """失败 run 任务书的同形状替身：只在 data/runs 那份不在时用。
    B/C 的 pending_note、交付清单、旧 block_note 与真实文件一字不差。"""
    from agent.brief import _LEGACY_ZERO_WINDOW_NOTE

    return {
        "query": QUERY,
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器并等待 MCP 端口连通",
             "deliverables": ["UE 编辑器进程启动完成", "MCP 端口可连通，后续动作可下发"],
             "pending_note": "", "depends_on": [], "external_dep": "", "estimate_hours": [0.1, 0.5],
             "windows": [{"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
                          "slots": {}, "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "新建关卡 L_NewTest2",
             "deliverables": ["在 /Game 下创建关卡资产 L_NewTest2", "把 L_NewTest2 设为当前编辑关卡"],
             "pending_note": "动作表里没有建关卡的动作，需要在编辑器内完成；代码/资产尚无，"
                             "缺创建与切换当前关卡的接线和验证",
             "depends_on": ["A"], "external_dep": "", "estimate_hours": [2.0, 8.0],
             "windows": [], "block_note": _LEGACY_ZERO_WINDOW_NOTE},
            {"id": "C", "title": "在 L_NewTest2 内实现简易 FPS 射击玩法",
             "deliverables": ["玩家角色：移动、视角、开火输入", "射击命中判定与可击倒/可拾取目标",
                              "计分或结算反馈（HUD/日志择一）", "把上述内容放进 L_NewTest2 并完成接线",
                              "在关卡内跑通一次射击到命中的验证"],
             "pending_note": "原句只说“自由发挥”，未点名具体蓝图或资产；实现清单、资产命名与接线都还没定，也没验证",
             "depends_on": ["B"], "external_dep": "", "estimate_hours": [5.0, 20.0],
             "windows": [], "block_note": _LEGACY_ZERO_WINDOW_NOTE},
        ],
        "integration": {"title": "累计集成、验证与交付",
                        "deliverables": ["按 A→B→C 的顺序串起，确认编辑器已连通后再操作关卡",
                                         "在 L_NewTest2 内完整跑一次：启动→移动→射击→命中反馈",
                                         "对修改做一次只读复验", "记录未接线/未验证的遗留条目"],
                        "estimate_hours": [2.0, 8.0]},
        "external_dependencies": [],
    }


def _real_failed_brief() -> tuple[dict, bool]:
    """(失败 run 的任务书原文, 是否真读到仓库里那份)。缺文件时用同形状替身，
    并在输出里说清楚，不假装验过真实件。"""
    if REAL_RUN.is_file():
        data = json.loads(REAL_RUN.read_text(encoding="utf-8"))
        brief = data.get("brief")
        if isinstance(brief, dict) and brief.get("scopes"):
            return brief, True
    return _inline_failed_brief(), False


def _patch_scope_windows(fake):
    """把补窗入口换成替身。返回恢复句柄（没落地时连属性一起删）。"""
    from agent import plan_windows

    had = hasattr(plan_windows, "scope_windows")
    saved = getattr(plan_windows, "scope_windows", None)
    plan_windows.scope_windows = fake
    return saved, had


def _restore_scope_windows(saved, had) -> None:
    from agent import plan_windows

    if had:
        plan_windows.scope_windows = saved
        return
    try:
        delattr(plan_windows, "scope_windows")
    except AttributeError:
        pass


def _validated(raw: dict) -> dict:
    from agent.brief import validate_brief

    return validate_brief(json.loads(json.dumps(raw, ensure_ascii=False)), raw["query"])


def _render(brief: dict) -> tuple[str, str]:
    """(md 文本, 人审门表格的控制台文本)。表格走 rich，输出进 stdout 由这里抓。"""
    from agent.brief import _print_brief_pretty, render_brief_md

    md = render_brief_md(brief)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_brief_pretty(brief, printer=lambda text: print(text))
    return md, buf.getvalue()


def _candidates_snapshot() -> bytes | None:
    """候选表当前字节内容；文件不存在返回 None。"""
    from agent import catalog as catalog_mod

    path = catalog_mod.candidates_path()
    return path.read_bytes() if path.is_file() else None


# ================================================================ 断言 1：覆盖必须被看见

def check_failure_brief_shows_coverage() -> bool:
    """失败 run 形状：人审文本必须给出每范围的覆盖，零窗口行必须同时喊「能力缺口」。
    返回是否用到真实文件。

    这里只钉「一致性与非空」，不钉「C 一定是缺口」——补窗能力将来可能变强，
    C 真被补出窗口时它就该显示「窗口：1 扇」而不是继续喊缺口。
    补不出来必喊缺口这条，由 check_rescue_variant_is_a_gap 用打桩钉死。"""
    from agent.brief import gap_scopes, preview_coverage

    raw, is_real = _real_failed_brief()
    brief = _validated(raw)
    # 人审时的一次 dry-run 预演（与 brief_flow 里的调用同一个函数）
    coverage = preview_coverage(brief)
    assert isinstance(coverage.get("scopes"), list) and coverage["scopes"], coverage
    assert coverage.get("total") is not None, coverage

    md, table = _render(brief)

    # 表格列可以加，但原来的三列语义不许丢
    for header in ("任务范围", "尚未完成的全部主要内容", "估时区间"):
        assert header in md, ("旧表头不见了", header, md)
    assert "窗口覆盖" in md, ("人审件没有覆盖列", md)

    # 表格型渲染：rich 的框线会被截断，断言改成「不是空白」+ 「零窗口行必须喊缺口」
    assert "窗口：" in table, ("人审表格里没有任何覆盖信息", table)

    # 每一条范围行都要有覆盖信息（不能是空白）
    lines = [line for line in md.splitlines() if line.startswith("| ") and "---" not in line
             and "任务范围" not in line]
    scope_lines = [line for line in lines if not line.startswith("| （末行）")]
    assert scope_lines, md
    by_id: dict[str, str] = {}
    for line in scope_lines:
        sid = line[2:].split(" ", 1)[0]
        by_id[sid] = line
        assert "窗口：" in line, ("这一行没有覆盖信息（缺口的表现是空白）", line)

    # B、C 两行的覆盖信息非空
    for sid in ("B", "C"):
        assert sid in by_id, (sid, md)
        assert "窗口：" in by_id[sid], (sid, by_id[sid])

    # 顺序断言：人审显示的覆盖数 == 预演算出来的那份（不是另算的一套数）
    counts = {row["id"]: row["count"] for row in coverage["scopes"] if isinstance(row, dict)}
    assert set(counts) >= {"B", "C"}, counts
    for sid, count in counts.items():
        if sid not in by_id:
            continue
        assert f"窗口：{count} 扇" in by_id[sid], (
            "预演结果与人审显示的覆盖数不一致", sid, count, by_id[sid])
        if count == 0:
            scope = next(item for item in brief["scopes"] if str(item.get("id")) == sid)
            if not scope.get("blocked") and not str(scope.get("external_dep") or "").strip():
                # 零窗口 + 非暂停 + 不等外部 ⇒ 人审件里必须同时说出「能力缺口」
                assert "能力缺口" in by_id[sid], (
                    "零窗口范围在 md 里没喊能力缺口", sid, by_id[sid])
                assert sid in [str(item.get("id")) for item in gap_scopes(brief)], (
                    "零窗口范围没进缺口清单", sid, gap_scopes(brief))

    # 有缺口时，md 的「能力缺口」小节与告知协议的话必须在
    gaps = gap_scopes(brief)
    if gaps:
        assert "能力缺口（人审必须逐条表态）" in md, md
        for gap in gaps:
            assert str(gap.get("id") or "").strip(), gap
            assert gap.get("note"), gap
        for line in md.split("能力缺口（人审必须逐条表态）", 1)[1].splitlines():
            if line.startswith("- 范围 "):
                assert "窗口：0 扇" in line, line
    return is_real


def check_rescue_variant_is_a_gap() -> None:
    """补窗也补不出来时（打桩 scope_windows 返回 []）：B、C 都必须显式写「窗口：0 扇」+ 能力缺口。"""
    from agent.brief import gap_scopes, preview_coverage

    raw, _is_real = _real_failed_brief()
    brief = _validated(raw)
    handle = _patch_scope_windows(lambda scope, query="": [])
    try:
        coverage = preview_coverage(brief)
        md, table = _render(brief)
    finally:
        _restore_scope_windows(*handle)
    assert [row["id"] for row in coverage["scopes"]] == ["A", "B", "C"], coverage["scopes"]
    counts = {row["id"]: row["count"] for row in coverage["scopes"]}
    assert counts["A"] == 1 and counts["B"] == 0 and counts["C"] == 0, counts
    for sid in ("B", "C"):
        row = next(line for line in md.splitlines() if line.startswith(f"| {sid} "))
        assert "窗口：0 扇" in row, (sid, row)
        assert "能力缺口" in row, (sid, row)
    assert "能力缺口" in md and "能力缺口" in table, (md, table)
    gaps = gap_scopes(brief)
    assert [item["id"] for item in gaps] == ["B", "C"], gaps
    # 缺口要带「缺什么」：B 的 pending_note 里写了「没有建关卡的动作」⇒ 抠得出「建关卡」
    b_gap = next(item for item in gaps if item["id"] == "B")
    assert b_gap.get("missing_actions") == ["建关卡"], b_gap
    assert "建关卡" in md, ("人审件里没写缺什么", md)


# ================================================================ 断言 2：缺口不许空输入通过

def check_gap_flow_requires_explicit_ack(directory: Path) -> None:
    """存在零窗口范围时：空输入不返回 confirm，必须循环；喂「确认缺口」才通过且 gap_ack 为真。"""
    from agent.brief import GAP_ACK_PHRASE, brief_flow

    raw, _is_real = _real_failed_brief()
    answers = ["", "", GAP_ACK_PHRASE]
    seen: list[str] = []
    prompts: list[str] = []

    def reader(prompt=""):
        prompts.append(str(prompt))
        value = answers[len(seen)] if len(seen) < len(answers) else GAP_ACK_PHRASE
        seen.append(value)
        return value

    def fake_complete(_messages):
        return json.dumps(raw, ensure_ascii=False)

    handle = _patch_scope_windows(lambda scope, query="": [])
    try:
        outcome = _brief_flow_silent(QUERY, windows=[], complete=fake_complete, reader=reader,
                             printer=_quiet, directory=directory)
    finally:
        _restore_scope_windows(*handle)
    assert outcome is not None, "任务书写不出来"
    status, brief, md, js = outcome
    # 空输入不许等于确认：前两次空输入必须被吃掉，第三次显式表态才放行
    assert seen == ["", "", GAP_ACK_PHRASE], ("空输入被当成确认了", seen)
    assert len(prompts) == len(seen), (prompts, seen)
    assert status == "confirm", status
    # status 的取值集合不变（run_windows 侧依赖 confirm / abort）
    assert status in ("confirm", "abort"), status
    ack = brief.get("gap_ack")
    assert isinstance(ack, dict) and ack.get("acknowledged") is True, ack
    assert [str(item) for item in ack.get("scopes") or []] == ["B", "C"], ack
    assert ack.get("escalated") is True, ack
    assert md.is_file() and js.is_file()
    written = md.read_text(encoding="utf-8")
    assert "窗口：0 扇" in written or "能力缺口" in written, written
    # 提示语必须写清协议（否则人不知道「确认缺口」是什么）
    assert any(GAP_ACK_PHRASE in text for text in prompts), prompts

    # 反例：缺口任务书里「放弃」必须 abort，且 gap_ack 记 false（不假装确认过）
    outcome2 = None
    handle = _patch_scope_windows(lambda scope, query="": [])
    try:
        outcome2 = _brief_flow_silent(QUERY, windows=[], complete=fake_complete,
                              reader=lambda _p="": "放弃", printer=_quiet, directory=directory)
    finally:
        _restore_scope_windows(*handle)
    assert outcome2 is not None
    status2, brief2, _md2, _js2 = outcome2
    assert status2 == "abort", status2
    ack2 = brief2.get("gap_ack")
    assert isinstance(ack2, dict) and ack2.get("acknowledged") is False, ack2


def check_escalation_bypass(directory: Path) -> None:
    """UE_ESCALATE_GAPS=false 时只记录不阻塞（§3.2 允许），但默认必须是开。"""
    from agent.brief import GAP_ACK_PHRASE, _escalate_gaps, brief_flow

    saved = os.environ.get("UE_ESCALATE_GAPS")
    if saved is None:
        assert _escalate_gaps() is True, "默认必须是开：不许默认放过缺口"
    raw, _is_real = _real_failed_brief()

    def fake_complete(_messages):
        return json.dumps(raw, ensure_ascii=False)

    handle = _patch_scope_windows(lambda scope, query="": [])
    os.environ["UE_ESCALATE_GAPS"] = "false"
    try:
        assert _escalate_gaps() is False
        outcome = _brief_flow_silent(QUERY, windows=[], complete=fake_complete,
                             reader=lambda _p="": "", printer=_quiet, directory=directory)
    finally:
        _restore_scope_windows(*handle)
        if saved is None:
            os.environ.pop("UE_ESCALATE_GAPS", None)
        else:
            os.environ["UE_ESCALATE_GAPS"] = saved
    assert outcome is not None
    status, brief, _md, _js = outcome
    # 旁路：空输入仍然通过（只记录），但缺口本身照样记在 gap_ack / coverage 里
    assert status == "confirm", status
    ack = brief.get("gap_ack")
    assert isinstance(ack, dict) and ack.get("acknowledged") is True, ack
    assert ack.get("escalated") is False, ack
    assert GAP_ACK_PHRASE  # 协议常量存在
    if saved is None:
        assert _escalate_gaps() is True, "旁路用完必须回到默认开"


# ================================================================ 断言 3：预演不写盘

def check_preview_is_dry_run(directory: Path) -> None:
    """dry-run 预演前后 data/skills/action_candidates.json 字节内容不变（或一直不存在）。

    光用失败 run 的夹具验不出写盘：那边补窗补出来的是表内动作（create_level），
    `materialize` 走不到 `note_candidate`。所以这里再造一个**补出表外动作**的夹具：
    scope_windows 返回一扇 action 不在动作表里的窗口 ⇒ 若 monkeypatch 失效，
    `materialize` 会调 `note_candidate` 写候选表（[事实] agent/plan_windows.py:568-572）。"""
    from agent.brief import preview_coverage

    before = _candidates_snapshot()
    raw, _is_real = _real_failed_brief()
    brief = _validated(raw)
    preview_coverage(brief)
    preview_coverage(brief)  # 跑两次也不能有一次落盘
    after = _candidates_snapshot()
    assert before == after, (
        "dry-run 预演写盘了：候选表内容被人审前的预演改掉了",
        len(before or b""), len(after or b""),
    )

    # 关键夹具：预演路径上真有「表外动作」时也不许落盘。
    # 必须让替身**真去调 materialize**（而不是自己造一个 dict）：只有走 materialize 的
    # 表外分支才会碰 `note_candidate`（[事实] agent/plan_windows.py:568-572），
    # 替身自己捏一个 dict 绕过了那条腿，等于什么都没验。
    def out_of_catalog_scope_windows(scope, query=""):
        from agent.plan_windows import materialize

        return [materialize("no_such_action_preview", {}, title="表外动作",
                            wid="SBW1", depends=[], retries=1)]

    outside = _validated(raw)
    handle = _patch_scope_windows(out_of_catalog_scope_windows)
    try:
        coverage = preview_coverage(outside)
    finally:
        _restore_scope_windows(*handle)
    assert _candidates_snapshot() == before, (
        "预演走了表外动作分支，候选表被写脏了（_no_candidate_writes 没盖住）")
    b_row = next(row for row in coverage["scopes"] if row["id"] == "B")
    assert b_row["count"] == 1 and b_row["runnable"] == 0, b_row
    assert b_row["gap"] is True, ("补出一扇暂停窗不算覆盖", b_row)
    md, _table = _render(outside)
    b_line = next(line for line in md.splitlines() if line.startswith("| B "))
    assert "可跑 0 扇" in b_line, ("暂停窗被当成真覆盖了", b_line)
    assert "能力缺口" in b_line, b_line

    # brief_flow 整条人审链也不许在预演阶段写盘
    from agent.brief import brief_flow

    def fake_complete(_messages):
        return json.dumps(raw, ensure_ascii=False)

    handle = _patch_scope_windows(lambda scope, query="": [])
    try:
        outcome = _brief_flow_silent(QUERY, windows=[], complete=fake_complete,
                             reader=lambda _p="": "确认缺口", printer=_quiet, directory=directory)
    finally:
        _restore_scope_windows(*handle)
    assert outcome is not None
    assert _candidates_snapshot() == before, "人审门把候选表写脏了"

    # 反向守卫：**人审之后**的 materialize_scopes 该怎么记还怎么记 —— 上面「不写盘」是
    # 预演专用的（临时 monkeypatch），不是把候选闭环这条腿卸掉了。
    # 用一条表外动作的范围逼出 note_candidate，断言候选表这次真的被写了。
    from agent.brief import materialize_scopes

    table_out_of_catalog = {
        "query": "一个动作表外的需求",
        "scopes": [{
            "id": "A", "title": "表外动作", "deliverables": ["做一件表里没有的事"],
            "pending_note": "", "depends_on": [], "external_dep": "",
            "windows": [{"id": "W1", "title": "表外", "action": "no_such_action_xyz",
                         "slots": {}, "depends_on": [], "max_retries": 1}],
        }],
        "integration": {},
        "external_dependencies": [],
    }
    with _silent():
        materialize_scopes(_validated(table_out_of_catalog))
    written = _candidates_snapshot()
    assert written != before, "materialize 的候选闭环被顺手卸掉了（表外动作不再记候选）"
    assert b"no_such_action_xyz" in written, written


# ================================================================ 断言 4：反例守卫

def check_normal_brief_is_unchanged(directory: Path) -> None:
    """每范围都有窗口的正常任务书：人审文本与旧版逐字一致，且空输入仍 = 通过。"""
    from agent.brief import brief_flow, gap_scopes, preview_coverage

    brief = _validated(NORMAL_BRIEF)
    brief["stamp"] = ""
    preview_coverage(brief)
    assert not gap_scopes(brief), ("正常任务书被误判成缺口", gap_scopes(brief))
    md, table = _render(brief)
    assert md == OLD_MD_REPR.replace("\\n", "\n"), (
        "正常任务书的 md 与旧版不一致（不许给正常任务加噪音）", md)
    assert table == OLD_PRETTY_REPR.replace("\\n", "\n"), (
        "正常任务书的人审表格与旧版不一致（不许给正常任务加噪音）", table)
    assert "窗口覆盖" not in md, "正常任务书不该多出一列"
    assert "能力缺口" not in md and "能力缺口" not in table

    # 空输入仍然 = 通过（缺省回车没有被动过）
    raw = json.loads(json.dumps(NORMAL_BRIEF, ensure_ascii=False))

    def fake_complete(_messages):
        return json.dumps(raw, ensure_ascii=False)

    outcome = _brief_flow_silent(NORMAL_BRIEF["query"], windows=[], complete=fake_complete,
                         reader=lambda _p="": "", printer=_quiet, directory=directory)
    assert outcome is not None
    status, brief2, md2, _js = outcome
    assert status == "confirm", ("正常任务书的空输入不再等于通过", status)
    assert "gap_ack" not in brief2, ("正常任务书不该有缺口表态字段", brief2.get("gap_ack"))
    assert brief2["review"]["status"] == "通过", brief2["review"]


# ================================================================ 断言 5：D9 未交付清单出口

def check_unresolved_reaches_brief_md(directory: Path) -> None:
    """intent.unresolved 必须进任务书 md 的「尚未完成清单」；不传时行为与旧版一致。"""
    from agent.brief import brief_flow, generate_brief, render_brief_md

    raw, _is_real = _real_failed_brief()

    def fake_complete(_messages):
        return json.dumps(raw, ensure_ascii=False)

    # (a) generate_brief 带 unresolved
    brief = generate_brief(QUERY, windows=[], complete=fake_complete, unresolved=UNRESOLVED)
    assert brief is not None
    assert brief.get("unresolved") == UNRESOLVED, brief.get("unresolved")
    md = render_brief_md(brief)
    assert "尚未完成清单" in md, ("unresolved 没触发「尚未完成清单」章节", md)
    section = md.split("尚未完成清单", 1)[1]
    assert "新建一个关卡" in section, ("第一条 unresolved 没进清单", section)
    for token in ("FPS", "射击"):
        assert token in section, ("FPS 那条没进清单", token, section)

    # (b) 不传 unresolved：行为与今天完全一致（不出现这个键、不渲染这一节）
    brief_plain = generate_brief(QUERY, windows=[], complete=fake_complete)
    assert brief_plain is not None
    assert "unresolved" not in brief_plain, brief_plain.keys()
    assert "尚未完成清单" not in render_brief_md(brief_plain), "不传 unresolved 却渲染了清单"

    # (c) brief_flow 透传：审阅落盘的 md 里也要有这一节
    handle_answers = iter(["确认缺口"])

    def reader(_prompt=""):
        return next(handle_answers, "确认缺口")

    handle = _patch_scope_windows(lambda scope, query="": [])
    try:
        outcome = _brief_flow_silent(QUERY, windows=[], complete=fake_complete, reader=reader,
                             printer=_quiet, directory=directory, unresolved=UNRESOLVED)
    finally:
        _restore_scope_windows(*handle)
    assert outcome is not None
    _status, brief3, md3, _js = outcome
    assert brief3.get("unresolved") == UNRESOLVED, brief3.get("unresolved")
    text = md3.read_text(encoding="utf-8")
    assert "尚未完成清单" in text, text
    section = text.split("尚未完成清单", 1)[1]
    for token in ("新建一个关卡", "L_NewTest2", "FPS"):
        assert token in section, (token, section)


# ================================================================ main

def _isolate(tmp: Path) -> dict:
    """把任务书目录、台账、候选表、技能路由全部指到临时目录，返回恢复用原值。"""
    from agent import brief as brief_mod
    from agent import ledger as ledger_mod

    saved = {"briefs": brief_mod.BRIEFS_DIR, "ledger": ledger_mod.LEDGER_PATH}
    brief_mod.BRIEFS_DIR = tmp / "briefs"
    ledger_mod.LEDGER_PATH = tmp / "ledger.json"
    return saved


def _restore(saved: dict) -> None:
    from agent import brief as brief_mod
    from agent import ledger as ledger_mod

    brief_mod.BRIEFS_DIR = saved["briefs"]
    ledger_mod.LEDGER_PATH = saved["ledger"]


def main() -> int:
    from agent import brief as brief_mod

    env_keys = ("UE_ACTION_CANDIDATES", "UE_LEARNED_ROUTES", "UE_LEARNED_SKILLS",
                "UE_ESCALATE_GAPS", "UE_PROMOTED_ACTIONS")
    prev_env = {key: os.environ.get(key) for key in env_keys}
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        os.environ["UE_ACTION_CANDIDATES"] = str(tmp / "skills" / "action_candidates.json")
        os.environ["UE_LEARNED_ROUTES"] = str(tmp / "skills" / "learned_routes.json")
        os.environ["UE_LEARNED_SKILLS"] = str(tmp / "skills" / "learned_skills.json")
        os.environ["UE_PROMOTED_ACTIONS"] = str(tmp / "skills" / "promoted_actions.json")
        os.environ.pop("UE_ESCALATE_GAPS", None)
        saved = _isolate(tmp)
        # 检索标题走真检索会加载模型：本用例只要标题，给空列表，不联网不加载
        saved_titles = brief_mod._retrieval_titles
        brief_mod._retrieval_titles = lambda query, k=3: []
        try:
            # 整条链包一层静音：generate_brief 里的阶段日志、rich 的人审表格都不该进 CI 输出
            with _silent():
                is_real = check_failure_brief_shows_coverage()
                check_rescue_variant_is_a_gap()
                check_gap_flow_requires_explicit_ack(tmp / "briefs")
                check_escalation_bypass(tmp / "briefs")
                check_preview_is_dry_run(tmp / "briefs")
                check_normal_brief_is_unchanged(tmp / "briefs")
                check_unresolved_reaches_brief_md(tmp / "briefs")
        finally:
            brief_mod._retrieval_titles = saved_titles
            _restore(saved)
            for key, value in prev_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    if not is_real:
        print("  失败 run 的任务书用内联替身验的（data/runs 那份不在）")
    print("review coverage ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
