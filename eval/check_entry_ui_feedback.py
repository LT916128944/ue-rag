# eval/check_entry_ui_feedback.py
"""入口页「不像卡死」的三件反馈（20261117 用户反馈）：预估倒计时 + 中央后台日志 + 规划期占位。

真事：任务卡在「出任务书」阶段时，右侧窗口图还没有（窗口要等任务书人审后才物化），
`/api/runs` 里这条 run 的 windows 是 0，页面上一片空白、只有「进行中」，用户以为卡死。
修法（纯投影层，全部只读）：
1. 顶栏加「用时 / 预估剩余」倒计时（`#g-eta` + `updateClock`），至少让「它还在动」可见；
2. 中央区域底部铺后台日志尾巴（`#logpanel` / `#logbody` + `fetch /api/uilog/<run_id>`），
   左对齐、可跟随，像 DSH 的轨迹——「正在写任务书 / 等待人审」这些阶段看得见；
3. 窗口还没拆出来时，右侧画一张「规划中」占位卡（`renderPlanning` / `.plan-card`），
   而不是空白；运行状态文案按后端日志推阶段（`derivePhase`）。

本检查只读静态页 + server/entry.py 的源码，全离线：不起服务、不跑浏览器、不连编辑器。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _page_text() -> str:
    from server import entry

    page = entry._page()
    check(page.is_file(), f"缺入口页：{page}")
    return page.read_text(encoding="utf-8")


def check_log_panel(text: str) -> str:
    for token in ('id="logpanel"', 'id="logbody"', 'id="logphase"', 'id="logauto"'):
        check(token in text, f"中央后台日志缺 {token}：规划期用户看不到「在写任务书」")
    check("fetch" in text and "/api/uilog/" in text, "页面没有拉取 /api/uilog/（后台日志尾巴）的接线")
    check("function pollUiLog" in text, "缺 pollUiLog：后台日志不会刷新")
    return "中央底部有后台日志面板，轮询 /api/uilog 左对齐显示"


def check_countdown(text: str) -> str:
    check('id="g-eta"' in text, "顶栏缺 #g-eta：没有「用时 / 预估剩余」倒计时")
    check("function updateClock" in text, "缺 updateClock：倒计时不会走")
    check("预估剩余" in text, "倒计时文案缺「预估剩余」")
    return "顶栏有预估倒计时（#g-eta + updateClock）"


def check_planning_placeholder(text: str) -> str:
    check("function renderPlanning" in text, "缺 renderPlanning：规划期右侧会空白")
    check(".plan-card" in text or "plan-card" in text, "缺规划期占位卡样式/节点")
    check("function derivePhase" in text, "缺 derivePhase：规划期只能说「进行中」")
    check("规划中" in text, "规划期文案缺「规划中」")
    return "窗口未拆出时右侧显示「规划中」占位卡，阶段由后台日志推断"


def check_done_card_not_marked_incomplete(text: str) -> str:
    """成功扇不许标「未完成」：交接 unmet 是整句合同里还没被下游扇证明的条款，
    不是这一扇没做到。20261117-185234 的 4/4 全绿 run 每张卡都挂「未完成 N/N」，
    看着像没做完。cardEl 的未完成清单只许在非 done 扇上出现。"""
    check('st !== "done" && win.unmet' in text or "st !== 'done' && win.unmet" in text,
          "cardEl 仍无条件把 handoff.unmet 显示成「未完成」：全绿 run 会被画成没做完")
    check('win.state !== "done"' in text,
          "showWindowLog 仍会给成功扇追加「未完成」")
    return "成功扇(done)不再显示「未完成 N/N」，unmet 只在未完成的扇上出现"


def check_server_route() -> str:
    import server.entry as entry

    src = Path(entry.__file__).read_text(encoding="utf-8")
    check('@app.get("/api/uilog/{run_id}")' in src, "server/entry.py 缺 /api/uilog/{run_id} 路由")
    check("data" in src and "logs" in src, "路由没有落到 data/ui/logs")
    return "server/entry.py 提供只读的 /api/uilog/{run_id}（tail data/ui/logs）"


def main() -> int:
    try:
        text = _page_text()
        rows = [
            check_log_panel(text),
            check_countdown(text),
            check_planning_placeholder(text),
            check_done_card_not_marked_incomplete(text),
            check_server_route(),
        ]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print("  " + row)
    print("entry ui feedback ok（倒计时 + 中央后台日志 + 规划期占位）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
