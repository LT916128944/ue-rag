
# D3 笔记

## 目标

把 ue_assets.jsonl 接进 RAG 检索链：JSONL → embedding → Chroma → retrieve

## 输入

- data/ue_assets.jsonl，由 D2 的 export_asset_metadata.py 生成
- 含两类记录：ue_asset（资产）和 ue_actor（关卡内 Actor）

## 关键设计

- collection：ue_assets
- 写入：collection.upsert()，重复跑不冲突
- metadata 全转 str，Chroma 只接受标量
- content_hash 存 metadata，为增量更新做准备
- embedding：BAAI/bge-small-zh-v1.5
- distance：cosine，越小越相似
- content 里加 CLASS_ALIASES 中文别名
- content 里读 BlueprintDescription（方案 B）

## 检索结果（5 条 query）

| Query    | Top-1            | 判定                 |
| -------- | ---------------- | -------------------- |
| 敌人蓝图 | BP_Gun           | 可接受（项目无敌人） |
| 枪的蓝图 | BP_Gun           | ✅                   |
| 测试关卡 | BP_Gun_C (Actor) | ✅                   |
| 游戏地图 | L_NewTest        | ✅                   |
| 武器资产 | BP_Gun           | ✅                   |

## D3 关键教训

### 1. 别名不是越多越好

- 加中文别名让「测试关卡」「游戏地图」命中正确。
- 但过泛别名（所有 Blueprint 都加「蓝图 逻辑 类」）会拉平同类资产，
  导致「枪的蓝图」退化为命中 BP_GameMode。
- 别名要「能区分」，不要「都加上」。

### 2. asset_name 是英文时，中文 query 命中依赖模型

- BP_Gun 能命中「枪」，是 bge-small-zh 的跨语言能力。
- 换成 BP_Weapon_A 可能失败。
- 解法：a) asset_name 中文映射表；b) W6 Hybrid + BM25；c) 资产上加中文描述。

### 3. Actor 与 Asset 混在同一 collection 会互相干扰

- 「测试关卡」Top-5 全是 Actor，关卡资产本身没进。
- Actor content 带「关卡」二字，和 query 直接匹配，占据榜单。
- 解法：W6 用 metadata filter，按 source_type 过滤。

### 4. Vector Only 对「短 query + 短 chunk + 专有名词」不友好

- 5 个资产时看不出来，语料多了会放大。
- 这是 W6 Hybrid + Rerank 的动机。

## 检索接口（当前实现）

```python
def retrieve(query, top_k=5, filters=None):
    q_emb = model.encode([query], normalize_embeddings=True).tolist()
    return collection.query(query_embeddings=q_emb, n_results=top_k, where=filters)
```
