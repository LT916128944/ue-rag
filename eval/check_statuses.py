# eval/check_statuses.py
"""D11 离线验收：状态词表未登记（20261024-192858 的遗留缺陷）。

真事：整改给整句引入了新状态「未完成」（`agent/harness.py:contract_status` 返回它），但同一个状态
概念当时散在四处——`harness.STOP_REASONS`、`run_windows._MD_STATUS`、`brief.unfinished_text` 里那段
内联台账标签、`ledger.py` 的 status 字段。**加一个状态只改一处，其余三处就漂**：渲染层拿不到中文名
就吐裸串，台账与 run md 用词不一致，而没有任何一层报错。

本用例钉八件事（`agent/statuses.py` 是唯一真值源），按 `main()` 的执行顺序：
1) 导入面：`agent/statuses.py` 导入时**不拉起** config / 编辑器 / 模型 / 检索等重模块；
2) 窗口状态：`run_windows._MD_STATUS` 的键集合与 `WINDOW_STATES` 相等，中文名逐项一致，
   且 `_TERMINAL_STATES` / `_RESUMABLE_STATES` 都是它的子集；
3) stop_reason：`harness.STOP_REASONS` 与本表同集合；
4) 台账状态：`brief.unfinished_text` 里那段标签 dict 的键 == `LEDGER_STATUSES`、值 == 中文标签表
   （内联字面量没有模块级符号，所以用 ast 从源码抽——见 `_brief_ledger_labels` 的注释）；
5) run 级状态：`contract_status` 的所有常量返回分支都登记在 `RUN_STATUSES`（「未完成」就在其中），
   并用真调用复核它只吐这五个词；
6) 静态扫描 `agent/*.py`：一切被比较/赋值给 `status` / `state` / `ledger_status` 的状态形字符串常量
   都必须在四表并集内；无法归类的逐条进 `_SKIP` 并写明原因，**不用异常吞**；
7) 反例守卫：未知字符串经 `*_label()` 原样回落（渲染层依赖这个宽松行为），判定函数对未知/空/非字符串
   一律 False 且不抛；
8) 真链路：台账经真 `ledger` 回写 + 真 `brief` 渲染仍然宽松；真 run 记录（`data/runs/**/*.json`，
   有的话）里的 status / state / stop_reason 全部能在词表里反查。

不打开编辑器、不调模型、不联网、不写真盘：台账往返用 tempfile 隔离。
"""
from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 静态扫描的锚点名字：只认这三个（题目口径）。别名（dep_state / current / adjusted / mark …）故意不做
# 锚点：它们的取值都是这里三个名字的子集，扩锚点只会把「PIE」「returnValue」这类无关常量放进误报面。
_ANCHOR_NAMES = {"status", "state", "ledger_status"}

# 状态形字面量的形状过滤：值里只允许 中文 / 小写字母 / 下划线，且长度 ≤ 16。
# `[事实]` 这条过滤自动排掉 PIE、INT、returnValue、--dry-run、error C 这些大写或带空格的常量。
_MAX_STATUS_LEN = 16

# 形态像状态词、但**不属于** D11 这四张表（run 总状态 / 窗口状态 / 台账状态 / stop_reason）的
# 显式跳过清单。每条都写清为什么不是同一张表；扫描命中时按 (文件, 值) 精确跳过，不做模糊匹配。
_SKIP: dict[tuple[str, str], str] = {
    ("agent/catalog.py", "approved"):
        "动作候选表（D8）的 status：候选生命周期 review→approved 的一环，是候选词表不是 run/窗口词表",
    ("agent/catalog.py", "dismissed"):
        "同上，动作候选表的终态（人否决候选）。D8 的 check_candidate_aging 才是它的守卫",
    ("agent/skill_memory.py", "promoted"):
        "技能生命周期 promoted/shadow/rejected：learned_skills.json 的字段，与 run 状态无关",
    ("agent/skill_memory.py", "shadow"):
        "同上，技能生命周期",
    ("agent/skill_memory.py", "rejected"):
        "同上，技能生命周期",
    ("agent/failure_memory.py", "promoted"):
        "失败模式记忆（failure_patterns.json）的 status，同样是记忆库词表",
    ("agent/failure_memory.py", "shadow"):
        "同上，失败模式记忆",
    ("agent/distill.py", "shadow"):
        "蒸馏候选记录的 status，同上（记忆库词表）",
    ("agent/garden.py", "active"):
        "记忆 md 的 front-matter status: active：文件维护状态，不是任何一条 run/窗口状态",
    ("agent/plan_windows.py", "route"):
        "`planned.intent.status` 的来源标记（规则/已学/规划者里的一档），是**意图来源**不是状态结论",
    ("agent/run_windows.py", "abort"):
        "brief_flow 的返回状态（confirm / abort）：人审门的结果码，写进的是返回值不是 run 记录",
    ("agent/run_windows.py", "review"):
        "action_candidate 事件的 status 参数（候选词表），与 agent/catalog.py 的 review 同一张表",
    ("agent/run_windows.py", "promoted"):
        "skill_promoted 事件的 status 参数（技能词表），与 skill_memory 的 promoted 同一张表",
}


# ---------------------------------------------------------------------------
# 1) 状态词表不许在导入时拉起重模块
# ---------------------------------------------------------------------------
_CLEAN_ALLOWED = {"agent", "agent.statuses"}
_FORBIDDEN_PREFIXES = ("config", "core", "core.", "retrieval", "eval", "dsh")
_FORBIDDEN_MODULES = {
    "agent.catalog", "agent.plan_windows", "agent.run_windows", "agent.harness", "agent.brief",
    "agent.ledger", "agent.sequence", "agent.supervise", "agent.intent", "agent.understand",
    "requests", "openai", "httpx", "vertexai",
}


def check_clean_import() -> None:
    """`agent/statuses.py` 是纯字面量模块：干净解释器里 import 它只应多出它自己（和命名空间包 agent）。

    `[事实]` 这条同时是「不许 import config」的机器证明——`config`/`core/*` 会拉起路径、模型、
    检索与编辑器网关；离线套件与 DSH 侧都要能在没有它们的解释器里读词表。
    """
    import importlib

    before = set(sys.modules)
    importlib.import_module("agent.statuses")
    added = set(sys.modules) - before
    leaked = sorted(
        name for name in added
        if name in _FORBIDDEN_MODULES
        or name.startswith(_FORBIDDEN_PREFIXES)
        or name.split(".")[0] in {"config", "requests", "openai", "httpx"}
    )
    assert not leaked, ("agent/statuses.py 导入时拉起了重模块（必须保持纯字面量）", leaked)
    assert added <= _CLEAN_ALLOWED, ("导入词表不该带出别的模块", sorted(added - _CLEAN_ALLOWED))


# ---------------------------------------------------------------------------
# 2) 四张表与四个声明点对齐
# ---------------------------------------------------------------------------
def check_window_states() -> None:
    """① 窗口状态：`_MD_STATUS` 的键 == `WINDOW_STATES`，中文名逐项一致。

    `[事实]` 2026-10-24 23:35 实测：`_MD_STATUS` 恰有 10 个键（queued/running/retry/done/failed/
    paused/skipped/waiting_external/revising/interrupted），与 `WINDOW_STATE_LABELS` 完全相等，
    所以把题面要求的「键 ⊆ WINDOW_STATES」写成更强的「键 == WINDOW_STATES」：两个方向都要守，
    少登记一个（渲染吐裸串）和多登记一个（词表里有渲染不认的状态）都是 D11 本体。
    `run_windows._TERMINAL_STATES` / `_RESUMABLE_STATES` 是这十个之上的**分类**，不参与相等断言。
    """
    import agent.statuses as statuses
    from agent import run_windows

    # hasattr 兜底：`_MD_STATUS` 是别人（run_windows 属主）的私有符号。它要是被改名或换成函数，
    # 这里要报一句看得懂的话，而不是 AttributeError 糊一行栈——否则下一个人只会把断言删掉。
    assert hasattr(run_windows, "_MD_STATUS"), (
        "run_windows._MD_STATUS 不见了：窗口状态渲染的声明点被搬走，本守卫需要跟着改"
    )
    keys = set(run_windows._MD_STATUS)
    assert keys == set(statuses.WINDOW_STATES), (
        "窗口状态词表与 _MD_STATUS 漂了",
        sorted(keys - set(statuses.WINDOW_STATES)),   # 声明了但没登记
        sorted(set(statuses.WINDOW_STATES) - keys),   # 登记了但渲染不认
    )
    for state in sorted(keys):
        assert statuses.is_window_state(state), ("不认识的窗口状态", state)
        assert run_windows._MD_STATUS[state] == statuses.window_label(state), (
            "同一窗口状态两个中文名", state, run_windows._MD_STATUS[state],
            statuses.window_label(state),
        )
    # `agent/statuses.py` 的文档里写了「_TERMINAL_STATES / _RESUMABLE_STATES 是这十个之上的分类，
    # 都是本集合的子集」——那句话在这里变成机器检查：往分类里塞一个没登记的窗口状态，就会红。
    # `[事实]` 23:40 实测两者都是 WINDOW_STATES 的子集（terminal 5 个、resumable 8 个）。
    # 用 hasattr 而不是硬取：这两个是 run_windows 的私有常量，别人重命名/合并它们不是 D11 的缺陷，
    # 不该让 D11 的守卫因为 AttributeError 报一个看不懂的红——那样只会逼人删掉这条断言。
    for name in ("_TERMINAL_STATES", "_RESUMABLE_STATES"):
        if not hasattr(run_windows, name):
            print(f"  提示：run_windows.{name} 已不存在（可能被重命名），跳过它的子集断言")
            continue
        table = getattr(run_windows, name)
        extra = sorted(set(table) - set(statuses.WINDOW_STATES))
        assert not extra, (f"run_windows.{name} 里有没登记进 WINDOW_STATES 的状态", extra)


def check_stop_reasons() -> None:
    """② stop_reason：`harness.STOP_REASONS` 与本表同集合。

    `[事实]` `harness.run_stop` 按 `harness.STOP_REASONS` 的内容挑 run_end 的 stop_reason；
    它和 `agent/statuses.STOP_REASONS` 是同值元组（顺序也沿用原样，便于哪天直接换真值源）。
    """
    import agent.statuses as statuses
    from agent import harness

    assert set(harness.STOP_REASONS) == set(statuses.STOP_REASONS), (
        "stop_reason 词表漂了",
        sorted(set(harness.STOP_REASONS) - set(statuses.STOP_REASONS)),
        sorted(set(statuses.STOP_REASONS) - set(harness.STOP_REASONS)),
    )
    for reason in statuses.STOP_REASONS:
        assert statuses.is_stop_reason(reason), reason


def _brief_ledger_labels() -> tuple[dict, str]:
    """从 `agent/brief.py` 的源码里抽 `unfinished_text` 那段台账标签 dict。

    为什么用 ast 而不是 import 真符号：`[事实]` 那三个键**是内联 dict 字面量**
    （`label = {"done": "完成", "partial": "部分完成", "pending": "未完成"}.get(status, status)`），
    brief 模块里没有对应的模块级常量可 import。直接 import 真函数只能拿到渲染结果，拿不到
    「键集合」这个要断言的东西；硬编码一份字面量副本又正是 D11 要禁止的（副本会漂）。
    所以唯一诚实的取法是解析源码：定位 `unfinished_text` 里**以 dict 字面量为接收者的 `.get(status, ...)`**，
    取那个字面量的键值——它就是这个仓库里台账标签的真实声明点。

    两种声明形态都接（免得有人把 brief 改成引用词表后这条断言变成硬红）：
      - `inline`：今天的样子（内联 dict 字面量）——从源码里抽，能抓到真漂移；
      - `shared`：将来 brief 改成 `from agent.statuses import LEDGER_STATUS_LABELS` 或
        `statuses.LEDGER_STATUS_LABELS.get(status, status)` 后，用**运行期真对象**比对；
        这时「加状态只改一处」在结构上已不可能漂，断言退化成「它引用的确实就是这张表」，
        所以这里直接取 statuses 表并要求 brief 侧拿到的是同一个对象（`is` 身份比较）。
    """
    path = ROOT / "agent" / "brief.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    funcs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "unfinished_text"
    ]
    assert len(funcs) == 1, ("brief.unfinished_text 应当唯一", len(funcs))
    shared_ref = ""
    for node in ast.walk(funcs[0]):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "get":
            continue
        if not (node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "status"):
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Dict):
            table: dict[str, str] = {}
            for key, value in zip(receiver.keys, receiver.values):
                assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
                    "台账标签 dict 的键应当是字符串常量", ast.dump(key)[:80]
                )
                assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
                    "台账标签 dict 的值应当是字符串常量", ast.dump(value)[:80]
                )
                table[key.value] = value.value
            return table, f"agent/brief.py:{node.lineno}（内联 dict）"
        if isinstance(receiver, (ast.Attribute, ast.Name)):
            name = receiver.attr if isinstance(receiver, ast.Attribute) else receiver.id
            shared_ref = f"{ast.unparse(receiver)}({name}) @ agent/brief.py:{node.lineno}"
            break
        if isinstance(receiver, ast.Call):
            # `.get(status, status)` 的接收者是现场构造的（`dict(...)` / `{**...}` 之类），
            # 那是**副本**不是引用——副本照样会漂，且值还能对上，比内联字面量更隐蔽。直接点名。
            raise AssertionError(
                "brief 的台账标签是现场构造的副本（接收者是调用而不是词表符号），副本会漂："
                f"改用 agent/statuses.LEDGER_STATUS_LABELS.get(status, status) @ agent/brief.py:{node.lineno}"
            )
    if shared_ref:
        # 共享形态：取出 brief 运行期真正用的那张表，用 `is` 证明它就是 statuses 里那张（不是副本）。
        import agent.statuses as statuses
        from agent import brief

        used = getattr(brief, "LEDGER_STATUS_LABELS", None)
        assert used is not None, (
            f"brief 用的标签表 {shared_ref} 在模块级拿不到，无法证明它引用的是词表", shared_ref
        )
        assert used is statuses.LEDGER_STATUS_LABELS, (
            "brief 里的台账标签表不是 agent/statuses.LEDGER_STATUS_LABELS 本身（是副本就会漂）", shared_ref
        )
        return dict(used), f"agent/brief.py:{shared_ref}"
    raise AssertionError("brief.unfinished_text 里找不到台账标签 dict（声明点被搬走了？先确认再看这里）")


def check_ledger_statuses() -> None:
    """③ 台账状态：`brief` 的内联标签键 == `LEDGER_STATUSES`，值 == 中文标签表。"""
    import agent.statuses as statuses

    table, where = _brief_ledger_labels()
    assert set(table) == set(statuses.LEDGER_STATUSES), (
        f"台账状态词表与 {where} 漂了",
        sorted(set(table) - set(statuses.LEDGER_STATUSES)),
        sorted(set(statuses.LEDGER_STATUSES) - set(table)),
    )
    for status, label in table.items():
        assert statuses.is_ledger_status(status), status
        assert statuses.ledger_label(status) == label, (
            f"{where} 的中文标签与词表不一致", status, label, statuses.ledger_label(status)
        )


def _flatten_string_constants(node) -> list[str]:
    """把 Return 的表达式里所有字符串常量摊平：兼容 `return "x"`、`return "x" if c else y`、
    `return "x" if c else "y"`。只用于「这个函数可能返回哪些字面量状态」，不追 Name/调用。"""
    out: list[str] = []
    for inner in ast.walk(node):
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            out.append(inner.value)
    return out


def check_run_statuses() -> None:
    """④ run 级状态：`contract_status` 的常量返回/分支全部登记；「未完成」必须在册。

    `[事实]` `harness.contract_status` 是「未完成」这个状态的**引入点**（20261024-192858 整改：
    有 capability_gap 范围时把成功/部分完成改记未完成）。它在源码里的形态是
    `return "未完成" if status in ("成功", "部分完成") else status`——条件表达式，不是裸常量返回。
    所以这里把 Return 的表达式整棵摊平取字符串常量（`_flatten_string_constants`），
    只看 `ast.Constant` 的写法会漏掉本次缺陷的主角。这条同时是题面要求的 ⑥ 直接回归。
    """
    import agent.statuses as statuses
    from agent import harness

    path = ROOT / "agent" / "harness.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    funcs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "contract_status"
    ]
    assert len(funcs) == 1, ("harness.contract_status 应当唯一", len(funcs))
    returned: set[str] = set()
    for node in ast.walk(funcs[0]):
        if not (isinstance(node, ast.Return) and node.value is not None):
            continue
        # `return "未完成" if status in ("成功", "部分完成") else status` 是条件表达式不是裸常量，
        # 只抽 ast.Constant 会漏掉「未完成」——本次缺陷的主角正好写在这个形态里，所以两支都要摊平。
        for branch in _flatten_string_constants(node.value):
            returned.add(branch)
    # 空串是「没结论」的传统写法（run md 的 status 行为空时由渲染层补），不是状态词。
    returned.discard("")
    assert returned, "contract_status 里一个常量状态都抽不到，断言失去了意义"
    unregistered = sorted(returned - set(statuses.RUN_STATUSES))
    assert not unregistered, (
        "contract_status 会返回没登记进 RUN_STATUSES 的状态", unregistered,
        sorted(statuses.RUN_STATUSES),
    )
    # 防漂的直接回归：20261024-192858 整改引入的「未完成」必须在册，且能被判定函数认出来。
    assert "未完成" in set(statuses.RUN_STATUSES), sorted(statuses.RUN_STATUSES)
    assert "未完成" in returned, ("contract_status 不再产出「未完成」了？先确认缺口路径还在", sorted(returned))
    assert statuses.is_run_status("未完成") and not statuses.is_ledger_status("未完成")
    # 五个人读结论都得在册（run md 的「状态：」行与台账渲染都从这里取）。
    for status in ("成功", "部分完成", "未完成", "失败", "中断"):
        assert statuses.is_run_status(status), status
    # 行为面复核（不只看源码）：真调一次 contract_status，看它真的只吐这五个词。
    # `[事实]` 空窗口 + capability_gap 范围 ⇒ 「未完成」；零窗口范围（无 gap 字段）⇒ 「部分完成」；
    # 已经是失败/中断的不改——三条都是 `harness.contract_status` 的现有分支（23:35 实测返回值）。
    gap_scope = [{"id": "B", "window_ids": [],
                  "capability_gap": {"reason": "能力缺口", "deliverables": ["新建关卡 L_NewTest2"]}}]
    observed = {
        harness.contract_status([], "成功", gap_scope),
        harness.contract_status([], "成功", [{"id": "B", "window_ids": []}]),
        harness.contract_status([], "成功", []),
        harness.contract_status([], "失败", gap_scope),
        harness.contract_status([], "中断", gap_scope),
    }
    assert observed <= set(statuses.RUN_STATUSES), (
        "contract_status 实测返回值超出了 RUN_STATUSES", sorted(observed - set(statuses.RUN_STATUSES))
    )
    assert harness.contract_status([], "成功", gap_scope) == "未完成", "缺口范围必须记「未完成」"


# ---------------------------------------------------------------------------
# 3) 静态扫描：agent/*.py 里被当状态用的字面量必须在四表内
# ---------------------------------------------------------------------------
def _is_status_shaped(value: str) -> bool:
    """状态形：非空、≤16 字、只含中文 / 小写字母 / 下划线。

    `[事实]` 这条过滤是防误报的第一道闸：`PIE`、`INT`、`returnValue`、`error C`、`--dry-run`
    这类常量在大小写或空格上就被排除了，不需要把它们塞进跳过清单。
    """
    if not value or len(value) > _MAX_STATUS_LEN:
        return False
    return all("\u4e00" <= ch <= "\u9fff" or ch.islower() or ch == "_" for ch in value)


def _const_strings(node) -> list[str]:
    """把常量或常量容器（tuple/list/set）摊平成字符串列表；含非常量元素就整侧放弃。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        out: list[str] = []
        for element in node.elts:
            if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                return []
            out.append(element.value)
        return out
    return []


def _is_anchor(node) -> bool:
    """比较/赋值的一端是不是「状态锚点」：裸名字、`x['status']`、`x.get('state')`。"""
    if isinstance(node, ast.Name):
        return node.id in _ANCHOR_NAMES
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value in _ANCHOR_NAMES
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        return (
            bool(node.args) and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in _ANCHOR_NAMES
        )
    return False


def _compare_literals(compare: ast.Compare) -> list[tuple[str, str]]:
    """比较语句里「锚点 == 字面量」的字面量侧，返回 [(值, 表达式)]。只认相等/包含四类运算符。"""
    out: list[tuple[str, str]] = []
    operands = [compare.left] + list(compare.comparators)
    for index, operator in enumerate(compare.ops):
        if not isinstance(operator, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
            continue
        left, right = operands[index], operands[index + 1]
        for anchor, other in ((left, right), (right, left)):
            if not _is_anchor(anchor):
                continue
            for value in _const_strings(other):
                out.append((value, ast.unparse(compare)))
    return out


def _assign_target_names(target) -> list[str | None]:
    """赋值目标的锚点身份：裸名字给 'name'，`x['status']` 给 'subscript'，其余 None。"""
    if isinstance(target, ast.Name):
        return [target.id if target.id in _ANCHOR_NAMES else None]
    if isinstance(target, ast.Subscript):
        if isinstance(target.slice, ast.Constant) and target.slice.value in _ANCHOR_NAMES:
            return [f"['{target.slice.value}']"]
        return [None]
    return [None]


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """返回 [(行号, 字面量, 锚点说明)]。四个**致命**锚点：赋值、元组解包赋值、比较、关键字参数。

    第五个锚点（`or` 缺省值，见 `_or_default_hits`）不在这个函数里：它只做提示、不做断言，
    理由写在那个函数的注释里。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value = node.value
            first = node.targets[0] if node.targets else None
            # `status, note = "成功", "全部窗口完成。"` 这种元组解包：按位置对齐，别把 note 的内容当状态。
            if (
                isinstance(first, ast.Tuple) and isinstance(value, ast.Tuple)
                and len(first.elts) == len(value.elts)
            ):
                for target, element in zip(first.elts, value.elts):
                    for anchor in _assign_target_names(target):
                        if anchor is None:
                            continue
                        for text in _const_strings(element):
                            hits.append((node.lineno, text, f"赋值→{anchor}"))
                continue
            for target in node.targets:
                for anchor in _assign_target_names(target):
                    if anchor is None:
                        continue
                    for text in _const_strings(value):
                        hits.append((node.lineno, text, f"赋值→{anchor}"))
        elif isinstance(node, ast.Compare):
            for text, expr in _compare_literals(node):
                hits.append((node.lineno, text, f"比较：{expr}"))
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in _ANCHOR_NAMES:
                    for text in _const_strings(keyword.value):
                        hits.append((node.lineno, text, f"关键字参数 {keyword.arg}="))
    return hits


def _or_default_hits(path: Path) -> list[tuple[int, str, str]]:
    """第五个锚点（**只提示、不断言**）：`x.get("state") or "queued"` / `status or "pending"` 这类缺省值。

    为什么它是状态声明点：它决定「没写状态时算哪一档」——`board` 面板的初始 `or "queued"`、
    `ledger.update_ledger` 的 `or "pending"` 都在这里 `[事实]`。加一个状态时最容易漏的就是缺省分支。

    为什么只提示不断言：这个锚点会捞到一堆**别的词表**的 `or` 缺省值
    （`agent/brief.py` 的外部依赖状态 `未登记`、人审状态 `待确认`；`skill_memory` / `plan_windows`
    的技能与意图状态 `promoted` / `shadow` / `learned`）。那些声明点属于 D6/D8 与记忆库，
    属主不是 D11；把它们写成致命断言，别人正当地改 brief 的人审措辞就会把 D11 的守卫点红。
    真正的漂移在这上面仍然会被抓到：同一个值一旦真被当状态用（比较 / 赋值 / 关键字参数），
    前四个致命锚点就会命中。所以这里只把「你没登记的缺省值」打进 CI 日志，让人自己判断。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)):
            continue
        if not node.values or not _is_anchor(node.values[0]):
            continue
        for value in node.values[1:]:
            for text in _const_strings(value):
                hits.append((node.lineno, text, f"缺省值：{ast.unparse(node)[:70]}"))
    return hits


def _file_mentions(path: Path, value: str) -> bool:
    """源码文本里还残留这个字面量吗？跳过清单死条目的判据（见 check_static_scan 末尾的 (a)/(b) 分档）。

    两种引号都查：仓库里单双引号混用，只查一种会给「还在用双引号」的文件误报死条目。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return f'"{value}"' in text or f"'{value}'" in text


def check_static_scan() -> None:
    """⑤ 静态扫描：所有「被比较/赋值给 status / state / ledger_status」的状态形常量都在四表并集内。

    这条是 D11 的机器证据：**新加一个状态、只改一处**时，只要它在 agent/ 里被比较或被赋值给这三个
    名字中的任何一个，扫描就会红。红了的处置有两条、且只有两条：登记进 `agent/statuses.py`，或者
    （确认它不是这四张表的词）在 `_SKIP` 里补一条带原因的跳过。没有第三条。
    """
    import agent.statuses as statuses

    # 扫描面 = 四表并集 + 已登记的其他词表。REVIEW_COVERAGE_STATES 是人审渲染结论
    # （""/gap/rescued，D6 落地时引入），它不属于 run/窗口/台账/stop_reason 四张身份表，
    # 但**必须是 statuses.py 里登记过的**——所以这里加进扫描面而不是加进 `_SKIP`：
    # `_SKIP` 是「不归 D11 管的词」，而「加一个状态必须先改 statuses.py」这条规则对它同样适用。
    known = set(statuses.all_statuses()) | set(statuses.REVIEW_COVERAGE_STATES)
    offenders: list[str] = []
    skipped_sites: dict[tuple[str, str], list[int]] = {}
    checked = 0
    for path in sorted((ROOT / "agent").glob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for lineno, value, anchor in _scan_file(path):
            if not _is_status_shaped(value):
                continue   # 形状不像状态值：不猜（见 _is_status_shaped 的注释）
            checked += 1
            if value in known:
                continue
            reason = _SKIP.get((rel, value))
            if reason is not None:
                skipped_sites.setdefault((rel, value), []).append(lineno)
                continue
            offenders.append(f"{rel}:{lineno} {value!r} ← {anchor}")
    assert checked > 0, "一个状态字面量都没扫到：锚点规则坏了，这条断言失去意义"
    assert not offenders, (
        "agent/ 里出现了没登记进 agent/statuses.py 的状态字面量（加状态必须同时改词表）",
        offenders,
    )
    # 跳过清单的死条目分两种，处置不同——这条判据是防「白名单掩盖漏报」的：
    #   (a) 字面量**还在**这个文件里，但锚点没抓到它 ⇒ 扫描面有洞，必须红（这是真漏报）；
    #   (b) 字面量已经整个不在这个文件里 ⇒ 只是白名单该清理了，打一行提示即可。
    # 不把 (b) 判红，是因为跳过清单横跨 agent/ 下多个文件（catalog / skill_memory / garden …），
    # 那些文件的属主不是 D11，别人正当地改掉一个候选状态不该把 D11 的守卫点红。
    dead = sorted(set(_SKIP) - set(skipped_sites))
    missed = [item for item in dead if _file_mentions(ROOT / item[0], item[1])]
    assert not missed, (
        "跳过清单里的字面量还在文件里，却一条扫描都没命中：锚点漏了它（真漏报），先补锚点",
        missed,
    )
    for rel, value in dead:
        if (rel, value) not in set(missed):
            print(f"  提示：跳过条目 {rel} {value!r} 已过期（该文件里已无此字面量），可顺手删掉")
    # 跳过的每条都留痕：CI 日志里能看见「哪些字面量被有理由地放过」，不是无声吞掉。
    for (rel, value), lines in sorted(skipped_sites.items()):
        sites = ",".join(str(line) for line in sorted(lines))
        print(f"  跳过 {rel}:{sites} {value!r} —— {_SKIP[(rel, value)]}")
    # 第五个锚点（`or` 缺省值）只提示：见 `_or_default_hits` 的注释（捞到多张别的词表，
    # 判红会误伤 brief 人审措辞等其他属主的改动）。这里把它摆在 CI 日志里，别让人以为没检查过。
    advisories: list[str] = []
    for path in sorted((ROOT / "agent").glob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for lineno, value, anchor in _or_default_hits(path):
            if not _is_status_shaped(value) or value in known:
                continue
            advisories.append(f"{rel}:{lineno} {value!r} ← {anchor}")
    for line in advisories:
        print(f"  提示（缺省值，未登记，请人工判断归属）：{line}")


# ---------------------------------------------------------------------------
# 4) 反例守卫 + 真渲染层往返
# ---------------------------------------------------------------------------
def check_label_fallbacks() -> None:
    """⑥ 回落不许抛：未知字符串原样返回（渲染层依赖这个宽松行为）。

    `[事实]` `run_windows` 三处 `_MD_STATUS.get(entry['state'], entry['state'])` 走的就是
    `dict.get(x, x)`；旧的 `brief.unfinished_text` 也是 `{...}.get(status, status)`。
    状态是**外部数据**（旧 run 记录、人工改过的任务书、将来别人加的新状态），词表不认识它时
    正确行为是「照原样显示」而不是抛异常——不然一次渲染就把整条 run 打断。
    """
    import agent.statuses as statuses

    unknown = "不存在的新状态"
    assert statuses.run_label(unknown) == unknown, statuses.run_label(unknown)
    assert statuses.window_label(unknown) == unknown, statuses.window_label(unknown)
    assert statuses.ledger_label(unknown) == unknown, statuses.ledger_label(unknown)
    # 英文未知值也一样回落（旧 run 记录里可能留着别的实现写的状态串）
    assert statuses.window_label("mystery_state") == "mystery_state"
    # 判定函数对未知值、空值、非字符串一律 False，且不抛（frozenset 对 list/dict 会抛 TypeError，
    # 所以判定函数先卡 isinstance）。
    for value in (unknown, "", "mystery_state"):
        assert statuses.is_run_status(value) is False, value
        assert statuses.is_window_state(value) is False, value
        assert statuses.is_ledger_status(value) is False, value
        assert statuses.is_stop_reason(value) is False, value
    for value in (None, ["done"], {"state": "done"}, 0):
        assert statuses.is_run_status(value) is False, value
        assert statuses.is_window_state(value) is False, value
        assert statuses.is_ledger_status(value) is False, value
    # 四张表的已知成员必须判得出来（正例，防止判定函数被写成恒 False）
    assert statuses.is_run_status("成功") and statuses.is_run_status("未完成")
    assert statuses.is_window_state("waiting_external") and statuses.is_window_state("revising")
    assert statuses.is_ledger_status("partial") and statuses.is_stop_reason("no_evidence")
    # 跨表不许混：中文 run 状态不是窗口状态，台账英文状态不是 run 状态
    assert not statuses.is_window_state("成功") and not statuses.is_run_status("done")
    # 并集 = 四表并集（去重后），且它能覆盖每一个成员
    union = set(statuses.all_statuses())
    assert union == (
        set(statuses.RUN_STATUSES) | set(statuses.WINDOW_STATES)
        | set(statuses.LEDGER_STATUSES) | set(statuses.STOP_REASONS)
    ), sorted(union)
    for table in (statuses.RUN_STATUSES, statuses.WINDOW_STATES,
                  statuses.LEDGER_STATUSES, statuses.STOP_REASONS):
        assert set(table) <= union, sorted(set(table) - union)
    # 人审覆盖状态是**另一张表**：必须在 statuses.py 里登记，但不许混进四表并集
    # （混进去会让「run 记录里的状态串必须都在册」这条反查失去意义——"" 会混进来）
    assert statuses.REVIEW_COVERAGE_STATES, "人审覆盖状态表不许为空"
    assert not (set(statuses.REVIEW_COVERAGE_STATES) & union), (
        "人审覆盖状态不许混进 run/窗口/台账/stop_reason 并集",
        sorted(set(statuses.REVIEW_COVERAGE_STATES) & union),
    )
    assert statuses.is_review_coverage_state("gap"), "is_review_coverage_state 判定坏了"
    assert statuses.is_review_coverage_state("rescued"), "is_review_coverage_state 判定坏了"
    assert not statuses.is_review_coverage_state("成功"), "is_review_coverage_state 判定太宽"
    # `[事实]` "paused" 同时是窗口状态和 stop_reason：同一个词在两表合法，并集里只算一个。
    assert "paused" in statuses.WINDOW_STATES and "paused" in statuses.STOP_REASONS


def check_ledger_round_trip() -> None:
    """真 ledger 回写 + 真 brief 渲染：词表内的状态能读回，词表外的状态照旧渲染不抛。

    `[事实]` `ledger.update_ledger` 对缺省 status 填 "pending"（`str(scope.get("status") or "pending")`），
    所以「pending」必须是台账词表成员；`brief.unfinished_text` 把台账英文渲染成中文，
    `ledger_summary_text` 保持英文原词给机器读——两级输出都得覆盖到。
    """
    import agent.statuses as statuses
    from agent import brief, ledger

    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "ledger.json"
        rows = [
            {"id": "A", "title": "启动编辑器", "status": "done", "pending": []},
            {"id": "B", "title": "新建关卡", "status": "partial", "pending": ["补 create_level 的接线"]},
            {"id": "C", "title": "FPS 玩法", "status": "pending", "pending": ["输入映射"]},
            # 不写 status：走 ledger 的缺省分支，证明缺省值也在词表里
            {"id": "D", "title": "缺省状态", "pending": []},
            # 词表外的状态：旧记录 / 别的实现写下的，渲染层必须照原样显示而不是抛
            {"id": "E", "title": "词表外状态", "status": "mystery_state", "pending": []},
        ]
        data = ledger.update_ledger("statuses-check", rows, path=path)
        by_id = {item["id"]: item for item in data["scopes"]}
        assert by_id["D"]["status"] == "pending", by_id["D"]
        for sid in ("A", "B", "C", "D"):
            assert statuses.is_ledger_status(by_id[sid]["status"]), (sid, by_id[sid])
            assert statuses.ledger_label(by_id[sid]["status"]) in {"完成", "部分完成", "未完成"}
        assert by_id["E"]["status"] == "mystery_state", by_id["E"]
        assert statuses.is_ledger_status(by_id["E"]["status"]) is False

        # 机器读：英文原词（台账摘要给任务书生成器用，不翻译）
        machine = ledger.ledger_summary_text(path)
        for token in ("done", "partial", "pending", "mystery_state"):
            assert token in machine, (token, machine)
        # 人读：词表内的翻成中文，词表外的原样留着（`dict.get(status, status)` 的回落）
        human = brief.unfinished_text(path)
        assert "A 完成" in human, human
        assert "B 部分完成" in human, human
        assert "C 未完成" in human, human
        assert "E mystery_state" in human, human
        # 也走一遍窗口标签的真出口：run md / 控制台都靠这一句
        for state in statuses.WINDOW_STATES:
            text = statuses.window_label(state)
            assert isinstance(text, str) and text and text != state, (state, text)


def check_run_records() -> None:
    """真 run 记录反查词表：`data/runs/**/*.json` 里的 status / windows[*].state / stop_reason 都得在册。

    这是「渲染/台账里出现未映射裸状态串」的可检测信号（题面 ①〜④ 的落盘侧证据）。
    `[事实]` `data/runs/` 被 gitignore（`.gitignore:4`），CI 上通常没有这份目录：那时**不假装验过**，
    打一行「跳过」就返回（与 `eval/check_capability_gap.py` 处理缺失真实任务书同一套做法）。
    """
    import json

    import agent.statuses as statuses

    records = sorted((ROOT / "data" / "runs").rglob("*.json"))
    if not records:
        print("  跳过真 run 记录反查：data/runs/ 为空或被 gitignore（CI 常态），本次不假装验过")
        return
    checked = 0
    offenders: list[str] = []
    for path in records:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 读不了就点名，不静默 skip：坏记录本身就是要看见的东西。
            offenders.append(f"{path.name} 读不动：{type(exc).__name__}")
            continue
        if not isinstance(data, dict):
            continue
        checked += 1
        rel = path.relative_to(ROOT).as_posix()
        status = data.get("status")
        if isinstance(status, str) and status and not statuses.is_run_record_status(status):
            offenders.append(f"{rel} status={status!r}")
        integrated = data.get("integration")
        if isinstance(integrated, dict):
            istatus = integrated.get("status")
            if isinstance(istatus, str) and istatus and not statuses.is_run_record_status(istatus):
                offenders.append(f"{rel} integration.status={istatus!r}")
        windows = data.get("windows")
        for wid, entry in (windows.items() if isinstance(windows, dict) else []):
            if not isinstance(entry, dict):
                continue
            state = entry.get("state")
            if isinstance(state, str) and state and not statuses.is_window_state(state):
                offenders.append(f"{rel} windows[{wid}].state={state!r}")
            reason = entry.get("stop_reason")
            if isinstance(reason, str) and reason and not statuses.is_stop_reason(reason):
                offenders.append(f"{rel} windows[{wid}].stop_reason={reason!r}")
    assert checked > 0, "data/runs/ 有文件却一条记录都没读成：断言失去意义"
    assert not offenders, ("落盘 run 记录里出现了词表外的状态串", offenders)
    print(f"  真 run 记录反查：{checked} 份记录的状态串全部在册")


def main() -> int:
    # 顺序有讲究：`check_clean_import` 必须最先跑，此时 sys.modules 里还没有任何 agent 模块，
    # 量到的增量才只属于 statuses.py（后两条 import 会把 config / core 带进来，量不准）。
    check_clean_import()
    check_window_states()
    check_stop_reasons()
    check_ledger_statuses()
    check_run_statuses()
    check_static_scan()
    check_label_fallbacks()
    check_ledger_round_trip()
    check_run_records()
    print("statuses ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
