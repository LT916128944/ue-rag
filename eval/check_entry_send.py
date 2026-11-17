# eval/check_entry_send.py
"""S-1（20261116 入口页「输入指令卡死」修复）：发送按钮的 click 接线。

背景（真 bug，不是假想）：`server/static/entry.html` 里 `send(resume)` 的第一个形参是
`resume`，而启动时写的是

    $("btn-send").addEventListener("click", send);

浏览器会把 click 的 MouseEvent 当成第一个实参传进 `send`。MouseEvent 恒为真值，于是
**点「发送」走的其实是续接分支**，真正发给 CLI 的是 `:resume latest`，输入框里的指令
根本没发出去。没有可续接的 run 时子进程立刻退出、不落新 run 记录，页面等 12 秒后报
「没等到新 run 记录」——这就是用户说的「输入指令卡死」。证据：请求文件
`data/ui/req/20261116-190224.txt.consumed` 里是 `:resume latest`，而输入框里是点光源指令。

这里钉两件事（只读静态页，全离线，不跑浏览器）：

1. `send` 必须把 `resume` 收成严格布尔：函数开头出现 `resume = resume === true`，
   以后谁再传进来一个事件对象也不会误判成续接。
2. `btn-send` 的 click 监听器不许是裸 `send`（那会把事件当 resume）；必须显式包一层。
"""
from __future__ import annotations

import re
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


def check_send_coerces_resume(text: str) -> str:
    """1) `send(resume)` 必须把 resume 收成严格布尔。"""
    head = re.search(r"function\s+send\s*\(\s*resume\s*\)\s*\{", text)
    check(head is not None, "找不到 function send(resume)：入口页结构变了，检查需同步")
    window = text[head.end():head.end() + 600]
    check(
        re.search(r"resume\s*=\s*resume\s*===\s*true", window) is not None,
        "send() 开头必须写 `resume = resume === true`，否则事件对象会被当成续接",
    )
    return "send(resume) 把 resume 收成严格布尔（事件对象不会误判为续接）"


def check_send_not_bound_raw(text: str) -> str:
    """2) `btn-send` 不许直接挂 `send`（会把 MouseEvent 当 resume）。"""
    buggy = re.search(
        r"addEventListener\(\s*[\"']click[\"']\s*,\s*send\s*\)", text
    )
    check(
        buggy is None,
        "发现 `addEventListener(\"click\", send)`：click 的 MouseEvent 会被当成 resume，"
        "点发送实际发 :resume latest。必须包一层，如 `function () { send(false); }`。",
    )
    check("send(false)" in text, "找不到 `send(false)`：发送按钮没显式走「发指令」分支")
    check("send(true)" in text, "找不到 `send(true)`：续接按钮接线丢了")
    return "btn-send 不裸挂 send；发送走 send(false)、续接走 send(true)"


def main() -> int:
    try:
        text = _page_text()
        rows = [check_send_coerces_resume(text), check_send_not_bound_raw(text)]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print("  " + row)
    print("entry send ok（发送按钮不会误走续接）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
