# agent/supervise.py
"""把一句话收成任务 JSON。路径必须在语料或源码树里唯一，否则返回错误，不猜。"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from config import UE_PROJECT_DIR, UE_SOURCE_ROOT, allow_blueprint
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
        "save": False,
        "pie": False,
        "max_retries": 3,
        "supervised": True,
    }


def _keep(query: str) -> bool:
    return bool(re.search(r"留下修改|保留修改|keep_source", query))


def _filter_in(query: str) -> str:
    match = _FILTER_RE.search(query)
    return match.group(1) if match else ""


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
_LAUNCH_EDITOR_RE = re.compile(r"启动编辑器|打开编辑器|启动\s*UE")
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


def _try_cpp(query: str) -> dict | None:
    if not (re.search(r"编译|error\s*C|构建|\bcpp\b", query, re.I) or _is_cpp_edit(query)):
        return None
    if _BP_RE.search(query) and not _cpp_tokens(query):
        return None
    tokens = _cpp_tokens(query)
    if not tokens:
        return _fail("写不出唯一的 C++ 路径，不猜。")
    rec = _pinned_class(query)
    if rec is None:
        rec = _unique_class(tokens)
    if rec is None:
        return _fail("C++ 类不是唯一的，不猜。")
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
    task = _base(query)
    header = f"Public/{stem}.h"
    files = rel
    if header != rel and (Path(UE_SOURCE_ROOT) / header).is_file():
        files = f"{header} 与 {rel}"
    if _is_cpp_edit(query):
        rule = "可以新增这次要求的函数。声明放头文件，实现放 cpp。不要改别的类。"
    else:
        rule = "不要新增无关函数，不要改别的文件。"
    task["query"] = (
        f"{query.strip()}\n"
        f"只改 {files}。用 read_source 读出要改的段落，再用 apply_unified_diff。"
        "diff 的旧行必须来自最近一次 read_source 的 text，不要带行号。写码成功后文件已变，旧行要用新读取重做；"
        "每段 diff 成功后行号会偏移：下一段 diff 前必须重新 read_source，@@ 旧行号用这次返回的 numbered。"
        f"{rule}"
    )
    task["verify"] = [{"kind": "cpp_build"}]
    task["judge_first"] = not _is_cpp_edit(query)
    task["launch_editor"] = _is_cpp_edit(query)
    if _movement_ready(stem) and re.search(r"WASD|前后左右|无法移动", query, re.I):
        task["judge_first"] = True
        task["query"] += "\n移动实现已经在源码里。这一轮只编译，不要改文件。"
    if rel.endswith("TestActor.cpp") and re.search(r"编译错误|error\s*C", query, re.I):
        text = (Path(UE_SOURCE_ROOT) / rel).read_text(encoding="utf-8", errors="replace")
        if "ThisFunctionDoesNotExist" not in text:
            task["inject_typo"] = True
            task["typo_path"] = rel
    if _keep(query):
        task["keep_source"] = True
    return _attach_filter(task, query)


def cpp_task_for_file(query: str, rel: str) -> dict | None:
    """人用 r 补上的源文件。必须是 Source/AIAgentProject 里已经存在的 .h 或 .cpp。"""
    rel = rel.replace("\\", "/").strip().lstrip("/")
    prefix = "Source/AIAgentProject/"
    if rel.startswith(prefix):
        rel = rel[len(prefix):]
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
    task = _base(query)
    rule = "可以新增这次要求的函数。声明放头文件，实现放 cpp。不要改别的类。" if _is_cpp_edit(query) else "不要新增无关函数，不要改别的文件。"
    task["query"] = (
        f"{query.strip()}\n"
        f"只改 {files}。用 read_source 读出要改的段落，再用 apply_unified_diff。"
        "diff 的旧行必须来自最近一次 read_source 的 text，不要带行号。写码成功后文件已变，旧行要用新读取重做；"
        "每段 diff 成功后行号会偏移：下一段 diff 前必须重新 read_source，@@ 旧行号用这次返回的 numbered。"
        f"{rule}"
    )
    task["verify"] = [{"kind": "cpp_build"}]
    task["judge_first"] = not _is_cpp_edit(query)
    task["launch_editor"] = _is_cpp_edit(query)
    task["title"] = "改 C++ 并编译"
    task["path_filled"] = True
    return _attach_filter(task, query)


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
    return "保存" in query or re.search(r"\bsave\b", query, re.I) is not None


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
        tail = "然后调用 save_assets，asset_paths 传空数组，保存改脏的关卡。不要关编辑器，不要启动 PIE。"
    else:
        tail = "不要保存，不要启动 PIE。"
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
    """反射名去掉 A/U/F 前缀。ATestActor 是 /Script/AIAgentProject.TestActor。"""
    bare = class_name
    if len(bare) > 1 and bare[0] in "AUF" and bare[1].isupper():
        bare = bare[1:]
    return f"/Script/AIAgentProject.{bare}"


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
        steps.append("把蓝图放进关卡。沿用上一格填的蓝图路径，不保存关卡。")
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
                "save": False,
                "pie": False,
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
            "save": False,
            "pie": False,
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
            f"AIAgentProject.{bare}" in content
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
        "save": False,
        "pie": False,
        "pickup_handled": True,
    }
    check = {
        "title": "只读裁决拾取",
        "action": "check_pickup",
        "read_only": True,
        "picker_name": picker.get("actor_name") or "",
        "actor_name": target.get("actor_name") or "",
        "save": False,
        "pie": False,
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
    if not cpp_hit and not project_compile and not named_bp and not editor_hit:
        manual = _try_manual_graph(query)
        if manual is not None:
            return [manual]
    steps = []
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
        steps.append({
            "ok": True,
            "title": "测试 WASD 移动",
            "action": "probe_move",
            "automation_filter": "AIAgent.Pawn.Wasd",
            "verify": [{"kind": "automation", "filter": "AIAgent.Pawn.Wasd"}],
            "save": False,
            "pie": False,
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
        steps.append({
            "ok": True,
            "title": "测试鼠标旋转镜头",
            "action": "probe_look",
            "automation_filter": "AIAgent.Pawn.Look",
            "verify": [{"kind": "automation", "filter": "AIAgent.Pawn.Look"}],
            "save": False,
            "pie": bool(re.search(r"PIE", query, re.I)),
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
