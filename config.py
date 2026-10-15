# config.py
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def _load_env(path: Path) -> None:
    """读 ue-rag/.env。编辑器内嵌 Python 没有 dotenv 时用最小解析。"""
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except ImportError:
        pass
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().strip('"')


def _env_path(name: str, default: Path) -> Path:
    return Path(_env_str(name, str(default)))


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return float(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def allow_blueprint() -> bool:
    """每次读环境变量。缺省 false：不改蓝图，只写 Plan。"""
    return _env_bool("UE_ALLOW_BLUEPRINT", False)


def allow_material() -> bool:
    return _env_bool("UE_ALLOW_MATERIAL", False)


def allow_umg() -> bool:
    return _env_bool("UE_ALLOW_UMG", False)


def allow_anim() -> bool:
    return _env_bool("UE_ALLOW_ANIM", False)


def allow_niagara() -> bool:
    return _env_bool("UE_ALLOW_NIAGARA", False)


_load_env(PROJECT_ROOT / ".env")

DATA_DIR = PROJECT_ROOT / "data"
CHROMA_PATH = DATA_DIR / "chroma"
MCP_INDEX_PATH = DATA_DIR / "ue_mcp_index.json"
TRACES_DIR = DATA_DIR / "traces"

_DEFAULT_EDITOR = Path(r"D:\UEEngline\UE_5.82\UE_5.8\Engine\Binaries\Win64\UnrealEditor-Cmd.exe")
_DEFAULT_PROJECT = Path(r"E:\UEProject\AIWork\AIAgentProject\AIAgentProject.uproject")
_DEFAULT_SOURCE = Path(r"E:\UEProject\AIWork\AIAgentProject\Source\AIAgentProject")

UE_MCP_URL = _env_str("UE_MCP_URL", "http://127.0.0.1:8000/mcp")
UE_EDITOR_CMD = _env_path("UE_EDITOR_CMD", _DEFAULT_EDITOR)
UE_PROJECT = _env_path("UE_PROJECT", _DEFAULT_PROJECT)
UE_SOURCE_ROOT = _env_path("UE_SOURCE_ROOT", _DEFAULT_SOURCE)
UE_EXEC_MODE = _env_str("UE_EXEC_MODE", "real").lower()
UE_LLM_MODEL = _env_str("UE_LLM_MODEL", "deepseek-chat")
# 裁判 / 编译等待。普通 MCP 调用仍是 60 秒，见 UE_MCP_CALL_TIMEOUT。
UE_MCP_TIMEOUT = _env_float("UE_MCP_TIMEOUT", 300.0)
UE_MCP_CALL_TIMEOUT = _env_float("UE_MCP_CALL_TIMEOUT", 60.0)
UE_ALLOW_BLUEPRINT = allow_blueprint()

# UnrealEditor-Cmd.exe -> Win64 -> Binaries -> Engine -> 引擎根
UE_ENGINE_ROOT = _env_path("UE_ENGINE_ROOT", UE_EDITOR_CMD.parents[3])
UE_PROJECT_DIR = UE_PROJECT.parent

ASSET_JSONL = DATA_DIR / "ue_assets.jsonl"
CPP_JSONL = DATA_DIR / "ue_cpp.jsonl"
BP_GRAPH_JSONL = DATA_DIR / "ue_bp_graph.jsonl"
DOCS_JSONL = DATA_DIR / "ue_docs.jsonl"
ERRORS_JSONL = DATA_DIR / "ue_errors.jsonl"
SKILL_CARDS_PATH = DATA_DIR / "skill_cards.json"

DOCS_DIR = UE_PROJECT_DIR / "documents"
CONVENTIONS_PATH = UE_PROJECT_DIR / ".cursor" / "memory" / "conventions.md"
SKILLS_DIR = UE_PROJECT_DIR / ".cursor" / "skills"
REGEN_INDEX_PS1 = (
    UE_PROJECT_DIR / ".codex" / "skills" / "ue-low-token-skill" / "references" / "tools" / "regenerate_index.ps1"
)
SKILL_MCP_INDEX = (
    UE_PROJECT_DIR / ".codex" / "skills" / "ue-low-token-skill" / "references" / "ue_mcp_index.json"
)
