## D1 决策

| 项             | 选择                                   | 理由                                                 |
| -------------- | -------------------------------------- | ---------------------------------------------------- |
| 语言           | Python 3.12                            | 生态全                                               |
| Embedding      | BAAI/bge-small-zh-v1.5                 | 中英混合 + 跨语言检索；all-MiniLM-L6-v2 中英匹配失效 |
| 备选 Embedding | BAAI/bge-m3                            | 若 bge-small-zh 效果不够，W5 后期替换                |
| 向量库         | Chroma PersistentClient                | 最快跑通，带 metadata 和持久化                       |
| 距离           | cosine                                 |                                                      |
| 持久化路径     | data/chroma/                           |                                                      |
| 文档格式       | JSONL                                  | 便于增量 ingest                                      |
| 检索接口       | retrieve(query, top_k=5, filters=None) | 后续接 MCP                                           |
| 分块策略       | D4 定                                  |                                                      |
| 更新策略       | hash 去重 + upsert                     |                                                      |
| 镜像           | HF_ENDPOINT=https://hf-mirror.com      | 国内直连 HuggingFace 超时                            |

## D2 UE 元数据导出

- UE 5.8 中 AssetData.object_path 已移除，用 `package_name.asset_name` 组合。
- asset_class 用 `asset_class_path.asset_name` 获取。
- 关卡资产 asset_class 为 World，蓝图资产为 Blueprint。
- 输出路径：data/ue_assets.jsonl
- 触发方式：UE Python 控制台里 `exec(open(path).read())`，或后续用 UnrealEditor-Cmd.exe -ExecutePythonScript。

## D3 更新

| 项                   | 选择                                                   | 理由                         |
| -------------------- | ------------------------------------------------------ | ---------------------------- |
| collection 名        | ue_assets                                              | 与 smoke_test 分开           |
| 写入方式             | collection.upsert()                                    | 幂等，支持增量               |
| metadata 类型        | 全部 str                                               | Chroma 只接受标量            |
| content 内容         | 资产名 + 类 + 别名 + ParentClass + 描述                | 提高语义密度                 |
| CLASS_ALIASES        | 按 asset_class 加通用别名，克制，不加“逻辑/类”等泛词 | 避免拉平同类资产             |
| BlueprintDescription | 读进 content                                           | 方案 B，中文描述直接提升检索 |
| 距离                 | cosine                                                 | 与 D1 一致                   |
| top_k                | 5                                                      | 当前默认值                   |
| 已知不足             | Vector Only 对专有名词弱；Actor 与 Asset 混 collection | W6 处理                      |

## D4 更新

| 项           | 选择                                           | 理由                           |
| ------------ | ---------------------------------------------- | ------------------------------ |
| C++ 导出     | 源码解析，不依赖 Asset Registry                | C++ 类不是 .uasset             |
| 输入范围     | Source/AIAgentProject/**/*.h                   | .cpp 与 .h 重复，只取 .h       |
| 解析方式     | 正则提取 UCLASS / UPROPERTY / UFUNCTION / 注释 | 快，够用；后续可换 tree-sitter |
| id 规则      | cpp:<ClassName></classname>                    | 稳定唯一                       |
| source_type  | ue_cpp                                         | 与 ue_asset / ue_actor 区分    |
| ingest 输入  | 多 JSONL 列表                                  | 支持多来源                     |
| content 内容 | 类名 + 父类 + 文件名 + 说明 + 属性 + 函数      | 信息密度高于资产记录           |
