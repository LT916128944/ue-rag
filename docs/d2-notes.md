
* Asset Registry 基本用法：`search_all_assets` → `get_assets_by_path('/Game', recursive=True)`
* AssetData 的关键字段：`asset_name`、`package_name`、`asset_class_path.asset_name`、`get_tag_value()`
* 5.8 与旧版差异：`UE 5.8 中 AssetData.object_path 已移除，用 package_name.asset_name 组合。`
* 触发方式：使用脚本：/scripts/run_export.ps1
  * cd E:\UEProject\AIWork\RAG\ue-rag
  * .\scripts\run_export.ps1
