# eval/check_catalog_parity.py
"""能力表 vs 执行器 parity 守卫（主文档 D1 / §4，骨架见 gap-taxonomy.md 附录 D）。

缺陷本体：一个动作名散落在 13 处手工声明里（catalog.py 的 CATALOG/DIRECT_ACTIONS/
READ_ONLY_ACTIONS/GRAPH_ACTIONS/EDITOR_LOCK_ACTIONS/_VERIFY、harness.py 的
EVIDENCE_KINDS/_KIND_BY_ACTION、sequence._run_action 分支、run_windows 的写盘口径、
brief 的估时表、core/judge.judge 的 4 个真裁判分支、plan_windows 的归地表），
没有任何一处是自动推导的。加一个动作要改 13 处，漏一处就产生两种反向缺陷：

  * 「能规划不能执行」—— 最坏是假成功（窗口跑了个「未知动作」，集成矩阵照样绿）；
  * 「执行器能跑但规划者永远看不见」—— 能力被静默埋掉。

本守卫只做两件事：静态解析出两个方向的动作集，断言差集为空；两边都不许靠人手维护的表糊过去。

断言（I1–I5，对应主文档 §4.2 / 附录 D）：
  I1  EXECUTOR_SET - PLANNER_SET ⊆ CODE_ONLY_ALLOWED，且白名单每条 inserted_by 的
      每个插入点都能在 agent/ 源码里用 ast 解析到，且该函数体内真的出现这个动作名。
  I2  PLANNER_SET - (EXECUTOR_SET | GRAPH_ACTIONS) == set()：规划者能写，执行器必须认。
  I3  能写东西的动作必须有裁判：_KIND_BY_ACTION 有映射，或登记在 SYNTHETIC_JUDGES
      （合成裁判，带 kind + 能解析到的站点），或登记在 PENDING_JUDGE（producer 侧今天
      确实还没声明判词，逐条 ast 复核，producer 一旦声明本表立刻红）。
  I4  set(_KIND_BY_ACTION.values()) ⊆ set(EVIDENCE_KINDS)；SYNTHETIC_JUDGES 声明的
      kind 也要在 EVIDENCE_KINDS 里，且站点源码里真的出现 kind 字符串（不许写空表混过）。
  I5  brief_system_prompt() 播出去的动作集 == iter_prompt_specs() 的动作集（集合相等，
      失败打印双向差集）。promoted 候选进来而不改任务书提示 ⇒ 红。

只读：不写盘、不联网、不调模型、不 import 编辑器（不 touch core.editor_exec / ue_mcp_client）。
退出码：全绿 0（打印一行），有问题 1（逐条打印 `  - I{id} ...`）。
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 静态分析要读的源文件（全部相对 ROOT）。
SEQUENCE_SRC = ROOT / "agent" / "sequence.py"
EXECUTOR_FUNC = "_run_action"

# 站点解析时按这个顺序找模块文件：agent/ 优先，其次 core/，最后仓库根。
SITE_MODULE_DIRS = (ROOT / "agent", ROOT / "core", ROOT)


def _code_only_allowed() -> dict[str, tuple[str, ...]]:
    """P1-6：代码专用扇白名单改读生成器产物 agent/actions.py（CODE_ONLY.inserted_by）。
    读不到生成器就红，不许拿一张空表混过去。"""
    try:
        from agent.actions import CODE_ONLY
    except Exception as exc:
        raise GuardError(f"agent.actions.CODE_ONLY 读不到（生成器产物缺失）：{exc}") from exc
    out: dict[str, tuple[str, ...]] = {}
    for name, row in CODE_ONLY.items():
        sites = tuple(str(site) for site in (row.get("inserted_by") or ()) if str(site).strip())
        if not sites:
            raise GuardError(f"actions.CODE_ONLY[{name!r}] 没写 inserted_by（等于空口无凭）")
        out[str(name)] = sites
    return out


# ---------------------------------------------------------------- 白名单 / 合成裁判登记
#
# 代码专用扇：执行器认，但规划者不该看见（规划者写不出它的槽位，只能由代码在某个
# 规划阶段插入）。每条必须写明 inserted_by，且每个插入点都要能真实解析到。
#
# inserted_by 的写法：`"<模块>.<函数>"`，模块在 agent/ 或 core/ 下（不带 .py）。
# 一个动作允许多站点 —— 一个动作经常有好几条插入路径，每条都必须能解析。
# 判定一个站点「真实」的三个条件（缺一即红）：
#   ① 模块文件存在；② 该文件里有同名 FunctionDef/AsyncFunctionDef（ast，不用字符串 in）；
#   ③ 该函数体内真的出现这个动作名（防止点到存在但无关的函数上）。
#
# P1-6（20261028）：白名单本体改读生成器产物 agent/actions.py 的 CODE_ONLY.inserted_by——
# 单一声明源在那里，这里只负责逐站 ast 复核与反向可解析性。
# 实测（2026-10-24，当时未提交工作树，本守卫跑出来的差集）：EXECUTOR_SET - PLANNER_SET 共 8 条。
# CODE_ONLY_ALLOWED 在 GuardError 定义之后赋值（_code_only_allowed 会抛 GuardError）。
#
# 说明（刻意不做的两条断言，避免守卫变成「拦住改进的墙」）：
#   * 不要求 CODE_ONLY_ALLOWED 的键一定还在差集里。将来把 create_blueprint 提升成
#     规划者可见（D1 的正向目标）后，这条白名单会自然失效；把它判红等于禁止改进。
#     这里只做反向可解析性：白名单里的每个键必须仍在 EXECUTOR_SET 里（防拼错名字）。
#
# 附录 D 骨架里 `plan_windows._place_pair` 与 `plan_windows._materialize_goals` 这两条是错的：
# 实测 plan_windows.py 里没有 `_place_pair` 这个函数（place_pair 的执行器函数在
# sequence.py:917，插入点在 supervise._append_pickup_slots），照抄会让守卫红。
#
# 合成裁判：kind 不由 core/judge.judge 判，而是运行期某处现造一条判决。
# 每条必须写 kind + site（可解析函数），且站点源码里真的出现 kind 与动作名。
SYNTHETIC_JUDGES = {
    # [事实] core/judge.py 只有 4 个 kind 分支（:547/:549/:551/:553），widget_graph 不在里面。
    # 「控件图扇跑完并编译通过」这条判决由 run_windows._absorb_result 现造
    # （run_windows.py:863-866：`if ok and action == "edit_widget_blueprint"` 才 append）。
    "edit_widget_blueprint": {
        "kind": "widget_graph",
        "site": ("run_windows._absorb_result",),
        "why": "控件图没有独立裁判；改完并编译通过由 run_windows 现造一条 widget_graph 判决。",
    },
}
#
# 待补裁判登记：现在为空。
# U1（20261027 升级计划）把当初的两条真缺陷补掉了——producer 侧先声明判词，映射两侧同时动：
#   bind_blueprint → sequence._bind_evidence（find_assets 只读核对唯一蓝图路径，editor_assert）
#   create_widget_blueprint → _create_widget_blueprint 保存后跑 blueprint_compile 编译裁判
# 两者的 _KIND_BY_ACTION 映射与判词同批合入（harness.py）。这张表继续当定时炸弹用：
# 下次再有「能规划、能执行、没裁判」的动作，先在这里登记 producer 站点，再按四道闸补判词。
#
# 每条都要过四道闸：
#   ① producer 站点能解析；② producer 源码里**没有** judge_kind（缺口的证明）；
#   ③ catalog._VERIFY 有该动作的验收口径（说明它本该有裁判）；④ why 非空。
# 一旦有人在 producer 里补了 judge_kind，②立刻红 —— 逼着把映射搬进 _KIND_BY_ACTION，
# 也就是「两侧必须同时动」。不写死原因/站点就无法靠改表混过去。
PENDING_JUDGE = {}
# PENDING_JUDGE 的 producer 站点规则（三条，逐站强制，与 CODE_ONLY_ALLOWED 同一把尺）：
#   ① 每个站点都必须能解析到真实函数（ast 找 FunctionDef/AsyncFunctionDef，不用字符串 in）；
#   ② 每个站点里都必须出现这个动作名（证明这条登记指的是这扇窗口，不是点到无关函数）；
#   ③ 任何站点都不许出现 judge_kind（这就是「producer 还没声明判词」的证据）。
# 站点只写「真的含这个动作名的那个函数」：helper（如 _ensure_blueprint）虽然能证明「这条路径上
# 只有错误信号」，但它不含动作名，写进去就是站点指错，宁可少写也不许凑数。

# 无需裁判的动作：只读裁决不写盘；launch_editor 的验收就是「端口通了」，由窗口自带判词，
# 主文档 §4.2 明确把它从 I3 里排除 `[事实]` launch_editor 也在 _KIND_BY_ACTION 里（editor_assert）。
NO_EVIDENCE_NEEDED = {"launch_editor"}


class GuardError(Exception):
    """可读的守卫内部错误：哪个文件、哪个符号没找到。由 main 转成一条 I* 问题行。"""


# P1-6：白名单在 GuardError 之后才赋值（_code_only_allowed 读 agent.actions，读不到就红）。
CODE_ONLY_ALLOWED = _code_only_allowed()


# ---------------------------------------------------------------- 静态解析
def _read_source(path: Path) -> str:
    if not path.is_file():
        raise GuardError(f"{path.relative_to(ROOT).as_posix()} 不存在（守卫要静态解析它）")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GuardError(f"{path.relative_to(ROOT).as_posix()} 读不出来：{exc}") from exc


def _parse(path: Path):
    src = _read_source(path)
    try:
        return src, ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        raise GuardError(
            f"{path.relative_to(ROOT).as_posix()} 语法错误，ast 解析不了（第 {exc.lineno} 行：{exc.msg}）"
        ) from exc


def _find_function(tree, name: str):
    """先找模块顶层，再退到任意嵌套层。找不到返回 None。"""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _module_path(stem: str) -> Path:
    for folder in SITE_MODULE_DIRS:
        candidate = folder / f"{stem}.py"
        if candidate.is_file():
            return candidate
    raise GuardError(
        f"站点模块 {stem}.py 找不到（找过 agent/、core/、仓库根）"
    )


def _resolve_site(site: str, *, want_literal: str = "") -> tuple[Path, object, str]:
    """把 `"模块.函数"` 解析到真实的函数节点。

    返回 (文件, 函数节点, 函数源码)。任何一步失败抛 GuardError（可读，不是 traceback）。
    want_literal 非空时，还要求函数源码里真的出现这个字符串。
    """
    raw = str(site or "").strip()
    if not raw:
        raise GuardError("站点写成了空串")
    if "/" in raw or "\\" in raw or raw.endswith(".py"):
        raise GuardError(f"站点 {raw!r} 应写成 `模块.函数`（不带路径与 .py）")
    parts = [part for part in raw.split(".") if part]
    if len(parts) < 2:
        raise GuardError(f"站点 {raw!r} 缺函数名：应写成 `模块.函数`")
    if parts[0] in ("agent", "core", "eval"):
        parts = parts[1:] or parts
        if len(parts) < 2:
            raise GuardError(f"站点 {raw!r} 缺函数名：应写成 `模块.函数`")
    stem, func = parts[0], parts[-1]
    if len(parts) > 2:
        # agent.windows.foo 这种：只认最后两段，其余当目录忽略（今天没有这种情况）。
        stem = parts[-2]
    path = _module_path(stem)
    src, tree = _parse(path)
    node = _find_function(tree, func)
    rel = path.relative_to(ROOT).as_posix()
    if node is None:
        raise GuardError(f"{rel} 里找不到函数 {func}（站点 {raw!r}）")
    segment = ast.get_source_segment(src, node) or ""
    if want_literal and want_literal not in segment:
        raise GuardError(
            f"站点 {raw!r} → {rel}:{node.lineno} 的函数体里没出现 {want_literal!r}"
            f"（点到了存在但无关的函数）"
        )
    return path, node, segment


def executor_actions() -> set:
    """静态解析 sequence._run_action 的 `action == X` / `action in (...)` 分支。

    不读任何人工表 —— 这就是 §4.2 的方向 1。解析不到（函数改名/挪走）要红，不当成空集放过。
    """
    src, tree = _parse(SEQUENCE_SRC)
    target = _find_function(tree, EXECUTOR_FUNC)
    rel = SEQUENCE_SRC.relative_to(ROOT).as_posix()
    if target is None:
        raise GuardError(f"{rel} 里找不到函数 {EXECUTOR_FUNC}（执行器分支解析失败）")
    out: set = set()
    for node in ast.walk(target):
        if not (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "action"
        ):
            continue
        # 只认 `action <op> ...` 左边的动作名；比较符不区分（== / in / != 的右侧都是动作名）
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                out.add(comparator.value)
            elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                for element in comparator.elts:
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        out.add(element.value)
    del src
    return out


_BRIEF_ACTION_RE = re.compile(r"^- ([a-z_][a-z0-9_]*)。", re.M)


def brief_prompt_actions() -> set:
    """brief_system_prompt 里真正播出去的动作名。

    [事实] agent/brief.py:219-225 遍历 CATALOG.items() 并跳过 INTEGRATE/DISPOSE，
    每行形如 `- {name}。槽位：…`。这里按同一形状抓，不 import 它的内部实现。
    """
    from agent.brief import brief_system_prompt

    try:
        text = brief_system_prompt()
    except Exception as exc:  # 生成提示本身炸了也要可读，不要裸 traceback
        raise GuardError(f"agent.brief.brief_system_prompt() 调用失败：{exc}") from exc
    return set(_BRIEF_ACTION_RE.findall(text or ""))


def planner_actions(catalog_mod) -> set:
    """iter_prompt_specs 的动作集：CATALOG 去掉 INTEGRATE/DISPOSE，再加人审通过的 promoted。

    [事实] agent/catalog.py:288-301。
    """
    try:
        return {str(name) for name, _ in catalog_mod.iter_prompt_specs()}
    except Exception as exc:
        raise GuardError(f"agent.catalog.iter_prompt_specs() 调用失败：{exc}") from exc


# ---------------------------------------------------------------- 断言
def check_i1(executor: set, planner: set, catalog_mod, problems: list) -> None:
    """I1：执行器支持但规划者看不见 ⇒ 必须在白名单里，且每个插入点都能解析到。"""
    code_only = executor - planner
    for name in sorted(code_only):
        if name not in CODE_ONLY_ALLOWED:
            problems.append(
                f"I1 {name}：sequence.{EXECUTOR_FUNC} 能跑，但规划者看不见，且不在 CODE_ONLY_ALLOWED"
            )
            continue
        sites = CODE_ONLY_ALLOWED[name]
        if not sites:
            problems.append(f"I1 {name}：在白名单里但没写 inserted_by")
            continue
        for site in sites:
            try:
                _resolve_site(site, want_literal=name)
            except GuardError as exc:
                problems.append(f"I1 {name}：inserted_by 站点不成立 —— {exc}")

    # 反向：白名单里的键必须在执行器里存在（拼错名字/动作被删了都要红）。
    for name in sorted(CODE_ONLY_ALLOWED):
        if name not in executor:
            problems.append(
                f"I1 {name}：在 CODE_ONLY_ALLOWED 里，但 sequence.{EXECUTOR_FUNC} 没有这个分支"
                f"（白名单过期或名字拼错）"
            )

    # 七件套第 2 项（派生）：执行器认的动作，必须至少被一条调度白名单认，
    # 否则 materialize 的 allows() 放行而调度不认。 `[事实]` 今天 8 条代码专用扇都在 DIRECT_ACTIONS 里。
    direct = set(getattr(catalog_mod, "DIRECT_ACTIONS", ()) or ())
    lock = set(getattr(catalog_mod, "EDITOR_LOCK_ACTIONS", ()) or ())
    for name in sorted(executor):
        if name not in direct and name not in lock:
            problems.append(
                f"I1 {name}：执行器有分支，但既不在 DIRECT_ACTIONS 也不在 EDITOR_LOCK_ACTIONS"
                f"（能执行但调度不认）"
            )


def check_i2(executor: set, planner: set, catalog_mod, problems: list) -> None:
    """I2：规划者能写 ⇒ 执行器必须认（或走 GRAPH_ACTIONS 的串行路径）。"""
    graph = set(getattr(catalog_mod, "GRAPH_ACTIONS", ()) or ())
    for name in sorted(planner - (executor | graph)):
        problems.append(
            f"I2 {name}：规划者能写（iter_prompt_specs 播得出去），但 sequence.{EXECUTOR_FUNC} "
            f"不认、也不在 GRAPH_ACTIONS —— 能规划、不能执行（最坏：假成功）"
        )


def check_i3(planner: set, catalog_mod, kind_by_action: dict, problems: list) -> None:
    """I3：能写东西的动作必须有裁判（kind 映射 / 合成裁判 / 已登记的待补缺口）。"""
    read_only = set(getattr(catalog_mod, "READ_ONLY_ACTIONS", ()) or ())
    verify = getattr(catalog_mod, "_VERIFY", {}) or {}
    scope = planner - read_only - NO_EVIDENCE_NEEDED
    for name in sorted(scope):
        if kind_by_action.get(name):
            continue
        if name in SYNTHETIC_JUDGES:
            continue
        entry = PENDING_JUDGE.get(name)
        if entry is None:
            problems.append(
                f"I3 {name}：能写东西，但既没有 _KIND_BY_ACTION 映射，也没有合成裁判，"
                f"也不在 PENDING_JUDGE —— 跑完没有任何裁判，只能靠模型自述"
            )
            continue
        # 待补条目本身也要逐条核实：站点可解析 + 站点里真的出现这个动作名 + 没有 judge_kind +
        # catalog 有验收口径。这样这张表不能靠改条目混过去，producer 一补判词就红。
        producer_sites = list(entry.get("producer") or ())
        if not producer_sites:
            problems.append(f"I3 {name}：PENDING_JUDGE 没写 producer 站点（等于空口无凭）")
        for site in producer_sites:
            try:
                _path, _node, segment = _resolve_site(site, want_literal=name)
            except GuardError as exc:
                problems.append(f"I3 {name}：PENDING_JUDGE 的 producer 站点不成立 —— {exc}")
                continue
            if "judge_kind" in segment:
                problems.append(
                    f"I3 {name}：producer（{site}）已经声明了 judge_kind，"
                    f"该把映射搬进 _KIND_BY_ACTION —— PENDING_JUDGE 这条过期了"
                )
        if not str(entry.get("why") or "").strip():
            problems.append(f"I3 {name}：PENDING_JUDGE 没写 why（不许只登记不解释）")
        if not (verify.get(name) or ()):
            problems.append(
                f"I3 {name}：PENDING_JUDGE 条目要求 catalog._VERIFY 里有验收口径，今天没有 —— "
                f"既没说怎么验、也没裁判，这条动作不该在 CATALOG 里"
            )

    # 反向：PENDING_JUDGE 里的动作必须真的还缺映射，且真的还需要裁判（不许拿它藏只读动作）。
    for name in sorted(PENDING_JUDGE):
        if kind_by_action.get(name):
            problems.append(
                f"I3 {name}：已经在 _KIND_BY_ACTION 里了，PENDING_JUDGE 这条要删（两侧必须同时动）"
            )
        if name in read_only:
            problems.append(f"I3 {name}：只读动作不需要裁判，不该进 PENDING_JUDGE")


def check_i4(executor: set, planner: set, kind_by_action: dict, evidence_kinds, problems: list) -> None:
    """I4：映射出来的 kind 必须在 EVIDENCE_KINDS 全集里；合成裁判的 kind 同理（双向）。"""
    allowed = set(evidence_kinds)
    for name in sorted(kind_by_action):
        kind = kind_by_action[name]
        if kind not in allowed:
            problems.append(f"I4 {name} 的 kind={kind!r} 不在 EVIDENCE_KINDS（{sorted(allowed)}）")
    for name in sorted(SYNTHETIC_JUDGES):
        entry = SYNTHETIC_JUDGES[name]
        kind = str(entry.get("kind") or "")
        if not kind:
            problems.append(f"I4 {name}：SYNTHETIC_JUDGES 条目没写 kind")
            continue
        if kind not in allowed:
            problems.append(f"I4 {name} 的合成裁判 kind={kind!r} 不在 EVIDENCE_KINDS")
        sites = entry.get("site") or ()
        if not sites:
            problems.append(f"I4 {name}：SYNTHETIC_JUDGES 条目没写 site（不许空表混过）")
        for site in sites:
            try:
                _path, _node, segment = _resolve_site(site, want_literal=kind)
            except GuardError as exc:
                problems.append(f"I4 {name}：合成裁判站点不成立 —— {exc}")
                continue
            # 站点必须同时认这个 kind 和这个动作：只认一边就等于把判决挂错了扇。
            if name not in segment:
                problems.append(
                    f"I4 {name}：合成裁判站点 {site} 里没出现动作名 {name!r}（判决挂错了扇）"
                )
        if not str(entry.get("why") or "").strip():
            problems.append(f"I4 {name}：SYNTHETIC_JUDGES 条目没写 why")


    # 反向：SYNTHETIC_JUDGES 的键必须仍是一个真实动作（执行器或规划者认识它），否则这条登记
    # 挂在了一个已经不存在的动作上 —— 过期登记会让守卫以为「这扇有裁判」而放过新的缺口。
    #
    # 注意：**不**断言「已经有 _KIND_BY_ACTION 映射的就不许有合成裁判」。实测
    # edit_widget_blueprint 就是两者都有：它自己有 blueprint_compile 的正式映射（控件图编译），
    # 而同一条窗口跑完还要额外现造一条 widget_graph 判决（run_windows.py:863-866），
    # 那是**另一个 kind 的条款**，不是替代品。按「有映射就删合成」判会冤杀真实设计。
    known = set(executor) | set(planner)
    for name in sorted(SYNTHETIC_JUDGES):
        if name not in known:
            problems.append(
                f"I4 {name}：在 SYNTHETIC_JUDGES 里，但执行器和规划者都不认识这个动作"
                f"（过期登记或名字拼错）"
            )


def check_i5(planner: set, problems: list) -> None:
    """I5：任务书提示与规划者提示必须看见同一套动作（D1 的第二条看不见的路径）。"""
    brief = brief_prompt_actions()
    if brief != planner:
        only_planner = sorted(planner - brief)
        only_brief = sorted(brief - planner)
        problems.append(
            f"I5 任务书提示（brief_system_prompt）与规划者提示（iter_prompt_specs）动作集不一致："
            f"只在规划者={only_planner}；只在任务书={only_brief}"
        )
    if not planner:
        problems.append("I5 iter_prompt_specs() 一个动作都没给出 —— 规划者提示空了")


# ---------------------------------------------------------------- 入口
def main() -> int:
    try:
        from agent import catalog as catalog_mod
        from agent.harness import EVIDENCE_KINDS, _KIND_BY_ACTION
    except Exception as exc:
        print("catalog parity FAILED")
        print(f"  - I0 导入 agent.catalog / agent.harness 失败：{exc}")
        return 1

    problems: list = []
    try:
        executor = executor_actions()
        if not executor:
            problems.append(
                f"I1 sequence.{EXECUTOR_FUNC} 里一个动作分支都没解析到（解析口径或函数体变了）"
            )
        planner = planner_actions(catalog_mod)
        kind_by_action = dict(_KIND_BY_ACTION or {})
        check_i1(executor, planner, catalog_mod, problems)
        check_i2(executor, planner, catalog_mod, problems)
        check_i3(planner, catalog_mod, kind_by_action, problems)
        check_i4(executor, planner, kind_by_action, EVIDENCE_KINDS, problems)
        check_i5(planner, problems)
    except GuardError as exc:
        problems.append(f"I1 静态解析失败：{exc}")
    except Exception as exc:  # 兜底：任何意外都要变成可读的一行，不是裸 traceback
        problems.append(f"I0 守卫内部错误：{type(exc).__name__}: {exc}")

    if problems:
        print("catalog parity FAILED")
        for line in problems:
            print("  -", line)
        return 1
    print(
        f"catalog parity ok（执行器 {len(executor)} 扇、规划者 {len(planner)} 扇、"
        f"白名单 {len(CODE_ONLY_ALLOWED)} 条）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
