# config.py
import json
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


def allow_ai() -> bool:
    """行为树、黑板、StateTree、EQS、感知、导航。缺省 false。"""
    return _env_bool("UE_ALLOW_AI", False)


def allow_monolith_write() -> bool:
    """Monolith 其余命名空间的非只读 action。缺省 false。official 时不放行任何官方工具。"""
    return _env_bool("UE_ALLOW_MONOLITH_WRITE", False)

def default_save() -> bool:
    """任务级 save 缺省。全闭环默认允许保存；UE_DEFAULT_SAVE=false 退回只改内存。"""
    return _env_bool("UE_DEFAULT_SAVE", True)

def default_pie() -> bool:
    """任务级 pie 缺省。全闭环默认允许进 PIE；UE_DEFAULT_PIE=false 退回不开 PIE。"""
    return _env_bool("UE_DEFAULT_PIE", True)

def task_flag(task: dict | None, key: str) -> bool:
    """读任务上的 save / pie。没写就取缺省，写了就按写的。"""
    value = (task or {}).get(key)
    if isinstance(value, bool):
        return value
    return default_save() if key == "save" else default_pie()

def task_policy(*, save: bool | None = None, pie: bool | None = None) -> dict:
    """造任务字典时展开：{"save": ..., "pie": ...}，缺省跟随上面两个开关。"""
    return {
        "save": default_save() if save is None else bool(save),
        "pie": default_pie() if pie is None else bool(pie),
    }


_load_env(PROJECT_ROOT / ".env")


def editor_backend() -> str:
    """每次读 .env。键不在文件里才看进程环境变量。空字符串等于没写，缺省 official。"""
    from core.graph_write_policy import _ENV, env_value, normalize_backend

    raw = env_value(_ENV, "UE_EDITOR_BACKEND")
    if raw is None:
        raw = os.getenv("UE_EDITOR_BACKEND")
    return normalize_backend(raw)


def _file_or_env(name: str, default: str) -> str:
    from core.graph_write_policy import _ENV, env_value

    raw = env_value(_ENV, name)
    if raw is None or not str(raw).strip():
        raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().strip('"').strip("'")


def monolith_url() -> str:
    """仅 backend=monolith 时作为编辑器地址。缺省 9316。"""
    return _file_or_env("UE_MONOLITH_URL", "http://127.0.0.1:9316/mcp")


def monolith_root() -> Path:
    """说明书与离线查询的源码位置。已加载的插件在工程 Plugins/Monolith。"""
    return Path(_file_or_env("UE_MONOLITH_ROOT", r"E:\UEProject\AIWork\RAG\monolith-master"))

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
PROJECT_CONTEXT_PATH = DATA_DIR / "project_context.json"
AGENT_SKILL_PATH = DATA_DIR / "agent_skill.json"


def project_context() -> dict:
    """工程身份。模块、当前关卡来自导出的 project_context，不写死工程名。"""
    try:
        data = json.loads(PROJECT_CONTEXT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def project_module() -> str:
    name = str(project_context().get("module") or "").strip()
    return name or UE_SOURCE_ROOT.name


def source_rel_prefix() -> str:
    """源码根相对工程目录，例如 Source/模块名/。路径对不上时用模块名拼，不写死某一份工程。"""
    try:
        rel = UE_SOURCE_ROOT.resolve().relative_to(UE_PROJECT_DIR.resolve()).as_posix().strip("/")
    except ValueError:
        rel = f"Source/{project_module()}"
    return f"{rel}/" if rel else ""


def project_level_package() -> str:
    """当前关卡包路径。level_name 是 L_ 名时落到 /Game/Maps。没有就不编一个默认关卡。"""
    name = str(project_context().get("level_name") or "").strip().replace("\\", "/")
    if name.startswith("/Game/"):
        return name.split(".", 1)[0]
    if name.startswith("L_"):
        return f"/Game/Maps/{name}"
    return ""


def script_object_path(class_name: str) -> str:
    """工程类的反射路径。模块名来自 project_module。"""
    bare = str(class_name or "").strip()
    if bare.startswith("/Script/"):
        return bare
    if len(bare) > 1 and bare[0] in "AUF" and bare[1].isupper():
        bare = bare[1:]
    return f"/Script/{project_module()}.{bare}"

DOCS_DIR = UE_PROJECT_DIR / "documents"
CURSOR_MEMORY = UE_PROJECT_DIR / ".cursor" / "memory"
CODEX_MEMORY = UE_PROJECT_DIR / ".codex" / "memory"
# 人读约定仍以 .cursor 这份为准。若 .codex 下另有一份，导出时一并收入。
CONVENTIONS_PATH = CURSOR_MEMORY / "conventions.md"
CONVENTIONS_PATHS = (
    CODEX_MEMORY / "conventions.md",
    CONVENTIONS_PATH,
)
CODEX_SKILLS_DIR = UE_PROJECT_DIR / ".codex" / "skills"
CURSOR_SKILLS_DIR = UE_PROJECT_DIR / ".cursor" / "skills"
# 卡片以 .codex 为源；同名技能不再读 .cursor 那份。
SKILL_DIRS = tuple(
    path for path in (CODEX_SKILLS_DIR, CURSOR_SKILLS_DIR) if path.is_dir()
)
SKILLS_DIR = SKILL_DIRS[0] if SKILL_DIRS else CODEX_SKILLS_DIR


def task_record_dirs() -> tuple[Path, ...]:
    """闭环镜像在前，人读的 .cursor 记录在后。缺目录也返回，调用方自己判断文件在不在。"""
    return (CODEX_MEMORY / "任务记录", CURSOR_MEMORY / "任务记录")
REGEN_INDEX_PS1 = (
    UE_PROJECT_DIR / ".codex" / "skills" / "ue-low-token-skill" / "references" / "tools" / "regenerate_index.ps1"
)
SKILL_MCP_INDEX = (
    UE_PROJECT_DIR / ".codex" / "skills" / "ue-low-token-skill" / "references" / "ue_mcp_index.json"
)
