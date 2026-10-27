# eval/check_lexicon.py
"""D3 lexicon 离线验收（data/skills/lexicon.json + agent/lexicon.py）。

不放宽任何闸门：lexicon 只是「术语 → 已有动作 / 显式 unplannable」的人写映射表，
动作真值源仍是 agent/catalog.py 的 CATALOG（next-upgrade-plan.md §5.3、gap-taxonomy.md 附录 E.1）。

七组断言：
1) 每行二选一：有 action **xor** 标了 unplannable；
2) unplannable 必带 reason **且** suggested_path；
3) {row["action"]} ⊆ set(CATALOG)，且不指向代码专用的 integrate_verify / dispose_residual；
4) 覆盖断言：读真实失败 run 的事件流第 2 行，「新建一个关卡」必须被**动作行**接住
   （action_for == create_level）。FPS 玩法不在术语表里硬绑动作，交给分解器。
5) agent.lexicon.verify() == []；
6) 反例守卫：空文本 / 无关中文句 lookup() 返回 []、action_for() 返回 None，不许乱匹配；
7) 每条 unplannable 行的 verbs 非空（D3 §5.4 第 5 步的 note_candidate 只依赖动词名，
   这里只做静态一致性断言，不真的写候选表）。

另加：模块导入不拉编辑器/模型/检索（AST 扫顶层 import）、文件缺失与坏 JSON 都不抛、
环境变量 UE_LEXICON 可覆盖路径、lookup 保持表内顺序且去重。

不打开编辑器、不调模型、不联网；只碰临时目录，不写仓库任何状态。"""
import ast
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LEXICON_JSON = ROOT / "data" / "skills" / "lexicon.json"
EVENTS = ROOT / "data" / "events" / "20261024-192858.jsonl"

# 真实 run 的两条 unresolved 原文（data/events/20261024-192858.jsonl 第 2 行）。
# data/events 是 gitignore 的，CI 上没有这个文件时用同形状内联替身，断言照样跑。
RUN_UNRESOLVED = [
    "新建一个关卡，名字为L_NewTest2（动作表无新建关卡动作）",
    "在这个关卡实现简易的FPS射击游戏逻辑（内容自由发挥，无点名类/蓝图/资产，无法落到具体动作）",
]
CODE_ONLY = ("integrate_verify", "dispose_residual")


def _rows() -> list[dict]:
    data = json.loads(LEXICON_JSON.read_text(encoding="utf-8"))
    rows = data.get("rows")
    assert isinstance(rows, list) and rows, "lexicon.json 没有 rows"
    return rows


def check_file_shape() -> None:
    """顶层字段齐全，行是对象。"""
    data = json.loads(LEXICON_JSON.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "lexicon.json 顶层必须是对象"
    for key in ("version", "owner", "note", "rows"):
        assert key in data, f"lexicon.json 顶层缺 {key}"
    assert isinstance(data["version"], int) and data["version"] >= 1, data["version"]
    assert str(data["owner"]).strip(), "owner 不能为空（本表是人写的白名单）"
    assert str(data["note"]).strip(), "note 不能为空"
    assert all(isinstance(row, dict) for row in data["rows"]), "rows 里每行必须是对象"


def check_either_or() -> None:
    """断言 1：每行「指向动作」与 unplannable 二选一。"""
    for index, row in enumerate(_rows(), 1):
        has_action = isinstance(row.get("action"), str) and bool(str(row["action"]).strip())
        is_gap = row.get("unplannable") is True
        assert has_action ^ is_gap, f"第 {index} 行必须二选一（action 或 unplannable）：{row}"
        assert not (has_action and row.get("unplannable")), f"第 {index} 行两者都有：{row}"


def check_unplannable_fields() -> None:
    """断言 2 + 断言 7：unplannable 必带 reason 与 suggested_path；verbs 非空。

    断言 7 是**静态**一致性：D3 §5.4 第 5 步要用 verbs 里的动词名去 `note_candidate`，
    而 `note_candidate` 对「已经在 CATALOG 里的名字」返回 None（catalog.py:304-307）——
    所以每条 unplannable 行至少要有一个动词不是动作名，否则那条缺口根本记不进候选表。
    这里不真的调 note_candidate（不写盘），只按它的字段判据做静态断言。"""
    from agent.catalog import CATALOG

    gaps = [(index, row) for index, row in enumerate(_rows(), 1) if row.get("unplannable") is True]
    assert gaps, "一条 unplannable 都没有：FPS 玩法那些缺口会被静默吞掉"
    for index, row in gaps:
        assert str(row.get("reason") or "").strip(), f"第 {index} 行 unplannable 缺 reason"
        assert str(row.get("suggested_path") or "").strip(), f"第 {index} 行 unplannable 缺 suggested_path"
        verbs = row.get("verbs")
        assert isinstance(verbs, list) and verbs, f"第 {index} 行 verbs 为空（候选记录依赖动词名）"
        assert all(isinstance(word, str) and word.strip() for word in verbs), f"第 {index} 行 verbs 有空项"
        recordable = [word for word in verbs if word.strip() not in CATALOG]
        assert recordable, (
            f"第 {index} 行 unplannable 的 verbs 全是已存在的动作名，note_candidate 会返回 None，"
            f"缺口记不进候选表：{verbs}"
        )


# 附录 E 草稿的 14 行覆盖（动词 → 期望）。做成显式清单，防止以后有人删到缺覆盖。
# 草稿第 2 行写的是 create_blueprint，但**那个动作不在 CATALOG**（实测 CATALOG 21 项，
# 见 next-upgrade-plan.md D1）：按「lexicon 不许发明动作」的铁律，本表落到 bind_blueprint
# ——它的 summary 就是「没有资产、且给了 /Game 路径时才新建，不改图」（catalog.py:26-30）。
DRAFT_COVERAGE = (
    ("新建关卡", "create_level"),
    ("创建蓝图", "bind_blueprint"),
    ("打开/启动编辑器", "launch_editor"),
    ("编译某张蓝图", "compile_blueprint"),
    ("编译工程", "compile_project"),
    ("加输入", None),
    ("开火/射击/命中/子弹/伤害", "open"),
    ("计分/HUD/文本反馈/结算", "edit_widget_blueprint"),
    ("移动/移到/坐标", "move_actor"),
    ("摆放/布置/放进去", "place_actor"),
    ("批量摆", "place_batch"),
    ("删掉多余的/离群实例", "remove_batch"),
    ("材质参数", "set_scalar_param"),
    ("跑测试/验证/自动化", "run_automation_test"),
    ("改蓝图图/加节点/连线", None),
)


def check_draft_coverage() -> None:
    """附录 E 草稿的 14 行必须都还落着；草稿想指向 create_blueprint 的那行必须指向真存在的动作。"""
    from agent import lexicon
    from agent.catalog import CATALOG

    assert "create_blueprint" not in CATALOG, (
        "create_blueprint 已进 CATALOG：本表第 2 行应改回 create_blueprint（D1 落地后同步）"
    )
    samples = {
        "新建关卡": "新建一个关卡",
        "创建蓝图": "新建蓝图 BP_Player",
        "打开/启动编辑器": "打开编辑器",
        "编译某张蓝图": "编译这张蓝图",
        "编译工程": "编译工程",
        "加输入": "加个输入映射",
        "开火/射击/命中/子弹/伤害": "实现开火与命中判定",
        "计分/HUD/文本反馈/结算": "左上角计分 HUD",
        "移动/移到/坐标": "把 Cube 移到 100,200,50",
        "摆放/布置/放进去": "把这个蓝图放进去",
        "批量摆": "按设计图批量摆一排",
        "删掉多余的/离群实例": "删掉多余的离群实例",
        "材质参数": "材质参数改成 0.5",
        "跑测试/验证/自动化": "跑测试验证一下",
        "改蓝图图/加节点/连线": "给蓝图加节点并连线",
    }
    for label, expected in DRAFT_COVERAGE:
        text = samples[label]
        if expected == "open":
            assert lexicon.action_for(text) is None, f"草稿行「{label}」不该再硬绑动作"
            assert lexicon.unplannable(text) == [], f"草稿行「{label}」不该标成缺口"
        elif expected is None:
            assert lexicon.unplannable(text), f"草稿行「{label}」应落成 unplannable，实际没接住"
            assert lexicon.action_for(text) is None or label == "材质参数", (
                f"草稿行「{label}」不该被映射成一个动作：{lexicon.action_for(text)!r}"
            )
        else:
            assert lexicon.action_for(text) == expected, f"草稿行「{label}」应映射到 {expected}"


def check_actions_in_catalog() -> None:
    """断言 3：lexicon 不许发明动作，也不许把代码专用动作变成可规划动作。"""
    from agent.catalog import CATALOG

    names = {row["action"].strip() for row in _rows() if str(row.get("action") or "").strip()}
    assert names, "lexicon 里一条动作映射都没有"
    assert names <= set(CATALOG), f"lexicon 发明了动作：{sorted(names - set(CATALOG))}"
    assert not (names & set(CODE_ONLY)), f"lexicon 不许指向代码专用动作：{sorted(names & set(CODE_ONLY))}"


def _run_unresolved() -> tuple[list[str], bool]:
    """真实 run 的两条 unresolved：事件流在就用文件里的原文，不在就用内联替身。
    返回 (文本, 是否真实件)。真实件的内容必须与内联替身一致，防止有人改了证据却以为断言还成立。"""
    if not EVENTS.is_file():
        return list(RUN_UNRESOLVED), False
    lines = EVENTS.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2, f"{EVENTS} 不足两行，取不到 intent 事件"
    event = json.loads(lines[1])
    assert event.get("type") == "intent", event.get("type")
    found = [str(item) for item in (event.get("unresolved") or [])]
    assert len(found) >= 2, f"第 2 行没有两条 unresolved：{found}"
    assert found[:2] == RUN_UNRESOLVED, f"真实证据与内联替身不一致：{found[:2]}"
    return found[:2], True


def check_graph_rows_keep_namespace() -> None:
    """蓝图/图相关的 unplannable 行要留下 namespace，好让规划层先试 Monolith，而不是零窗口。"""
    wanted = {
        "空白关卡": "mesh",
        "加输入": "input",
        "改蓝图图": "blueprint",
    }
    for row in _rows():
        verbs = [str(word) for word in (row.get("verbs") or [])]
        for verb, namespace in wanted.items():
            if verb in verbs:
                assert row.get("unplannable") is True, row
                assert row.get("namespace") == namespace, (verb, row.get("namespace"))
                wanted.pop(verb)
                break
    assert not wanted, f"缺 namespace 行：{sorted(wanted)}"


def check_run_coverage() -> None:
    """断言 4：真实 run 的两条 unresolved 分别被动作行与 unplannable 行接住。"""
    from agent import lexicon

    texts, is_real = _run_unresolved()
    first, second = texts
    assert lexicon.action_for(first) == "create_level", (
        f"「新建一个关卡」必须被动作行接住，实际 {lexicon.action_for(first)!r}"
    )
    assert lexicon.action_for(first) not in CODE_ONLY
    assert lexicon.action_for(second) is None, (
        f"FPS 玩法不该被术语表硬绑，实际 {lexicon.action_for(second)!r}"
    )
    assert lexicon.unplannable(second) == [], f"FPS 不该再标成缺口：{second}"
    assert lexicon.uncovered([first]) == [], "「新建一个关卡」必须被术语表接住"
    if is_real:
        print("  覆盖断言用的是真实事件流 data/events/20261024-192858.jsonl 第 2 行")


def check_verify_clean() -> None:
    """断言 5：结构体检为空。"""
    from agent import lexicon

    issues = lexicon.verify()
    assert issues == [], "lexicon.verify() 报了问题：" + "；".join(issues)


def check_no_wild_match() -> None:
    """断言 6：反例守卫——空文本与无关中文句都不许匹配。"""
    from agent import lexicon

    for text in ("", "   ", "\n\t", "把桌子擦干净", "把桌子擦干净，再泡杯茶", "今天天气不错"):
        rows = lexicon.lookup(text)
        assert rows == [], f"{text!r} 不该被 lexicon 接住：{rows}"
        assert lexicon.action_for(text) is None, f"{text!r} 不该映射到动作"
        assert lexicon.unplannable(text) == [], f"{text!r} 不该被标成能力缺口"
    assert lexicon.uncovered(["把桌子擦干净"]) == ["把桌子擦干净"], "未覆盖排查没报出来"


def check_match_rules() -> None:
    """匹配口径：中文按子串、大小写与空白归一、表内顺序、去重、ASCII 词不做词内命中。"""
    from agent import lexicon

    assert lexicon.action_for("新建 一个关卡") == "create_level", "空白没归一"
    assert lexicon.action_for("编译这张蓝图") == "compile_blueprint", "「编译这张蓝图」被别行抢走"
    assert lexicon.action_for("编译整个工程") == "compile_project", "「编译整个工程」被别行抢走"
    assert lexicon.action_for("打开UE编辑器") == "launch_editor"
    assert lexicon.action_for("材质参数改成 0.5") == "set_scalar_param"
    assert lexicon.action_for("读材质参数") == "query_material", "只读的读参数动作该排在前面"
    # ASCII 词要求左侧不是字母数字：hit 不许命中 white
    assert lexicon.lookup("这是一个 white box") == [], "ASCII 词做了词内命中"
    assert lexicon.action_for("做成 fps 玩法") is None, "FPS 不该被术语表硬绑"
    # 顺序与去重：命中的行按表内顺序返回，且不重复
    rows = lexicon.lookup("先新建一个关卡，再新建一张蓝图，最后编译工程")
    indexes = [_rows().index(row) for row in rows]
    assert indexes == sorted(indexes), "lookup 没保持表内顺序"
    assert len(indexes) == len(set(indexes)), "lookup 有重复行"
    # 一条行同时是动作行与 unplannable 行时，action_for 只认动作行
    assert lexicon.action_for("加个计分HUD") == "edit_widget_blueprint"
    assert lexicon.unplannable("加个输入映射"), "「加输入」必须是 unplannable"


def check_missing_and_bad_file() -> None:
    """文件缺失 / 坏 JSON 都返回 [] 且不抛；UE_LEXICON 能覆盖路径。"""
    from agent import lexicon

    saved = os.environ.get("UE_LEXICON")
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope.json"
        broken = Path(tmp) / "broken.json"
        broken.write_text("{ 这不是 JSON", encoding="utf-8")
        try:
            os.environ["UE_LEXICON"] = str(missing)
            assert lexicon.lexicon_path() == missing, lexicon.lexicon_path()
            assert lexicon.load_rows() == [], "文件缺失必须返回 []"
            assert lexicon.lookup("新建一个关卡") == []
            assert lexicon.action_for("新建一个关卡") is None
            assert lexicon.unplannable("开火") == []
            assert lexicon.verify(), "文件缺失时 verify() 必须报出来，不许静默通过"
            os.environ["UE_LEXICON"] = str(broken)
            assert lexicon.load_rows() == [], "坏 JSON 必须返回 [] 而不是抛"
            assert lexicon.verify(), "坏 JSON 时 verify() 必须报出来"
        finally:
            if saved is None:
                os.environ.pop("UE_LEXICON", None)
            else:
                os.environ["UE_LEXICON"] = saved
    # 恢复后一切照旧
    assert lexicon.load_rows(), "UE_LEXICON 恢复后读不到真实表"


def check_import_is_light() -> None:
    """导入本模块不许拉起编辑器 / 模型 / 检索：顶层 import 只能是标准库。"""
    source = (ROOT / "agent" / "lexicon.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned = {"agent", "core", "config", "openai", "chromadb", "requests", "urllib"}
    for node in tree.body:  # 只看顶层语句；函数内延迟 import 是允许的
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            assert name.split(".")[0] not in banned, f"agent/lexicon.py 顶层 import 了 {name}（会拉重依赖）"
    # 延迟 import 也必须是真的在函数里（上面已排除顶层），这里再钉一次运行期副作用：
    loaded = {name for name in ("core.editor_exec", "agent.understand", "core.llm_call") if name in sys.modules}
    assert not loaded, f"导入 agent.lexicon 顺带加载了 {sorted(loaded)}"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not EVENTS.is_file():
        print("data/events 里没有 20261024-192858 的事件流（data/ 是 gitignore 的，CI 上不会有）："
              "用同形状内联替身跑覆盖断言")
    check_file_shape()
    check_either_or()
    check_unplannable_fields()
    check_actions_in_catalog()
    check_draft_coverage()
    check_graph_rows_keep_namespace()
    check_run_coverage()
    check_verify_clean()
    check_no_wild_match()
    check_match_rules()
    check_missing_and_bad_file()
    check_import_is_light()
    print("lexicon ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
