"""能力面视图生成器（20261028 P1-5）：跑 monolith 全 namespace discover，落机读 JSON。

CATALOG 是人工背下来的清单；执行器真实面（monolith 各 namespace 的 action）与它之间
此前只有「已声明项」的对账，没有「未声明项」的发现机制——Niagara 的能力在插件里躺了
三个月，规划者一直以为不存在。这里把执行器的真实面导出成
``data/capability_surface.json``（action 名、描述），eval/check_capability_surface.py
拿它对账 CATALOG：执行器有、规划者看不见 → 自动提名进 action_candidates；
CATALOG 声明了 surface 原语而执行器没有 → parity 报错。

需要编辑器在线（monolith MCP 在 9316）。输出只留决策字段（action 名 + 一行描述）。
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def surface_path() -> Path:
    """能力面 JSON 的位置。实现搬到了 core（20261029 边界整改），这里只是转出。"""
    from core.capability_surface import surface_path as _path

    return _path()


def _backend_name() -> str:
    """与 solver.load_describe_cache 的指纹同一口径：缺省 official。"""
    from core.capability_surface import backend_fingerprint

    return backend_fingerprint()


def _action_row(namespace: str, item: dict) -> dict:
    """action 名、一行描述，describe 成功时带上 params 形状。失败就省略 params。"""
    row = {
        "name": str(item["name"]),
        "description": str(item.get("description") or "")[:150],
    }
    try:
        from backends.monolith import describe

        found = describe(namespace, row["name"])
        params = found.get("params") if isinstance(found, dict) else None
        if isinstance(params, dict) and params:
            row["params"] = params
    except Exception:
        pass
    return row


def export() -> dict:
    from backends.monolith import discover

    names = discover("")
    if not names.get("ok"):
        raise RuntimeError(str(names.get("error") or "monolith_discover 失败"))
    namespaces: dict[str, list[dict]] = {}
    errors: list[str] = []
    for ns in names.get("namespaces") or []:
        name = str(ns).strip()
        if not name:
            continue
        rows = discover(name)
        if not rows.get("ok"):
            errors.append(f"{name}: {str(rows.get('error') or '')[:120]}")
            continue
        actions = []
        for item in rows.get("actions") or []:
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                continue
            actions.append(_action_row(name, item))
        namespaces[name] = actions
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "backend": _backend_name(),
        "namespace_count": len(namespaces),
        "action_count": sum(len(rows) for rows in namespaces.values()),
        "namespaces": namespaces,
        "errors": errors,
    }
    target = surface_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return payload


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        payload = export()
    except Exception as exc:
        print(f"capability surface FAILED：{type(exc).__name__}: {exc}")
        return 1
    print(
        f"capability surface ok：{payload['namespace_count']} 个 namespace、"
        f"{payload['action_count']} 个 action → {surface_path()}"
    )
    for line in payload.get("errors") or []:
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())