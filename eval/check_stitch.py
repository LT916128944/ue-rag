# eval/check_stitch.py
"""中断接续：没人认领的条款由脚本拿掉或补编译扇；断线只重跑那一扇；其余才问一次模型。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.run_windows import _RunState
from agent.stitch import apply_stitch


def _state(query: str, windows: list[dict], criteria: list[dict], scopes=None) -> _RunState:
    state = _RunState("stitch-test", query, "test", windows, scopes=scopes or [])
    state.data["criteria"] = criteria
    state.save = lambda: None
    return state


def _window(ident: str, action: str, depends=None) -> dict:
    return {
        "id": ident, "title": ident, "action": action,
        "depends_on": list(depends or []), "realizable": True,
    }


def test_drop_unowned_without_model() -> None:
    calls = []
    state = _state(
        "打开编辑器，在关卡 L_NewTest3 中实现简易的FPS射击游戏",
        [_window("W1", "launch_editor"), _window("W2", "implement_shooter", ["W1"]),
         _window("W3", "integrate_verify", ["W1", "W2"])],
        [
            {"id": "c-cpp-build", "evidence_kind": "cpp_build", "text": "要编译", "passed": False, "windows": []},
            {"id": "c-hit", "evidence_kind": "automation", "text": "命中", "passed": True,
             "filter": "AIAgent.Fps.Hit", "windows": ["W2"]},
        ],
        scopes=[{"id": "A", "window_ids": ["W1", "W2"], "criteria": [
            {"id": "c-cpp-build", "evidence_kind": "cpp_build", "passed": False, "windows": []},
        ]}],
    )
    assert apply_stitch(state, state.data["query"], complete=lambda _messages: calls.append(1)) is True
    ids = [item["id"] for item in state.data["criteria"]]
    assert ids == ["c-hit"], ids
    assert state.data["scopes"][0]["criteria"] == [], state.data["scopes"]
    assert calls == []
    assert apply_stitch(state, state.data["query"], complete=lambda _messages: calls.append(1)) is False
    assert calls == []


def test_compile_sentence_inserts_one_window() -> None:
    state = _state(
        "编译代码，打开编辑器",
        [_window("W1", "launch_editor"), _window("W2", "integrate_verify", ["W1"])],
        [{"id": "c-cpp-build", "evidence_kind": "cpp_build", "text": "编译通过", "passed": False, "windows": []}],
        scopes=[{"id": "A", "window_ids": ["W1"]}],
    )
    assert apply_stitch(state, state.data["query"], complete=lambda _messages: "不许调用") is True
    added = state.window("S1")
    assert added["action"] == "compile_project", added
    assert added["state"] == "queued"
    assert state.window("W1")["depends_on"] == ["S1"]
    assert state.window("W2")["depends_on"] == ["W1", "S1"]
    assert state.data["criteria"][0]["windows"] == ["S1"]
    assert state.data["scopes"][0]["window_ids"] == ["W1", "S1"]
    assert "S1" not in state.data["windows"] or state.window("W1")["state"] == "queued"


def test_owned_criterion_stays() -> None:
    state = _state(
        "打开编辑器",
        [_window("W1", "launch_editor")],
        [{"id": "c-cpp-build", "evidence_kind": "cpp_build", "passed": False, "windows": ["W1"]},
         {"id": "c-manual", "evidence_kind": "manual", "passed": False, "windows": []}],
    )
    assert apply_stitch(state, state.data["query"], complete=lambda _messages: "不许调用") is False
    assert [item["id"] for item in state.data["criteria"]] == ["c-cpp-build", "c-manual"]


def test_connection_requeues_once() -> None:
    calls = []
    state = _state(
        "打开编辑器",
        [_window("W1", "launch_editor"), _window("W2", "integrate_verify", ["W1"])],
        [],
    )
    failed = state.window("W1")
    failed["state"] = "failed"
    failed["summary"] = "UE MCP 调用失败: ConnectError: [WinError 10061]"
    state.window("W2")["state"] = "failed"
    assert apply_stitch(state, state.data["query"], complete=lambda _messages: calls.append(1)) is True
    assert state.window("W1")["state"] == "queued"
    assert state.window("W1")["summary"] == ""
    assert state.window("W2")["state"] == "queued"
    assert calls == []
    state.window("W1")["state"] = "failed"
    state.window("W1")["summary"] = "ConnectError: 10061"
    assert apply_stitch(state, state.data["query"], complete=lambda _messages: calls.append(1)) is True
    assert calls == [1]


def test_model_inserts_one_catalog_action() -> None:
    seen = {}

    def complete(messages):
        seen["user"] = messages[1]["content"]
        return '{"action":"launch_editor","unresolved":""}'

    state = _state(
        "在关卡 L_NewTest3 里放一个靶",
        [_window("W1", "implement_shooter"), _window("W2", "integrate_verify", ["W1"])],
        [{"id": "c-hit", "evidence_kind": "automation", "passed": False, "filter": "AIAgent.Fps.Hit", "windows": ["W1"]}],
    )
    state.window("W1")["state"] = "failed"
    state.window("W1")["summary"] = "编辑器未连通"
    state.window("W2")["state"] = "failed"
    assert apply_stitch(state, state.data["query"], complete=complete) is True
    assert "L_NewTest3" in seen["user"]
    assert "编辑器未连通" in seen["user"]
    assert state.window("S1")["action"] == "launch_editor"
    assert state.window("W1")["state"] == "queued"
    assert state.window("W1")["depends_on"] == ["S1"]
    assert state.window("W2")["state"] == "queued"
    assert apply_stitch(state, state.data["query"], complete=lambda _messages: (_ for _ in ()).throw(AssertionError("第二次"))) is False


def test_model_rejects_unknown_action_and_missing_slot() -> None:
    state = _state(
        "打开编辑器",
        [_window("W1", "implement_shooter")],
        [],
    )
    state.window("W1")["state"] = "failed"
    state.window("W1")["summary"] = "没有关卡"
    assert apply_stitch(state, state.data["query"], complete=lambda _messages: '{"action":"invent","unresolved":""}') is True
    assert "S1" not in state.data["windows"]
    assert "表外" in state.data["stitch_unresolved"]
    assert state.window("W1")["state"] == "failed"

    state.data["stitch_model"] = False
    assert apply_stitch(
        state, "打开两个关卡",
        complete=lambda _messages: '{"action":"create_level","unresolved":""}',
    ) is True
    assert "S1" not in state.data["windows"]
    assert "槽位" in state.data["stitch_note"]


def main() -> int:
    test_drop_unowned_without_model()
    test_compile_sentence_inserts_one_window()
    test_owned_criterion_stays()
    test_connection_requeues_once()
    test_model_inserts_one_catalog_action()
    test_model_rejects_unknown_action_and_missing_slot()
    print("stitch ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
