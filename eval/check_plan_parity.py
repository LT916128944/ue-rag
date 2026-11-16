# eval/check_plan_parity.py
"""离线/在线双路径一致性（20261111-1945 计划 P4）。

对 `eval/bench_manifest.json` 的十句各做两件事：

1. 在封闭环境（`eval._pin_offline.closed_env`）下跑 `plan_windows`，记 `source`
   （rules / hud-rules / learned / solver-fallback / planner）与窗口动作序列；
2. 读 `data/archive/<该句>/*/` 里**最近一次真机** run 的 `data/events/<run_id>.jsonl`：
   `run_start.source` 是权威 source（`intent.source` 与它可能不一致），
   `run_start.windows` 是那次真的窗口动作序列。

两者不一致就红，并打印两个 source 与两句动作序列。

口径（照计划，不放宽）：

- 真机记录按「句子」找：`run_start.query` 与清单里的原话一字不差，且 `intent.actions`
  与 `run_start.windows` 的动作集合一致（后者是权威），这样「打开编辑器，打开关卡」
  与「打开关卡，按设计图…」不会被互相认成对方。
- 取「最近一次真机」：`run_start` 的 `ts` 最晚的那一次。
- 十句里除 `patrol-presentation`（预期 partial）外，离线与最近一次真机的 source 一致；
  至少 9 句一致才绿。没有真机记录的句子记 `no-record`，不进分母。
- `patrol-presentation` 缺记录也红：计划在 P7 要它三轮都是 partial，它不许没记录。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MANIFEST = Path(__file__).resolve().parent / "bench_manifest.json"
# 预期 partial 的那一句：source 不作要求，但记录必须存在。
PARTIAL_SENTENCE = "patrol-presentation"
MIN_CONSISTENT = 9


def _manifest_rows() -> list[dict]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows = data.get("sentences") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != 10:
        raise SystemExit("bench_manifest.json 要有 10 句")
    return [row for row in rows if isinstance(row, dict)]


def _actions(windows) -> list[str]:
    out = []
    for window in windows or []:
        if isinstance(window, dict):
            out.append(str(window.get("action") or ""))
    return out


def _events_root() -> Path:
    from config import DATA_DIR

    return DATA_DIR / "events"


def _archive_root() -> Path:
    from config import DATA_DIR

    return DATA_DIR / "archive"


def _read_events(path: Path) -> tuple[dict | None, dict | None]:
    """一次 run 的 run_start 与 intent（intent 只用来核对是不是同一套窗口）。"""
    start = None
    intent = None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("type") == "run_start" and start is None:
            start = row
        elif row.get("type") == "intent" and intent is None:
            intent = row
    return start, intent


def _real_runs(events_root: Path, archive_root: Path) -> list[dict]:
    """data/archive 里每次真机 run 的 run_start。bench-offline-* 是离线核对，不算真机。"""
    runs: list[dict] = []
    if not events_root.is_dir():
        return runs
    archived = set()
    if archive_root.is_dir():
        archived = {
            path.name for path in archive_root.iterdir()
            if path.is_dir() and not path.name.startswith("bench-offline-")
        }
    for path in sorted(events_root.glob("*.jsonl")):
        run_id = path.stem
        if run_id.startswith("bench-offline-"):
            continue
        start, intent = _read_events(path)
        if not start:
            continue
        if archived and run_id not in archived:
            # 归档里没有这次 run 的目录：它不是被记下来的真机基准记录，不当证据。
            continue
        runs.append({
            "run_id": run_id,
            "ts": str(start.get("ts") or ""),
            "query": str(start.get("query") or "").strip(),
            "source": str(start.get("source") or ""),
            "windows": list(start.get("windows") or []),
            "actions": _actions(start.get("windows")),
            "intent_actions": [str(item) for item in (intent or {}).get("actions") or []],
            "intent_source": str((intent or {}).get("source") or ""),
            "archived": run_id in archived,
        })
    return runs


def latest_real_run(sentence: dict, runs: list[dict]) -> dict | None:
    """这句最近一次真机 run。原话一字不差；窗口动作集合要与 intent.actions 对得上。"""
    text = str(sentence.get("text") or "").strip()
    hits = [run for run in runs if run["query"] == text]
    if not hits:
        return None
    # intent.actions 与 run_start.windows 不一致的那些，不算这句的证据（来源说不清）。
    consistent = [run for run in hits if list(run["intent_actions"]) == list(run["actions"])]
    pool = consistent or hits
    return sorted(pool, key=lambda run: (run["ts"], run["run_id"]))[-1]


def offline_plan(sentence: dict) -> dict:
    """读这台机器的真实规划链（封闭环境：`.env` 换空、进程环境里的开关清掉）。

    `complete` 是抛异常的替身：离线检查一次模型调用都不许发生。
    `UE_SOLVER_ENABLED=true` 是显式盖上的：这台机器与三次真机 open-level 都在求解扇开着的
    条件下跑（`RAG/ue-rag/.env` 就是这个值）；不盖上「求解开着」而只测缺省，验的就不是
    在线那条路。除了这一个开关，其余一律走缺省。
    """
    from eval._pin_offline import closed_env

    calls: list = []

    def _offline_stub(_messages):
        calls.append(_messages)
        raise RuntimeError("离线检查不许叫模型（plan_windows 的 complete 替身）")

    with closed_env(UE_SOLVER_ENABLED="true"):
        # complete=None 时 plan_windows 会真的调模型/规划者；这条进程级会审把那个洞堵上：
        # 直接调 _complete_live 就是离线检查自己犯规，立刻断言。
        from agent import plan_windows as module

        saved = module._complete_live

        def _forbid(_messages):
            raise AssertionError("离线检查里不许走 _complete_live（它会真的调模型）")

        module._complete_live = _forbid
        try:
            planned = module.plan_windows(str(sentence.get("text") or ""), complete=_offline_stub)
        finally:
            module._complete_live = saved
    source = str((planned or {}).get("source") or "")
    if not source:
        raise AssertionError(f"{sentence.get('id')}: plan_windows 没给 source")
    return {
        "source": source,
        "actions": _actions((planned or {}).get("windows")),
        "model_calls": len(calls),
    }


def compare(sentence: dict, offline: dict, live: dict | None, partial: bool) -> dict:
    verdict = {
        "id": str(sentence.get("id") or ""),
        "expected_partial": partial,
        "offline_source": offline["source"],
        "offline_actions": offline["actions"],
        "model_calls": offline["model_calls"],
        "live_source": (live or {}).get("source", ""),
        "live_actions": (live or {}).get("actions", []),
        "run_id": (live or {}).get("run_id", ""),
        "intent_source": (live or {}).get("intent_source", ""),
        "status": "",
        "why": "",
    }
    if partial:
        if live is None:
            verdict["status"] = "missing"
            verdict["why"] = "预期 partial，但没有真机记录（P7 要求它三轮都是 partial）"
        else:
            verdict["status"] = "partial-expected"
        return verdict
    if live is None:
        verdict["status"] = "no-record"
        verdict["why"] = "没有真机记录，不进分母"
        return verdict
    if offline["source"] == live["source"]:
        verdict["status"] = "consistent"
        return verdict
    verdict["status"] = "mismatch"
    verdict["why"] = (
        f"离线 {offline['source']}({'→'.join(offline['actions']) or '无窗口'}) 对 "
        f"真机 {(live.get('run_id') or '')} {live['source']}({'→'.join(live['actions']) or '无窗口'})"
    )
    if live.get("intent_source") and live["intent_source"] != live["source"]:
        verdict["why"] += f"；那次 intent.source={live['intent_source']}（run_start 才是权威）"
    return verdict


def evaluate() -> list[dict]:
    rows = _manifest_rows()
    runs = _real_runs(_events_root(), _archive_root())
    results = []
    for sentence in rows:
        sid = str(sentence.get("id") or "")
        partial = sid == PARTIAL_SENTENCE
        offline = offline_plan(sentence)
        results.append(compare(sentence, offline, latest_real_run(sentence, runs), partial))
    return results


def _note_missing_records(rows: list[dict]) -> None:
    """P7 之前这段必然红。红要红得能办事：把「这台机器上哪些句子已经能离线跑出路径、
    只是还没有真机记录」一条条列出来，省得去真跑一遍才知道哪些句子会白跑。"""
    print("\n缺记录的原因就是还没跑 P7（十句 × 3 轮真机）。这些句子现在离线就能跑到：")
    for row in rows:
        if row["status"] not in ("no-record", "missing"):
            continue
        actions = "→".join(row["offline_actions"]) or "无窗口"
        calls = "（离线还要点 {0} 次模型）".format(row["model_calls"]) if row["model_calls"] else ""
        print(f"  {row['id']:<20} 离线 {row['offline_source']}：{actions}{calls}")


def main() -> int:
    try:
        results = evaluate()
    except Exception as exc:  # 离线跑不出来也要说清楚是哪一步
        print(f"FAIL 离线 plan_windows 跑不出来：{type(exc).__name__}: {exc}")
        return 1

    print(f"{'句子':<20} {'离线 source':<15} {'真机 source':<15} 结论")
    for row in results:
        print(
            f"{row['id']:<20} {row['offline_source']:<15} {(row['live_source'] or '-'):<15} "
            f"{row['status']}"
        )
        if row["why"]:
            print(f"    {row['why']}")

    mismatched = [row for row in results if row["status"] == "mismatch"]
    missing = [row for row in results if row["status"] == "missing"]
    counted = [row for row in results if row["status"] in ("consistent", "mismatch")]
    consistent = [row for row in counted if row["status"] == "consistent"]
    no_record = [row for row in results if row["status"] == "no-record"]

    print(
        f"\n计入 {len(counted)} 句（no-record {len(no_record)} 句不进分母）："
        f"一致 {len(consistent)} 句，不一致 {len(mismatched)} 句"
        + (f"，缺记录 {len(missing)} 句" if missing else "")
    )
    if mismatched:
        print("不一致：" + "；".join(f"{row['id']}：{row['why']}" for row in mismatched))
    if missing:
        print("预期 partial 的句子缺真机记录：" + "、".join(row["id"] for row in missing))
    if no_record or missing:
        print("no-record（还没有真机记录）：" + "、".join(row["id"] for row in no_record + missing))
        _note_missing_records(results)

    if mismatched or missing or len(consistent) < MIN_CONSISTENT:
        print(
            f"FAIL 离线与在线不是同一条路：除 {PARTIAL_SENTENCE} 外要至少 "
            f"{MIN_CONSISTENT} 句 source 一致，现在 {len(consistent)} 句"
        )
        return 1
    print(f"ok 十句离线与最近一次真机来源一致（{len(consistent)}/{len(counted)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
