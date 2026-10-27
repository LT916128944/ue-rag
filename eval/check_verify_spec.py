# eval/check_verify_spec.py
"""离线检查：DSH 的 verify 能从 run 记录里取回 verify spec，且只由裁判改 passed。

钉住四件事（不连编辑器、不调模型、不写真 run 记录）：
1. 新记录 `entry["task"]["verify"]` 可读回——写侧 `run_windows._task_snapshot` 落的那一份；
2. 旧记录（没有 `entry["task"]`）回落到确定性复原 / `window.task` 也不炸；
3. spec 的 kind 与条款的 evidence_kind 对上才算候选；
4. 只有裁判 `{ok:true, kind}` 而且 kind 对得上才改 passed，`manual` 永远是 false，
   模型文本「已完成」改不动任何条款。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import dsh_cli, harness, run_windows  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEGACY_FIXTURE = FIXTURES / "dsh_run_legacy.json"
# 真 run 记录（只读夹具）：W3 place_actor / W8 probe_play 就是没有 task.verify 的那种。
REAL_RUN = ROOT / "data" / "runs" / "20261024" / "20261024-011917.json"


def fail(message: str) -> None:
    print(f"verify-spec failed: {message}")
    raise SystemExit(1)


def check(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    check(isinstance(data, dict), f"夹具不是对象: {path}")
    return data


def kinds(specs: list[dict]) -> list[str]:
    return [str(spec.get("kind") or "") for spec in specs]


class _Args:
    def __init__(self, run: str, criterion: str | None = None, all_: bool = False, dry_run: bool = False):
        self.run = run
        self.criterion = criterion
        self.all = all_
        self.dry_run = dry_run


def capture(fn, *args) -> dict:
    saved = dsh_cli._emit
    box: dict = {}

    def grab(payload: dict) -> int:
        box["payload"] = payload
        return 0 if payload.get("ok") else 1

    dsh_cli._emit = grab
    try:
        fn(*args)
    finally:
        dsh_cli._emit = saved
    return box["payload"]


def call_verify(run: str, criterion: str | None = None, all_: bool = False, dry_run: bool = False) -> dict:
    return capture(dsh_cli.cmd_verify, _Args(run, criterion, all_, dry_run))


def write_run(root: Path, run_id: str, doc: dict) -> Path:
    body = json.loads(json.dumps(doc))
    body["run_id"] = run_id
    path = root / f"{run_id}.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_run(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def row(doc: dict, cid: str) -> dict:
    return next(item for item in doc["criteria"] if item["id"] == cid)


def fake_judge(spec: dict, task: dict | None = None) -> dict:
    """假裁判：只看 spec 里写的 fake 字段，不连编辑器。顺便把拿到的 task 记下来验策略透传。

    没写 fake 的 spec（自动化合成的那种 `{"kind": "automation", "filter": ...}`）按通过算：
    这里验的是「spec 取回来了、裁判被叫到了」，不是真去跑 Automation。"""
    seen.append({"spec": spec, "task": task or {}})
    kind = str((spec or {}).get("kind") or "")
    mode = str((spec or {}).get("fake") or "match")
    if mode == "match":
        return {"ok": True, "kind": kind, "summary": "过了"}
    if mode == "wrong_kind":
        return {"ok": True, "kind": "automation", "summary": "kind 不对"}
    return {"ok": False, "kind": kind, "summary": "没过"}


seen: list[dict] = []


def check_write_side() -> None:
    """写侧：_task_snapshot 把这一扇真正要跑的 spec 与策略摊平。"""
    move = {
        "id": "W2",
        "action": "move_actor",
        "verify": [{"kind": "editor_assert", "actor_name": "Cube",
                    "expect": {"location": {"x": 1, "y": 2, "z": 3}}}],
        "allow_writes": ["set_actor_transform"],
        "save": False,
        "pie": True,
    }
    snap = run_windows._task_snapshot(move)
    check(snap is not None, "带 task 的窗口没有 task 快照")
    check(kinds(snap["verify"]) == ["editor_assert"], f"快照没留下 spec 原件: {snap}")
    check(snap["verify"][0]["expect"]["location"]["z"] == 3, "快照改了 spec 原文")
    check(snap["allow_writes"] == ["set_actor_transform"], f"快照丢了 allow_writes: {snap}")
    check(snap["save"] is False and snap["pie"] is True, f"快照改了 save/pie: {snap}")

    # 只靠 automation_filter 的扇（run_automation_test）也要留一条 automation spec：
    # 运行期 nodes._test_specs 就是这么合成的，不留就还是「窗口里没有 spec」。
    auto = run_windows._task_snapshot({
        "id": "W7", "action": "run_automation_test", "verify": [],
        "automation_filter": "AIAgent.Platform", "allow_writes": [],
    })
    check(auto is not None and auto["verify"] == [{"kind": "automation", "filter": "AIAgent.Platform"}],
          f"automation_filter 没合成 spec: {auto}")

    # 非 spec 的坏 kind 不收；空 verify 的扇老老实实留空，不补一条。
    junk = run_windows._task_snapshot({"id": "W3", "action": "place_actor", "verify": [{"kind": "nonsense"}]})
    check(junk is not None and junk["verify"] == [], f"非法 kind 被当成 spec: {junk}")
    empty = run_windows._task_snapshot({"id": "W8", "action": "probe_play", "verify": []})
    check(empty is not None and empty["verify"] == [], f"空 verify 被补了 spec: {empty}")

    # 窗口定义（规划产物）进 _entry 时，规划期就带 task 的先把 spec 落一份。
    state_entry = run_windows._RunState.__new__(run_windows._RunState)
    entry = state_entry._entry({
        "id": "W1", "action": "compile_blueprint", "depends_on": [],
        "task": {"id": "W1", "verify": [{"kind": "blueprint_compile", "refPath": "/Game/BP_Gun.BP_Gun"}],
                 "allow_writes": [], "save": True, "pie": False},
    })
    check(kinds(entry.get("task", {}).get("verify") or []) == ["blueprint_compile"],
          f"_entry 没带上规划期的 spec: {entry.get('task')}")
    check(entry["window"]["task"]["verify"][0]["refPath"] == "/Game/BP_Gun.BP_Gun", "_entry 改了 window 定义")

    # 规划窗口本身没有 task 时不凭空造一个。
    bare = state_entry._entry({"id": "W3", "action": "place_actor", "depends_on": []})
    check("task" not in bare, f"没 task 的窗口被造了一个: {bare.get('task')}")


def check_read_side_new_record() -> None:
    """读侧：新记录（entry["task"] 在）优先用它，取到空就不再往后补。"""
    new_doc = load(LEGACY_FIXTURE)
    new_doc["windows"]["W3"]["task"] = {
        "id": "W3", "action": "place_actor",
        "verify": [{"kind": "editor_assert", "actor_name": "BP_Coin",
                    "expect": {"box": {"min": {"x": 0, "y": 0, "z": 0},
                                       "max": {"x": 200, "y": 200, "z": 200}}}}],
        "allow_writes": [], "save": True, "pie": False, "automation_filter": "AIAgent.Coin",
    }
    entry = new_doc["windows"]["W3"]
    specs, task, source = dsh_cli._verify_specs(entry, new_doc)
    check(source == "task", f"新记录没走 entry.task: {source}")
    check(kinds(specs) == ["editor_assert", "automation"], f"新记录 spec 没读回: {specs}")
    check(task.get("save") is True and task.get("pie") is False, f"新记录策略没读回: {task}")

    # 记录里 verify 是空的，就老实说没有——不许拿别的来源或规则补一条。
    blank = json.loads(json.dumps(new_doc))
    blank["windows"]["W3"]["task"]["verify"] = []
    blank["windows"]["W3"]["task"].pop("automation_filter")
    blank["windows"]["W3"]["window"]["task"] = {"verify": [{"kind": "editor_assert"}]}
    specs, _task, source = dsh_cli._verify_specs(blank["windows"]["W3"], blank)
    check(specs == [] and source == "task", f"空 verify 被别的来源补上了: {source} {specs}")

    # detail.test / detail.build 是裁判结论，不是 spec，任何时候都不当 spec 用。
    judged = json.loads(json.dumps(blank))
    judged["windows"]["W3"]["detail"] = {
        "test": {"ok": True, "kind": "editor_assert", "summary": "过了"},
        "build": {"ok": True, "kind": "cpp_build", "summary": "过了"},
    }
    specs, _task, _source = dsh_cli._verify_specs(judged["windows"]["W3"], judged)
    check(specs == [], f"裁判结论被当成 spec 用了: {specs}")


def check_read_side_legacy_record() -> None:
    """读侧：旧记录（没有 entry["task"]）走确定性复原 / window.task，不炸也不猜。"""
    legacy = load(LEGACY_FIXTURE)
    for entry in legacy["windows"].values():
        check("task" not in entry, f"旧夹具不该有 entry.task: {entry.get('id')}")

    specs, task, source = dsh_cli._verify_specs(legacy["windows"]["W1"], legacy)
    check(source == "rebuilt" and kinds(specs) == ["blueprint_compile"],
          f"旧记录复原不出 blueprint_compile: {source} {specs}")
    check(task.get("save") is True and task.get("pie") is True, f"旧记录复原丢了策略: {task}")

    specs, task, source = dsh_cli._verify_specs(legacy["windows"]["W7"], legacy)
    check(source == "rebuilt" and specs == [{"kind": "automation", "filter": "AIAgent.Platform"}],
          f"旧记录复原不出 automation: {source} {specs}")

    # place_actor 运行期本来就不跑裁判：复原出来仍是空，不许猜一条 editor_assert。
    specs, _task, source = dsh_cli._verify_specs(legacy["windows"]["W3"], legacy)
    check(specs == [] and source == "none", f"直接动作扇被猜了 spec: {source} {specs}")

    # 规划期带 task 的窗口（probe_play 那种）走 window.task。
    specs, task, source = dsh_cli._verify_specs(legacy["windows"]["W8"], legacy)
    check(source == "window.task" and specs == [], f"window.task 回落不对: {source} {specs}")
    check(task.get("pie") is True and task.get("save") is False, f"window.task 策略没读回: {task}")

    # 空 entry / 坏 entry 不许抛。
    for entry in ({}, {"window": {}}, {"window": {"task": None}}, {"task": "not-a-dict"}):
        specs, task, source = dsh_cli._verify_specs(entry, legacy)
        check(specs == [] and isinstance(task, dict), f"坏 entry 没被兜住: {entry} -> {source}")

    # 真 run 记录在就一并验：W3 place_actor / W8 probe_play 就是合同空洞的现场证据。
    if REAL_RUN.is_file():
        real = load(REAL_RUN)
        for wid in ("W3", "W8"):
            specs, task, source = dsh_cli._verify_specs(real["windows"][wid], real)
            check(specs == [], f"{wid} 不该凭空有 spec: {source} {specs}")
            check(isinstance(task, dict), f"{wid} 没回落出任务策略")
        specs, _task, source = dsh_cli._verify_specs(real["windows"]["W7"], real)
        check(kinds(specs) == ["automation"], f"真 run 的 W7 取不回 automation spec: {source} {specs}")


def check_verify_only_judge_writes() -> None:
    """verify 全链：spec 是候选、裁判 kind 要对得上、manual 与模型文本都不许改 passed。"""
    saved_judge = dsh_cli.run_judge
    dsh_cli.run_judge = fake_judge
    previous_runs = os.environ.get("UE_DSH_RUNS_DIR")
    seen.clear()
    try:
        with tempfile.TemporaryDirectory(prefix="verify-spec-") as tmp_name:
            root = Path(tmp_name)
            os.environ["UE_DSH_RUNS_DIR"] = str(root)

            doc = load(LEGACY_FIXTURE)
            # W3 补一条 editor_assert spec（模拟写侧落盘后的新记录）；W8 保持没有 spec。
            doc["windows"]["W3"]["task"] = {
                "id": "W3", "action": "place_actor",
                "verify": [{"kind": "editor_assert", "fake": "match"}],
                "allow_writes": [], "save": True, "pie": False,
            }
            # W7 只靠 automation_filter 的 spec，另给一条 evidence_kind 对不上的 editor_assert。
            doc["windows"]["W7"]["task"] = {
                "id": "W7", "action": "run_automation_test", "verify": [],
                "automation_filter": "AIAgent.Platform", "allow_writes": [], "save": False, "pie": True,
            }
            path = write_run(root, "verify-spec-new", doc)

            # 1. 有 spec 的 editor_assert 条款：拿到 spec 跑裁判，裁判 {ok:true, kind} 对上才写 passed。
            result = call_verify("verify-spec-new", criterion="c-editor_assert-level")
            check(result.get("ok") is True and result.get("kind") == "editor_assert",
                  f"editor_assert 条款没跑成: {result}")
            check(result.get("window") == "W3", f"候选窗口不对: {result}")
            saved_doc = read_run(path)
            check(row(saved_doc, "c-editor_assert-level").get("passed") is True, "裁判通过后没写 passed")
            check(row(saved_doc, "c-widget_graph-hud").get("passed") is False, "同窗口别的条款被勾了")
            check(row(saved_doc, "c-manual-plan").get("passed") is False, "manual 被改成了 passed")
            check(row(saved_doc, "c-automation-platform").get("passed") is False, "别的条款被改成 passed")
            # 裁判拿到的 task 是这一扇落盘的策略，不是 window 定义。
            assert seen, "裁判根本没被调用"
            check(seen[-1]["task"].get("save") is True and seen[-1]["task"].get("pie") is False,
                  f"裁判拿到的策略不是落盘的那份: {seen[-1]['task']}")

            # 2. manual 条款：连裁判都不许叫，passed 保持 false。
            seen.clear()
            manual = call_verify("verify-spec-new", criterion="c-manual-plan")
            check(manual.get("ok") is False and manual.get("kind") == "manual",
                  f"manual 条款不该能 verify: {manual}")
            check(seen == [], "manual 条款叫了裁判")
            check(row(read_run(path), "c-manual-plan").get("passed") is False, "manual 被改成 passed")

            # 3. 只有 automation_filter 的 automation 条款：spec 合成成功才能跑裁判。
            auto = call_verify("verify-spec-new", criterion="c-automation-platform")
            check(auto.get("ok") is True and auto.get("kind") == "automation",
                  f"automation 条款没跑成: {auto}")
            check(auto.get("window") == "W7", f"automation 候选窗口不对: {auto}")
            check(row(read_run(path), "c-automation-platform").get("passed") is True,
                  "automation 裁判通过后没写 passed")

            # 4. 没有 spec 的条款：老实失败，且不动 run JSON。
            no_spec = json.loads(json.dumps(doc))
            del no_spec["windows"]["W3"]["task"]
            no_spec["windows"]["W3"]["window"].pop("task", None)
            no_spec["windows"]["W3"]["window"]["action"] = "place_actor"
            no_spec_path = write_run(root, "verify-spec-nospec", no_spec)
            before = no_spec_path.read_text(encoding="utf-8")
            missed = call_verify("verify-spec-nospec", criterion="c-editor_assert-level")
            after = no_spec_path.read_text(encoding="utf-8")
            check(missed.get("ok") is False and "verify spec" in str(missed.get("error")),
                  f"没 spec 时没说清楚: {missed}")
            check(isinstance(missed.get("checked"), list) and missed["checked"],
                  f"没 spec 时没交代查过哪些窗口: {missed}")
            check(before == after, "没 spec 却写了 run JSON")

            # 5. 裁判 kind 与条款 kind 对不上：不算证据，passed 不变。
            wrong = json.loads(json.dumps(doc))
            wrong["windows"]["W3"]["task"]["verify"] = [{"kind": "editor_assert", "fake": "wrong_kind"}]
            wrong_path = write_run(root, "verify-spec-wrongkind", wrong)
            before = wrong_path.read_text(encoding="utf-8")
            mismatched = call_verify("verify-spec-wrongkind", criterion="c-editor_assert-level")
            after = wrong_path.read_text(encoding="utf-8")
            check(mismatched.get("ok") is False, f"kind 不匹配却算过: {mismatched}")
            check(row(read_run(wrong_path), "c-editor_assert-level").get("passed") is False,
                  "kind 不匹配却写了 passed")
            check(before == after, "kind 不匹配却写了 run JSON")

            # 6. 模型文本「已完成」不许动合同：ignore_model_claim 是唯一的处理口径。
            claimed = harness.ignore_model_claim(doc["criteria"], "已完成，全部通过")
            check(all(item.get("passed") is False for item in claimed
                      if item["id"] in ("c-editor_assert-level", "c-manual-plan")),
                  f"模型文本改了 passed: {claimed}")

            # 7. dry-run：拿到 spec 也不写。
            dry_path = write_run(root, "verify-spec-dry", doc)
            before = dry_path.read_text(encoding="utf-8")
            dry = call_verify("verify-spec-dry", criterion="c-editor_assert-level", dry_run=True)
            after = dry_path.read_text(encoding="utf-8")
            check(dry.get("ok") is True and dry.get("dry_run") is True, f"dry-run 失败: {dry}")
            check(dry.get("candidates") == 1, f"dry-run 候选数不对: {dry}")
            check(before == after, "dry-run 写了 run JSON")
    finally:
        dsh_cli.run_judge = saved_judge
        if previous_runs is None:
            os.environ.pop("UE_DSH_RUNS_DIR", None)
        else:
            os.environ["UE_DSH_RUNS_DIR"] = previous_runs


def main() -> int:
    check_write_side()
    check_read_side_new_record()
    check_read_side_legacy_record()
    check_verify_only_judge_writes()
    print("verify-spec ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
