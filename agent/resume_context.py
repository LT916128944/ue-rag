# agent/resume_context.py
"""开工读档：打印目标、阶段、下一步、第一条 pending、最新交接的未完成。无参数，退出码 0。

progress.json 与 feature_list.json 是两份并列文件：前者记 phase / goal / next_action，
后者按 id 记 status / passed / evidence，谁也不包含谁。打印合计不超过 200 字。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DATA_DIR


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def latest_handoff_unmet(root: Path) -> str:
    """交接目录按 run id 字符串排序取最新，再取最后一份交接的 unmet。没有就空串，不编。"""
    root = Path(root)
    if not root.is_dir():
        return ""
    runs = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name)
    if not runs:
        return ""
    files = sorted(runs[-1].glob("*.json"))
    if not files:
        return ""
    doc = _load(files[-1])
    unmet = [str(item) for item in (doc.get("unmet") or []) if str(item)]
    return "；".join(unmet)[:80]


def build_line(progress: dict, features: dict, unmet: str) -> str:
    items = features.get("items") if isinstance(features, dict) else []
    pending = next(
        (item for item in items or [] if isinstance(item, dict) and item.get("status") == "pending"),
        None,
    )
    parts = [
        f"goal={str(progress.get('goal') or '')[:60]}",
        f"phase={str(progress.get('phase') or '')[:12]}",
        f"next_action={str(progress.get('next_action') or '')[:40]}",
    ]
    if pending:
        title = str(pending.get("title") or pending.get("notes") or "")
        parts.append(f"pending={str(pending.get('id') or '')}:{title[:40]}")
    if unmet:
        parts.append(f"unmet={unmet}")
    return " ".join(parts)[:200]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    progress = _load(DATA_DIR / "harness" / "progress.json")
    features = _load(DATA_DIR / "harness" / "feature_list.json")
    unmet = latest_handoff_unmet(DATA_DIR / "handoffs")
    print(build_line(progress, features, unmet))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
