# eval/check_info_layer.py
"""升级 1（信息层）离线检查。只做既定断言；成功只打印 info ok，失败打印三行后非 0 退出。"""
import json
import sys
from pathlib import Path

SEE = r"E:\UEProject\AIWork\AIAgentProject\documents\harness\context-tiers.md"
AGENT_MD = Path(r"E:\UEProject\AIWork\AIAgentProject\AGENTS.md")
RULES_DIR = Path(r"E:\UEProject\AIWork\AIAgentProject\.cursor\rules")
HARNESS_DOCS_DIR = Path(r"E:\UEProject\AIWork\AIAgentProject\documents\harness")
WORKFLOW_MD = Path(r"E:\UEProject\AIWork\AIAgentProject\WORKFLOW.md")
EXPORT_SCRIPT = Path(r"E:\UEProject\AIWork\RAG\ue-rag\pipelines\export_docs.py")
DATA_HARNESS = Path(r"E:\UEProject\AIWork\RAG\ue-rag\data\harness")

HARNESS_DOCS = ["overview.md", "boundaries.md", "context-tiers.md", "design-template.md", "scoreboard.md"]
RULE_LIMITS = {
    "ue-rag-route.mdc": 40,
    "ue-history.mdc": 40,
    "ue-cpp-plan.mdc": 40,
    "ue-harness-session.mdc": 25,
}
ALL_IDS = [
    "loop-graph", "tool-contract", "hybrid-retrieval", "context-slice", "harness-contract",
    "patrol-cpp", "cost-log",
    "patrol-presentation", "editor-transaction", "doc-freshness", "architecture-lint",
    "schema-lint", "risk-matrix", "role-allowlist", "memory-provenance", "scoreboard-file",
    "context-switch-drift",
    "rerank", "second-graph", "drop-langgraph", "metrics-stack", "cursor-query-default", "docker",
]
WONT_IDS = ["rerank", "second-graph", "drop-langgraph", "metrics-stack", "cursor-query-default", "docker"]


def fail(msg: str, fix: str) -> None:
    print(f"❌ {msg}")
    print(f"✅ FIX: {fix}")
    print(f"📖 See: {SEE}")
    sys.exit(1)


def main() -> None:
    # AGENTS.md：行数与铁律关键词
    lines = AGENT_MD.read_text(encoding="utf-8").splitlines()
    if len(lines) > 120:
        fail(f"AGENTS.md {len(lines)} 行，超过 120", "AGENTS.md 行数压回 120 行以内（AGENTS.md 全文长度）")
    agents_text = "\n".join(lines)
    for key in ("禁探索", "默认不保存", "语义查询必须 RAG 先行"):
        if key not in agents_text:
            fail(f"AGENTS.md 缺铁律关键词「{key}」", f"AGENTS.md 铁律段补回「{key}」")

    # 四份规则：存在 + 行数上限
    for name, limit in RULE_LIMITS.items():
        path = RULES_DIR / name
        if not path.is_file():
            fail(f"缺 .cursor/rules/{name}", f".cursor/rules/{name} 新建（status: 文件存在）")
        n = len(path.read_text(encoding="utf-8").splitlines())
        if n > limit:
            fail(f"{name} {n} 行，超过 {limit}", f".cursor/rules/{name} 压到 {limit} 行以内")

    # 五份 harness 文档：存在 + 抬头 + 二级标题
    for name in HARNESS_DOCS:
        path = HARNESS_DOCS_DIR / name
        if not path.is_file():
            fail(f"缺 documents/harness/{name}", f"documents/harness/{name} 新建（status: 文件存在）")
        doc = path.read_text(encoding="utf-8")
        if "last_updated:" not in doc:
            fail(f"{name} 缺 last_updated", f"documents/harness/{name} 抬头补 last_updated 字段")
        if "status: active" not in doc:
            fail(f"{name} 缺 status: active", f"documents/harness/{name} 抬头补 status: active")
        if "owner:" not in doc:
            fail(f"{name} 缺 owner", f"documents/harness/{name} 抬头补 owner 字段")
        if "\n## " not in doc:
            fail(f"{name} 没有二级标题，export_docs.py 切不出 chunk", f"documents/harness/{name} 加至少一个「## 」标题")

    # export_docs.py：收集了 harness
    if "harness" not in EXPORT_SCRIPT.read_text(encoding="utf-8"):
        fail("export_docs.py 没有收集 documents/harness", "export_docs.py main() 补 harness 目录的 glob 收集")

    # WORKFLOW.md：指向 feature_list.json
    if "feature_list.json" not in WORKFLOW_MD.read_text(encoding="utf-8"):
        fail("WORKFLOW.md 没有指向 feature_list.json", "WORKFLOW.md 标题下的开工协议指向 feature_list.json")

    # feature_list.json：id 完整、wont 齐全、无重复
    data = json.loads((DATA_HARNESS / "feature_list.json").read_text(encoding="utf-8"))
    items = data["items"]
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        fail("feature_list.json 有重复 id", "feature_list.json items 里 id 去重")
    for ident in ALL_IDS:
        if ident not in ids:
            fail(f"feature_list.json 缺 id「{ident}」", f"feature_list.json items 补 id「{ident}」")
    for ident in WONT_IDS:
        if ident not in ids:
            fail(f"feature_list.json 缺 wont id「{ident}」", f"feature_list.json 补 wont id「{ident}」（status=wont, passed=false, notes=放弃原因）")
    for item in items:
        if item["status"] == "done" and item.get("passed") is not True:
            fail(f"id「{item['id']}」status=done 但 passed 不为 true", f"feature_list.json「{item['id']}」的 passed 改为 true")
        if item["status"] in ("pending", "wont") and item.get("passed") is not False:
            fail(f"id「{item['id']}」status={item['status']} 但 passed 不为 false", f"feature_list.json「{item['id']}」的 passed 改为 false")

    # progress.json：goal / phase / next_action
    prog = json.loads((DATA_HARNESS / "progress.json").read_text(encoding="utf-8"))
    if not prog.get("goal"):
        fail("progress.json 的 goal 为空", "progress.json 的 goal 写回目标句（goal 字段）")
    if prog.get("phase") != "automation":
        fail(f"progress.json phase={prog.get('phase')}，应为 automation", "progress.json 的 phase 改为 automation（升级 3 已落地）")
    if "pending" not in str(prog.get("next_action", "")) or "feature_list" not in str(prog.get("next_action", "")):
        fail("progress.json 的 next_action 未指向 feature_list 的 pending", "progress.json 的 next_action 改为「下一次只从 feature_list 的第一条 pending 做起」")

    # boundaries.md：已知例外
    boundaries = (HARNESS_DOCS_DIR / "boundaries.md").read_text(encoding="utf-8")
    if "pipelines/export_agent_skill.py" not in boundaries:
        fail("boundaries.md 缺已知例外 pipelines/export_agent_skill.py", "boundaries.md 已知例外段补 pipelines/export_agent_skill.py 这条")

    print("info ok")


if __name__ == "__main__":
    main()