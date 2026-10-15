import os
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

from sentence_transformers import SentenceTransformer
import chromadb

model = SentenceTransformer("BAAI/bge-small-zh-v1.5")

client = chromadb.PersistentClient(path="./data/chroma")
collection = client.get_collection(name="smoke_test")

q_emb = model.encode(["敌人蓝图"], normalize_embeddings=True).tolist()
res = collection.query(query_embeddings=q_emb, n_results=2)

print(res["documents"])
print(res["metadatas"])