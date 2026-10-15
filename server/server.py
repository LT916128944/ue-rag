# mcp_server/server.py
import sys
import os

# 把 scripts 目录加入路径，才能 import rag_core
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # 向上找一层到 ue-rag

from mcp.server.mcpserver import MCPServer
from core.rag_core import retrieve
from core.tool_router import retrieve_tools, build_agent_prompt

mcp = MCPServer("ue-rag")

@mcp.tool()
def search_ue_project(query: str, top_k: int = 5, where: dict | None = None) -> list[dict]:
    """【语义检索】用自然语言模糊查询 UE 项目中的资产、Actor、C++ 类。

    适用场景：
    - 用户用描述性语言提问，如「跟枪有关的蓝图」「AI 相关的类」「测试关卡里有什么」
    - 用户不确定确切的资产名，想按语义找
    - 需要跨语言查询（中文 query 查英文资产名）

    不适用场景：
    - 用户给出确切的资产路径，如 /Game/BP_Gun → 用 unreal-mcp 的精确查询
    - 需要实时属性值 → 用 unreal-mcp

    Args:
        query: 中文或英文自然语言查询
        top_k: 返回条数，默认 5
        where: 可选。例如 {"source_type": "ue_error"}。不传时与原来一样。
    """
    return retrieve(query, top_k, where=where)

@mcp.tool()
def get_project_summary() -> str:
    """返回当前 RAG 语料库的统计概览。"""
    from core.rag_core import _get_collection
    col = _get_collection()
    return f"collection=ue_assets, total={col.count()}"

if __name__ == "__main__":
    mcp.run()  # 默认 stdio