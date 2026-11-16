# eval/run_bench.py
"""冻结的 10 句。

不带 --live：只核对不连编辑器的形状，每句写成 skipped。skipped 不是成功。
--live：完成记录不足 30 条，或 10 句没有各 3 轮时，不打印百分比，退出码 2。
--live --verify（20261111 P6）：先把每条记录同产地核一遍，核不过整条失败（退出码 1），
再谈百分比——记录对不上产地时的百分比没有意义。
不在这里启动编辑器。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MANIFEST = Path(__file__).resolve().parent / "bench_manifest.json"
DONE = ("ok", "partial", "failed")
_ALLOWED_RESET = {"readonly", "self_restore", "unsaved_level"}
_ALLOWED_PROFILE = {"pipeline", "agent", "slow"}
# 没有产地的记录：离线形状核对（bench-offline-*）与 skipped 都不进完成记录，也不许被 verify 认。
_NO_PROVENANCE_PREFIX = "bench-offline-"
SENTENCES = ("ok", "partial", "failed")


def validate_manifest() -> list[str]:
    """核对清单里的 tasks。不连编辑器。check_routes 依赖这份返回。"""
    from agent.task_schema import load_task
    from config import PROJECT_ROOT

    errors = []
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    tasks = data.get("tasks") or []
    if not tasks:
        return ["bench_manifest.json 没有 tasks"]
    seen = set()
    for item in tasks:
        if not isinstance(item, dict):
            errors.append("tasks 里有非对象")
            continue
        task_id = item.get("id") or ""
        if task_id in seen:
            errors.append(f"重复 id {task_id}")
        seen.add(task_id)
        if item.get("reset") not in _ALLOWED_RESET:
            errors.append(f"{task_id} reset 非法")
        if item.get("profile") not in _ALLOWED_PROFILE:
            errors.append(f"{task_id} profile 非法")
        if item.get("counts") and item.get("reset") == "unsaved_level":
            errors.append(f"{task_id} 改关卡，不能计入成功率")
        path = PROJECT_ROOT / item.get("path", "")
        if not path.is_file():
            errors.append(f"{task_id} 找不到 {path}")
            continue
        try:
            task = load_task(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{task_id} 读任务失败 {exc}")
            continue
        if task.get("id") != task_id:
            errors.append(f"{task_id} 与文件 id {task.get('id')} 不一致")
        if task.get("save") or task.get("pie"):
            errors.append(f"{task_id} 不许 save 或 pie")
        if item.get("profile") == "pipeline" and not task.get("judge_first"):
            errors.append(f"{task_id} pipeline 任务要 judge_first")
    for name in ("editor-cube.json", "bp-gun-compile.json", "cpp-typo.json"):
        path = PROJECT_ROOT / "eval" / "tasks" / name
        if not path.is_file():
            errors.append(f"默认三条缺少 {name}")
    return errors


def _load_manifest() -> list[dict]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows = data.get("sentences") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != 10:
        raise SystemExit("bench_manifest.json 要有 10 句")
    return rows


def _archive() -> Path:
    from config import DATA_DIR

    return DATA_DIR / "archive"


def _verdicts(folder: Path | None = None) -> list[dict]:
    folder = Path(folder) if folder is not None else _archive()
    rows = []
    if not folder.is_dir():
        return rows
    for path in sorted(folder.glob("*/verdict.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            data["_path"] = str(path)
            rows.append(data)
    return rows


def _false_pass(row: dict) -> bool:
    if row.get("false_pass") is True:
        return True
    if row.get("window_failed") and row.get("criteria_passed"):
        return True
    if row.get("status") == "ok" and row.get("manual_unsigned"):
        return True
    if row.get("status") == "ok" and row.get("existing_instance_property_as_ok") is True:
        return True
    return False


def _completed(rows: list[dict], ids: set[str]) -> list[dict]:
    return [
        row for row in rows
        if row.get("sentence_id") in ids and row.get("status") in DONE
    ]


def _ready(done: list[dict], ids: list[str]) -> bool:
    if len(done) < 30:
        return False
    counts: dict[str, int] = {}
    for row in done:
        key = str(row.get("sentence_id") or "")
        counts[key] = counts.get(key, 0) + 1
    return all(counts.get(item, 0) >= 3 for item in ids)


def _run_record(run_id: str, runs_root: Path) -> Path | None:
    """data/runs/YYYYMMDD/<run_id>.json；老 run 平铺在 data/runs 下也认。"""
    if not runs_root.is_dir():
        return None
    hits = [path for path in runs_root.glob(f"*/{run_id}.json") if path.is_file()]
    flat = runs_root / f"{run_id}.json"
    if flat.is_file():
        hits.append(flat)
    return hits[0] if len(hits) == 1 else None


def verify_records(rows: list[dict], *, archive: Path | None = None,
                   runs_root: Path | None = None) -> list[str]:
    """逐份核产地（20261111 P6）。返回问题清单，空清单才算过。

    一条记录要同时满足：

    1. 有多轮字段 `round`、有 `run_id`、有 `record_sha256`，且 `record_sha256`
       与 `agent.bench_record.record_digest(整份记录)` 相等（同一个函数，不另写一套）。
    2. `run_id` 在 `data/runs/` 里真有对应记录，那份记录的 `sentence` 与 `status` 一致。
    3. 归档目录名就是 `run_id`；不是 `bench-offline-*` 那批没有产地的离线核对。

    只核「声称跑完了」的那些行（`status` 是 ok / partial / failed）。`skipped` 是离线形状
    核对写的，不声称真机跑过，跳过；声称完成了却拿不出产地的一条都不放过。

    手写的 `verdict.json`（没有 `record_sha256`）与改过 `status` 的记录都会在这里落网。
    """
    from config import DATA_DIR

    from agent.bench_record import Refused, find_run, record_digest
    from agent.bench_record import _evidence_digest

    archive = Path(archive) if archive is not None else _archive()
    runs_root = Path(runs_root) if runs_root is not None else (DATA_DIR / "runs")
    problems: list[str] = []
    for row in rows:
        path = Path(str(row.get("_path") or ""))
        name = path.parent.name
        where = str(path) if str(path) else "(无路径)"
        if str(row.get("status") or "") not in DONE:
            # `skipped` 是「离线形状核对」写的那批（`run_bench.py` 不带 `--live` 时），
            # 它不声称真机跑过，也不是完成记录：既不进分母，也不该让 `--verify` 变红。
            # 声称完成了的记录（ok / partial / failed）一条都跑不掉，下面照旧逐份核产地。
            continue
        if name.startswith(_NO_PROVENANCE_PREFIX):
            problems.append(f"{where}: 目录名 {name} 说明这是离线形状核对，不是真机记录，不许当完成记录")
            continue
        missing = [key for key in ("record_sha256", "run_id", "round") if key not in row]
        if missing:
            problems.append(
                f"{where}: 缺 {('、'.join(missing))}——不是 agent/bench_record.py 写的记录"
                "（手写的 verdict.json 没有产地），整条不认"
            )
            continue
        stored = str(row.get("record_sha256") or "")
        # `_path` 是这里自己塞进去的排查字段，不是记录的一部分，算哈希前先摘掉。
        actual = record_digest({key: value for key, value in row.items() if key != "_path"})
        if stored != actual:
            problems.append(
                f"{where}: record_sha256 对不上（记录里 {stored or '空'}，重算 {actual}）"
                "——文件被改过，或被别的算法写过"
            )
            continue
        run_id = str(row.get("run_id") or "").strip()
        if name != run_id:
            problems.append(f"{where}: 归档目录名 {name} 与记录里的 run_id {run_id} 不一致")
            continue
        try:
            run_path = find_run(run_id, runs_root)
        except Refused as exc:
            problems.append(f"{where}: {exc}")
            continue
        try:
            run = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{where}: 读 run 记录失败 {run_path}：{exc}")
            continue
        if not isinstance(run, dict):
            problems.append(f"{where}: run 记录不是对象 {run_path}")
            continue
        if str(run.get("sentence") or "") != str(row.get("status") or ""):
            problems.append(
                f"{where}: status={row.get('status')!r} 与 data/runs 里那次的 "
                f"sentence={run.get('sentence')!r} 不一致"
            )
            continue
        if row.get("status") not in DONE:
            continue
        try:
            evidence = _evidence_digest(run)
        except Refused as exc:
            problems.append(f"{where}: 同产地复核证据失败：{exc}")
            continue
        if str(row.get("evidence_sha1") or "") != evidence:
            problems.append(
                f"{where}: evidence_sha1 与 data/runs 里那次的印章 {evidence} 不一致"
            )
    return problems


def _probe(sentence: dict) -> None:
    text = str(sentence.get("text") or "")
    kind = str(sentence.get("id") or "")
    if kind == "compile":
        from agent.supervise import _is_project_compile, _project_compile_task

        if not _is_project_compile(text):
            raise AssertionError("没有走工程编译规则")
        task = _project_compile_task(text)
        if task.get("title") != "编译工程":
            raise AssertionError(str(task.get("title")))
        return
    if kind == "fps-hud":
        from agent.fps_hud import hud_windows

        windows = hud_windows(text)
        if not windows:
            raise AssertionError("hud_windows 没有窗口")
        return
    if kind == "create-level":
        from agent.plan_windows import resolve_level
        from agent.supervise import _level_token, _wants_create_level

        if not _wants_create_level(text):
            raise AssertionError("没有认出创建关卡")
        found = resolve_level(_level_token(text))
        if not found.get("ok"):
            raise AssertionError(str(found.get("error") or found))
        return
    raise AssertionError(f"没有离线形状: {kind}")


def _write_skipped(sentence: dict, reason: str) -> None:
    from config import editor_backend

    run_id = "bench-offline-" + str(sentence.get("id") or "x")
    folder = _archive() / run_id
    folder.mkdir(parents=True, exist_ok=True)
    text = str(sentence.get("text") or "")
    body = {
        "sentence_id": sentence.get("id"),
        "status": "skipped",
        "evidence_sha1": hashlib.sha1(text.encode("utf-8")).hexdigest(),
        "entry": "offline",
        "backend": editor_backend(),
        "reason": reason,
        "false_pass": False,
        "window_failed": False,
        "criteria_passed": False,
        "manual_unsigned": False,
        "existing_instance_property_as_ok": False,
    }
    path = folder / "verdict.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _live(sentences: list[dict], *, verify: bool = False, archive: Path | None = None,
          runs_root: Path | None = None) -> int:
    ids = [str(item.get("id") or "") for item in sentences]
    rows = _verdicts(archive)
    done = _completed(rows, set(ids))
    if verify:
        # 产地核不过就整条失败：这时百分比没有意义，先别说数字。
        problems = verify_records(rows, archive=archive, runs_root=runs_root)
        if problems:
            print(f"产地核不过 {len(problems)} 条，整条失败：")
            for line in problems:
                print("  - " + line)
            return 1
        print(f"产地核对通过：{len(rows)} 份记录都带 record_sha256 且与 data/runs 对得上")
    if not _ready(done, ids):
        print("完成记录不足 30 条，不打印百分比")
        return 2
    ok = sum(1 for row in done if row.get("status") == "ok" and not _false_pass(row))
    rate = ok / len(done)
    print(f"完成记录 {len(done)} 条，成功率 {rate:.0%}")
    return 0


def _offline(sentences: list[dict]) -> int:
    failed = []
    for sentence in sentences:
        if sentence.get("offline") is True:
            try:
                _probe(sentence)
            except Exception as exc:
                _write_skipped(sentence, f"形状没对上: {exc}")
                failed.append(str(sentence.get("id")))
                continue
            _write_skipped(sentence, "离线形状已核对，跳过不算成功")
        else:
            _write_skipped(sentence, "离线不连编辑器")
    if failed:
        print("形状没对上: " + ", ".join(failed))
        return 1
    print("bench skipped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--verify", action="store_true",
        help="逐份重算 record_sha256，并核 run_id 在 data/runs 里的 sentence 与 status 一致",
    )
    args = parser.parse_args()
    sentences = _load_manifest()
    if args.live:
        return _live(sentences, verify=args.verify)
    if args.verify:
        problems = verify_records(_verdicts())
        if problems:
            print(f"产地核不过 {len(problems)} 条：")
            for line in problems:
                print("  - " + line)
            return 1
        print("产地核对通过")
        return 0
    return _offline(sentences)


if __name__ == "__main__":
    raise SystemExit(main())
