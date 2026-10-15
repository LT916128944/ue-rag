import os
import re
import json
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import UE_SOURCE_ROOT, CPP_JSONL

SOURCE_ROOT = str(UE_SOURCE_ROOT)
OUTPUT_PATH = str(CPP_JSONL)

_CLASS_HEAD = re.compile(
    r"class\s+(?:(?P<api>\w+_API)\s+)?(?P<name>\w+)"
    r"(?:\s*:\s*public\s+(?P<parent>[\w:]+))?",
)


def iter_source_files(root):
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith((".h", ".cpp")):
                yield os.path.join(dirpath, fn)


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _close_paren(text, open_at):
    depth = 0
    for index in range(open_at, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def extract_classes(text):
    """每个 UCLASS 一块。括号配平，和 class 之间可以换行。再补上没被 UCLASS 扫到的模块 API 类。"""
    found = []
    seen = set()
    for match in re.finditer(r"\bUCLASS\s*\(", text):
        end = _close_paren(text, match.end() - 1)
        if end is None:
            continue
        head = _CLASS_HEAD.match(text[end:])
        if not head:
            continue
        name = head.group("name")
        if name in seen:
            continue
        seen.add(name)
        parent = head.group("parent") or ""
        found.append({"name": name, "parent": parent.split("::")[-1], "at": end})
    for match in re.finditer(r"class\s+\w+_API\s+(\w+)(?:\s*:\s*public\s+([\w:]+))?", text):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        parent = match.group(2) or ""
        found.append({"name": name, "parent": parent.split("::")[-1], "at": match.start()})
    found.sort(key=lambda item: item["at"])
    for index, item in enumerate(found):
        nxt = found[index + 1]["at"] if index + 1 < len(found) else len(text)
        item["body"] = text[item["at"]:nxt]
    return found


def extract_uclass_comment(text):
    m = re.search(r"/\*\*(.*?)\*/\s*UCLASS", text, re.S)
    if m:
        return " ".join(line.strip(" *\t") for line in m.group(1).splitlines() if line.strip())
    return ""


def extract_members(text):
    props = re.findall(r"UPROPERTY\s*\(([^)]*)\)\s*[\r\n]+\s*([\w:<>\s\*&]+?)\s+(\w+)\s*;", text, re.S)
    funcs = re.findall(r"UFUNCTION\s*\(([^)]*)\)\s*[\r\n]+\s*([\w:<>\s\*&]+?)\s+(\w+)\s*\(", text, re.S)
    return props, funcs


def extract_doc_comments(text):
    comments = re.findall(r"//\s*(.+)", text)
    return [c.strip() for c in comments if c.strip()]


def build_records(path):
    text = read_text(path)
    classes = extract_classes(text)
    if not classes:
        return []
    records = []
    for item in classes:
        body = item["body"]
        class_comment = extract_uclass_comment(body) or extract_uclass_comment(text)
        props, funcs = extract_members(body)
        doc_comments = extract_doc_comments(body)
        content_parts = [
            f"C++ 类 {item['name']}",
            f"父类 {item['parent']}" if item["parent"] else "",
            f"文件 {os.path.basename(path)}",
        ]
        if class_comment:
            content_parts.append(f"说明: {class_comment}")
        for _, ptype, pname in props:
            content_parts.append(f"属性 {pname} ({ptype.strip()})")
        for _, ftype, fname in funcs:
            content_parts.append(f"函数 {fname}")
        for comment in doc_comments[:10]:
            content_parts.append(comment)
        content = "；".join([part for part in content_parts if part])
        records.append({
            "id": f"cpp:{item['name']}",
            "source_type": "ue_cpp",
            "source_path": path,
            "content": content,
            "metadata": {
                "class_name": item["name"],
                "parent_class": item["parent"],
                "file_name": os.path.basename(path),
                "file_path": path,
                "property_count": str(len(props)),
                "function_count": str(len(funcs)),
                "content_hash": hashlib.sha1(content.encode("utf-8")).hexdigest(),
            },
        })
    return records


def main():
    by_id = {}
    for path in iter_source_files(SOURCE_ROOT):
        for record in build_records(path):
            old = by_id.get(record["id"])
            if old and str(old["metadata"].get("file_name", "")).endswith(".h") and record["metadata"]["file_name"].endswith(".cpp"):
                continue
            by_id[record["id"]] = record
    records = list(by_id.values())
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Exported {len(records)} C++ records to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
