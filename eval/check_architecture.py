# eval/check_architecture.py
"""依赖方向测试。用 ast 扫 core/ server/ pipelines/ agent/ config.py 的 import，含函数内 import。
不执行 UE，不调用模型。违反时对每条 import 打印 ❌ / ✅ FIX / 📖 See；通过只打印 architecture ok。"""
import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SEE = r"E:\UEProject\AIWork\AIAgentProject\documents\harness\boundaries.md"
WHITELIST_PATH = ROOT / "data" / "harness" / "arch_whitelist.json"

# 本仓库内部顶层包。其余（标准库与第三方）一律放行。
LOCAL_PACKAGES = {"agent", "core", "server", "pipelines", "eval", "config"}

# 每层可以 import 的本仓库包。eval/ 不限制。
ALLOWED = {
    "config": set(),
    "core": {"config", "core"},
    "server": {"config", "core", "server"},
    "pipelines": {"config", "core", "pipelines"},
    "agent": {"config", "core", "agent"},
}

# 白名单只允许这两条：core/editor_exec.py 的 agent.harness、server/gateway.py 的 agent.understand。
EXPECTED_WHITELIST = {
    ("core/editor_exec.py", "agent.harness"),
    ("server/gateway.py", "agent.understand"),
}

SCAN_DIRS = ("core", "server", "pipelines", "agent")


def fail(msg: str, fix: str) -> None:
    print(f"❌ {msg}")
    print(f"✅ FIX: {fix}")
    print(f"📖 See: {SEE}")


def _layer_of(path: Path) -> str:
    return "config" if path.name == "config.py" else path.parent.name


def _imports_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # 相对 import 按文件所在包装算。
                base = path.parent.name
                modules.append(f"{base}.{node.module}" if node.module else base)
            elif node.module:
                modules.append(node.module)
    return modules


def _load_whitelist() -> set[tuple[str, str]]:
    try:
        data = json.loads(WHITELIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return set()
    found = set()
    for item in entries:
        if isinstance(item, dict) and item.get("file") and item.get("import"):
            found.add((str(item["file"]), str(item["import"])))
    return found


def main() -> int:
    whitelist = _load_whitelist()
    if whitelist != EXPECTED_WHITELIST:
        fail(
            f"arch_whitelist.json 是 {sorted(whitelist)}，只允许 {sorted(EXPECTED_WHITELIST)} 这两条",
            "data/harness/arch_whitelist.json 改回 editor_exec 与 gateway 两条；新违规不进白名单，改 import 方向",
        )
        return 1

    errors: list[tuple[Path, str, str]] = []
    targets = [ROOT / "config.py"]
    for folder in SCAN_DIRS:
        targets.extend(sorted((ROOT / folder).glob("*.py")))
    for path in targets:
        layer = _layer_of(path)
        rel = path.relative_to(ROOT).as_posix()
        for module in _imports_of(path):
            top = module.split(".")[0]
            if top not in LOCAL_PACKAGES:
                continue
            if (rel, module) in whitelist:
                continue
            if top in ALLOWED.get(layer, set()):
                continue
            errors.append((path, module, layer))
    if errors:
        for path, module, layer in errors:
            rel = path.relative_to(ROOT).as_posix()
            fail(
                f"{rel} 引用 {module}（{layer} 只能引用 {'/'.join(sorted(ALLOWED[layer])) or '标准库与第三方'}）",
                f"{rel} 的 import {module} 改成从允许的包拿，或把常量搬到 core；不允许就删这条引用",
            )
        return 1
    print("architecture ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
