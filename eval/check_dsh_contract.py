# eval/check_dsh_contract.py
"""离线合同检查：夹具 + 假 judge，不连编辑器、不调模型。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import dsh_cli, harness  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dsh_run_min.json"


def fail(message: str) -> None:
    print(f"dsh-contract failed: {message}")
    raise SystemExit(1)


def check(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def fake_judge(spec: dict, task: dict | None = None) -> dict:
    del task
    kind = str((spec or {}).get("kind") or "")
    mode = str((spec or {}).get("fake") or "")
    if mode == "mismatch":
        return {"ok": True, "kind": "automation", "summary": "kind 不对"}
    if mode == "fail":
        return {
            "ok": False,
            "kind": kind,
            "summary": "没过",
            "policy": {"save": False, "pie": False, "allow_writes": []},
        }
    if mode == "long":
        return {"ok": True, "kind": kind, "summary": "长" * 300}
    if mode == "match":
        return {"ok": True, "kind": kind, "summary": "过了"}
    return {"ok": False, "kind": "unknown", "summary": f"未知夹具 {mode}"}


class _Args:
    def __init__(self, run: str, criterion: str | None = None, all_: bool = False, dry_run: bool = False):
        self.run = run
        self.criterion = criterion
        self.all = all_
        self.dry_run = dry_run


def load_fixture() -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    check(isinstance(data, dict), "夹具不是对象")
    kinds = [item.get("evidence_kind") for item in data.get("criteria") or []]
    check(kinds == ["cpp_build", "manual", "editor_assert"], f"夹具条款不对: {kinds}")
    return data


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


def call_status(run: str) -> dict:
    return capture(dsh_cli.cmd_contract_status, _Args(run))


def passed_map(doc: dict) -> dict:
    return {item["id"]: bool(item.get("passed")) for item in doc["criteria"]}


def main() -> int:
    base = load_fixture()
    rows = [dict(item) for item in base["criteria"]]

    claimed = harness.ignore_model_claim(rows, "已完成")
    check(passed_map({"criteria": claimed}) == {"c-build": False, "c-manual": False, "c-assert": False},
          "模型文本「已完成」改动了 passed")

    matched = harness.apply_judge(rows, {"ok": True, "kind": "cpp_build", "summary": "过了", "window": "W1"})
    check(row({"criteria": matched}, "c-build").get("passed") is True, "匹配 judge 没有把 cpp_build 改成通过")
    check(row({"criteria": matched}, "c-manual").get("passed") is False, "匹配 judge 改动了 manual")
    check(row({"criteria": matched}, "c-assert").get("passed") is False, "匹配 judge 改动了其他条款")

    mismatched = harness.apply_judge(rows, {"ok": True, "kind": "automation", "summary": "过了", "window": "W1"})
    check(passed_map({"criteria": mismatched}) == {"c-build": False, "c-manual": False, "c-assert": False},
          "kind 不匹配却改了合同")

    manual_hit = harness.apply_judge(
        rows, {"ok": True, "kind": "manual", "summary": "已完成", "window": "W1"}
    )
    manual_hit = harness.apply_judge(
        manual_hit, {"ok": True, "kind": "cpp_build", "summary": "已完成", "window": "W1"}
    )
    check(row({"criteria": manual_hit}, "c-manual").get("passed") is False, "manual 被任意 judge 改成通过")

    autos_done = []
    for item in rows:
        copied = dict(item)
        if copied.get("evidence_kind") != "manual":
            copied["passed"] = True
        autos_done.append(copied)
    check(harness.contract_status(autos_done, "成功") == "部分完成", "自动全过且有 manual 时应为部分完成")

    dsh_cli.run_judge = fake_judge
    with tempfile.TemporaryDirectory(prefix="dsh-contract-") as tmp_name:
        root = Path(tmp_name)
        os.environ["UE_DSH_RUNS_DIR"] = str(root)

        happy = write_run(root, "dsh-run-min", base)
        batch = call_verify("dsh-run-min", all_=True)
        saved = read_run(happy)
        check(batch.get("verified") and len(batch["verified"]) == 2, f"--all 应跑两条自动条款: {batch}")
        check(row(saved, "c-build").get("passed") is True, "--all 后 cpp_build 未通过")
        check(row(saved, "c-assert").get("passed") is True, "--all 后 editor_assert 未通过")
        check(row(saved, "c-manual").get("passed") is False, "--all 改动了 manual")
        status = call_status("dsh-run-min")
        check(status.get("status") == "部分完成", f"contract_status 应为部分完成: {status}")

        one = call_verify("dsh-run-min", criterion="c-build")
        check(one.get("ok") is True and one.get("kind") == "cpp_build", f"单条 verify 缺少 ok/kind: {one}")
        check(one.get("window") == "W1" and one.get("judge"), f"单条 verify 缺少 window/judge: {one}")
        check(len(str(one.get("judge"))) <= 240, "judge 原文超过 240 字")

        manual = call_verify("dsh-run-min", criterion="c-manual")
        check(manual.get("ok") is False and row(read_run(happy), "c-manual").get("passed") is False,
              f"manual verify 不应通过: {manual}")
        check(isinstance(manual.get("policy"), dict), "拒绝时没有 policy")

        long_doc = json.loads(json.dumps(base))
        long_doc["windows"]["W1"]["window"]["task"]["verify"] = [
            {"kind": "cpp_build", "fake": "long"}
        ]
        long_path = write_run(root, "dsh-long", long_doc)
        long_result = call_verify("dsh-long", criterion="c-build")
        check(long_result.get("passed") is True, f"长 summary 应仍可通过: {long_result}")
        check(len(str(long_result.get("summary") or "")) <= 240, "summary 超过 240 字")
        check(len(str(long_result.get("judge") or "")) <= 240, "长 judge 超过 240 字")
        check(row(read_run(long_path), "c-manual").get("passed") is False, "长 summary 改动了其他条款")

        mismatch_doc = json.loads(json.dumps(base))
        mismatch_doc["windows"]["W1"]["window"]["task"]["verify"] = [
            {"kind": "editor_assert", "fake": "mismatch"}
        ]
        mismatch_path = write_run(root, "dsh-mismatch", mismatch_doc)
        os.utime(mismatch_path, (time.time() - 3600, time.time() - 3600))
        before = (mismatch_path.stat().st_mtime_ns, mismatch_path.read_text(encoding="utf-8"))
        mismatch = call_verify("dsh-mismatch", criterion="c-assert")
        after = (mismatch_path.stat().st_mtime_ns, mismatch_path.read_text(encoding="utf-8"))
        check(mismatch.get("ok") is False, f"kind 不匹配应失败: {mismatch}")
        check(before == after, "kind 不匹配却写了 run JSON")

        stop_doc = json.loads(json.dumps(base))
        stop_doc["windows"]["W1"]["window"]["task"]["verify"] = [
            {"kind": "cpp_build", "fake": "fail"},
            {"kind": "cpp_build", "fake": "match"},
        ]
        stop_path = write_run(root, "dsh-stop", stop_doc)
        stopped = call_verify("dsh-stop", criterion="c-build")
        check(stopped.get("ok") is False and stopped.get("passed") is False, f"第一个 kind 匹配应被采用: {stopped}")
        check(stopped.get("policy", {}).get("save") is False, "失败裁判应带上 policy")
        check(row(read_run(stop_path), "c-build").get("passed") is False, "失败后的后续 spec 仍把条款改成通过")

        empty_doc = json.loads(json.dumps(base))
        for item in empty_doc["criteria"]:
            if item["id"] == "c-build":
                item["windows"] = []
        empty_path = write_run(root, "dsh-empty", empty_doc)
        os.utime(empty_path, (time.time() - 3600, time.time() - 3600))
        before = (empty_path.stat().st_mtime_ns, empty_path.read_text(encoding="utf-8"))
        empty = call_verify("dsh-empty", criterion="c-build")
        after = (empty_path.stat().st_mtime_ns, empty_path.read_text(encoding="utf-8"))
        check(empty.get("ok") is False and "窗口" in str(empty.get("error")), f"空 windows 应失败: {empty}")
        check(before == after, "空 windows 却写了 run JSON")

        dry_doc = json.loads(json.dumps(base))
        dry_path = write_run(root, "dsh-dry", dry_doc)
        os.utime(dry_path, (time.time() - 3600, time.time() - 3600))
        before = (dry_path.stat().st_mtime_ns, dry_path.read_text(encoding="utf-8"))
        dry = call_verify("dsh-dry", criterion="c-build", dry_run=True)
        after = (dry_path.stat().st_mtime_ns, dry_path.read_text(encoding="utf-8"))
        check(dry.get("ok") is True and dry.get("dry_run") is True, f"dry-run 失败: {dry}")
        check(before == after, "dry-run 改写了 run JSON")

        cont_doc = json.loads(json.dumps(base))
        cont_doc["windows"]["W1"]["window"]["task"]["verify"] = [
            {"kind": "cpp_build", "fake": "fail"},
            {"kind": "editor_assert", "fake": "match"},
        ]
        cont_path = write_run(root, "dsh-cont", cont_doc)
        continued = call_verify("dsh-cont", all_=True)
        cont_saved = read_run(cont_path)
        check(len(continued.get("verified") or []) == 2, f"--all 失败后中断了: {continued}")
        check(row(cont_saved, "c-build").get("passed") is False, "失败条款被写成通过")
        check(row(cont_saved, "c-assert").get("passed") is True, "失败后的下一条没有继续跑")
        check(row(cont_saved, "c-manual").get("passed") is False, "--all 失败路径改动了 manual")

        handoff = harness.build_handoff(
            did="编译工程",
            judges=[{"kind": "cpp_build", "ok": True, "summary": "过了"}],
            ok=True,
            criteria=[{"id": "c-build", "text": "编译通过", "evidence_kind": "cpp_build", "passed": True}],
            next_title="下一扇",
            artifacts={"BP_A": {"ref": "Actor.A", "in_level": True}},
            record_dir=root,
            day="2026-10-23",
        )
        handoff_path = root / "handoff.json"
        handoff_path.write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")

        class _File:
            file = str(handoff_path)

        validated = capture(dsh_cli.cmd_handoff_validate, _File())
        check(validated.get("ok") is True, f"合法交接没通过: {validated}")

    print("dsh-contract ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
