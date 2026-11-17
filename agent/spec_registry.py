# agent/spec_registry.py
"""全域 Spec 内核登记表（20261117 边界升级总览 §3/§4）。

一处登记、处处可查：每个域一个 ``agent/<域>_spec.py`` 模块，模块提供统一的
KIND / SPEC_HINT / validate_spec / plan_ops / exec_op / assertions / verify_spec /
recognize / write_task / build 接口（见 agent/spec_pipeline.py 文件头）。

本表只做「发现与分派」，不含任何域的业务逻辑：
  - ``describe()``     给理解和规划看的「本域 Spec 结构 + 可用性」清单；
  - ``all_hints()``    拼接所有 SPEC_HINT，喂给理解器/规划器（让模型按域产 Spec）；
  - ``recognize(text)`` 一句话稳定归属哪个域（宁缺毋滥：多域同时命中即判歧义、交回模型）；
  - ``build(kind, step)`` 按域跑完整闭环（validate→kernel→verify→repair→终态）。

**逐域惰性 import**：某个域模块写坏了，只让那一个域不可用，绝不连坐整张表——
这保证「一个域缺口不会把其它域的兜住能力一起拖垮」。
"""
from __future__ import annotations

import importlib

# 顺序即文档顺序；与总览 §4 的分模块顺序一致（图→材质/Niagara→动画/Sequencer/AnimBP→后处理→C++）。
_DOMAINS: tuple[tuple[str, str], ...] = (
    ("umg", "agent.umg_spec"),
    ("graph", "agent.graph_spec"),
    ("animgraph", "agent.animgraph_spec"),
    ("material", "agent.material_spec"),
    ("niagara", "agent.niagara_spec"),
    ("animation", "agent.animation_spec"),
    ("sequencer", "agent.sequencer_spec"),
    ("postprocess", "agent.postprocess_spec"),
    ("cpp", "agent.cpp_spec"),
)

DOMAIN_NAMES: tuple[str, ...] = tuple(name for name, _ in _DOMAINS)


def try_load(name: str) -> tuple[object | None, str]:
    """惰性加载一个域模块。返回 (模块, 错误串)。错误串非空表示该域今天不可用。"""
    for domain, module_path in _DOMAINS:
        if domain != name:
            continue
        try:
            mod = importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 —— 单域失败不许连坐
            return None, f"{module_path} 导入失败：{type(exc).__name__}: {exc}"
        return mod, ""
    return None, f"未知域 {name!r}（可选：{', '.join(DOMAIN_NAMES)}）"


def _kind_of(mod) -> str:
    return str(getattr(mod, "KIND", "") or "")


def describe() -> dict:
    """每域一行：是否可导入、KIND、judge_kind、是否有 SPEC_HINT。离线可调用。"""
    out: dict = {}
    for name in DOMAIN_NAMES:
        mod, err = try_load(name)
        if mod is None:
            out[name] = {"ok": False, "error": err}
            continue
        out[name] = {
            "ok": True,
            "kind": _kind_of(mod),
            "judge_kind": str(getattr(mod, "JUDGE_KIND", "") or ""),
            "has_hint": bool(str(getattr(mod, "SPEC_HINT", "") or "").strip()),
        }
    return out


def all_hints() -> list[tuple[str, str]]:
    """[(域, SPEC_HINT)]，给理解器/规划器注入。导入失败的域跳过（附一行说明）。"""
    rows: list[tuple[str, str]] = []
    for name in DOMAIN_NAMES:
        mod, err = try_load(name)
        if mod is None:
            continue
        hint = str(getattr(mod, "SPEC_HINT", "") or "").strip()
        if hint:
            rows.append((name, hint))
    return rows


def recognize(text: str) -> str | None:
    """这句话稳定属于哪个域。多域同时命中=歧义 → None（交回模型，不硬猜）。"""
    blob = str(text or "").strip()
    if not blob:
        return None
    hits: list[str] = []
    for name in DOMAIN_NAMES:
        mod, _ = try_load(name)
        if mod is None:
            continue
        fn = getattr(mod, "recognize", None)
        if fn is None:
            continue
        try:
            if fn(blob):
                hits.append(name)
        except Exception:  # noqa: BLE001 —— 单域识别异常不影响其它域
            continue
    if len(hits) == 1:
        return hits[0]
    return None


def build(kind: str, step: dict, **kwargs) -> dict:
    """按域跑完整闭环。kind 命中不到域就结构化失败（不猜）。"""
    for name in DOMAIN_NAMES:
        mod, err = try_load(name)
        if mod is None:
            continue
        if _kind_of(mod) == kind or name == kind:
            return mod.build(step, **kwargs)
    return {
        "ok": False, "phase": "registry", "domain": str(kind), "paused": True,
        "reason": f"没有登记域 {kind!r} 的 Spec 内核", "errors": [], "tools": [], "exec_count": 0,
    }
