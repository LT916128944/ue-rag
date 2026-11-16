# agent/manual_attest.py
"""人签只翻 evidence_kind=manual 的条款。

读 data/attestations/<criterion_id>.json。文件由人写。
本模块不调用模型，不读聊天文本，不改 feature_list.json 的 passed。
"""
import hashlib
import json
from pathlib import Path

_FIELDS = ("criterion_id", "person", "signed_at", "evidence_path", "evidence_sha1")


def file_sha1(path: Path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def _safe_id(criterion_id: str) -> str:
    text = str(criterion_id or "").strip()
    if not text or text in (".", "..") or "/" in text or "\\" in text or ".." in text:
        return ""
    return text


def _resolve_evidence(attest_dir: Path, raw: str) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = Path(attest_dir) / path
    return path


def _read_valid(path: Path, criterion_id: str, attest_dir: Path) -> dict | None:
    """字段齐、criterion_id 对得上、证据文件字节的 sha1 与印章一致才算一份人签。"""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    fields = {key: str(data.get(key) or "").strip() for key in _FIELDS}
    if any(not fields[key] for key in _FIELDS):
        return None
    if fields["criterion_id"] != criterion_id:
        return None
    evidence = _resolve_evidence(attest_dir, fields["evidence_path"])
    if evidence is None or not evidence.is_file():
        return None
    try:
        digest = file_sha1(evidence)
    except OSError:
        return None
    if digest.lower() != fields["evidence_sha1"].lower():
        return None
    fields["evidence_sha1"] = digest.lower()
    return fields


def apply_manual_attest(criteria: list | None, attest_dir) -> list[dict]:
    """只翻 manual。editor_assert 等条款即使目录里有同名 json 也不翻。
    无文件、字段缺、哈希不符：该条款保持 false。"""
    folder = Path(attest_dir)
    out: list[dict] = []
    for item in criteria or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        if str(row.get("evidence_kind") or "") != "manual":
            out.append(row)
            continue
        cid = _safe_id(str(row.get("id") or ""))
        record = _read_valid(folder / f"{cid}.json", cid, folder) if cid else None
        if record is None:
            row["passed"] = False
            row.pop("evidence_sha1", None)
        else:
            row["passed"] = True
            row["evidence_sha1"] = record["evidence_sha1"]
        out.append(row)
    return out
