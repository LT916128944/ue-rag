# scripts/ingest_tools.py
import os
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

import json
import hashlib
from pathlib import Path
from sentence_transformers import SentenceTransformer
import chromadb

# 用 __file__ 推导项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CHROMA_PATH = _PROJECT_ROOT / "data" / "chroma"
_MCP_INDEX_PATH = _PROJECT_ROOT / "data" / "ue_mcp_index.json"
_COLLECTION_NAME = "ue_tools"
_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()

def main():
    if not _MCP_INDEX_PATH.exists():
        print(f"Error: 找不到 MCP 索引文件 {_MCP_INDEX_PATH}")
        return

    with open(_MCP_INDEX_PATH, "r", encoding="utf-8") as f:
        mcp_data = json.load(f)

    model = SentenceTransformer(_MODEL_NAME)
    client = chromadb.PersistentClient(path=str(_CHROMA_PATH))
    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    ids, documents, metadatas = [], [], []

    for toolset_name, toolset_info in mcp_data.get("toolsets", {}).items():
        for tool_name, tool_data in toolset_info.get("tools", {}).items():
            full_name = tool_data.get("full_name", f"{toolset_name}.{tool_name}")
            desc = tool_data.get("description", "").replace("\n", " ").strip()
            
            # 构建用于 Embedding 的 document
            # 注意：这里不包含完整的参数 Schema，只保留描述，避免上下文过长
            doc_content = f"工具名: {full_name}\n描述: {desc}"
            
            # 将参数名拼进去，增加语义命中率
            properties = tool_data.get("properties", {})
            param_names = list(properties.keys())
            if param_names:
                doc_content += f"\n参数: {', '.join(param_names)}"

            ids.append(full_name)
            documents.append(doc_content)
            metadatas.append({
                "toolset": toolset_name,
                "tool_name": tool_name,
                "full_name": full_name,
                "content_hash": content_hash(doc_content),
            })

    print(f"准备入库 {len(ids)} 个工具...")
    embeddings = model.encode(documents, normalize_embeddings=True).tolist()

    collection.upsert(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )
    print(f"成功将 {len(ids)} 个工具写入集合 '{_COLLECTION_NAME}'")

if __name__ == "__main__":
    main()