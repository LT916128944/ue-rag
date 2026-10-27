# core/rag_core.py

from config import CHROMA_PATH, PROJECT_ROOT
import os
from pathlib import Path

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

from sentence_transformers import SentenceTransformer
import chromadb

# 用 __file__ 推导项目根目录，避免工作目录问题
_PROJECT_ROOT = PROJECT_ROOT
_CHROMA_PATH = CHROMA_PATH
_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

_MODEL = None
_COLLECTION = None

def _get_model():
    global _MODEL
    if _MODEL is None:
        # 权重条打在 stderr 上，Live 盖不住，最后一行会停在 100%，看起来像卡死。
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        os.environ["TQDM_DISABLE"] = "1"
        try:
            from transformers.utils import logging as tf_logging

            tf_logging.disable_progress_bar()
        except Exception:
            pass
        try:
            from core.console import status

            status("正在加载检索模型")
        except Exception:
            print("正在加载检索模型", flush=True)
        _MODEL = SentenceTransformer(_MODEL_NAME)
        try:
            from core.console import status

            status("检索模型已加载")
        except Exception:
            print("检索模型已加载", flush=True)
    return _MODEL

def _get_collection():
    global _COLLECTION
    if _COLLECTION is None:
        client = chromadb.PersistentClient(path=str(_CHROMA_PATH))
        _COLLECTION = client.get_collection("ue_assets")
    return _COLLECTION

def retrieve_dense(query: str, top_k: int = 5, where: dict | None = None) -> list[dict]:
    """纯向量，用来和漏斗比 Recall。日常检索走 retrieve。"""
    model = _get_model()
    col = _get_collection()
    emb = model.encode([query], normalize_embeddings=True).tolist()
    kwargs = {"query_embeddings": emb, "n_results": top_k}
    if where:
        kwargs["where"] = where
    res = col.query(**kwargs)
    ids = (res.get("ids") or [[]])[0]
    if not ids:
        return []
    out = []
    for i in range(len(ids)):
        out.append({
            "id": res["ids"][0][i],
            "content": res["documents"][0][i],
            "metadata": res["metadatas"][0][i],
            "distance": float(res["distances"][0][i]),
        })
    return out


def retrieve(query: str, top_k: int = 5, where: dict | None = None) -> list[dict]:
    from core.retrieve_funnel import retrieve_funnel
    return retrieve_funnel(query, top_k=top_k, where=where, collection="ue_assets")