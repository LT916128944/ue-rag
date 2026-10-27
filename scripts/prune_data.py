# scripts/prune_data.py
"""按天清理 data/ 下堆积的运行残留，默认保留最近 3 天。

清理对象（每个根目录下的直接子项）：
    archive     <run_id>/
    briefs      <stamp>-<slug>.md|.json
    events      <run_id>.jsonl
    handoffs    <run_id>/<wid>.json
    runs        <YYYYMMDD>/（老 run 平铺的文件按 mtime 判断）
    snapshots   <run_id>/
    traces      <name>-<run_id>.jsonl

年龄判断：先看条目名里的日期（YYYYMMDD 或 YYYYMMDD-HHMMSS），
名字里没有日期才回退最近修改时间。名字旧但最近还在被写的条目跳过，
避免删掉正在跑的 run。目录只删子项，根目录本身保留。

不动的东西：data/chroma（向量库）、data/harness、data/skills、
data/distill，以及 data 根下的 .jsonl/.json 单文件。

用法：
    python scripts/prune_data.py --dry-run
    python scripts/prune_data.py --days 3 --verbose
    python scripts/prune_data.py --roots traces --days 7
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

DEFAULT_ROOTS = ("archive", "briefs", "events", "handoffs", "runs", "snapshots", "traces")
DEFAULT_DAYS = 3

_NAME_DATE = re.compile(r"^(20\d{2})(\d{2})(\d{2})(?:[-_]\d{6})?")


def _name_date(name: str) -> date | None:
    match = _NAME_DATE.match(name)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _newest_mtime(path: Path) -> float:
    try:
        newest = path.stat().st_mtime
    except OSError:
        return 0.0
    if not path.is_dir():
        return newest
    for child in path.rglob("*"):
        if child.is_symlink():
            continue
        try:
            newest = max(newest, child.stat().st_mtime)
        except OSError:
            continue
    return newest


def _size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_symlink():
            continue
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _human(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _doomed(root: Path, cutoff: date) -> list[Path]:
    """名字日期和最近修改日期都不晚于 cutoff 的子项才删。"""
    out: list[Path] = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        try:
            newest = _newest_mtime(child)
        except OSError:
            continue
        touched = datetime.fromtimestamp(newest).date() if newest else date.min
        named = _name_date(child.name)
        aged = named if named is not None else touched
        if aged <= cutoff and touched <= cutoff:
            out.append(child)
    return out


def prune(roots: list[Path], days: int, *, dry_run: bool, verbose: bool) -> dict:
    cutoff = date.today() - timedelta(days=max(0, days))
    report: dict = {"cutoff": cutoff.isoformat(), "days": days, "roots": {}, "removed": 0, "bytes": 0}
    print(f"保留 {days} 天：删除 {cutoff.isoformat()}（含）及更早的条目")
    for root in roots:
        name = root.name
        if not root.is_dir():
            report["roots"][name] = {"state": "missing", "removed": 0, "bytes": 0}
            print(f"  {name:<10} 目录不存在，跳过")
            continue
        targets = _doomed(root, cutoff)
        freed = 0
        for path in targets:
            freed += _size(path)
            if verbose or dry_run:
                print(f"    - {name}/{path.name}")
            if not dry_run:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    try:
                        path.unlink()
                    except OSError:
                        pass
        report["roots"][name] = {"state": "ok", "removed": len(targets), "bytes": freed}
        report["removed"] += len(targets)
        report["bytes"] += freed
        text = f"删 {len(targets)} 项 / {_human(freed)}" if targets else "无"
        print(f"  {name:<10} {text}")
    tail = "（dry-run，未实际删除）" if dry_run else ""
    print(f"合计 {report['removed']} 项 / {_human(report['bytes'])}{tail}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理 data/ 下三天前的运行残留")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="保留天数，默认 3")
    parser.add_argument("--dry-run", action="store_true", help="只打印要删什么")
    parser.add_argument("--verbose", action="store_true", help="逐个列出删除项")
    parser.add_argument("--roots", default="", help="逗号分隔的子目录名，覆盖默认清单")
    parser.add_argument("--data", default=str(DATA_DIR), help="data 目录，默认 ue-rag/data")
    args = parser.parse_args(argv)

    data = Path(args.data)
    names = [item.strip() for item in args.roots.split(",") if item.strip()] or list(DEFAULT_ROOTS)
    roots = [data / name for name in names]
    prune(roots, args.days, dry_run=args.dry_run, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
