# agent/diagnose.py
"""只读诊断句归地。不叫模型，不写盘，不连编辑器。

动作表不新增行。原句或台账里的 ASCII token 只能唯一命中一条已声明过滤器时，
才收成现成的 run_automation_test。零条或多条都不挑。
"""
from pathlib import Path

_READ = ("看看", "检查", "核对", "是否有问题", "有没有问题")
_EXCLUDE = ("实现", "新建", "创建", "修改", "摆", "放置")


def is_diagnostic(text: str) -> bool:
    """只读诊断句。含实现、新建、创建、修改、摆、放置的不算。"""
    raw = str(text or "")
    if any(word in raw for word in _EXCLUDE):
        return False
    return any(word in raw for word in _READ)


def _ascii_needles(text: str) -> list[str]:
    """专名 token。太短的碎片和 Game 这类停用词不拿去对过滤器。"""
    from agent.ledger import _ASCII_RE, _ASCII_STOP

    found: list[str] = []
    seen: set[str] = set()
    for word in _ASCII_RE.findall(text or ""):
        key = word.lower()
        if key in _ASCII_STOP or key in seen:
            continue
        seen.add(key)
        found.append(word)
    return found


def _filter_hits(*needles: str) -> list[str]:
    """与 unique_automation_filter 同一条命中规则，但把候选留下。零条或多条都不挑。"""
    from agent.supervise import declared_automation_filters

    usable = [needle for needle in needles if needle]
    if not usable:
        return []
    hits: list[str] = []
    for name in declared_automation_filters():
        low = name.lower()
        if any(needle.lower() in low for needle in usable):
            hits.append(name)
    return hits


def _matching_scopes(text: str, scopes: list) -> list[dict]:
    """标题去掉通用词之后，和诊断句有重合的范围。"""
    from agent.ledger import content_tokens

    query = content_tokens(text)
    if not query:
        return []
    matched = []
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        title = str(scope.get("title") or "")
        if content_tokens(title) & query:
            matched.append(scope)
    return matched


def _shared_title_needles(scopes: list[dict]) -> list[str]:
    """多条范围只保留标题里都有的 ASCII token。只共享「射击」这种汉字不算。"""
    sets = [
        {word.lower() for word in _ascii_needles(str(scope.get("title") or ""))}
        for scope in scopes
    ]
    if not sets:
        return []
    return sorted(set.intersection(*sets))


def _ground_needles(needles: list[str]) -> dict:
    from agent.supervise import unique_automation_filter

    if not needles:
        return {"ok": False, "filters": []}
    chosen = unique_automation_filter(*needles)
    if chosen:
        return {"ok": True, "filter": chosen}
    return {"ok": False, "filters": _filter_hits(*needles)}


def ground_diagnostic(text: str, ledger_path: Path | None = None) -> dict:
    """唯一过滤器返回 {ok, filter}。否则 {ok: False, filters}，多条可为空。不挑一条。

    台账对上多条时，只有标题共享同一个 ASCII token、且该 token 仍只命中一条过滤器，
    才当成同一件事。没有共同 token 的范围维持缺口。
    """
    from agent.ledger import LEDGER_PATH, read_ledger
    from agent.supervise import unique_automation_filter

    needles = _ascii_needles(text)
    if needles:
        chosen = unique_automation_filter(*needles)
        if chosen:
            return {"ok": True, "filter": chosen}
    data = read_ledger(LEDGER_PATH if ledger_path is None else ledger_path)
    scopes = [item for item in (data.get("scopes") or []) if isinstance(item, dict)]
    matched = _matching_scopes(text, scopes)
    if len(matched) == 1:
        scope = matched[0]
        scope_needles = _ascii_needles(
            str(scope.get("title") or "") + "\n" + str(scope.get("note") or "")
        )
        return _ground_needles(scope_needles)
    if len(matched) > 1:
        return _ground_needles(_shared_title_needles(matched))
    return {"ok": False, "filters": []}
