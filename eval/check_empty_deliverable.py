# eval/check_empty_deliverable.py
"""交付清单为空时跳过，不问补路径，下游照跑。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check_release_and_downstream() -> None:
    from agent.run_windows import _empty_skip_satisfies, release_empty_deliverables

    compile_win = {
        "id": "W1", "state": "paused", "action": "compile_project",
        "error": "范围 A 的交付清单为空，这一窗暂停。", "summary": "", "depends_on": [],
    }
    launch = {
        "id": "W2", "state": "paused", "action": "launch_editor",
        "error": "范围 B 的交付清单为空，这一窗暂停。", "summary": "", "depends_on": ["W1"],
    }
    play = {
        "id": "W4", "state": "queued", "action": "implement_shooter",
        "error": "", "summary": "", "depends_on": ["W2"],
    }
    notes = release_empty_deliverables([compile_win, launch, play])
    assert len(notes) == 2, notes
    assert compile_win["state"] == "skipped" and "不用补路径" in compile_win["summary"], compile_win
    assert launch["state"] == "skipped", launch
    assert play["state"] == "queued", play
    assert _empty_skip_satisfies(compile_win) and _empty_skip_satisfies(launch)
    assert not _empty_skip_satisfies(play)

    windows = {item["id"]: item for item in (compile_win, launch, play)}
    unmet = []
    for dep in play["depends_on"]:
        dep_entry = windows[dep]
        if _empty_skip_satisfies(dep_entry):
            continue
        unmet.append(dep)
    assert unmet == [], unmet


def check_missing_name_does_not_ask_for_a_path() -> None:
    """query_actor 缺 name：不是 /Game 或头文件，跳过，下游不用等，也不问补路径。"""
    from agent.run_windows import _unfilled_skip_satisfies, release_unfilled_slots

    query = {
        "id": "W4", "state": "paused", "action": "query_actor",
        "error": "缺少槽位 name，不猜。", "summary": "", "depends_on": ["W3"],
        "window": {"action": "query_actor", "slots": {}, "error": "缺少槽位 name，不猜。"},
    }
    hud = {
        "id": "W5", "state": "queued", "action": "check_platform_hud",
        "error": "", "summary": "", "depends_on": ["W4"],
    }
    level = {
        "id": "W3", "state": "paused", "action": "open_level",
        "error": "缺少槽位 level，不猜。", "summary": "", "depends_on": [],
        "window": {"action": "open_level"},
    }
    notes = release_unfilled_slots([query, hud, level])
    assert len(notes) == 1 and "W4" in notes[0], notes
    assert query["state"] == "skipped" and "不用填" in query["summary"], query
    assert hud["state"] == "queued", hud
    assert level["state"] == "paused", level
    assert _unfilled_skip_satisfies(query)
    assert not _unfilled_skip_satisfies(hud)
    assert not _unfilled_skip_satisfies(level)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    check_release_and_downstream()
    check_missing_name_does_not_ask_for_a_path()
    print("empty deliverable ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
