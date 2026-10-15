import json
import sys
from pathlib import Path

import unreal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import ASSET_JSONL
from pipelines.actor_snapshot import actor_text

OUTPUT_PATH = str(ASSET_JSONL)

# asset_class 中文别名：让 content 携带 query 可能出现的同义语义
CLASS_ALIASES = {
    "World": "关卡 地图 level map",
    "Blueprint": "蓝图 blueprint",
    "Material": "材质 material",
    "MaterialInstance": "材质实例 材质",
    "StaticMesh": "静态网格 模型 mesh",
    "SkeletalMesh": "骨骼网格 模型 mesh",
    "Texture": "贴图 纹理 texture",
    "Texture2D": "贴图 纹理 texture",
    "AnimSequence": "动画 序列 animation",
    "AnimBlueprint": "动画蓝图 animation",
    "SoundWave": "音效 声音 sound",
    "SoundCue": "音效 声音 sound",
    "Actor": "场景对象 实体 actor",
    "LevelSequence": "关卡序列 过场 sequence",
    "WidgetBlueprint": "UI 控件 界面 widget",
    "DataTable": "数据表 表格 data",
    "Enum": "枚举 enum",
    "Struct": "结构体 struct",
    "CurveFloat": "曲线 float curve",
    "ParticleSystem": "粒子 特效 particle",
    "NiagaraSystem": "Niagara 特效 particle",
}

# 从 AssetData 提取通用字段，5.8 兼容
def get_class_name(asset):
    try:
        return str(asset.asset_class_path.asset_name)
    except Exception:
        return str(asset.asset_class)

def get_object_path(asset):
    # 5.8 已移除 asset.object_path，手动组合
    return f"{asset.package_name}.{asset.asset_name}"

def build_asset_content(asset):
    class_name = get_class_name(asset)
    aliases = CLASS_ALIASES.get(class_name, "")

    parts = [
        f"资产 {asset.asset_name}",
        f"类 {class_name}" + (f" {aliases}" if aliases else ""),
        f"路径 {asset.package_name}",
    ]

    # 通用 tag
    for tag_key in ("AssetImportData", "ParentClass", "BlueprintType", "NumRevisions"):
        val = asset.get_tag_value(tag_key)
        if val:
            parts.append(f"{tag_key}: {val}")

    # 蓝图描述（方案 B 的核心）
    desc = asset.get_tag_value("BlueprintDescription")
    if desc:
        parts.append(f"描述: {desc}")

    # 蓝图父类名单独提出来，方便中文 query 命中
    parent = asset.get_tag_value("ParentClass")
    if parent:
        parent_short = parent.split("'")[-2] if "'" in parent else parent
        parent_short = parent_short.split(".")[-1]
        parts.append(f"父类名: {parent_short}")

    return class_name, "；".join(parts)

def build_asset_record(asset):
    class_name, content = build_asset_content(asset)
    object_path = get_object_path(asset)
    package_name = str(asset.package_name)

    return {
        "id": f"asset:{object_path}",
        "source_type": "ue_asset",
        "source_path": package_name,
        "content": content,
        "metadata": {
            "asset_name": str(asset.asset_name),
            "asset_class": class_name,
            "object_path": object_path,
            "package_name": package_name,
        },
    }

def build_actor_records(asset):
    """为关卡资产导出 Actor 记录"""
    records = []
    object_path = get_object_path(asset)

    # 注意：load_asset 会加载关卡，-unattended 模式下也可以跑，
    # 但如果当前 Editor 已打开其他关卡，行为可能不可预期。
    level = unreal.load_asset(object_path)
    if level is None:
        unreal.log_warning(f"Failed to load level: {object_path}")
        return records

    actors = unreal.EditorLevelLibrary.get_all_level_actors()
    for actor in actors:
        try:
            label = str(actor.get_actor_label())
            actor_class = actor.get_class().get_name()
            loc = actor.get_actor_location()
            loc_str = f"({loc.x:.1f}, {loc.y:.1f}, {loc.z:.1f})"
            actor_path = str(actor.get_path_name())

            records.append({
                "id": f"actor:{actor_path}",
                "source_type": "ue_actor",
                "source_path": object_path,
                "content": actor_text(asset.asset_name, label, actor_class, loc_str),
                "metadata": {
                    "actor_label": label,
                    "actor_class": actor_class,
                    "location": loc_str,
                    "location_kind": "snapshot",
                    "level_path": object_path,
                    "level_name": str(asset.asset_name),
                },
            })
        except Exception as e:
            unreal.log_warning(f"Actor export failed: {e}")

    return records

def main():
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    registry.search_all_assets(True)
    assets = list(registry.get_assets_by_path('/Game', recursive=True))
    unreal.log(f"Found {len(assets)} assets under /Game")

    total_assets = 0
    total_actors = 0

    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        for asset in assets:
            record = build_asset_record(asset)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            total_assets += 1

            class_name = get_class_name(asset)
            if class_name in ("World", "Level"):
                for actor_rec in build_actor_records(asset):
                    f.write(json.dumps(actor_rec, ensure_ascii=False) + "\n")
                    total_actors += 1

    unreal.log(f"Exported {total_assets} assets + {total_actors} actors to {OUTPUT_PATH}")

if __name__ == "__main__":
    main()