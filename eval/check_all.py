# eval/check_all.py
"""把 .github/workflows/offline.yml 里已有的 python eval/check_*.py 逐条跑完。

不复制检查正文，不改 workflow 的触发方式。摘要写 data/harness/check_all.json。
check_scorecard.py 放在最后，让它读到刚跑完的退出码。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "offline.yml"
SUMMARY = ROOT / "data" / "harness" / "check_all.json"
_RUN = re.compile(r"^\s*run:\s*python\s+(eval/check_[A-Za-z0-9_]+\.py)(?:\s+(\S.*?))?\s*$")


def _listed() -> list[tuple[str, list[str]]]:
    rows = []
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        match = _RUN.match(line)
        if not match:
            continue
        script = match.group(1).replace("\\", "/")
        if script.endswith("check_all.py") or script.endswith("check_scorecard.py"):
            continue
        args = match.group(2).split() if match.group(2) else []
        rows.append((script, args))
    if not rows:
        raise SystemExit(f"没有从 {WORKFLOW} 读到检查")
    return rows


def _run(script: str, args: list[str]) -> tuple[int, float]:
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, script, *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    seconds = round(time.perf_counter() - started, 3)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
        if len(tail) > 200:
            tail = tail[:200]
        print(f"{script} exit={proc.returncode} {seconds}s {tail}")
    else:
        print(f"{script} exit=0 {seconds}s")
    return proc.returncode, seconds


def _write(checks: list[dict]) -> None:
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(
        json.dumps({"checks": checks}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    checks = []
    failed = False
    for script, args in _listed():
        code, seconds = _run(script, args)
        checks.append({
            "name": Path(script).stem,
            "script": script,
            "exit": code,
            "seconds": seconds,
        })
        if code != 0:
            failed = True
    _write(checks)
    env = os.environ.copy()
    env["UE_CHECK_ALL_SUMMARY"] = str(SUMMARY)
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "eval/check_scorecard.py"],
        cwd=str(ROOT),
        env=env,
    )
    checks.append({
        "name": "check_scorecard",
        "script": "eval/check_scorecard.py",
        "exit": proc.returncode,
        "seconds": round(time.perf_counter() - started, 3),
    })
    _write(checks)
    if proc.returncode != 0:
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
