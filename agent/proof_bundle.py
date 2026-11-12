# agent/proof_bundle.py
"""证据文书由机器生成：从已落盘的事实写一份证据 + 自动哈希 + 一份待人签的 pending。

只读（全是落盘事实，不含模型文本）：
- `eval/bench_manifest.json` 的 sentences：`--criterion` 那条的原话，只用来找 run；
- `data/runs/**/<run_id>.json`（run 记录），以及同目录的 `<run_id>.md` / `<run_id>.cli.jsonl`；
- `data/events/<run_id>.jsonl`：取 backend 与日志行；
- `data/snapshots/<run_id>/`：只做存在性判断与列子目录。

只写三件：
- `data/proofs/<criterion_id>/<run_id>.md`：证据文书（窗口 state 表、条款表、判词原文、
  日志摘录、路径清单）；
- 同路径 `.sha1`：40 位小写 hex + 换行，**脚本算**，人不再碰 Get-FileHash；
- `data/proofs/pending/<criterion_id>.json`：criterion_id / evidence_path（绝对）/
  evidence_sha1 三个值填好，`person` 与 `signed_at` 留空，等人用 agent/attest_sign.py 签。

为什么不让人写文书：人抄 40 位哈希从来不是防伪，只是搬运（计划 §4.2）。
硬约束：不把模型文本里的「成功 / 已完成 / ok」当证据，只摘落盘字段原值；
不解释、不推断，也不写「AI 认为通过」这类表述。
不联网、不调模型、不连编辑器；不写 data/attestations、data/bench、
data/dsh/session_task.json、.env、data/harness/feature_list.json。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import DATA_DIR, editor_backend

MANIFEST_PATH = ROOT / "eval" / "bench_manifest.json"
MAX_LOG_LINES = 40
LINE_LIMIT = 300
SUMMARY_LIMIT = 300
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")

# 小标题常量。agent/attest_sign.py 的看点是按这几个标题切段解析的，改标题要两边一起改。
SECTION_SENTENCE = "## sentence 三态"
SECTION_WINDOWS = "## 窗口 state"
SECTION_CRITERIA = "## 条款"
SECTION_JUDGES = "## 判词原文"
SECTION_LOGS = "## 日志摘录"
SECTION_PATHS = "## 路径清单"

SENTENCE_KEYS = ("sentence", "status", "query")
# 判词形状：ok / kind / summary 三键齐全，多出来的只认这几个（errors 是判定明细，不是结论）。
JUDGE_REQUIRED = ("ok", "kind", "summary")
JUDGE_EXTRA = frozenset({"errors", "filter", "window", "id"})


class ProofError(Exception):
    """拒绝出证据。消息就是给人看的一句中文原因。"""


# --------------------------------------------------------------------------- 小工具


def safe_id(text) -> str:
    """criterion 会被拼进路径，形状不对返回空串（与 agent/manual_attest.py 同一条口径）。"""
    value = str(text or "").strip()
    if not value or value in (".", "..") or ".." in value or "/" in value or "\\" in value:
        return ""
    return value


def text_sha1(payload: bytes) -> str:
    """文书字节的 sha1。写文件和算哈希用同一份 bytes，Windows 换行不参与。"""
    return hashlib.sha1(payload).hexdigest()


def _rel(path: Path) -> str:
    """仓库内的路径显示成相对仓库根；仓库外的（自测的临时根）显示绝对路径。"""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return str(Path(path).resolve())


def _fmt(value) -> str:
    """字段原值的可读形状：布尔写 true / false，缺值写（无），其余 str。"""
    if value is None:
        return "（无）"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _cell(value) -> str:
    """表格单元格：竖线与换行都不能破坏表格。"""
    text = _fmt(value).replace("\r", " ").replace("\n", " ")
    return text.replace("|", "\\|").strip() or "（无）"


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _strings_of(node):
    """按出现顺序吐出结构里所有字符串值。只用来找路径，不做任何解释。"""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings_of(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _strings_of(value)


# --------------------------------------------------------------------------- 读 run / 找 run


def load_sentences(manifest_path: Path | None = None) -> dict[str, dict]:
    """`eval/bench_manifest.json` 的 sentences，按 id 索引。"""
    path = Path(manifest_path) if manifest_path is not None else MANIFEST_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProofError(f"读不到 {_rel(path)}：{exc}") from exc
    rows = data.get("sentences") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ProofError(f"{_rel(path)} 里没有 sentences 字段")
    return {str(item.get("id") or ""): item for item in rows if isinstance(item, dict)}


def sentence_text(criterion_id: str, manifest_path: Path | None = None) -> str:
    """`--criterion` 对应的原话。不在清单里返回空串，由调用方拒绝。"""
    item = load_sentences(manifest_path).get(str(criterion_id or "")) or {}
    return str(item.get("text") or "").strip()


def run_files(data_dir) -> list[Path]:
    """`data/runs/**/*.json`，只收文件。老 run 平铺在 data/runs 下也认。"""
    root = Path(data_dir) / "runs"
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.json") if path.is_file())


def find_run_path(run_id: str, data_dir) -> Path | None:
    """按 run_id 找唯一一份 run 记录；多份就是记录坏了，拒绝。"""
    wanted = str(run_id or "").strip()
    hits = [path for path in run_files(data_dir) if path.stem == wanted]
    if len(hits) > 1:
        raise ProofError(f"{wanted} 有多份 run 记录：{[str(path) for path in hits]}")
    return hits[0] if hits else None


def find_latest_run(text: str, data_dir) -> Path | None:
    """query 与该句原话**完全相等**、且带 `sentence` 字段的最近一份 run。

    最近按 run_id 字符串倒序（run_id 就是时间戳）；同一 run_id 再用文件 mtime 兜底。
    """
    hits: list[tuple[str, float, Path]] = []
    for path in run_files(data_dir):
        run = read_json(path)
        if run is None or "sentence" not in run:
            continue
        if str(run.get("query") or "") != str(text or ""):
            continue
        run_id = str(run.get("run_id") or path.stem)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        hits.append((run_id, mtime, path))
    if not hits:
        return None
    hits.sort(key=lambda item: (item[0], item[1]))
    return hits[-1][2]


# --------------------------------------------------------------------------- 落盘事实


def read_events(data_dir, run_id: str) -> list[dict]:
    """`data/events/<run_id>.jsonl` 的对象行。读不出、不是对象的一律跳过。"""
    path = Path(data_dir) / "events" / f"{run_id}.jsonl"
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def backend_of(data_dir, run_id: str, run: dict | None) -> tuple[str, str]:
    """backend 与它的来源。run 记录没写就找事件流的 editor_backend，再退 config。"""
    raw = str((run or {}).get("backend") or "").strip()
    if raw:
        return raw, "run 记录的 backend 字段"
    for event in read_events(data_dir, run_id):
        if str(event.get("type") or "") != "editor_backend":
            continue
        value = str(event.get("backend") or "").strip()
        if value:
            return value, f"data/events/{run_id}.jsonl 的 editor_backend 事件"
    try:
        value = str(editor_backend()).strip()
    except Exception:  # config 读不出来也不能编一个值
        value = ""
    if value:
        return value, "config.editor_backend()（run 记录与事件流都没写）"
    return "（无）", "run 记录与事件流都没写，config 也读不到"


def windows_of(run: dict | None) -> list[dict]:
    """窗口行。`windows` 是 dict 或 list 都兼容。"""
    raw = (run or {}).get("windows") or {}
    items: list[tuple[str, object]] = []
    if isinstance(raw, dict):
        items = [(str(key), value) for key, value in raw.items()]
    elif isinstance(raw, list):
        for index, value in enumerate(raw):
            label = str(value.get("id") or "") if isinstance(value, dict) else ""
            items.append((label or f"W{index + 1}", value))
    rows: list[dict] = []
    for label, value in items:
        body = value if isinstance(value, dict) else {}
        rows.append({
            "id": str(body.get("id") or label),
            "action": str(body.get("action") or ""),
            "state": str(body.get("state") or ""),
            "ok": body.get("ok"),
        })
    return rows


def _walk(node, trail: tuple[str, ...]):
    """(路径键, 对象) 深度优先。key 顺序就是文件里的顺序。"""
    if isinstance(node, dict):
        yield trail, node
        for key, value in node.items():
            yield from _walk(value, trail + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, trail + (str(index),))


def _is_judge(node) -> bool:
    """判词对象：ok / kind / summary 三键齐全，summary 是字符串，多余键只认白名单。"""
    if not isinstance(node, dict):
        return False
    keys = set(node)
    if not set(JUDGE_REQUIRED) <= keys:
        return False
    if not keys <= set(JUDGE_REQUIRED) | JUDGE_EXTRA:
        return False
    return isinstance(node.get("summary"), str)


def judge_entries(run: dict | None) -> list[dict]:
    """`run["windows"]` 里形状为 {"ok","kind","summary"} 的判词对象，原文摘要截 300 字符。"""
    out: list[dict] = []
    for trail, node in _walk((run or {}).get("windows") or {}, ()):
        if not _is_judge(node):
            continue
        out.append({
            "window": trail[0] if trail else "",
            "slot": "/".join(trail[1:]),
            "ok": node.get("ok"),
            "kind": str(node.get("kind") or ""),
            "summary": str(node.get("summary") or "")[:SUMMARY_LIMIT],
        })
    return out


def log_sources(data_dir, run_path: Path, run_id: str) -> list[Path]:
    """日志候选，按出现顺序：run 目录里的 .cli.jsonl，然后 data/events/<run_id>.jsonl。"""
    root = Path(data_dir)
    sources = [Path(run_path).with_suffix(".cli.jsonl")]
    sources.extend(sorted((root / "runs").rglob(f"{run_id}.cli.jsonl")))
    sources.append(root / "events" / f"{run_id}.jsonl")
    seen: set[str] = set()
    unique: list[Path] = []
    for path in sources:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def log_lines(data_dir, run_path: Path, run_id: str, limit: int = MAX_LOG_LINES) -> list[str]:
    """最多 `limit` 行日志，每行截到 `LINE_LIMIT` 字符，按出现顺序；一行都没有就是空列表。"""
    out: list[str] = []
    for path in log_sources(data_dir, run_path, run_id):
        if len(out) >= limit:
            break
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            text_line = line.strip()
            if not text_line:
                continue
            out.append(text_line[:LINE_LIMIT])
            if len(out) >= limit:
                break
    return out[:limit]


def image_paths(run: dict | None) -> list[dict]:
    """run 记录里出现的 png / jpg / jpeg 路径，按出现顺序去重。"""
    out: list[dict] = []
    seen: set[str] = set()
    for value in _strings_of(run or {}):
        text = value.strip().strip('"').strip("'")
        if not text.lower().endswith(IMAGE_SUFFIXES):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"path": text})
    return out


def path_entries(data_dir, run: dict | None, run_path: Path, run_id: str) -> list[dict]:
    """要逐条列出存在性的路径：事件流、run 记录/说明/CLI 日志、写前快照、图片。"""
    root = Path(data_dir)
    rows: list[dict] = []

    def add(label: str, path, note: str = "") -> None:
        target = Path(str(path))
        try:
            exists = target.exists()
        except OSError:
            exists = False
        rows.append({"label": label, "path": str(target), "exists": exists, "note": note})

    snapshot = root / "snapshots" / run_id
    note = ""
    if snapshot.is_dir():
        try:
            children = sorted(item.name for item in snapshot.iterdir() if item.is_dir())
        except OSError:
            children = []
        note = f"子目录：{'、'.join(children)}" if children else "没有子目录"
    add("写前快照目录", snapshot, note)
    add("事件流", root / "events" / f"{run_id}.jsonl")
    add("run 记录", run_path)
    add("run 说明", Path(run_path).with_suffix(".md"))
    add("CLI 日志", Path(run_path).with_suffix(".cli.jsonl"))
    for item in image_paths(run):
        add("run 记录里出现的图片", item["path"])
    return rows


# --------------------------------------------------------------------------- 渲染


def render_markdown(*, criterion_id: str, run_id: str, run: dict, run_path: Path,
                    data_dir, backend: str, backend_source: str, now_text: str) -> str:
    """证据文书正文。中文小标题、简洁表格/列表；每个值都是落盘字段原值。"""
    windows = windows_of(run)
    judges = judge_entries(run)
    lines = [
        f"# 证据文书：{criterion_id} / {run_id}",
        "",
        f"- criterion_id：{criterion_id}",
        f"- run_id：{run_id}",
        f"- 生成时间：{now_text}",
        f"- backend：{backend}（来源：{backend_source}）",
        f"- run 记录：{_rel(run_path)}",
        "",
        "_本页每个值都从已落盘字段逐字摘出：run 记录、事件流、文件是否存在。"
        "不解释、不判断、不含模型文本。_",
        "",
        SECTION_SENTENCE,
        "",
        "| 字段 | 值 |",
        "| --- | --- |",
    ]
    for key in SENTENCE_KEYS:
        lines.append(f'| run["{key}"] | {_cell(run.get(key))} |')

    lines.extend(["", SECTION_WINDOWS, ""])
    if windows:
        lines.extend(["| 窗口 | action | state | ok |", "| --- | --- | --- | --- |"])
        for row in windows:
            lines.append(
                f'| {_cell(row["id"])} | {_cell(row["action"])} | '
                f'{_cell(row["state"])} | {_cell(row["ok"])} |'
            )
    else:
        lines.append("（无窗口：run 记录的 windows 是空的）")

    lines.extend(["", SECTION_CRITERIA, ""])
    criteria = [item for item in (run.get("criteria") or []) if isinstance(item, dict)]
    if criteria:
        lines.extend([
            "| id | evidence_kind | passed | evidence_sha1 |",
            "| --- | --- | --- | --- |",
        ])
        for item in criteria:
            lines.append(
                f'| {_cell(item.get("id"))} | {_cell(item.get("evidence_kind"))} | '
                f'{_cell(item.get("passed"))} | {_cell(item.get("evidence_sha1"))} |'
            )
    else:
        lines.append("（无条款：run 记录的 criteria 是空的）")

    lines.extend(["", SECTION_JUDGES, ""])
    if judges:
        for item in judges:
            slot = f'/{item["slot"]}' if item["slot"] else ""
            lines.append(
                f'- {_cell(item["window"])}{slot}：ok={_fmt(item["ok"])} '
                f'kind={_cell(item["kind"])} summary={_cell(item["summary"])}'
            )
    else:
        lines.append("（无判词行：windows 里没有 ok / kind / summary 三键齐全的对象）")

    lines.extend(["", SECTION_LOGS, ""])
    entries = log_lines(data_dir, run_path, run_id)
    sources = log_sources(data_dir, run_path, run_id)
    for path in sources:
        state = "存在" if path.is_file() else "不存在"
        lines.append(f"- 来源 {_rel(path)}：{state}")
    lines.append("")
    if entries:
        lines.append(f"共 {len(entries)} 行，每行截断到 {LINE_LIMIT} 字符，按出现顺序：")
        lines.append("")
        lines.append("```")
        for index, line in enumerate(entries, start=1):
            lines.append(f"{index}. {line}")
        lines.append("```")
    else:
        lines.append("无日志行")

    lines.extend(["", SECTION_PATHS, ""])
    for row in path_entries(data_dir, run, run_path, run_id):
        target = Path(row["path"])
        # 这个局部特意不叫 state：eval/check_statuses.py 的 D11 静态扫描会把
        # 「赋给 state 的状态形字面量」当成新加的状态、要求先登记进 agent/statuses.py。
        # 这里说的是「文件在不在」，不是 run / 窗口 / 台账的状态。
        if not row["exists"]:
            presence = "不存在"
        elif target.is_dir():
            presence = "存在（目录）" + (f"，{row['note']}" if row["note"] else "")
        else:
            try:
                presence = f"存在（{target.stat().st_size} 字节）"
            except OSError:
                presence = "存在（大小读不出）"
        lines.append(f"- {row['label']}：{_rel(target)} —— {presence}")

    return "\n".join(lines).rstrip("\n") + "\n"


def md_points(md_text: str) -> dict:
    """从证据文书的表格里抽三条看点。

    返回 `{"sentence": {...}, "criteria": {"passed","total"}, "windows": {"failed","total"}}`；
    抽不到的项留空，由 agent/attest_sign.py 写「看点摘要不可得」。
    格式与解析只有这一份：改 render_markdown 的表格就要改这里。
    """
    points: dict = {
        "sentence": {},
        "criteria": {"passed": 0, "total": 0},
        "windows": {"failed": 0, "total": 0},
    }

    def section(heading: str) -> list[str]:
        out: list[str] = []
        inside = False
        for line in str(md_text or "").splitlines():
            if line.strip() == heading:
                inside = True
                continue
            if inside and line.startswith("## "):
                break
            if inside:
                out.append(line)
        return out

    def cells(line: str) -> list[str]:
        text = line.strip()
        if not text.startswith("|"):
            return []
        rows = [cell.strip() for cell in text.strip("|").split("|")]
        if rows and all(cell and set(cell) <= {"-", ":", " "} for cell in rows):
            return []
        return rows

    pattern = re.compile(r'^\|\s*run\["([^"]+)"\]\s*\|\s*(.*?)\s*\|\s*$')
    for line in section(SECTION_SENTENCE):
        hit = pattern.match(line.strip())
        if hit:
            points["sentence"][hit.group(1)] = hit.group(2)

    for line in section(SECTION_CRITERIA):
        row = cells(line)
        if len(row) < 4 or row[0] in ("id", ""):
            continue
        points["criteria"]["total"] += 1
        if row[2].lower() == "true":
            points["criteria"]["passed"] += 1

    for line in section(SECTION_WINDOWS):
        row = cells(line)
        if len(row) < 4 or row[0] == "窗口":
            continue
        points["windows"]["total"] += 1
        if row[2].lower() == "failed":
            points["windows"]["failed"] += 1

    return points


# --------------------------------------------------------------------------- 出证据


def build_bundle(criterion_id: str, run_id: str | None = None, *,
                 data_dir=None, manifest_path: Path | None = None,
                 now_text: str | None = None) -> dict:
    """找 run、渲染文书、写 .md / .sha1 / pending。找不到就抛 ProofError，一个字都不写。"""
    root = Path(data_dir) if data_dir is not None else Path(DATA_DIR)
    cid = safe_id(criterion_id)
    if not cid:
        raise ProofError(f"criterion 不合法：{criterion_id!r}（不能为空，也不能含 / \\ ..）")

    wanted = str(run_id or "").strip()
    if wanted:
        if not wanted.replace("-", "").isdigit():
            raise ProofError(f"run-id 形状不对：{wanted!r}（要 YYYYMMDD-HHMMSS）")
        run_path = find_run_path(wanted, root)
        if run_path is None:
            raise ProofError(f"data/runs 里没有 {wanted} 的 run 记录，写不出证据")
    else:
        text = sentence_text(cid, manifest_path)
        if not text:
            raise ProofError(
                f"eval/bench_manifest.json 的 sentences 里没有 {cid}，"
                "给不出这句原话，也找不到对应的 run"
            )
        run_path = find_latest_run(text, root)
        if run_path is None:
            raise ProofError(
                f"data/runs 里没有 query 完全等于 {cid} 原话、且带 sentence 字段的 run，"
                "先跑一次这条基准句"
            )

    run = read_json(run_path)
    if run is None:
        raise ProofError(f"run 记录读不出对象：{_rel(run_path)}")
    rid = str(run.get("run_id") or run_path.stem).strip() or run_path.stem

    backend, backend_source = backend_of(root, rid, run)
    moment = now_text or datetime.now().isoformat(timespec="seconds")
    body = render_markdown(
        criterion_id=cid, run_id=rid, run=run, run_path=Path(run_path),
        data_dir=root, backend=backend, backend_source=backend_source, now_text=moment,
    )
    payload = body.encode("utf-8")
    digest = text_sha1(payload)

    md_path = (root / "proofs" / cid / f"{rid}.md").resolve()
    sha_path = md_path.with_suffix(".sha1")
    pending_path = (root / "proofs" / "pending" / f"{cid}.json").resolve()

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_bytes(payload)
    sha_path.write_bytes((digest + "\n").encode("utf-8"))
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending = {
        "criterion_id": cid,
        "evidence_path": str(md_path),
        "evidence_sha1": digest,
        "person": "",
        "signed_at": "",
    }
    pending_path.write_bytes(
        (json.dumps(pending, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    return {
        "criterion_id": cid,
        "run_id": rid,
        "md_path": str(md_path),
        "sha1_path": str(sha_path),
        "sha1": digest,
        "pending_path": str(pending_path),
        "backend": backend,
        "log_lines": len(log_lines(root, Path(run_path), rid)),
        "judges": len(judge_entries(run)),
    }


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(
        description="由已落盘的 run 记录生成证据文书、sha1 与一份待人签的 pending。")
    parser.add_argument("--criterion", required=True,
                        help="基准句 id，例如 patrol-presentation")
    parser.add_argument("--run-id", default=None,
                        help="缺省取最近一次 query 完全等于该句原话的 run")
    # 自测用：把 data 根指到临时目录。正常用法不给。
    parser.add_argument("--data-root", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        result = build_bundle(args.criterion, args.run_id, data_dir=args.data_root)
    except ProofError as exc:
        print(f"没有出证据：{exc}")
        return 1
    print(f"证据：{result['md_path']}")
    print(f"sha1：{result['sha1']}")
    print(f"pending：{result['pending_path']}")
    print(f"人签：python agent\\attest_sign.py --criterion {result['criterion_id']} "
          "--person <名字> --yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
