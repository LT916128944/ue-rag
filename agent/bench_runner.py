# agent/bench_runner.py
"""十句基准的无人值守执行者（20261112-1220 计划 §4.1）。

它只做一件事：把「人一条一条跑」换成「机器按轮次跑」，并且让每一步都留下可复核的落盘事实。

一轮（一个句子的一轮）的十步固定动作，一步都不省：

1. 扫 `live_run_ids`，有活心跳就退 3 并打印 run_id，不 `--force`；
2. 该句在 `data/archive` 里已满 `--rounds` 轮就跳过（默认即 resume），重跑要显式 `--force-round`；
3. 记 `t0` + 快照 `data/runs/**/*.json` 文件名集合；
4. `Popen(run_windows.py --plain)`，把「句子的原话 + `:quit`」喂进 stdin；
5. 逐行读 stdout，抓 `窗口结束，退出码 N。记录：<path>`；没抓到就用「新增 run 文件」兜底；
6. 超时整棵进程树杀掉，本次尝试记 `timeout`；
7. 调 `agent.bench_record.record_round(run_id)`，`Refused` 就原样打印原因、不写 archive；
8. 每次尝试追加一行到 `data/bench/attempts.jsonl`（**不是** archive）；
9. 成功由 `record_round` 自己写 `data/archive/<run_id>/verdict.json`，轮次由 archive 计数自增；
10. 句间冷却，并检查 monolith 端口是否已释放。

失败策略：同一句同一轮最多 `--attempts` 次；都失败就把该句标 `give_up` 并跳过剩余轮次，
**不拿失败记录去凑分母**，最后打印「缺哪几句、原因原文」。

跑完写 `data/bench/batch.json`。`sampled` 是批次结束后用 batch 的 `seed` 抽的三条，
不可事后改：`eval/check_bench_audit.py` 用同一个 seed 重算，名单一动就红。

不读模型文本里的「成功」字样。状态只认 `record_round` 从 `data/runs` 的 `sentence` 字段
读回来的那一份——这是「机器自己证明自己做对了」的唯一入口。

用法：
    python agent\\bench_runner.py --rounds 3
    python agent\\bench_runner.py --only compile,open-level --rounds 1
    python agent\\bench_runner.py --only patrol-presentation --rounds 1 --no-record
    python agent\\bench_runner.py --only compile --rounds 1 --dry-run
    python agent\\bench_runner.py --rounds 3 --force-round
"""
from __future__ import annotations

import argparse
import json
import random
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MANIFEST = ROOT / "eval" / "bench_manifest.json"
PRODUCED_BY = "agent/bench_runner.py"
# 计划 §2.2 实测：这条行由 run_windows.py 打印，直接拿它，不必猜。
END_RE = re.compile(r"窗口结束，退出码\s*(-?\d+)。记录：\s*(\S+)")
DEFAULT_PORT = 9316
# Windows：子进程自成进程组，超时才能整棵树杀掉。
_CREATE_NEW_PROCESS_GROUP = 0x00000200
BUSY_EXIT = 3


class ResetUnsupported(Exception):
    """这一句的 `reset` 形状 runner 执行不了。消息就是给人看的原因。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _data_root(data_root: Path | None = None) -> Path:
    if data_root is not None:
        return Path(data_root)
    from config import DATA_DIR

    return Path(DATA_DIR)


def _bench_root(data_root: Path) -> Path:
    return data_root / "bench"


# --------------------------------------------------------------------------
# 清单与 archive 计数
# --------------------------------------------------------------------------


def _manifest_rows(manifest: Path = MANIFEST) -> list[dict]:
    data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    rows = data.get("sentences") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"{manifest} 里没有 sentences")
    return [row for row in rows if isinstance(row, dict)]


def _manifest_index(manifest: Path = MANIFEST) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in _manifest_rows(manifest):
        sid = str(row.get("id") or "").strip()
        if sid:
            out[sid] = row
    return out


def _archive_rounds(sentence_id: str, archive_root: Path) -> int:
    """该句在 archive 里已有几轮。只看 verdict.json 的 sentence_id。"""
    total = 0
    if not Path(archive_root).is_dir():
        return 0
    for path in Path(archive_root).glob("*/verdict.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and str(data.get("sentence_id") or "") == sentence_id:
            total += 1
    return total


def _archive_run_ids(archive_root: Path) -> set[str]:
    ids: set[str] = set()
    if not Path(archive_root).is_dir():
        return ids
    for path in Path(archive_root).glob("*/verdict.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            rid = str(data.get("run_id") or path.parent.name)
            if rid:
                ids.add(rid)
    return ids


def _archive_rows(archive_root: Path) -> list[dict]:
    """archive 里已有的全部记录（续跑时要把它们算进这一批，否则抽查门第 3 条会红）。"""
    rows: list[dict] = []
    if not Path(archive_root).is_dir():
        return rows
    for path in Path(archive_root).glob("*/verdict.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


# --------------------------------------------------------------------------
# reset 钩子（计划 §7 坑 1）
# --------------------------------------------------------------------------


def _apply_reset(row: dict, *, timeout: float | None = None) -> str:
    """每轮开始前执行该句的 `reset`。

    `sentences[i].reset` 认三种形状：

    - 缺省 / `{"kind": "none"}`：不做事；
    - `{"kind": "tool", "toolset": ..., "tool": ..., "args": {...}}`：走 `core.editor_exec.exec_tool`；
    - 其它：抛 `ResetUnsupported`，消息原样打印——**不假装做过**（计划 §9 决策 2 的认账路径）。

    返回一句给人看的说明（做过什么 / 没做什么），由调用方写进 attempts 与现场输出。
    """
    spec = row.get("reset")
    if not isinstance(spec, dict) or not spec:
        return ""
    kind = str(spec.get("kind") or "").strip()
    if kind in ("", "none"):
        return ""
    if kind != "tool":
        raise ResetUnsupported(
            f"{row.get('id')!r} 的 reset 形状 {kind!r} 不认识；"
            "只认 none / tool 两种，别的要在 bench_runner 里先实现再写进清单"
        )
    toolset = str(spec.get("toolset") or "").strip()
    tool = str(spec.get("tool") or "").strip()
    if not toolset or not tool:
        raise ResetUnsupported(f"{row.get('id')!r} 的 reset 缺 toolset/tool")
    args = spec.get("args") if isinstance(spec.get("args"), dict) else {}
    from core import editor_exec

    result = editor_exec.exec_tool(toolset, tool, dict(args), timeout=timeout)
    if not isinstance(result, dict):
        raise ResetUnsupported(f"reset {toolset}.{tool} 返回了非对象：{result!r}")
    if result.get("ok") is False:
        reason = result.get("error") or result.get("message") or json.dumps(
            result, ensure_ascii=False
        )[:300]
        raise ResetUnsupported(f"reset {toolset}.{tool} 被拒绝：{reason}")
    return f"reset {toolset}.{tool} ok"


# --------------------------------------------------------------------------
# 编辑器端口与进程
# --------------------------------------------------------------------------


def _port_listening(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def wait_port_released(port: int, limit: float) -> bool:
    """等到端口不再被占用。返回 True 表示已释放，False 表示等到 limit 秒还在听。"""
    deadline = time.monotonic() + max(0.0, float(limit))
    while True:
        if not _port_listening(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:  # noqa: BLE001 - 杀不掉就交给下一个兜底，不掩盖原异常
        pass
    try:
        proc.wait(timeout=15)
    except Exception:  # noqa: BLE001
        pass


def _snapshot_runs(runs_root: Path) -> set[str]:
    if not runs_root.is_dir():
        return set()
    return {
        str(path.resolve())
        for path in runs_root.rglob("*.json")
        if ".tmp" not in path.suffixes
    }


def _pick_new_run(before: set[str], runs_root: Path) -> str:
    """兜底：取新增 run 文件里最新的一份，返回 run_id（文件名主干）。"""
    fresh = [Path(item) for item in (_snapshot_runs(runs_root) - before)]
    if not fresh:
        return ""
    fresh.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return fresh[0].stem


# --------------------------------------------------------------------------
# 一次 run_windows
# --------------------------------------------------------------------------


def run_sentence(text: str, *, python: str, timeout: float, cwd: Path = ROOT,
                 env: dict | None = None) -> tuple[int, str, bool]:
    """起一次 `run_windows.py --plain`，喂原话 + `:quit`。返回 (退出码, 全部输出, 是否超时)。"""
    proc = subprocess.Popen(
        [python, "agent/run_windows.py", "--plain"],
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        creationflags=_CREATE_NEW_PROCESS_GROUP,
    )
    chunks: list[str] = []

    def _pump() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                chunks.append(line)
        except Exception:  # noqa: BLE001 - 读端异常不该盖住退出码
            pass

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(str(text).replace("\n", " ").strip() + "\n")
        proc.stdin.write(":quit\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        chunks.append(f"[bench_runner] 写入 stdin 失败：{exc}\n")

    timed_out = False
    try:
        code = proc.wait(timeout=float(timeout))
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        code = proc.returncode if proc.returncode is not None else -1
    reader.join(timeout=30)
    return int(code), "".join(chunks), timed_out


def parse_end_line(output: str) -> tuple[int | None, str]:
    """从输出里抓最后一次 `窗口结束，退出码 N。记录：<path>`。"""
    matches = END_RE.findall(output or "")
    if not matches:
        return None, ""
    code_text, path_text = matches[-1]
    try:
        code = int(code_text)
    except (TypeError, ValueError):
        code = None
    return code, path_text


# --------------------------------------------------------------------------
# 试跑形状（--dry-run）
# --------------------------------------------------------------------------


def synthetic_run(run_id: str, sentence_id: str) -> dict:
    """一条形状正确的假 run，只给 `--dry-run` 验 `record_round` 的键用。

    它不进 `data/runs`（写临时根），也不进 archive：dry-run 之后 archive 必须还是 0 份。
    """
    digest = "0" * 40
    return {
        "run_id": run_id,
        "query": "",
        "status": "成功",
        "sentence": "ok",
        "backend": "dry-run",
        "windows": {"w1": {"action": "dry_run", "state": "done", "ok": True}},
        "criteria": [
            {
                "id": f"{sentence_id}-synthetic",
                "evidence_kind": "editor_assert",
                "passed": True,
                "evidence_sha1": digest,
            }
        ],
    }


def dry_run_once(row: dict, *, data_root: Path) -> dict:
    """不碰编辑器、不写 archive：写一份临时 run，走一遍 `record_round(dry_run=True)`。"""
    import tempfile

    from agent.bench_record import record_round

    sentence_id = str(row.get("id") or "").strip()
    run_id = _stamp().replace("-", "")[:8] + "-000000"
    with tempfile.TemporaryDirectory(prefix="bench-dry-") as tmp:
        tmp_root = Path(tmp)
        runs_root = tmp_root / "runs" / _stamp()[:8]
        runs_root.mkdir(parents=True, exist_ok=True)
        record = synthetic_run(run_id, sentence_id)
        record["query"] = str(row.get("text") or "")
        (runs_root / f"{run_id}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        archive_root = tmp_root / "archive"
        return record_round(
            run_id, sentence_id,
            runs_root=tmp_root / "runs",
            archive_root=archive_root,
            dry_run=True,
        )


# --------------------------------------------------------------------------
# attempts.jsonl
# --------------------------------------------------------------------------


def append_attempt(data_root: Path, row: dict) -> Path:
    folder = _bench_root(data_root)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "attempts.jsonl"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return target


# --------------------------------------------------------------------------
# batch.json
# --------------------------------------------------------------------------


def build_batch(*, rounds: int, seed: str, note: str, produced_by: str = PRODUCED_BY,
                sentences: list[dict], missing: list[dict], started_at: str,
                finished_at: str) -> dict:
    """批次收尾形状。`sampled` 在这里才算，且只由 seed 决定。"""
    run_ids = sorted({
        str(item.get("run_id") or "") for item in sentences if item.get("run_id")
    })
    sampled: list[str] = []
    if len(run_ids) >= 3:
        sampled = random.Random(str(seed)).sample(run_ids, 3)
    return {
        "batch_id": f"{started_at[:19].replace('-', '').replace(':', '').replace('T', '-')}-{rounds}r",
        "started_at": started_at,
        "finished_at": finished_at,
        "rounds": int(rounds),
        "seed": str(seed),
        "produced_by": produced_by,
        "note": str(note or ""),
        "sentences": sentences,
        "missing": missing,
        "sampled": sampled,
    }


def write_batch(data_root: Path, batch: dict) -> Path:
    folder = _bench_root(data_root)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "batch.json"
    target.write_text(json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run(args) -> int:
    from agent.bench_record import Refused, record_round
    from agent.run_guard import live_run_ids

    data_root = _data_root(args.data_root)
    runs_root = data_root / "runs"
    archive_root = data_root / "archive"
    manifest = Path(args.manifest) if args.manifest else MANIFEST
    index = _manifest_index(manifest)

    if args.only:
        wanted = [item.strip() for item in args.only.split(",") if item.strip()]
        unknown = [item for item in wanted if item not in index]
        if unknown:
            print(f"--only 里有清单外的句子：{unknown}")
            return 1
        targets = [index[item] for item in wanted]
    else:
        targets = [row for row in _manifest_rows(manifest) if str(row.get("id") or "")]

    live = live_run_ids(runs_root)
    if live:
        print("仍在心跳的 run：" + "、".join(live))
        print("bench_runner 不 --force：等它们收尾，或先把死掉的 run 处理掉再跑。")
        return BUSY_EXIT

    python = args.python or sys.executable
    started_at = _now()
    seed = str(args.seed or secrets.token_hex(16))
    recorded: list[dict] = []
    missing: list[dict] = []
    # 续跑时 archive 里已有的那些记录也属于这一批：抽查门第 3 条要求
    # 「archive 的 run_id 集合 == batch.sentences 的 run_id 集合」，漏掉续跑的那些就会红。
    target_ids = {str(row.get("id") or "").strip() for row in targets}
    prior_rows = [
        row for row in _archive_rows(archive_root)
        if str(row.get("sentence_id") or "") in target_ids
    ]

    if _port_listening(args.port):
        print(f"提示：{args.port} 端口已经在听（编辑器可能已在跑），本轮复用不复位。")

    if args.dry_run:
        for row in targets:
            sentence_id = str(row.get("id") or "")
            outcome = dry_run_once(row, data_root=data_root)
            record = outcome["record"]
            append_attempt(data_root, {
                "ts": _now(),
                "sentence_id": sentence_id,
                "round": int(record.get("round") or 1),
                "run_id": "",
                "exit": 0,
                "status": "dry_run",
                "refused_reason": "",
                "seconds": 0.0,
                "dry_run": True,
            })
            print(f"[dry-run] {sentence_id} 第 {record.get('round')} 轮，要写的记录：")
            print(json.dumps(record, ensure_ascii=False, indent=2))
            print(f"[dry-run] {sentence_id}：archive 未落盘（{archive_root}）")
        return 0

    gave_up: set[str] = set()
    try:
        for row in targets:
            sentence_id = str(row.get("id") or "").strip()
            text = str(row.get("text") or "").strip()
            if not sentence_id or not text:
                continue
            done = _archive_rounds(sentence_id, archive_root)
            needed = int(args.rounds) if args.force_round else max(0, int(args.rounds) - done)
            if needed == 0:
                print(f"{sentence_id}：archive 已有 {done} 轮，跳过（重跑用 --force-round）")
                continue
            print(f"\n=== {sentence_id}：已有 {done} 轮，本轮做 {needed} 轮 ===")
            for _ in range(needed):
                if sentence_id in gave_up:
                    break
                try:
                    reset_note = _apply_reset(row, timeout=args.reset_timeout)
                except ResetUnsupported as exc:
                    print(f"{sentence_id}：reset 做不了，跳过剩余轮次 —— {exc}")
                    missing.append({
                        "id": sentence_id,
                        "round": _archive_rounds(sentence_id, archive_root) + 1,
                        "reason": f"reset 不可用：{exc}",
                    })
                    gave_up.add(sentence_id)
                    break
                if reset_note:
                    print(f"{sentence_id}：{reset_note}")

                settled = False
                last_reason = ""
                for attempt_no in range(1, max(1, int(args.attempts)) + 1):
                    round_no = _archive_rounds(sentence_id, archive_root) + 1
                    before = _snapshot_runs(runs_root)
                    started = time.perf_counter()
                    print(f"[{sentence_id} 第 {round_no} 轮 / 尝试 {attempt_no}] 起 run_windows…")
                    code, output, timed_out = run_sentence(
                        text, python=python, timeout=args.timeout,
                    )
                    seconds = round(time.perf_counter() - started, 1)
                    end_code, end_path = parse_end_line(output)
                    run_id = ""
                    if end_path:
                        run_id = Path(end_path).stem
                    if not run_id:
                        run_id = _pick_new_run(before, runs_root)
                        if run_id:
                            print(f"（没抓到「窗口结束」行，按新增 run 文件兜底：{run_id}）")

                    status = "timeout" if timed_out else ("exit_%s" % (code,))
                    refused = ""
                    if args.no_record:
                        status = "no_record"
                        print(f"[{sentence_id}] --no-record：run_id={run_id or '无'} 用时 {seconds}s")
                        settled = True
                    elif timed_out:
                        print(f"[{sentence_id}] 超时（{args.timeout}s），整棵进程树已杀")
                        last_reason = f"超时 {args.timeout}s"
                    elif not run_id:
                        print(f"[{sentence_id}] 这次没产出 run 记录（退出码 {code}），原样贴输出尾部：")
                        print((output or "").strip()[-800:])
                        last_reason = f"没有 run 记录（退出码 {code}）"
                    else:
                        try:
                            outcome = record_round(
                                run_id, sentence_id,
                                runs_root=runs_root, archive_root=archive_root,
                            )
                            status = str(outcome["record"].get("status") or "")
                            settled = True
                            print(
                                f"[{sentence_id}] 记录成功 {outcome['path']} "
                                f"status={status} round={outcome['record'].get('round')}"
                            )
                        except Refused as exc:
                            refused = str(exc)
                            status = "refused"
                            last_reason = refused
                            print(f"[{sentence_id}] 拒绝记录（原样）：{refused}")

                    append_attempt(data_root, {
                        "ts": _now(),
                        "sentence_id": sentence_id,
                        "round": round_no,
                        "run_id": run_id,
                        "exit": int(code),
                        "status": status,
                        "refused_reason": refused,
                        "seconds": seconds,
                    })
                    if settled:
                        recorded.append({
                            "id": sentence_id,
                            "round": round_no,
                            "run_id": run_id,
                            "status": status,
                            "attempts": attempt_no,
                            "seconds": seconds,
                        })
                        break
                    if attempt_no < int(args.attempts):
                        print(f"[{sentence_id}] 第 {attempt_no} 次没成，冷却 {args.cooldown}s 再试")
                        time.sleep(max(0.0, float(args.cooldown)))

                if not settled:
                    print(f"{sentence_id}：{args.attempts} 次都没成，give_up —— {last_reason}")
                    missing.append({
                        "id": sentence_id,
                        "round": _archive_rounds(sentence_id, archive_root) + 1,
                        "reason": last_reason or "未知原因",
                    })
                    gave_up.add(sentence_id)

                if args.cooldown and args.cooldown > 0:
                    time.sleep(float(args.cooldown))
                if not wait_port_released(args.port, args.port_wait):
                    print(f"警告：{args.port} 端口冷却 {args.port_wait}s 后仍在听；下一句 launch 可能撞端口")
    except KeyboardInterrupt:
        print("\n收到中断：已完成的轮次保留在 archive，batch.json 仍按现场情况写一份。")

    seen = {str(item.get("run_id") or "") for item in recorded}
    resumed: list[dict] = []
    for row in prior_rows:
        run_id = str(row.get("run_id") or "")
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        resumed.append({
            "id": str(row.get("sentence_id") or ""),
            "round": int(row.get("round") or 0),
            "run_id": run_id,
            "status": str(row.get("status") or ""),
            "attempts": 0,
            "seconds": 0.0,
            "resumed": True,
        })
    all_rows = sorted(resumed + recorded, key=lambda item: (item["id"], item.get("round") or 0))

    batch = build_batch(
        rounds=args.rounds, seed=seed, note=args.note or "",
        sentences=all_rows, missing=missing, started_at=started_at, finished_at=_now(),
    )
    if args.no_record:
        print("\n--no-record：不写 batch.json（这一轮只为产证据，不记分）")
    else:
        target = write_batch(data_root, batch)
        print(f"\n批次 {batch['batch_id']} 已写：{target}")
        print(f"  记录 {len(batch['sentences'])} 条（其中续跑 {len(resumed)} 条）/ 缺 {len(batch['missing'])} 条")
        if batch["sampled"]:
            print("  sampled（抽查名单，由 seed 定，不可事后改）：" + "、".join(batch["sampled"]))
        else:
            print("  sampled：不足 3 条 run，抽不出来（这一批不可审计）")

    archive_ids = _archive_run_ids(archive_root)
    batch_ids = {str(item.get("run_id") or "") for item in batch["sentences"]}
    stray = sorted(archive_ids - batch_ids)
    if stray:
        print(f"\n警告：archive 里还有 {len(stray)} 份不属于这一批的记录：" + "、".join(stray))
        print("      check_bench_audit 第 3 条会因此判红（它要求两边集合相等）。")
    if missing:
        print("\n缺哪几句：")
        for item in missing:
            print(f"  {item['id']} 第 {item['round']} 轮：{item['reason']}")
    print(f"\narchive 现有 {len(archive_ids)} 份记录。")
    return 0 if not missing else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="十句基准的无人值守执行者：按轮次跑 run_windows，落 archive，写 batch。"
    )
    parser.add_argument("--rounds", type=int, default=3, help="每句跑几轮，缺省 3")
    parser.add_argument("--only", default="", help="只跑这些句子，逗号分隔")
    parser.add_argument("--no-record", action="store_true", help="跑但不记 archive、不写 batch")
    parser.add_argument("--force-round", action="store_true", help="不管 archive 已有几轮，都重跑")
    parser.add_argument("--dry-run", action="store_true", help="不碰编辑器：只验 record_round 的记录形状")
    parser.add_argument("--attempts", type=int, default=2, help="同句同轮最多试几次，缺省 2")
    parser.add_argument("--timeout", type=float, default=2400.0, help="单句超时秒数，缺省 2400")
    parser.add_argument("--cooldown", type=float, default=15.0, help="句间冷却秒数，缺省 15")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="monolith 端口，缺省 9316")
    parser.add_argument("--port-wait", type=float, default=60.0, help="等端口释放的秒数，缺省 60")
    parser.add_argument("--reset-timeout", type=float, default=120.0, help="reset 调用超时秒数")
    parser.add_argument("--note", default="", help="写进 batch.json 的 note（例如预算上限）")
    parser.add_argument("--seed", default="", help="抽样种子；缺省由 secrets 生成，不复用")
    parser.add_argument("--python", default="", help="跑 run_windows 的解释器，缺省 sys.executable")
    parser.add_argument("--manifest", default="", help="清单路径，缺省 eval/bench_manifest.json")
    parser.add_argument("--data-root", default="", help="数据根，缺省 config.DATA_DIR（自测用）")
    args = parser.parse_args(argv)
    args.data_root = Path(args.data_root) if args.data_root else None
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
