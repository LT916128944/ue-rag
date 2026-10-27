# 本地网关：模型名映射（只改写，不设卡）

Cursor 在发请求**之前**就会按模型名前缀拦截（`gpt-` / `opus-` 等）。这个网关让 Cursor 只看到 `custom-*`，
由网关在转发前把 `model` 字段换成真实名字，回包再换回来，所以 Cursor 侧的会话、计费、上下文窗口都还认这个模型。

```
Cursor ──(model: custom-gpt-6.1-sol)──> 127.0.0.1:8787 ──(model: gpt-6.1-sol)──> 中转站
       <──(model: custom-gpt-6.1-sol)──                <──(model: gpt-6.1-sol)──
```

## 0. 边界：网关不做准入

网关的职责只有「改名」，**不判断模型名合不合法**：

| 客户端发来的 model | 网关行为 |
| --- | --- |
| `custom-gpt-6.1-sol` | 剥前缀 → 转发 `gpt-6.1-sol` |
| `gpt-6.1-sol`（不带前缀） | **原样透传**，不拦、不改、不猜 |
| `some-unknown-model` | **原样透传**，是否可用由上游判定 |
| `custom-`（裸前缀） | 原样透传，不会变成空模型名 |

理由：用户自己填真名，说明 Cursor 侧本来就不拦它，网关没有任何立场再拦一道。
网关不认识的模型名不是错误，网关只负责转发，错误由上游的 HTTP 状态码如实返回。

## 1. 配置

编辑 `config.json`，最少改两处：

| 字段 | 说明 |
| --- | --- |
| `upstream.openai.baseUrl` | 你中转站的 [OI] 兼容地址，例如 `https://xxx.com/v1` |
| `upstream.anthropic.baseUrl` | 中转站的 Anthropic 地址，一般和上面同域名但不带 `/v1` |
| `upstream.*.apiKey` | 中转站 key，已填好（也可用环境变量 `UPSTREAM_API_KEY` 覆盖） |

模型名映射由一条前缀规则搞定，**不用一条条写**：

```json
"stripPrefix": "custom-"
```

于是：

| Cursor 里填的名字 | 实际发出去的 |
| --- | --- |
| `custom-gpt-6.1-sol` | `gpt-6.1-sol` |
| `custom-gpt-6-astra` | `gpt-6-astra` |
| `custom-opus-5` | `opus-5` |
| `gpt-6.1-sol` | `gpt-6.1-sol`（无前缀，原样） |

前缀规则覆盖不了的特例写 `modelAliases`，优先级更高：

```json
"modelAliases": {
  "custom-fast": "gpt-6-astra",
  "custom-sonnet": ""
}
```

> 裸写 `custom-`（后面没名字）不会被替换成空模型名，会原样转发。

## 2. 启动

```powershell
# 方式一
.\start.bat

# 方式二
node gateway.mjs --config config.json
```

看到 `llm-gateway 已启动: http://127.0.0.1:8787` 即可。自检：

```powershell
node selftest.mjs     # 起假上游跑全部断言，不需要真 key
```

**双击 `start.bat` 不会闪退了**：脚本在所有退出路径（启动成功、已在运行、找不到 node、缺
`config.json`）都会 `pause` 并提示「按任意键关闭」，任何报错都留得住。

**重复启动是安全的**：端口已被本网关占用时不会再抛一屏 `EADDRINUSE` 堆栈，而是提示
`[已在运行]` 并退出 0。想同时跑第二份配置就换端口：

```powershell
.\start.bat --port 8788
# 或 node gateway.mjs --config config.json --port 8788
```

## 3. 配到 Cursor

`Settings → Models → API Keys`：

- **[OI] API Key**：随便填（网关会用 config 里的 key 覆盖）
- **Override [OI] Base URL**：`http://127.0.0.1:8787/v1`
- 然后在模型列表里 **Add model**，名字填 `custom-gpt-6.1-sol`

Anthropic 同理：**Override Anthropic Base URL** 填 `http://127.0.0.1:8787`（不要带 `/v1`），
网关按路径自动分流：`/v1/messages` 走 anthropic 上游（用 `x-api-key`），其余走 openai 上游（用 `Bearer`）。

访问 `http://127.0.0.1:8787/health` 可以看当前生效的配置。

`GET /v1/models` 返回 **`modelAliases` ∪ 上游真实模型清单**：

```json
{ "object": "list", "data": [{ "id": "gpt-6.1-sol", ... }, { "id": "custom-fast", ... }], "upstream_ok": true }
```

这样不带 `custom-` 前缀的真名也能在 Cursor 的模型列表里被认出来，不会因为「网关只列别名」而被当成不存在。
上游列不出来时（`upstream_ok: false`）仍会返回别名，不影响正常转发。

## 4. 行为细节

- **流式不缓冲**：SSE 逐行改写 `"model"` 字段后立刻转发，不影响首字延迟。
- **不改 key 泄漏**：Cursor 传上来的 `authorization` / `x-api-key` 一律丢弃，换成配置里的 key。
- **`/v1/v1` 去重**：baseUrl 已带 `/v1` 时不会重复拼。
- **请求头**：去掉 `accept-encoding` 才能改写响应体；上游仍返回压缩体会自动解压。
- **日志**：每条请求打一行，带前缀的显示 `custom-x -> x`，不带前缀的显示 `x (透传)`。

## 5. 注意

- 这是**本地开发用**的中转：只监听 `127.0.0.1`，不要暴露到公网，也不要把带 key 的 `config.json` 提交进仓库（已加 `.gitignore`）。
- 如果中转站对 `gpt-6.1-sol` 这类名字本身也做校验，那说明拦截发生在中转站侧，网关这边无法绕过——需要换支持该模型名的中转站。
- 端口被占用就改 `listen.port`，或加 `--port`，Cursor 里的 base URL 同步改。
