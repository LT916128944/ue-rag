# 配套ingest_assets.py (向量入库脚本) 的检索测试脚本
import os
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

from sentence_transformers import SentenceTransformer
import chromadb

CHROMA_PATH = r"E:\UEProject\AIWork\RAG\ue-rag\data\chroma"
COLLECTION_NAME = "ue_assets"
TOP_K = 5

model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
client = chromadb.PersistentClient(path=CHROMA_PATH)
collection = client.get_collection(name=COLLECTION_NAME)

queries = [
    "敌人蓝图",
    "枪的蓝图",
    "测试关卡",
    "游戏地图",
    "武器资产",
    "AI 控制器",
    "巡逻 AI",
    "角色类",
    "游戏模式",
    "玩家控制器",
]

for q in queries:
    q_emb = model.encode([q], normalize_embeddings=True).tolist()
    res = collection.query(query_embeddings=q_emb, n_results=TOP_K)

    print(f"\n=== Query: {q} ===")
    for i in range(len(res["ids"][0])):
        doc_id = res["ids"][0][i]
        doc = res["documents"][0][i]
        dist = res["distances"][0][i]
        meta = res["metadatas"][0][i]
        print(f"  [{i+1}] dist={dist:.4f}  id={doc_id}")
        print(f"      content={doc[:80]}")
        print(f"      asset_class={meta.get('asset_class')}")