# agent/supervise.py
"""把一句话收成任务 JSON。路径必须在语料或源码树里唯一，否则返回错误，不猜。"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# 20261028：不裸调 load_dotenv()——它按 CWD 自己找 .env，会把 eval 封闭环境
# （eval/_pin_offline.close_now 指向的空临时 .env）打穿，.env 开关重新渗回断言。
# 统一从 config.ENV_PATH 读：入口封闭时这里自然也封闭。
from config import ENV_PATH as _UE_RAG_ENV_PATH
load_dotenv(str(_UE_RAG_ENV_PATH), override=True)

from config import UE_PROJECT_DIR, UE_SOURCE_ROOT, allow_blueprint, task_flag, task_policy
from core.retrieve_funnel import _corpus

_BP_RE = re.compile(r"(?<![A-Za-z0-9_])BP_[A-Za-z0-9_]+(?![A-Za-z0-9_])")
_CLASS_RE = re.compile(r"(?<![A-Za-z0-9_])A[A-Z][A-Za-z0-9_]*(?![A-Za-z0-9_])")
_FILE_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9_]+)\.(?:h|cpp)(?![A-Za-z0-9_])")
_PATH_STRIP_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z0-9_\\/\-]+(?:[\\/][A-Za-z0-9_\-]+)+\.(?:h|cpp)(?![A-Za-z0-9_])"
)
_LOOSE_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Za-z0-9_]*Actor)(?![A-Za-z0-9_])")
_LEVEL_RE = re.compile(r"(?<![A-Za-z0-9_])(L_[A-Za-z0-9_]+)(?![A-Za-z0-9_])")
_FILTER_RE = re.compile(r"(?:过滤器|automation_filter|filter)\s*[:：=]?\s*([A-Za-z0-9_.]+)")
_OTHER_GRAPH_RE = re.compile(
    r"材质图|材质节点|UMG|控件树|动画蓝图|AnimBP|Anim\s*Blueprint|Niagara|发射器",
    re.I,
)
_BP_EDIT_RE = re.compile(
    r"加(?:一个)?节点|创建节点|连线|接线|事件图|Event\s*Graph|改图|改蓝图|"
    r"BeginPlay|ReceiveBeginPlay|"
    r"蓝图.{0,16}(?:节点|连线|接线|事件)|(?:节点|连线|接线).{0,16}蓝图",
    re.I,
)
_BP_EDIT_TOOLS = [
    "read_graph_dsl",
    "get_node_infos",
    "write_graph_dsl",
    "create_node",
    "connect_pins",
    "add_event",
    "set_pin_value",
    "add_variable",
]


def _fail(message: str) -> dict:
    return {"ok": False, "error": message}


def _records(source_type: str | None = None) -> list[dict]:
    corpus = _corpus("ue_assets")
    rows = []
    for i, item_id in enumerate(corpus["ids"]):
        meta = corpus["metas"][i] or {}
        if source_type and str(meta.get("source_type") or "") != source_type:
            continue
        rows.append({"id": item_id, "content": corpus["docs"][i] or "", "metadata": meta})
    return rows


def _base(query: str) -> dict:
    return {
        "id": "q-" + datetime.now().strftime("%Y%m%d-%H%M%S"),
        "query": query.strip(),
        "verify": [],
        "allow_writes": [],
        **task_policy(),
        "max_retries": 3,
        "supervised": True,
    }


def _keep(query: str) -> bool:
    return bool(re.search(r"留下修改|保留修改|keep_source", query))


def _filter_in(query: str) -> str:
    match = _FILTER_RE.search(query)
    return match.group(1) if match else ""


def declared_automation_filters() -> list[str]:
    """源码里 IMPLEMENT_SIMPLE_AUTOMATION_TEST 声明的过滤器。不写死测试名。"""
    from config import UE_SOURCE_ROOT

    if not Path(UE_SOURCE_ROOT).is_dir():
        return []
    pattern = re.compile(
        r'IMPLEMENT_SIMPLE_AUTOMATION_TEST\s*\(\s*[^,]+,\s*"([^"]+)"',
        re.S,
    )
    found: list[str] = []
    for path in Path(UE_SOURCE_ROOT).rglob("*.cpp"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.extend(pattern.findall(text))
    return list(dict.fromkeys(found))


def unique_automation_filter(*needles: str) -> str:
    """关键字只命中一条已声明过滤器时返回它。零条或多条都返回空，不挑。"""
    hits = []
    for name in declared_automation_filters():
        low = name.lower()
        if any(needle.lower() in low for needle in needles if needle):
            hits.append(name)
    if len(hits) == 1:
        return hits[0]
    return ""


def _attach_filter(task: dict, query: str) -> dict:
    filt = _filter_in(query)
    if filt:
        task["automation_filter"] = filt
    return task


_CPP_EDIT_RE = re.compile(r"新增|加一个|加函数|添加|改一下|改成|实现一个|打印日志|打日志|测试函数")
_PROJECT_COMPILE_RE = re.compile(r"编译工程|编译项目|编译一下工程|编译代码")
_BP_CREATE_PHRASE_RE = re.compile(
    r"(?:新增|添加|创建|新建|生成|加)一?个?\s*(?:/Game/[A-Za-z0-9_/]+|BP_[A-Za-z0-9_]+|蓝图)"
)
_LAUNCH_EDITOR_RE = re.compile(
    r"(?:启动|打开|开启)\s*(?:UE|Unreal|虚幻)\s*(?:引擎)?\s*(?:编辑器)?"
    r"|(?:启动|打开|开启)\s*编辑器",
    re.I,
)
# 单独一个「启动/打开 UE 或虚幻」不够：「打开虚幻商城」也会命中，必须排除商店这类对象。
_LAUNCH_EDITOR_EXCLUDE_RE = re.compile(r"商城|市场|Marketplace|商店|资产库", re.I)
_PICKUP_BY_RE = re.compile(
    r"(?P<target>A[A-Z][A-Za-z0-9_]*)\s*能否被\s*(?P<picker>A[A-Z][A-Za-z0-9_]*)\s*拾取"
)
_PICKUP_VERB_RE = re.compile(
    r"(?P<picker>A[A-Z][A-Za-z0-9_]*)\s*拾取\s*(?P<target>A[A-Z][A-Za-z0-9_]*)"
)
_PICKUP_BEI_RE = re.compile(r"被\s*(?P<picker>A[A-Z][A-Za-z0-9_]*)\s*拾取")


def _is_cpp_edit(query: str) -> bool:
    """要改源码。修编译错误仍走先裁判、再改，不算新增功能。「新增蓝图」那半句不算 C++ 改动。"""
    if re.search(r"编译错误|error\s*C", query, re.I):
        return False
    text = _BP_CREATE_PHRASE_RE.sub(" ", query)
    return bool(_CPP_EDIT_RE.search(text) or _is_source_fix(text))


def _is_source_fix(query: str) -> bool:
    """修改/修复源码。旧的新增、改一下等说法仍走 _CPP_EDIT_RE。"""
    if re.search(r"编译错误|error\s*C", query, re.I):
        return False
    if not re.search(r"修改|修复", query):
        return False
    if re.search(r"代码|\.h\b|\.cpp\b", query):
        return True
    return bool(_cpp_tokens(query))


def _wants_scene_move(query: str) -> bool:
    """把场景里的 Actor 挪到坐标。改源码里的移动不是这一类。"""
    if _is_source_fix(query) and _coords(query) is None:
        return False
    if re.search(r"视角|镜头|鼠标", query) and _coords(query) is None:
        return False
    if re.search(r"移动|移到", query):
        return True
    if re.search(r"放到", query) and not re.search(r"放到场景|放到关卡", query):
        return True
    return bool(_coords(query) and len(_actor_labels(query)) == 1)


def _strip_paths(query: str) -> str:
    """把带分隔符的 .h/.cpp 路径整段抹掉。路径里的目录名（如 AIAgentProject）不当类名。"""
    return _PATH_STRIP_RE.sub(" ", query)


def _cpp_tokens(query: str) -> list[str]:
    text = _strip_paths(query)
    tokens = []
    tokens.extend(_CLASS_RE.findall(text))
    tokens.extend(_FILE_RE.findall(query))
    for token in _LOOSE_RE.findall(text):
        if _resolve_class(token):
            tokens.append(token)
    for rec in _records("ue_cpp"):
        name = str((rec.get("metadata") or {}).get("class_name") or "")
        if len(name) >= 3 and re.search(rf"\b{re.escape(name)}\b", text):
            tokens.append(name)
    seen = []
    for token in tokens:
        if token not in seen:
            seen.append(token)
    return seen


def _resolve_class(token: str) -> list[dict]:
    bare = token[1:] if token.startswith("A") and len(token) > 1 else token
    hits = []
    for rec in _records("ue_cpp"):
        meta = rec["metadata"]
        class_name = str(meta.get("class_name") or "")
        stem = Path(str(meta.get("file_name") or "")).stem
        if class_name in (token, "A" + bare) or stem in (token, bare):
            hits.append(rec)
    return hits


def _movement_ready(stem: str) -> bool:
    rel = _implementation(stem)
    if not rel:
        return False
    text = (Path(UE_SOURCE_ROOT) / rel).read_text(encoding="utf-8", errors="replace")
    return 'BindAxis(TEXT("MoveForward")' in text and 'BindAxis(TEXT("MoveRight")' in text


def _implementation(stem: str) -> str | None:
    """唯一的同名 cpp 返回相对路径。多于一个时返回空字符串，表示不能猜。"""
    root = Path(UE_SOURCE_ROOT)
    found = [path for path in root.rglob(f"{stem}.cpp") if path.is_file()]
    if len(found) > 1:
        return ""
    if len(found) == 1:
        return found[0].relative_to(root).as_posix()
    return None


def _source_rel(raw: str) -> str:
    """人写的路径归一化成相对当前源码根的 posix 路径。"""
    from config import source_rel_prefix

    rel = raw.replace("\\", "/").strip().strip("\"'`").lstrip("/")
    prefix = source_rel_prefix()
    if prefix and rel.startswith(prefix):
        rel = rel[len(prefix):]
    return rel


def _unique_source_file(query: str, stem: str) -> str | None:
    """句子里点名的那个 stem 在源码树里的唯一相对路径。多于一份就返回 None，不猜。

    句子给了目录就只认那个目录：写错目录不能悄悄换成同名文件（那是猜）。
    只写文件名时按 stem（+点名的后缀）在源码树里找唯一一份。
    """
    root = Path(UE_SOURCE_ROOT)
    pathed: list[str] = []
    bare: list[str] = []
    for raw in _PATH_STRIP_RE.findall(query):
        rel = _source_rel(raw)
        if Path(rel).stem == stem and rel not in pathed:
            pathed.append(rel)
    for match in _FILE_RE.finditer(query):
        rel = _source_rel(match.group(0))
        if Path(rel).stem == stem and rel not in bare:
            bare.append(rel)
    for rel in [*pathed, *bare]:
        if (root / rel).is_file():
            return rel
    if pathed:
        # 写了目录但源码树里没有这一份：不猜
        return None
    # 只写了文件名：按 stem + 点名的后缀找唯一一份
    suffixes = {Path(rel).suffix for rel in bare} or {".h", ".cpp"}
    found = [
        path for path in root.rglob(f"{stem}.*")
        if path.is_file() and path.suffix in suffixes
    ]
    if len(found) == 1:
        return found[0].relative_to(root).as_posix()
    return None


def _pinned_class(query: str) -> dict | None:
    """句子里点名了唯一的 .h/.cpp 文件时，目标类由文件钉死，其余类名只当引用参数。"""
    files = _FILE_RE.findall(query)
    if len(files) != 1:
        return None
    classes = {}
    for rec in _resolve_class(files[0]):
        class_name = str(rec["metadata"].get("class_name") or "")
        if class_name:
            classes[class_name] = rec
    if len(classes) == 1:
        return next(iter(classes.values()))
    return None


def _unique_class(tokens: list[str]) -> dict | None:
    classes = {}
    for token in tokens:
        for rec in _resolve_class(token):
            class_name = str(rec["metadata"].get("class_name") or "")
            if class_name:
                classes[class_name] = rec
    if len(classes) == 1:
        return next(iter(classes.values()))
    return None


def _cpp_rule(query: str) -> str:
    if _is_cpp_edit(query):
        return "可以新增这次要求的函数。声明放头文件，实现放 cpp。不要改别的类。"
    return "不要新增无关函数，不要改别的文件。"


def _cpp_task_body(query: str, files: str) -> dict:
    """钉死文件的 C++ 任务体。句式与判定口径只留这一份，_try_cpp / cpp_task_for_file 共用。"""
    task = _base(query)
    task["query"] = (
        f"{query.strip()}\n"
        f"只改 {files}。用 read_source 读出要改的段落，再用 apply_unified_diff。"
        "diff 的旧行必须来自最近一次 read_source 的 text，不要带行号。写码成功后文件已变，旧行要用新读取重做；"
        "每段 diff 成功后行号会偏移：下一段 diff 前必须重新 read_source，@@ 旧行号用这次返回的 numbered。"
        f"{_cpp_rule(query)}"
    )
    task["verify"] = [{"kind": "cpp_build"}]
    task["judge_first"] = not _is_cpp_edit(query)
    task["launch_editor"] = _is_cpp_edit(query)
    return task


def _cpp_task_for_named_file(query: str, stem: str) -> dict:
    """句子里唯一点名的文件没有 UCLASS：钉死这个文件，不要求类，也不回落到句子里的别的类。"""
    rel = _unique_source_file(query, stem)
    if not rel:
        # 让 sequence.mark_missing_path 能直接标成 needs=source，等人 r 补一次
        return {
            **_fail("点名的 C++ 文件不是唯一的，不猜。只给一次：r Private/名字.cpp。"),
            "needs": "source",
            "blocked": True,
        }
    task = _cpp_task_body(query, rel)
    if _keep(query):
        task["keep_source"] = True
    return _attach_filter(task, query)


def _class_task(query: str, rec: dict, shortcuts: bool = True) -> dict:
    """把一个已解析出的 UCLASS 记录做成任务。路径不唯一时返回 _fail 结果，不猜。

    shortcuts 管「移动已实现就只编译」「注入 typo」这类单循环专用改写：规划器的 edit_cpp 扇
    要的是真改源码（expect_source_write），带上它们会自相矛盾。
    """
    stem = Path(str(rec["metadata"].get("file_name") or "")).stem
    rel = _implementation(stem)
    if rel == "":
        return _fail("C++ 路径不是唯一的，不猜。")
    if not rel:
        header = str(rec["metadata"].get("file_name") or "")
        header_path = Path(UE_SOURCE_ROOT) / "Public" / header
        if header and header_path.is_file():
            rel = f"Public/{header}"
        else:
            return _fail("写不出唯一的 C++ 路径，不猜。")
    head = f"Public/{stem}.h"
    files = rel
    if head != rel and (Path(UE_SOURCE_ROOT) / head).is_file():
        files = f"{head} 与 {rel}"
    task = _cpp_task_body(query, files)
    if shortcuts and _movement_ready(stem) and re.search(r"WASD|前后左右|无法移动", query, re.I):
        task["judge_first"] = True
        task["query"] += "\n移动实现已经在源码里。这一轮只编译，不要改文件。"
    if shortcuts and rel.endswith("TestActor.cpp") and re.search(r"编译错误|error\s*C", query, re.I):
        text = (Path(UE_SOURCE_ROOT) / rel).read_text(encoding="utf-8", errors="replace")
        if "ThisFunctionDoesNotExist" not in text:
            task["inject_typo"] = True
            task["typo_path"] = rel
    if _keep(query):
        task["keep_source"] = True
    return _attach_filter(task, query)


def _try_cpp(query: str) -> dict | None:
    if not (re.search(r"编译|error\s*C|构建|\bcpp\b", query, re.I) or _is_cpp_edit(query)):
        return None
    if _BP_RE.search(query) and not _cpp_tokens(query):
        return None
    tokens = _cpp_tokens(query)
    if not tokens:
        return _fail("写不出唯一的 C++ 路径，不猜。")
    named = _FILE_RE.findall(query)
    rec = _pinned_class(query)
    if rec is None:
        # 句子里只点名一个 .h/.cpp：目标就是它，绝不用句子里的别的类替换
        if len(named) == 1:
            return _cpp_task_for_named_file(query, named[0])
        rec = _unique_class(tokens)
    if rec is None:
        return _fail("C++ 类不是唯一的，不猜。")
    return _class_task(query, rec)


def cpp_task_for_file(query: str, rel: str) -> dict | None:
    """人用 r 补上的源文件。必须是 Source/AIAgentProject 里已经存在的 .h 或 .cpp。"""
    rel = _source_rel(rel)
    if not rel.endswith((".h", ".cpp")) or ".." in rel.split("/"):
        return None
    root = Path(UE_SOURCE_ROOT)
    if not (root / rel).is_file():
        return None
    stem = Path(rel).stem
    header = f"Public/{stem}.h"
    files = rel
    if rel.endswith(".h"):
        cpp = _implementation(stem)
        if cpp and cpp != rel:
            files = f"{rel} 与 {cpp}"
    elif header != rel and (root / header).is_file():
        files = f"{header} 与 {rel}"
    task = _cpp_task_body(query, files)
    task["title"] = "改 C++ 并编译"
    task["path_filled"] = True
    return _attach_filter(task, query)


def cpp_task_for_class(query: str, class_name: str) -> dict | None:
    """按类名定位这一个类，再复用 cpp_task_for_file 的任务体。解析不出来返回 None，由调用方回落。

    query 必须是原句（origin）：规划者给的 title 只是扇名，拿它当需求等于把需求丢掉
    （20261024-011917 的 W1 就是这么把 WASD/跳跃/金币那句丢光的）。
    原句里可能同时提到别的类名或蓝图，所以仍然按这个类钉死它自己所在的文件。
    """
    token = (class_name or "").strip()
    if not token:
        return None
    classes = {}
    for hit in _resolve_class(token):
        name = str(hit["metadata"].get("class_name") or "")
        if name:
            classes[name] = hit
    if len(classes) != 1:
        # 索引里没有这个类（或不是唯一一条）：返回 None，由调用方决定回落，这里不编
        return None
    task = _class_task(query, next(iter(classes.values())), shortcuts=False)
    if task.get("ok") is False:
        return None
    task["title"] = "改 C++ 并编译"
    task["path_filled"] = True
    return task


def pinned_file_matches_class(query: str, class_name: str) -> bool:
    """原句唯一点名的那个文件，stem 是否正好是这个类名（A 前缀可有可无）。

    规划者的 class 槽位不一定是 UCLASS：PlatformRunTest 在工程里是 AutomationTest，索引里没有
    class_name 记录，但原句点名的 Private/PlatformRunTest.cpp 就是目标。对得上才认，绝不换成别的类。
    """
    files = _FILE_RE.findall(query or "")
    token = (class_name or "").strip()
    if not token or len(files) != 1:
        return False
    bare = token[1:] if token.startswith("A") and len(token) > 1 else token
    return files[0] in (token, bare)


def named_file_class(query: str) -> str | None:
    """原句唯一点名的那个文件解析出的 UCLASS 类名。解析不出（无 UCLASS / 不唯一）返回 None。

    给调用方做判据优先级用：点名文件优先，而且「点名的文件有类」时走按类的任务
    （shortcuts=False），不要走 _try_cpp 那条带单循环改写的路。
    """
    if len(_FILE_RE.findall(query or "")) != 1:
        return None
    rec = _pinned_class(query or "")
    if rec is None:
        return None
    return str(rec["metadata"].get("class_name") or "") or None


def _unique_blueprint_ref(query: str) -> tuple[str | None, str | None]:
    names = list(dict.fromkeys(_BP_RE.findall(query)))
    paths = re.findall(r"/Game/[A-Za-z0-9_./]+", query)
    if not (names or paths or "蓝图" in query):
        return None, "写不出唯一的蓝图 refPath，不猜。"
    hits = []
    for rec in _records("ue_asset"):
        meta = rec["metadata"]
        asset_name = str(meta.get("asset_name") or "")
        package = str(meta.get("package_name") or "")
        object_path = str(meta.get("object_path") or "")
        if names and asset_name in names:
            hits.append(rec)
            continue
        if any(package == path or object_path == path for path in paths):
            hits.append(rec)
    if "蓝图" in query or names:
        hits = [
            rec for rec in hits
            if str(rec["metadata"].get("asset_class") or "") == "Blueprint" or names
        ]
    if len(hits) != 1:
        return None, "写不出唯一的蓝图 refPath，不猜。"
    ref = str(hits[0]["metadata"].get("object_path") or "")
    if not ref:
        return None, "写不出唯一的蓝图 refPath，不猜。"
    return ref, None


def _try_blueprint(query: str) -> dict | None:
    if not re.search(r"编译|compile", query, re.I):
        return None
    if not (_BP_RE.search(query) or "/Game/" in query or "蓝图" in query):
        return None
    ref, error = _unique_blueprint_ref(query)
    if error or not ref:
        return _fail(error or "写不出唯一的蓝图 refPath，不猜。")
    task = _base(query)
    task["query"] = f"{query.strip()}\n不要改蓝图 Graph。不要调用工具，让裁判编译 {ref}。"
    task["verify"] = [{"kind": "blueprint_compile", "refPath": ref}]
    return _attach_filter(task, query)


def _coords(query: str) -> tuple[float, float, float] | None:
    match = re.search(
        r"x\s*=\s*(-?\d+(?:\.\d+)?)\D+y\s*=\s*(-?\d+(?:\.\d+)?)\D+z\s*=\s*(-?\d+(?:\.\d+)?)",
        query,
        re.I,
    )
    if not match:
        match = re.search(
            r"\(\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*\)",
            query,
        )
    if not match:
        return None
    return tuple(float(part) for part in match.groups())


def _contains_name(query: str, name: str) -> bool:
    """ASCII 边界。中文紧挨着 Cube 也算命中，X= 这种单字母不会被当成名字。"""
    if len(name) < 2:
        return False
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
        query,
        re.I,
    ) is not None


def _actor_labels(query: str) -> list[str]:
    found = []
    for rec in _records("ue_actor"):
        label = str((rec.get("metadata") or {}).get("actor_label") or "")
        if label and label not in found and _contains_name(query, label):
            found.append(label)
    return found


def _wants_save(query: str) -> bool:
    """句子明说要保存，或没写「不保存」时跟随任务缺省。"""
    if re.search(r"不\s*保存|别\s*保存|不要\s*保存", query or ""):
        return False
    if "保存" in query or re.search(r"\bsave\b", query, re.I) is not None:
        return True
    return task_flag({}, "save")


def _try_editor(query: str) -> dict | None:
    labels = _actor_labels(query)
    coords = _coords(query)
    verb = re.search(r"移动|移到|放到|坐标", query) is not None
    if not verb and not (coords and labels):
        return None
    if len(labels) != 1 or coords is None:
        return _fail("写不出唯一的 Actor 或坐标，不猜。")
    name = labels[0]
    level = _LEVEL_RE.search(query)
    hits = []
    for rec in _records("ue_actor"):
        meta = rec["metadata"]
        if str(meta.get("actor_label") or "") != name:
            continue
        if level and str(meta.get("level_name") or "") != level.group(1):
            continue
        hits.append(rec)
    if len(hits) != 1:
        return _fail("Actor 不是唯一的，不猜。")
    x, y, z = coords
    save = _wants_save(query)
    task = _base(query)
    level_text = f"当前关卡要包含 {level.group(1)}。" if level else ""
    if save:
        tail = "然后调用 save_assets，asset_paths 传空数组，保存改脏的关卡。"
    else:
        tail = "不要保存。"
    task["query"] = (
        f"{query.strip()}\n"
        f"把名为 {name} 的 Actor 移动到世界坐标 x={x:g} y={y:g} z={z:g}。{level_text}"
        "先用 find_actors（name、tag、collision_channels 都要传，tag 传空字符串，collision_channels 传空数组）"
        f"拿到 refPath，再调用 set_actor_transform，只改 location，worldspace 为 true。{tail}"
    )
    task["verify"] = [{
        "kind": "editor_assert",
        "toolset": "editor_toolset.toolsets.actor.ActorTools",
        "tool": "get_actor_transform",
        "actor_name": name,
        "expect": {"location": {"x": x, "y": y, "z": z}},
    }]
    task["allow_writes"] = ["set_actor_transform"]
    task["save"] = save
    if save:
        task["allow_writes"].append("save_assets")
    if level:
        task["require_level"] = level.group(1)
    return _attach_filter(task, query)


def _try_automation(query: str) -> dict | None:
    filt = _filter_in(query)
    if not filt or not re.search(r"自动化|automation|测试", query, re.I):
        return None
    task = _base(query)
    task["verify"] = [{"kind": "automation", "filter": filt}]
    task["automation_filter"] = filt
    return task


def script_class_path(class_name: str) -> str:
    """反射名去掉 A/U/F 前缀。模块名来自 project_context，不写死工程名。"""
    from config import script_object_path

    return script_object_path(class_name)


def _blueprint_identity(query: str) -> dict | None:
    """只确定类和默认资产名。目录必须由人用 r 填，不默认丢进 /Game。"""
    classes = {}
    for token in _cpp_tokens(query):
        for rec in _resolve_class(token):
            name = str((rec.get("metadata") or {}).get("class_name") or "")
            if name.startswith("A"):
                classes[name] = rec
    if len(classes) != 1:
        return None
    class_name = next(iter(classes))
    asset_name = "BP_" + class_name[1:]
    return {
        "asset_name": asset_name,
        "parent": script_class_path(class_name),
        "actor_name": asset_name,
    }


def _followups(query: str) -> list[str]:
    """长句里排在 C++ 后面的步骤。有具体蓝图路径时不另建。"""
    if _BP_RE.search(query) or "/Game/" in query:
        return []
    steps = []
    if re.search(r"创建蓝图|新建蓝图|生成蓝图", query):
        steps.append("创建蓝图。需要目录，暂停后用 r /Game/某目录 补上。")
    if re.search(r"丢到场景|放到场景|放进场景|拖到场景", query):
        steps.append("把蓝图放进关卡。沿用上一格填的蓝图路径，放完按任务策略保存关卡。")
    if re.search(r"拾取", query):
        steps.append("用 Pawn 测试拾取。放进场景后给实例打上 InteractableActor 标签再查一次，不进 PIE。")
    return steps


# 放置句式：兼容「放一个到场景中」「再放一条进关卡」这类量词写法；「放一个地方」不算
_PLACE_RE = re.compile(
    r"(?:丢|放|摆|拖|扔)(?:[一1][个条只])?(?:个|份)?(?:到|进|入|去)(?:场景|关卡|地图)"
    r"|丢到场景|放到场景|放进场景|拖到场景"
    r"|添加到场景|加进场景|放到当前关卡|放进当前关卡"
)


def _blocked_create(ident: dict, note: str) -> dict:
    step = {
        "ok": False,
        "blocked": True,
        "needs": "path",
        "title": "创建蓝图",
        "action": "create_blueprint",
        "error": note,
    }
    step.update(ident)
    return step


def _try_blueprint_create(query: str) -> list[dict] | None:
    """「新增/创建蓝图」那半句。动词后面直接跟 BP_ / /Game / 蓝图才算；
    只提「给某类加个属性」、蓝图只是顺带要编译的不算。句子里给了 /Game 路径就直接绑好；没给就照旧暂停，等一次 r。"""
    if not _BP_CREATE_PHRASE_RE.search(query):
        return None
    ident = _blueprint_identity(query) or {}
    wants_place = bool(_PLACE_RE.search(query))
    paths = [match.group(0) for match in re.finditer(r"/Game(?:/[A-Za-z0-9_]+)+", query)]
    if not paths:
        steps = [_blocked_create(ident, "需要蓝图目录。只给一次：r /Game/某目录，或 r /Game/某目录/BP_名字。")]
        if wants_place:
            steps.append({
                "ok": False,
                "blocked": True,
                "needs": "blueprint",
                "title": "放进场景",
                "action": "place_blueprint",
                "error": "等蓝图路径。只给一次：r /Game/某目录/BP_名字。创建蓝图时填过的目录会带到这里。",
                **ident,
            })
        return steps
    if len(paths) > 1:
        return [_blocked_create(
            ident,
            "/Game 路径不是唯一的一条，不猜。只给一次：r /Game/某目录/BP_名字。",
        )]
    from agent.sequence import apply_blueprint_path
    filled = apply_blueprint_path(
        {"title": "创建蓝图", "action": "create_blueprint", **ident}, paths[0]
    )
    if filled is None:
        return [_blocked_create(
            ident,
            "需要蓝图目录。只给一次：r /Game/某目录，或 r /Game/某目录/BP_名字。",
        )]
    create = dict(filled)
    create["title"] = "创建蓝图"
    create["action"] = "create_blueprint"
    steps = [create]
    if wants_place:
        steps.append({
            "ok": True,
            "title": "放进场景",
            "action": "place_blueprint",
            "folder_path": create["folder_path"],
            "asset_name": create["asset_name"],
            "actor_name": create["actor_name"],
            "ref_path": create["ref_path"],
        })
    return steps


def _bp_name_identity(query: str) -> dict | None:
    """句子里没写 A 类名时，从唯一 BP_ 名推父类：BP_TestEnemy 对应 C++ 索引里的 ATestEnemy。
    写了 A 类名且推出的资产名和 BP_ 名一致时优先用它。推不出唯一候选返回 None，不猜。"""
    names = list(dict.fromkeys(_BP_RE.findall(query)))
    if len(names) != 1:
        return None
    ident = _blueprint_identity(query)
    if ident and ident.get("asset_name") == names[0]:
        return ident
    stem = names[0][3:] if names[0].startswith("BP_") and len(names[0]) > 3 else ""
    if not stem:
        return None
    classes = {
        str(rec["metadata"].get("class_name") or "")
        for rec in _resolve_class(stem)
    }
    classes = {name for name in classes if name.startswith("A")}
    if len(classes) != 1:
        return None
    class_name = next(iter(classes))
    return {
        "asset_name": names[0],
        "parent": script_class_path(class_name),
        "actor_name": names[0],
    }


_PLACE_BLOCK = {
    "ok": False,
    "blocked": True,
    "needs": "blueprint",
    "title": "放进场景",
    "action": "place_blueprint",
}


def _try_named_blueprint_place(query: str) -> list[dict] | None:
    """「把已点名的蓝图放进场景」。没有创建动词、句子写明放到/放进场景、点名的
    是 BP_ 名或 /Game/ 路径才触发；创建短语仍归 _try_blueprint_create。
    索引里唯一命中：直接放。索引里没有：唯一 BP_ 名加最多一条 /Game 路径，父类
    从 C++ 索引按 BP_ 名推（推不出唯一候选就暂停问一次 r）。路径上还没有资产时
    按该类新建再放，不改图。蓝图对不上仍然不猜。"""
    if not _PLACE_RE.search(query):
        return None
    if not (_BP_RE.search(query) or "/Game/" in query):
        return None
    if _BP_CREATE_PHRASE_RE.search(query):
        return None
    ref, _error = _unique_blueprint_ref(query)
    if ref:
        package = ref.rsplit("/", 1)[0]
        asset = ref.rsplit("/", 1)[1].split(".", 1)[0]
        if package and asset:
            return [{
                "ok": True,
                "title": "放进场景",
                "action": "place_blueprint",
                "folder_path": package,
                "asset_name": asset,
                "actor_name": asset,
                "ref_path": ref,
                **task_policy(),
            }]
    names = list(dict.fromkeys(_BP_RE.findall(query)))
    paths = [match.group(0) for match in re.finditer(r"/Game(?:/[A-Za-z0-9_]+)+", query)]
    if len(paths) > 1:
        paths = []
    if not names:
        return [dict(_PLACE_BLOCK, error="没有点名蓝图，只给一次：r /Game/某目录/BP_名字。")]
    from agent.grounding import link_asset

    linked = link_asset(names[0], paths=paths)
    create = dict(linked.get("create") or {})
    if linked.get("plan") == "linked" and isinstance(linked.get("fields"), dict):
        # link_asset 已唯一命中（索引或磁盘兜底）：直接放，不再当推不出而暂停
        fields = linked["fields"]
        return [{
            "ok": True,
            "title": "放进场景",
            "action": "place_blueprint",
            "folder_path": fields.get("folder_path"),
            "asset_name": fields.get("asset_name"),
            "actor_name": fields.get("actor_name") or fields.get("asset_name"),
            "ref_path": fields.get("ref_path"),
            **task_policy(),
        }]
    if linked.get("plan") == "create_planned" and create.get("asset_name"):
        filler = {key: value for key, value in create.items() if key in ("asset_name", "parent")}
        if create.get("folder_path"):
            filled = dict(create)
            filled["title"] = "创建蓝图"
            filled["action"] = "create_blueprint"
            filled["ok"] = True
            filled["blocked"] = False
            filled["actor_name"] = filled["asset_name"]
            return [
                filled,
                {
                    "ok": True,
                    "title": "放进场景",
                    "action": "place_blueprint",
                    "folder_path": filled["folder_path"],
                    "asset_name": filled["asset_name"],
                    "actor_name": filled["actor_name"],
                    "ref_path": filled["ref_path"],
                },
            ]
        # 与旧路径一致：create 扇缺目录暂停要一次 r，place 扇等蓝图路径
        create_step = {
            "ok": False,
            "blocked": True,
            "needs": "path",
            "title": "创建蓝图",
            "action": "create_blueprint",
            "error": "索引里没有这张蓝图，也没有 /Game 路径。只给一次：r /Game/某目录，或 r /Game/某目录/BP_名字。",
            "asset_name": create["asset_name"],
        }
        if create.get("parent"):
            create_step["parent"] = create["parent"]
        return [create_step, dict(
            _PLACE_BLOCK,
            error="等蓝图路径。只给一次：r /Game/某目录/BP_名字。创建蓝图时填过的目录会带到这里。",
            **{**filler, "actor_name": create["asset_name"]},
        )]
    # pause_r（推不出父类或蓝图不唯一）：create 扇（不带 parent）+ place 扇各暂停一次，r 补了目录再跑
    ident = {"asset_name": names[0], "actor_name": names[0]}
    pause_note = str(linked.get("error") or "索引里没有这张蓝图，也没有 /Game 路径。只给一次：r /Game/某目录，或 r /Game/某目录/BP_名字。")
    return [
        {
            "ok": False,
            "blocked": True,
            "needs": "path",
            "title": "创建蓝图",
            "action": "create_blueprint",
            "error": pause_note,
            "asset_name": names[0],
        },
        dict(
            _PLACE_BLOCK,
            error="等蓝图路径。只给一次：r /Game/某目录/BP_名字。创建蓝图时填过的目录会带到这里。",
            **ident,
        ),
    ]


def _with_followups(task: dict | None, query: str) -> dict | None:
    if not task or task.get("ok") is False:
        return task
    steps = _followups(query)
    if not steps:
        return task
    task["followups"] = steps
    task["query"] = (
        task["query"].rstrip()
        + "\n本轮只做 C++ 修改、编译，以及源码留下后的重新导出和启动编辑器。"
        "不要创建蓝图，不要放置 Actor，不要 PIE。留到下一句：\n"
        + "\n".join(f"- {step}" for step in steps)
    )
    return task


def _from_rules(query: str) -> dict | None:
    compile_hit = bool(re.search(r"编译|compile", query, re.I))
    named_bp = bool(_BP_RE.search(query) or "/Game/" in query)
    bp = bool(compile_hit and named_bp)
    cpp = bool(_cpp_tokens(query) and (re.search(r"编译|error\s*C|构建", query, re.I) or _is_cpp_edit(query)))
    editor = _wants_scene_move(query)
    if sum(bool(item) for item in (bp, cpp, editor)) > 1:
        return _fail("一句话里有多种目标，不猜。")
    if bp:
        return _try_blueprint(query)
    if cpp:
        return _with_followups(_try_cpp(query), query)
    if editor:
        return _try_editor(query)
    if compile_hit and "蓝图" in query:
        return _fail("写不出唯一的蓝图 refPath，不猜。")
    auto = _try_automation(query)
    if auto:
        return auto
    return None


def _from_model(query: str) -> dict:
    key = os.getenv("DEEPSEEK_API_KEY") or ""
    if not key.strip():
        return _fail("这句话套不上规则，且没有 DEEPSEEK_API_KEY。")
    from core.llm_call import complete

    response, _record = complete(
        [
            {
                "role": "system",
                "content": (
                    "你只输出 JSON。字段 kind 只能是 cpp_build、blueprint_compile、editor_assert、automation、none 之一。"
                    "不要发明路径。看不准就 kind=none。"
                ),
            },
            {"role": "user", "content": query},
        ],
        temperature=0,
        caller="supervise",
    )
    text = response.choices[0].message.content or ""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return _fail("监督者没有写出任务。")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return _fail("监督者没有写出任务。")
    kind = str(data.get("kind") or "none")
    if kind == "cpp_build":
        task = _try_cpp(query if re.search(r"编译|error\s*C|构建", query, re.I) else query + " 编译")
        return task or _fail("写不出唯一的 C++ 路径，不猜。")
    if kind == "blueprint_compile":
        task = _try_blueprint(query if re.search(r"编译|compile", query, re.I) else query + " 编译")
        return task or _fail("写不出唯一的蓝图 refPath，不猜。")
    if kind == "editor_assert":
        task = _try_editor(query)
        return task or _fail("写不出唯一的 Actor 或坐标，不猜。")
    if kind == "automation":
        task = _try_automation(query)
        return task or _fail("写不出过滤器，不猜。")
    ruled = _try_editor(query)
    if ruled:
        return ruled
    return _fail("这句话写不成任务。需要唯一的类、蓝图、Actor 或过滤器。")


def _is_other_graph(query: str) -> bool:
    if not _OTHER_GRAPH_RE.search(query):
        return False
    if _BP_RE.search(query) or "蓝图" in query or re.search(r"事件图|Event\s*Graph", query, re.I):
        return False
    return True


def _is_blueprint_edit(query: str) -> bool:
    if _is_other_graph(query):
        return False
    return _BP_EDIT_RE.search(query) is not None


def _event_label(query: str) -> tuple[str, str]:
    if re.search(r"\btick\b|ReceiveTick", query, re.I):
        return "Event Tick", "没有就右键添加 Event Tick。"
    if re.search(r"hit|被击中|受击", query, re.I):
        return "Event Hit", "没有就右键添加 Event Hit。"
    if re.search(r"overlap|重叠", query, re.I):
        return "Event ActorBeginOverlap", "没有就右键添加 Event ActorBeginOverlap。"
    if re.search(r"begin\s*play|ReceiveBeginPlay|开始", query, re.I):
        return "Event BeginPlay", "没有就右键添加 Event BeginPlay。"
    return "Event BeginPlay", "查询没有点名事件。用 Event BeginPlay 当入口；图里已有更对的事件就用那一个。"


def _wire_lines(query: str, event: str) -> list[str]:
    duration = "0.1"
    found = re.search(r"(\d+(?:\.\d+)?)\s*秒", query)
    if found:
        duration = found.group(1)
    material = "目标材质"
    named = re.search(r"\bM_[A-Za-z0-9_]+\b", query)
    if named:
        material = named.group(0)
    flash = re.search(r"闪", query) is not None
    delay_at = re.search(r"delay|延迟", query, re.I)
    material_at = re.search(r"set material|设置材质|换材质|闪", query, re.I)
    print_at = re.search(r"print|打印", query, re.I)
    if flash and delay_at:
        return [
            f"从 {event} 的白色执行引脚连到 Set Material，材质换成{material}。",
            f"Set Material 的执行输出连到 Delay，Duration 填 {duration}。",
            "Delay 的 Completed 再连一个 Set Material，换回原来的材质。",
        ]
    marks = []
    if delay_at:
        marks.append((delay_at.start(), f"连到 Delay，Duration 填 {duration}"))
    if material_at:
        marks.append((material_at.start(), f"连到 Set Material，材质用 {material}"))
    if print_at:
        marks.append((print_at.start(), "连到 Print String，In String 填要显示的字"))
    if not marks:
        return [f"从 {event} 的白色执行引脚拖出线，按这句话添加节点并接上执行引脚：{query.strip()}"]
    lines = []
    for _pos, action in sorted(marks):
        src = f"{event} 的白色执行引脚" if not lines else "上一节点的执行输出"
        lines.append(f"从 {src}{action}。")
    return lines


def _write_blueprint_plan(query: str) -> str:
    names = list(dict.fromkeys(_BP_RE.findall(query)))
    label = names[0] if names else "这句话里的蓝图"
    ref, _error = _unique_blueprint_ref(query)
    event, event_note = _event_label(query)
    wires = _wire_lines(query, event)
    folder = UE_PROJECT_DIR / "Plan"
    folder.mkdir(parents=True, exist_ok=True)
    slug = names[0] if names else "蓝图"
    path = folder / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}.md"
    lines = [
        f"# {label} 蓝图操作",
        "",
        "`UE_ALLOW_BLUEPRINT=false`。这次不改蓝图。",
        "",
        "## 目标",
        "",
        query.strip(),
    ]
    if ref:
        lines.append(f"资产：`{ref}`。")
    lines.extend([
        "",
        "## 操作",
        "",
        f"1. 内容浏览器打开 `{label}`，进入 Event Graph。",
        f"2. 找到 **{event}**。{event_note}",
        *[f"{index}. {line}" for index, line in enumerate(wires, start=3)],
        f"{len(wires) + 3}. 点编译。编译通过即可。",
        "",
    ])
    body = "\n".join(lines)
    path.write_text(body, encoding="utf-8")
    return f"Plan/{path.name}"


def _blueprint_edit_task(query: str) -> dict:
    ref, error = _unique_blueprint_ref(query)
    if error or not ref:
        return _fail(error or "写不出唯一的蓝图 refPath，不猜。")
    task = _base(query)
    task["query"] = (
        f"{query.strip()}\n"
        f"可以改蓝图 {ref} 的 Graph。不保证一次成功。\n"
        "先 read_graph_dsl 读一次，再最小改动：add_event、create_node、connect_pins、set_pin_value。\n"
        "然后不要再调用工具，让裁判编译。编译失败会进入 fix。"
        "达到 max_retries 后停止，不要换一张蓝图。"
    )
    task["verify"] = [{"kind": "blueprint_compile", "refPath": ref}]
    task["allow_writes"] = list(_BP_EDIT_TOOLS)
    task["max_retries"] = 3
    return task


def _try_manual_graph(query: str) -> dict | None:
    """蓝图改图看 UE_ALLOW_BLUEPRINT。别的图仍拒绝。编译已有蓝图、移动 Actor、改 C++ 不进这里。"""
    if _is_blueprint_edit(query):
        if allow_blueprint():
            return _blueprint_edit_task(query)
        rel = _write_blueprint_plan(query)
        return _fail(f"改蓝图已关闭（UE_ALLOW_BLUEPRINT=false）。操作步骤在 {rel}") | {"plan": rel}
    if _is_other_graph(query):
        return _fail("改图不进无人值守闭环。在 Cursor 里按人工清单做完，回复 done 后再验一次。")
    return None


def _wants_launch_editor(query: str) -> bool:
    """句子里真的要求启动编辑器。商店/市场这类对象排除，否则「打开虚幻商城」会被当成启动编辑器。"""
    text = query or ""
    if not _LAUNCH_EDITOR_RE.search(text):
        return False
    return _LAUNCH_EDITOR_EXCLUDE_RE.search(text) is None


def _launch_editor_task(query: str) -> dict:
    """只启动编辑器并等 MCP。不编译、不改场景、不保存、不进 PIE。
    「启动 UE 编辑器，其他啥也别干」这类句子走到这里，不再映射成 compile_project 空转。"""
    task = _base(query)
    task["title"] = "启动 UE 编辑器"
    task["query"] = f"{query.strip()}\n只启动编辑器并等 MCP 连通。不要编译、不要改场景、不要保存、不要进 PIE。"
    task["action"] = "launch_editor"
    task["verify"] = [{"kind": "editor_assert", "text": "编辑器进程在，MCP 端口连通"}]
    task["allow_writes"] = []
    task["save"] = False
    task["pie"] = False
    task["max_retries"] = 0
    return task


_LEVEL_CREATE_RE = re.compile(
    r"(?:新建|创建|新增|添加|生成|建(?:立|造)?|做)\s*(?:一|1)?\s*(?:个|张|幅)?\s*(?:新的|新)?\s*"
    r"(?:关卡|地图|level|map)"
    r"|\b(?:create|add|make)\s+(?:a\s+)?(?:new\s+)?(?:level|map)\b"
    r"|\bnew\s+(?:level|map)\b",
    re.I,
)


def _wants_create_level(query: str) -> bool:
    """句子里真的要求新建一个关卡（不是「在这个关卡里做什么」）。
    「新建/创建/建一个/做一个 + 紧邻的 关卡/地图」才算；中间夹了别的词（做一个平台跳跃关卡）不算。"""
    return _LEVEL_CREATE_RE.search(query or "") is not None


def _level_token(query: str) -> str:
    """句中关卡名：唯一一条 L_ 名优先，其次唯一一条 /Game 路径。多于一条或没有返回空串，不猜。"""
    text = query or ""
    names = list(dict.fromkeys(_LEVEL_RE.findall(text)))
    if len(names) == 1:
        return names[0]
    if len(names) > 1:
        return ""
    paths = list(dict.fromkeys(
        match.group(0) for match in re.finditer(r"/Game(?:/[A-Za-z0-9_]+)+", text)
    ))
    return paths[0] if len(paths) == 1 else ""


def _create_level_task(query: str) -> dict:
    """「新建一个关卡 L_X」在句子级就由代码接住，不让整句退化成只启动编辑器。
    （20261024-192858：动作表里没有建关卡的动作，B 范围拿到 0 扇窗口后整段蒸发。）
    名字不唯一或为空时给一扇带 error 的暂停步，不猜也不静默。"""
    from agent.plan_windows import resolve_level

    task = _base(query)
    task["title"] = "新建关卡并切过去"
    task["action"] = "create_level"
    task["verify"] = [{"kind": "editor_assert", "text": "/Game 下关卡资产存在且已成为当前关卡"}]
    task["allow_writes"] = ["duplicate", "save_assets", "load_level"]
    task["max_retries"] = 1
    raw = _level_token(query)
    found = resolve_level(raw) if raw else {"ok": False, "error": "缺少关卡名，不猜。"}
    if not found.get("ok"):
        task.update({
            "ok": False,
            "blocked": True,
            "error": str(found.get("error") or "缺少关卡名，不猜。"),
        })
        return task
    task["slots"] = {"level": raw}
    for key in ("level_name", "level_path", "ref_path", "asset_name", "package_path"):
        if found.get(key):
            task[key] = found[key]
    return task


def _is_project_compile(query: str) -> bool:
    """编译整个工程。句里同时还在改函数时，仍走原来的改源码步骤。"""
    if not _PROJECT_COMPILE_RE.search(query):
        return False
    return not _is_cpp_edit(query)


def _project_compile_task(query: str) -> dict:
    task = _base(query)
    task["title"] = "编译工程"
    task["query"] = (
        f"{query.strip()}\n"
        "只编译当前工程。不要修改源码。"
    )
    task["verify"] = [{"kind": "cpp_build"}]
    task["judge_first"] = True
    task["freeze_source"] = True
    task["allow_writes"] = []
    if _LAUNCH_EDITOR_RE.search(query):
        task["launch_editor"] = True
        task["launch_without_source"] = True
        task["wait_mcp"] = True
    return task


def _pickup_roles(query: str) -> dict | None:
    """两个类名时按槽位拆。对不上「被 A 拾取」或「A 拾取 B」就返回 error，不猜。"""
    if "拾取" not in query:
        return None
    mentioned = []
    for token in _CLASS_RE.findall(query):
        if token not in mentioned:
            mentioned.append(token)
    matched = _PICKUP_BY_RE.search(query)
    if matched:
        return {"picker": matched.group("picker"), "target": matched.group("target")}
    matched = _PICKUP_VERB_RE.search(query)
    if matched:
        return {"picker": matched.group("picker"), "target": matched.group("target")}
    matched = _PICKUP_BEI_RE.search(query)
    if matched:
        picker = matched.group("picker")
        others = [name for name in mentioned if name != picker]
        if len(others) == 1:
            return {"picker": picker, "target": others[0]}
        if not others:
            return {"error": "句子里有「被某类拾取」，但没有物体类，不猜。"}
        return {"error": "物体类不是唯一的，不猜角色。"}
    if len(mentioned) >= 2:
        return {"error": "这句话里的拾取对不上「被 A 拾取」或「A 拾取 B」，不猜角色。"}
    return None


def _bare_class(class_name: str) -> str:
    if len(class_name) > 1 and class_name[0] in "AUF" and class_name[1].isupper():
        return class_name[1:]
    return class_name


def _bp_fields(class_name: str, folder: str, asset: str, asset_path: str, ref: str) -> dict:
    return {
        "ok": True,
        "class_name": class_name,
        "folder_path": folder,
        "asset_name": asset,
        "actor_name": asset,
        "asset_path": asset_path,
        "ref_path": ref,
        "parent": script_class_path(class_name),
    }


def _explicit_blueprint(query: str, class_name: str) -> dict | None:
    asset = "BP_" + _bare_class(class_name)
    found = []
    for match in re.finditer(rf"(/Game(?:/[A-Za-z0-9_]+)*)/{re.escape(asset)}(?:\.{re.escape(asset)})?", query):
        found.append(match.group(1))
    if len(found) > 1:
        return {"ok": False, "error": f"{asset} 的路径不是唯一的，不猜。"}
    if len(found) == 1:
        folder = found[0]
        asset_path = f"{folder}/{asset}"
        return _bp_fields(class_name, folder, asset, asset_path, f"{asset_path}.{asset}")
    return None


def _indexed_blueprint(class_name: str) -> dict | None:
    from config import project_module

    bare = _bare_class(class_name)
    asset = "BP_" + bare
    hits = []
    for rec in _records("ue_asset"):
        meta = rec.get("metadata") or {}
        if str(meta.get("asset_class") or "") != "Blueprint":
            continue
        if str(meta.get("asset_name") or "") != asset:
            continue
        content = rec.get("content") or ""
        parent_ok = (
            f"{project_module()}.{bare}" in content
            or f"父类名: {bare}" in content
            or f"父类名: {class_name}" in content
        )
        if parent_ok:
            hits.append(meta)
    if len(hits) > 1:
        return {"ok": False, "error": f"{class_name} 的蓝图不是唯一的，不猜。"}
    if not hits:
        return None
    meta = hits[0]
    ref = str(meta.get("object_path") or "")
    asset_path = ref.split(".", 1)[0] if "." in ref else str(meta.get("package_name") or "")
    if not asset_path.startswith("/Game"):
        return None
    folder, _, name = asset_path.rpartition("/")
    return _bp_fields(class_name, folder or "/Game", name or asset, asset_path, ref or f"{asset_path}.{asset}")


def _bind_slot(query: str, slot: str, class_name: str) -> dict:
    title = "绑定拾取者" if slot == "picker" else "绑定物体"
    base = {"title": title, "slot": slot, "class_name": class_name, "pickup_handled": True}
    hits = _resolve_class(class_name)
    if len(hits) != 1:
        return {
            **base,
            "ok": False,
            "blocked": True,
            "error": f"{class_name} 在 C++ 索引里不是唯一的一条，不猜，不创建蓝图。",
        }
    chosen = _explicit_blueprint(query, class_name)
    if chosen is None:
        chosen = _indexed_blueprint(class_name)
    if chosen and chosen.get("ok") is True:
        chosen.update(base)
        chosen["action"] = "bind_slot"
        return chosen
    if chosen and chosen.get("ok") is False:
        chosen.update(base)
        chosen["blocked"] = True
        return chosen
    asset = "BP_" + _bare_class(class_name)
    return {
        **base,
        "ok": False,
        "blocked": True,
        "needs": "blueprint",
        "action": "bind_slot",
        "asset_name": asset,
        "actor_name": asset,
        "parent": script_class_path(class_name),
        "error": (
            f"{class_name} 在索引里没有唯一蓝图。只给一次：r /Game/目录 或 r /Game/目录/BP_名字。"
            "路径上还没有资产时会按这个类新建，不改图。"
        ),
    }


def _append_pickup_slots(steps: list[dict], query: str) -> bool:
    roles = _pickup_roles(query)
    if roles is None:
        return False
    if roles.get("error"):
        steps.append({
            "ok": False,
            "blocked": True,
            "pickup_handled": True,
            "title": "绑定拾取",
            "error": roles["error"],
        })
        return True
    picker = _bind_slot(query, "picker", roles["picker"])
    target = _bind_slot(query, "target", roles["target"])
    steps.append(picker)
    steps.append(target)
    ready = picker.get("ok") is True and target.get("ok") is True
    place = {
        "title": "放进场景",
        "action": "place_pair",
        "picker": {key: picker.get(key) for key in ("asset_path", "actor_name", "ref_path", "asset_name", "folder_path", "parent")},
        "target": {key: target.get(key) for key in ("asset_path", "actor_name", "ref_path", "asset_name", "folder_path", "parent")},
        **task_policy(),
        "pickup_handled": True,
    }
    check = {
        "title": "只读裁决拾取",
        "action": "check_pickup",
        "read_only": True,
        "picker_name": picker.get("actor_name") or "",
        "actor_name": target.get("actor_name") or "",
        **task_policy(save=False, pie=False),
        "pickup_handled": True,
    }
    if ready:
        place["ok"] = True
        check["ok"] = True
    else:
        place.update({
            "ok": False,
            "blocked": True,
            "error": "有槽位没绑上。跳过没绑上的槽位后，这里不放置，也不裁决。",
        })
        check.update({
            "ok": False,
            "blocked": True,
            "error": "物体或拾取者没绑上，不裁决能否拾取。",
        })
    steps.append(place)
    steps.append(check)
    return True


def plan_sequence(query: str) -> list[dict]:
    """一句话拆成按顺序执行的步骤。手写规则和已学句式都先于规划者。"""
    ruled = _plan_rules(query)
    if ruled is not None:
        return ruled
    from agent.plan_windows import match_learned

    learned = match_learned(query)
    if learned is not None:
        return learned
    return [supervise(query)]


def _plan_rules(query: str) -> list[dict] | None:
    """现有手写规则。对不上时返回 None，不调用模型。"""
    query = (query or "").strip()
    if not query:
        return [_fail("没有任务内容。")]
    compile_hit = bool(re.search(r"编译|compile", query, re.I))
    named_bp = bool(_BP_RE.search(query) or "/Game/" in query)
    project_compile = _is_project_compile(query)
    cpp_hit = (not project_compile) and bool(
        _cpp_tokens(query) and (re.search(r"编译|error\s*C|构建", query, re.I) or _is_cpp_edit(query))
    )
    editor_hit = _wants_scene_move(query)
    create_level_hit = _wants_create_level(query)
    launch_only = (
        _wants_launch_editor(query)
        and not compile_hit
        and not project_compile
        and not cpp_hit
        and not named_bp
        and not editor_hit
        and not create_level_hit
    )
    if launch_only:
        # 只要求启动编辑器：给一扇真正启动编辑器的步骤，不再交给规划者编 compile_project
        return [_launch_editor_task(query)]
    if not cpp_hit and not project_compile and not named_bp and not editor_hit:
        manual = _try_manual_graph(query)
        if manual is not None:
            return [manual]
    steps = []
    if create_level_hit:
        # 建关卡前编辑器要先起来，顺序固定：先启动，再建关卡。
        if _wants_launch_editor(query):
            steps.append(_launch_editor_task(query))
        steps.append(_create_level_task(query))
    if project_compile:
        steps.append(_project_compile_task(query))
    if cpp_hit:
        task = _with_followups(_try_cpp(query), query)
        if task is None:
            task = _fail("写不出唯一的 C++ 路径，不猜。")
        if task.get("ok") is not False:
            task["title"] = "改 C++ 并编译"
        steps.append(task)
    create_steps = _try_blueprint_create(query)
    place_steps = None
    if create_steps is None:
        place_steps = _try_named_blueprint_place(query)
    if create_steps is not None:
        steps.extend(create_steps)
    elif place_steps is not None:
        # 「放进场景」接管了点名蓝图：句里的「编译」属于工程编译那一步，
        # 不再另拆一扇必停的「编译蓝图」
        steps.extend(place_steps)
    elif named_bp and compile_hit:
        task = _try_blueprint(query)
        if task is None:
            task = _fail("写不出唯一的蓝图 refPath，不猜。")
        if task.get("ok") is not False:
            task["title"] = "编译蓝图"
        steps.append(task)
    if editor_hit:
        task = _try_editor(query)
        if task is None:
            task = _fail("写不出唯一的 Actor 或坐标，不猜。")
        if task.get("ok") is not False:
            task["title"] = "移动 Actor"
        steps.append(task)
    handled_pickup = _append_pickup_slots(steps, query)
    for text in _followups(query):
        if handled_pickup and "拾取" in text:
            continue
        if create_steps is not None and (
            text.startswith("创建蓝图") or text.startswith("把蓝图放进")
        ):
            continue
        ident = _blueprint_identity(query)
        if text.startswith("创建蓝图"):
            step = {
                "ok": False,
                "blocked": True,
                "needs": "path",
                "title": "创建蓝图",
                "action": "create_blueprint",
                "error": "需要蓝图目录。只给一次：r /Game/某目录，或 r /Game/某目录/BP_名字。",
            }
            if ident:
                step.update(ident)
            steps.append(step)
            continue
        if text.startswith("把蓝图放进"):
            steps.append({
                "ok": False,
                "blocked": True,
                "needs": "blueprint",
                "title": "放进场景",
                "action": "place_blueprint",
                "error": "等蓝图路径。只给一次：r /Game/某目录/BP_名字。创建蓝图时填过的目录会带到这里。",
                **(ident or {}),
            })
            continue
        if "拾取" in text:
            steps.append({
                "ok": False,
                "blocked": True,
                "needs": "blueprint",
                "title": "用 Pawn 测试拾取",
                "action": "check_pickup",
                "read_only": True,
                "error": "等蓝图放进场景后做只读裁决，不写标签。",
                **(ident or {}),
            })
            continue
    if (
        any((step.get("verify") or [{}])[0].get("kind") == "cpp_build" and step.get("ok") is not False for step in steps)
        and _is_source_fix(query)
        and "测试" in query
        and re.search(r"WASD|前后左右|无法移动", query, re.I)
    ):
        wasd_filter = unique_automation_filter("wasd")
        if wasd_filter:
            steps.append({
                "ok": True,
                "title": "测试 WASD 移动",
                "action": "probe_move",
                "automation_filter": wasd_filter,
                "verify": [{"kind": "automation", "filter": wasd_filter}],
                **task_policy(save=False),
                "max_retries": 1,
            })
    if (
        any((step.get("verify") or [{}])[0].get("kind") == "cpp_build" and step.get("ok") is not False for step in steps)
        and re.search(r"镜头|视角|鼠标", query)
        and "测试" in query
    ):
        for step in steps:
            kinds = [item.get("kind") for item in (step.get("verify") or []) if isinstance(item, dict)]
            if "cpp_build" in kinds and step.get("ok") is not False:
                step["launch_editor"] = True
                step["launch_without_source"] = True
                step["wait_mcp"] = True
        look_filter = unique_automation_filter("look")
        if look_filter:
            steps.append({
                "ok": True,
                "title": "测试鼠标旋转镜头",
                "action": "probe_look",
                "automation_filter": look_filter,
                "verify": [{"kind": "automation", "filter": look_filter}],
                **task_policy(save=False),
                "max_retries": 1,
            })
    if steps:
        return _without_automation_pause(steps, query)
    if compile_hit and "蓝图" in query:
        return [_fail("写不出唯一的蓝图 refPath，不猜。")]
    pickup = _pickup_step(query)
    if pickup is not None:
        return [pickup]
    return None


def _pickup_step(query: str) -> dict | None:
    if not re.search(r"拾取", query):
        return None
    names = _BP_RE.findall(query)
    target = next((name for name in names if "pawn" not in name.lower()), "")
    picker = next((name for name in names if "pawn" in name.lower()), "")
    if not target and names:
        target = names[-1]
    ident = _blueprint_identity(query) or {}
    actor_name = target or str(ident.get("actor_name") or "")
    if not actor_name:
        return {
            "ok": False,
            "blocked": True,
            "needs": "blueprint",
            "title": "用 Pawn 测试拾取",
            "action": "check_pickup",
            "read_only": True,
            "error": "要测哪一个 Actor。只给一次：r /Game/目录/BP_名字。不写标签。",
        }
    return {
        "ok": True,
        "title": "用 Pawn 测试拾取",
        "action": "check_pickup",
        "read_only": True,
        "actor_name": actor_name,
        "picker_name": picker,
        "asset_name": actor_name,
        **{key: ident[key] for key in ("parent",) if ident.get(key)},
    }


def _without_automation_pause(steps: list[dict], query: str) -> list[dict]:
    """拾取按标签查，不再因为没有 Automation 过滤器而暂停。"""
    cleaned = []
    replaced = False
    for step in steps:
        err = str(step.get("error") or "")
        if step.get("needs") == "pie" or "Automation" in err:
            pickup = _pickup_step(query) or {
                "ok": False,
                "blocked": True,
                "needs": "blueprint",
                "title": "用 Pawn 测试拾取",
                "action": "check_pickup",
                "read_only": True,
                "error": "等实例放进场景后做只读裁决，不写标签。",
            }
            if not replaced:
                cleaned.append(pickup)
                replaced = True
            continue
        cleaned.append(step)
    if any(step.get("pickup_handled") for step in cleaned):
        return cleaned
    if re.search(r"拾取", query) and not any(step.get("action") == "check_pickup" for step in cleaned):
        pickup = _pickup_step(query)
        if pickup is not None:
            cleaned.append(pickup)
    return cleaned


def supervise(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        return _fail("没有任务内容。")
    manual = _try_manual_graph(query)
    if manual is not None:
        return manual
    ruled = _from_rules(query)
    if ruled is not None:
        return ruled
    return _from_model(query)
