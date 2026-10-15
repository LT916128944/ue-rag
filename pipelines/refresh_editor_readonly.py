"""编辑器开着时，只读刷新 /Game 资产卡片和当前关卡 Actor 快照。不保存，不加载其他关卡。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import ASSET_JSONL
from core.editor_exec import exec_tool
from pipelines.actor_snapshot import actor_text, with_snapshot

_ASSET = "editor_toolset.toolsets.asset.AssetTools"
_SCENE = "editor_toolset.toolsets.scene.SceneTools"
_TASK = {"allow_writes": [], "save": False, "pie": False}


def _unwrap(data):
    if isinstance(data, dict) and "returnValue" in data:
        inner = data["returnValue"]
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                return inner
        return _unwrap(inner)
    return data


def _as_list(data):
    data = _unwrap(data)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("returnValue", "assets", "actors"):
            if key in data:
                return _as_list(data[key])
    if isinstance(data, str) and data:
        return [data]
    return []


def _label(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("refPath", "path", "name", "label", "actor_label"):
            if item.get(key):
                return str(item[key])
    return ""


def _asset_id(path: str) -> str:
    text = path.strip()
    leaf = text.rstrip("/").split("/")[-1]
    if "." not in leaf and leaf:
        text = f"{text}.{leaf}"
    return f"asset:{text}"


def _loc(item) -> str:
    if not isinstance(item, dict):
        return ""
    loc = item.get("location") or item.get("actor_location") or {}
    if isinstance(loc, dict) and all(axis in loc for axis in ("x", "y", "z")):
        return f"({float(loc['x']):.1f}, {float(loc['y']):.1f}, {float(loc['z']):.1f})"
    return ""


def _load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def main() -> int:
    rows = [with_snapshot(row) for row in _load(ASSET_JSONL)]
    by_id = {row["id"]: row for row in rows if row.get("id")}
    found = exec_tool(
        _ASSET,
        "find_assets",
        {"folder_path": "/Game", "name": "", "recursive": True},
        task=_TASK,
    )
    added = 0
    if not found.get("ok"):
        print(f"asset refresh skipped: {found.get('error')}")
    else:
        for item in _as_list(found.get("data")):
            label = _label(item)
            if not label.startswith("/Game"):
                continue
            record_id = _asset_id(label)
            if record_id in by_id:
                continue
            name = record_id.split(".")[-1]
            by_id[record_id] = {
                "id": record_id,
                "source_type": "ue_asset",
                "source_path": label,
                "content": f"资产 {name}；路径 {label}",
                "metadata": {"asset_name": name, "object_path": record_id[len("asset:"):], "package_name": label},
            }
            added += 1
        level = exec_tool(_SCENE, "get_current_level", {}, task=_TASK)
        actors = exec_tool(
            _SCENE,
            "find_actors",
            {"name": "", "tag": "", "collision_channels": []},
            task=_TASK,
        )
        level_name = _label(_unwrap(level.get("data"))) or "当前关卡"
        updated = 0
        if actors.get("ok"):
            for item in _as_list(actors.get("data")):
                if not isinstance(item, dict):
                    continue
                loc = _loc(item)
                if not loc:
                    continue
                label = str(item.get("label") or item.get("name") or "")
                actor_path = str(item.get("refPath") or item.get("path") or label)
                record_id = f"actor:{actor_path}"
                actor_class = str(item.get("class") or item.get("actor_class") or "")
                content = actor_text(level_name, label, actor_class, loc)
                by_id[record_id] = with_snapshot({
                    "id": record_id,
                    "source_type": "ue_actor",
                    "source_path": level_name,
                    "content": content,
                    "metadata": {
                        "actor_label": label,
                        "actor_class": actor_class,
                        "location": loc,
                        "location_kind": "snapshot",
                        "level_name": level_name,
                    },
                })
                updated += 1
        print(f"asset refresh added={added} actors_updated={updated}")
    if not by_id and not ASSET_JSONL.is_file():
        print("asset refresh skipped: no local jsonl")
        return 0
    ASSET_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with ASSET_JSONL.open("w", encoding="utf-8") as handle:
        for record in by_id.values():
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"asset snapshot tagged records={len(by_id)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
