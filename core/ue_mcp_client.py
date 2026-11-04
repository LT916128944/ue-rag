# core/ue_mcp_client.py
"""
UE 官方 MCP (ModelContextProtocol) 客户端。

适配 UE 5.8 的 Streamable HTTP 有状态传输模式。
流程：initialize 握手 -> 获取 Mcp-Session-Id -> 携带 ID 调用工具。
"""
import httpx
import json
import os
import sys
import uuid
from typing import Any

def _log(msg: str) -> None:
    print(f"[UEMCP] {msg}", file=sys.stderr)


def is_connection_error(e: BaseException) -> bool:
    """连接类错误：传输层故障或 5xx 网关错误。允许连接层一次重试，不扩业务重试。"""
    if isinstance(e, httpx.TransportError):
        return True
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code >= 500
    return False


class UEOfficialMCPClient:
    def __init__(self):
        # 默认使用官方文档中的地址和端口。
        # 普通调用 60 秒。UE_MCP_TIMEOUT 留给裁判，避免一次编译把所有工具调用拉到 300 秒。
        self.url = os.getenv("UE_MCP_URL", "http://127.0.0.1:8000/mcp") or "http://127.0.0.1:8000/mcp"
        call_timeout = os.getenv("UE_MCP_CALL_TIMEOUT", "60")
        self.timeout = float(call_timeout) if str(call_timeout).strip() else 60.0
        self._session_id: str | None = None
        self._http = httpx.Client(timeout=self.timeout)

    def _ensure_session(self) -> str:
        """确保已建立会话，返回 Mcp-Session-Id。"""
        if self._session_id:
            return self._session_id

        # 1. 发送 initialize 请求
        init_payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",  # 对齐 UE 5.8 支持的版本
                "capabilities": {},
                "clientInfo": {"name": "ue-rag-agent", "version": "0.1.0"},
            },
        }
        _log("正在进行 initialize 握手...")
        resp = self._http.post(
            self.url,
            json=init_payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        resp.raise_for_status()

        # 2. 从响应头提取会话 ID
        session_id = resp.headers.get("mcp-session-id")
        if not session_id:
            # 如果服务器不返回 ID（无状态模式），则跳过
            _log("服务器未返回 Mcp-Session-Id，将按无状态模式工作。")
            self._session_id = ""  # 用空字符串表示无状态
            return ""

        self._session_id = session_id
        _log(f"会话已建立，Mcp-Session-Id = {session_id}")

        # 3. 发送 initialized 通知（协议要求，无响应）
        self._send_notification("notifications/initialized", {})
        return session_id

    def _send_notification(self, method: str, params: dict) -> None:
        """发送 JSON-RPC 通知（无 id，无响应）。"""
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        headers = self._build_headers()
        try:
            self._http.post(self.url, json=payload, headers=headers)
        except Exception as e:
            _log(f"发送通知 {method} 失败: {e}")

    def _build_headers(self) -> dict:
        """构建包含会话 ID 的请求头。"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _request(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        """发送 JSON-RPC 请求并返回结果。"""
        self._ensure_session()
        if timeout is not None:
            self._http.timeout = httpx.Timeout(timeout)

        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        resp = self._http.post(self.url, json=payload, headers=self._build_headers())

        # 会话过期时自动重连一次
        if resp.status_code in (404, 400) and "session" in resp.text.lower():
            _log("会话可能已过期，正在重新初始化...")
            self._session_id = None
            self._ensure_session()
            resp = self._http.post(self.url, json=payload, headers=self._build_headers())

        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")

        if "text/event-stream" in content_type:
            return self._parse_sse(resp.text)

        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result")

    def _parse_sse(self, text: str) -> Any:
        """解析 SSE 格式的响应。"""
        result = None
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                data_str = line[len("data:"):].strip()
                if data_str:
                    try:
                        data = json.loads(data_str)
                        if "result" in data:
                            result = data["result"]
                        elif "error" in data:
                            raise RuntimeError(f"MCP error: {data['error']}")
                    except json.JSONDecodeError:
                        continue
        return result

    # ---- 公开 API ----
    def list_tools(self) -> list[dict]:
        """列出所有可用工具。"""
        result = self._request("tools/list", {})
        return result.get("tools", []) if result else []

    def call_tool(self, name: str, arguments: dict) -> dict:
        """调用指定工具。"""
        try:
            result = self._request("tools/call", {"name": name, "arguments": arguments})
            # 归一化返回内容
            texts = []
            for item in (result or {}).get("content", []):
                if item.get("type") == "text":
                    texts.append(item.get("text", ""))
            raw = "\n".join(texts)
            try:
                data = json.loads(raw)
            except Exception:
                data = raw
            if result and result.get("isError"):
                return {"ok": False, "error": data}
            return {"ok": True, "data": data}
        except Exception as e:
            failed = {"ok": False, "error": f"UE MCP 调用失败: {type(e).__name__}: {e}"}
            if is_connection_error(e):
                failed["connection_error"] = True
            return failed

    def call_ue_tool(self, toolset_name: str, tool_name: str, arguments: dict, timeout: float | None = None) -> dict:
        """通过 `call_tool` 元工具转发真实 UE 工具调用。

        Args:
            toolset_name: toolset 全名，如 'ToolsetRegistry.AgentSkillToolset'
                          传空字符串表示调用顶层 MCP 工具
            tool_name:    短名，如 'UpdateSkill'
            arguments:    参数字典
        """
        call_args: dict = {
            "tool_name": tool_name,
            "arguments": arguments or {},
        }
        if toolset_name:
            call_args["toolset_name"] = toolset_name

        try:
            result = self._request("tools/call", {
                "name": "call_tool",
                "arguments": call_args,
            }, timeout=timeout)
            # 归一化返回
            texts = []
            for item in (result or {}).get("content", []):
                if item.get("type") == "text":
                    texts.append(item.get("text", ""))
            raw = "\n".join(texts)
            try:
                data = json.loads(raw)
            except Exception:
                data = raw
            if result and result.get("isError"):
                return {"ok": False, "error": data}
            return {"ok": True, "data": data}
        except Exception as e:
            return {"ok": False, "error": f"call_ue_tool 失败: {type(e).__name__}: {e}"}
    
    def close(self):
        self._http.close()


# ---- 单例 ----
_client: UEOfficialMCPClient | None = None

def get_ue_mcp_client() -> UEOfficialMCPClient:
    global _client
    if _client is None:
        _client = UEOfficialMCPClient()
    return _client


def reset_ue_mcp_client() -> None:
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
    _client = None