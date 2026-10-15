# pipelines/export_docs.py
"""把 documents 和 conventions 按二级标题切成 ue_doc，不收录 Plan/。"""
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CONVENTIONS_PATH, DOCS_DIR, DOCS_JSONL


def _kind_meta(path: Path) -> dict:
    kind = {
        "历史解决方案.md": "history",
        "排障条目.md": "debug",
    }.get(path.name)
    if not kind:
        return {}
    return {"doc_kind": kind}


def _chunks(path: Path, text: str) -> list[dict]:
    parts = re.split(r"(?m)^## ", text)
    records = []
    for part in parts[1:]:
        title, _, body = part.partition("\n")
        title = title.strip()
        content = f"## {title}\n{body.strip()}"
        if len(content) > 3000:
            content = content[:3000]
        rel = str(path)
        digest = hashlib.sha1(f"{rel}#{title}".encode("utf-8")).hexdigest()
        records.append({
            "id": f"doc:{digest}",
            "source_type": "ue_doc",
            "source_path": rel,
            "content": content,
            "metadata": {"title": title, "doc_path": rel, **_kind_meta(path)},
        })
    return records


def main() -> None:
    files = []
    if DOCS_DIR.is_dir():
        files.extend(sorted(DOCS_DIR.glob("*.md")))
    if CONVENTIONS_PATH.is_file():
        files.append(CONVENTIONS_PATH)
    records = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        records.extend(_chunks(path, text))
    DOCS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with DOCS_JSONL.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Exported {len(records)} doc chunks to {DOCS_JSONL}")


if __name__ == "__main__":
    main()
