"""Monolith 入口：discover、describe、exec。不按 action 手写函数，也不回退 8000。"""
from __future__ import annotations

import json

from backends.policy import unfiltered_graph, write_block
from backends.select import monolith_refusal

_PCG = "Monolith 无此命名空间"
_GUIDANCE = "；检查 UnrealEditor 是否在跑、Monolith 插件日志是否出现 listening on port 9316"


def connection_guidance() -> str:
    """连接类失败最终附带的固定指引。"""
    return _GUIDANCE


def _connection_failed(raw: dict) -> bool:
    return isinstance(raw, dict) and raw.get("ok") is False and bool(raw.get("connection_error"))


def _send_with_retry(tool_name: str, arguments: dict, send) -> dict:
    """连接层一次重试。_call 每次新建客户端，重试等价于全新连接。

    业务语义不变：铁律「遇错即停，最多重试一次」的额度只花在这里，不进 fix/plan。
    20261028-163746 起加自愈：连接被拒（编辑器没跑/编译后被关）先 ensure_editor
    一次再重试——编辑器没开是环境前置，不是能力缺口，不该让整扇窗口陪葬。
    自愈只认连接类失败，业务错误照常直接返回。"""
    raw = send(tool_name, arguments)
    if not _connection_failed(raw):
        return raw
    healed = _ensure_editor_quiet()
    retry = send(tool_name, arguments)
    if _connection_failed(retry) and healed:
        retry["error"] = f"{retry.get('error') or 'monolith 调用失败'}（自愈启动过编辑器仍未连通）{_GUIDANCE}"
    elif _connection_failed(retry):
        retry["error"] = f"{retry.get('error') or 'monolith 调用失败'}{_GUIDANCE}"
    return retry


def _ensure_editor_quiet() -> bool:
    """拉起编辑器并等端口。只认真拉过（started 或已连通）才返回 True；失败不抛。"""
    try:
        from core.editor_launch import ensure_editor

        out = ensure_editor()
        return bool(out.get("ok"))
    except Exception:
        return False


def health(timeout: float = 3.0, transport=None) -> dict:
    """轻量探活：任何 HTTP 响应（含 404/405）都算编辑器侧在听。

    不做 MCP 握手、不消耗一次编辑器调用的 8 次预算。"""
    import httpx

    from config import monolith_url

    probe = transport or (lambda url: httpx.get(url, timeout=timeout))
    try:
        resp = probe(monolith_url())
    except Exception as e:  # ConnectError / timeout 等都算探活失败
        return {"ok": False, "error": f"{type(e).__name__}: {e}{_GUIDANCE}"}
    return {"ok": True, "status": getattr(resp, "status_code", 0)}


def discover_arguments(namespace: str = "") -> dict:
    """不传 detail / verbose。带 namespace 时只问这一个。"""
    args: dict = {}
    text = (namespace or "").strip()
    if text:
        args["namespace"] = text
    return args


def _one_line(text: str) -> str:
    line = " ".join((text or "").split())
    for mark in ("。", ". "):
        if mark in line:
            line = line.split(mark, 1)[0]
            break
    if len(line) > 150:
        line = line[:150] + "..."
    return line


def _as_object(data):
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return data
    return data


def _discover_decision(data, namespace: str) -> dict:
    data = _as_object(data)
    if not isinstance(data, dict):
        text = " ".join(str(data or "").split())
        return {"ok": True, "data": text[:200]}
    if not (namespace or "").strip():
        names = []
        for item in data.get("namespaces") or []:
            if isinstance(item, dict) and item.get("namespace"):
                names.append(str(item["namespace"]))
            elif isinstance(item, str):
                names.append(item)
        return {"ok": True, "namespaces": names, "count": len(names)}
    actions = []
    for item in data.get("actions") or []:
        if isinstance(item, str):
            actions.append({"name": item, "description": ""})
        elif isinstance(item, dict):
            actions.append({
                "name": str(item.get("name") or item.get("action") or ""),
                "description": _one_line(str(item.get("description") or "")),
            })
    return {"ok": True, "namespace": namespace, "actions": actions, "count": len(actions)}


def _call(tool_name: str, arguments: dict) -> dict:
    from config import monolith_url
    from core.ue_mcp_client import UEOfficialMCPClient

    client = UEOfficialMCPClient()
    client.url = monolith_url()
    try:
        return client.call_tool(tool_name, arguments or {})
    finally:
        client.close()


def _pcg(namespace: str) -> str:
    if (namespace or "").strip().lower() == "pcg":
        return _PCG
    return ""


def discover(namespace: str = "", transport=None) -> dict:
    refused = monolith_refusal()
    if refused:
        return {"ok": False, "error": refused}
    missing = _pcg(namespace)
    if missing:
        return {"ok": False, "error": missing}
    send = transport or _call
    raw = _send_with_retry("monolith_discover", discover_arguments(namespace), send)
    if not raw.get("ok"):
        return {"ok": False, "error": raw.get("error") or "monolith_discover 失败"}
    return _discover_decision(raw.get("data"), namespace)


def describe(namespace: str, action: str, transport=None) -> dict:
    refused = monolith_refusal()
    if refused:
        return {"ok": False, "error": refused}
    missing = _pcg(namespace)
    if missing:
        return {"ok": False, "error": missing}
    send = transport or _call
    raw = _send_with_retry("describe_query", {
        "action": "action_schema",
        "params": {"target_namespace": namespace, "target_action": action},
    }, send)
    if not raw.get("ok"):
        return {"ok": False, "error": raw.get("error") or "describe_query 失败"}
    data = _as_object(raw.get("data"))
    if not isinstance(data, dict):
        return {"ok": True, "namespace": namespace, "action": action, "params": {}}
    return {
        "ok": True,
        "namespace": str(data.get("namespace") or namespace),
        "action": str(data.get("action") or action),
        "params": data.get("params") if isinstance(data.get("params"), dict) else {},
    }


def _detail_blocked(params: dict) -> str:
    for key in ("detail", "verbose"):
        value = params.get(key)
        if value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}:
            return "禁止 detail=true"
    return ""


def _dispatch(namespace: str, action: str, params: dict) -> tuple[str, dict]:
    body = dict(params)
    body.pop("detail", None)
    body.pop("verbose", None)
    if namespace.lower() == "monolith":
        return f"monolith_{action}", body
    return f"{namespace}_query", {"action": action, "params": body}


def exec(namespace: str, action: str, params: dict | None = None, task: dict | None = None, transport=None) -> dict:
    refused = monolith_refusal()
    if refused:
        return {"ok": False, "error": refused}
    body = dict(params or {})
    detail = _detail_blocked(body)
    if detail:
        return {"ok": False, "error": detail}
    graph = unfiltered_graph(action, body)
    if graph:
        return {"ok": False, "error": graph}
    blocked = write_block(namespace, action, task)
    if blocked:
        return {"ok": False, "error": blocked}
    tool_name, arguments = _dispatch(namespace, action, body)
    send = transport or _call
    raw = _send_with_retry(tool_name, arguments, send)
    if not raw.get("ok"):
        return {"ok": False, "error": raw.get("error") or f"{tool_name} 失败"}
    return {"ok": True, "data": raw.get("data")}
