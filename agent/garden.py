# agent/garden.py
"""园丁：只做确定性扫描。补缺失抬头、记 stale。不叫模型，不读 UE_DISTILL_TEACHER，不改正文。"""
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

UPGRADE_FILES = {"01-信息层.md", "02-约束层.md", "03-自动化层.md"}
STALE_DAYS = 45


def _scan_dirs(override=None) -> list[Path]:
    if override:
        return [Path(item) for item in override]
    from config import UE_PROJECT_DIR

    return [
        UE_PROJECT_DIR / "documents" / "harness",
        UE_PROJECT_DIR / "Plan" / "终极蒸馏方案" / "升级执行",
    ]


def _report_target(override=None) -> Path:
    if override:
        return Path(override)
    from config import DATA_DIR

    return DATA_DIR / "harness" / "garden_report.json"


def _split_front(text: str) -> tuple[dict | None, int]:
    """(抬头字段, 正文起始行号)。没有合法抬头就 (None, 0)。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, 0
    front = {}
    for index in range(1, len(lines)):
        stripped = lines[index].strip()
        if stripped == "---":
            if "status" in front or "last_updated" in front:
                return front, index + 1
            return None, 0
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            front[key.strip()] = value.strip()
    return None, 0


def _parse_day(text: str):
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _patch_header(path: Path, front: dict | None, text: str) -> str:
    """只补抬头：status / last_updated / owner。三份升级文件保持 ready 或 done，不下改成 draft。不改正文。"""
    today = date.today().isoformat()
    status = str((front or {}).get("status") or "").strip()
    add = []
    if not status:
        status = "ready" if path.name in UPGRADE_FILES else "draft"
        add.append(f"status: {status}")
    if not str((front or {}).get("last_updated") or "").strip():
        add.append(f"last_updated: {today}")
    if not str((front or {}).get("owner") or "").strip():
        add.append("owner: 侑")
    if front is None:
        return "---\n" + "\n".join(add) + "\n---\n\n" + text
    lines = text.splitlines()
    for offset, line in enumerate(add):
        lines.insert(1 + offset, line)
    fresh = "\n".join(lines)
    if text.endswith("\n") and not fresh.endswith("\n"):
        fresh += "\n"
    return fresh


def garden(scan_dirs=None, report_path=None) -> dict:
    """扫 documents/harness 与 升级执行/*.md。缺抬头只补抬头；active 超过 45 天记 stale 不改文件。"""
    report = {"scanned": 0, "patched": 0, "stale": []}
    for directory in _scan_dirs(scan_dirs):
        if not Path(directory).is_dir():
            continue
        for path in sorted(Path(directory).glob("*.md")):
            report["scanned"] += 1
            text = path.read_text(encoding="utf-8")
            front, _body_at = _split_front(text)
            status = str((front or {}).get("status") or "").strip()
            last_updated = str((front or {}).get("last_updated") or "").strip()
            if status == "active":
                day = _parse_day(last_updated)
                if day is not None and (date.today() - day).days > STALE_DAYS:
                    report["stale"].append({"path": str(path), "last_updated": last_updated})
                    continue
            if status and last_updated:
                continue
            path.write_text(_patch_header(path, front, text), encoding="utf-8")
            report["patched"] += 1
    target = _report_target(report_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    scan_dirs = None
    raw = os.environ.get("UE_GARDEN_DIRS", "").strip()
    if raw:
        scan_dirs = [item for item in raw.split(os.pathsep) if item]
    report_path = os.environ.get("UE_GARDEN_REPORT", "").strip() or None
    report = garden(scan_dirs=scan_dirs, report_path=report_path)
    print(f"garden scanned={report['scanned']} patched={report['patched']} stale={len(report['stale'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
