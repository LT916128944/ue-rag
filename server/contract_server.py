# server/contract_server.py
"""对外只暴露契约里的六个工具。不返回官方 830 个。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mcp.server.mcpserver import MCPServer

from core import contract_tools

mcp = MCPServer("ue-contract")


@mcp.tool()
def query_actor(name: str) -> dict:
    """按名字读当前关卡里唯一 Actor 的位置，零个或多个就停。"""
    return contract_tools.query_actor(name)


@mcp.tool()
def query_blueprint(ref_path: str) -> dict:
    """读一张已有蓝图的图名、变量名和事件名，不改图。"""
    return contract_tools.query_blueprint(ref_path)


@mcp.tool()
def find_asset(name: str) -> dict:
    """在 /Game 里按名字找资产，不是唯一就停。"""
    return contract_tools.find_asset(name)


@mcp.tool()
def build_project() -> dict:
    """关掉编辑器后编译 AIAgentProjectEditor，并留下错误原文。"""
    return contract_tools.build_project()


@mcp.tool()
def run_automation_test(filter: str) -> dict:
    """按过滤器跑 Automation，失败数不是 0 就不算通过。"""
    return contract_tools.run_automation_test(filter)


@mcp.tool()
def get_build_error() -> dict:
    """读上一次 build_project 留下的编译错误，没有记录就停。"""
    return contract_tools.get_build_error()


if __name__ == "__main__":
    mcp.run()
