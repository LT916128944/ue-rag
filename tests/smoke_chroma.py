# 最小化的端到端冒烟测试脚本，用来验证整套 RAG 向量检索链路是否可以正常跑通：在本地 Chroma 数据库新建一个临时集合，写入 3 条 UE 相关的测试样本文档，生成向量入库，然后执行一次向量查询，打印返回结果
import os
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

from sentence_transformers import SentenceTransformer
import chromadb

model = SentenceTransformer("BAAI/bge-small-zh-v1.5")

client = chromadb.PersistentClient(path="./data/chroma")
collection = client.get_or_create_collection(
    name="smoke_test",
    metadata={"hnsw:space": "cosine"}
)

docs = [
    "BP_Enemy 是敌人蓝图，父类 AEnemyCharacter",
    "BP_Player 是玩家蓝图",
    "Build error: Missing module UnrealEd",
]

metadatas = [
    {"source_type": "blueprint", "asset_path": "/Game/BP_Enemy"},
    {"source_type": "blueprint", "asset_path": "/Game/BP_Player"},
    {"source_type": "build_error", "asset_path": ""},
]

ids = ["1", "2", "3"]
embeddings = model.encode(docs, normalize_embeddings=True).tolist()

collection.add(
    ids=ids,
    documents=docs,
    embeddings=embeddings,
    metadatas=metadatas,
)

q_emb = model.encode(["敌人蓝图"], normalize_embeddings=True).tolist()
res = collection.query(query_embeddings=q_emb, n_results=2)

print(res)