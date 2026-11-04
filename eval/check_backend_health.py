# -*- coding: utf-8 -*-
"""U0：Monolith 后端连接层健康。

钉住三件事：
1. 连接类失败（502/超时/断连）在连接层重试一次，成功则不带指引、不影响业务语义；
2. 重试仍失败时，错误原文追加固定指引，不回退 8000、不改写业务错误；
3. health() 探活不消耗 MCP 调用：任何 HTTP 响应算活，连接失败带指引。
不打开编辑器、不调真实 MCP。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backends import monolith  # noqa: E402
from core import failure_class  # noqa: E402
from core.ue_mcp_client import is_connection_error  # noqa: E402


class _FakeResp:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeHttpError(Exception):
    pass


def _fail_client(error: str, connection: bool = True):
    """模拟 UEOfficialMCPClient.call_tool 的失败形状。"""
    def send(tool_name, arguments):
        raw = {"ok": False, "error": error}
        if connection:
            raw["connection_error"] = True
        return raw
    return send


def check_retry_once_then_success() -> None:
    calls = []

    def send(tool_name, arguments):
        calls.append(tool_name)
        if len(calls) == 1:
            return {"ok": False, "error": "UE MCP 调用失败: HTTPStatusError: Server error '502 Bad Gateway'", "connection_error": True}
        return {"ok": True, "data": {"value": 1}}

    raw = monolith._send_with_retry("mesh_query", {"action": "get_actor_info", "params": {}}, send)
    assert raw.get("ok") is True, raw
    assert len(calls) == 2, calls
    assert "listening on port 9316" not in str(raw), "成功结果不应带指引"
    print("ok: 连接类失败重试一次后成功，结果干净")


def check_retry_exhausted_gets_guidance() -> None:
    send = _fail_client("UE MCP 调用失败: HTTPStatusError: Server error '502 Bad Gateway'")
    raw = monolith._send_with_retry("mesh_query", {"action": "get_actor_info", "params": {}}, send)
    assert raw.get("ok") is False
    assert "Bad Gateway" in str(raw.get("error")), raw
    assert monolith.connection_guidance() in str(raw.get("error")), raw
    assert "8000" not in str(raw.get("error")), "指引不得指向回退 8000"
    print("ok: 重试仍失败时错误原文带指引，且不指向 8000")


def check_business_error_not_retried() -> None:
    calls = []

    def send(tool_name, arguments):
        calls.append(tool_name)
        return {"ok": False, "error": "write refused"}

    raw = monolith._send_with_retry("blueprint_query", {"action": "add_node", "params": {}}, send)
    assert raw.get("ok") is False and raw.get("error") == "write refused", raw
    assert len(calls) == 1, "业务拒绝不重试"
    print("ok: 业务类失败（write refused）不消耗连接层重试")


def check_connection_error_classifier() -> None:
    import httpx

    def status_error(status: int) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "http://127.0.0.1:9316/mcp")
        response = httpx.Response(status, request=request)
        return httpx.HTTPStatusError(f"Server error '{status}'", request=request, response=response)

    transport_error = httpx.ConnectError("[WinError 10061]")

    assert is_connection_error(Exception("x")) is False
    assert is_connection_error(transport_error) is True
    assert is_connection_error(status_error(502)) is True
    assert is_connection_error(status_error(503)) is True
    assert is_connection_error(status_error(404)) is False
    print("ok: 5xx 与传输层错误归连接类，4xx 不归")


def check_health_probe() -> None:
    alive = monolith.health(transport=lambda url: _FakeResp(405))
    assert alive.get("ok") is True and alive.get("status") == 405, alive

    def dead(url):
        raise _FakeHttpError("ConnectError: [WinError 10061]")

    gone = monolith.health(transport=dead)
    assert gone.get("ok") is False, gone
    assert monolith.connection_guidance() in str(gone.get("error")), gone
    print("ok: health 探活——HTTP 响应算活，连接失败带指引")


def check_failure_class_mcp_down() -> None:
    row = {"ok": False, "error": "UE MCP 调用失败: HTTPStatusError: Server error '502 Bad Gateway'"}
    assert failure_class.failure_code(row) == "mcp_down", row

    row2 = {"ok": False, "error": "Source/AIAgentProject/Private/Test.cpp(502): error C3861: 'Foo'"}
    assert failure_class.failure_code(row2) == "compile", row2

    row3 = {"ok": False, "error": "UE MCP 调用失败: ConnectError: [WinError 10061]"}
    assert failure_class.failure_code(row3) == "mcp_down", row3
    print("ok: 502/ConnectError 归 mcp_down，行号 502 的编译错误仍归 compile")


def main() -> int:
    check_retry_once_then_success()
    check_retry_exhausted_gets_guidance()
    check_business_error_not_retried()
    check_connection_error_classifier()
    check_health_probe()
    check_failure_class_mcp_down()
    print("backend health ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
