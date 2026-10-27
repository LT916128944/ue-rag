# eval/check_diagnose.py
"""诊断句归地。不连编辑器，不叫模型。"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DIAG = "看看 level3 射击游戏是否有问题"
IMPLEMENT = "在 L_NewTest3 内实现简易 FPS 射击玩法"
HIT = "AIAgent.Fps.Hit"
MOVE = "AIAgent.Fps.Move"
FORBIDDEN = ("query_actor", "implement_shooter", "check_platform_hud", "open_level")


def _poison(_messages):
    return json.dumps({
        "steps": [{"script": "check_platform_hud", "slots": {}}],
    }, ensure_ascii=False)


def _cpp(*filters: str) -> str:
    lines = []
    for index, name in enumerate(filters):
        lines.append(
            "IMPLEMENT_SIMPLE_AUTOMATION_TEST("
            f"FDiag{index}, \"{name}\", "
            "EAutomationTestFlags::EditorContext | EAutomationTestFlags::ProductFilter)"
        )
    return "\n".join(lines) + "\n"


def _scope(sid: str = "A", title: str = "简易 FPS 射击", note: str = "") -> dict:
    item = {"id": sid, "title": title}
    if note:
        item["note"] = note
    return item


class _World:
    def __init__(self, filters: tuple[str, ...], scopes: list[dict]) -> None:
        self.filters = filters
        self.scopes = scopes
        self.root: Path | None = None
        self._tmp = None
        self._config = None
        self._ledger = None
        self._old_root = None
        self._old_path = None

    def __enter__(self) -> "_World":
        import config
        from agent import ledger

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        src = root / "src"
        src.mkdir()
        (src / "Diag.cpp").write_text(_cpp(*self.filters), encoding="utf-8")
        path = root / "ledger.json"
        path.write_text(
            json.dumps({"scopes": self.scopes}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.root = root
        self._config = config
        self._ledger = ledger
        self._old_root = config.UE_SOURCE_ROOT
        self._old_path = ledger.LEDGER_PATH
        config.UE_SOURCE_ROOT = src
        ledger.LEDGER_PATH = path
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._config is not None:
            self._config.UE_SOURCE_ROOT = self._old_root
        if self._ledger is not None:
            self._ledger.LEDGER_PATH = self._old_path
        if self._tmp is not None:
            self._tmp.cleanup()
        return False


def _actions(windows: list[dict]) -> list[str]:
    return [str(item.get("action") or "") for item in windows]


def _no_forbidden(windows: list[dict]) -> None:
    actions = _actions(windows)
    for name in FORBIDDEN:
        assert name not in actions, actions


def check_unique_filter_becomes_one_window() -> None:
    """诊断句不含 Fps。台账一个范围的标题含 FPS，源码恰好一条过滤器。"""
    from agent.diagnose import ground_diagnostic
    from agent.plan_windows import scope_windows

    with _World((HIT,), [_scope()]) as world:
        made = scope_windows(
            {"id": "C", "title": DIAG},
            "",
            complete=_poison,
            titles=[],
        )
        assert len(made) == 1, made
        window = made[0]
        assert window.get("action") == "run_automation_test", window
        assert window.get("realizable") is True, window
        assert window.get("automation_filter") == HIT, window
        assert (window.get("slots") or {}).get("filter") == HIT, window
        assert window.get("id") == "SCW1", window
        _no_forbidden(made)

        two = world.root / "two.json"
        two.write_text(json.dumps({
            "scopes": [_scope("A"), _scope("B", "射击练习")],
        }, ensure_ascii=False), encoding="utf-8")
        named = ground_diagnostic("看看 Fps 有没有问题", two)
        assert named == {"ok": True, "filter": HIT}, named


def check_many_filters_pause_without_picking() -> None:
    """两条过滤器都能被台账 token 命中：暂停，两条全名都在，不预选。"""
    from agent.plan_windows import scope_windows

    with _World((HIT, MOVE), [_scope()]):
        made = scope_windows(
            {"id": "C", "title": DIAG},
            "",
            complete=_poison,
            titles=[],
        )
        assert len(made) == 1, made
        window = made[0]
        assert window.get("action") == "run_automation_test", window
        assert window.get("realizable") is False, window
        error = str(window.get("error") or "")
        assert HIT in error and MOVE in error, error
        assert "r 过滤器全名" in error, error
        assert not str((window.get("slots") or {}).get("filter") or ""), window
        assert not str(window.get("automation_filter") or ""), window
        _no_forbidden(made)


def check_no_unique_grounding_stays_empty() -> None:
    """源码零条，或台账零个 / 多个范围对得上：空列表。"""
    from agent.diagnose import ground_diagnostic
    from agent.plan_windows import scope_windows

    cases = (
        ((), [_scope()]),
        (("AIAgent.Other.Walk",), [_scope()]),
        ((HIT,), []),
        ((HIT,), [_scope("A", "平台跳跃")]),
        ((HIT,), [_scope("A"), _scope("B", "射击练习")]),
    )
    for filters, scopes in cases:
        with _World(filters, scopes):
            made = scope_windows(
                {"id": "C", "title": DIAG},
                "",
                complete=_poison,
                titles=[],
            )
            assert made == [], (filters, scopes, made)
            grounded = ground_diagnostic(DIAG)
            assert grounded == {"ok": False, "filters": []}, (filters, scopes, grounded)


def check_duplicate_fps_scopes_share_one_filter() -> None:
    """同一件 FPS 写了两条台账时，「看看射击游戏是否有问题」仍收成那一条过滤器。"""
    from agent.plan_windows import scope_windows

    sentence = "看看射击游戏是否有问题"
    scopes = [
        _scope("A", "在 L_NewTest3 中实现简易 FPS 射击玩法"),
        _scope("D", "在 L_NewTest3 内实现简易 FPS 射击玩法"),
    ]
    with _World((HIT,), scopes):
        made = scope_windows(
            {"id": "C", "title": sentence},
            "",
            complete=_poison,
            titles=[],
        )
        assert len(made) == 1, made
        assert made[0].get("action") == "run_automation_test", made
        assert made[0].get("realizable") is True, made
        assert made[0].get("automation_filter") == HIT, made
        _no_forbidden(made)
    with _World((HIT, MOVE), scopes):
        made = scope_windows({"id": "C", "title": sentence}, "", complete=_poison, titles=[])
        assert len(made) == 1 and made[0].get("realizable") is False, made
        error = str(made[0].get("error") or "")
        assert HIT in error and MOVE in error and "r 过滤器全名" in error, error
        assert not str(made[0].get("automation_filter") or "")


def check_implement_sentence_stays_empty() -> None:
    """实现句不是诊断句。即便台账和源码都能唯一对上 FPS，仍然是 0 扇。"""
    from agent.diagnose import is_diagnostic
    from agent.plan_windows import scope_windows

    assert is_diagnostic(IMPLEMENT) is False
    assert is_diagnostic(DIAG) is True
    with _World((HIT,), [_scope()]):
        made = scope_windows({"id": "D", "title": IMPLEMENT}, "")
        assert made == [], made


def check_filter_reply_is_exact() -> None:
    """回复必须与声明全名逐字一致。过滤器暂停不按缺槽位跳过。"""
    from agent.run_windows import _apply_path, _pause_hint, release_unfilled_slots

    with _World((HIT, MOVE), [_scope()]):
        window = {
            "id": "SCW1",
            "action": "run_automation_test",
            "slots": {},
            "realizable": False,
            "blocked": True,
            "error": f"过滤器不是唯一的一条：{HIT}、{MOVE}。只给一次：r 过滤器全名。",
        }
        assert _pause_hint(window) == "s 停止 / c 跳过 / r 过滤器全名"
        assert "/Game" in _pause_hint({"error": "只给一次：r /Game/某目录"})
        assert _apply_path(dict(window), "aiagent.fps.hit", "") is None
        assert _apply_path(dict(window), "Fps.Hit", "") is None
        good = _apply_path(dict(window), HIT, "")
        assert good is not None
        assert good["slots"]["filter"] == HIT
        assert good["automation_filter"] == HIT
        assert good["realizable"] is True
        assert "error" not in good
        assert window["slots"] == {}

        paused = {
            "id": "SCW1", "state": "paused", "action": "run_automation_test",
            "error": window["error"], "summary": "", "depends_on": [],
        }
        missing = {
            "id": "W4", "state": "paused", "action": "query_actor",
            "error": "缺少槽位 name，不猜。", "summary": "", "depends_on": [],
        }
        notes = release_unfilled_slots([paused, missing])
        assert paused["state"] == "paused", paused
        assert missing["state"] == "skipped", missing
        assert len(notes) == 1 and "W4" in notes[0], notes


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    check_unique_filter_becomes_one_window()
    check_many_filters_pause_without_picking()
    check_no_unique_grounding_stays_empty()
    check_duplicate_fps_scopes_share_one_filter()
    check_implement_sentence_stays_empty()
    check_filter_reply_is_exact()
    print("diagnose ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
