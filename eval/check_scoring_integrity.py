# eval/check_scoring_integrity.py
"""记分口径的自证（20261111 W1 P1/P2/P3）。全离线、可重复、退出码 0/1。

**硬规矩一：真实数据只读。**
    本文件与它驱动的所有断言，对真实数据**只读**。任何写操作只允许落在
    `tempfile` 造出来的临时目录里。**严禁**为了构造「坏状态」而原地替换、
    截断、临时挪走 `data/` 下的任何真实文件（本进程唯一的例外是 `data/tmp/` 下的
    单实例锁）。上一轮就是用「临时替换 data/costs.jsonl、finally 写回」的办法验证
    「清空后掉分」，一次写回失败直接丢了 1400 多条成本遥测。
    文件末尾的「⑧」把这条规矩变成可红断言：跑完之后真实数据的字节数与 mtime
    必须与跑之前完全一致。

**硬规矩二：不许挂。**
    - 每条断言有硬超时，超时判红并打印原因，不无限等。
    - 起 uvicorn 子进程那条：等待端口最多 `BOOT_SECONDS` 秒，HTTP 请求 `timeout=5`，
      子进程一律 `finally` 里 kill + wait，不留孤儿。
    - 整个检查有总时长上限 `TOTAL_BUDGET` 秒，断言之间用 `time.monotonic()` 检查，
      超了 `SystemExit(1)` 并打印「超时」。

**硬规矩三：不许并发。**
    `data/tmp/check_scoring_integrity.lock` 用 `open(..., "x")` 抢占；拿不到就立刻
    退出码 1 打印「另一个实例在跑」。理由：现场有多个 check_all 同时跑，会互相踩
    `data/harness/check_all.json`，结论不可信。

**八条断言**
1. 无证据（空 attestations + 空 archive）时 total 不含 A、B；
   把 A、B 从 BASE 删掉，total 不变——证明 A/B 的分只可能来自证据。
2. 真实数据下 A、B 仍带着缺口：没有机器判词就不许说已验。
3. 把 health 顶层 ok 改回写死 true → I 掉 2 分；改回来 → 那 2 分跟顶层 ok 一起回来。
4. 回执哈希改成假值 → I 掉分；回执 mtime 不对 → I 掉分；早于 venv → I 掉分。
5. 纯函数 caller_split_ok 对「空文件／单 caller／双 caller」分别判未验／未验／已验。
6. offline.yml 里没有 check_distill 时，缓存说它绿也不算：G 当场重跑、拿不到分。
7. health 起不来时 I 掉分（不许改成永远跳过）。
8. 断言没动真实数据（字节数与 mtime 一致）。

跑法：工作目录 RAG/ue-rag，`.venv\\\\Scripts\\\\python.exe eval/check_scoring_integrity.py`。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval._pin_offline import close_now  # noqa: E402

close_now()  # 离线封闭：本机 .env 的求解/放行开关不进断言

from eval import check_scorecard as sc  # noqa: E402

_GATEWAY = ROOT / "server" / "gateway.py"
_WORKFLOW = ROOT / ".github" / "workflows" / "offline.yml"
# health() 函数体里第一处 `"ok": …`（派生写法有嵌套括号，所以匹配到行尾逗号为止）。
_GATEWAY_OK_LINE = re.compile(r'^(\s*)"ok":\s*[^\n]+?,\s*$', re.MULTILINE)

LOCK_PATH = ROOT / "data" / "tmp" / "check_scoring_integrity.lock"
TOTAL_BUDGET = 240.0  # 整个检查的总时长上限（秒）：实测 76–87s，留出 2.5 倍余量
CASE_BUDGET = 120.0   # 单条断言的提示上限（真正兜底的是 TOTAL_BUDGET）
# 反向对照要跑一条「不在 workflow 里」的检查，它不存在、必然立刻失败，给 20 秒足够。
PROBE_TIMEOUT = 20.0

# 断言只可能碰这几个真实文件，且只读。
_WATCHED = (
    ROOT / "data" / "costs.jsonl",
    ROOT / "data" / "harness" / "setup_receipt.json",
    ROOT / "data" / "harness" / "check_all.json",
    ROOT / "data" / "harness" / "scorecard.json",
)

_STARTED = time.monotonic()


def _elapsed() -> float:
    return time.monotonic() - _STARTED


def _left() -> float:
    return TOTAL_BUDGET - _elapsed()


def _cap_budget(seconds: float) -> None:
    """把「这一条最多还能跑多久」压进 check_scorecard 的子进程超时里，免得单条吃掉整个预算。"""
    sc.CHECK_TIMEOUT = max(5.0, float(seconds))


def _report(name: str, ok: bool, note: str) -> bool:
    print(f"{'✅' if ok else '❌'} {name}：{note}")
    if not ok:
        print(f"   缺口明细：{note}")
    return ok


def _scoreboard(**kwargs) -> dict:
    return sc._score(**kwargs)


def _snapshot() -> dict[str, tuple[int, float] | None]:
    """真实数据的 (字节数, mtime)。不存在记 None。只读 stat，不打开文件。"""
    found: dict[str, tuple[int, float] | None] = {}
    for path in _WATCHED:
        try:
            stat = path.stat()
        except OSError:
            found[str(path)] = None
            continue
        found[str(path)] = (stat.st_size, stat.st_mtime)
    return found


def _empty_root(folder: Path) -> Path:
    """一个只有 bench_manifest 的根：没有 attestations，没有 archive/*/verdict.json。"""
    (folder / "data" / "attestations").mkdir(parents=True, exist_ok=True)
    (folder / "data" / "archive").mkdir(parents=True, exist_ok=True)
    (folder / "eval").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "eval" / "bench_manifest.json", folder / "eval" / "bench_manifest.json")
    return folder


def _rows_file(folder: Path, name: str, lines: list[str]) -> list[dict]:
    """在临时目录里写一份 costs 并读成 rows。写只发生在 tempfile 里。"""
    path = folder / name
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _quiet_cached(folder: Path, present: int = 0) -> Path:
    """给临时根的评分一次「所有 evidence 检查都已通过」的缓存。

    配合 _Summary(trust=True) 用：评一条断言只跑 check_scorecard 自己的记分逻辑，
    不用把每条 evidence 检查真跑一遍（那要好几分钟，会把整条断言拖成 timeout）。
    缓存只替换「谓词退出码」这个输入，不改被断言的记分逻辑：A、B 的
    verified/basis/counted 都不看缓存。缓存策略本身由第 ⑥ 条单独钉住。
    """
    names = list(sc.ALL_CHECKS) + [name for group in sc.PREDICATE_CHECKS.values() for name in group]
    path = folder / "check_all.json"
    path.write_text(
        json.dumps({"checks": [
            {"name": name, "exit": present} for name in dict.fromkeys(names)
        ]}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


class _Summary:
    """临时把 UE_CHECK_ALL_SUMMARY 指到给定文件、把 UE_CHECK_ALL_TRUST_CACHE 设成给定值，
    退出时一并还原。"""

    def __init__(self, path: Path | None, *, trust: bool = True) -> None:
        self.path = path
        self.trust = trust
        self.previous: str | None = None
        self.previous_trust: str | None = None

    def __enter__(self) -> None:
        self.previous = os.environ.get("UE_CHECK_ALL_SUMMARY")
        self.previous_trust = os.environ.get("UE_CHECK_ALL_TRUST_CACHE")
        if self.path is None:
            os.environ.pop("UE_CHECK_ALL_SUMMARY", None)
        else:
            os.environ["UE_CHECK_ALL_SUMMARY"] = str(self.path)
        os.environ["UE_CHECK_ALL_TRUST_CACHE"] = "1" if self.trust else "0"

    def __exit__(self, *exc) -> None:
        for key, saved in (("UE_CHECK_ALL_SUMMARY", self.previous),
                           ("UE_CHECK_ALL_TRUST_CACHE", self.previous_trust)):
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved


# ---------------------------------------------------------------- ①
def _case_ab_need_evidence() -> bool:
    quiet = {k: v for k, v in sc.BASE.items() if k not in ("A", "B")}
    with tempfile.TemporaryDirectory() as tmp:
        empty = _empty_root(Path(tmp))
        with _Summary(_quiet_cached(empty)):
            normal = _scoreboard(root=empty)
            trimmed = _scoreboard(base=quiet, root=empty)
    dims = normal["dimensions"]
    checks = [
        ("A 在无证据时 verified=false", dims["A"]["verified"] is False),
        ("B 在无证据时 verified=false", dims["B"]["verified"] is False),
        ("A、B 的 basis 是 baseline",
         dims["A"]["basis"] == "baseline" and dims["B"]["basis"] == "baseline"),
        ("A、B 的分数没进 total", dims["A"]["counted"] == 0 and dims["B"]["counted"] == 0),
        (f"total 恰好等于已验维度的分数和（不含 A、B 的 {sc.BASE['A'] + sc.BASE['B']} 分）",
         normal["total"] == sum(dims[k]["counted"] for k in sc.ORDER)),
        ("total 里每一个 counted 都来自 verified 为真的维度",
         all(dims[k]["counted"] == 0 for k in sc.ORDER if not dims[k]["verified"])),
        ("把 A、B 从 BASE 删掉，total 不变", normal["total"] == trimmed["total"]),
    ]
    bad = [name for name, ok in checks if not ok]
    return _report(
        "① 无证据时 A、B 不计分",
        not bad,
        f"临时根（空 attestations、空 archive）total={normal['total']}，"
        f"删掉 A/B 的 BASE 后 total={trimmed['total']}"
        + ("；全部成立" if not bad else "；不成立：" + "；".join(bad)),
    )


# ---------------------------------------------------------------- ②
def _case_real_data_ab_unverified() -> bool:
    """A、B 必须带着缺口：没有比这更强的证据时不许说已验。只读现状，不伪造证据。

    evidence 检查那一半（C/D/E/G/H）给一份「都还绿」的缓存，免得为了验 A、B 把
    每条检查真跑一遍；A、B 的 verified/basis 完全不看那份缓存。
    """
    archive = ROOT / "data" / "archive"
    verdicts = sorted(archive.glob("*/verdict.json")) if archive.is_dir() else []
    ok_rows = []
    for path in verdicts:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(row, dict) and row.get("status") == "ok":
            ok_rows.append(row)
    machine_ok = [row for row in ok_rows if row.get("evidence_kind") == "machine"]
    with tempfile.TemporaryDirectory() as tmp:
        with _Summary(_quiet_cached(Path(tmp))):
            dims = _scoreboard()["dimensions"]
    checks = [
        ("A 的 basis 只能来自人签",
         dims["A"]["verified"] is True or dims["A"]["basis"] == "baseline"),
        ("archive 里没有判词时 B 不许已验", bool(verdicts) or dims["B"]["verified"] is False),
        ("B 已验就必须有 machine ok 的判词", dims["B"]["verified"] is False or bool(machine_ok)),
    ]
    bad = [name for name, ok in checks if not ok]
    return _report(
        "② 真实数据下 A、B 仍是未验",
        not bad,
        f"判词 {len(verdicts)} 份，status=ok {len(ok_rows)} 条（machine ok {len(machine_ok)} 条），"
        f"A.verified={dims['A']['verified']}（{dims['A']['basis']}），"
        f"B.verified={dims['B']['verified']}（{dims['B']['basis']}）"
        + ("" if not bad else "；不成立：" + "；".join(bad)),
    )


# ---------------------------------------------------------------- ③
def _gateway_fixed_version() -> str:
    """把 health() 顶层 ok 改回写死：health() 函数体里第一处 `"ok": …` 换成 `"ok": True,`。
    只在这条断言里临时改 server/gateway.py（源码，不是运行态数据），finally 原样写回；
    替换不到就返回空串，让断言直接红。"""
    text = _GATEWAY.read_text(encoding="utf-8")
    start = text.find("def health() -> dict:")
    if start < 0:
        return ""
    head, body = text[:start], text[start:]
    patched, count = _GATEWAY_OK_LINE.subn(r'\1"ok": True,', body, count=1)
    return head + patched if count == 1 else ""


# ---------------------------------------------------------------- ③ / ④ 共用夹具
def _setup_fixture(folder: Path) -> tuple[Path, Path]:
    """在临时目录里造一份 **真能判过** 的 harness + venv，供 ③ ④ 把 I 的 setup 半边钉死。

    返回 (harness_dir, venv_root)。造的东西：
      harness/check_all.json      —— 全部检查 exit 0
      harness/setup_receipt.json  —— ok:true、editor_seen:true、python 3.12、
                                     check_all_sha256 = 那份 check_all.json 的字节哈希、
                                     check_all_mtime = 它的 mtime
      venv_root/.venv/            —— 一个 pyvenv.cfg，代表「建过 venv」，时间早于回执
    全部写在临时目录里，真实 data/harness/ 一个字节不动。
    """
    harness = folder / "harness"
    harness.mkdir(parents=True, exist_ok=True)
    summary = harness / "check_all.json"
    summary.write_text(
        json.dumps({"checks": [{"name": n, "exit": 0} for n in sc.ALL_CHECKS]},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    venv_root = folder / "fake_root"
    venv_dir = venv_root / ".venv"
    venv_dir.mkdir(parents=True, exist_ok=True)
    cfg = venv_dir / "pyvenv.cfg"
    cfg.write_text("home = fake\n", encoding="utf-8")
    venv_at = time.time() - 86400 * 30          # 假的 venv：一个月前建的
    os.utime(cfg, (venv_at, venv_at))
    os.utime(venv_dir, (venv_at, venv_at))
    receipt = harness / "setup_receipt.json"
    receipt.write_text(json.dumps({
        "ok": True,
        "ok_source": "check_all",
        "python": "3.12.10",
        "venv": str(venv_dir / "Scripts" / "python.exe"),
        "editor_seen": True,
        "editor": "fixture",
        "check_all_sha256": sc.sha256_file(summary),
        "check_all_mtime": summary.stat().st_mtime,
        "check_all_exit": 0,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fresh = time.time()
    os.utime(receipt, (fresh, fresh))           # 回执必须比 venv 新
    return harness, venv_root


def _health_ok(ok: bool = True):
    """I 的 health 半边替身：把结论钉成给定值，好让 setup 成为唯一变量。"""
    def _stub() -> tuple[bool, str, str]:
        return ok, "fixture(health)", "" if ok else "fixture(health 判红)"
    return _stub


# ---------------------------------------------------------------- ③
def _case_health_written_true() -> bool:
    """③ 在「setup 真通过」的前提下，把 health 顶层 ok 改回写死 → I 必须掉 4→2。

    关键：先把 setup 半边在临时目录里造到真通过，否则 health 坏了 I 也还是 BASE，
    看不出任何差别——那这条断言就是恒真的。
    """
    original = _GATEWAY.read_text(encoding="utf-8")
    fixed = _gateway_fixed_version()
    if not fixed:
        return _report("③ health 顶层 ok 改回写死 → I 掉分", False,
                       "没找到可替换的顶层 ok 行，这条断言没验到")
    base = {k: v for k, v in sc.BASE.items() if k not in ("A", "B")}
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        harness, venv_root = _setup_fixture(folder)
        with _Summary(_quiet_cached(folder)):
            # 先确认夹具本身真的让 setup 判过
            setup_ok, setup_gap, _ = sc._setup_ran({}, sc._workflow_checks(), harness, venv_root)
            # A：health 诚实（探针真跑）
            real = _scoreboard(base=base, harness=harness, venv_root=venv_root)["dimensions"]["I"]
            # B：源码改成写死 → health 真判红 → I 必须掉下来
            try:
                _GATEWAY.write_text(fixed, encoding="utf-8")
                broken = _scoreboard(base=base, harness=harness, venv_root=venv_root)["dimensions"]["I"]
            finally:
                _GATEWAY.write_text(original, encoding="utf-8")
            # C：改回诚实源码 → 拿回 4 分
            restored = _scoreboard(base=base, harness=harness, venv_root=venv_root)["dimensions"]["I"]
    checks = [
        ("夹具让 setup 半边真判过", setup_ok is True),
        ("诚实源码真跑探针 → I 已验且 4 分",
         real["verified"] is True and real["score"] == sc.BASE["I"] + 2),
        ("诚实源码真跑探针 → counted=4", real["counted"] == 4),
        ("把 ok 写死 → I 不是已验", broken["verified"] is False),
        ("把 ok 写死 → I 从 4 掉到 2", broken["score"] == sc.BASE["I"]),
        ("把 ok 写死 → counted 从 4 掉到 0", broken["counted"] == 0),
        ("缺口点名「写死」", "写死" in (broken["gap"] or "")),
        ("改回诚实源码 → I 拿回 4 分",
         restored["score"] == sc.BASE["I"] + 2 and restored["verified"] is True),
    ]
    bad = [name for name, ok in checks if not ok]
    return _report(
        "③ health 顶层 ok 改回写死 → I 掉分",
        not bad,
        f"setup 夹具判过={setup_ok}（{setup_gap or '无缺口'}）；"
        f"诚实源码 I: score={real['score']} verified={real['verified']} counted={real['counted']}；"
        f"写死之后 I: score={broken['score']} verified={broken['verified']} "
        f"counted={broken['counted']}（{broken['gap']}）；"
        f"改回 I: score={restored['score']}"
        + ("；全部成立" if not bad else "；不成立：" + "；".join(bad)),
    )


# ---------------------------------------------------------------- ④
def _case_receipt_hash() -> bool:
    """④ 在「health 真通过」的前提下，把回执哈希/时间做坏 → I 必须掉 4→2。

    与 ③ 互为对照：③ 钉 setup、让 health 成为唯一变量；④ 钉 health、让 setup 成为唯一变量。
    两条都不许建立在「另一边本来就红」之上，否则都恒真。
    所有坏状态只写临时目录；真实 data/harness/ 一个字节不动。
    """
    base = {k: v for k, v in sc.BASE.items() if k not in ("A", "B")}
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        harness, venv_root = _setup_fixture(folder)
        receipt_path = harness / "setup_receipt.json"
        honest_data = json.loads(receipt_path.read_text(encoding="utf-8"))
        venv_at = sc.venv_created_at(venv_root)

        def write(payload: dict) -> None:
            receipt_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
            now = time.time()
            os.utime(receipt_path, (now, now))

        def verdict() -> dict:
            return _scoreboard(base=base, harness=harness, venv_root=venv_root,
                               health_ready=_health_ok(True))["dimensions"]["I"]

        with _Summary(_quiet_cached(folder)):
            write(honest_data)
            honest = verdict()
            # 坏法一：哈希改成假值
            payload = dict(honest_data)
            payload["check_all_sha256"] = "0" * 64
            write(payload)
            bad_hash = verdict()
            # 坏法二：哈希对，但 mtime 记的不是现在那份
            payload = dict(honest_data)
            payload["check_all_mtime"] = 0.0
            write(payload)
            bad_time = verdict()
            # 坏法三：哈希与时间都对，只是回执早于 venv
            write(honest_data)
            if venv_at is not None:
                old = venv_at - 86400
                os.utime(receipt_path, (old, old))
            bad_old = verdict()
            write(honest_data)
            restored = verdict()
    checks = [
        ("health 通过 + 诚实回执 → I 已验且 4 分",
         honest["verified"] is True and honest["score"] == sc.BASE["I"] + 2),
        ("假哈希 → I 掉到 2 且 counted=0",
         bad_hash["score"] == sc.BASE["I"] and bad_hash["counted"] == 0
         and bad_hash["verified"] is False),
        ("假哈希的缺口点名哈希", "哈希" in (bad_hash["gap"] or "")),
        ("错时间 → I 掉到 2", bad_time["score"] == sc.BASE["I"] and bad_time["verified"] is False),
        ("错时间的缺口点名修改时间", "修改时间" in (bad_time["gap"] or "")),
        ("回执早于 venv → I 掉到 2", bad_old["score"] == sc.BASE["I"] and bad_old["verified"] is False),
        ("早于 venv 的缺口点名 venv", "venv" in (bad_old["gap"] or "")),
        ("回执修回来 → I 拿回 4 分",
         restored["score"] == sc.BASE["I"] + 2 and restored["verified"] is True),
    ]
    bad = [name for name, ok in checks if not ok]
    return _report(
        "④ 回执哈希改成假值 / 时间早于 venv → I 掉分",
        not bad,
        f"诚实 I: score={honest['score']} verified={honest['verified']}；"
        f"假哈希 score={bad_hash['score']} counted={bad_hash['counted']}（{bad_hash['gap']}）；"
        f"错时间 score={bad_time['score']}（{bad_time['gap']}）；"
        f"早于 venv score={bad_old['score']}（{bad_old['gap']}）；"
        f"修回 score={restored['score']}"
        + ("；全部成立" if not bad else "；不成立：" + "；".join(bad)),
    )


# ---------------------------------------------------------------- ⑤
def _case_caller_split_pure() -> bool:
    """只喂纯函数，只写临时目录。三种输入：空／单 caller／双 caller。"""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        empty = _rows_file(folder, "empty.jsonl", [])
        one = _rows_file(folder, "one.jsonl", [
            json.dumps({"caller": "plan", "ts": "2026-11-11T00:00:00", "prompt_tokens": 1}),
        ])
        two = _rows_file(folder, "two.jsonl", [
            json.dumps({"caller": "plan", "ts": "2026-11-11T00:00:00", "prompt_tokens": 1}),
            json.dumps({"caller": "solve", "ts": "2026-11-11T00:00:00", "prompt_tokens": 2}),
        ])
        results = {
            "empty": sc.caller_split_detail(empty),
            "one": sc.caller_split_detail(one),
            "two": sc.caller_split_detail(two),
        }
    checks = [
        ("空文件判成未验", results["empty"][0] is False),
        ("空文件的缺口说的是 0 个 caller", "只有 0 个 caller" in results["empty"][1]),
        ("单 caller 判成未验", results["one"][0] is False),
        ("单 caller 的缺口说的是 1 个 caller", "只有 1 个 caller" in results["one"][1]),
        ("双 caller 判成已验", results["two"][0] is True),
        ("双 caller 两个都真打出了小计", results["two"][2] == ["plan", "solve"]),
    ]
    bad = [name for name, ok in checks if not ok]
    return _report(
        "⑤ costs 判据是纯函数（只碰临时目录）",
        not bad,
        f"空→{results['empty'][:2]}；单 caller→{results['one'][:2]}；"
        f"双 caller→{results['two'][:2]}（{results['two'][2]}）"
        + ("；全部成立" if not bad else "；不成立：" + "；".join(bad)),
    )


# ---------------------------------------------------------------- ⑥
def _case_cache_outside_ci() -> bool:
    """「没验过的绿不许算分」：钉住 _run_check 的四种取数路径，一次不真跑。

    为什么不真跑：真跑 check_distill 要 15 秒，整条断言必然拖垮总预算；而「G 有没有
    记分」是 _run_check 的返回值乘上 PREDICATE_CHECKS 的分支，验它的取数顺序就够。
    workflow 的检查清单**读成内存里的 set**，全程不改 offline.yml（现场已经因为
    「改文件 + 被 kill」把 workflow 留在残缺状态过）。
    """
    real_in_ci = sc._workflow_checks()
    ghost = "check_scoring_integrity_ghost_probe"
    original_flow = _WORKFLOW.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        cached = folder / "check_all.json"
        cached.write_text(
            json.dumps({"checks": [{"name": "check_distill", "exit": 0},
                                   {"name": ghost, "exit": 0}]}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            with _Summary(cached):
                # ① 默认模式 + 在 CI + 缓存说绿 → 仍必须重跑，来源写「重跑」
                os.environ.pop("UE_CHECK_ALL_TRUST_CACHE", None)
                default_code, default_src = sc._run_check(
                    "check_distill", {"check_distill": 0}, real_in_ci,
                )
                # ② 显式 trust=1 + 在 CI + 缓存说绿 → 才允许吃缓存
                os.environ["UE_CHECK_ALL_TRUST_CACHE"] = "1"
                try:
                    trust_code, trust_src = sc._run_check(
                        "check_distill", {"check_distill": 0}, real_in_ci,
                    )
                finally:
                    os.environ.pop("UE_CHECK_ALL_TRUST_CACHE", None)
                # ③ 不论哪种模式，不在 workflow 的检查都拒吃缓存
                ghost_code, ghost_src = sc._run_check(ghost, {ghost: 0}, real_in_ci)
                os.environ["UE_CHECK_ALL_TRUST_CACHE"] = "1"
                try:
                    ghost_trust_code, ghost_trust_src = sc._run_check(ghost, {ghost: 0}, real_in_ci)
                finally:
                    os.environ.pop("UE_CHECK_ALL_TRUST_CACHE", None)
                # ④ 缓存说红一律采信（两种模式都不给重跑洗白）
                red_code, red_src = sc._run_check("check_distill", {"check_distill": 1}, real_in_ci)
        finally:
            if _WORKFLOW.read_text(encoding="utf-8") != original_flow:
                _WORKFLOW.write_text(original_flow, encoding="utf-8")

    def g_score(code: int) -> int:
        return sc.BASE["G"] + (1 if code == 0 else 0)

    checks = [
        ("默认模式下缓存说绿也重跑（来源写「重跑」）", "重跑" in default_src
         and "check_all.json#" not in default_src),
        ("默认模式重跑出真结论（check_distill 实际是绿的）", default_code == 0),
        ("默认模式 G 的分只来自重跑", g_score(default_code) == 9),
        ("显式 trust=1 才吃缓存", "check_all.json#check_distill" in trust_src and trust_code == 0),
        ("默认模式下不在 workflow 的检查和 trust 模式一样拒吃缓存",
         "check_all.json#" not in ghost_src and "check_all.json#" not in ghost_trust_src),
        ("不在 workflow 的 ghost 重跑出红，G 因此少 1 分",
         ghost_code != 0 and g_score(ghost_code) == 8),
        ("缓存说红一律采信、不给重跑洗白", red_code != 0 and "check_all.json#" in red_src),
    ]
    bad = [name for name, ok in checks if not ok]
    return _report(
        "⑥ 没验过的绿不算分（默认重跑；只有 TRUST_CACHE=1 才吃缓存）",
        not bad,
        f"默认 → {default_src} 退出码 {default_code}（G={g_score(default_code)}）；"
        f"trust=1 → {trust_src}；ghost → {ghost_src} 退出码 {ghost_code}（G={g_score(ghost_code)}）；"
        f"缓存红 → {red_src} 退出码 {red_code}"
        + ("；全部成立" if not bad else "；不成立：" + "；".join(bad)),
    )


# ---------------------------------------------------------------- ⑦
def _case_health_unreachable() -> bool:
    """I 不许「连不上就跳过」：把 uvicorn 钉在 -1 端口（建不了监听），必须变成未验。
    改的是「服务起在哪」，不是「去哪打」——去哪打永远是 127.0.0.1:8010 那一句。

    -1 端口根本建不了监听，子进程立刻退出，所以这条实测 0.0 秒；门槛按「够不够一次
    正常启动」给（实测正常启动 2.7 秒），不是按 HEALTH_BOOT_SECONDS 那 60 秒给。"""
    need = 20.0
    if _left() < need:
        return _report("⑦ health 读不到 → I 掉分（不跳过）", False,
                       f"总预算只剩 {_left():.1f}s，不到这条断言需要的 {need:.0f}s")
    original = sc.HEALTH_PORT
    started = time.monotonic()
    try:
        sc.HEALTH_PORT = -1
        ok, evidence, gap = sc._health_ready()
    finally:
        sc.HEALTH_PORT = original
    spent = time.monotonic() - started
    checks = [
        ("服务起不来时 I 的健康项为假", ok is False),
        ("缺口说「没连上」", "没连上" in (gap or "")),
        (f"没挂：{spent:.1f}s 就判红（上限 {need:.0f}s）", spent <= need),
    ]
    bad = [name for name, ok_ in checks if not ok_]
    return _report(
        "⑦ health 读不到 → I 掉分（不跳过）",
        not bad,
        f"服务起不来时 ok={ok}，缺口={gap}，耗时 {spent:.1f}s"
        + ("；全部成立" if not bad else "；不成立：" + "；".join(bad)),
    )


# ---------------------------------------------------------------- 锁
def _alive(pid: int) -> bool:
    """这个 pid 现在还活着吗。Windows 用 OpenProcess，其它平台用 os.kill(pid, 0)。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class _Lock:
    """`data/tmp/<name>.lock` 抢 pid 文件。拿不到立刻返回 False，不等待。

    持锁的进程已经死了（被杀 / 崩溃没清）就回收这把锁——那是陈旧锁，不是「别人在跑」。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def _stale(self) -> bool:
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            return True  # 读不出来的锁等于没有锁
        return not _alive(pid)

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                handle = self.path.open("x", encoding="utf-8")
            except FileExistsError:
                if attempt == 0 and self._stale():
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                return False
            try:
                handle.write(str(os.getpid()))
            finally:
                handle.close()
            self.held = True
            return True
        return False

    def __exit__(self, *exc) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False


def main() -> int:
    with _Lock(LOCK_PATH) as got:
        if not got:
            print(f"❌ 另一个实例在跑（{LOCK_PATH}）。这次不跑，退出码 1。")
            return 1
        return _run()


def _run() -> int:
    before = _snapshot()
    cases = (
        _case_ab_need_evidence,
        _case_real_data_ab_unverified,
        _case_health_written_true,
        _case_receipt_hash,
        _case_caller_split_pure,
        _case_cache_outside_ci,
        _case_health_unreachable,
    )
    results = []
    for case in cases:
        if _left() <= 0:
            print(f"❌ 超时：总预算 {TOTAL_BUDGET:.0f}s 用完（停在 {case.__name__}）")
            return 1
        started = time.monotonic()
        _cap_budget(min(_left(), CASE_BUDGET))
        try:
            ok = case()
        except Exception as exc:  # 断言炸了也是红，不是跳过
            ok = _report(case.__name__, False, f"断言炸了：{type(exc).__name__}: {exc}")
        spent = time.monotonic() - started
        if spent > CASE_BUDGET:
            print(f"   ⚠ {case.__name__} 用了 {spent:.1f}s（超过单条提示上限 {CASE_BUDGET:.0f}s）")
        results.append(ok)
    after = _snapshot()
    drifted = [
        f"{Path(path).name} {before[path]} → {after[path]}"
        for path in before
        if before[path] != after[path]
    ]
    results.append(_report(
        "⑧ 断言没动真实数据（字节数与 mtime 一致）",
        not drifted,
        "没有一行真实数据被改过" if not drifted else "被改了：" + "；".join(drifted),
    ))

    board = _scoreboard()
    dims = board["dimensions"]
    print(
        "记分现状：已验 %d / 未验 %d（%s）" % (
            board["total"], board["unverified_total"], "、".join(board["unverified"]) or "无",
        )
    )
    print("维度 " + " ".join(
        f"{k}={dims[k]['score']}{'✓' if dims[k]['verified'] else '✗'}" for k in sc.ORDER
    ))
    print(f"耗时 {_elapsed():.1f}s / 预算 {TOTAL_BUDGET:.0f}s")
    failed = [index + 1 for index, ok in enumerate(results) if not ok]
    if failed:
        print(f"FAIL 第 {'、'.join(str(item) for item in failed)} 条没成立")
        return 1
    print(f"PASS {len(results)} 条断言全部成立")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
