# agent/lexicon.py
"""术语→动作映射（D3）：把用户动词映射到「已有的动作」或「显式 unplannable」。

真值源两层，互不冒名：
- `data/skills/lexicon.json` 是**人写**的表（维护者见文件里的 owner/note），代码只读；
- `agent/catalog.py` 的 `CATALOG` 是**动作**真值源，本模块只引不改，lexicon 里出现表外动作
  一律由 `verify()` 报出来，绝不放宽 `allows()` 门（next-upgrade-plan.md §5.3 断言 3/4）。

本模块是纯标准库、纯只读：
- 导入时不拉起编辑器、不叫模型、不做检索（`config` 与 `agent.catalog` 都是函数内延迟 import）；
- 不写盘、不抛异常：文件缺失 / 坏 JSON → `[]`，`verify()` 只收集问题字符串。

匹配口径（`lookup` / `action_for` / `unplannable` 共用）：
- 文本与动词都先小写、把连续空白收敛成一个空格；
- 非 ASCII 动词（中文）按**子串**匹配，比较时把空白整个去掉（「新建 一个关卡」也接得住）；
- ASCII 动词（WASD / FPS / BP_ / hit 等）要求左侧不是字母数字下划线，避免 "hit" 命中 "white"。
`verbs` 参与动作映射；`aliases`（可选）只是「相关但不做结论」的弱线索，只进 `lookup` 与
`uncovered` 排查，不参与 `action_for` / `unplannable` 的结论。
"""
import re
from pathlib import Path

# 只由代码追加的两个动作（catalog.py:2 的约定）。规划者可见的 lexicon 行不许指向它们：
# 指向了就是「把代码专用动作偷偷变成可规划动作」，verify() 要报出来。
_CODE_ONLY_ACTIONS = frozenset({"integrate_verify", "dispose_residual"})

_ASCII_ONLY = re.compile(r"^[\x00-\x7f]+$")
_SPACES = re.compile(r"\s+")


def lexicon_path() -> Path:
    """lexicon.json 的位置。风格对齐 agent/catalog.py:_skills_dir_file（catalog.py:212）：
    `UE_LEXICON` 显式覆盖优先，其次 `UE_LEARNED_ROUTES` 的同目录，最后落 config.DATA_DIR。

    与 `_skills_dir_file` 的唯一差别：`UE_LEARNED_ROUTES` 同级**不存在** lexicon.json 时落回
    `config.DATA_DIR/skills/`，而不是返回一个不存在的路径。原因是离线检查几乎都把
    `UE_LEARNED_ROUTES` 指向临时目录做隔离（eval/check_*.py 全是这么写的），照抄会让本表在
    所有离线检查里静默消失、挂接永远不被覆盖——本模块是只读的，没有写污染的顾虑。"""
    import os

    override = os.environ.get("UE_LEXICON", "").strip()
    if override:
        return Path(override)
    routes = os.environ.get("UE_LEARNED_ROUTES", "").strip()
    if routes:
        sibling = Path(routes).with_name("lexicon.json")
        if sibling.is_file():
            return sibling
    from config import DATA_DIR

    return DATA_DIR / "skills" / "lexicon.json"


def load_rows() -> list[dict]:
    """读表里的 rows。文件缺失、坏 JSON、rows 不是列表 → []（不抛）。"""
    import json

    try:
        path = lexicon_path()
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _squeeze(text) -> str:
    """小写 + 空白收敛成一个空格。ASCII 词的前边界检查用这一份。"""
    return _SPACES.sub(" ", str(text or "")).strip().lower()


def _tight(text) -> str:
    """小写 + 去掉所有空白。中文子串匹配用这一份（「新建 一个关卡」与「新建一个关卡」等价）。"""
    return _SPACES.sub("", str(text or "")).lower()


def _words(row: dict, key: str) -> list[str]:
    values = row.get(key)
    if not isinstance(values, list):
        return []
    return [word for word in values if isinstance(word, str) and word.strip()]


def _hit(hay_squeeze: str, hay_tight: str, word: str) -> bool:
    squeezed = _squeeze(word)
    if not squeezed:
        return False
    if _ASCII_ONLY.match(squeezed):
        # 左侧不许是字母/数字/下划线：'hit' 不命中 'white'，'BP_' 仍能命中 'BP_Player'。
        return re.search(r"(?<![a-z0-9_])" + re.escape(squeezed), hay_squeeze) is not None
    return _tight(squeezed) in hay_tight


def _match(text: str, *, weak: bool) -> list[dict]:
    """命中的行，保持表内顺序、去重。weak=True 时把 `aliases` 也算作命中线索。"""
    hay_squeeze = _squeeze(text)
    hay_tight = _tight(text)
    if not hay_tight:
        return []
    found: list[dict] = []
    seen: set[int] = set()
    for row in load_rows():
        if id(row) in seen:
            continue
        words = _words(row, "verbs") + (_words(row, "aliases") if weak else [])
        if any(_hit(hay_squeeze, hay_tight, word) for word in words):
            seen.add(id(row))
            found.append(row)
    return found


def lookup(text: str) -> list[dict]:
    """命中的行，保持表内顺序、去重。`verbs` 与 `aliases` 都算命中线索。"""
    return _match(text, weak=True)


def action_for(text: str) -> str | None:
    """第一个指向「真在 CATALOG 里」的动作名；unplannable 行不算，代码专用动作不算。

    只有 `verbs` 参与这个结论：`aliases` 是弱线索（例如裸 BP_），让它参与会被「编译这张
    蓝图 BP_X」这类句子带偏（先命中创建行、后命中编译行），所以这里用 verbs 严格匹配。"""
    catalog = _catalog_actions()
    if not catalog:
        return None
    for row in _match(text, weak=False):
        if row.get("unplannable") is True:
            continue
        action = row.get("action")
        if not isinstance(action, str):
            continue
        name = action.strip()
        if name and name in catalog and name not in _CODE_ONLY_ACTIONS:
            return name
    return None


def unplannable(text: str) -> list[dict]:
    """命中的 unplannable 行（原文整行，含 reason / suggested_path 供调用方取用）。

    同样只认 `verbs`：宁可少标一条缺口，也不许靠弱线索把一句无关的话标成 unplannable。"""
    return [row for row in _match(text, weak=False) if row.get("unplannable") is True]


def uncovered(texts: list[str]) -> list[str]:
    """一条 lexicon 行都没接住的文本（空白文本不算缺口）。供 check_lexicon.py 与人工排查。"""
    missing: list[str] = []
    for text in texts or []:
        value = str(text or "").strip()
        if not value:
            continue
        if not lookup(value):
            missing.append(text)
    return missing


def _catalog_actions() -> set[str]:
    """CATALOG 的动作名。延迟 import（catalog 不 import 本模块，无环），失败就返回空集。"""
    try:
        from agent.catalog import CATALOG

        return set(CATALOG)
    except Exception:
        return set()


def verify() -> list[str]:
    """结构问题清单，空列表 = 通过。只读、不写盘、不抛异常。

    检查项：每行二选一（既无 action 又不是 unplannable、或两者都有）、unplannable 缺
    reason/suggested_path、action 不在 CATALOG 或指向代码专用动作、verbs 为空或非列表、
    aliases 非列表（可选字段写错也算结构问题）。
    """
    issues: list[str] = []
    try:
        path = lexicon_path()
        if not path.is_file():
            return [f"lexicon.json 不存在：{path}"]
        rows = load_rows()
        if not rows:
            return [f"lexicon.json 没有可用的 rows：{path}"]
        catalog = _catalog_actions()
        for index, row in enumerate(rows, 1):
            where = f"第 {index} 行"
            verbs = row.get("verbs")
            if not isinstance(verbs, list) or not verbs:
                issues.append(f"{where}：verbs 为空或非列表")
            elif any(not isinstance(word, str) or not word.strip() for word in verbs):
                issues.append(f"{where}：verbs 里有空项或非字符串")
            if "aliases" in row:
                aliases = row.get("aliases")
                if not isinstance(aliases, list) or any(
                    not isinstance(word, str) or not word.strip() for word in aliases
                ):
                    issues.append(f"{where}：aliases 必须是字符串列表（可空）")
            has_action = isinstance(row.get("action"), str) and bool(str(row.get("action")).strip())
            if "unplannable" in row and row.get("unplannable") is not True:
                issues.append(f"{where}：unplannable 必须是 true")
            is_gap = row.get("unplannable") is True
            if has_action and is_gap:
                issues.append(f"{where}：既指向动作 {row.get('action')} 又标 unplannable，二选一")
            elif not has_action and not is_gap:
                issues.append(f"{where}：既没有 action 也没标 unplannable")
            if is_gap:
                if not str(row.get("reason") or "").strip():
                    issues.append(f"{where}：unplannable 缺 reason")
                if not str(row.get("suggested_path") or "").strip():
                    issues.append(f"{where}：unplannable 缺 suggested_path")
            if has_action:
                action = str(row.get("action")).strip()
                if catalog and action not in catalog:
                    issues.append(f"{where}：action {action} 不在 CATALOG（lexicon 不许发明动作）")
                if action in _CODE_ONLY_ACTIONS:
                    issues.append(f"{where}：action {action} 只由代码追加，规划者可见的 lexicon 不许指向它")
    except Exception as exc:  # 体检自己坏了也要交出可读字符串，不抛给调用方
        issues.append(f"verify 自身异常：{exc}")
    return issues
