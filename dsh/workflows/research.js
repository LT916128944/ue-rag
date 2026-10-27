// dsh/workflows/research.js —— P4 只读并行研究模板
// 贴进 DSH `workflow` 工具的 script，args = {question, top_k=5}
// 只读红线：子代理不得调 exec_editor_tool / verify / window_state，不得写工程文件，不跑 git。
// 预算：5 个子代理（1 检索 + 3 分头读 + 1 汇总），返回 ≤8KB，每路最多读 5 个文件。

const question = String((args && args.question) || "").trim();
const topK = Math.max(1, Math.min(Number((args && args.top_k) || 5) || 5, 10));
if (!question) {
  return { ok: false, error: "args.question 为空，没有要研究的问题" };
}

const RED = "只读红线：不得调用 exec_editor_tool / verify / window_state；不得写 .uasset/.umap/Source/Content；不跑 git。";
const LANES = [
  { name: "cpp", focus: "C++ 类与源码（Source 下的 .h/.cpp、类名、函数、编译错误）" },
  { name: "asset", focus: "蓝图与资产（/Game 路径、蓝图类名、组件、关卡里的实例）" },
  { name: "doc", focus: "文档与历史排障（Plan/、documents/、历史错误记录）" },
];

phase("检索");
const hits = await agent(
  [
    `只读检索，不改任何文件。调用 mcp__uerag__search_project，query=${JSON.stringify(question)}，top_k=${topK}。`,
    "只回报候选：标题、路径、类名（有就给），最多 10 条，不要贴正文。",
    RED,
  ].join("\n"),
  {
    label: "search",
    phase: "检索",
    schema: {
      type: "object",
      properties: {
        items: {
          type: "array",
          items: {
            type: "object",
            properties: { title: { type: "string" }, path: { type: "string" }, class_name: { type: "string" } },
            required: ["title"],
            additionalProperties: false,
          },
        },
      },
      required: ["items"],
      additionalProperties: false,
    },
  }
);
const seed = hits && Array.isArray(hits.items) ? hits.items.slice(0, 10) : [];

phase("分头读");
const read = await pipeline(LANES, (prev, lane) =>
  agent(
    [
      `问题：${question}`,
      `候选（可能为空，别全信）：${JSON.stringify(seed)}`,
      `你只负责这一路：${lane.focus}`,
      "最多读 5 个文件，只读需要的片段，不要把整份文件塞进上下文。",
      "结论 ≤500 字，固定四行：命中 / 结论 / 依据（文件:行）/ 未解。没有就写没有，不编。",
      RED,
    ].join("\n"),
    { label: `read-${lane.name}`, phase: "分头读" }
  )
);
const lanes = LANES.map((lane, index) => ({ lane: lane.name, note: String(read[index] || "").trim() }))
  .filter((item) => item.note)
  .map((item) => ({ lane: item.lane, note: item.note.slice(0, 1200) }));

phase("汇总");
const brief = await agent(
  [
    "把三路只读结论汇总成一份任务书，并落盘两个文件：",
    "- data/briefs/<YYYYMMDD-HHMMSS>-<slug>.md（≤120 行；slug 取问题里的英文短名，时间用当前本地时间）",
    "- data/briefs/<同名>.json，字段 {goal, findings[], files[], unresolved[], next}",
    `问题：${question}`,
    `三路结论：${JSON.stringify(lanes)}`,
    "只汇总，不新增事实；对不上的进 unresolved。写完回报两个路径与 md 行数。",
    RED,
  ].join("\n"),
  {
    label: "brief",
    phase: "汇总",
    schema: {
      type: "object",
      properties: {
        md: { type: "string" },
        json: { type: "string" },
        lines: { type: "number" },
        unresolved: { type: "array", items: { type: "string" } },
      },
      required: ["md", "json"],
      additionalProperties: false,
    },
  }
);

return {
  ok: Boolean(brief && brief.md),
  question,
  agents: 1 + LANES.length + 1,
  seed: seed.length,
  lanes,
  brief: brief || null,
};
