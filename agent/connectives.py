# agent/connectives.py
"""连接词层：识别「然后 / 并且 / 顺便…」，供拆句与顺序排序用（20261117 需求）。

纯标准库、纯只读（与 `agent/lexicon.py` 同风格）：导入时不检索、不调模型、不碰编辑器；
不写盘、不抛异常。真值源是 `data/skills/connectives.json`（人写，owner 见文件），本模块只读。

为什么不塞进 `plan_windows._FILLER_RE` 字符类：字符类只能「抹字」，区分不了「顺序 / 并列 /
选择」，还容易把正文里的同名字吃掉（如「不超过」里的「不过」）。单独词表能带语义、可人工维护。

三个用途：
1. `split(text)`：按标点（与原 `_clause_rows` 同一套 `[，,；;。]`）**和**多字连接词切半句，
   返回 `[{clause, link}]`；`link` 是这一半句前面的连接词类别（首个为 ""）。
2. `strip(text)`：抹掉连接词，供「残句是否为空」的判定（连接词本身不是需求）。
3. `has_seq / has_parallel / has_disjunction`：按类别判，供 `windows_from_rules` 的依赖排序。

降级口径：词表缺失 / 坏 JSON → 空表 → `split()` 退化成「只按标点切」，与旧行为逐字一致；
绝不因为本模块挂了就改变拆句结果。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_PUNCT = re.compile(r"[，,；;。]")
_SPACES = re.compile(r"\s+")
_KINDS = frozenset({"seq", "parallel", "disjunction", "contrast"})


def connectives_path() -> Path:
    """`connectives.json` 的位置：`UE_CONNECTIVES` 显式覆盖优先，否则 `config.DATA_DIR/skills/`。"""
    override = os.environ.get("UE_CONNECTIVES", "").strip()
    if override:
        return Path(override)
    from config import DATA_DIR

    return Path(DATA_DIR) / "skills" / "connectives.json"


def load_rows() -> list[dict]:
    """读表。文件缺失 / 坏 JSON / rows 不是列表 → []（不抛）。"""
    try:
        path = connectives_path()
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "").strip().lower()
        words = row.get("words")
        if kind not in _KINDS or not isinstance(words, list):
            continue
        clean = [str(word) for word in words if str(word).strip()]
        if clean:
            out.append({"kind": kind, "words": clean})
    return out


def _pairs() -> list[tuple[str, str]]:
    """(词, kind) 列表，长词优先（「并且」先于「并」）。供 kinds() 识别全部类别。"""
    pairs: list[tuple[str, str]] = []
    for row in load_rows():
        for word in row["words"]:
            pairs.append((word, row["kind"]))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


def _split_pairs() -> list[tuple[str, str]]:
    """拆句/剥离用的连接词：**排除 disjunction**。

    「或者 / 还是 / 要么」是二选一：拆开当两半会变成「两个都做」，替人做了决定（违反不猜）。
    所以它们不参与 split/strip——留在句子里，让半句判成没盖住、回模型或问人；
    仅用于 has_disjunction 让上层知情。
    """
    return [pair for pair in _pairs() if pair[1] != "disjunction"]



def _earliest(text: str, pairs: list[tuple[str, str]]):
    """文本里最先出现的连接词（同位置取更长的）。没有返回 None。"""
    best = None
    for word, kind in pairs:
        pos = text.find(word)
        if pos == -1:
            continue
        if best is None or pos < best[0] or (pos == best[0] and len(word) > len(best[1])):
            best = (pos, word, kind)
    return best


def split(text: str) -> list[dict]:
    """按标点与多字连接词切半句。返回 `[{clause, link}]`，跳过空半句。"""
    pairs = _split_pairs()
    out: list[dict] = []
    for part in _PUNCT.split(str(text or "")):
        part = part.strip()
        if not part:
            continue
        if not pairs:
            out.append({"clause": part, "link": ""})
            continue
        link = ""
        buf = part
        while buf:
            hit = _earliest(buf, pairs)
            if hit is None:
                piece = buf.strip()
                if piece:
                    out.append({"clause": piece, "link": link})
                break
            pos, word, kind = hit
            head = buf[:pos].strip()
            if head:
                out.append({"clause": head, "link": link})
            link = kind
            buf = buf[pos + len(word):]
    return out


def strip(text: str) -> str:
    """抹掉所有（非选择类）连接词（换空格），供「残句是否为空」判定。"""
    result = str(text or "")
    for word, _kind in _split_pairs():
        result = result.replace(word, " ")
    return _SPACES.sub(" ", result).strip()


def kinds(text: str) -> set[str]:
    blob = str(text or "")
    found: set[str] = set()
    for word, kind in _pairs():
        if word in blob:
            found.add(kind)
    return found


def has_seq(text: str) -> bool:
    return "seq" in kinds(text)


def has_parallel(text: str) -> bool:
    return "parallel" in kinds(text)


def has_disjunction(text: str) -> bool:
    return "disjunction" in kinds(text)


def verify() -> list[str]:
    """结构问题清单，空 = 通过。只读、不写盘、不抛。"""
    issues: list[str] = []
    try:
        path = connectives_path()
        if not path.is_file():
            return [f"connectives.json 不存在：{path}"]
        rows = load_rows()
        if not rows:
            return [f"connectives.json 没有可用的 rows：{path}"]
        for index, row in enumerate(rows, 1):
            if not row["words"]:
                issues.append(f"第 {index} 行：words 为空")
            if len(set(row["words"])) != len(row["words"]):
                issues.append(f"第 {index} 行：words 里有重复项")
    except Exception as exc:  # noqa: BLE001 - 体检坏了也要交出可读字符串
        issues.append(f"verify 自身异常：{exc}")
    return issues
