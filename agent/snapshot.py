# agent/snapshot.py
"""源码字节快照与还原。从 run_windows 拆出，供续接与 DSH 网关共用。"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from config import PROJECT_ROOT, UE_SOURCE_ROOT

SNAPSHOTS_DIR = PROJECT_ROOT / "data" / "snapshots"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_source(run_id: str, wid: str) -> dict:
    """会写源码的窗口开始前拍字节快照到 data/snapshots/<run_id>/<wid>/。"""
    if not UE_SOURCE_ROOT.is_dir():
        return {"dir": "", "files": {}}
    out = SNAPSHOTS_DIR / run_id / wid
    if out.exists():
        shutil.rmtree(out)
    files: dict[str, str] = {}
    for path in sorted(UE_SOURCE_ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".h", ".cpp", ".cs", ".ini"):
            continue
        rel = path.relative_to(UE_SOURCE_ROOT).as_posix()
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        files[rel] = sha256_file(path)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps({"files": files}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return {"dir": str(out), "files": files}


def restore_source(run_id: str, wid: str, snapshot: dict) -> list[str]:
    """窗口失败且无 keep_source 时还原：只回滚这次窗口改过的文件字节。"""
    base = Path(str(snapshot.get("dir") or ""))
    restored: list[str] = []
    for rel, sha in (snapshot.get("files") or {}).items():
        path = UE_SOURCE_ROOT / rel
        if path.is_file() and sha256_file(path) == sha:
            continue
        src = base / rel
        if src.is_file():
            shutil.copyfile(src, path)
            restored.append(rel)
    return restored
