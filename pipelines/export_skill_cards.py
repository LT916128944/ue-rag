# pipelines/export_skill_cards.py
"""从 .cursor/skills/*/SKILL.md 的 front matter 抽出 name 和 description。不向量化全文。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import SKILL_CARDS_PATH, SKILLS_DIR


def _front_matter(text: str) -> tuple[str, str] | None:
    lines = text.splitlines()[:20]
    if not lines or lines[0].strip() != "---":
        return None
    name = ""
    description = ""
    index = 1
    while index < len(lines):
        line = lines[index]
        if line.strip() == "---":
            break
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("'\"")
        elif line.startswith("description:"):
            rest = line.split(":", 1)[1].strip()
            if rest in {">", ">-", "|", "|-"}:
                parts = []
                index += 1
                while index < len(lines) and (lines[index].startswith(" ") or lines[index].startswith("\t")):
                    parts.append(lines[index].strip())
                    index += 1
                description = " ".join(parts)
                continue
            description = rest.strip("'\"")
        index += 1
    if not name and not description:
        return None
    return name, description


def main() -> None:
    cards = []
    if SKILLS_DIR.is_dir():
        for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
            parsed = _front_matter(path.read_text(encoding="utf-8", errors="replace"))
            if not parsed:
                continue
            name, description = parsed
            cards.append({"name": name, "description": description, "path": str(path)})
    SKILL_CARDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKILL_CARDS_PATH.write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(cards)} skill cards to {SKILL_CARDS_PATH}")


if __name__ == "__main__":
    main()
