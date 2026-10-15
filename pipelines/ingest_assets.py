# UE知识库 RAG 向量库构建脚本，读取本地 JSONL 格式的 UE 文档素材（UE 资产说明、UE C++ 代码文档），用 BGE 中文嵌入模型生成向量，存入 Chroma 持久化向量数据库，为后续问答检索做准备

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import ASSET_JSONL, CPP_JSONL, BP_GRAPH_JSONL, DOCS_JSONL, ERRORS_JSONL, CHROMA_PATH

os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

import json
import hashlib
from sentence_transformers import SentenceTransformer
import chromadb

JSONL_PATHS = [ASSET_JSONL, CPP_JSONL, BP_GRAPH_JSONL, DOCS_JSONL, ERRORS_JSONL]
CHROMA_PATH = str(CHROMA_PATH)
COLLECTION_NAME = "ue_assets"

model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
client = chromadb.PersistentClient(path=CHROMA_PATH)
collection = client.get_or_create_collection(
    name=COLLECTION_NAME,
    metadata={"hnsw:space": "cosine"},
)

def load_jsonl(path_or_paths):
    if isinstance(path_or_paths, (str, os.PathLike)):
        paths = [path_or_paths]
    else:
        paths = list(path_or_paths)
    for path in paths:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)

def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()

def delete_stale(collection, ids_by_type: dict[str, set[str]]) -> None:
    """删掉本次 JSONL 里出现过的 source_type 中、不在本次 id 集合里的旧条目。"""
    for source_type, keep in ids_by_type.items():
        if not source_type:
            continue
        got = collection.get(where={"source_type": source_type}, include=[])
        stale = [i for i in got.get("ids", []) if i not in keep]
        for start in range(0, len(stale), 200):
            collection.delete(ids=stale[start:start + 200])
        if stale:
            print(f"Deleted {len(stale)} stale {source_type}")


def main():
    present = [p for p in JSONL_PATHS if Path(p).is_file()]
    missing = [str(p) for p in JSONL_PATHS if not Path(p).is_file()]
    if missing:
        print("Skip missing jsonl: " + ", ".join(missing))
    records = list(load_jsonl(present))
    print(f"Loaded {len(records)} records")
    if not records:
        print("No records, skip upsert")
        return

    ids = []
    documents = []
    metadatas = []
    ids_by_type: dict[str, set[str]] = {}

    for r in records:
        rid = r["id"]
        content = r["content"]
        meta = r.get("metadata", {})
        # Chroma metadata 只接受 str/int/float/bool
        meta = {k: str(v) for k, v in meta.items()}
        meta["source_type"] = r.get("source_type", "")
        meta["source_path"] = r.get("source_path", "")
        meta["content_hash"] = content_hash(content)

        ids.append(rid)
        documents.append(content)
        metadatas.append(meta)
        ids_by_type.setdefault(meta["source_type"], set()).add(rid)

    embeddings = model.encode(documents, normalize_embeddings=True).tolist()

    collection.upsert(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )
    print(f"Upserted {len(ids)} records into collection '{COLLECTION_NAME}'")
    delete_stale(collection, ids_by_type)

if __name__ == "__main__":
    main()