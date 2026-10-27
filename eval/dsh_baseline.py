# eval/dsh_baseline.py
"""只读基线：汇总模型调用 token 与 scoreboard，供 DSH 绑定前后对比。

不调用模型、不改任何状态，只读 data/costs.jsonl 与 data/harness/scoreboard.json。
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COSTS = ROOT / "data" / "costs.jsonl"
SCOREBOARD = ROOT / "data" / "harness" / "scoreboard.json"
OUT = ROOT / "data" / "harness" / "dsh_baseline.json"
CALLERS = ("understand", "planner", "plan", "supervise")


def _p50(values: list[float]) -> int:
    return int(round(statistics.median(values))) if values else 0


def summarize(rows: list[dict]) -> dict:
    out = {}
    for caller in CALLERS:
        items = [
            row
            for row in rows
            if row.get("caller") == caller
            and row.get("prompt_tokens") is not None
            and not row.get("error")
        ]
        values = [float(item["prompt_tokens"]) for item in items]
        out[caller] = {
            "n": len(values),
            "p50": _p50(values),
            "max": int(max(values)) if values else 0,
        }
    return out


def main() -> int:
    rows = []
    if COSTS.is_file():
        for line in COSTS.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    scoreboard = {}
    if SCOREBOARD.is_file():
        try:
            scoreboard = json.loads(SCOREBOARD.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            scoreboard = {}
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "callers": summarize(rows),
        "scoreboard": scoreboard,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    parts = [f"{key}={value['n']}/{value['p50']}" for key, value in payload["callers"].items()]
    print("dsh-baseline " + " ".join(parts) + f" -> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
