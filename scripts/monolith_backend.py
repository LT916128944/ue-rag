# Monolith 三条命令：discover / describe / exec。不连 8000。stdout 约 200 字。
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backends.monolith import describe, discover, exec as monolith_exec
from config import default_pie, default_save

CLIP = 200


def _clip(text: str, limit: int = CLIP) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _emit(payload: dict) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(_clip(json.dumps(payload, ensure_ascii=False)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Call one Monolith discover, describe, or exec.")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--exec", action="store_true")
    parser.add_argument("--namespace", default="")
    parser.add_argument("--action", default="")
    parser.add_argument("--args", default="{}")
    parser.add_argument("--allow-python", action="store_true")
    parser.add_argument("--save", dest="save", action="store_true")
    parser.add_argument("--no-save", dest="save", action="store_false")
    parser.add_argument("--pie", dest="pie", action="store_true")
    parser.add_argument("--no-pie", dest="pie", action="store_false")
    parser.set_defaults(save=None, pie=None)
    ns = parser.parse_args()
    chosen = sum(1 for flag in (ns.discover, ns.describe, ns.exec) if flag)
    if chosen != 1:
        _emit({"ok": False, "error": "只选 --discover、--describe、--exec 其中一个"})
        return 1
    try:
        params = json.loads(ns.args) if ns.args else {}
    except json.JSONDecodeError as exc:
        _emit({"ok": False, "error": f"bad --args json: {exc}"})
        return 1
    if not isinstance(params, dict):
        _emit({"ok": False, "error": "--args must be a JSON object"})
        return 1
    if ns.discover:
        payload = discover(ns.namespace)
    elif ns.describe:
        if not ns.namespace or not ns.action:
            _emit({"ok": False, "error": "--describe 需要 --namespace 和 --action"})
            return 1
        payload = describe(ns.namespace, ns.action)
    else:
        if not ns.namespace or not ns.action:
            _emit({"ok": False, "error": "--exec 需要 --namespace 和 --action"})
            return 1
        task = {
            "save": default_save() if ns.save is None else bool(ns.save),
            "pie": default_pie() if ns.pie is None else bool(ns.pie),
            "allow_python": bool(ns.allow_python),
        }
        payload = monolith_exec(ns.namespace, ns.action, params, task=task)
    _emit(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
