
# D4 笔记：C++ 源码元数据导出

## 目标

把项目 C++ 源码接进 RAG，形成第二类语料。

## 为什么 C++ 类不在 /Game

- C++ 类是编译产物，不是 .uasset
- Asset Registry 只扫 /Game
- C++ 类真正的信息在源码里

## 导出方式

- 脚本：scripts/export_cpp_metadata.py
- 输入：Source/AIAgentProject 下的 .h
- 输出：data/ue_cpp.jsonl
- 用正则提取 UCLASS / UPROPERTY / UFUNCTION / 注释
- id：cpp:<ClassName></classname>
- source_type：ue_cpp

## 检索结果

| Query      | Top-1                     | dist   |
| ---------- | ------------------------- | ------ |
| AI 控制器  | cpp:ATestAIController     | 0.3563 |
| 巡逻 AI    | cpp:ATestAIController     | 0.4621 |
| 角色类     | BP_Character              | 0.4818 |
| 游戏模式   | cpp:ATestGameMode         | 0.4062 |
| 玩家控制器 | cpp:ATestPlayerController | 0.3590 |

## 关键教训

1. C++ 记录质量高于资产记录，因为 content 有中文注释 + 属性名 + 函数名。
2. 写代码注释 = 造语料。注释越全，检索越准。
3. 同一 collection 里，cpp 与 asset 记录共存无冲突。
4. 5 个资产时向量检索的弱点不明显；语料变多后要靠 Hybrid + metadata filter。

## 已知不足

- 正则解析是简化版，模板、多行宏、条件编译可能漏。
- 后续如果 C++ 数量多，可换 tree-sitter 或 clang。
- 目前每个类只取 .h，不取 .cpp。函数实现未入语料。
