# agent/statuses.py
"""状态词表的**唯一真值源**（D11）。加一个状态，必须先改这里，再改用到它的模块。

今天的漂移现场（`[事实]` 2026-10-24 23:27–23:35 基准工作树实测）：同一个状态概念有四处声明点，
20261024-192858 的整改只往其中一处加了「未完成」（`agent/harness.py` 的 `contract_status`
返回它），其余三处没同时登记，于是渲染层与台账各说各话：
1. `agent/harness.py` 的 `STOP_REASONS`（元组，`run_stop` 按它挑 stop_reason）→ 本文件 `STOP_REASONS`
2. `agent/run_windows.py` 的 `_MD_STATUS`（窗口状态 → 中文名，实测 10 个键）→ 本文件 `WINDOW_STATE_LABELS`
3. `agent/brief.py` 的 `unfinished_text` 里那段台账标签 dict（内联字面量，没有模块级符号）→ `LEDGER_STATUS_LABELS`
4. `agent/ledger.py` 写的 `status` 字段 + run 记录 `status` 字段 → `LEDGER_STATUSES` / `RUN_STATUSES`

约定（改本文件前先读这四条）：
- **零 import、零副作用。** `eval/check_statuses.py` 先 import 本模块再看 `sys.modules`，
  以此证明「导入状态词表不会拉起 config / 编辑器 / 模型 / 检索」——所以这里只许写字面量。
- 三张身份表由各自的标签表 `frozenset(标签表)` 派生 ⇒「有状态没标签」在结构上不可能发生。
  `[事实]` 今天 `WINDOW_STATES` 的成员与 `_MD_STATUS` 的键完全相等（10 个 vs 10 个）。
- 标签查询一律 `.get(x, x)` 回落：未知字符串**原样返回，绝不抛异常**。渲染层依赖这个宽松行为
  （`run_windows` 三处 `_MD_STATUS.get(entry["state"], entry["state"])`）——`[事实]` 状态是外部
  数据（旧 run 记录、人工改过的任务书），词表不认识的值不能让渲染炸掉。
- **不在本文件范围**（它们是别的词表，D11 只收 run / 窗口 / 台账 / stop_reason 四张）：
  动作候选与技能生命周期（review / approved / promoted / shadow / rejected / route）、
  记忆 md 的 front-matter status（active）、任务书人审状态（待确认 / 通过）、
  `brief_flow` 的返回状态（confirm / abort）。`eval/check_statuses.py` 的静态扫描遇到它们时
  **逐条显式跳过并写明原因**，不用宽泛异常吞掉。

为什么没有让 `agent/ledger.py` 反向 `import` 本表（可选改动，本次刻意不做，`[推测]` 属工程判断）：
`ledger.py` 本来就 `from config import PROJECT_ROOT`，让它再依赖本表只会增加耦合，守卫强度却不加一分——
漂移是 `eval/check_statuses.py` 的静态扫描在兜的，不靠运行期引用。

维护者：人。模型可以提议（写进 D8 的候选表），但不能改本文件。
"""
from __future__ import annotations

__all__ = [
    "RUN_STATUSES",
    "RUN_RECORD_INFLIGHT",
    "WINDOW_STATES",
    "LEDGER_STATUSES",
    "STOP_REASONS",
    "REVIEW_COVERAGE_STATES",
    "RUN_STATUS_LABELS",
    "WINDOW_STATE_LABELS",
    "LEDGER_STATUS_LABELS",
    "is_run_status",
    "is_run_record_status",
    "is_window_state",
    "is_ledger_status",
    "is_stop_reason",
    "is_review_coverage_state",
    "run_label",
    "window_label",
    "ledger_label",
    "all_statuses",
]

# ---------------------------------------------------------------------------
# 1) run 级总状态：整句话的结论。写进 data/runs/<run_id>.json 的 status 与 run md 的「状态：」行。
# ---------------------------------------------------------------------------
# `[事实]` agent/run_windows.py 收尾分支的赋值（实测行号会随他人改动位移，以符号为准）：
#   status, note = "成功", ...            # 全部窗口 done
#   status, note = "失败", ...            # 没有窗口 / 人工裁决停止 / 有窗口没完成
#   status, note = "中断", ...            # KeyboardInterrupt
#   status = "部分完成"                    # 失败扇无下游依赖 / 等外部
# `[事实]` 「未完成」由 agent/harness.py 的 contract_status 在有 capability_gap 范围时返回
#   （20261024-192858 的整改引入；本文件的防漂断言专门钉它）。
# 中文原词就是人读件上的原词，所以标签是恒等映射——留这张表不是废话：渲染层从此只查表不拼字面量，
# 将来要改措辞（例如「失败」→「未通过」）只改这里一处。
RUN_STATUS_LABELS = {
    "成功": "成功",
    "部分完成": "部分完成",
    "未完成": "未完成",
    "失败": "失败",
    "中断": "中断",
}
RUN_STATUSES = frozenset(RUN_STATUS_LABELS)

# run 记录的 status 字段在收尾**之前**还有英文在途标记：`[事实]` `_RunState.__init__` 写
# "running"、Ctrl-C 分支写 "interrupted"。它们与 WINDOW_STATES 的成员同值，所以不重复登记进
# all_statuses()（并集里本来就有）；这里单独点名，是为了让「run 记录的 status 能取哪些值」有出处。
# "orphaned" 只出现在 run 记录上（超 30 分钟无心跳，agent/run_guard.py），不是窗口状态，
# 也不进五个中文结论。扫描面要额外并上这张表，否则赋值会被当成未登记状态。
RUN_RECORD_INFLIGHT = frozenset({"running", "interrupted", "orphaned"})

# ---------------------------------------------------------------------------
# 2) 窗口状态：data/runs/<run_id>.json 的 windows[*].state，以及 window_state 事件的 state。
# ---------------------------------------------------------------------------
# `[事实]` 与 agent/run_windows.py 的 `_MD_STATUS` 一一对应（实测 10 个键、10 个中文名，字面完全相同）。
# `[事实]` run_windows 另有 `_TERMINAL_STATES = {done, failed, paused, skipped, waiting_external}`
#   与 `_RESUMABLE_STATES = {queued, running, retry, failed, paused, waiting_external, revising, interrupted}`，
#   两者都是本集合的子集——它们是「分类」，不是第二份身份表，所以不进本文件。
WINDOW_STATE_LABELS = {
    "queued": "未开始",
    "running": "运行中",
    "retry": "重试",
    "done": "成功",
    "failed": "失败",
    "paused": "暂停",
    "skipped": "跳过",
    "waiting_external": "等待外部",
    "revising": "返工",
    "interrupted": "中断",
}
WINDOW_STATES = frozenset(WINDOW_STATE_LABELS)

# ---------------------------------------------------------------------------
# 3) 台账状态：agent/ledger.py 的 scopes[*].status，只给机器读（人读件由 brief 渲染成中文）。
# ---------------------------------------------------------------------------
# `[事实]` 键与 agent/brief.py `unfinished_text` 里 `{...}.get(status, status)` 的 dict 完全一致；
# 值是该 dict 的中文标签，一字不差。`[事实]` ledger.py 里默认值就是 "pending"
# （`str(scope.get("status") or "pending")`），`ledger_summary_text` 只对 "done" 做特殊处理。
LEDGER_STATUS_LABELS = {
    "done": "完成",
    "partial": "部分完成",
    "pending": "未完成",
}
LEDGER_STATUSES = frozenset(LEDGER_STATUS_LABELS)

# ---------------------------------------------------------------------------
# 4) stop_reason：run_end / windows[*].stop_reason 的取值。
# ---------------------------------------------------------------------------
# `[事实]` 与 agent/harness.py 的 `STOP_REASONS` 同值同序（元组顺序沿用原样，便于哪天直接
# `from agent.statuses import STOP_REASONS` 顶替，行为不变）。注意 "paused" 同时是窗口状态：
# 同一个词在两个词表里合法，故四表并集要去重。
STOP_REASONS = ("ok", "paused", "retry_exceeded", "read_stall", "judge_failed", "unresolved",
                "no_evidence")

# ---------------------------------------------------------------------------
# 5) 人审覆盖状态：`agent/brief._review_state` 给「任务书这一行在人审眼里算什么」的结论。
# ---------------------------------------------------------------------------
# `[事实]` 2026-10-24 D6 落地时引入（`agent/brief.py` 的 `_review_state`）：人审门要能区分
# 「这一行没有窗口是个真缺口」与「模型没给窗口但代码能补出来」。第三种 "" 是正常态。
# 这三个值是**人审渲染的判定结果**，不是 run/窗口状态；单独成表是因为 D11 的静态扫描
# 会把 `state == "gap"` 这类比较当成状态字面量 —— 它确实是，只是属主不同。
# 收在这里而不是加进 WINDOW_STATES：窗口状态是运行期机器状态，人审覆盖状态是渲染期结论，
# 混进同一张表会让 `_MD_STATUS` 的一致性断言失去意义。
REVIEW_COVERAGE_STATES = frozenset({"", "gap", "rescued"})

_ALL_STATUSES = RUN_STATUSES | WINDOW_STATES | LEDGER_STATUSES | frozenset(STOP_REASONS)
# RUN_RECORD_INFLIGHT 不进并集：它已包含在 WINDOW_STATES 里（同样两个串），重复计算只会让人
# 误以为还有第三份身份表。
# REVIEW_COVERAGE_STATES 同理不进并集：它是人审渲染结论，不是 run/窗口/台账/stop_reason 身份，
# 进并集会让「run 记录里的状态串必须都在册」这条反查失去意义（"" 会混进来）。


# ---------------------------------------------------------------------------
# 判定与标签（都不会抛异常：外部数据里出现词表不认识的值是常态）
# ---------------------------------------------------------------------------
def _registered(table: frozenset, value) -> bool:
    """先卡 `isinstance(str)`：frozenset 对不可哈希输入（list/dict）会抛 TypeError，那是异常不是判定。"""
    return isinstance(value, str) and value in table


def is_run_status(x) -> bool:
    """x 是不是 run 级总状态（五选一）。别名在途标记 running/interrupted 不算——见 is_run_record_status。"""
    return _registered(RUN_STATUSES, x)


def is_run_record_status(x) -> bool:
    """run 记录 data/runs/*.json 的 status 字段能取的值：五个中文结论 + 两个英文在途标记。"""
    return is_run_status(x) or _registered(RUN_RECORD_INFLIGHT, x)


def is_window_state(x) -> bool:
    """x 是不是窗口状态（十选一）。"""
    return _registered(WINDOW_STATES, x)


def is_ledger_status(x) -> bool:
    """x 是不是台账状态（done / partial / pending）。"""
    return _registered(LEDGER_STATUSES, x)


def is_stop_reason(x) -> bool:
    """x 是不是 stop_reason（七选一）。"""
    return isinstance(x, str) and x in STOP_REASONS


def is_review_coverage_state(x) -> bool:
    """x 是不是人审覆盖结论（"" / gap / rescued）。见 REVIEW_COVERAGE_STATES 的注释。"""
    return _registered(REVIEW_COVERAGE_STATES, x)


def run_label(x) -> str:
    """run 状态的显示名。未知字符串原样返回（`.get(x, x)` 回落）——渲染层依赖这个宽松行为。"""
    return RUN_STATUS_LABELS.get(x, x)


def window_label(x) -> str:
    """窗口状态的显示名。未知字符串原样返回（`.get(x, x)` 回落），与 `_MD_STATUS.get` 的旧写法一致。"""
    return WINDOW_STATE_LABELS.get(x, x)


def ledger_label(x) -> str:
    """台账状态的显示名。未知字符串原样返回（`.get(x, x)` 回落），与 `brief.unfinished_text` 的旧写法一致。"""
    return LEDGER_STATUS_LABELS.get(x, x)


def all_statuses() -> frozenset:
    """四表并集，供静态扫描与对外一致性断言用。import 时算好，每调用一次不重建。"""
    return _ALL_STATUSES
