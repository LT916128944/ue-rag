# core/source_edit.py
"""锁在 UE_SOURCE_ROOT 内的读文件和 unified diff。"""
import re
from pathlib import Path

from config import UE_SOURCE_ROOT

MAX_READ_LINES = 200
_BLOCKED_SUFFIX = {".uasset", ".umap", ".exe", ".dll", ".pdb", ".png", ".jpg"}
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def resolve_source(rel: str) -> Path:
    if not rel or not str(rel).strip():
        raise ValueError("path required")
    rel_path = Path(str(rel).strip())
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise ValueError("path refused")
    root = Path(UE_SOURCE_ROOT).resolve()
    target = (root / rel_path).resolve()
    if target != root and root not in target.parents:
        raise ValueError("path refused")
    if target.suffix.lower() in _BLOCKED_SUFFIX:
        raise ValueError("path refused")
    return target


def read_source(path: str, start_line: int = 1, end_line: int = 1) -> dict:
    try:
        target = resolve_source(path)
        start_line = int(start_line)
        end_line = int(end_line)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    if start_line < 1 or end_line < start_line:
        return {"ok": False, "error": "bad line range"}
    if end_line - start_line + 1 > MAX_READ_LINES:
        return {"ok": False, "error": f"at most {MAX_READ_LINES} lines"}
    if not target.is_file():
        return {"ok": False, "error": "file not found"}
    text = target.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    chunk = lines[start_line - 1:end_line]
    # text 是纯代码不带行号，可直接用作 diff 的旧行；行号只看 numbered
    numbered = [f"{start_line + i}|{line}" for i, line in enumerate(chunk)]
    return {
        "ok": True,
        "data": {
            "path": path,
            "start_line": start_line,
            "end_line": start_line + len(chunk) - 1 if chunk else start_line,
            "text": "\n".join(chunk),
            "numbered": "\n".join(numbered),
        },
    }


def _parse_hunks(diff: str) -> list[tuple[int, list[tuple[str, str]]]] | None:
    if "diff --git" in diff and diff.count("diff --git") > 1:
        return None
    hunks = []
    current = None
    for line in diff.splitlines():
        match = _HUNK_RE.match(line)
        if match:
            current = (int(match.group(1)), [])
            hunks.append(current)
            continue
        if current is None:
            continue
        if line.startswith(("---", "+++", "diff ")):
            return None
        if line.startswith("\\"):
            continue
        if not line:
            return None
        tag = line[0]
        if tag not in " +-":
            return None
        current[1].append((tag, line[1:]))
    if not hunks:
        return None
    return hunks


def _apply_hunks(original: list[str], hunks) -> tuple[list[str] | None, str]:
    """返回 (更新后的行, 冲突说明)。冲突说明指出第一处对不上的地方，让模型能自我纠正。"""
    result: list[str] = []
    cursor = 0
    for old_start, body in hunks:
        start = old_start - 1
        if start < cursor or start > len(original):
            return None, (
                f"hunk 旧行号 {old_start} 超出文件（共 {len(original)} 行）或与上一段重叠。"
                "行号要以最近一次 read_source 的 numbered 为准。"
            )
        result.extend(original[cursor:start])
        cursor = start
        for tag, text in body:
            if tag in (" ", "-"):
                if cursor >= len(original):
                    return None, (
                        f"旧行到第 {cursor + 1} 行时文件已经结束（共 {len(original)} 行）。"
                        "文件可能已被上一次成功的 diff 改过，用最近一次 read_source 重做。"
                    )
                if original[cursor] != text:
                    got = text[:60]
                    have = original[cursor][:60]
                    return None, (
                        f"旧行对不上（第 {cursor + 1} 行）：diff 给的是 {got!r}，文件里是 {have!r}。"
                        "用最近一次 read_source 的 text 重做 diff；文件可能已被上一次成功的 diff 改过。"
                    )
                if tag == " ":
                    result.append(original[cursor])
                cursor += 1
            elif tag == "+":
                result.append(text)
    result.extend(original[cursor:])
    return result, ""


def apply_unified_diff(path: str, diff: str) -> dict:
    try:
        target = resolve_source(path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not diff or "@@" not in diff:
        return {"ok": False, "error": "not a unified diff"}
    if len(diff) > 100_000:
        return {"ok": False, "error": "diff too large"}
    if not target.is_file():
        return {"ok": False, "error": "file not found"}
    hunks = _parse_hunks(diff)
    if not hunks:
        return {"ok": False, "error": "not a unified diff：要 @@ -n,m +n,m @@ 头，行首用 ' '（上下文）、'+'（新增）、'-'（删除）"}
    raw = target.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8")
    original = text.splitlines()
    updated, note = _apply_hunks(original, hunks)
    if updated is None:
        return {"ok": False, "error": "diff conflict：" + note}
    out = newline.join(updated)
    if text.endswith(("\n", "\r\n")):
        out += newline
    target.write_bytes(out.encode("utf-8"))
    return {"ok": True, "data": {"path": path, "lines": len(updated)}}
