import os
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

from sentence_transformers import SentenceTransformer
import numpy as np


# D1 快速验证用 all-MiniLM-L6-v2
# 如果中文效果差，W5 后期换 BAAI/bge-small-zh-v1.5 或 bge-m3
# model = SentenceTransformer("all-MiniLM-L6-v2")
model = SentenceTransformer("BAAI/bge-small-zh-v1.5")

texts = [
    "敌人出生点",
    "Enemy spawn point",
    "蓝图修改",
    "Build failed: missing module UnrealEd",
    "玩家攻击逻辑",
]

query = "敌人生成位置"

text_emb = model.encode(texts, normalize_embeddings=True)
query_emb = model.encode([query], normalize_embeddings=True)[0]

scores = text_emb @ query_emb
for text, score in sorted(zip(texts, scores), key=lambda x: x[1], reverse=True):
    print(f"{score:.4f}\t{text}")