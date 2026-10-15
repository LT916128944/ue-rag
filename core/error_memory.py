# core/error_memory.py
"""成功的重试才把错误签名写进 ue_errors.jsonl。"""
import hashlib
import json

from config import ERRORS_JSONL


def remember_fixed(errors: list[dict], tool_names: list[str]) -> int:
    if not errors or not tool_names:
        return 0
    ERRORS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if ERRORS_JSONL.is_file():
        for line in ERRORS_JSONL.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            existing[record["id"]] = record
    tools = ",".join(tool_names)
    written = 0
    for err in errors:
        message = str(err.get("message") or "")
        file_name = str(err.get("file") or "")
        digest = hashlib.sha1(message.encode("utf-8")).hexdigest()
        record_id = f"error:{digest}"
        existing[record_id] = {
            "id": record_id,
            "source_type": "ue_error",
            "source_path": file_name,
            "content": f"文件 {file_name}；错误 {message}；修复工具 {tools}",
            "metadata": {"file": file_name, "tools": tools},
        }
        written += 1
    with ERRORS_JSONL.open("w", encoding="utf-8") as handle:
        for record in existing.values():
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return written
