# agent/ledger.py
"""范围级台账：done / partial / pending。任务书的「尚未完成」从这里取。
只给机器读；给人的记录仍走 .cursor/memory/任务记录/，闭环再镜像一份到 .codex/memory/任务记录/，两边不互相替代。"""
import json
import re
import threading
from datetime import datetime
from pathlib import Path

from config import PROJECT_ROOT

LEDGER_PATH = PROJECT_ROOT / "data" / "ledger.json"
_LOCK = threading.Lock()


def read_ledger(path: Path | None = None) -> dict:
    target = Path(path) if path is not None else LEDGER_PATH
    empty = {"scopes": [], "external": [], "updated": ""}
    if not target.is_file():
        return empty
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("scopes", [])
    data.setdefault("external", [])
    data.setdefault("updated", "")
    return data


def update_ledger(run_id: str, scopes: list[dict] | None, path: Path | None = None) -> dict:
    """集成结果回写台账。scopes: [{id,title,status,run_id,pending,note}]。按 id 合并，原子写。"""
    with _LOCK:
        data = read_ledger(path)
        by_id: dict[str, dict] = {}
        for item in data.get("scopes") or []:
            if isinstance(item, dict) and item.get("id"):
                by_id[str(item["id"])] = item
        now = datetime.now().isoformat(timespec="seconds")
        for scope in scopes or []:
            if not isinstance(scope, dict) or not str(scope.get("id") or "").strip():
                continue
            entry = {
                "id": str(scope["id"]),
                "title": str(scope.get("title") or ""),
                "status": str(scope.get("status") or "pending"),
                "run_id": str(run_id),
                "updated": now,
                "pending": [str(x) for x in scope.get("pending") or [] if str(x).strip()],
            }
            if str(scope.get("note") or "").strip():
                entry["note"] = str(scope["note"])
            by_id[entry["id"]] = entry
        data["scopes"] = list(by_id.values())
        data["updated"] = now
        target = Path(path) if path is not None else LEDGER_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(target)
        return data


# 这些词在很多任务里都会出现。用它们判断「是不是同一件事」会把别的任务带进来
# （20261021-143603：平台跳跃原句里的「玩家」「关卡」套住了台账里的敌人巡逻）。
_GENERIC = frozenset({
    "玩家", "关卡", "逻辑", "验证", "编译", "运行", "接线", "代码", "测试", "行为",
    "当前", "蓝图", "编辑", "工程", "放置", "完成", "集成", "交付", "范围", "发现",
    "尚未", "确认", "启动", "修改", "资产", "功能", "系统", "项目", "任务", "一个",
    "可以", "以及", "用于", "已经", "没有", "通过", "需要", "执行", "窗口", "本次",
    "上一", "剩余", "沿用", "进行", "之后", "然后", "其中", "这个", "那个", "自己",
    "我们", "包括", "最终", "状态", "汇总", "遗留", "核对", "处理", "表现", "条件",
    "触发", "统计", "判定", "绑定", "创建", "显示", "实现", "内容",
})
_ASCII_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]{2,}")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]+")
# /Game/ 里的 Game、状态词 done 不能把两条路径粘成同一件事。
_ASCII_STOP = frozenset({"game", "true", "false", "done", "json", "none", "null", "the", "and"})


def scope_text(scope: dict) -> str:
    """标题、现状、待办拼成一段，用来判断两段文字是不是在说同一件事。"""
    parts = [
        str(scope.get("title") or ""),
        str(scope.get("note") or ""),
        str(scope.get("pending_note") or ""),
    ]
    for key in ("pending", "deliverables"):
        for item in scope.get(key) or []:
            parts.append(str(item))
    return "\n".join(parts)


def content_tokens(text: str) -> set[str]:
    """专名和实词。通用词去掉，避免「玩家」这种词把两件不同的事粘在一起。"""
    tokens: set[str] = set()
    for word in _ASCII_RE.findall(text or ""):
        lowered = word.lower()
        if lowered not in _ASCII_STOP:
            tokens.add(lowered)
    for seg in _HAN_RE.findall(text or ""):
        if len(seg) < 2:
            continue
        mark = [False] * len(seg)
        for i in range(len(seg) - 1):
            if seg[i:i + 2] in _GENERIC:
                mark[i] = True
                mark[i + 1] = True
        if len(seg) == 2:
            if not mark[0]:
                tokens.add(seg)
            continue
        for i in range(len(seg) - 1):
            gram = seg[i:i + 2]
            if gram in _GENERIC or (mark[i] and mark[i + 1]):
                continue
            tokens.add(gram)
    return tokens


def same_subject(a: str, b: str) -> bool:
    """两边有共同专名或实词，才算同一件事。"""
    return bool(content_tokens(a) & content_tokens(b))


def overlap_size(a: str, b: str) -> int:
    return len(content_tokens(a) & content_tokens(b))


def ledger_summary_text(path: Path | None = None, limit: int = 600, query: str | None = None) -> str:
    """给任务书生成器的台账摘要：范围级 done/pending，几百字以内。

    带了 query 时只留和这句话同一件事的范围。对不上就明说不要写进本次范围，
    避免生成器把别的任务的 pending 当成这次要做的事。
    """
    data = read_ledger(path)
    raw_scopes = [s for s in data.get("scopes") or [] if isinstance(s, dict) and s.get("id")]
    scopes = raw_scopes
    if query is not None:
        scopes = [s for s in raw_scopes if same_subject(scope_text(s), query)]
        if raw_scopes and not scopes:
            return "台账里有别的任务的进度，和这句话对不上，不要写进本次范围。"[:limit]
    lines: list[str] = []
    for scope in scopes:
        status = str(scope.get("status") or "pending")
        title = str(scope.get("title") or scope.get("id"))
        if status == "done":
            lines.append(f"{scope.get('id')} {title}：done")
            continue
        pending = [str(x) for x in scope.get("pending") or [] if str(x).strip()]
        left = "；".join(pending[:4])
        lines.append(f"{scope.get('id')} {title}：{status}" + (f"（剩：{left}）" if left else ""))
    if not lines:
        return "台账为空：还没有按范围累计的进度。"
    return "；".join(lines)[:limit]
