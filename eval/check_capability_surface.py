"""能力面对账（20261028 P1-5）：执行器真实面 vs 规划者看得见的动作。

两个方向：
- 方向一（找遗漏）：执行器有、规划者看不见的 monolith 原语——凡是 actions.SURFACE
  声明过的 namespace 里、没被任何声明覆盖的 action，自动提名进 action_candidates
  （source="capability_surface"），把 D8 的候选闭环接到真数据源上。
- 方向二（防谎报）：actions.SURFACE 声明的每条 ``namespace.action`` 必须真在
  capability_surface.json 里；声明了执行器却没有的原语 = 谎报能力，逐条报错。

数据源 ``data/capability_surface.json`` 由 pipelines/export_capability_surface.py 生成
（需要编辑器在线）。文件不在时本检查跳过（打印 skip，退出码 0）——离线 CI 不连编辑器。
用 ``UE_CAPABILITY_SURFACE`` 环境变量或 ``--surface`` 参数指到 fixture 即可离线断言。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _surface_file(argv: list[str]) -> Path:
    if "--surface" in argv:
        return Path(argv[argv.index("--surface") + 1])
    override = os.environ.get("UE_CAPABILITY_SURFACE", "").strip()
    if override:
        return Path(override)
    try:
        from pipelines.export_capability_surface import surface_path

        return surface_path()
    except Exception:
        from config import DATA_DIR

        return DATA_DIR / "capability_surface.json"


def load_surface(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def declared_primitives() -> dict[str, dict[str, list[str]]]:
    from agent.actions import SURFACE

    return SURFACE


def check_declared_primitives_exist(surface: dict, problems: list[str]) -> None:
    """方向二：声明了的原语，执行器里必须真有。"""
    namespaces = surface.get("namespaces") if isinstance(surface.get("namespaces"), dict) else {}
    for ns, actions in declared_primitives().items():
        rows = namespaces.get(ns)
        if rows is None:
            problems.append(f"方向二 capability_surface.json 里没有 namespace {ns}（编辑器没导出它）")
            continue
        present = {str(row.get("name") or "") for row in rows if isinstance(row, dict)}
        for action in actions:
            if action not in present:
                problems.append(
                    f"方向二 {ns}.{action}：actions.SURFACE 声明了它，执行器里没有——"
                    f"声明在撒谎，先把声明或执行器对齐"
                )


def nominate_uncovered(surface: dict) -> list[str]:
    """方向一：声明过的 namespace 里、没被任何声明覆盖的 action → 提名进候选表。"""
    from agent import catalog

    coverage = declared_primitives()
    namespaces = surface.get("namespaces") if isinstance(surface.get("namespaces"), dict) else {}
    nominated: list[str] = []
    for ns, rows in namespaces.items():
        declared_actions = coverage.get(str(ns)) or {}
        if not declared_actions:
            continue  # 没声明过的 namespace 整个跳过：提名只落在已知相关的域里
        for row in rows:
            if not isinstance(row, dict):
                continue
            action = str(row.get("name") or "").strip()
            if not action or action in declared_actions:
                continue
            label = f"{ns}.{action}"
            description = str(row.get("description") or "").strip()
            summary = "执行器有、规划者看不见的能力面原语" + (
                f"：{description[:80]}" if description else ""
            )
            created = catalog.note_candidate(label, source="capability_surface", summary=summary)
            if created is not None:
                nominated.append(label)
    return nominated


def _selftest(tmp: Path) -> None:
    """离线自测：fixture 能力面 + 临时候选表，两个方向都断言到。"""
    fixture = tmp / "surface.json"
    fixture.write_text(json.dumps({
        "generated_at": "2026-10-28T00:00:00",
        "namespace_count": 2,
        "action_count": 4,
        "namespaces": {
            "editor": [
                {"name": "load_level", "description": "载入关卡"},
                {"name": "create_empty_map", "description": "新建空关卡"},
                {"name": "save_packages", "description": "保存包"},
                {"name": "niagara_like_new_action", "description": "执行器新长出来的原语"},
            ],
        },
    }, ensure_ascii=False), encoding="utf-8")

    saved_candidates = os.environ.get("UE_ACTION_CANDIDATES")
    os.environ["UE_ACTION_CANDIDATES"] = str(tmp / "candidates.json")
    saved_surface = os.environ.get("UE_CAPABILITY_SURFACE")
    os.environ["UE_CAPABILITY_SURFACE"] = str(fixture)
    try:
        from agent.actions import SURFACE

        assert SURFACE.get("editor", {}).get("load_level"), "actions.SURFACE 应声明 editor.load_level"
        # 方向一：同一 namespace 里未被声明覆盖的原语 → 提名进候选表
        first = nominate_uncovered(load_surface(fixture) or {})
        assert "editor.niagara_like_new_action" in first, first
        rows = json.loads((tmp / "candidates.json").read_text(encoding="utf-8"))["candidates"]
        hit = next(row for row in rows if row["name"] == "editor.niagara_like_new_action")
        assert hit.get("source") == "capability_surface", hit
        assert hit.get("hits") == 1, hit
        # 再跑一次：累计命中，不重复建行
        nominate_uncovered(load_surface(fixture) or {})
        rows = json.loads((tmp / "candidates.json").read_text(encoding="utf-8"))["candidates"]
        hit = next(row for row in rows if row["name"] == "editor.niagara_like_new_action")
        assert hit.get("hits") == 2, hit

        # 方向二：声明了执行器没有的原语 → 报错
        problems: list[str] = []
        broken = {"namespaces": {"editor": [{"name": "load_level"}]}}
        check_declared_primitives_exist(broken, problems)
        assert problems and any("editor.create_empty_map" in line for line in problems), problems
    finally:
        if saved_candidates is None:
            os.environ.pop("UE_ACTION_CANDIDATES", None)
        else:
            os.environ["UE_ACTION_CANDIDATES"] = saved_candidates
        if saved_surface is None:
            os.environ.pop("UE_CAPABILITY_SURFACE", None)
        else:
            os.environ["UE_CAPABILITY_SURFACE"] = saved_surface


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if "--selftest" in sys.argv:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            _selftest(Path(tmp))
        print("capability surface selftest ok")
        return 0
    path = _surface_file(sys.argv)
    surface = load_surface(path)
    if surface is None:
        print(f"capability surface skip：{path} 不存在（先跑 pipelines/export_capability_surface.py，需编辑器在线）")
        return 0
    problems: list[str] = []
    check_declared_primitives_exist(surface, problems)
    if problems:
        print("capability surface FAILED")
        for line in problems:
            print("  -", line)
        return 1
    nominated = nominate_uncovered(surface)
    print(
        f"capability surface ok：{surface.get('namespace_count', '?')} 个 namespace、"
        f"{surface.get('action_count', '?')} 个 action；本轮提名 {len(nominated)} 条"
        + ("：" + "、".join(nominated[:8]) if nominated else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())