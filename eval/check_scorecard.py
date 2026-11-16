# eval/check_scorecard.py
"""按 1425 第 3 节记分。每一分对应一个谓词。谓词为假就不加，并写下缺口。

不写 WORKFLOW.md，不改 feature_list.json。脚本自己存在不加分。

报分口径（20261111 W1-P1）：
- 每个维度多两个字段：verified（bool）与 basis（"evidence" / "baseline"）。
  basis 是「这一维现在算的分有没有证据」：谓词真的验过就是 evidence，
  只拿到 BASE 的底分就是 baseline。
- total 只加 verified 为真的维度；没有证据的那部分单独记在 unverified_total。
  A、B 没有 attestations / verdict 时 verified=false，不进 total。
- 终端打两行：已验 N / 未验 M（哪几维）。

P3（20261111）：缓存不再能顶替重跑。一个检查名只有同时出现在
.github/workflows/offline.yml 里，才允许读 data/harness/check_all.json 的退出码；
不在 workflow 里的检查当场重跑，并且拒绝读缓存那一项。
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORKFLOW = ROOT / ".github" / "workflows" / "offline.yml"
_RUN = re.compile(r"^\s*run:\s*python\s+(eval/check_[A-Za-z0-9_]+\.py)(?:\s+(\S.*?))?\s*$")
HEALTH_PORT = 8010
HEALTH_URL = f"http://127.0.0.1:{HEALTH_PORT}/api/health"
HEALTH_BOOT_SECONDS = 60.0
# 跑一条 evidence 检查的硬上限（秒）。超时按 124 算红，绝不无限等。
CHECK_TIMEOUT = 300.0

BASE = {
    "A": 7, "B": 16, "C": 13, "D": 10, "E": 10,
    "F": 9, "G": 8, "H": 6, "I": 2,
}
ORDER = ("A", "B", "C", "D", "E", "F", "G", "H", "I")
DONE = ("ok", "partial", "failed")
HEALTH_FIELDS = (
    "backend", "index_present", "circuit_closed", "checkpoint_writable", "disk_free_mb",
)
PREDICATE_CHECKS = {
    "D": ("check_orphan_run", "check_dsh_lock"),
    "C": ("check_criteria_binding", "check_harness"),
    "E": ("check_backend_coverage", "check_write_rollback"),
    "H": ("check_risk",),
    "G": ("check_distill",),
}
# 记分用到的全部 evidence 检查（去重保序）。断言要伪造一份「都通过」的缓存时按这个列。
ALL_CHECKS = tuple(dict.fromkeys(
    name for names in PREDICATE_CHECKS.values() for name in names
))


def _summary() -> dict[str, int]:
    raw = os.environ.get("UE_CHECK_ALL_SUMMARY", "").strip()
    if not raw:
        return {}
    path = Path(raw)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    found = {}
    for item in data.get("checks") or []:
        if isinstance(item, dict) and item.get("name"):
            found[str(item["name"])] = int(item.get("exit") or 0)
    return found


def _workflow_checks() -> set[str]:
    """offline.yml 里单行 `run: python eval/check_x.py …` 的检查名。读不到就是空集：
    读不到就不该相信缓存，所有检查当场重跑。"""
    try:
        lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    names: set[str] = set()
    for line in lines:
        match = _RUN.match(line)
        if match:
            names.add(Path(match.group(1)).stem)
    return names


def _run_check(name: str, cached: dict[str, int], in_ci: set[str]) -> tuple[int, str]:
    """缓存只在**显式**许可、且不改变结果时用。默认一律当场重跑。

    规则（20261111 W1 收口）：
    - 检查名不在 offline.yml 里 → 一律拒绝读缓存，当场重跑（P3 的硬要求）；
    - 默认（未设 UE_CHECK_ALL_TRUST_CACHE）→ 一律当场重跑，来源写「重跑」；
    - UE_CHECK_ALL_TRUST_CACHE=1 → 才允许对「在 CI 里且缓存说绿」的检查吃缓存
      （本地/CI 里 check_all 刚跑完、图快时用）。缓存说红永远是红，不给重跑洗白。

    默认不能是豁免：把「没验过的绿」当成分数，正是这份计划要治的老毛病，
    不能从 BASE 挪到 check_all.json 上重演。

    子进程一律：stdin=DEVNULL（读 stdin 会永远等）、硬超时、超时后杀进程树并 wait。
    绝不用 subprocess.run(capture_output=True)——它超时后 reader 线程还挂在管道上，
    主进程照旧等，CPU=0 挂死（现场实测）。
    """
    source = f"eval/{name}.py"
    trust = os.environ.get("UE_CHECK_ALL_TRUST_CACHE", "0").strip() == "1"
    if name in cached and name in in_ci:
        if cached[name] != 0:
            return cached[name], f"data/harness/check_all.json#{name}"
        if trust:
            return 0, f"data/harness/check_all.json#{name}"
    code, out = _spawn(
        [sys.executable, source],
        timeout=CHECK_TIMEOUT,
    )
    if name == "check_write_rollback" and code == 0 and "compensating" not in out:
        code = 1
    return code, f"{source}（重跑）"


def _spawn(cmd: list[str], *, timeout: float,
           env: dict[str, str] | None = None) -> tuple[int, str]:
    """跑一个子进程，带硬超时。返回 (退出码, 合并输出)。超时按 124 算红。

    stdout/stderr 直接重定向到临时文件，不建管道：没有 reader 线程可挂。
    超时之后杀进程树并 wait，不留孤儿。env 为 None 时继承父进程环境。
    """
    import tempfile

    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as sink:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc, kwargs.get("start_new_session", False))
            return 124, f"超时 {timeout:.0f}s 被终止"
        finally:
            if proc.poll() is None:
                _kill_tree(proc, kwargs.get("start_new_session", False))
        sink.seek(0)
        return code, sink.read()


def _kill_tree(proc: subprocess.Popen, new_session: bool) -> None:
    """杀掉子进程（Windows 上是整个进程树）。失败也不抛，最后再 wait 一次收尸。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def _group(names: tuple[str, ...], cached: dict[str, int], in_ci: set[str]) -> tuple[bool, str, list[str]]:
    evidence = []
    gaps = []
    for name in names:
        code, path = _run_check(name, cached, in_ci)
        evidence.append(path)
        if code != 0:
            gaps.append(f"{name} 退出码 {code}")
    return not gaps, "；".join(gaps), evidence


def _attest_passed() -> tuple[bool, str]:
    from agent.manual_attest import _read_valid, apply_manual_attest
    from config import DATA_DIR

    folder = DATA_DIR / "attestations"
    if not folder.is_dir():
        return False, "data/attestations"
    for path in sorted(folder.glob("*.json")):
        cid = path.stem
        if _read_valid(path, cid, folder) is None:
            continue
        flipped = apply_manual_attest(
            [{"id": cid, "evidence_kind": "manual", "passed": False}],
            folder,
        )
        if flipped and flipped[0].get("passed") is True:
            return True, str(path.relative_to(ROOT)).replace("\\", "/")
    return False, "data/attestations"


def _verdicts(root: Path | None = None) -> list[dict]:
    folder = (ROOT if root is None else root) / "data" / "archive"
    rows = []
    if not folder.is_dir():
        return rows
    for path in folder.glob("*/verdict.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def _false_pass(row: dict, signed: bool) -> bool:
    if row.get("false_pass") is True:
        return True
    if row.get("window_failed") and row.get("criteria_passed"):
        return True
    if row.get("status") == "ok" and row.get("manual_unsigned"):
        return True
    if row.get("status") == "ok" and row.get("existing_instance_property_as_ok") is True:
        return True
    if row.get("sentence_id") == "patrol-presentation" and row.get("status") == "ok" and not signed:
        return True
    return False


def _bench(signed: bool, base_root: Path | None = None) -> tuple[int, str, str, int]:
    where = ROOT if base_root is None else base_root
    manifest = json.loads((where / "eval" / "bench_manifest.json").read_text(encoding="utf-8"))
    ids = [str(item.get("id") or "") for item in manifest.get("sentences") or []]
    done = [
        row for row in _verdicts(base_root)
        if row.get("sentence_id") in ids and row.get("status") in DONE
    ]
    evidence = "data/archive"
    counts: dict[str, int] = {}
    for row in done:
        key = str(row.get("sentence_id") or "")
        counts[key] = counts.get(key, 0) + 1
    # 判词里的 sentence_id 认不出是基准句时，能算的只有真 machine ok 的那几条。
    ok_rows = [row for row in done if row.get("status") == "ok"]
    ok = sum(1 for row in ok_rows if row.get("evidence_kind") == "machine")
    if len(done) < 30 or any(counts.get(item, 0) < 3 for item in ids):
        return 0, "档案不足 10 句×3 轮", evidence, ok
    if any(_false_pass(row, signed) for row in done):
        return 0, "有假通过，B 不加分", evidence, ok
    rate = ok / len(done)
    if rate >= 0.9:
        return 3, "", evidence, ok
    if rate >= 0.8:
        return 2, "成功率在 80% 到 90% 之间，对外不能说 95", evidence, ok
    return 0, "成功率低于 80%", evidence, ok


def _cost_rows() -> list[dict]:
    from config import DATA_DIR

    path = DATA_DIR / "costs.jsonl"
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return rows
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _caller_names(rows: list[dict]) -> list[str]:
    names: list[str] = []
    for row in rows:
        name = str(row.get("caller") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def caller_split_ok(rows: list[dict]) -> tuple[bool, str]:
    """F 的判据，纯函数：至少两个不同 caller 各有一行，且 report 对这两个 caller 各打一处小计。

    只看传进来的 rows，不读文件、不写文件。真实数据里没有第二个 caller 就是未验，不记分；
    断言的「坏状态」由调用方把临时目录里的文件读成 rows 再喂进来。
    """
    return caller_split_detail(rows)[:2]


def caller_split_detail(rows: list[dict]) -> tuple[bool, str, list[str]]:
    """同 caller_split_ok，多返回「每个 caller 各打了多少行」。断言的细节就靠这个。"""
    from eval.report import caller_usage_lines

    names = _caller_names(rows)
    if len(names) < 2:
        return False, f"只有 {len(names)} 个 caller，成本没按两个 caller 分列", names
    text = "\n".join(caller_usage_lines(rows))
    titles = ["DSH 用量" if name == "dsh" else f"{name} 用量" for name in names]
    hit = sum(1 for title in titles if title in text)
    if hit < 2:
        return False, "成本没有按 caller 分列", names
    return True, "", names


def _caller_split() -> tuple[bool, str, str]:
    """F：只读真实 data/costs.jsonl，判据交给 caller_split_ok。这一维从不写任何文件。"""
    rows = _cost_rows()
    ok, gap = caller_split_ok(rows)
    return ok, gap, f"data/costs.jsonl（{len(rows)} 行）；eval/report.py"


def _probe_child(source: str, env_values: dict[str, str] | None) -> tuple[int, str]:
    """一次性子进程：环境由本函数给的 env_values 决定（None 表示继承父进程）。
    子进程只做这一件事，不读调用方的其它状态。返回 (退出码, 标准输出)。

    带 120 秒硬上限，超时按红（124）处理，不无限等。"""
    env = None
    if env_values is not None:
        env = dict(env_values)
        env.setdefault("PYTHONPATH", str(ROOT))
        env.setdefault("PYTHONIOENCODING", "utf-8")
    return _spawn([sys.executable, "-c", source], timeout=CHECK_TIMEOUT, env=env)


def sha256_file(path: Path) -> str:
    """一份文件的 sha256 十六进制小写。读不到就空字符串（调用方按不一致处理）。"""
    import hashlib

    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def venv_created_at(root: Path) -> float | None:
    """venv 的创建时间：pyvenv.cfg 与 venv 目录本身取更早的那个。
    两次下载/解压都能把 mtime 往前推，但创建时间不会比它们更晚，取小已是上界里最保守的。"""
    cfg = root / ".venv" / "pyvenv.cfg"
    folder = root / ".venv"
    stamps = []
    for path in (cfg, folder):
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            continue
    if not stamps:
        return None
    return min(stamps)


def _read_text(path: Path) -> str:
    """读成 str 再去行首 BOM。utf-8-sig 会静默吞掉 BOM，这里要看得到它。"""
    return path.read_text(encoding="utf-8").lstrip("\ufeff")


def _write_closed_env(folder: Path, values: list[tuple[str, str]]) -> Path:
    """一份封闭 .env（UTF-8 无 BOM）。"""
    path = folder / ".env"
    path.write_text(
        "".join(f"{key}={val}\n" for key, val in values),
        encoding="utf-8",
    )
    return path


def _base_env() -> dict[str, str]:
    """父进程环境的一份拷贝，去掉会改动探活字段的键（免得本机 .env 的开关漏进断言）。

    大小写敏感的 dict 拷贝挡不住 Windows：从 pwsh 继承来的 "ue_mcp_index_path" 与
    "UE_MCP_INDEX_PATH" 是两个键，os.environ 却按不区分大小写取到它。所以按大写名再筛一遍。
    """
    watched = {
        "UE_EDITOR_BACKEND", "UE_EXEC_MODE", "MCP_INDEX_PATH", "UE_DOTENV_PATH",
        "UE_DATA_DIR", "UE_MCP_INDEX_PATH", "UE_COSTS_PATH",
    }
    return {key: val for key, val in os.environ.items() if key.upper() not in watched}


_LAUNCH = """
import importlib, uvicorn
app = importlib.import_module({module!r}).app
uvicorn.run(app, host="127.0.0.1", port={port}, log_level="error")
"""


def _http_health(env: dict[str, str], port: int | None = None) -> dict | None:
    """进程外打一次真实 HTTP：自己起 uvicorn 子进程，跑完杀掉。

    硬上限：等端口最多 HEALTH_BOOT_SECONDS 秒，单次请求 timeout=5 秒。超时就返回 None
    （I 维判红并说「没连上」），**不无限等**。子进程一律 finally 里 kill + wait，不留孤儿。
    """
    port = HEALTH_PORT if port is None else int(port)
    if port < 1 or port > 65535:
        return None  # 故意给一个建不了监听的端口：这条路径就是用来验「起不来会红」
    proc = subprocess.Popen(
        [sys.executable, "-c", _LAUNCH.format(module="server.gateway", port=port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + HEALTH_BOOT_SECONDS
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return None
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=5,
                ) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.3)
                continue
            return body if isinstance(body, dict) else None
        return None
    finally:
        proc.kill()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass


def _health_ready() -> tuple[bool, str, str]:
    """I（健康）：进程外真打一次 /api/health，验字段形状，并验顶层 ok 由五项派生：
    至少一项 ok 为 false 时顶层 ok 必须跟着 false。返回 (是否过, 证据, 缺口)。"""
    import tempfile

    evidence = f"server/gateway.py#/api/health（127.0.0.1:{HEALTH_PORT} 真 HTTP）"
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        closed = _write_closed_env(folder, [
            ("UE_EDITOR_BACKEND", "official"),
            ("UE_EXEC_MODE", "mock"),
            ("UE_MCP_INDEX_PATH", str(folder / "no_index.json")),
            ("UE_MCP_URL", "http://127.0.0.1:8710/mcp"),
        ])
        env = _base_env()
        env["UE_DOTENV_PATH"] = str(closed)
        body = _http_health(env)
    if body is None:
        return False, evidence, "健康检查没连上（uvicorn 起不来或 /api/health 不通）"
    for key in ("ok", "model", "mcp_url", "mcp_up"):
        if key not in body:
            return False, evidence, "健康返回缺 ok/model/mcp_url/mcp_up"
    for key in HEALTH_FIELDS:
        item = body.get(key)
        if not isinstance(item, dict) or not isinstance(item.get("ok"), bool) or "reason" not in item:
            return False, evidence, f"探活字段 {key} 不是 {{ok, reason}}"
    if not isinstance(body.get("ok"), bool):
        return False, evidence, "顶层 ok 不是 bool"
    if body.get("index_present", {}).get("ok") is not False:
        return False, evidence, "假索引地址没能造出 ok=false，派生断言没验到"
    if body["ok"] is not False:
        return False, evidence, "有一项探活为 false，顶层 ok 仍是 true（写死的）"
    return True, evidence, ""


def _setup_ran(cached: dict[str, int], in_ci: set[str],
               harness: Path | None = None, venv_root: Path | None = None) -> tuple[bool, str, str]:
    """I（换机回执）。harness / venv_root 是本函数唯一的两个路径参数，都**只读**：
    默认走真实 data/harness 与仓库根的 .venv；断言要在临时目录里造一份「真通过」的回执时
    传进来，不必也不许去碰真实 data/harness/。"""
    from config import DATA_DIR

    data_dir = harness if harness is not None else DATA_DIR / "harness"
    repo = venv_root if venv_root is not None else ROOT
    receipt_path = data_dir / "setup_receipt.json"
    summary_path = data_dir / "check_all.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "换机脚本还没跑通", "shortcuts/setup.ps1"
    if not isinstance(receipt, dict) or receipt.get("ok") is not True or receipt.get("editor_seen") is not True:
        return False, "换机脚本还没在有编辑器的机器上跑通", "data/harness/setup_receipt.json"
    if not str(receipt.get("python") or "").startswith("3.12"):
        return False, "换机用的不是 Python 3.12", "data/harness/setup_receipt.json"
    if not summary_path.is_file():
        return False, "check_all 还没跑", "data/harness/check_all.json"
    recorded = str(receipt.get("check_all_sha256") or "").strip()
    if not recorded:
        return False, "回执里没有 check_all 的摘要哈希", "data/harness/setup_receipt.json"
    if sha256_file(summary_path) != recorded:
        return False, "回执哈希与现在的 check_all.json 不一致", "data/harness/setup_receipt.json"
    stamp = receipt.get("check_all_mtime")
    try:
        stamp_value = float(stamp)
    except (TypeError, ValueError):
        return False, "回执里没有 check_all 的修改时间", "data/harness/setup_receipt.json"
    if float(stamp_value) != summary_path.stat().st_mtime:
        return False, "回执记的 check_all 修改时间与现在那份不一致", "data/harness/setup_receipt.json"
    venv_at = venv_created_at(repo)
    if venv_at is None:
        return False, "找不到 .venv 的创建时间", "shortcuts/setup.ps1"
    if receipt_path.stat().st_mtime <= venv_at:
        return False, "回执不比 .venv 新（不是这台机器上建的 venv 跑出来的）", "data/harness/setup_receipt.json"
    if not cached:
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, "check_all 还没跑", "data/harness/check_all.json"
        cached = {
            str(item.get("name")): int(item.get("exit") or 0)
            for item in (data.get("checks") or [])
            if isinstance(item, dict) and item.get("name") and item.get("name") != "check_scorecard"
        }
    failed = [name for name, code in cached.items() if code != 0]
    if not cached or failed:
        names = "、".join(failed) if failed else "没有检查结果"
        return False, f"check_all 未全部通过：{names}", "data/harness/check_all.json"
    return True, "", (
        str(receipt_path) if harness is not None else "data/harness/setup_receipt.json"
    )


def _evidence_of(score: int, base: int, verified: bool) -> str:
    return "evidence" if verified else "baseline"


def _dims(base: dict[str, int] | None = None, root: Path | None = None, *,
          harness: Path | None = None, venv_root: Path | None = None,
          health_ready=None, setup_ran=None) -> dict[str, dict]:
    """按证据算每一维的 score / verified / basis / gap / evidence。纯函数，不写文件。

    base 缺省是 BASE。传进来的权重只影响「底分多少」，不影响 A、B 有没有证据。
    root 缺省是本仓库：换一个没有 verdict 的根，B 就没证据。
    harness / venv_root 透传给 _setup_ran（只读路径参数），断言拿它把 I 的 setup 半边
    造到临时目录里。
    health_ready / setup_ran 是可选替身，只为断言能把 I 的其中一半钉成「真通过」，
    好让另一半成为唯一变量；默认就是真函数，记分口径不受影响。
    """
    weights = dict(BASE) if base is None else {k: int(v) for k, v in base.items()}
    # 删掉的权重就按 0 算：A、B 被删掉时它们的底分必须是 0，否则「删了 BASE 总分还变」。
    weights = {key: weights.get(key, 0) for key in BASE}
    cached = _summary()
    in_ci = _workflow_checks()
    signed, attest_path = _attest_passed()
    dims: dict[str, dict] = {}

    dims["A"] = {
        "score": weights["A"] + (1 if signed else 0),
        "verified": signed,
        "gap": "" if signed else "没有哈希相符的人签",
        "evidence": attest_path,
    }

    bonus, gap, evidence, machine_ok = _bench(signed, root)
    # B 的底分只认「真 machine ok 的判词条数」。没有一条真 ok 就是未验，底分不进 total。
    dims["B"] = {
        "score": weights["B"] + bonus,
        "verified": bonus > 0 or machine_ok > 0,
        "gap": gap,
        "evidence": evidence,
    }

    for key, names in PREDICATE_CHECKS.items():
        ok, gap, paths = _group(names, cached, in_ci)
        dims[key] = {
            "score": weights[key] + (2 if ok and key in ("C", "D", "E") else 1 if ok else 0),
            "verified": ok,
            "gap": gap,
            "evidence": paths,
        }

    split_ok, split_gap, split_path = _caller_split()
    dims["F"] = {
        "score": weights["F"] + (1 if split_ok else 0),
        "verified": split_ok,
        "gap": split_gap,
        "evidence": split_path,
    }

    health_ok, health_path, health_gap = (health_ready or _health_ready)()
    setup_ok, setup_gap, setup_path = (setup_ran or _setup_ran)(
        cached, in_ci, harness, venv_root,
    )
    if health_ok and setup_ok:
        i_gap = ""
    elif not health_ok:
        i_gap = health_gap
    else:
        i_gap = setup_gap
    dims["I"] = {
        "score": weights["I"] + (2 if health_ok and setup_ok else 0),
        "verified": health_ok and setup_ok,
        "gap": i_gap,
        "evidence": [health_path, setup_path],
    }

    for key in ORDER:
        dims[key]["basis"] = _evidence_of(
            dims[key]["score"], weights[key], dims[key]["verified"],
        )
        # total 只加 verified 为真的维度。未验的那几维，分数留在 score 里给 unverified_total
        # 汇总，counted 一律是 0——A、B 与 F、I 一视同仁，底分不是证据。
        dims[key]["counted"] = dims[key]["score"] if dims[key]["verified"] else 0
    return dims


def _score(dims: dict[str, dict] | None = None, *, base: dict[str, int] | None = None,
           root: Path | None = None, harness: Path | None = None,
           venv_root: Path | None = None, health_ready=None, setup_ran=None) -> dict:
    """total 只加 verified 为真的维度；unverified_total 是「没验那几维一共有多少分还没到手」。

    两者相加永远不超过满分 100：counted 对未验维度是 0，unverified_total 取的是 score。
    """
    dims = _dims(base, root, harness=harness, venv_root=venv_root,
                 health_ready=health_ready, setup_ran=setup_ran) if dims is None else dims
    verified_keys = [key for key in ORDER if dims[key]["verified"]]
    unverified = [key for key in ORDER if not dims[key]["verified"]]
    return {
        "total": sum(dims[key]["counted"] for key in verified_keys),
        "unverified_total": sum(dims[key]["score"] for key in unverified),
        "unverified": unverified,
        "dimensions": {key: dims[key] for key in ORDER},
    }


def main() -> int:
    board = _score()
    target = ROOT / "data" / "harness" / "scorecard.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    gaps = [
        f"{key}:{board['dimensions'][key]['gap']}"
        for key in ORDER
        if board["dimensions"][key]["gap"]
    ]
    print(f"已验 {board['total']} / 未验 {board['unverified_total']}"
          f"（{'、'.join(board['unverified']) if board['unverified'] else '无'}）")
    print(f"缺口 {'；'.join(gaps) if gaps else '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
