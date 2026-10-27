# eval/check_dsh_config.py
"""离线校验 DSH 配置模板：不连 DSH、不连编辑器、不调模型。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "dsh" / "cordis.patch.yml"
WORKSPACE = ROOT.parents[1]
SKILLS = WORKSPACE / "AIAgentProject" / ".agents" / "skills"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def fail(message: str) -> None:
    print(f"dsh-config failed: {message}")
    raise SystemExit(1)


def skill_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def flatten_entries(entries: list) -> list[tuple[dict, bool]]:
    """按 DSH patch 方言展开：`- insert: [rows]` 是新行；裸行是按 id 覆盖 base 行。"""
    rows: list[tuple[dict, bool]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            fail("entry must be a mapping")
        if "insert" in entry:
            inserted = entry.get("insert")
            if not isinstance(inserted, list) or not inserted:
                fail("insert must be a non-empty list")
            for row in inserted:
                if not isinstance(row, dict):
                    fail("insert row must be a mapping")
                rows.append((row, True))
        else:
            rows.append((entry, False))
    return rows


def main() -> int:
    if not TEMPLATE.is_file():
        fail(f"missing {TEMPLATE}")
    try:
        entries = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        fail(f"yaml error: {exc}")
    if not isinstance(entries, list) or not entries:
        fail("template must be a non-empty YAML list")

    rows = flatten_entries(entries)
    ids: set[str] = set()
    server_names: set[str] = set()
    for entry, from_insert in rows:
        entry_id = str(entry.get("id") or "")
        if not entry_id or entry_id in ids:
            fail(f"entry id missing/duplicate: {entry_id!r}")
        ids.add(entry_id)
        name = entry.get("name")
        config = entry.get("config") or {}
        if not isinstance(config, dict):
            fail(f"{entry_id}: config must be a mapping")
        if name == "@deepseek-ai/dsh-mcp-client":
            if entry_id == "mcp-uerag" and not from_insert:
                fail("mcp-uerag 是本项目新增行，必须放在 insert 列表里，否则 DSH 按 id 找不到会跳过")
            server = str(config.get("serverName") or "")
            if not ID_RE.fullmatch(server):
                fail(f"{entry_id}: bad serverName {server!r}")
            if server in server_names:
                fail(f"duplicate serverName {server!r}")
            server_names.add(server)
            transport = config.get("transport")
            if transport not in ("stdio", "streamable-http"):
                fail(f"{entry_id}: bad transport {transport!r}")
            if transport == "stdio":
                for key in ("command", "args", "cwd"):
                    if not config.get(key):
                        fail(f"{entry_id}: stdio missing {key}")
                command = Path(str(config["command"]))
                if not command.is_file():
                    fail(f"{entry_id}: command not found: {command}")
                cwd = Path(str(config["cwd"]))
                if not cwd.is_dir():
                    fail(f"{entry_id}: cwd not found: {cwd}")
                for arg in config["args"]:
                    path = Path(str(arg))
                    if not path.is_absolute():
                        path = cwd / path
                    if not path.exists():
                        fail(f"{entry_id}: arg not found: {path}")
                joined = " ".join(str(item) for item in config["args"])
                if "ue_mcp_index" in joined or "unreal" in joined.lower():
                    fail(f"{entry_id}: refusing full UE tool index in DSH")
    if not server_names:
        fail("no mcp-client entry found")

    skill_root = Path(str(next(
        (entry.get("config") or {}).get("customSkillDirs", [""])[0]
        for entry, _inserted in rows
        if entry.get("id") == "skill-filesystem"
    ) or ""))
    if not skill_root.is_dir():
        fail(f"skill root not found: {skill_root}")
    skills = sorted(skill_root.glob("*/SKILL.md"))
    if len(skills) < 19:
        fail(f"expected >=19 skills under {skill_root}, found {len(skills)}")
    for path in skills:
        data = skill_frontmatter(path)
        name = str(data.get("name") or "")
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
            fail(f"{path.parent.name}: bad skill name {name!r}")
        if not str(data.get("description") or "").strip():
            fail(f"{path.parent.name}: missing description")
        if len(str(data.get("description"))) > 500:
            fail(f"{path.parent.name}: description too long")

    for entry, _inserted in rows:
        if entry.get("id") == "tool-skill":
            config = entry.get("config") or {}
            limit = config.get("catalogDescriptionMaxLength")
            if limit is None or int(limit) > 240:
                fail(f"tool-skill catalogDescriptionMaxLength must be <=240, got {limit!r}")
        if entry.get("id") == "skill-filesystem":
            dirs = (entry.get("config") or {}).get("customSkillDirs") or []
            if len(dirs) != 1:
                fail("skill-filesystem must configure exactly one custom skill root")
            if ".cursor" in str(dirs[0]):
                fail("do not scan .cursor/skills (duplicates .codex/skills)")

    print(f"dsh-config ok ({len(entries)} entries, {len(skills)} skills)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
