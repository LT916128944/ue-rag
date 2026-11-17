# agent/cpp_spec.py
"""C++ 域：Spec → 确定性执行 kernel → 只读复核（20261117 边界升级，Cpp 计划 §3）。

与 `agent/umg_spec.py` **完全同形**的数据驱动内核。C++ 本来就有成熟落地面
（`apply_unified_diff` / `apply_spec` / `compile_project`，验收硬判据 `cpp_build`，
编译回灌见 `agent/build_error.py`）。本模块只补**「模型产出的代码没被 Spec 化」**这一层：
把「要建哪些类 / 加哪些 UPROPERTY/UFUNCTION / 落在哪个编译单元 / 需要哪些模块依赖」
收口成 C++ Spec，脚本按 Spec 确定性出 op → 落源码 → 编译 → 只读读回源码断言。

**诚实边界（务必读）**：C++ 只能验「**编译过 + 源码里符号（UCLASS/UPROPERTY/UFUNCTION
名）存在**」；**逻辑正确性无法只读验**——`compiled == True` 只代表编得过，**绝不等于功能完成**。
终态 `summarize` 与 `SPEC_HINT` 都写明这条，绝不把「编译过」当功能完成。

「声明式 add → 源码」的 codegen / `apply_spec` 适配器是 P0 接入点：真实的 `cpp` 伪工具集
由 `_cpp_call` 桥到 `core.source_edit`（read_source/apply_unified_diff）与
`core.judge`（cpp_build）；本模块只出确定性 op 清单与只读断言，不重复实现 codegen。
"""
from __future__ import annotations

from agent import spec_pipeline as sp

KIND = "cpp"
JUDGE_KIND = "cpp_build"

# add[].kind 白名单（表外 → 结构化「不支持」）。
ADD_KINDS: tuple[str, ...] = (
    "UCLASS", "UPROPERTY", "UFUNCTION", "USTRUCT", "UENUM", "UINTERFACE",
)

SPEC_HINT = (
    "C++ Spec，JSON：{target_module:'AIAgentProject', build_deps:['EnhancedInput'],"
    " files:[{path:'Public/X.h', add:["
    "{kind:'UCLASS',name:'UX',parent:'UActorComponent'},"
    "{kind:'UPROPERTY',macro:'EditAnywhere',type:'float',name:'MaxHealth',default:'100.f'},"
    "{kind:'UFUNCTION',macro:'BlueprintCallable',ret:'float',name:'GetHealthPercent',"
    "body_hint:'Health/MaxHealth'}]}], verify:['编译通过','存在 UPROPERTY MaxHealth',...]}。"
    "add[].kind 只能取 UCLASS/UPROPERTY/UFUNCTION/USTRUCT/UENUM/UINTERFACE；"
    "build_deps 是模块名字符串数组（写 .Build.cs 的 PublicDependencyModuleNames）。"
    "⚠诚实边界：C++ 只能验『编译过 + 源码里符号（UCLASS/UPROPERTY/UFUNCTION 名）存在』，"
    "逻辑正确性无法只读验——编译过不等于功能完成。"
)

# 写闸白名单：读源码 / 落源码 diff / 编译工程。
_WRITE_ALLOW = ("read_source", "apply_unified_diff", "apply_spec", "compile_project")


def _module():
    import sys

    return sys.modules[__name__]


# ------------------------------------------------------------------ 校验
def validate_spec(spec: dict) -> list:
    errors: list = []
    if not isinstance(spec, dict):
        return ["spec 必须是对象"]

    module = spec.get("target_module")
    if not isinstance(module, str) or not module.strip():
        errors.append(sp.spec_error("bad_module", "缺 target_module（如 AIAgentProject）",
                                    where="target_module"))

    deps = spec.get("build_deps", [])
    if deps is not None:
        if not isinstance(deps, list):
            errors.append(sp.spec_error("bad_build_deps", "build_deps 必须是字符串数组",
                                        where="build_deps"))
        else:
            for i, dep in enumerate(deps):
                if not isinstance(dep, str):
                    errors.append(sp.spec_error("bad_build_deps",
                                                f"build_deps[{i}] 必须是字符串（模块名）",
                                                where=f"build_deps[{i}]"))

    files = spec.get("files")
    if not isinstance(files, list) or not files:
        errors.append(sp.spec_error("no_files", "files 必须是非空数组", where="files"))
        return errors

    for fi, file in enumerate(files):
        where = f"files[{fi}]"
        if not isinstance(file, dict):
            errors.append(sp.spec_error("bad_file", "文件项必须是对象", where=where))
            continue
        path = file.get("path")
        if not isinstance(path, str) or not path.strip():
            errors.append(sp.spec_error("bad_path", f"{where} 缺 path（如 Public/X.h）",
                                        where=f"{where}.path"))
        add = file.get("add")
        if not isinstance(add, list) or not add:
            errors.append(sp.spec_error("no_add", f"{where} 的 add 必须是非空数组",
                                        where=f"{where}.add"))
            continue
        for ai, item in enumerate(add):
            aw = f"{where}.add[{ai}]"
            if not isinstance(item, dict):
                errors.append(sp.spec_error("bad_add", "add 项必须是对象", where=aw))
                continue
            kind = item.get("kind")
            if kind not in ADD_KINDS:
                errors.append(sp.spec_error(
                    "unknown_kind", f"{aw} 的 kind {kind!r} 不在白名单",
                    where=f"{aw}.kind", hint="可用：" + "/".join(ADD_KINDS)))
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                errors.append(sp.spec_error("bad_name", f"{aw} 缺 name", where=f"{aw}.name"))
    return errors


def extract_spec(step: dict) -> dict | None:
    if not isinstance(step, dict):
        return None
    for key in ("spec", "cpp_spec"):
        if isinstance(step.get(key), dict):
            return step[key]
    return None


def write_task(step: dict) -> dict:
    save = bool((step or {}).get("save", False))
    return sp.make_write_task(_WRITE_ALLOW, save=save, pie=False, allow_key=None)


# ------------------------------------------------------------------ 执行 plan
def plan_ops(spec: dict) -> list[dict]:
    """确定性、幂等、无时间/随机的抽象原语清单。"""
    module = spec.get("target_module")
    deps = list(spec.get("build_deps") or [])
    ops: list[dict] = [{"op": "snapshot", "target_module": module, "build_deps": deps}]
    if deps:
        ops.append({"op": "ensure_build_deps", "target_module": module, "deps": deps})
    for file in spec.get("files") or []:
        ops.append({"op": "apply_file", "path": file.get("path"),
                    "add": file.get("add"), "target_module": module})
    ops.append({"op": "compile", "target_module": module})
    ops.append({"op": "readback",
                "files": [{"path": f.get("path")} for f in (spec.get("files") or [])]})
    return ops


def _result(ok: bool, *, tools=None, readback=None, error: str = "") -> dict:
    return {"ok": ok, "tools": list(tools or []), "readback": dict(readback or {}), "error": error}


def exec_op(op: dict, call, write: dict) -> dict:
    """抽象原语 → 真实调用。snapshot/readback 容忍空 data 仍返回 ok。"""
    kind = op.get("op")
    module = op.get("target_module") or ""

    if kind == "snapshot":
        # 只拿基线（文件可能还不存在）：读不到也 ok，快照不是裁判。
        return _result(True, tools=[], readback={})

    if kind == "ensure_build_deps":
        r = call("cpp", "ensure_build_deps",
                 {"module": module, "deps": list(op.get("deps") or [])}, write)
        ok = bool(isinstance(r, dict) and r.get("ok"))
        return _result(ok, tools=["ensure_build_deps"],
                       error="" if ok else "写 .Build.cs 模块依赖失败")

    if kind == "apply_file":
        # 声明式 add → 源码，交给 cpp 适配器（复用 apply_spec / apply_unified_diff）。
        r = call("cpp", "apply_spec",
                 {"path": op.get("path"), "add": op.get("add") or [],
                  "target_module": module}, write)
        ok = bool(isinstance(r, dict) and r.get("ok"))
        err = "" if ok else str((r or {}).get("error") or "落源码失败") if isinstance(r, dict) else "落源码失败"
        return _result(ok, tools=["apply_spec"], error=err)

    if kind == "compile":
        # 编译工程（cpp_build 硬判据）：复用 compile_project / core.judge 的 cpp_build。
        r = call("cpp", "compile_project", {"target_module": module}, write)
        ok = bool(isinstance(r, dict) and r.get("ok"))
        data = r.get("data") if isinstance(r, dict) else None
        if isinstance(data, dict) and data.get("compiled") is False:
            ok = False
        return _result(ok, tools=["compile_project"], readback={"compiled": ok},
                       error="" if ok else "编译未通过")

    if kind == "readback":
        sources: list[dict] = []
        for file in op.get("files") or []:
            path = file.get("path")
            text = _read_text(call, path, write)
            sources.append({"path": path, "text": text})
        return _result(True, tools=["read_source"], readback={"sources": sources})

    return _result(False, error=f"未知原语 {kind!r}")


def _read_text(call, path, write) -> str:
    """按 200 行上限分块读回整份源码。读不到 → 空串（不阻断 readback op）。"""
    text = ""
    start = 1
    for _ in range(200):  # 200*200=40000 行足够，且这里只读，不写
        got = call("cpp", "read_source",
                   {"path": path, "start_line": start, "end_line": start + 199}, write)
        if not (isinstance(got, dict) and got.get("ok")):
            break
        data = got.get("data")
        chunk = ""
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            chunk = data["text"]
        elif isinstance(data, str):
            chunk = data
        text += chunk
        if len(chunk.splitlines()) < 200:
            break
        start += 200
    return text


# ------------------------------------------------------------------ 只读复核
def assertions(spec: dict) -> list:
    out: list = [{"path": "compiled", "op": "eq", "value": True}]
    for i, file in enumerate(spec.get("files") or []):
        for item in (file.get("add") or []):
            name = item.get("name") if isinstance(item, dict) else None
            if isinstance(name, str) and name:
                out.append({"path": f"sources.{i}.text", "op": "contains", "value": name})
    return out


def verify_spec(spec: dict, readback: dict) -> list:
    """compiled==True + 源码读回含每个 UCLASS/UPROPERTY/UFUNCTION 名（只读复核）。"""
    return sp.eval_assertions(assertions(spec), readback or {})


def summarize(spec: dict) -> str:
    names: list[str] = []
    files: list[str] = []
    for file in spec.get("files") or []:
        files.append(str(file.get("path")))
        for item in (file.get("add") or []):
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
    return (
        f"{spec.get('target_module')} 落地 {len(files)} 个文件（{'、'.join(files)}），"
        f"编译通过且源码读回含符号：{'、'.join(names)}。"
        "注意：只验『编译过 + 符号存在』，逻辑正确性未验证——编译过不等于功能完成。"
    )


# ------------------------------------------------------------------ 路由识别（宁缺毋滥）
_STRONG = (
    "c++", "cpp", ".h", ".cpp", "uproperty", "ufunction", "uclass", "ustruct",
    "uenum", "uinterface", "加个类", "加一个类", "新增一个类", "编译工程",
    "射速", "组件", "ucomponent",
)


def recognize(text: str) -> bool:
    """一句话是否稳定属于 C++ 域。命中 C++ 符号/改码特征词才算，场景摆放不算。"""
    blob = str(text or "").lower()
    if not blob:
        return False
    return any(tok.lower() in blob for tok in _STRONG)


# ------------------------------------------------------------------ 真实落地桥（P0 接入点）
def _cpp_call(toolset: str, tool: str, args: dict | None = None, task: dict | None = None) -> dict:
    """'cpp' 伪工具集 → 本地源码编辑 / 编译（惰性 import，不碰编辑器）。

    复用 `core.source_edit.read_source / apply_unified_diff` 与 `core.judge` 的 `cpp_build`；
    其余工具集回落到 `sp.default_call`。声明式 add→源码的 codegen 待 P0 模板接入：
    此时可给 `apply_spec` 传 `content`（整份源码），或改用 `apply_unified_diff` 传 `diff`。
    """
    args = args or {}
    if toolset != "cpp":
        return sp.default_call(toolset, tool, args, task)
    try:
        if tool == "read_source":
            from core.source_edit import read_source
            return read_source(str(args.get("path") or ""),
                               int(args.get("start_line") or 1),
                               int(args.get("end_line") or 1))
        if tool == "apply_unified_diff":
            from core.source_edit import apply_unified_diff
            return apply_unified_diff(str(args.get("path") or ""), str(args.get("diff") or ""))
        if tool == "apply_spec":
            return _write_spec_file(args)
        if tool == "ensure_build_deps":
            return _ensure_build_deps(args)
        if tool == "compile_project":
            from core.judge import judge
            return judge({"kind": "cpp_build"}, task=task or {})
    except Exception as exc:  # noqa: BLE001 —— 桥内异常结构化成 ok=False
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": False, "error": f"未知 cpp 工具 {tool!r}"}


def _write_spec_file(args: dict) -> dict:
    """apply_spec 落地：有 content 直接写；只有声明式 add 时诚实返回「codegen 待 P0」。"""
    path = str(args.get("path") or "")
    if not path:
        return {"ok": False, "error": "apply_spec 缺 path"}
    content = args.get("content")
    if not isinstance(content, str) or not content:
        return {"ok": False,
                "error": "声明式 add→源码 codegen 待 P0 接入（请给 apply_spec 传 content，"
                        "或改用 apply_unified_diff 传 diff）"}
    from core.source_edit import resolve_source
    target = resolve_source(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"ok": True, "data": {"path": path, "lines": len(content.splitlines())}}


def _ensure_build_deps(args: dict) -> dict:
    """把模块依赖写进 .Build.cs（P3 覆盖项，路径待 P0 确认）。找不到源码就诚实跳过，不瞎写。"""
    module = str(args.get("module") or "")
    deps = [str(d) for d in (args.get("deps") or []) if str(d).strip()]
    if not module or not deps:
        return {"ok": True, "data": {"module": module, "added": []}}
    # 典型路径 Source/<Module>/<Module>.Build.cs；P3 会按项目实际结构调整。此处只读探一下。
    return {"ok": True,
            "data": {"module": module, "deps": deps, "skipped": True,
                     "note": ".Build.cs 依赖写入待 P3 确认路径（Source/<Module>/<Module>.Build.cs）"}}


# ------------------------------------------------------------------ 对外入口
def build(step: dict, **kwargs) -> dict:
    """交付接口：跑完整闭环。无注入 call 时用 _cpp_call 桥到本地源码编辑 / 编译。"""
    if not kwargs.get("call"):
        kwargs["call"] = _cpp_call
    return sp.build_domain(_module(), step, **kwargs)
