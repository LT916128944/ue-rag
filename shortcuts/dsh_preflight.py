# shortcuts/dsh_preflight.py
"""DSH 会话预检：只读，不启动编辑器、不调模型。"""
from __future__ import annotations

import re
import socket
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
SKILLS = WORKSPACE / "AIAgentProject" / ".agents" / "skills"
TEMPLATE = ROOT / "dsh" / "cordis.patch.yml"
ENV = ROOT / ".env"
CHROMA = ROOT / "data" / "chroma"


def line(ok: bool, name: str, detail: str = "") -> tuple[bool, str]:
    mark = "ok" if ok else "miss"
    text = f"[{mark}] {name}" + (f" — {detail}" if detail else "")
    return ok, text


def check_venv() -> tuple[bool, str]:
    expected = ROOT / ".venv" / "Scripts" / "python.exe"
    return line(Path(sys.executable).resolve() == expected.resolve(), "venv", str(expected))


def check_env() -> tuple[bool, str]:
    return line(ENV.is_file(), ".env", "present" if ENV.is_file() else "missing")


def check_template() -> tuple[bool, str]:
    if not TEMPLATE.is_file():
        return line(False, "dsh template", "missing dsh/cordis.patch.yml")
    try:
        entries = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return line(False, "dsh template", f"yaml error: {exc}")
    if not isinstance(entries, list):
        return line(False, "dsh template", "not a list")
    rows: list[dict] = []
    for item in entries:
        if not isinstance(item, dict):
            return line(False, "dsh template", "entry not a mapping")
        if "insert" in item:
            inserted = item.get("insert")
            if not isinstance(inserted, list):
                return line(False, "dsh template", "insert not a list")
            rows.extend(row for row in inserted if isinstance(row, dict))
        else:
            rows.append(item)
    ids = [str((row or {}).get("id")) for row in rows]
    has_mcp = bool({"mcp-uerag", "mcp-ue-rag"} & set(ids))
    missing = {"skill-filesystem"} - set(ids)
    if not has_mcp:
        missing.add("mcp-uerag")
    return line(not missing, "dsh template", f"{len(ids)} rows" + (f", missing {sorted(missing)}" if missing else ""))


def check_skills() -> tuple[bool, str]:
    if not SKILLS.is_dir():
        return line(False, "skills", f"missing {SKILLS}")
    files = sorted(SKILLS.glob("*/SKILL.md"))
    bad = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        match = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
        if not match:
            bad.append(path.parent.name)
            continue
        try:
            data = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            bad.append(path.parent.name)
            continue
        if not data.get("name") or not data.get("description"):
            bad.append(path.parent.name)
    return line(len(files) >= 19 and not bad, "skills", f"{len(files)} found" + (f", bad {bad}" if bad else ""))


def check_chroma() -> tuple[bool, str]:
    if not CHROMA.is_dir():
        return line(False, "chroma", "missing data/chroma")
    try:
        sys.path.insert(0, str(ROOT))
        from core.rag_core import _get_collection  # noqa: PLC0415

        count = _get_collection().count()
    except Exception as exc:  # noqa: BLE001
        return line(False, "chroma", f"error: {exc}")
    return line(count > 0, "chroma", f"ue_assets={count}")


def check_mcp_port() -> tuple[bool, str]:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=1.0):
            return line(True, "UE MCP :8000", "reachable")
    except OSError:
        return line(False, "UE MCP :8000", "not reachable (editor closed?)")


def check_editor_process() -> tuple[bool, str]:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe"], capture_output=True, text=True, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return line(False, "UnrealEditor", f"tasklist failed: {exc}")
    return line("UnrealEditor.exe" in out.stdout, "UnrealEditor", "running" if "UnrealEditor.exe" in out.stdout else "not running")


def main() -> int:
    critical = [
        check_venv(),
        check_env(),
        check_template(),
        check_skills(),
        check_chroma(),
    ]
    informational = [check_mcp_port(), check_editor_process()]
    for _, text in critical + informational:
        print(text)
    failed = [text for ok, text in critical if not ok]
    if failed:
        print(f"preflight failed: {len(failed)} critical")
        return 1
    print("preflight ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
