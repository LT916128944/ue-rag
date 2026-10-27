// dsh/workflows/triage.js —— P4 只读排障模板
// 贴进 DSH `workflow` 工具的 script，args = {error_text}
// 两路子代理各查一处：历史错误（source_type=ue_error）、排障文档（doc_kind=debug），各 top_k=1。
// 证据对不上就返回 root_cause: "unknown"，不编。只读，不改 passed，不碰编辑器。

const errorText = String((args && args.error_text) || "").trim();
if (!errorText) {
  return {
    ok: false,
    root_cause: "unknown",
    evidence_kind: "none",
    next_step: "",
    confidence: 0,
    error: "args.error_text 为空，没有要排的错",
  };
}

const RED = "只读红线：不得调用 exec_editor_tool / verify / window_state；不得写工程文件；不跑 git。";
const LANES = [
  { name: "ue_error", where: { source_type: "ue_error" }, hint: "历史错误记录（同一现象以前怎么解决的）" },
  { name: "debug", where: { doc_kind: "debug" }, hint: "排障文档（这类现象的定位步骤与根因）" },
];

phase("查两处");
const found = await pipeline(LANES, (prev, lane) =>
  agent(
    [
      `排障现象原文：${JSON.stringify(errorText)}`,
      `只做一件事：调用 mcp__uerag__search_project，query 用现象原文，top_k=1，where=${JSON.stringify(lane.where)}。`,
      `这一路查的是：${lane.hint}`,
      "只有检索结果真的对得上这条现象，才给出 root_cause；对不上就 root_cause=\"unknown\"、confidence=0。",
      "evidence 只写命中的标题与路径，不要贴正文。confidence 取 0~1。",
      RED,
    ].join("\n"),
    {
      label: `triage-${lane.name}`,
      phase: "查两处",
      schema: {
        type: "object",
        properties: {
          root_cause: { type: "string" },
          evidence_kind: { type: "string", enum: ["ue_error", "debug", "none"] },
          next_step: { type: "string" },
          confidence: { type: "number" },
          evidence: { type: "string" },
        },
        required: ["root_cause", "evidence_kind", "next_step", "confidence"],
        additionalProperties: false,
      },
    }
  )
);

const lanes = LANES.map((lane, index) => {
  const hit = found[index] || {};
  return {
    lane: lane.name,
    root_cause: String(hit.root_cause || "unknown").slice(0, 200),
    evidence_kind: String(hit.evidence_kind || "none").slice(0, 32),
    next_step: String(hit.next_step || "").slice(0, 240),
    confidence: Math.max(0, Math.min(Number(hit.confidence) || 0, 1)),
    evidence: String(hit.evidence || "").slice(0, 240),
  };
});

const matched = lanes.filter((item) => item.root_cause && item.root_cause !== "unknown");
if (!matched.length) {
  return {
    ok: false,
    root_cause: "unknown",
    evidence_kind: "none",
    next_step: "检索没给出对得上的证据：按域技能只读自查，不要按猜测改代码",
    confidence: 0,
    lanes,
  };
}

matched.sort((a, b) => b.confidence - a.confidence);
const best = matched[0];
return {
  ok: true,
  root_cause: best.root_cause,
  evidence_kind: best.evidence_kind,
  next_step: best.next_step,
  confidence: best.confidence,
  evidence: best.evidence,
  lanes,
};
