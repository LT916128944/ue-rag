"""受控求解窗（L1 solve）：表外需求的第二条路。

查表（L0）接不住的需求，先给一扇 solve 扇：盘点（monolith discover，只读）→
方案（模型产出步骤规格：现有 action 组合或 apply_spec 的 C++ spec）→ 执行（逐原语，
遇错读原文最多重试一次）→ 编译回灌（apply_spec 后强制 build，错误原文进下一轮）→
收口（有 automation 判据才 verify，没有就诚实失败）。

预算闸门（这是「有一定解决问题能力」和「失控 agent」的分界线）：
- 编辑器调用 ≤UE_SOLVER_EDITOR_CALLS（缺省 8）/扇（执行与重试）
- 元数据调用 ≤UE_SOLVER_META_CALLS（缺省 12）/扇（discover / describe / list_automation_tests）
- 编译轮次 ≤UE_SOLVER_BUILD_ROUNDS（缺省 3）
- 模型调用 ≤UE_SOLVER_MODEL_CALLS（缺省 6）
- namespace 白名单 UE_SOLVER_NAMESPACES（缺省 editor,mesh,niagara,asset）
总开关 UE_SOLVER_ENABLED 缺省 false：人开了才走。预算烧完 / 闸门拒绝 → 降级回
capability_gap 的诚实路径；整条轨迹落 data/distill/solver_trajectories.jsonl。

写策略不加特权：save / pie / allow_writes 唯一来源仍是任务书与 session_task.json，
执行原语一律经 backends.monolith.exec 的 write_block，本模块不再开第二道口。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

# 原句带着「改变」语气而方案全是只读步骤时，automation 收口再绿也只是改动前的
# 旧状态——测试不许替没实现的需求报成功（20261028-185716 实测：求解扇 0 次编译、
# 0 次写，拿本来就绿的 AIAgent.Fps.Hit 报了「求解收口成功」）。
# 这是白名单。漏掉的状态动词（「把门打开」）不在这里扩：read_only_plan 进失败模式
# 影子，人审时再补词，并且必须同时加 check_solver_patch 用例。
_CHANGE_IMPLYING_RE = re.compile(r"改|调整|设置|倍|实现|新建|创建|删除|换成|提高|降低|加速|加上|去掉")
# 基础设施动作：让编辑器到位 / 观察 / 编译已有代码，本身不交付任何「改动」。
# 20261028-191105 实测：方案只有一扇 load_level 也被算成「改东西」，绕过守卫。
_SOLVER_INFRA_ACTIONS = frozenset({
    "load_level", "save_packages", "list_dirty_packages", "create_empty_map",
    "trigger_build", "live_compile", "get_build_errors", "get_build_status",
    "get_build_summary", "search_build_output", "get_compile_output",
    "get_recent_logs", "search_logs", "tail_log", "get_log_categories", "get_log_stats",
    "get_crash_context", "get_module_status", "get_viewport_info", "get_stat_group_values",
    "list_automation_tests", "list_errored_blueprints",
})
_SOLVER_READ_ACTIONS = frozenset({
    "run_automation_tests", "run_automation_test", "run_pie_smoke", "poll_pie_smoke",
})

# 缺省白名单：20261028 实测 monolith 真实存在的 namespace 只有这几个——asset 在
# monolith 里是 Unknown namespace（官方后端才有），留着只会白烧一次 discover 并在
# 轨迹里留噪音。blueprint / material 的 discover 是只读，写仍走 UE_ALLOW_* 图写闸。
DEFAULT_NAMESPACES = ("editor", "mesh", "niagara", "blueprint", "material")

_MAX_STEPS = 8
# 20261028 载具实测：editor 有 49 个 action，截到 8 个全剩 build/log 类，
# 模型永远看不到放 Actor 的原语，方案必缺「放进关卡」这一步。名字本身很便宜，
# 截断只对超长正文有意义，对 action 名没意义——放宽到 96（覆盖现有最大 namespace）。
_MAX_DISCOVER_ACTIONS = 96
_MAX_FILES = 4
# 与 agent/decompose.safe_files 的护栏一致（20261028 载具实测放宽到 12000）。
_MAX_CONTENT = 12000

_SYSTEM = (
    "你只输出 JSON。为这句表外需求给一个最小执行方案。"
    "可用原语只有「已盘点」清单里的 namespace.action、patch 和 apply_spec。"
    "盘点清单里没有的 action 一个都不许编（清单已按写闸过滤，写闸不放行的不会出现）。"
    "步骤总数不超过 "
    f"{_MAX_STEPS}。apply_spec.files 每项是 {{path, content}}，path 只能是 "
    f"Public/名字.h 或 Private/名字.cpp，最多 {_MAX_FILES} 个、每个不超过 {_MAX_CONTENT} 字。"
    "content 要完整可编译，不许写省略号或「其余同上」占位。"
    "改已有类里的常量/默认值/属性，优先用 patch（对已有文件做一处替换），不要新建文件："
    '{"patch":{"path":"Public/名字.h","old":"原文一字不差且全文唯一","new":"新文"}}。'
    "old 必须逐字照抄「源码定位」或常识里的那一行，包含空格与分号；命中不唯一或不命中都会失败。"
    "new 里需要数值运算时直接写算好的结果（如 900 变 90000），不要写表达式。"
    "原句要求改变什么时，方案必须至少有一个改东西的步骤（patch / apply_spec 或写原语）；"
    "开关卡、保存、编译、查日志、跑测试这类基础设施动作不算改东西；"
    "只有验收步骤的方案会在收口被拒——改动前的旧测试通过了也不算完成。"
    "patch 与 apply_spec 都会触发编译并重启编辑器，重启后当前关卡会回到默认地图——"
    "排在它们之后的编辑器步骤，第一步必须是 editor.load_level 回目标关卡，"
    "否则放 Actor 会放进错误的地图。"
    '形状：{"steps":[{"tool":"namespace.action","args":{}}或'
    '{"patch":{...}}或{"apply_spec":{"files":[...]}},'
    '{"automation":"收口过滤器"}]}。'
    "最后给一个 automation 过滤器当收口判据：过滤器只能取「automation_tests」清单里"
    "出现的前缀或完整名；清单里挑不出贴切的就省略它——编一个不存在的测试名，求解会诚实失败。"
)


# ---------------------------------------------------------------- 开关与预算

def _env(name: str, default: str) -> str:
    try:
        from config import ENV_PATH, env_value

        raw = env_value(ENV_PATH, name)
    except Exception:
        raw = None
    if raw is None or not str(raw).strip():
        raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().strip('"').strip("'")


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(_env(name, str(default))))
    except (TypeError, ValueError):
        return default


def solver_enabled() -> bool:
    """UE_SOLVER_ENABLED 缺省 false。空串也当没开，不许含糊。"""
    return _env("UE_SOLVER_ENABLED", "false").lower() in ("1", "true", "yes", "on")


def solver_budget() -> dict:
    return {
        "editor_calls": _env_int("UE_SOLVER_EDITOR_CALLS", 8),
        "meta_calls": _env_int("UE_SOLVER_META_CALLS", 12),
        "build_rounds": _env_int("UE_SOLVER_BUILD_ROUNDS", 3),
        "model_calls": _env_int("UE_SOLVER_MODEL_CALLS", 6),
    }


def solver_namespaces() -> tuple[str, ...]:
    raw = _env("UE_SOLVER_NAMESPACES", ",".join(DEFAULT_NAMESPACES))
    found = []
    for part in raw.replace("，", ",").split(","):
        name = part.strip().lower()
        if name and name not in found:
            found.append(name)
    return tuple(found)


class Budget:
    """预算计数器。spend 返回 False 就是烧尽，调用方必须停，不许绕。

    显式 limits 没写 meta_calls 时补上缺省 12：旧测试只声明执行预算，
    discover / describe 改走元数据额度后仍能盘点，不会把执行次数吃掉。
    """

    def __init__(self, limits: dict | None = None) -> None:
        self.limits = dict(limits or solver_budget())
        if "meta_calls" not in self.limits:
            self.limits["meta_calls"] = _env_int("UE_SOLVER_META_CALLS", 12)
        self.used: dict[str, int] = {key: 0 for key in self.limits}

    def spend(self, key: str, n: int = 1) -> bool:
        limit = int(self.limits.get(key, 0))
        if self.used.get(key, 0) + n > limit:
            return False
        self.used[key] = self.used.get(key, 0) + n
        return True

    def exhausted(self, key: str) -> bool:
        return self.used.get(key, 0) >= int(self.limits.get(key, 0))

    def snapshot(self) -> dict:
        return {key: {"used": self.used.get(key, 0), "limit": int(self.limits.get(key, 0))}
                for key in self.limits}


# ---------------------------------------------------------------- namespace 挑选

_NS_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("niagara", ("粒子", "特效", "爆炸", "烟雾", "火焰", "拖尾", "闪电", "niagara")),
    ("mesh", ("网格", "静态", "模型", "mesh", "碰撞体", "lod")),
    ("asset", ("资产", "资源", "导入", "引用", "asset", "目录")),
    # editor 是缺省：查场景、放 Actor、开关卡都在这里。
)


def pick_namespaces(text: str, limit: int = 2) -> list[str]:
    """按范围动词挑 namespace。editor 永远在内（放 Actor / 查场景的兜底，缺它方案
    必缺「放进关卡」这步——20261028 载具实测）。关键词用多字词，不收单字：
    「开火」里的「火」把 niagara 拽进来、editor 反而落选，就是这么误命中的。"""
    allowed = set(solver_namespaces())
    body = str(text or "")
    picked: list[str] = []
    if "editor" in allowed:
        picked.append("editor")
    for name, words in _NS_HINTS:
        if name not in allowed or name in picked:
            continue
        if any(word in body for word in words):
            picked.append(name)
        if len(picked) >= limit:
            return picked
    if not picked and "editor" in allowed:
        picked.append("editor")
    return picked[:limit]


# ---------------------------------------------------------------- 源码定位（本地，零预算）

_CODE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 中文域词 → 源码符号。只放项目里真实出现的类名/属性词，不求全。
    ("射速", ("Speed", "FireRate", "Cooldown", "FireInterval", "Bullet")),
    ("子弹", ("Bullet", "Projectile", "Speed")),
    ("手榴弹", ("Grenade", "Explode", "Explosion")),
    ("爆炸", ("Grenade", "Explode", "Explosion")),
    ("金币", ("Coin", "Pickup")),
    ("门", ("Door",)),
    ("平台", ("Platform", "MovingPlatform")),
    ("伤害", ("Damage", "Health")),
    ("血", ("Health", "Damage")),
    ("巡逻", ("Patrol",)),
    ("载具", ("Vehicle",)),
    ("跳跃", ("Jump", "JumpZ")),
    ("倒计时", ("Timer", "Countdown", "Count")),
    ("拾取", ("Pickup", "Coin")),
)


def locate_code(text: str, max_hits: int = 16) -> list[dict]:
    """在 UE_SOURCE_ROOT 的 .h/.cpp 里找与需求相关的行。纯本地扫描，不花编辑器
    预算、不叫模型；失败/空都返回 []，不阻塞求解。

    20261028-183641 实测：模型拿着「把射速改 100 倍」却只能编 run_python——
    因为它根本不知道射速定义在哪个文件哪一行。给它 file:line + 一行原文，
    它就能走 patch（改已有文件）这条真路，而不是瞎猜一个编辑器原语。

    20261028-185716 再修两点：字母序 Private/ 排在 Public/ 前面，8 条命中全被
    .cpp 里的用法行占满，头文件里的定义行根本进不了计划载荷；改成头文件优先、
    每文件最多 3 行，定义行（赋值/声明/UPROPERTY）优先于用法行。"""
    import re as _re
    from pathlib import Path

    try:
        from config import UE_SOURCE_ROOT

        root = Path(UE_SOURCE_ROOT)
    except Exception:
        return []
    if not root.is_dir():
        return []
    body = str(text or "")
    terms: list[str] = []
    for word in _re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", body):
        up = word.upper()
        if up in ("FOR", "THE", "AND", "NEW", "SET", "GET"):
            continue
        if word not in terms:
            terms.append(word)
    for word, symbols in _CODE_HINTS:
        if word in body:
            for sym in symbols:
                if sym not in terms:
                    terms.append(sym)
    if not terms:
        return []
    try:
        files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".h", ".cpp")]
    except OSError:
        return []
    # 头文件（定义的家）排前；同扩展名内 Public/ 排前（工程习惯：声明在 Public）。
    files.sort(key=lambda p: (
        0 if p.suffix.lower() == ".h" else 1,
        0 if "public" in str(p.parent).lower() else 1,
        str(p).lower(),
    ))
    hits: list[dict] = []
    seen: set[tuple[str, int]] = set()
    per_file: dict[str, int] = {}
    for path in files:
        if len(hits) >= max_hits:
            break
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        # 定义行先收集，用法行兜底：同文件内两遍扫，定义行不占每文件配额的前排。
        file_def: list[tuple[int, str]] = []
        file_use: list[tuple[int, str]] = []
        for no, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            low = line.lower()
            if not any(term.lower() in low for term in terms):
                continue
            bucket = file_def if _re.search(r"=\s*[0-9.]|constexpr|UPROPERTY|class\s+\w|void\s+\w+\s*\(", stripped) else file_use
            bucket.append((no, stripped))
        for no, stripped in file_def + file_use:
            if len(hits) >= max_hits or per_file.get(rel, 0) >= 3:
                break
            key = (rel, no)
            if key in seen:
                continue
            seen.add(key)
            per_file[rel] = per_file.get(rel, 0) + 1
            hits.append({"file": rel, "line": no, "text": stripped[:160]})
    return hits


# ---------------------------------------------------------------- 盘点（只读）

def _monolith_discover(namespace: str) -> dict:
    from backends.monolith import discover

    return discover(namespace)


def inventory(text: str, budget: Budget, discover_fn=None, task: dict | None = None) -> dict:
    """按范围动词挑 1-2 个 namespace 做 discover。返回 {namespaces, actions, error}。

    discover 是只读 action（is_read 放行，不消耗写闸），但每次 HTTP 往返记一笔
    编辑器预算。输出截到决策字段，不把整份 action 列表塞进上下文。

    两个 20261028-183641 的实证修正：
    - 写闸感知：清单先过 backends.policy.write_block——run_python 这类「任务旗标
      没放行就必被拒」的原语不进清单，规划者根本看不见，方案就不会编一条注定
      被拒的步骤再白烧一次预算加一次无谓重试。
    - discover 单个 namespace 失败不再中断整轮盘点（asset 在 monolith 是
      Unknown namespace，一 break 连 editor 的盘点结果都拿不到），改记错误继续；
      全部失败才算盘点失败。"""
    discover_fn = discover_fn or _monolith_discover
    namespaces = pick_namespaces(text)
    found: dict[str, list[str]] = {}
    errors: list[str] = []
    for ns in namespaces:
        if not budget.spend("meta_calls"):
            errors.append("元数据预算烧尽，盘点提前结束")
            break
        result = discover_fn(ns)
        if not result.get("ok"):
            errors.append(f"discover {ns} 失败：{str(result.get('error') or '')[:120]}")
            continue
        rows = [str(item.get("name") or "") for item in (result.get("actions") or []) if isinstance(item, dict)]
        names = [name for name in rows if name]
        if task is not None:
            from backends.policy import write_block

            names = [name for name in names if not write_block(ns, name, task)]
        found[ns] = names[:_MAX_DISCOVER_ACTIONS]
    return {"namespaces": list(found), "actions": found, "error": "；".join(errors)}


def automation_filters(budget: Budget, list_fn=None) -> list[str]:
    """项目内 automation 测试的真前缀清单（20261028-164813：模型编造
    L_NewTest3.SimpleVehicle 当过滤器，收口必败）。花 1 次元数据预算；
    失败/空都返回 []，不阻塞求解——空清单时方案只能省略收口或诚实失败。"""
    if list_fn is None:
        def list_fn(namespace, action, params, task):
            return _monolith_exec(namespace, action, params, task)
    if not budget.spend("meta_calls"):
        return []
    try:
        out = list_fn("editor", "list_automation_tests", {"prefix": "AIAgent."}, {"id": "solve"})
    except Exception:
        return []
    rows = ((out or {}).get("data") or {}).get("tests") or []
    names = [str(item.get("full_path") or "") for item in rows if isinstance(item, dict)]
    return [name for name in names if name][:20]


def retrieve_paradigm(query: str, retrieve_fn=None) -> str:
    """同模块范式 1 条，只取标题级一句话。失败就空，不阻塞求解。"""
    try:
        fn = retrieve_fn
        if fn is None:
            from core.rag_core import retrieve as fn
        rows = fn(query, top_k=1)
    except Exception:
        return ""
    for row in rows or []:
        if isinstance(row, str):
            return row.strip()[:60]
        meta = row.get("metadata") or {}
        title = str(meta.get("display_name") or meta.get("class_name") or "").strip()
        if title:
            return title
        text = str(row.get("content") or "").strip()
        if text:
            return text[:60]
    return ""


# ---------------------------------------------------------------- 方案（模型）

def _text_of(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, tuple) and raw:
        # core.llm_call.complete 返回 (response, record)；取第一件才是响应本体。
        raw = raw[0]
        if raw is None:
            return ""
    try:
        return raw.choices[0].message.content or ""
    except Exception:
        return str(raw or "")


# 方案层解析失败的错误分类。symptom 用这几个短串直接进失败模式影子，
# 人审时按类看，不用去翻整段 plan_error。
_PLAN_PROBLEMS = {
    "truncated": ("solver-plan-truncated", "方案 JSON 被截断"),
    "not_json": ("solver-plan-not-json", "方案不是 JSON"),
    "json_invalid": ("solver-plan-json-invalid", "方案 JSON 解析失败"),
    "no_steps": ("solver-plan-no-steps", "方案没有 steps"),
    "call_failed": ("solver-plan-call-failed", "模型调用失败"),
}
# 首轮 + 一次结构化重问（铁律 4 的「最多重试一次」在方案层的落法）。
_MAX_PLAN_ATTEMPTS = 2

# 重问时追加的两段话：一段治「不是 JSON」，一段治「被截断」。
# 截断重问必须要求更短，否则同一段超长 JSON 再问一次还是撞上限，白烧预算。
_PLAN_RETRY_NOT_JSON = (
    "上一轮的回答不能被解析成方案 JSON。这一次只输出一个 JSON 对象，"
    "不要解释、不要 markdown 代码块、不要任何前后缀文字。严格按这个骨架（键名一字不差，"
    "可选键直接省略）："
    '{"steps":[{"tool":"namespace.action","args":{}},{"patch":{"path":"Public/X.h","old":"原文","new":"新文"}},'
    '{"automation":"收口过滤器"}]}。'
    "括号与引号必须成对，不许注释、不许尾逗号、不许用省略号或「其余同上」占位。"
)
_PLAN_RETRY_TRUNCATED = (
    "上一轮的输出在 JSON 中途就被截断了（撞了输出长度上限）。这次只输出精简 JSON："
    "步骤压到 3 条以内，args 只留必需键，content/patch 只留能编译的最短形式。"
    "仍然只输出一个 JSON 对象，不要解释、不要前后缀文字，括号与引号必须成对。"
)


def _finish_reason_of(raw) -> str:
    """响应里的 finish_reason。假件/裸串没有就返回空串。"""
    if isinstance(raw, tuple) and raw:
        record = raw[1]
        if isinstance(record, dict) and str(record.get("finish_reason") or "").strip():
            return str(record["finish_reason"]).strip().lower()
        raw = raw[0]
    try:
        return str(getattr(raw.choices[0], "finish_reason", "") or "").strip().lower()
    except Exception:
        return ""


def _scan_json_body(text: str) -> tuple[str, str]:
    """从正文里抠出第一个括号配平的 JSON 对象。返回 (正文片段, 问题)。

    问题为空串表示抠到了完整对象；"not_json" 表示找不到 `{`；
    "truncated" 表示扫到正文末尾还没闭合——这就是「JSON 中途截断」的判据，
    与「压根不是 JSON」必须区分开，否则重问会按格式问题去修长度问题。
    引号内的括号不计数（content 里常有 `{}` 的 C++ 代码）。
    """
    body = str(text or "")
    start = body.find("{")
    if start < 0:
        return "", "not_json"
    curly = square = 0
    in_string = escape = False
    for index in range(start, len(body)):
        ch = body[index]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            curly += 1
        elif ch == "}":
            curly -= 1
            if curly <= 0 and square <= 0:
                return body[start:index + 1], ""
        elif ch == "[":
            square += 1
        elif ch == "]":
            square -= 1
    return body[start:], "truncated"


def _parse_plan_body(raw) -> tuple[list | None, str, str]:
    """一次响应 → (raw_steps, problem, detail)。problem 为空串即成功。

    截断判据两条，命中任一就算截断（不伪装成「不是 JSON」、也不无限重问）：
    1. finish_reason == "length"：输出撞了长度上限，即便括号侥幸配平，方案也缺尾巴；
    2. 正文里的 JSON 括号/引号配不平：输出在对象中途结束。
    """
    finish = _finish_reason_of(raw)
    if finish == "length":
        return None, "truncated", "finish_reason=length：输出撞了输出长度上限"
    body, problem = _scan_json_body(_text_of(raw))
    if problem == "not_json":
        return None, "not_json", "正文里找不到 JSON 对象"
    if problem == "truncated":
        return None, "truncated", "JSON 在对象中途结束：括号或引号不配对"
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return None, "json_invalid", exc.msg
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        return None, "no_steps", "JSON 里没有 steps 数组"
    return steps, "", ""


def _plan_error_text(problem: str, detail: str, retried: bool = False) -> str:
    """问题分类 → 可读 plan_error。重问过就必须在正文里说出来，便于人审归因。"""
    head = _PLAN_PROBLEMS.get(problem, ("solver-plan-unknown", "方案解析失败"))[1]
    text = f"{head}：{detail}" if detail else head
    if retried:
        text += "（已结构化重问 1 次，仍未取到合法方案 JSON）"
    return text


def _plan_messages(payload: dict, previous: str, problem: str) -> list[dict]:
    """首轮消息；重问轮在原始消息后追加「上一轮原文 + 更严格的格式要求」。"""
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    if not problem:
        return messages
    hint = _PLAN_RETRY_TRUNCATED if problem == "truncated" else _PLAN_RETRY_NOT_JSON
    # 上一轮原文只带前 2000 字：截断轮本来就超长，整段回灌只会再撞一次上限。
    messages.append({"role": "assistant", "content": (previous or "")[:2000] or "（上一轮没有正文）"})
    messages.append({"role": "user", "content": hint})
    return messages


def _record_plan_failure(problem: str, plan_error: str, task_id: str) -> None:
    """解析彻底失败 → 失败模式库影子一条（successes=0，promote 由人审走）。

    只写影子：这条路径拿不到「修好过」的证据，不能进 fix。
    写库失败一律吞掉——记不了样本不能让求解路径变成崩溃。
    """
    symptom = _PLAN_PROBLEMS.get(problem, ("solver-plan-unknown", ""))[0]
    try:
        from agent.failure_memory import add_shadow

        add_shadow({
            "id": symptom,
            "symptom": symptom,
            "symptom_detail": "",
            "note": f"求解方案层解析失败（{problem}）：{str(plan_error or '')[:160]}",
            "fix_tools": [],
            "source_runs": [str(task_id)] if str(task_id or "").strip() else [],
        })
    except Exception:
        pass


def plan_steps(text: str, inv: dict, paradigm: str, complete, budget: Budget,
               task_id: str = "") -> tuple[list[dict], str, str]:
    """方案：模型调用产出步骤规格。返回 (steps, automation_filter, error)。

    重问策略（铁律 4 在方案层的落法）：
    - 首轮解析失败（非 JSON / JSON 解析失败 / 没有 steps / 截断）→ 用更严格的追问消息
      再问一次，明确「只输出 JSON、不要解释」并给字段骨架；
    - 只重问一次，不循环；每次尝试都先过 budget.spend("model_calls")，烧尽就诚实失败；
    - 两次都失败 → 可读 plan_error，并写一条失败模式影子（source_runs 带 task_id）。
    """
    if not callable(complete):
        return [], "", "没有可用的模型回调"
    payload = {
        "原句": str(text or "")[:500],
        "盘点": inv.get("actions") or {},
        "源码定位": (inv.get("code_hits") or [])[:8],
        "范式": paradigm,
        "automation_tests": inv.get("automation_tests") or [],
    }
    # apply_spec 内联整份 C++，4 文件 × 12000 字按纯 ASCII 算可能超过缺省输出
    # 上限；推理型模型（UE_LLM_MODEL=deepseek-flash）还会先烧 reasoning_content
    # （20261028 实测：8192 全被思考吃光，content 为空、finish=length）。
    # 显式给 32768，防 JSON 截断或空正文。 TypeError 兜底兼容注入的假件。
    _PLAN_MAX_TOKENS = 32768

    def _call_once(msgs):
        try:
            return complete(msgs, caller="solve", max_tokens=_PLAN_MAX_TOKENS)
        except TypeError:
            return complete(msgs)

    problem = ""
    detail = ""
    previous = ""
    attempt = 0
    retried = False
    seen_truncated = False
    msgs = _plan_messages(payload, "", "")
    while attempt < _MAX_PLAN_ATTEMPTS:
        # 每次尝试都先记一笔模型预算：重问不许绕过闸门，烧尽就诚实失败。
        if not budget.spend("model_calls"):
            if not problem:
                return [], "", "模型预算烧尽，方案没产出"
            # 预算把重问挡在发不出去的门口：如实说「没发出」，不许谎报重问过。
            plan_error = (f"模型预算烧尽，方案重问没发出"
                          f"（上一轮：{_plan_error_text(problem, detail)}）")
            _record_plan_failure(problem, plan_error, task_id)
            return [], "", plan_error
        try:
            raw = _call_once(msgs)
        except Exception as exc:
            problem, detail = "call_failed", f"{type(exc).__name__}: {exc}"
        else:
            previous = _text_of(raw)
            raw_steps, problem, detail = _parse_plan_body(raw)
            if not problem:
                steps, automation, error = _validate_steps(raw_steps, inv)
                if error:
                    return [], "", error
                return steps, automation, ""
            seen_truncated = seen_truncated or problem == "truncated"
        attempt += 1
        if attempt >= _MAX_PLAN_ATTEMPTS:
            break
        retried = True
        msgs = _plan_messages(payload, previous, problem)
    plan_error = _plan_error_text(problem, detail, retried)
    # 首轮被截断、重问却换成别的毛病：把截断这件事留在正文里，人审才不会漏。
    if seen_truncated and problem != "truncated":
        plan_error += "（本轮之前的尝试曾因输出过长被截断）"
    _record_plan_failure(problem, plan_error, task_id)
    return [], "", plan_error


def _validate_steps(raw_steps: list, inv: dict) -> tuple[list[dict], str]:
    """逐条验收：白名单 namespace、盘点清单里真实存在的 action、文件路径合法。
    非法步骤整条丢（不换动作顶上），全丢就算方案失败。"""
    from agent.decompose import _PATH_RE, safe_files

    whitelist = set(solver_namespaces())
    known: dict[str, set[str]] = {
        ns: set(rows) for ns, rows in (inv.get("actions") or {}).items()
    }
    steps: list[dict] = []
    automation = ""
    for raw in raw_steps[:_MAX_STEPS]:
        if not isinstance(raw, dict):
            continue
        if raw.get("automation"):
            filt = str(raw["automation"]).strip()
            tests = [str(name) for name in (inv.get("automation_tests") or []) if str(name).strip()]
            if filt and tests and not any(name == filt or name.startswith(filt) for name in tests):
                continue  # 清单里没有的过滤器是编的，整条丢（20261028-164813 教训）
            if filt and not automation:
                automation = filt
            continue
        if isinstance(raw.get("patch"), dict):
            # patch：对已有源文件做一处替换。文件读取与唯一性校验在执行时做
            # （_materialize_patch），这里只验收形状与路径。
            item = raw["patch"]
            path = str(item.get("path") or "").replace("\\", "/").lstrip("/")
            old = str(item.get("old") or "")
            new = str(item.get("new") or "")
            if not _PATH_RE.match(path):
                return [], "", f"patch 路径不收：{path}"
            if not old.strip():
                return [], "", "patch 的 old 是空的"
            if len(old) > 2000 or len(new) > 2000:
                return [], "", "patch 的 old/new 超长（>2000 字）"
            if old == new:
                return [], "", "patch 的 old 与 new 相同"
            steps.append({"patch": {"path": path, "old": old, "new": new}})
            continue
        if isinstance(raw.get("apply_spec"), dict):
            files, error = safe_files((raw["apply_spec"] or {}).get("files"))
            if error:
                return [], "", f"apply_spec 不合法：{error}"
            if files:
                steps.append({"apply_spec": files})
            continue
        tool = str(raw.get("tool") or "").strip()
        ns, _, action = tool.partition(".")
        ns = ns.strip().lower()
        action = action.strip()
        if not ns or not action or ns not in whitelist:
            continue
        if action not in known.get(ns, set()):
            continue  # 盘点清单里没有的原语，不编
        args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
        steps.append({"tool": tool, "namespace": ns, "action": action, "args": args})
    if not steps:
        return [], "", "方案里没有可执行的步骤"
    return steps, automation, ""


# ---------------------------------------------------------------- 执行

def _monolith_exec(namespace: str, action: str, params: dict, task: dict) -> dict:
    from backends.monolith import exec as monolith_exec

    return monolith_exec(namespace, action, params, task=task)


def _materialize_patch(patch: dict) -> tuple[list[dict], str]:
    """patch → 整文件内容（复用 apply_spec 的写+编译管线）。
    文件必须存在、old 必须全文唯一；替换后内容没变也算失败。"""
    from pathlib import Path

    try:
        from config import UE_SOURCE_ROOT

        root = Path(UE_SOURCE_ROOT)
    except Exception as exc:
        return [], f"源码根不可用：{exc}"
    rel = str(patch.get("path") or "").replace("\\", "/").lstrip("/")
    target = root / rel
    if not target.is_file():
        return [], f"文件不存在：{rel}"
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], f"读不了 {rel}：{exc}"
    if len(text) > 200_000:
        return [], f"{rel} 超过 200000 字，不收 patch"
    old = str(patch.get("old") or "")
    new = str(patch.get("new") or "")
    count = text.count(old)
    if count == 0:
        return [], f"old 在 {rel} 里一字不差地找不到（注意空格与分号）"
    if count > 1:
        return [], f"old 在 {rel} 里命中 {count} 次，要求全文唯一（多带些上下文行）"
    content = text.replace(old, new, 1)
    if content == text:
        return [], "替换后内容没变"
    return [{"path": rel, "content": content}], ""


def _step_label(step: dict) -> str:
    """方案步骤的一行摘要，进轨迹：事后查「当时方案是什么」不再靠猜。"""
    if not isinstance(step, dict):
        return "?"
    if step.get("tool"):
        return f"{step['tool']} {json.dumps(step.get('args') or {}, ensure_ascii=False)[:100]}"
    if "patch" in step:
        patch = step.get("patch") or {}
        return (f"patch {patch.get('path')} "
                f"({str(patch.get('old'))[:40]} -> {str(patch.get('new'))[:40]})")
    if "apply_spec" in step:
        spec = step.get("apply_spec")
        files = spec.get("files") if isinstance(spec, dict) else spec
        names = [str(item.get("path") or "") for item in (files or []) if isinstance(item, dict)]
        return "apply_spec " + "、".join(names)
    return "?"


def _is_mutating_step(step: dict) -> bool:
    """这一步会不会改东西。patch / apply_spec 算；编辑器原语按只读前缀反判，
    但基础设施动作（开关卡 / 保存 / 编译 / 观察类，见 _SOLVER_INFRA_ACTIONS）
    与验收动作（run_automation_tests 等）不算改——20261028-191105 实测：
    一扇 load_level 曾被当成「改东西」，把假施工方案放进了收口。"""
    if not isinstance(step, dict):
        return False
    if "patch" in step or "apply_spec" in step:
        return True
    action = str(step.get("action") or "").strip().lower()
    if not action or action in _SOLVER_READ_ACTIONS or action in _SOLVER_INFRA_ACTIONS:
        return False
    from backends.policy import is_read

    return not is_read(action)


def _has_mutating_step(steps: list) -> bool:
    return any(_is_mutating_step(step) for step in (steps or []))


def _monolith_describe(namespace: str, action: str) -> dict:
    from backends.monolith import describe

    return describe(namespace, action)


def _backend_fingerprint() -> str:
    """能力面文件上的 backend 要跟它比。口径统一走 core，别再各写一份 .env 解析。"""
    from core.capability_surface import backend_fingerprint

    return backend_fingerprint()


def load_describe_cache(path=None) -> dict:
    """capability_surface.json 里的 params，按 ns.action 取。

    文件上的 backend 必须和当前 UE_EDITOR_BACKEND 一致；没有 params、
    或指纹对不上，就当没有缓存（调用方再真 describe）。
    """
    from pathlib import Path

    if path is None:
        # 位置与后端指纹都在 core（20261029 边界整改）：agent 不许 import pipelines。
        try:
            from core.capability_surface import surface_path

            path = surface_path()
        except Exception:
            from config import DATA_DIR

            path = DATA_DIR / "capability_surface.json"
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if str(data.get("backend") or "").strip().lower() != _backend_fingerprint():
        return {}
    found: dict = {}
    namespaces = data.get("namespaces") if isinstance(data.get("namespaces"), dict) else {}
    for ns, rows in namespaces.items():
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            params = row.get("params")
            if name and isinstance(params, dict) and params:
                found[f"{ns}.{name}"] = params
    return found


def describe_actions(steps: list[dict], budget: Budget, describe_fn=None, cache=None) -> dict:
    """对方案里用到的编辑器原语逐个取参数形状（≤4 个）。返回 {"ns.action": params}。

    盘点只给 action 名不给参数名，参数形状全靠模型猜——20261028-190109 实测：
    load_level 给了 level，monolith 要 path，一次失败加一次无谓重试。执行前
    describe 一轮，形状对不上还有机会修。缓存命中不占预算；未命中花元数据
    预算，不占执行用的编辑器次数。失败 / 超预算就不给那个键，不阻塞。
    cache 显式传入（含空 dict）时不读盘。
    """
    describe_fn = describe_fn or _monolith_describe
    if cache is None:
        cache = load_describe_cache()
    out: dict = {}
    seen: set[str] = set()
    for step in steps or []:
        if not isinstance(step, dict) or "tool" not in step:
            continue
        key = str(step.get("tool") or "")
        if not key or key in seen or len(out) >= 4:
            continue
        seen.add(key)
        cached = cache.get(key) if isinstance(cache, dict) else None
        if isinstance(cached, dict) and cached:
            out[key] = cached
            continue
        if not budget.spend("meta_calls"):
            break
        result = describe_fn(str(step.get("namespace") or ""), str(step.get("action") or ""))
        if result.get("ok"):
            out[key] = result.get("params") if isinstance(result.get("params"), dict) else {}
    return out


def _filter_in_list(filt: str, tests: list[str]) -> bool:
    """过滤器必须是真清单里的完整名或前缀。编造的整条不算。"""
    text = str(filt or "").strip()
    if not text or text in {"无", "none", "null"}:
        return False
    names = [str(name).strip() for name in tests if str(name).strip()]
    return any(name == text or name.startswith(text) for name in names)


def _pick_criteria(query: str, tests: list[str], steps: list[dict], complete, budget: Budget) -> str:
    """方案省略了 automation 时，收口前再花至多 1 次模型调用补选。

    输出只能是真清单里的过滤器或「无」。对不上清单就丢掉，调用方维持 no_criteria。
    清单是空的就不叫模型、不花预算。
    """
    names = [str(name).strip() for name in (tests or []) if str(name).strip()]
    if not names or not callable(complete):
        return ""
    if not budget.spend("model_calls"):
        return ""
    payload = {
        "原句": str(query or "")[:400],
        "automation_tests": names[:20],
        "步骤": [_step_label(step) for step in (steps or [])][:8],
    }
    messages = [
        {"role": "system", "content": (
            "你只输出 JSON。从 automation_tests 里补选收口过滤器："
            "只能是清单中的完整名或它的前缀。挑不出就输出 {\"filter\":\"无\"}。"
            "不许编造清单里没有的名字。"
            '形状：{"filter":"..."}。')},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        raw = _text_of(complete(messages, caller="solve-criteria", max_tokens=256))
    except TypeError:
        try:
            raw = _text_of(complete(messages))
        except Exception:
            return ""
    except Exception:
        return ""
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return ""
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    filt = str(data.get("filter") or "").strip()
    if not _filter_in_list(filt, names):
        return ""
    return filt


def _calibrate_args(steps: list[dict], schemas: dict, query: str, complete, budget: Budget,
                    inv: dict | None = None, validate_fn=None) -> tuple[list[dict], str]:
    """拿 describe 的参数形状对方案的 args 做对账：extra 键 / 缺 required 键都算形状
    对不上。对不上才花一次模型调用重出 steps（同 plan 形状，重新过 _validate_steps）；
    全对就原样返回，不花模型预算。修不出（模型失败 / 仍不合法）就返回原方案 + 错误说明，
    让执行层按原样失败——诚实路径不因为校准失败而变宽。"""
    validate = validate_fn or _validate_steps
    problems: list[dict] = []
    for step in steps or []:
        key = str(step.get("tool") or "")
        schema = schemas.get(key)
        if not isinstance(schema, dict) or not schema:
            continue
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        extra = [str(k) for k in args if k not in schema]
        missing = []
        for pname, meta in schema.items():
            required = not (isinstance(meta, dict) and meta.get("required") is False)
            if required and pname not in args:
                missing.append(str(pname))
        if extra or missing:
            problems.append({"tool": key, "args": args, "schema": schema,
                             "unexpected": extra, "missing": missing})
    if not problems:
        return steps, ""
    if not callable(complete):
        return steps, "形状对不上但没有可用的模型回调"
    if not budget.spend("model_calls"):
        return steps, "形状对不上但模型预算烧尽"
    payload = {
        "原句": str(query or "")[:400],
        "有问题的步骤": problems,
    }
    messages = [
        {"role": "system", "content": (
            "你只输出 JSON。按 schema 修正这些步骤的 args："
            "unexpected 的键删掉，missing 的键补上（值从原句或常识取，取不到就给合理的占位）。"
            "steps 形状与输入一致，不许增删步骤、不许改 action 名。"
            '形状：{"steps":[...]}。')},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        raw = _text_of(complete(messages, caller="solve-calibrate", max_tokens=4096))
    except TypeError:
        try:
            raw = _text_of(complete(messages))
        except Exception as exc:
            return steps, f"校准调用失败：{type(exc).__name__}"
    except Exception as exc:
        return steps, f"校准调用失败：{type(exc).__name__}"
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return steps, "校准输出不是 JSON"
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return steps, "校准 JSON 解析失败"
    raw_steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(raw_steps, list):
        return steps, "校准输出没有 steps"
    fixed, automation, error = validate(raw_steps, inv or {})
    if error or not fixed:
        return steps, f"校准后的方案不合法：{error or '空'}"
    return fixed, ""


def execute(steps: list[dict], task: dict, budget: Budget, exec_fn=None, build=None,
            ensure_editor_fn=None) -> tuple[list[dict], list[dict], str, dict]:
    """逐原语执行。遇错读原文最多重试一次（铁律 4），再失败就停。
    返回 (exec_results, spec_files, error, build_info)。

    20261028 载具实测的执行序修正：apply_spec 不再攒到最后统一编译——
    写码步骤在这里就地「写 → 编译 → 编辑器回连」，后面的放 Actor 步骤
    才能用上新编译的类；否则要么放的时候类不存在，要么编译时编辑器断连。
    build 不传（离线假件场景）时保持旧的延迟语义。

    20261028-183641 的两个修正：
    - patch 步骤就地物化成整文件内容，走与 apply_spec 相同的「写→编译→回连」管线；
    - 每个编辑器步骤执行前先过 backends.policy.write_block：被写闸拒绝的调用
      是确定性失败，不进重试、不烧预算，原文直接交回（run_python 没放行时
      一次都不少烧，但也不许多烧）。"""
    exec_fn = exec_fn or _monolith_exec
    results: list[dict] = []
    spec_files: list[dict] = []
    build_info: dict = {}
    deferred_specs: list[dict] = []
    for step in steps:
        if "patch" in step:
            files, patch_error = _materialize_patch(step["patch"])
            if patch_error:
                results.append({"tool": "patch", "ok": False, "error": patch_error[:200]})
                return results, spec_files, f"patch 落地失败：{patch_error}", build_info
            step = {"apply_spec": files}
        if "apply_spec" in step:
            if build is None:
                deferred_specs.extend(step["apply_spec"])
                continue
            spec = _apply_spec_now(step["apply_spec"], task, budget, build, ensure_editor_fn)
            build_info = spec
            if not spec.get("ok"):
                results.append({"tool": "apply_spec+build", "ok": False,
                                "error": str(spec.get("error") or "")[:200]})
                return results, spec_files, f"编译失败：{str(spec.get('error') or '')[:200]}", build_info
            results.append({"tool": "apply_spec+build", "ok": True,
                            "error": "", "build_rounds": spec.get("build_rounds") or 0})
            spec_files.extend(step["apply_spec"])
            continue
        ns, action, args = step["namespace"], step["action"], step["args"]
        from backends.policy import write_block

        block = write_block(ns, action, task)
        if block:
            results.append({"tool": f"{ns}.{action}", "ok": False, "error": f"写闸拒绝：{block}"})
            return results, spec_files, f"{ns}.{action} 被写闸拒绝：{block}（任务旗标或 UE_ALLOW_* 没放行）", build_info
        if not budget.spend("editor_calls"):
            return results, spec_files, "编辑器预算烧尽，执行提前结束", build_info
        out = exec_fn(ns, action, dict(args), task)
        ok = bool(out.get("ok"))
        results.append({"tool": f"{ns}.{action}", "ok": ok,
                        "error": "" if ok else str(out.get("error") or "")[:200]})
        if not ok and budget.spend("editor_calls"):
            # 一次重试：同样的调用，不换动作、不改参数形状。
            out = exec_fn(ns, action, dict(args), task)
            ok = bool(out.get("ok"))
            results[-1] = {"tool": f"{ns}.{action}", "ok": ok,
                           "error": "" if ok else str(out.get("error") or "")[:200],
                           "retried": True}
        if not ok:
            return results, spec_files, f"{ns}.{action} 失败：{results[-1]['error']}", build_info
    return results, spec_files + deferred_specs, "", build_info


def _apply_spec_now(files: list[dict], task: dict, budget: Budget, build, ensure_editor_fn) -> dict:
    """就地 apply_spec：写 → 预算内编译 → 编辑器回连。失败带原文，不重试写。"""
    from agent.build_error import apply_spec_loop

    build_budget = Budget({"build_rounds": budget.limits.get("build_rounds", 0)})

    def bounded_build(_files=None):
        if not build_budget.spend("build_rounds"):
            return {"ok": False, "kind": "cpp_build", "summary": "编译预算烧尽",
                    "errors": [{"file": "", "line": 0, "message": "编译预算烧尽"}]}
        return build()

    spec = apply_spec_loop(files, wid=str(task.get("id") or "solve"), build=bounded_build, fix=None)
    # 编译裁判会先关编辑器（DLL 占用）。成败都要回连：成了，后面的放 Actor 步骤
    # 要用新类；败了，本扇结束，但下一扇/下一个 run 的编辑器窗口不该跟着陪葬
    # （20261028-163746 实测：build 失败后编辑器没人拉，下一轮全撞 10061）。
    if ensure_editor_fn is not None:
        editor = ensure_editor_fn()
        if editor.get("ok"):
            spec["editor"] = str(editor.get("note") or "")[:80]
        elif spec.get("ok"):
            spec["ok"] = False
            spec["error"] = f"编译后编辑器没回连：{str(editor.get('note') or '')[:160]}"
            spec["editor"] = str(editor.get("note") or "")[:80]
    return spec


# ---------------------------------------------------------------- 轨迹落盘

def _distill_dir():
    from pathlib import Path

    override = os.environ.get("UE_DISTILL_OUT", "").strip()
    if override:
        return Path(override)
    from config import DATA_DIR

    return DATA_DIR / "distill"


def _write_trajectory(record: dict) -> str:
    try:
        from agent.distill import redact

        path = _distill_dir() / "solver_trajectories.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = dict(record)
        row["ts"] = datetime.now().isoformat(timespec="seconds")
        clean = json.loads(redact(json.dumps(row, ensure_ascii=False)))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(clean, ensure_ascii=False) + "\n")
        return str(path)
    except Exception:
        return ""


# ---------------------------------------------------------------- 主循环

def solve(task: dict, *, complete=None, discover_fn=None, exec_fn=None,
          retrieve_fn=None, build=None, judge_fn=None, budget_limits=None,
          ensure_editor_fn=None, automation_list_fn=None, describe_fn=None) -> dict:
    """受控求解循环。返回 run_task 风格的摘要；失败带结构化缺口，不假成功。

    所有外部依赖（模型、discover、exec、检索、编译、裁判）都可注入，离线检查用假件驱动。
    """
    task = dict(task or {})
    tid = str(task.get("id") or "solve")
    text = str(task.get("query") or task.get("scope_text") or "")
    budget = Budget(budget_limits)
    trace: dict = {
        "task_id": tid,
        "scope_text": str(text)[:300],
        "budget_limits": budget.limits,
    }
    outcome = ""

    # 1. 盘点（只读）+ 源码定位（本地扫描，零编辑器预算、零模型预算）
    inv = inventory(text, budget, discover_fn=discover_fn, task=task)
    inv["code_hits"] = locate_code(text)
    trace["inventory"] = inv
    if inv.get("error") and not inv.get("namespaces"):
        outcome = "inventory_failed"
        return _fail(task, tid, budget, trace, outcome,
                     f"求解盘点失败：{inv['error']}（能力缺口：编辑器盘点不出来，不编方案）")
    paradigm = retrieve_paradigm(text, retrieve_fn)
    trace["paradigm"] = paradigm
    # 真测试前缀清单：收口过滤器只许从这里挑（20261028-164813 编造测试名教训）。
    # 离线注入 exec_fn 而没注 automation_list_fn 时跳过（不碰网络、不动预算）。
    if automation_list_fn is not None or exec_fn is None:
        tests = automation_filters(budget, list_fn=automation_list_fn)
    else:
        tests = []
    inv["automation_tests"] = tests
    trace["automation_tests"] = tests

    # 2. 方案
    if complete is None:
        from core.llm_call import complete as live_complete

        complete = live_complete
    steps, automation, plan_error = plan_steps(text, inv, paradigm, complete, budget, task_id=tid)
    trace["plan_error"] = plan_error
    if plan_error or not steps:
        outcome = "plan_failed"
        return _fail(task, tid, budget, trace, outcome,
                     f"求解方案没产出：{plan_error or '空方案'}（能力缺口：方案层接不住）")

    # 2.5 参数形状校准：盘点只有 action 名，参数名靠猜必翻车（20261028-190109：
    # load_level 给了 level，monolith 要 path）。对方案用到的原语 describe 一轮，
    # 形状对不上就花一次模型调用修 args；describe 不可用（离线假件）就跳过。
    if describe_fn is None and discover_fn is None:
        describe_fn = _monolith_describe  # 真机路径：默认用真 describe
    if describe_fn is not None:
        schemas = describe_actions(steps, budget, describe_fn=describe_fn)
        if schemas:
            steps, calib_error = _calibrate_args(
                steps, schemas, text, complete, budget, inv=inv)
            trace["calibrate_error"] = calib_error
            trace["schemas"] = schemas

    # 3. 执行（apply_spec/patch 就地写+编译+编辑器回连；build 未注入且无写码步骤时退回延迟语义）
    # 20261028-190722 教训：inline_build 只认 apply_spec，patch 物化出的文件走进
    # deferred 分支被静默丢弃——代码没写、没编译，automation 收口照绿，假成功。
    inline_build = any("apply_spec" in step or "patch" in step for step in steps)
    if build is not None:
        build_fn = build
    elif inline_build:
        def build_fn(_files=None):
            from agent.build_error import build_and_record

            return build_and_record()
    else:
        build_fn = None
    if ensure_editor_fn is not None:
        ensure_fn = ensure_editor_fn
    elif build_fn is not None and inline_build:
        def ensure_fn():
            from core.editor_launch import ensure_editor

            return ensure_editor()
    else:
        ensure_fn = None
    results, spec_files, exec_error, build_info = execute(
        steps, task, budget, exec_fn=exec_fn, build=build_fn, ensure_editor_fn=ensure_fn,
    )
    trace["plan_steps"] = [_step_label(step) for step in steps]
    trace["spec_files"] = spec_files
    trace["executions"] = results
    trace["build"] = {"rounds": int(build_info.get("build_rounds") or 0),
                      "ok": build_info.get("ok"), "fixes": build_info.get("fixes")}
    build_rounds = int(build_info.get("build_rounds") or 0)
    if exec_error and ("编译失败" in exec_error or "编译预算烧尽" in exec_error):
        outcome = "build_failed"
        return _fail(task, tid, budget, trace, outcome,
                     f"求解编译失败：{str(build_info.get('error') or exec_error)[:200]}"
                     "（轨迹已落盘，供蒸馏收失败模式）")
    if exec_error and not spec_files:
        outcome = "exec_failed"
        return _fail(task, tid, budget, trace, outcome,
                     f"求解执行失败：{exec_error}（能力缺口：缺这个原语或参数形状对不上）")
    if exec_error:
        trace["exec_error"] = exec_error

    # 4.5 假成功守卫：原句要求改变，方案却全是只读步骤——automation 收口再绿
    # 也只是改动前的旧状态，不算完成，不编证据（20261028-185716 实测）。
    if _CHANGE_IMPLYING_RE.search(text) and not _has_mutating_step(steps):
        outcome = "read_only_plan"
        return _fail(task, tid, budget, trace, outcome,
                     "方案全是只读步骤，什么都没改；不能拿改动前就通过的测试当完成"
                     "（能力缺口：方案层没给改动步骤）")
    # 4.6 写码被跳过的守卫：方案里有 patch / apply_spec，但一次编译都没跑——
    # 写码步骤被 defer 或跳过时（20261028-190722 实测：代码没落盘，收口照绿），
    # 源码与「已改」的结论必然对不上，诚实失败。
    if any("patch" in step or "apply_spec" in step for step in steps) and build_rounds == 0:
        outcome = "write_skipped"
        return _fail(task, tid, budget, trace, outcome,
                     "方案里有写码步骤但一次编译都没跑（写码被跳过）；源码没变，不算完成")

    # 5. 收口：有判据（automation 过滤器）才 verify。方案省略时再补选一次，
    # 补选仍必须命中真清单；编造的丢掉，挑不出就维持 no_criteria，不放宽。
    filt = str(task.get("automation_filter") or "").strip() or automation
    if not filt:
        filt = _pick_criteria(text, tests, steps, complete, budget)
        if filt:
            trace["criteria_backfill"] = filt
    if not filt:
        outcome = "no_criteria"
        return _fail(task, tid, budget, trace, outcome,
                     "求解执行完了但没有收口判据（没有 automation 过滤器），不算成功，不编证据")
    if judge_fn is None:
        from core.judge import judge as judge_fn
    verdict = judge_fn({"kind": "automation", "filter": filt}, task=task)
    trace["verdict"] = {"ok": verdict.get("ok"), "kind": verdict.get("kind"),
                        "summary": str(verdict.get("summary") or "")[:160]}
    ok = bool(verdict.get("ok"))
    outcome = "success" if ok else "criteria_failed"
    summary_text = (
        f"求解收口：{filt} 失败数 0（编辑器 {budget.used['editor_calls']} 次、编译 {build_rounds} 轮）"
        if ok else f"求解收口失败：{str(verdict.get('summary') or '')[:160]}"
    )
    path = _write_trajectory({**trace, "outcome": outcome})
    trace["trajectory_path"] = path
    trace["outcome"] = outcome
    distilled = distill_trajectory(trace) if _distill_enabled() else {"bucket": "skipped", "id": ""}
    out = {
        "id": tid,
        "ok": ok,
        "summary": summary_text,
        "index_note": summary_text,
        "outcome": outcome,
        "budget": budget.snapshot(),
        "trajectory": path,
        "distill": distilled,
        "test": {
            "ok": ok,
            "kind": "automation",
            "filter": filt,
            "summary": summary_text,
            "errors": [] if ok else [item for item in (verdict.get("errors") or [])][:4],
        },
        "judge_kind": "automation",
        "pie": False,
    }
    if spec_files and build_info.get("ok") is True:
        out["spec_files"] = list(spec_files)
        out["build"] = {
            "ok": True,
            "kind": "cpp_build",
            "summary": "编译通过",
            "build_rounds": build_rounds,
        }
    if not ok:
        out["error"] = summary_text
    return out


def _fail(task: dict, tid: str, budget: Budget, trace: dict, outcome: str, message: str) -> dict:
    """预算烧尽 / 闸门拒绝 / 判据缺失：降级回 capability_gap 的诚实口径，轨迹照落。"""
    path = _write_trajectory({**trace, "outcome": outcome})
    trace["trajectory_path"] = path
    trace["outcome"] = outcome
    distilled = distill_trajectory(trace) if _distill_enabled() else {"bucket": "skipped", "id": ""}
    return {
        "id": tid,
        "ok": False,
        "summary": message,
        "error": message,
        "index_note": message,
        "outcome": outcome,
        "budget": budget.snapshot(),
        "trajectory": path,
        "distill": distilled,
        "capability_gap": {
            "reason": message,
            "missing_actions": [item.get("tool") for item in (trace.get("executions") or [])
                                if isinstance(item, dict) and not item.get("ok")],
        },
    }


# ---------------------------------------------------------------- P2-7 蒸馏闭环

def _distill_enabled() -> bool:
    return _env("UE_SOLVER_DISTILL", "1").lower() not in ("0", "false", "no", "off")


def lexicon_candidates_path():
    """lexicon 提名行（人审队列）。lexicon.json 本身是人写的，代码只提名不落表。"""
    from pathlib import Path

    override = os.environ.get("UE_LEXICON_CANDIDATES", "").strip()
    if override:
        return Path(override)
    from config import DATA_DIR

    return DATA_DIR / "skills" / "lexicon_candidates.json"


def nominate_lexicon_row(row: dict) -> dict | None:
    """同族动词的 lexicon 提名：同一轨迹 sha 只一行，重复命中累计 hits。人审通过后由人
    把行抄进 lexicon.json（动作表与术语表的写入权都在人，红线 5）。"""
    import hashlib
    import json

    from pathlib import Path

    ident = str(row.get("id") or "").strip()
    if not ident:
        return None
    path: Path = lexicon_candidates_path()
    rows: list[dict] = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            rows = [item for item in (data.get("rows") if isinstance(data, dict) else data) or []
                    if isinstance(item, dict)]
        except Exception:
            rows = []
    found = next((item for item in rows if str(item.get("id") or "") == ident), None)
    today = datetime.now().date().isoformat()
    if found is None:
        row = dict(row)
        row.setdefault("status", "review")
        row["first_seen"] = today
        row["last_seen"] = today
        row["hits"] = 1
        rows.append(row)
    else:
        found["last_seen"] = today
        found["hits"] = int(found.get("hits") or 0) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return found if found is not None else row


def distill_trajectory(trace: dict) -> dict:
    """P2-7：求解轨迹 → 蒸馏闭环。成功 → learned_skills 影子条目 + lexicon 提名行；
    失败 → 失败模式库影子条目。人审之前都不进执行链。返回 {bucket, id}。"""
    import hashlib

    trace = dict(trace or {})
    outcome = str(trace.get("outcome") or "")
    sequence = [
        str(item.get("tool") or "")
        for item in (trace.get("executions") or [])
        if isinstance(item, dict) and str(item.get("tool") or "").strip()
    ]
    if trace.get("build"):
        sequence.append("apply_spec+build")
    digest = hashlib.sha1(json.dumps(sequence, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:8]
    if outcome == "success":
        filt = str((trace.get("verdict") or {}).get("summary") or "")
        skill = {
            "id": f"solver-{digest}",
            "when": str(trace.get("scope_text") or "")[:80],
            "roles": [],
            "goals": sequence or ["solve"],
            "preconditions": ["动作原语经 monolith 盘点确认存在"],
            "action_sequence": sequence,
            "verification": [f"automation 收口：{filt[:80]}"] if filt else [],
            "failure_recovery": ["预算烧尽降级为能力缺口", "编译错误原文进下一轮，最多 3 轮"],
            "confidence": 0.5,
            "source_runs": [str(trace.get("task_id") or "")],
            "conflicts": [],
        }
        from agent.skill_memory import remember_solver_skill

        skill_id = remember_solver_skill(skill)
        row = {
            "id": f"lexicon-{digest}",
            "verbs": [],
            "scope_text": str(trace.get("scope_text") or "")[:200],
            "action_sequence": sequence,
            "automation_filter": str((trace.get("verdict") or {}).get("filter") or ""),
            "trajectory": str(trace.get("trajectory_path") or ""),
            "note": "求解成功的同族动词提名：人审时补 verbs 并绑上（新建或已有）动作",
        }
        nominate_lexicon_row(row)
        return {"bucket": "success", "id": skill_id or f"solver-{digest}"}
    if outcome == "read_only_plan":
        # 词表没拦住的「像是要改、方案却没改」单独留一条影子，方便人审时补动词。
        pattern = {
            "id": f"solver-fail-{digest}",
            "symptom": "solver-read_only_plan",
            "symptom_detail": str(trace.get("scope_text") or "")[:120],
            "note": (
                "求解 read_only_plan：原句像是要改东西，方案没有改动步骤。"
                "人审时若是词表漏了状态动词，补进 _CHANGE_IMPLYING_RE，"
                "并在 check_solver_patch 加用例。"
            ),
            "fix_tools": [],
            "source_runs": [str(trace.get("task_id") or "")],
        }
        from agent.failure_memory import add_shadow

        pattern_id = add_shadow(pattern)
        return {"bucket": "failure", "id": pattern_id or f"solver-fail-{digest}"}
    # 失败轨迹 → 失败模式库影子
    message = ""
    for item in (trace.get("executions") or []):
        if isinstance(item, dict) and not item.get("ok") and item.get("error"):
            message = str(item["error"])
            break
    if not message:
        message = str(trace.get("exec_error") or trace.get("plan_error") or "")
    if not message and isinstance(trace.get("build"), dict):
        errors = trace["build"].get("fixes") or []
        if errors:
            message = str(errors[-1].get("error") or "")
    import re as _re

    coded = _re.search(r"error C\d+", message)
    symptom = coded.group(0) if coded else (message.strip()[:40] or f"solver-{outcome}")
    pattern = {
        "id": f"solver-fail-{digest}",
        "symptom": symptom,
        "symptom_detail": "",
        "note": f"求解 {outcome}：{str(trace.get('scope_text') or '')[:120]}",
        "fix_tools": [],
        "source_runs": [str(trace.get("task_id") or "")],
    }
    from agent.failure_memory import add_shadow

    pattern_id = add_shadow(pattern)
    return {"bucket": "failure", "id": pattern_id or f"solver-fail-{digest}"}


def solve_window(scope: dict, query: str = "") -> list[dict]:
    """零窗口范围的 solve 扇（materialize_scopes 的补窗模式）。开关关着返回 []，
    行为与今天完全一致（check_solve_gap_fallback 钉这条）。"""
    if not solver_enabled():
        return []
    scope = scope if isinstance(scope, dict) else {}
    sid = str(scope.get("id") or "S").strip() or "S"
    title = str(scope.get("title") or "").strip() or f"范围 {sid}"
    from agent.plan_windows import _scope_text

    text = _scope_text(scope) or str(query or "")
    if not str(text).strip():
        return []
    task = {
        "id": f"SBW-{sid}-SOLVE",
        "action": "solve",
        "title": f"受控求解：{title}",
        "query": str(text)[:600],
        "slots": {},
        "allow_writes": [],
        "max_retries": 0,
    }
    window = {
        "id": f"SBW-{sid}-SOLVE",
        "title": f"受控求解：{title}",
        "action": "solve",
        "slots": {},
        "depends_on": [],
        "max_retries": 0,
        "realizable": True,
        "ok": True,
        "blocked": False,
        "goal": True,
        "scope_text": str(text)[:600],
        "task": task,
    }
    return [window]