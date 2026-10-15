
# D1 笔记

## RAG 最小链路

文档 -> chunk -> embedding -> 向量库 -> query -> Top-K -> 拼进 Prompt

## 概念

- Chunk：文档切出来的小块，避免整篇塞上下文
- Embedding：文本转向量，语义相近距离近
- Vector DB：存向量 + 原文 + metadata，支持相似度查询
- Top-K：返回最相似的 K 条，K 太小漏，太大噪声多
- Metadata Filter：按 source_type / asset_path 等过滤
- 持久化：向量库落盘，重启不重算

## W5 怎么用

- 资产/蓝图：每个资产 1 chunk，metadata 带 asset_path / class_name
- C++：按类/函数切，500~1000 tokens，overlap 100~200
- 文档/规则：按标题层级切
- 错误日志：按单次错误切

## D1 踩坑

- `.venv\Scripts\activate`目前pip下的模块都进的虚拟环境
- all-MiniLM-L6-v2 是纯英文模型，中文 query 对英文 text 相似度几乎为 0。
- 换 BAAI/bge-small-zh-v1.5 后跨语言匹配生效。
- HuggingFace 直连超时，需 HF_ENDPOINT=https://hf-mirror.com。
- Windows symlink warning 可忽略。
- Chroma distance 越小越相似，与 cosine 相似度方向相反。
- VSCode 内嵌终端默认不激活 venv，需手动 .\.venv\Scripts\Activate.ps1。

## 总结  

`smoke_embedding.py` 不涉及向量库，只验证 embedding 模型：给定一组中文/英文文本，加一个中文 query，用 cosine 相似度排序，确认模型能跨语言匹配。这一步发现英文模型 all-MiniLM-L6-v2 中英匹配失效，换成 BAAI/bge-small-zh-v1.5 后正常。

`smoke_chroma.py` 验证向量库：把 docs + embedding + metadata 写进 Chroma 的 PersistentClient，再用 query 向量查询，确认 Top-K 排序合理、metadata 完整、数据落盘。

`smoke_chroma_query.py` 是只读版，验证跨进程持久化：新开进程、不写入、只 get_collection + query，仍能查到 BP_Enemy，说明 Chroma 已正确落盘。

另外在 decisions.md 里定了后续要用的策略：embedding 模型、向量库、cosine 距离、检索接口 retrieve(query, top_k=5, filters=None)、文档格式 JSONL、更新策略 hash 去重 + upsert。这些今天只定，不实现，W5 后续的 D4/D5 才实现。
