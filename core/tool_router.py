# core/tool_router.py
import os
import json
import hashlib
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import CHROMA_PATH, MCP_INDEX_PATH, PROJECT_ROOT

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

import chromadb

_PROJECT_ROOT = PROJECT_ROOT
_CHROMA_PATH = CHROMA_PATH
_MCP_INDEX_PATH = MCP_INDEX_PATH
_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

_MODEL = None
_COLLECTION = None
_MCP_INDEX = None

# ⭐ safe_name <-> full_name 双向映射
_SAFE_TO_FULL: dict[str, str] = {}
_FULL_TO_SAFE: dict[str, str] = {}


# ---------- 查询改写表 ----------
# 工具说明全是英文。中文和英文都改写成英文工具描述，只用于 ue_tools。
# 顺序：更具体的说法在前，修改类优先于查询类。命中即返回，不拼接原 query。
_EN_REWRITE = [
    (["编译蓝图", "蓝图编译", "compile blueprint", "compile the blueprint"],
     "Compiles the given Blueprint. Blueprints should be compiled after all graph modifications are complete."),
    (["move ", "moving ", "translate", "lift", "raise", "lower",
      "up by", "down by", "set z", "set x", "set y",
      "position the", "place the", "relocate", "shift ",
      "移动", "上移", "下移", "平移", "移到", "挪到", "抬高", "升高", "放低", "向上", "向下",
      "设置坐标", "改坐标"],
     "Updates the position, rotation, and/or scale of an actor. set actor transform"),
    (["get position", "get location", "where is", "current transform",
      "current location", "where are"],
     "Returns the position, rotation, and scale of an actor. get actor transform"),
    (["list all", "list actors", "all actors", "enumerate",
      "visible actors", "what actors", "which actors",
      "列出", "有哪些", "所有角色", "全部actor", "所有actor"],
     "Searches the scene for actors that match specific criteria. Returns all actors in the current level. find actors"),
    (["delete ", "remove ", "destroy ",
      "删除", "删掉", "销毁", "移除"],
     "Deletes an actor from the scene. remove actor from level"),
    (["spawn ", "create actor", "place actor", "add actor",
      "生成actor", "创建actor", "放置actor", "刷出"],
     "Creates a new instance of the specified object at the specified transform. spawn actor"),
    (["blueprint", "bp ", "蓝图"],
     "Searches the project for assets that match specific criteria. find blueprint asset"),
    (["unit test", "automation test", "单元测试", "自动化测试", "跑测试"],
     "run automation test, run tests by filter, get test results"),
    (["build", "compile", "rebuild", "编译", "构建"],
     "build project, compile project, get build error"),
    (["find ", "search ", "look for", "locate ",
      "查找", "寻找", "搜索"],
     "Searches the scene for actors that match specific criteria. find actors by name"),
    (["property", "properties", "attribute", "属性"],
     "get object properties, set object properties, list properties"),
    (["material", "材质", "材质实例"],
     "find material, modify material, set material parameter"),
    (["asset", "资源", "资产"],
     "Searches the project for assets that match specific criteria. find asset"),
    (["什么位置", "当前位置", "坐标是", "的位置", "在哪"],
     "Returns the position, rotation, and scale of an actor. get actor transform"),
]


def _rewrite_query(q: str) -> str:
    """把自然语言 query 改写为英文技术描述，提升跨语言/跨术语检索命中率。
    只返回改写结果，不拼接原 query，避免实体名污染 embedding。
    """
    ql = q.lower()
    for keys, rewrite in _EN_REWRITE:
        for k in keys:
            if k in ql:
                return rewrite
    return q


# ---------- safe name 映射 ----------
def _make_safe_name(full_name: str) -> str:
    """MCP full_name → OpenAI function name。
    OpenAI 约束：^[a-zA-Z0-9_-]{1,64}$
    """
    if full_name in _FULL_TO_SAFE:
        return _FULL_TO_SAFE[full_name]

    safe = full_name.replace(".", "__").replace("/", "__").replace(":", "__")
    if len(safe) > 64:
        suffix = hashlib.md5(full_name.encode()).hexdigest()[:8]
        safe = safe[:55] + "_" + suffix
    _SAFE_TO_FULL[safe] = full_name
    _FULL_TO_SAFE[full_name] = safe
    return safe


def get_full_name(safe_name: str) -> str | None:
    """safe_name → full_name，失败返回 None。"""
    return _SAFE_TO_FULL.get(safe_name)


def split_full_name(full_name: str) -> tuple[str, str]:
    """ToolsetRegistry.AgentSkillToolset.UpdateSkill
       → ("ToolsetRegistry.AgentSkillToolset", "UpdateSkill")
    顶层工具（无 "."）→ ("", tool_name)
    """
    if "." not in full_name:
        return "", full_name
    toolset, tool = full_name.rsplit(".", 1)
    return toolset, tool


# ---------- 单例资源 ----------
def _get_model():
    """和资产检索共用一个 SentenceTransformer。"""
    from core.rag_core import _get_model as shared
    return shared()


def _get_collection():
    global _COLLECTION
    if _COLLECTION is None:
        client = chromadb.PersistentClient(path=str(_CHROMA_PATH))
        _COLLECTION = client.get_collection("ue_tools")
    return _COLLECTION


def _get_mcp_index():
    global _MCP_INDEX
    if _MCP_INDEX is None:
        with open(_MCP_INDEX_PATH, "r", encoding="utf-8") as f:
            _MCP_INDEX = json.load(f)
    return _MCP_INDEX


# ---------- 检索 ----------
def retrieve_tools(query: str, top_k: int = 8) -> list[dict]:
    """漏斗选出少量工具后，只给这些工具带上 input_schema。"""
    from core.retrieve_funnel import retrieve_funnel

    query = _rewrite_query(query)
    hits = retrieve_funnel(query, top_k=top_k, collection="ue_tools")
    mcp_index = _get_mcp_index()
    tools = []
    for hit in hits:
        meta = hit.get("metadata") or {}
        toolset_name = meta.get("toolset") or ""
        tool_name = meta.get("tool_name") or ""
        try:
            tool_data = mcp_index["toolsets"][toolset_name]["tools"][tool_name]
        except KeyError:
            continue
        full_name = tool_data.get("full_name") or hit.get("id") or f"{toolset_name}.{tool_name}"
        safe = _make_safe_name(full_name)
        tools.append({
            "type": "function",
            "function": {
                "name": safe,
                "description": tool_data.get("description", ""),
                "parameters": tool_data.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return tools


def tool_schema(short_name: str) -> dict | None:
    """按短名从索引取一个 OpenAI tool。允许列表里的工具不靠向量命中。"""
    index = _get_mcp_index()
    for toolset_name, toolset in (index.get("toolsets") or {}).items():
        tools = toolset.get("tools") or {}
        if short_name not in tools:
            continue
        data = tools[short_name]
        full_name = data.get("full_name") or f"{toolset_name}.{short_name}"
        return {
            "type": "function",
            "function": {
                "name": _make_safe_name(full_name),
                "description": data.get("description", ""),
                "parameters": data.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
    return None


# ---------- Prompt 组装 ----------
def build_agent_prompt(user_query: str, project_context: str = "", *, require_verify: bool = False) -> str:
    """只负责组装 prompt 文本。工具列表由 plan_node 通过 tools= 参数传入 LLM。

    不再内部调用 retrieve_tools，避免和 retrieve_node 检索结果不一致。
    """
    if require_verify:
        missing = "返回空的工具调用，让裁判执行。不要调用 finish。"
    else:
        missing = "调用 finish(success=false, summary=...) 说明原因。"
    prompt = f"""你是一个 Unreal Engine 5 的 AI Agent。
当前项目上下文：
{project_context}

**核心纪律**：
1. 你只能使用 tools 参数里提供的工具来完成任务。
2. 严禁臆造不存在的工具，严禁调用 list_tools / describe_toolset / call_tool 等元工具。
3. 如果 tools 里没有合适工具，{missing}

**工具调用纪律**：
- 修改 Actor 前，必须先用 find_actors 拿到它的完整对象路径。name、tag、collision_channels 都要传。
- Actor 完整路径格式示例：
    /Game/Maps/L_NewTest.L_NewTest:PersistentLevel.SkyLight_0
- 严禁使用短名（SkyLight_0）、资产路径（/Game/SkyLight_0）或 Class.name 形式。
- 参数错误返回后，不要用同一个错误路径重试，先回去查真实路径。

请根据用户输入，输出准确的 Function Calling 指令。"""
    return prompt
# ---------- 常驻本地工具（不走 UE MCP，直接调 Python） ----------
LOCAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_ue_project",
            "description": (
                "【首选·项目资产检索】用自然语言查询本 UE 项目里的蓝图、C++ 类、资产。"
                "返回资产名、路径、类、元数据。"
                "适用：用户问'我项目里有哪些 XX''XX 类在哪''关于 XX 的蓝图'。"
                "不适用：查询当前关卡 Actor 的实时位置/属性 → 用 GetVisibleActors / find_actors。"
                "不适用：需要修改资产 → 用 set_actor_transform 等写工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "中文或英文自然语言查询，例如 '所有敌人相关的 C++ 类'",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回条数，默认 5",
                        "default": 5,
                    },
                    "where": {
                        "type": "object",
                        "description": "可选 metadata 过滤，例如 {\"source_type\": \"ue_error\"}",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_summary",
            "description": "返回当前 RAG 语料库的统计概览（collection 名、条目数）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_source",
            "description": (
                "读取 UE_SOURCE_ROOT 下的源文件。path 相对该根目录，例如 Private/TestActor.cpp。"
                "拒绝 .. 。一次最多 200 行。返回带行号的 numbered，以及不带行号的 text。"
                "unified diff 必须匹配 text，不要把行号写进 diff。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_unified_diff",
            "description": (
                "把 unified diff 应用到 UE_SOURCE_ROOT 下的同一个 path。"
                "只接受一个文件的 @@ 块。应用前旧行必须与文件一致，否则返回 diff conflict 且不写入。"
                "这是修改 C++ 的唯一方式。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "diff": {"type": "string", "description": "unified diff，含 @@ 块"},
                },
                "required": ["path", "diff"],
            },
        },
    },
]


def get_local_tools(names: set[str] | None = None) -> list[dict]:
    tools = [dict(t) for t in LOCAL_TOOLS]
    if names is None:
        return tools
    return [item for item in tools if item["function"]["name"] in names]


LOCAL_TOOL_NAMES = {t["function"]["name"] for t in LOCAL_TOOLS}

# ---------- 自测 ----------
if __name__ == "__main__":
    test_queries = [
        "Raise the Z-coordinate of all enemy spawn points in the scene by 100 and verify",
        "Move the SkyLight_0 actor up by 100 units and verify",
        "List all actors in the current level",
        "Compile and pass unit tests",
    ]
    for q in test_queries:
        print(f"\n=== Query: {q} ===")
        tools = retrieve_tools(q, top_k=8)
        for i, t in enumerate(tools):
            print(f"  [{i+1}] {t['function']['name']}")