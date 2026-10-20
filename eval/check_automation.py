# eval/check_automation.py
"""升级 3 自动化层检查：角色白名单、交接 goal、记忆来源、园丁、读档、记分板。

用临时 trace 与临时目录，不读生产 data/traces，不改真实 learned_skills.json。
不另写一套 graph.py 边断言；边由 eval/check_routes.py 保证。"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.roles import tool_allowed


def check_roles() -> None:
    research = {
        "harness_role": "research",
        "verify": [{"kind": "editor_assert"}],
        "allow_writes": ["apply_unified_diff", "set_actor_transform"],
    }
    assert not tool_allowed("apply_unified_diff", research), "research 角色的写工具必须被 tool_allowed 拒绝"
    assert not tool_allowed("set_actor_transform", research), "allow_writes 写了也拒（收紧）"
    assert tool_allowed("read_source", research)
    assert tool_allowed("search_ue_project", research)
    assert tool_allowed("judge_pickup", research)
    assert tool_allowed("run_automation_test", research)
    review = {"harness_role": "review", "verify": [{"kind": "editor_assert"}], "allow_writes": ["set_pickable"]}
    assert not tool_allowed("set_pickable", review)
    plan_task = {"harness_role": "plan", "verify": [{"kind": "cpp_build"}]}
    assert not tool_allowed("read_source", plan_task), "plan 角色无工具，只返回窗口清单"
    cpp = {"verify": [{"kind": "cpp_build"}], "allow_writes": []}
    assert tool_allowed("read_source", cpp), "未设置 harness_role 时行为与现在完全一致"
    assert tool_allowed("apply_unified_diff", cpp)


def check_handoff_goal() -> None:
    from agent.harness import build_handoff, load_goal

    payload = build_handoff(did="自动化层检查", ok=True)
    assert "goal" in payload, "交接 JSON 必须含 goal 键"
    assert payload["goal"] == load_goal()


def check_promote_provenance() -> None:
    from agent.skill_memory import load_skills, promote_from_route

    route = {
        "id": "auto-provenance",
        "when": ["自动化"],
        "roles": ["target"],
        "windows": [{"action": "set_pickable", "slots": {"target": "target"}}],
    }
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        saved = {
            key: os.environ.get(key)
            for key in ("UE_LEARNED_SKILLS", "UE_LEARNED_ROUTES", "UE_DISTILL_ROOT")
        }
        os.environ["UE_LEARNED_SKILLS"] = str(tmp / "learned_skills.json")
        os.environ["UE_LEARNED_ROUTES"] = str(tmp / "learned_routes.json")
        os.environ["UE_DISTILL_ROOT"] = str(tmp)
        try:
            rid = promote_from_route(route, source_run="")
            assert rid == "auto-provenance", "空来源停在影子，仍返回技能 id"
            stored = load_skills()
            assert stored and all(str(item.get("status")) != "promoted" for item in stored), \
                "source_run 为空不得把状态写成 promoted"
            assert promote_from_route(route, source_run="run-1") == "auto-provenance"
            hit = next(item for item in load_skills() if item.get("id") == "auto-provenance")
            assert str(hit.get("status")) == "promoted"
            assert hit.get("source_runs") == ["run-1"], hit.get("source_runs")
            expect = hashlib.sha1(
                json.dumps(hit.get("action_sequence"), sort_keys=True).encode("utf-8")
            ).hexdigest()
            assert hit.get("content_sha1") == expect, "提升时写 content_sha1"
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def check_garden() -> None:
    from agent.garden import garden

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        plain = tmp / "无抬头文档.md"
        plain.write_text("# 正文\n\n内容一行。\n", encoding="utf-8")
        upgrade = tmp / "02-约束层.md"
        upgrade.write_text("# 升级二\n", encoding="utf-8")
        report = garden(scan_dirs=[tmp], report_path=tmp / "garden_report.json")
        text = plain.read_text(encoding="utf-8")
        assert text.startswith("---\n"), "无抬头只补抬头"
        assert "status: draft" in text and "last_updated:" in text and "owner: 侑" in text
        assert "# 正文" in text and "内容一行。" in text, "不改正文"
        up_text = upgrade.read_text(encoding="utf-8")
        assert "status: ready" in up_text, "升级执行文件保持 ready，不下改成 draft"
        assert "# 升级二" in up_text
        assert report["scanned"] == 2 and report["patched"] == 2 and report["stale"] == []
        saved_report = json.loads((tmp / "garden_report.json").read_text(encoding="utf-8"))
        assert saved_report == report


def check_resume_context() -> None:
    from config import DATA_DIR

    progress = json.loads((DATA_DIR / "harness" / "progress.json").read_text(encoding="utf-8"))
    goal = str(progress.get("goal") or "")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "agent" / "resume_context.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert goal[:30] in (proc.stdout or ""), "stdout 必须含 progress.json 里的 goal 子串"


def check_scoreboard() -> None:
    from report import compute_scoreboard

    rows = [
        {"ok": True, "retry_count": 0, "prompt_tokens": 100},
        {"ok": True, "retry_count": 1, "prompt_tokens": 50},
    ]
    board = compute_scoreboard(rows, source="trace 路径")
    assert board["tasks"] == 2 and board["ok"] == 2
    assert board["first_pass"] == 1, "只有 retry 0 的 ok 计首次通过"
    assert board["prompt_tokens_per_ok"] == 75
    assert board["tool_refused"] == 0
    assert board["source"] == "trace 路径"
    refused = compute_scoreboard([
        {"id": "t1", "ok": False, "retry_count": 0, "failure_class": "tool_refused"},
    ])
    assert refused["tool_refused"] == 1 and refused["ok"] == 0
    assert refused["prompt_tokens_per_ok"] is None, "没有 usage 留空，不填 0"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    check_roles()
    check_handoff_goal()
    check_promote_provenance()
    check_garden()
    check_resume_context()
    check_scoreboard()
    print("automation ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
