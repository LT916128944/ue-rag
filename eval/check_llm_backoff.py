"""模型调用的错误分类与有限退避（升级计划第二节残差 5）的离线断言。

真机症状：供应商偶发 429 限流与 5xx 抖动，`complete_logged` 一次就放弃，整句落到
暂停窗；401 失效密钥时也是同一形状，看不出该换键还是该等。这里钉住五件事：

1. 429 退避后成功 —— 第二次拿到响应，attempts=2，record 不留 error_kind；
2. 401 一次就停 —— 不重试、不 sleep，error_kind=auth，报错文案提示换 DEEPSEEK_API_KEY；
3. 5xx 重试用满上限后诚实失败 —— 请求次数 = MAX_RETRIES+1，返回 None，error_kind=server；
4. 落盘的 costs 行带 error_kind 与 attempts，成功路径 attempts=1 且 error_kind 为 None；
5. usage_for_trace 把 llm_error_kind / llm_attempts 带进 trace。

全离线：`openai.OpenAI` 整个换成假客户端，`_sleep` 换成记录器（否则检查会真睡住），
costs 落在临时目录。没有任何网络请求，也不需要真 key。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAKE_KEY = "sk-offline-backoff-check"


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Usage:
    def __init__(self) -> None:
        self.prompt_tokens = 11
        self.completion_tokens = 3


class _Resp:
    """假响应：形状与 openai 的 chat.completion 一致到 llm_call 用到的字段。"""

    def __init__(self, content: str = "ok") -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()


def _status_error(status: int, message: str = "boom"):
    """按状态码造一个真的 openai 异常（纯本地构造，不走网络）。"""
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(status, request=request)
    classes = {
        401: openai.AuthenticationError,
        403: openai.PermissionDeniedError,
        429: openai.RateLimitError,
        500: openai.InternalServerError,
        503: openai.InternalServerError,
        502: openai.InternalServerError,
    }
    return classes[status](message, response=response, body=None)


class _FakeCreate:
    """按队列吐异常或响应；队列空了就重复最后一项。每次调用都计数。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        return item


class _FakeClient:
    """替身 OpenAI：构造时不读 key、不建连接。"""

    def __init__(self, *args, **kwargs):
        self.created_kwargs = kwargs
        self.chat = type("_Chat", (), {})()
        self.chat.completions = type("_Completions", (), {})()


def _install(create_fn):
    """把 `openai.OpenAI` 整个换成替身，create 挂到构造出来的客户端上。

    `complete_logged` 是在函数体内 `from openai import OpenAI` 的，所以替换模块属性
    就够——检查进程里不会再有真客户端被建出来。返回替身类，供 _no_network 断言。
    """
    import openai

    class _Client(_FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.chat.completions.create = create_fn

    openai.OpenAI = _Client
    return _Client


def _no_network(fake_cls) -> None:
    """确认真客户端已经被换掉：检查里除了替身不该有人建 socket/读 key。"""
    import openai

    assert openai.OpenAI is fake_cls, "openai.OpenAI 必须已被假客户端替换，否则会打真接口"


def _run(script: list, *, tmp_costs: Path, env_key: str | None = FAKE_KEY,
         caller: str = "check"):
    """跑一次 complete_logged：假客户端 + 记录 sleep + 临时 costs。返回结果包。"""
    import core.llm_call as llm_mod

    create = _FakeCreate(script)
    fake_cls = _install(create)
    _no_network(fake_cls)

    sleeps: list[float] = []
    saved_sleep = llm_mod._sleep
    saved_costs = llm_mod.COSTS_PATH
    saved_key = os.environ.get("DEEPSEEK_API_KEY")
    llm_mod._sleep = lambda seconds: sleeps.append(seconds)
    llm_mod.COSTS_PATH = tmp_costs
    if env_key is None:
        os.environ.pop("DEEPSEEK_API_KEY", None)
    else:
        os.environ["DEEPSEEK_API_KEY"] = env_key
    try:
        response, record = llm_mod.complete_logged([{"role": "user", "content": "hi"}], caller=caller)
    finally:
        llm_mod._sleep = saved_sleep
        llm_mod.COSTS_PATH = saved_costs
        if saved_key is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = saved_key
    rows = []
    if tmp_costs.is_file():
        rows = [json.loads(line) for line in tmp_costs.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {"response": response, "record": record, "sleeps": sleeps,
            "calls": create.calls, "rows": rows}


def _costs(tmp: Path, name: str) -> Path:
    return tmp / f"{name}.costs.jsonl"


def check_rate_limit_retries_then_succeeds(tmp: Path) -> None:
    """429 → 退避一次 → 第二次成功。"""
    import core.llm_call as llm_mod

    out = _run([_status_error(429, "rate limited"), _Resp("recovered")],
               tmp_costs=_costs(tmp, "rate"))
    record = out["record"]
    assert out["response"] is not None, record
    assert out["response"].choices[0].message.content == "recovered"
    assert out["calls"] == 2, f"429 应该重试一次（实到 {out['calls']} 次）"
    assert record["attempts"] == 2, record
    assert record["error"] is None, record
    assert record["error_kind"] is None, f"成功后不该留 error_kind: {record}"
    assert len(out["sleeps"]) == 1, f"只该睡一次: {out['sleeps']}"
    delay = out["sleeps"][0]
    assert 0 < delay <= llm_mod.BACKOFF_MAX_SECONDS, f"退避秒数不合理: {delay}"
    # 退避是指数 + 抖动：第 1 次重试落在基准 ±25% 内
    low = llm_mod.BACKOFF_BASE_SECONDS * (1 - llm_mod.BACKOFF_JITTER)
    high = llm_mod.BACKOFF_BASE_SECONDS * (1 + llm_mod.BACKOFF_JITTER)
    assert low - 1e-6 <= delay <= high + 1e-6, f"第 1 次退避应在 {low}~{high}，实际 {delay}"
    assert len(out["rows"]) == 1, f"整次调用只落一行 costs: {out['rows']}"
    assert out["rows"][0]["attempts"] == 2, out["rows"][0]
    assert out["rows"][0]["error_kind"] is None, out["rows"][0]


def check_auth_fails_immediately(tmp: Path) -> None:
    """401 不重试、不 sleep，文案提示换键。"""
    out = _run([_status_error(401, "invalid key")], tmp_costs=_costs(tmp, "auth"))
    record = out["record"]
    assert out["response"] is None
    assert out["calls"] == 1, f"401 只许请求一次（实到 {out['calls']} 次）"
    assert record["attempts"] == 1, record
    assert record["error_kind"] == "auth", record
    assert out["sleeps"] == [], f"401 不该退避: {out['sleeps']}"
    assert "DEEPSEEK_API_KEY" in str(record["error"]), f"401 文案要提示换键: {record['error']}"
    assert out["rows"][0]["error_kind"] == "auth", out["rows"][0]
    assert out["rows"][0]["attempts"] == 1, out["rows"][0]


def check_server_error_exhausts_retries(tmp: Path) -> None:
    """5xx 一直抖：用满上限后诚实失败，不假成功。"""
    import core.llm_call as llm_mod

    out = _run([_status_error(503, "upstream down")], tmp_costs=_costs(tmp, "server"))
    record = out["record"]
    assert out["response"] is None, "5xx 用满上限后必须返回 None"
    expected_calls = llm_mod.MAX_RETRIES + 1
    assert out["calls"] == expected_calls, f"应请求 {expected_calls} 次（实到 {out['calls']}）"
    assert record["attempts"] == expected_calls, record
    assert record["error_kind"] == "server", record
    assert record["error"], "失败必须留错误原文"
    assert len(out["sleeps"]) == llm_mod.MAX_RETRIES, f"每次重试前睡一次: {out['sleeps']}"
    # 指数增长：后一次不比前一次短
    assert out["sleeps"] == sorted(out["sleeps"]), f"退避应递增: {out['sleeps']}"
    assert all(0 < s <= llm_mod.BACKOFF_MAX_SECONDS for s in out["sleeps"]), out["sleeps"]
    assert len(out["rows"]) == 1, f"重试不该多落 costs 行: {out['rows']}"
    assert out["rows"][0]["attempts"] == expected_calls, out["rows"][0]


def check_success_has_no_extra_attempts(tmp: Path) -> None:
    """一次就成：attempts=1、不 sleep、error_kind 为 None。"""
    out = _run([_Resp("first try")], tmp_costs=_costs(tmp, "ok"))
    record = out["record"]
    assert out["response"] is not None
    assert out["calls"] == 1, f"成功路径只该请求一次（实到 {out['calls']}）"
    assert record["attempts"] == 1, record
    assert record["error_kind"] is None, record
    assert record["error"] is None, record
    assert out["sleeps"] == [], f"成功路径不该退避: {out['sleeps']}"
    row = out["rows"][0]
    assert row["attempts"] == 1 and row["error_kind"] is None, row
    # 既有字段名一个都没改
    for key in ("caller", "model", "elapsed", "prompt_tokens", "completion_tokens", "error", "ts"):
        assert key in row, f"costs 行缺既有字段 {key}: {row}"
    assert row["prompt_tokens"] == 11 and row["completion_tokens"] == 3, row


def check_network_and_other_classified_without_retry(tmp: Path) -> None:
    """连接层错误归 network，其他归 other；两者都不在 429/5xx 白名单里，一次就停。"""
    import httpx

    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    out = _run([httpx.ConnectError("no route", request=request)], tmp_costs=_costs(tmp, "network"))
    assert out["calls"] == 1, out["calls"]
    assert out["record"]["error_kind"] == "network", out["record"]
    assert out["sleeps"] == [], out["sleeps"]

    out = _run([ValueError("unexpected shape")], tmp_costs=_costs(tmp, "other"))
    assert out["calls"] == 1, out["calls"]
    assert out["record"]["error_kind"] == "other", out["record"]
    assert out["sleeps"] == [], out["sleeps"]


def check_missing_key_is_auth_without_request(tmp: Path) -> None:
    """没有 key 是配置问题：不发请求、不重试，也标 auth 好统计。"""
    out = _run([_Resp("never")], tmp_costs=_costs(tmp, "nokey"), env_key=None)
    assert out["calls"] == 0, f"没 key 不该发请求（实到 {out['calls']}）"
    assert out["record"]["attempts"] == 0, out["record"]
    assert out["record"]["error_kind"] == "auth", out["record"]
    assert "DEEPSEEK_API_KEY" in str(out["record"]["error"]), out["record"]["error"]


def check_trace_carries_kind_and_attempts() -> None:
    """trace 那一份能看到重试次数与分类（不改既有键）。"""
    from core.llm_call import usage_for_trace

    failed = usage_for_trace({"error": "RateLimitError: 429", "error_kind": "rate", "attempts": 3,
                              "prompt_tokens": None, "completion_tokens": None,
                              "model": "deepseek-chat", "elapsed": 1.2})
    assert failed["llm_error_kind"] == "rate", failed
    assert failed["llm_attempts"] == 3, failed
    assert failed["llm_error"] == "RateLimitError: 429", failed
    ok = usage_for_trace({"error": None, "error_kind": None, "attempts": 1,
                          "prompt_tokens": 5, "completion_tokens": 2,
                          "model": "deepseek-chat", "elapsed": 0.4})
    assert ok["llm_attempts"] == 1, ok
    assert "llm_error_kind" not in ok, f"成功不该带分类: {ok}"
    assert "llm_error" not in ok, ok


def check_complete_raises_after_retry_exhausted(tmp: Path) -> None:
    """complete() 仍然抛 RuntimeError（记录已落盘），返回形状没变。"""
    import core.llm_call as llm_mod

    create = _FakeCreate([_status_error(500, "boom")])
    _install(create)
    saved_sleep = llm_mod._sleep
    saved_costs = llm_mod.COSTS_PATH
    saved_key = os.environ.get("DEEPSEEK_API_KEY")
    llm_mod._sleep = lambda seconds: None
    llm_mod.COSTS_PATH = _costs(tmp, "raise")
    os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
    try:
        try:
            llm_mod.complete([{"role": "user", "content": "hi"}], caller="check")
        except RuntimeError as exc:
            text = str(exc)
        else:
            raise AssertionError("complete() 失败时必须抛 RuntimeError")
    finally:
        llm_mod._sleep = saved_sleep
        llm_mod.COSTS_PATH = saved_costs
        if saved_key is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = saved_key
    assert text, "RuntimeError 文案不该为空"
    assert create.calls == llm_mod.MAX_RETRIES + 1, create.calls


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="llm-backoff-") as raw:
        tmp = Path(raw)
        check_rate_limit_retries_then_succeeds(tmp)
        check_auth_fails_immediately(tmp)
        check_server_error_exhausts_retries(tmp)
        check_success_has_no_extra_attempts(tmp)
        check_network_and_other_classified_without_retry(tmp)
        check_missing_key_is_auth_without_request(tmp)
        check_trace_carries_kind_and_attempts()
        check_complete_raises_after_retry_exhausted(tmp)
    print("llm backoff ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
