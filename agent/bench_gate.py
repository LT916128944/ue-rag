# agent/bench_gate.py
"""四道人工闸门的工作流外壳（20261112-1220 计划 §3 的四行人工介入）。

这份计划把「人执行任务」压到零，但留下四处必须由人做的判断。它们不再是「人去手撸一份
JSON」，而是工作流里的四个**闸门步骤**：闸门自己不替人做判断，只做三件事——

1. 把这一步需要人看的**落盘事实**摊开（证据路径、条款、窗口、archive 原文）；
2. 等一句人的输入（授权 / 看过了 / 名字 / 逐条 ok）；
3. 把这句输入**记进产物**，再用机器复核一遍产物能不能自己读回来。

四个闸门与它们的产物：

| 闸门 | 谁做 | 产物 | 本模块做什么 |
| --- | --- | --- | --- |
| `authorize` | 人（安全边界） | `data/dsh/session_task.json` | **只读**：打印现状与还缺哪几项，不写这个文件 |
| `pie` | 人（能力边界） | 无（判断在眼里） | 打印要看的 run 与三处看点，等人回话 |
| `attest` | 人（审计） | `data/attestations/<criterion>.json` | 调 `agent/attest_sign.py`，人给了名字与 `--yes` 才签 |
| `audit` | 人（审计） | `data/bench/audit.json` | 摊开 `batch.sampled` 那三条的原文，收人的逐条 ok，写出回执 |

**这个模块不是模型的自证工具**：`authorize` 永不写 `session_task.json`（那是 fail-closed 的
唯一读源，只由人改）；`attest` 与 `audit` 都要求命令行上出现人的姓名与 `--yes`，并且写完之后
必须能通过 `agent.manual_attest._read_valid` 或 `eval/check_bench_audit.py` 自己的复核。

不带 `--yes` 时一律退 2（2 = 「在等人」，和「红了」区分开）。

用法：
    python agent\\bench_gate.py --gate list
    python agent\\bench_gate.py --gate authorize
    python agent\\bench_gate.py --gate pie --run-id <run_id>
    python agent\\bench_gate.py --gate attest --criterion patrol-presentation
    python agent\\bench_gate.py --gate attest --criterion patrol-presentation --person "张三" --yes
    python agent\\bench_gate.py --gate audit
    python agent\\bench_gate.py --gate audit --person "张三" --verdict ok --mark <run_id>=ok --yes
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRODUCED_BY = "agent/bench_gate.py"
WAITING = 2
GATES = ("authorize", "pie", "attest", "audit")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _data_root(data_root: Path | None = None) -> Path:
    if data_root is not None:
        return Path(data_root)
    from config import DATA_DIR

    return Path(DATA_DIR)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _short(text, limit: int = 200) -> str:
    row = str(text or "").replace("\n", " ").strip()
    return row if len(row) <= limit else row[:limit] + "…"


# --------------------------------------------------------------------------
# 闸门 1：授权（只读）
# --------------------------------------------------------------------------


def _session_task() -> dict:
    return _read_json(_data_root() / "dsh" / "session_task.json") or {}


DEFAULT_ENV_GATES = ("UE_ALLOW_BLUEPRINT", "UE_DEFAULT_SAVE", "UE_DEFAULT_PIE")


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def gate_authorize(need_env: str = "") -> int:
    """打印两条入口各自的授权现状。**不写任何配置文件。**

    20261112 复核发现（计划 §3 第 1 条的边界）：`data/dsh/session_task.json` 只喂
    `config.run_policy("dsh")`，而唯一的调用方是 `server/dsh_bridge.py`（DSH 网关）。
    `bench_runner` 走的是 `agent/run_windows.py --plain`，那一条用
    `config.run_policy("menu", task)` → `task_policy()`，save/pie 从 `.env` 的
    `UE_DEFAULT_SAVE` / `UE_DEFAULT_PIE` 读，写开关从 `.env` 的 `UE_ALLOW_*` 读。
    所以计划里「人开 session_task.json」这一步对本流水线**不是必需**，真正要人保证的是
    `.env` 里那几个开关——这也是人写的文件，模型同样不代写。
    """
    task = _session_task()
    writes = [str(item).strip() for item in (task.get("allow_writes") or []) if str(item).strip()]

    print("闸门 1 / 授权（安全边界）：两个入口两套策略源，本工具只读不写。")
    print("\n[DSH 入口] server/dsh_bridge.py → config.run_policy('dsh')")
    print(f"  文件：{_data_root() / 'dsh' / 'session_task.json'}")
    print(f"  allow_writes = {json.dumps(writes, ensure_ascii=False)}")
    print(f"  save = {str(task.get('save') is True).lower()}  pie = {str(task.get('pie') is True).lower()}")
    print("  本流水线不走这条路（bench_runner 直接起 run_windows），所以这里是「不适用」。")

    from config import ENV_PATH, env_value

    print("\n[menu 入口] agent/run_windows.py → config.task_policy()，从 .env 读")
    print(f"  文件：{ENV_PATH}")
    wanted = [item.strip() for item in str(need_env or "").split(",") if item.strip()]
    if not wanted:
        wanted = list(DEFAULT_ENV_GATES)

    gaps: list[str] = []
    for key in wanted:
        raw = env_value(ENV_PATH, key)
        ok = _truthy(raw)
        print(f"  {key} = {str(raw if raw is not None else '<未写>')}")
        if not ok:
            gaps.append(f"{key} 现在是 {raw!r}，这一步要它开")

    if gaps:
        print("\n还缺：")
        for gap in gaps:
            print(f"  - {gap}")
        print("\n请人按这次的任务改 `.env`（模型不代写），改完再审这个闸门。")
        return WAITING

    print("\n授权够用（menu 入口要的开关都开着）。")
    return 0


# --------------------------------------------------------------------------
# 闸门 2：PIE 看一眼
# --------------------------------------------------------------------------


def _run_path(run_id: str) -> Path | None:
    from agent.bench_record import find_run

    try:
        return find_run(run_id)
    except Exception:  # noqa: BLE001 - 找不到就返回 None，由调用方打印原因
        return None


def _window_rows(run: dict) -> list[dict]:
    windows = run.get("windows") or {}
    rows: list[dict] = []
    if isinstance(windows, dict):
        for name, item in windows.items():
            if isinstance(item, dict):
                row = dict(item)
                row.setdefault("window", name)
                rows.append(row)
    elif isinstance(windows, list):
        rows = [item for item in windows if isinstance(item, dict)]
    return rows


def gate_pie(run_id: str) -> int:
    """把「该看什么」摊开。判断在人的眼睛里，本工具不下结论。"""
    print("闸门 2 / PIE 看一眼（能力边界）：审美与手感，机器没有可靠裁判。")
    if not run_id:
        pending = list((_data_root() / "proofs" / "pending").glob("*.json"))
        for path in pending:
            data = _read_json(path) or {}
            text = str(data.get("evidence_path") or "")
            if text:
                print(f"  pending 证据：{text}")
        print("\n没给 --run-id。抓一个最近的 run 再来：--gate pie --run-id <run_id>")
        return WAITING

    run = _read_json(_run_path(run_id) or Path("__none__")) or {}
    print(f"  run_id：{run_id}")
    print(f"  原话：{_short(run.get('query'), 160)}")
    print(f"  整句三态：sentence={run.get('sentence')!r} status={run.get('status')!r}")
    rows = _window_rows(run)
    print(f"  窗口 {len(rows)} 个：")
    for row in rows:
        print(f"    - {row.get('action') or row.get('window')}: state={row.get('state')!r} ok={row.get('ok')!r}")
    criteria = [item for item in (run.get("criteria") or []) if isinstance(item, dict)]
    passed = sum(1 for item in criteria if item.get("passed") is True)
    print(f"  条款 {passed}/{len(criteria)} passed")
    print("\n要看的三处（人眼）：")
    print("  1. 敌人进 PIE 后**是否沿路径巡逻**（有位移、有朝向变化，不是原地抖）；")
    print("  2. 发现玩家后**是否转入追逐**（速度/朝向变了，而不是继续巡逻）；")
    print("  3. 贴身后**是否有近战表现**（动作或可见反馈，不是穿模过去）。")
    print("\n看完了再回话；本工具不替你判。下一步：--gate attest --criterion patrol-presentation")
    return WAITING


# --------------------------------------------------------------------------
# 闸门 3：签一条判断（调 attest_sign）
# --------------------------------------------------------------------------


def gate_attest(criterion: str, person: str, yes: bool, note: str) -> int:
    """人不碰路径和哈希：打印证据与三条看点，加 `--yes` 才写 attestation。"""
    if not criterion:
        print("--gate attest 要给 --criterion（例如 patrol-presentation）")
        return 1
    from agent import attest_sign

    argv = ["--criterion", criterion]
    if person:
        argv += ["--person", person]
    if note:
        argv += ["--note", note]
    if yes:
        argv.append("--yes")
    code = attest_sign.main(argv)
    if code == 0 and yes:
        folder = _data_root() / "attestations"
        target = folder / f"{criterion}.json"
        from agent.manual_attest import _read_valid

        record = _read_valid(target, criterion, folder)
        if record is None:
            print(f"人签写出来了却自己读不过：{target}")
            return 1
        print(f"机器复核通过：{target}")
        print(f"  person={record['person']} signed_at={record['signed_at']}")
        print(f"  evidence_sha1={record['evidence_sha1']}")
        print("\n下一步：python eval\\check_scorecard.py（A 维应转绿）")
    return code


# --------------------------------------------------------------------------
# 闸门 4：抽查 3 条（写 data/bench/audit.json）
# --------------------------------------------------------------------------


def _batch() -> tuple[Path, dict | None]:
    path = _data_root() / "bench" / "batch.json"
    return path, _read_json(path)


def _archive_verdict(run_id: str) -> dict | None:
    return _read_json(_data_root() / "archive" / str(run_id) / "verdict.json")


def _render_audit_item(index: int, run_id: str) -> None:
    verdict = _archive_verdict(run_id) or {}
    run = _read_json(_run_path(run_id) or Path("__none__")) or {}
    rows = _window_rows(run)
    failed = [row for row in rows if str(row.get("state") or "") == "failed"]
    criteria = [item for item in (run.get("criteria") or []) if isinstance(item, dict)]
    passed = sum(1 for item in criteria if item.get("passed") is True)
    print(f"\n—— 抽查第 {index} 条：{run_id} ——")
    print(f"  archive/verdict.json：{json.dumps(verdict, ensure_ascii=False)[:400]}")
    print(f"  sentence_id={verdict.get('sentence_id')!r} status={verdict.get('status')!r} "
          f"round={verdict.get('round')!r} evidence_kind={verdict.get('evidence_kind')!r}")
    print(f"  record_sha256={verdict.get('record_sha256')}")
    print(f"  run 原话：{_short(run.get('query'), 180)}")
    print(f"  run 三态：sentence={run.get('sentence')!r} status={run.get('status')!r}")
    print(f"  窗口 {len(rows)} 个，failed {len(failed)} 个；条款 {passed}/{len(criteria)} passed")
    for row in rows[:8]:
        print(f"    - {row.get('action') or row.get('window')}: state={row.get('state')!r} ok={row.get('ok')!r}")
    print(f"  run 文件：{_run_path(run_id)}")
    print(f"  events：{_data_root() / 'events' / (run_id + '.jsonl')}")


def _parse_marks(raw: list[str]) -> dict[str, tuple[bool, str]]:
    out: dict[str, tuple[bool, str]] = {}
    for item in raw or []:
        text = str(item).strip()
        if not text or "=" not in text:
            continue
        run_id, rest = text.split("=", 1)
        run_id = run_id.strip()
        parts = rest.split(":", 1) if ":" in rest else [rest, ""]
        flag = parts[0].strip().lower()
        note = parts[1].strip() if len(parts) > 1 else ""
        if not run_id:
            continue
        out[run_id] = (flag in ("ok", "1", "true", "yes"), note)
    return out


def gate_audit(person: str, verdict: str, marks: list[str], yes: bool, note: str) -> int:
    """摊开 `batch.sampled` 那三条，收人的逐条判断，写 `data/bench/audit.json`。"""
    path, batch = _batch()
    if not batch:
        print(f"没有一批机器基准：{path} 不存在或读不出来。先跑 agent\\bench_runner.py。")
        return 1
    sampled = [str(item) for item in (batch.get("sampled") or []) if str(item)]
    if not sampled:
        print("batch.sampled 是空的：这一批不足 3 条 run，抽不出来，不可审计。")
        return 1

    print("闸门 4 / 抽查（审计）：按 batch.sampled 对一眼。名单由 batch 的 seed 定，改不动。")
    print(f"  batch_id={batch.get('batch_id')} rounds={batch.get('rounds')} "
          f"seed={batch.get('seed')}")
    print(f"  记录 {len(batch.get('sentences') or [])} 条，缺 {len(batch.get('missing') or [])} 条")
    print(f"  sampled（{len(sampled)} 条）：" + "、".join(sampled))
    for index, run_id in enumerate(sampled, start=1):
        _render_audit_item(index, run_id)

    if not yes:
        print("\n看完再回话。逐条给判断（ok / 假），然后带 --person 与 --yes 再跑一次：")
        print("  --gate audit --person \"你的名字\" --verdict ok "
              "--mark <run_id>=ok[:一句话] --yes")
        return WAITING

    if not str(person or "").strip():
        print("要 `--person`：抽查是审计，得有人名。")
        return 1
    truth = str(verdict or "ok").strip().lower()
    if truth not in ("ok", "fake"):
        print("--verdict 只认 ok 或 fake。")
        return 1
    marked = _parse_marks(marks)
    missing = [run_id for run_id in sampled if run_id not in marked]
    if missing:
        print("这几条还没给判断：" + "、".join(missing))
        print("每条都要 --mark <run_id>=ok 或 <run_id>=fake[:原因]")
        return WAITING

    rows: list[dict] = []
    problems: list[str] = []
    for run_id in sampled:
        archive = _archive_verdict(run_id)
        if archive is None:
            problems.append(f"{run_id} 不在 data/archive 里")
            continue
        ok, item_note = marked[run_id]
        status = str(archive.get("status") or "")
        if not ok and truth == "ok":
            problems.append(f"{run_id} 人判为假，但 --verdict 是 ok；两边对不上")
        rows.append({
            "run_id": run_id,
            "status": status,
            "ok": bool(ok),
            "note": item_note,
        })
    if problems:
        print("这批回执有问题，不写：")
        for item in problems:
            print(f"  - {item}")
        return 1

    body = {
        "batch_id": str(batch.get("batch_id") or ""),
        "person": str(person).strip(),
        "signed_at": _now(),
        "verdict": truth,
        "produced_by": PRODUCED_BY,
        "notes": str(note or ""),
        "sampled": rows,
    }
    target = _data_root() / "bench" / "audit.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n抽查回执已写：{target}")
    for row in rows:
        print(f"  {row['run_id']}  status={row['status']}  ok={row['ok']}  {row['note']}")
    if truth == "ok":
        print("\n下一步：python eval\\check_bench_audit.py（应退 0）")
    else:
        print("\n判为假：整批作废，按 eval\\check_bench_audit.py 打印的目录清单删干净再重跑。")
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def gate_list() -> int:
    task = _session_task()
    print("四道人工闸门（计划 §3）。每一步都是工作流里的一次「人回话 → 机器记账」。")
    print(f"  1. authorize  现状 allow_writes={json.dumps(task.get('allow_writes') or [], ensure_ascii=False)} "
          f"save={str(task.get('save') is True).lower()} pie={str(task.get('pie') is True).lower()}")
    pending = sorted((_data_root() / "proofs" / "pending").glob("*.json"))
    print(f"  2. pie        待看证据 {len(pending)} 份")
    attestations = sorted((_data_root() / "attestations").glob("*.json"))
    print(f"  3. attest     已签 {len(attestations)} 条")
    _, batch = _batch()
    if batch:
        print(f"  4. audit      batch {batch.get('batch_id')} sampled={len(batch.get('sampled') or [])} 条，"
              f"audit.json {'已写' if (_data_root() / 'bench' / 'audit.json').is_file() else '未写'}")
    else:
        print("  4. audit      还没有批次")
    print("\n退 2 = 在等人回话；退 1 = 这一步本身有问题；退 0 = 闸门放行。")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="四道人工闸门的工作流外壳。")
    parser.add_argument("--gate", default="list", choices=("list",) + GATES)
    parser.add_argument("--need-env", default="", help="authorize：要检查的 .env 开关，逗号分隔")
    parser.add_argument("--run-id", default="", help="pie：要看哪个 run")
    parser.add_argument("--criterion", default="", help="attest：条款 id")
    parser.add_argument("--person", default="", help="attest/audit：人的名字")
    parser.add_argument("--verdict", default="ok", help="audit：ok 或 fake")
    parser.add_argument("--mark", action="append", default=[], help="audit：<run_id>=ok[:一句话]")
    parser.add_argument("--note", default="", help="attest/audit：一句话备注")
    parser.add_argument("--yes", action="store_true", help="人到场确认；不带就只展示")
    parser.add_argument("--data-root", default="", help="数据根，缺省 config.DATA_DIR（自测用）")
    args = parser.parse_args(argv)

    global _data_root
    if args.data_root:
        pinned = Path(args.data_root)

        def _pinned(_ignored=None, _root=pinned):
            return _root

        _data_root = _pinned  # type: ignore[assignment]

    if args.gate == "list":
        return gate_list()
    if args.gate == "authorize":
        return gate_authorize(args.need_env)
    if args.gate == "pie":
        return gate_pie(args.run_id)
    if args.gate == "attest":
        return gate_attest(args.criterion, args.person, args.yes, args.note)
    return gate_audit(args.person, args.verdict, args.mark, args.yes, args.note)


if __name__ == "__main__":
    raise SystemExit(main())
