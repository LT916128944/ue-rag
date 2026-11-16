# eval/check_observer_labels.py
"""O-1 / O-2（20261116-1530 HTML 入口层计划 §8）：观测层的两张表不许各说各话。

O-1  `server/static/entry.html` 的 `BOARD_STATUS_TABLE` 键集与值必须与
     `agent/statuses.py::WINDOW_STATE_LABELS`（statuses.py:91）逐项一致。
     理由：窗口状态是外部数据（旧 run 记录、人工改过的任务书），渲染层按 .get 回落；
     但**词表本身**只能有一份真值源，否则加一个状态就有一半界面显示英文键。

O-2  `server/entry.py::_runs_day` 与 `agent/run_windows.py::_runs_day` 逐样本一致。
     入口层不能 import agent（架构白名单只有两条），所以分桶规则在 entry 里重写了 3 行；
     这 3 行必须与 agent 侧同口径，否则 UI 按 run_id 拼出来的路径会找不到真记录。

另钉一条**结构性**断言：入口页不许出现 `localStorage` / `XMLHttpRequest` /
外链 `http(s)`（离线可用、不写浏览器存储、不引 CDN）。

不连编辑器、不调模型、不联网。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _page() -> Path:
    """入口页的真实路径，**问服务端要**而不是在这里再拼一次。

    页面 20261116 迁出到 `AIAgentProject/entry.html`，随后搬回本仓库
    `server/static/entry.html`。路径只在 `server.entry._page()` 里有一份；这里若自己拼
    `ROOT/server/static/...`，迁移时这条检查会去读一个不存在的文件，红的原因就和被测的
    事无关了。
    """
    from server.entry import _page as entry_page

    return entry_page()


PAGE = _page()
MARK_BEGIN = "/* BOARD_STATUS_TABLE:BEGIN"
MARK_END = "/* BOARD_STATUS_TABLE:END */"

# O-2 的样本：正常 run_id、跨日、外来 id、缺日期、纯日期。逐样本比，不比实现。
DAY_SAMPLES = [
    "20261116-153000",
    "20261029-090000",
    "20261116-000000",
    "imported-id",
    "2026111-153000",
    "20261116",
]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _js_table() -> dict[str, str]:
    text = PAGE.read_text(encoding="utf-8")
    at = text.find(MARK_BEGIN)
    check(at >= 0, f"{PAGE.name} 里没有 `{MARK_BEGIN}` 标记——状态表必须有可定位的边界")
    end = text.find(MARK_END, at)
    check(end > at, f"{PAGE.name} 里没有 `{MARK_END}` 标记（成对才可解析）")
    block = text[at:end]
    body = block[block.find("{") + 1:]
    table: dict[str, str] = {}
    for key, value in re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"([^"]*)"', body):
        table[key] = value
    return table


def check_o1() -> str:
    from agent.statuses import WINDOW_STATE_LABELS

    js = _js_table()
    py = dict(WINDOW_STATE_LABELS)
    check(bool(js), "BOARD_STATUS_TABLE 没解析出任何键值")
    check(
        set(js) == set(py),
        f"O-1 失败：键集不一致——页面多 {sorted(set(js) - set(py))}，页面缺 {sorted(set(py) - set(js))}",
    )
    diff = {key: (js[key], py[key]) for key in py if js.get(key) != py[key]}
    check(not diff, f"O-1 失败：标签不一致 {diff}")
    return f"  BOARD_STATUS_TABLE == WINDOW_STATE_LABELS（{len(py)} 键逐项一致）"


def check_o2() -> str:
    from agent.run_windows import _runs_day as agent_day
    from server.entry import _runs_day as entry_day

    bad = []
    for run_id in DAY_SAMPLES:
        left, right = entry_day(run_id), agent_day(run_id)
        if left != right:
            bad.append(f"{run_id}: entry={left!r} agent={right!r}")
    check(not bad, "O-2 失败：_runs_day 分桶不一致——" + "；".join(bad))
    return f"  entry._runs_day == run_windows._runs_day（{len(DAY_SAMPLES)} 个样本）"


def check_page_structure() -> str:
    text = PAGE.read_text(encoding="utf-8")
    for token in ("localStorage", "sessionStorage", "XMLHttpRequest"):
        check(token not in text, f"入口页不许用 {token}（离线可用、不写浏览器存储）")
    # 外链脚本/样式/CDN：允许 SVG 的 xmlns 命名空间与本机自述地址，其余 http(s) 一律不许。
    externals = [
        hit for hit in re.findall(r'https?://[^\s"\'<>]+', text)
        if "www.w3.org/2000/svg" not in hit
        and not re.match(r"^https?://(127\.0\.0\.1|localhost)([:/]|$)", hit)
    ]
    check(not externals, f"入口页不许有外链资源：{externals[:3]}")
    # 装配只有一份实现：页面不许再写一遍 build()。
    check(
        "function build(" not in text,
        "入口页不许自带装配器：装配只走 server/prompt_builder.py（POST /api/preview）",
    )
    check("/api/preview" in text, "入口页必须通过 /api/preview 取装配结果")
    # 写锁缺省文案：没有 editor_gate 时显示「—」，不是「空闲」。
    check(
        re.search(r'写锁 <b>—</b>', text) is not None,
        "写锁缺省文案必须是「—」：没有 editor_gate 事件时不许显示「空闲」",
    )
    return "  无 localStorage/XHR/外链，装配走 /api/preview，写锁缺省为「—」"


def main() -> int:
    try:
        rows = [check_o1(), check_o2(), check_page_structure()]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print(row)
    print("observer labels ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
