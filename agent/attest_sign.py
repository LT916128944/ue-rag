# agent/attest_sign.py
"""人一句话签：只读 pending 与证据文书，写一份人签。不碰文书、不碰哈希。

只读：
- `data/proofs/pending/<criterion_id>.json`：agent/proof_bundle.py 写的待签记录
  （criterion_id / evidence_path / evidence_sha1，person 与 signed_at 留空）；
- 它指向的证据 `.md`：按字节重算 sha1（调 `agent.manual_attest.file_sha1`，不另写一套），
  另外抽三条看点（sentence 三态、条款 passed 统计、窗口 failed 统计）。

只写：
- 带 `--yes` 时写 `data/attestations/<criterion_id>.json`：
  criterion_id / person / signed_at / evidence_path / evidence_sha1 五个字段
  （可选的 note 来自 `--note`）。写完后调 `agent.manual_attest._read_valid` 自检，
  读不过就提示人删掉并退 1。

为什么要有这一步：人不再写文书、不再算哈希（计划 §4.3）。人只签「我看过了」，
防伪靠抽查（计划 §4.4），不靠 40 位十六进制。`agent/manual_attest.py` 一行不改——
它的语义（哈希相符才算签）是对的，错的只是「让人去做文书」这件事。

退出码：0 签成了 / 1 拒绝（缺 pending、证据被改、写出的人签读不过）/ 2 等人加 --yes。

绝不写 .env、data/dsh/session_task.json、data/harness/feature_list.json 的 passed、
data/bench/audit.json。不联网、不调模型、不连编辑器。
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

from config import DATA_DIR
from agent.manual_attest import _read_valid, file_sha1
from agent.proof_bundle import md_points, safe_id

QUERY_LIMIT = 120
PENDING_KEYS = ("criterion_id", "evidence_path", "evidence_sha1")
SENTENCE_KEYS = ("sentence", "status", "query")


def _said(text) -> str:
    """看点里的值：空串写（无），表格里的（无）保持原样。"""
    value = str(text if text is not None else "").strip()
    return value or "（无）"


def highlights(md_text: str) -> list[str]:
    """三条看点。抽不到的那条写「看点摘要不可得」，不猜。"""
    points = md_points(md_text)
    sentence = points.get("sentence") or {}
    criteria = points.get("criteria") or {}
    windows = points.get("windows") or {}

    out: list[str] = []
    if all(key in sentence for key in SENTENCE_KEYS):
        parts = []
        for key in SENTENCE_KEYS:
            value = _said(sentence.get(key))
            if key == "query" and len(value) > QUERY_LIMIT:
                value = value[:QUERY_LIMIT] + "…"
            parts.append(f"{key}={value}")
        out.append("看点 1（sentence 三态）：" + "；".join(parts))
    else:
        out.append("看点 1（sentence 三态）：看点摘要不可得")

    total = int(criteria.get("total") or 0)
    if total > 0:
        out.append(f"看点 2（条款）：{int(criteria.get('passed') or 0)}/{total} 条条款 passed")
    else:
        out.append("看点 2（条款）：看点摘要不可得")

    window_total = int(windows.get("total") or 0)
    if window_total > 0:
        out.append(f"看点 3（窗口）：{int(windows.get('failed') or 0)} 个窗口 failed"
                   f"（共 {window_total} 个窗口）")
    else:
        out.append("看点 3（窗口）：看点摘要不可得")
    return out


def read_pending(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_attestation(target: Path, body: dict) -> None:
    """人签落盘：键序固定、UTF-8、末尾一个换行。"""
    text = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(
        description="读 pending、重算证据 sha1、打三条看点；加 --yes 才写人签。")
    parser.add_argument("--criterion", required=True,
                        help="基准句 id，例如 patrol-presentation")
    parser.add_argument("--person", default="", help="签名的人名，必填")
    parser.add_argument("--note", default="", help="可选的一句人话备注，写进人签的 note")
    parser.add_argument("--yes", action="store_true",
                        help="不带只展示，退 2；带了才算「我看过了」")
    # 自测用：把 data 根指到临时目录。正常用法不给。
    parser.add_argument("--data-root", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    cid = safe_id(args.criterion)
    if not cid:
        print(f"criterion 不合法：{args.criterion!r}（不能为空，也不能含 / \\ ..）")
        return 1
    person = str(args.person or "").strip()
    if not person:
        print('人签要写 --person "名字"：无名印不算签。')
        return 1

    root = Path(args.data_root) if args.data_root else Path(DATA_DIR)
    pending_path = root / "proofs" / "pending" / f"{cid}.json"
    pending = read_pending(pending_path)
    if pending is None:
        print(f"先跑 python agent\\proof_bundle.py --criterion {cid}")
        return 1

    missing = [key for key in PENDING_KEYS if not str(pending.get(key) or "").strip()]
    if missing:
        print(f"pending 不齐（缺 {missing}）：先跑 python agent\\proof_bundle.py --criterion {cid}")
        return 1

    evidence = Path(str(pending.get("evidence_path")).strip())
    claimed = str(pending.get("evidence_sha1") or "").strip().lower()
    if not evidence.is_file():
        print(f"证据文件不见了，拒绝签名：{evidence}")
        return 1
    digest = file_sha1(evidence).lower()
    if digest != claimed:
        print("证据被改过，拒绝签名")
        print(f"  pending 记的是：{claimed}")
        print(f"  文件现在算出来：{digest}")
        return 1

    try:
        md_text = evidence.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"证据读不出来，拒绝签名：{exc}")
        return 1

    print(f"证据：{evidence.resolve()}")
    print(f"sha1：{digest}")
    for line in highlights(md_text):
        print(line)

    if not args.yes:
        print("加 --yes 才算我看过了")
        return 2

    target = (root / "attestations" / f"{cid}.json").resolve()
    body = {
        "criterion_id": cid,
        "person": person,
        "signed_at": datetime.now().isoformat(timespec="seconds"),
        "evidence_path": str(pending.get("evidence_path")).strip(),
        "evidence_sha1": digest,
    }
    note = str(args.note or "").strip()
    if note:
        body["note"] = note
    write_attestation(target, body)

    record = _read_valid(target, cid, target.parent)
    if record is None:
        print("写出的人签自己也读不过（agent/manual_attest._read_valid 返回 None），这份不能作数。")
        print(f"请删掉：{target}")
        return 1
    print(f"人签：{target}")
    print(f"person={record['person']} signed_at={record['signed_at']} "
          f"evidence_sha1={record['evidence_sha1']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
