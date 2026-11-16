# server/prompt_builder.py
"""提示词装配器（20261116-1530 HTML 入口层计划 §5）。纯函数、零 IO、零副作用。

它只做一件事：把「人的原句 + 带角色的附件」拼成**将要交给 CLI 的那一行原文**，
并说清每一段从哪来、哪些组合下游会静默吃掉。

六条规则（计划 §5，每条都有离线断言，见 `eval/check_entry_paths.py`）：

* A1 人的原句一字不动放最前。不 trim 内部空白、不改标点——`agent.plan_windows._clause_rows`
  只按 `，,；;。` 切半句，改一个标点就等于改拆句结果。
* A2 附件一律 `。` 开头、自成一句、顺序 = chip 顺序。换行不切半句，只有 `。` 切。
* A3 每条附件句都要能被 `_clause_accounted` 认成「引用」（靠 `_is_reference_clause`）。
* A4 全篇 `/Game` 路径最多一条：`_intent_query_paths` 见到两条就返回空表，等于两个都不生效。
* A5 源码/文档 token 一律相对：`core.source_edit.resolve_source` 拒绝绝对路径与 `..`。
* A6 外部文件（浏览器只给文件名）不进 prompt：假路径会改归地。

边界：只 import `config`（架构检查 `eval/check_architecture.py` 的 server 行）。不 import
`agent`、不碰编辑器、不调模型、不写文件——「UI 不许做第二套裁决」是这一层的立身之本。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import UE_PROJECT_DIR, UE_SOURCE_ROOT  # noqa: E402

ROLES = ("level", "asset", "dir", "source", "doc", "other")

# 角色 → 附件句的前缀。逐个对应计划 §4.3 的表。改这里就是改下游读到的口径。
REF_LABEL = {
    "level": "参考关卡",
    "asset": "参考资产",
    "dir": "参考路径",
    "source": "参考文件",
    "doc": "参考文档",
    "other": "参考路径",
}

ROLE_NAME = {
    "level": "关卡",
    "asset": "资产",
    "dir": "目录",
    "source": "源码",
    "doc": "文档",
    "other": "未识别",
}

# 铁律 2 的同栏目录：只读侧边栏不列、装配器不给 token。
BLOCKED_DIRS = ("Binaries", "Intermediate", "Saved", "DerivedDataCache", ".vs")

_SUFFIX_ROLE = {
    "umap": "level",
    "uasset": "asset",
    "h": "source",
    "cpp": "source",
    "cs": "source",
    "md": "doc",
    "json": "doc",
    "txt": "doc",
    "ini": "doc",
    "yml": "doc",
    "yaml": "doc",
}

_GAME_PATH_RE = re.compile(r"/Game(?:/[A-Za-z0-9_]+)+")
_CJK_OR_SPACE_RE = re.compile(r"[\u4e00-\u9fff\s]")
_DRIVE_RE = re.compile(r"^[A-Za-z]:/")


def _posix(path) -> str:
    return str("" if path is None else path).replace("\\", "/")


def is_blocked(rel) -> bool:
    """命中 Binaries/Intermediate/Saved/DDC/.vs 任一段。只按段比，不做子串匹配。"""
    text = _posix(rel).strip("/")
    if not text:
        return False
    return any(part in BLOCKED_DIRS for part in text.split("/"))


def classify(path, is_dir: bool = False) -> str:
    """后缀 → 角色。目录单独判断（is_dir 由调用方给，与后缀无关）。"""
    if is_dir:
        return "dir"
    text = _posix(path)
    suffix = ""
    name = text.rsplit("/", 1)[-1]
    if "." in name:
        suffix = name.rsplit(".", 1)[-1].lower()
    if not suffix:
        return "dir" if text else "other"
    return _SUFFIX_ROLE.get(suffix, "other")


def looks_absolute(path) -> bool:
    text = _posix(path)
    return bool(_DRIVE_RE.match(text)) or text.startswith("//") or text.startswith("/")


def strip_root(abs_path, root) -> str | None:
    """绝对路径削到工程根下，返回工程相对路径；不在根下返回 None（不猜）。

    取**最外层**出现位置：路径里可能还有 `Source/AIAgentProject` 这种同名子目录，
    用 rfind 会匹配到内层，把源码路径削成 `Public/X.h` 而判错根（20261116 实测缺陷 3）。
    """
    text = _posix(abs_path)
    base = _posix(root).strip("/")
    if not base:
        return None
    lower = text.lower()
    needle = "/" + base.lower() + "/"
    at = lower.find(needle)
    if at >= 0:
        return text[at + len(base) + 2:]
    prefix = base.lower() + "/"
    if lower.startswith(prefix):
        return text[len(base) + 1:]
    return None


def _content_rel_prefix() -> str:
    return "Content/"


def _source_rel_prefix() -> str:
    """`Source/<模块名>/` 的工程相对前缀，从 UE_SOURCE_ROOT 反推。"""
    try:
        rel = Path(UE_SOURCE_ROOT).resolve().relative_to(Path(UE_PROJECT_DIR).resolve())
    except (ValueError, OSError):
        return ""
    return _posix(rel).strip("/") + "/"


def game_paths_in(text) -> list[str]:
    """句子里的 `/Game/...` 路径，去重保序。"""
    found: list[str] = []
    for hit in _GAME_PATH_RE.findall(str(text or "")):
        if hit not in found:
            found.append(hit)
    return found


def token_of(path, role: str | None = None, *, root=None) -> dict:
    """路径 → 能进句子里的 token。返回 `{ok, role, token, rel, absolute, reason}`。

    | 角色 | 工程相对 | token |
    | --- | --- | --- |
    | 关卡/资产/目录 | `Content/Maps/L_NewTest3.umap` | `/Game/Maps/L_NewTest3` |
    | 源码 | `Source/<模块>/Public/X.h` | `Public/X.h`（`resolve_source` 只收相对） |
    | 文档 | `documents/harness/boundaries.md` | 同左（相对 `UE_PROJECT_DIR`） |

    `reason` 非空即拒：`outside_root` / `blocked_dir` / `not_in_content` /
    `not_in_source_root` / `empty`。
    """
    raw = _posix(path)
    kind = role or classify(raw, False)
    absolute = looks_absolute(raw)
    base_root = root if root is not None else UE_PROJECT_DIR
    rel_from_root = strip_root(raw, base_root) if absolute else raw
    out = {"ok": False, "role": kind, "token": "", "rel": raw, "absolute": absolute, "reason": ""}

    if rel_from_root is None:
        out["reason"] = "outside_root"
        return out
    if is_blocked(rel_from_root):
        out["reason"] = "blocked_dir"
        out["rel"] = rel_from_root
        return out
    rel = rel_from_root.strip("/")
    out["rel"] = rel
    if not rel:
        out["reason"] = "empty"
        return out

    content_prefix = _content_rel_prefix()
    source_prefix = _source_rel_prefix()
    content_rel = rel[len(content_prefix):] if rel.startswith(content_prefix) else None
    source_rel = rel[len(source_prefix):] if source_prefix and rel.startswith(source_prefix) else None
    # 已经归一化过的源码 token（`Public/X.h`）原样认，装配可幂等。
    if source_rel is None and re.match(r"^(Public|Private)/[^/]+\.(h|cpp)$", rel):
        source_rel = rel

    if kind in ("level", "asset", "dir"):
        if content_rel is None:
            out["reason"] = "not_in_content"
            return out
        out["token"] = "/Game/" + re.sub(r"\.[A-Za-z0-9]+$", "", content_rel)
        out["ok"] = True
        return out
    if kind == "source":
        if source_rel is None:
            out["reason"] = "not_in_source_root"
            return out
        out["token"] = source_rel
        out["ok"] = True
        return out
    # 文档与未识别：相对工程根，纯上下文，不参与落点。
    out["token"] = rel
    out["ok"] = True
    return out


_REASON_TEXT = {
    "outside_root": "不在工程根下（浏览器只给得出工程内的相对路径）",
    "blocked_dir": "命中屏蔽目录（Binaries/Intermediate/Saved/DerivedDataCache/.vs）",
    "not_in_content": "不是 Content 下的路径，`/Game` 无从换算",
    "not_in_source_root": "不在 UE_SOURCE_ROOT 下，源码只能用相对路径",
    "empty": "路径是空的",
}


def _warn(level: str, code: str, msg: str) -> dict:
    return {"level": level, "code": code, "msg": msg}


def build(text, atts, *, root=None, allow_any_level_path: bool = False) -> dict:
    """装配：人的原句 + 附件句。返回 `{ok, prompt, orig, lines, accepted, warnings}`。

    `atts` 每项：`{id, path, role?, is_dir?, external?, name?}`。
    `external=True` 表示来自资源管理器（浏览器拿不到真实路径）——一律不进 prompt（A6）。
    """
    orig = str("" if text is None else text)
    warnings: list[dict] = []
    accepted: list[dict] = []

    for att in atts or []:
        item = dict(att or {})
        path = item.get("path") or ""
        role = item.get("role") or classify(path, bool(item.get("is_dir")))
        if item.get("external"):
            name = item.get("name") or _posix(path).rsplit("/", 1)[-1]
            warnings.append(_warn(
                "yellow", "external_file",
                f"「{name}」来自资源管理器，浏览器拿不到真实路径——已排除，不进 prompt。"
                "要引用就把它从侧边栏拖进来。",
            ))
            continue
        tok = token_of(path, role, root=root)
        if not tok["ok"]:
            why = _REASON_TEXT.get(tok["reason"], tok["reason"])
            warnings.append(_warn("red", "att_bad_path", f"附件「{path}」不能用：{why}。"))
            continue
        if tok["absolute"] and tok["role"] == "source":
            warnings.append(_warn(
                "info", "source_relativized",
                f"源码绝对路径已相对化为 {tok['token']}。",
            ))
        if _CJK_OR_SPACE_RE.search(tok["token"]):
            warnings.append(_warn(
                "red", "token_illegal_chars",
                f"token「{tok['token']}」含中文或空格，下游解析不了。",
            ))
            continue
        accepted.append({
            "id": str(item.get("id") or ""),
            "role": tok["role"],
            "token": tok["token"],
            "path": path,
            "absolute": tok["absolute"],
        })

    # A4 / 去重：同一 token 原句里已有就不再追加。原句的那一条占掉整个 /Game 名额。
    seen: dict[str, str] = {}
    orig_game = game_paths_in(orig)
    for path in orig_game:
        seen[path] = "text"
    if orig_game:
        seen["__game"] = orig_game[0]

    att_lines: list[tuple[str, dict]] = []
    for item in accepted:
        token = item["token"]
        if seen.get(token):
            warnings.append(_warn(
                "yellow", "token_already_in_text",
                f"「{token}」原句里已经有了，不重复追加（重复出现会变成两条 /Game 路径，"
                "下游只认唯一一条）。",
            ))
            continue
        if token.startswith("/Game/"):
            if seen.get("__game"):
                warnings.append(_warn(
                    "red", "multi_game_path",
                    f"已有一条 /Game 路径「{seen['__game']}」，再带「{token}」会让 "
                    "`_intent_query_paths` 返回空表（两条就不是唯一）。只留一个，"
                    "或把它写进正文说清关系。",
                ))
                continue
            seen["__game"] = token
        seen[token] = "att"
        att_lines.append(("。" + REF_LABEL.get(item["role"], REF_LABEL["dir"]) + "：" + token, item))

    # 角色与路径类型对不上：resolve_level 收任意 /Game 路径，会把蓝图当关卡。
    for item in accepted:
        if (item["role"] == "level" and item["token"].startswith("/Game/")
                and not item["token"].startswith("/Game/Maps/") and not allow_any_level_path):
            warnings.append(_warn(
                "yellow", "level_outside_maps",
                f"关卡角色指向 {item['token']}，不在 /Game/Maps 下——"
                "`resolve_level` 会照收，确认它真的是关卡。",
            ))
        if item["role"] == "dir" and not item["token"].startswith("/Game/"):
            warnings.append(_warn(
                "info", "dir_not_game",
                f"目录附件「{item['token']}」不是 /Game 路径，只当上下文。",
            ))

    # A1：原句一字不动。A2：附件句各成一句，顺序 = chip 顺序。
    prompt = (orig if orig else "") + "".join(line for line, _item in att_lines)

    if not orig.strip():
        warnings.append(_warn(
            "yellow", "empty_text",
            "正文是空的，只有附件——下游会把它当成一条没说需求的引用句。",
        ))

    red = any(w["level"] == "red" for w in warnings)
    final_paths = game_paths_in(prompt)
    if len(final_paths) > 1 and not red:
        warnings.append(_warn(
            "red", "multi_game_path_final",
            f"最终 prompt 里有 {len(final_paths)} 条 /Game 路径，下游只认唯一一条。",
        ))
        red = True

    lines = []
    if orig:
        lines.append({"kind": "text", "text": orig, "attId": ""})
    for line, item in att_lines:
        lines.append({"kind": "att", "text": line, "attId": item["id"]})

    return {
        "ok": (not red) and bool(prompt.strip()),
        "prompt": prompt,
        "orig": orig,
        "lines": lines,
        "accepted": accepted,
        "warnings": warnings,
    }


def describe(res: dict) -> list[str]:
    """把装配结果说成人话（预览下方那块「装配说明」）。逐行说清这段字从哪来。"""
    rows: list[str] = []
    rows.append(f"正文：{len(res.get('orig') or '')} 字，原样放在最前（A1：不改标点、不 trim）。")
    for item in res.get("accepted") or []:
        rows.append(
            f"附件 {item['id'] or '—'}：{ROLE_NAME.get(item['role'], item['role'])} → "
            f"{item['token']}，追加一句「。{REF_LABEL.get(item['role'], '参考路径')}：{item['token']}」"
        )
    rows.append("通道：写请求文件 → 起 agent 进程（多窗口 `--plain`，非交互 stdin）。")
    rows.append("退出码：0 正常 / 1 输入或环境有问题 / 2 另一个 run 在跑（不是失败）。")
    return rows
