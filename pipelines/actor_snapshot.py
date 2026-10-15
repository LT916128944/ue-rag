"""Actor 坐标只表示导出当时的位置。"""


def actor_text(level_name, label, actor_class, loc_str) -> str:
    return (
        f"关卡 {level_name} 中的 Actor {label}；"
        f"类 {actor_class}；位置快照 {loc_str}；"
        f"坐标不是实时位置；actor 场景对象 实体"
    )


def with_snapshot(record: dict) -> dict:
    if record.get("source_type") != "ue_actor":
        return record
    updated = dict(record)
    meta = dict(record.get("metadata") or {})
    meta["location_kind"] = "snapshot"
    content = str(record.get("content") or "")
    if "坐标不是实时位置" not in content:
        if "位置快照 " not in content and "位置 " in content:
            content = content.replace("位置 ", "位置快照 ", 1)
        content = content.rstrip("；") + "；坐标不是实时位置"
    updated["content"] = content
    updated["metadata"] = meta
    return updated
