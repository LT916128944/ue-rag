# agent/model_fill.py
"""缺信息暂停前的「先问模型」兜底（20261117）：减少人工裁决。

现状：拆句/执行时遇到「写不出唯一路径 / 蓝图 / 源文件 / 过滤器」会暂停，等人敲 `r` 补一次。
本模块在把人拉进来之前，先用模型试着补出一个**形状合法**的值：
    - 补得出 → 当成人敲了 `r <值>` 直接往下走（少一次人工）；
    - 补不出 / 形状不合法 / 没有模型可用（离线检查把 `_complete_live` 换成抛异常的替身）
      → 返回 ""，调用方照旧走人工暂停，行为与改动前逐字一致。

只补「缺信息」，不补「能力缺口」：模型能说出一条 /Game 路径或 Public/x.h，但说不了
「这个功能怎么做」。返回前按 needs 做形状校验（源文件还要求磁盘上真的存在），宁缺勿猜。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_PATH_RE = re.compile(r"^/Game(?:/[A-Za-z0-9_.]+)+$")
_SOURCE_RE = re.compile(r"^(?:Public|Private)(?:/[A-Za-z0-9_.]+)*/[A-Za-z0-9_]+\.(?:h|cpp)$")
_FILTER_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

_SYSTEM = (
    "你是 UE5 项目的「缺失路径补全器」。用户给你：原句、动作名、缺什么（needs）、暂停原因。"
    "只输出一个补全值，不要解释、不要引号、不要代码块、不要多余的行：\n"
    "- needs=path / blueprint：输出形如 /Game/目录/BP_名字 的资产路径；句子里已有的 /Game 路径、"
    "L_ 名、BP_ 名优先沿用，不要编不相关目录。\n"
    "- needs=source：输出形如 Public/类名.h 或 Private/类名.cpp 的源码相对路径；只给这一条文件。\n"
    "- needs=filter：输出 Automation 过滤器全名，如 AIAgent.Fps.Hit。\n"
    "实在推不出就输出一个空行。"
)


def _norm_needs(needs: str) -> str:
    text = str(needs or "").strip().lower()
    if text in ("source", "filter"):
        return text
    return "path"  # path / blueprint / 其他一律按路径


def _clean(raw: str) -> str:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
    # 取第一行非空、去掉两端引号与代码块痕迹
    for line in text.splitlines():
        line = line.strip().strip("`").strip().strip('"').strip("'").strip()
        if line:
            return line.rstrip("/")
    return ""


def _source_exists(rel: str) -> bool:
    """源文件补全要求在 UE_SOURCE_ROOT 下真的存在（edit_cpp 只能改已有文件）。"""
    try:
        from config import UE_SOURCE_ROOT

        return (Path(UE_SOURCE_ROOT) / rel).is_file()
    except Exception:
        return True  # 拿不到源码根就不拦，保持「模型优先」；真正的失败由执行层如实报


def _shape_ok(needs: str, value: str) -> str:
    if not value:
        return ""
    if needs == "source":
        return value if _SOURCE_RE.match(value) and _source_exists(value) else ""
    if needs == "filter":
        return value if _FILTER_RE.match(value) else ""
    return value if _PATH_RE.match(value) else ""


def fill_missing(query: str, needs: str, *, error: str = "", action: str = "",
                 slots: dict | None = None, complete=None) -> str:
    """让模型补一个缺失值。返回合法补值或 ""（"" = 交回人工）。不抛异常。"""
    kind = _norm_needs(needs)
    body = {
        "原句": str(query or "").strip(),
        "动作": str(action or "").strip(),
        "needs": kind,
        "暂停原因": str(error or "").strip()[:400],
        "已知槽位": slots if isinstance(slots, dict) else {},
    }
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": json.dumps(body, ensure_ascii=False)},
    ]
    try:
        if complete is not None:
            raw = complete(messages)
        else:
            from agent.plan_windows import _complete_live

            raw = _complete_live(messages)
    except Exception:
        return ""
    return _shape_ok(kind, _clean(raw))
