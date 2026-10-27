# eval/check_archive_fidelity.py
"""T5 离线验收（20261024-192858）：归档证据不许被第二次归档覆盖成空。

真事：agent/run_windows.py 的 `_run_integrate` 先带 hashes/matrix 归档一次（integrate），
收尾为了把最终 md 复制进归档又调了一次 `_archive_run(state, md_path=path)`，不带证据；
旧实现 `payload = {"hashes": hashes or [], "matrix": matrix or []}` 把已经落好的证据清成空。
实测 data/archive/20261024-192858/deliverables.json 现在就是 {"hashes":[],"matrix":[]}，
而同一 run 的 data/runs/20261024/20261024-192858.json 里 W2 的 matrix 有 5 项。

本用例钉四件事（外加一条端到端）：
1) 带证据归档后再用 md_path 归档一次，matrix/hashes 不被清空（written_by 换成 finalize）；
2) 归档件 matrix 长度 == 真实 run json 里集成扇 detail.matrix 的长度（真实 run json 只读）；
3) 幂等：同样参数连续归档两次结果一致；老归档件（没 written_by）与非对象坏文件读回不炸；
4) 反例守卫：显式传 hashes=[]/matrix=[] 是「明确给空」，不被合并逻辑吃掉；
5) 端到端：假 execute 真跑一次 run，归档件的 matrix 必须等于 run json 集成扇的 matrix。

不连编辑器、不调模型：run_task 是替身；RUNS_DIR/EVENTS_DIR/ARCHIVE_DIR 用临时目录隔离。
data/archive/20261024-192858/ 下的既有文件一个字节都不许动（历史证据，见红线断言）。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent
REAL_RUN_JSON = REPO / "data" / "runs" / "20261024" / "20261024-192858.json"
REAL_ARCHIVE_FILE = REPO / "data" / "archive" / "20261024-192858" / "deliverables.json"


def _quiet(*_args, **_kwargs):
    return None


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _make_state(run_id: str):
    from agent.run_windows import _RunState

    return _RunState(run_id, "归档保真", "brief", [{
        "id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
        "depends_on": [], "max_retries": 1,
    }])


def _write_md(tmp: Path, run_id: str) -> Path:
    path = tmp / f"{run_id}.md"
    path.write_text(f"# {run_id}\n\n## 集成\n- 状态：部分完成\n", encoding="utf-8")
    return path


def check_second_archive_keeps_evidence(tmp: Path) -> None:
    """集成归档后再用 md_path 收尾归档：证据留着，md 照复制。"""
    from agent.run_windows import _archive_run

    state = _make_state("20260901-000001")
    matrix = [
        {"item": "重复请求幂等", "ok": None, "note": "没有可复验的只读裁决，本次不适用"},
        {"item": "拒答路径", "ok": True, "note": "W3"},
    ]
    hashes = [{"path": "Source/AIAgentProject/TestPawn.cpp", "sha256": "ab" * 32, "note": "已修改"}]
    folder = _archive_run(state, hashes=hashes, matrix=matrix, written_by="integrate")
    first = _read_json(folder / "deliverables.json")
    assert first["matrix"] == matrix, first
    assert first["hashes"] == hashes, first
    assert first["written_by"] == "integrate", first

    md = _write_md(tmp, state.run_id)
    _archive_run(state, md_path=md, written_by="finalize")
    second = _read_json(folder / "deliverables.json")
    assert second["matrix"] == matrix, ("收尾归档把矩阵清了", second)
    assert second["hashes"] == hashes, ("收尾归档把哈希清了", second)
    assert second["written_by"] == "finalize", second
    assert (folder / md.name).is_file(), "最终 md 仍要复制进归档"


def check_explicit_empty_allowed(tmp: Path) -> None:
    """显式传空是「明确要给空」，不许被合并逻辑吃掉。"""
    from agent.run_windows import _archive_run

    state = _make_state("20260901-000002")
    matrix = [{"item": "未知态恢复", "ok": True, "note": "每扇都有明确终态"}]
    hashes = [{"path": "Source/x.cpp", "sha256": "0" * 64, "note": "已修改"}]
    folder = _archive_run(state, hashes=hashes, matrix=matrix, written_by="integrate")
    assert _read_json(folder / "deliverables.json")["matrix"] == matrix
    _archive_run(state, hashes=[], matrix=[], written_by="finalize")
    payload = _read_json(folder / "deliverables.json")
    assert payload["hashes"] == [] and payload["matrix"] == [], ("显式给空被合并逻辑吃掉了", payload)


def check_idempotent_and_legacy(tmp: Path) -> None:
    """同样参数两次归档结果一致；老归档件与坏文件读回都不炸。"""
    from agent.run_windows import _archive_run

    state = _make_state("20260901-000003")
    md = _write_md(tmp, state.run_id)
    matrix = [{"item": "延迟窗口", "ok": None, "note": "本次没有重试，不适用"}]
    hashes = [{"path": "Source/y.cpp", "sha256": "1" * 64, "note": "已修改"}]
    folder = _archive_run(state, hashes=hashes, matrix=matrix, md_path=md, written_by="finalize")
    target = folder / "deliverables.json"
    first = target.read_text(encoding="utf-8")
    _archive_run(state, hashes=hashes, matrix=matrix, md_path=md, written_by="finalize")
    second = target.read_text(encoding="utf-8")
    assert first == second, "同样参数归档两次结果不一致"

    # 老归档件：旧形状 {run_id, hashes, matrix}，没有 written_by
    target.write_text(
        json.dumps({"run_id": state.run_id, "hashes": hashes, "matrix": matrix},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _archive_run(state, md_path=md, written_by="finalize")
    payload = _read_json(target)
    assert payload["matrix"] == matrix and payload["hashes"] == hashes, ("老归档件丢证据", payload)
    assert payload["written_by"] == "finalize", payload

    # 坏归档件：不是 JSON 对象，也不许把归档整段打断
    target.write_text("[]", encoding="utf-8")
    _archive_run(state, md_path=md, written_by="finalize")
    payload = _read_json(target)
    assert payload["matrix"] == [] and payload["hashes"] == [], payload


def check_real_run_matrix(tmp: Path) -> None:
    """真实 run 对照：归档件 matrix 长度 == run json 集成扇 detail.matrix 长度。只读 run json。"""
    from agent.run_windows import _archive_run

    if not REAL_RUN_JSON.is_file():
        # data/runs 是 gitignore 的，CI 上没有这份历史 run：跳过并说出来，不假装验过。
        # 真实链条由下面的端到端用例兜底（那条不依赖 data/）。
        print("真实 run json 不在（data/runs 是 gitignore 的），真实对照跳过")
        return
    run = _read_json(REAL_RUN_JSON)
    detail = ((run.get("windows") or {}).get("W2") or {}).get("detail") or {}
    run_matrix = [item for item in (detail.get("matrix") or []) if isinstance(item, dict)]
    run_hashes = [item for item in (detail.get("hashes") or []) if isinstance(item, dict)]
    assert run_matrix, "这条用例的前提是真实 run 的集成矩阵非空（旧归档件正是被清空的那个）"

    # 同名 run 只写临时 ARCHIVE_DIR（main 已隔离），真归档不动
    state = _make_state(str(run.get("run_id") or "20261024-192858"))
    md = _write_md(tmp, state.run_id)
    folder = _archive_run(state, hashes=run_hashes, matrix=run_matrix, written_by="integrate")
    _archive_run(state, md_path=md, written_by="finalize")
    archived = _read_json(folder / "deliverables.json")
    assert len(archived["matrix"]) == len(run_matrix), (
        "归档件 matrix 长度和 run json 集成扇对不上", len(archived["matrix"]), len(run_matrix)
    )
    assert [item.get("item") for item in archived["matrix"]] == [
        item.get("item") for item in run_matrix
    ], archived
    assert len(archived["hashes"]) == len(run_hashes), (len(archived["hashes"]), len(run_hashes))


def check_end_to_end_archive() -> None:
    """假 execute 真跑一次 run：归档件 matrix 必须等于 run json 集成扇的 matrix。"""
    from agent import run_task as run_task_mod
    from agent import run_windows as rw
    from agent.brief import materialize_scopes

    brief = {
        "query": "只读裁决 BP_TestActor 能否拾取",
        "scopes": [{
            "id": "A", "title": "只读裁决", "deliverables": ["裁决能否拾取"], "pending_note": "",
            "depends_on": [], "external_dep": "",
            "windows": [{"id": "W1", "title": "裁决", "action": "judge_pickup",
                         "slots": {"target": "BP_TestActor", "picker": "BP_TestPawn"},
                         "depends_on": [], "max_retries": 1}],
        }],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["只读复验"]},
        "external_dependencies": [],
    }
    windows, scopes = materialize_scopes(brief)

    def fake(task, trace_path=None, thread_id=None, on_row=None):
        return {
            "id": task.get("id"), "ok": True, "summary": "唯一命中，距离 90 uu",
            "judge": {"ok": True, "distance": 90.0},
            "test": {"ok": True, "kind": "editor_assert", "summary": "唯一命中", "errors": []},
            "thread_id": thread_id,
        }

    original = run_task_mod.run_task
    run_task_mod.run_task = fake
    try:
        code, path = rw.execute_windows(
            brief["query"], planned={"source": "brief"}, windows=windows, brief=brief,
            scopes=scopes, echo=_quiet,
        )
    finally:
        run_task_mod.run_task = original
    assert code == 0, "这条正常范围应当成功（不然不是同一条归档链）"
    run = _read_json(path.with_suffix(".json"))
    integrate_id = windows[-1]["id"]
    entry = run["windows"][integrate_id]
    run_matrix = (entry.get("detail") or {}).get("matrix") or []
    assert run_matrix, "集成矩阵不该是空的"
    assert len([one for one in run_matrix if one.get("ok") is not None]) >= 1, run_matrix

    folder = rw.ARCHIVE_DIR / path.stem
    archived = _read_json(folder / "deliverables.json")
    assert len(archived["matrix"]) == len(run_matrix), ("端到端归档丢了集成矩阵", archived)
    assert [item.get("item") for item in archived["matrix"]] == [
        item.get("item") for item in run_matrix
    ], archived
    assert archived.get("written_by") == "finalize", archived
    assert (folder / f"{path.stem}.md").is_file(), "最终 md 要进归档"


def main() -> int:
    from agent import brief as brief_mod
    from agent import ledger as ledger_mod
    from agent import run_windows as rw
    from agent import snapshot as snapshot_mod

    real_before = REAL_ARCHIVE_FILE.read_bytes() if REAL_ARCHIVE_FILE.is_file() else None
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        saved = {
            "runs": rw.RUNS_DIR, "events": rw.EVENTS_DIR, "archive": rw.ARCHIVE_DIR,
            "src": rw.UE_SOURCE_ROOT, "project": rw.UE_PROJECT_DIR,
            "snap_src": snapshot_mod.UE_SOURCE_ROOT, "snaps": snapshot_mod.SNAPSHOTS_DIR,
            "briefs": brief_mod.BRIEFS_DIR, "ledger": ledger_mod.LEDGER_PATH,
        }
        prev_candidates = os.environ.get("UE_ACTION_CANDIDATES")
        prev_routes = os.environ.get("UE_LEARNED_ROUTES")
        os.environ["UE_ACTION_CANDIDATES"] = str(tmp / "action_candidates.json")
        os.environ["UE_LEARNED_ROUTES"] = str(tmp / "learned_routes.json")
        try:
            rw.RUNS_DIR = tmp / "runs"
            rw.EVENTS_DIR = tmp / "events"
            rw.ARCHIVE_DIR = tmp / "archive"
            rw.UE_SOURCE_ROOT = tmp / "source"
            rw.UE_PROJECT_DIR = tmp
            snapshot_mod.SNAPSHOTS_DIR = tmp / "snapshots"
            snapshot_mod.UE_SOURCE_ROOT = tmp / "source"
            brief_mod.BRIEFS_DIR = tmp / "briefs"
            ledger_mod.LEDGER_PATH = tmp / "ledger.json"

            check_second_archive_keeps_evidence(tmp)
            check_explicit_empty_allowed(tmp)
            check_idempotent_and_legacy(tmp)
            check_real_run_matrix(tmp)
            check_end_to_end_archive()
        finally:
            rw.RUNS_DIR = saved["runs"]
            rw.EVENTS_DIR = saved["events"]
            rw.ARCHIVE_DIR = saved["archive"]
            rw.UE_SOURCE_ROOT = saved["src"]
            rw.UE_PROJECT_DIR = saved["project"]
            snapshot_mod.UE_SOURCE_ROOT = saved["snap_src"]
            snapshot_mod.SNAPSHOTS_DIR = saved["snaps"]
            brief_mod.BRIEFS_DIR = saved["briefs"]
            ledger_mod.LEDGER_PATH = saved["ledger"]
            for key, value in (("UE_ACTION_CANDIDATES", prev_candidates),
                               ("UE_LEARNED_ROUTES", prev_routes)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    # 红线：data/archive/20261024-192858/ 下既有文件一个字节都不许动
    if real_before is not None:
        assert REAL_ARCHIVE_FILE.is_file(), "真实归档件被删了"
        assert REAL_ARCHIVE_FILE.read_bytes() == real_before, "真实归档件被改了（历史证据只读）"
    print("archive fidelity ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())