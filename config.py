# config.py
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


# 进程里显式钉住的 mock 不得被 .env 覆盖。
# UE_EXEC_MODE 是「这次进程要不要真连编辑器」的开关：离线检查在 import config 之前
# 就把它设成 mock。override=True 会把它盖回 .env 里的 real，于是自称 mock 的检查
# 一路走到真编辑器（20261103 实证：check_dsh_bridge 打到 127.0.0.1:8000，报
# "EnemySpawnPoint_1 is not a valid object path"）。子进程（_cli 起的 dsh_cli.py）
# 继承 os.environ，本来就带着 mock，同样需要这层保护才不会被自己的 .env 翻回 real。
#
# 只保护 mock 这一个方向，不是「进程环境变量一律优先」：反方向（进程是 real、.env 是
# mock）仍以 .env 为准。否则 shell 里残留一个 UE_EXEC_MODE=mock 就会让真任务静默
# 跑在假执行器上产生假成功——那是比「检查误连编辑器」更坏的失败。
_MOCK_ONLY_PINNED = ("UE_EXEC_MODE",)


def _pinned_mock() -> dict:
    """取出进程里被显式钉成 mock 的键，留给 _load_env 在读完 .env 后放回去。"""
    return {
        key: os.environ[key]
        for key in _MOCK_ONLY_PINNED
        if str(os.environ.get(key) or "").strip().lower() == "mock"
    }


def _load_env(path: Path) -> None:
    """读 ue-rag/.env。编辑器内嵌 Python 没有 dotenv 时用最小解析。"""
    if not path.is_file():
        return
    pinned = _pinned_mock()
    try:
        from dotenv import load_dotenv
        # 20261028：override=True。系统/User 级环境变量里的旧 DEEPSEEK_API_KEY 会把
        # .env 的新 key 盖掉（load_dotenv 默认不覆盖），理解器因此一直 401。以 .env 为准。
        load_dotenv(path, override=True)
        # 除 _MOCK_ONLY_PINNED 以外都以上面的 .env 为准，这里只把显式钉成 mock 的键放回去。
        os.environ.update(pinned)
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
            os.environ[key] = value
    os.environ.update(pinned)


ENV_PATH = PROJECT_ROOT / ".env"
# 【计划外改动，20261111 W1】进程级注入点，只有这一个用途：离线检查（check_scorecard 的
# 健康探针、check_scoring_integrity）要起一个「配置自由」的子进程去打一次真实 /api/health，
# 不给注入点就造不出「某项探活 ok=false」的假，P2(b) 会退化成恒真。不给的话下面所有路径
# 与改动前逐字同名同值：ENV_PATH = PROJECT_ROOT/".env"。
_ENV_OVERRIDE = os.environ.get("UE_DOTENV_PATH", "").strip()
if _ENV_OVERRIDE:
    ENV_PATH = Path(_ENV_OVERRIDE).resolve()


def env_value(env_path: Path, key: str) -> str | None:
    """每次读 .env 里的原文。键不在文件里返回 None；在但为空返回空字符串。

    标准库实现，config 不得引用 core（架构边界）；core 反过来从这里取。"""
    if not env_path.is_file():
        return None
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    found: str | None = None
    seen = False
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, value = text.split("=", 1)
        if name.strip() != key:
            continue
        seen = True
        found = value.strip().strip('"').strip("'")
    if not seen:
        return None
    return found


def normalize_backend(raw: str | None) -> str:
    """空字符串等于没写。非法拼写保留小写，交给选择层拒绝。"""
    if raw is None or not str(raw).strip():
        return "official"
    return str(raw).strip().strip('"').strip("'").lower()


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().strip('"')


def _env_path(name: str, default: Path) -> Path:
    """环境变量为空或没写时返回 default 本身（不是一个字符串化的 default）。"""
    raw = _env_str(name, "")
    return Path(raw) if raw else default

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


def allow_python() -> bool:
    """editor.run_python 的缺省放行（UE_ALLOW_PYTHON，缺省 false）。

    任务上显式写 allow_python: false 的仍然拒绝——DSH 的写策略以 session_task.json
    为唯一来源，.env 只是给多窗口/求解扇这类「任务上没写这个键」的路径一个缺省。"""
    return _env_bool("UE_ALLOW_PYTHON", False)

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

def _save_pie(*, save: bool | None, pie: bool | None) -> dict:
    return {
        "save": default_save() if save is None else bool(save),
        "pie": default_pie() if pie is None else bool(pie),
    }


def _policy_writes(value) -> list:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _dsh_session_path() -> Path:
    """与 server/dsh_bridge.py 同一条：UE_DSH_TASK_PATH，否则数据目录下的 session_task.json。"""
    override = os.environ.get("UE_DSH_TASK_PATH", "").strip()
    if override:
        return Path(override)
    data_override = os.environ.get("UE_DSH_DATA_DIR", "").strip()
    root = Path(data_override) if data_override else DATA_DIR
    return root / "dsh" / "session_task.json"


def _dsh_policy() -> dict:
    """只认 session_task.json。缺文件、读失败、不是对象，都是全拒绝。不看调用方 task。"""
    deny = {"allow_writes": [], "save": False, "pie": False}
    path = _dsh_session_path()
    if not path.is_file():
        return dict(deny)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(deny)
    if not isinstance(data, dict):
        return dict(deny)
    return {
        "allow_writes": _policy_writes(data.get("allow_writes")),
        "save": bool(data.get("save")),
        "pie": bool(data.get("pie")),
    }


def run_policy(entry: str, task: dict | None = None) -> dict:
    """两套剖面，同一组键：allow_writes、save、pie。两边的 bool 不必相等。

    menu：save/pie 等于 task_policy，allow_writes 用调用方已经放进 task 的那份。
    dsh：只读 session_task.json。task 参数里的 allow_writes / save / pie 一律忽略。
    """
    if entry == "dsh":
        return _dsh_policy()
    if entry != "menu":
        raise ValueError(f"未知入口 {entry}")
    body = task or {}
    save = body.get("save") if isinstance(body.get("save"), bool) else None
    pie = body.get("pie") if isinstance(body.get("pie"), bool) else None
    flags = _save_pie(save=save, pie=pie)
    return {
        "allow_writes": _policy_writes(body.get("allow_writes")),
        "save": flags["save"],
        "pie": flags["pie"],
    }


def task_policy(*, save: bool | None = None, pie: bool | None = None) -> dict:
    """薄包装：只要菜单剖面的 save / pie。缺省跟随 default_save / default_pie。"""
    policy = run_policy("menu", {
        **({} if save is None else {"save": bool(save)}),
        **({} if pie is None else {"pie": bool(pie)}),
    })
    return {"save": policy["save"], "pie": policy["pie"]}


_load_env(ENV_PATH)


def editor_backend() -> str:
    """每次读 .env。键不在文件里才看进程环境变量。空字符串等于没写，缺省 official。"""
    raw = env_value(ENV_PATH, "UE_EDITOR_BACKEND")
    if raw is None:
        raw = os.getenv("UE_EDITOR_BACKEND")
    return normalize_backend(raw)


def _file_or_env(name: str, default: str) -> str:
    raw = env_value(ENV_PATH, name)
    if raw is None or not str(raw).strip():
        raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().strip('"').strip("'")


def llm_base_url() -> str:
    """每次读 .env。模型 API 地址（OpenAI 兼容）。缺省 DeepSeek 官方。"""
    return _file_or_env("UE_LLM_BASE_URL", "https://api.deepseek.com")


def eval_base_url() -> str:
    """评测专用 API 地址。没写跟随 UE_LLM_BASE_URL，再缺省 DeepSeek 官方。"""
    raw = env_value(ENV_PATH, "UE_EVAL_BASE_URL")
    if raw is None or not str(raw).strip():
        raw = os.getenv("UE_EVAL_BASE_URL")
    if raw is None or not str(raw).strip():
        return llm_base_url()
    return str(raw).strip().strip('"').strip("'")


def monolith_url() -> str:
    """仅 backend=monolith 时作为编辑器地址。缺省 9316。"""
    return _file_or_env("UE_MONOLITH_URL", "http://127.0.0.1:9316/mcp")


def monolith_root() -> Path:
    """说明书与离线查询的源码位置。已加载的插件在工程 Plugins/Monolith。"""
    return Path(_file_or_env("UE_MONOLITH_ROOT", r"E:\UEProject\AIWork\RAG\monolith-master"))

# 【计划外改动，20261111 W1】DATA_DIR / MCP_INDEX_PATH 各多一个环境变量注入点，
# 理由同上（健康探针要能把索引指到一个不存在的文件，才造得出 index_present.ok=false）。
# 不设这两个变量时，行为与改动前逐字等价：
#   DATA_DIR       = PROJECT_ROOT / "data"
#   MCP_INDEX_PATH = DATA_DIR / "ue_mcp_index.json"
DATA_DIR = _env_path("UE_DATA_DIR", PROJECT_ROOT / "data")
CHROMA_PATH = DATA_DIR / "chroma"
MCP_INDEX_PATH = _env_path("UE_MCP_INDEX_PATH", DATA_DIR / "ue_mcp_index.json")
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
