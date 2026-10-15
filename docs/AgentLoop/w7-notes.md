# W7 学习笔记：LangGraph Agent Loop V2

## 一、W7 完成度

| 拼图   | 内容                       | 状态      |
| ------ | -------------------------- | --------- |
| 拼图 1 | RetryPolicy + 强制错误注入 | ✅ 完整。失败注入只属于 `UE_EXEC_MODE=mock` |
| 拼图 2 | SqliteSaver 持久化         | ✅ 完整   |
| 拼图 3 | Build + Test + Fix 闭环    | ✅ 完整   |
| 附加 A | 结构化 finish 信号         | ✅ 完整   |
| 附加 C | Streaming 可视化           | ✅ 完整   |
| 待办 B | 真实 MCP 接入              | ✅ 已完成。出口是 `core/editor_exec.py` |

---

## 二、Agent Loop V2 的 7 条核心设计原则

### 1. 状态机 > 过程式代码

循环、分支、重试全部交给 LangGraph 图结构表达。每个节点是纯函数，返回 state 的增量。

### 2. 外部裁判 > LLM 自评

**能验证的任务（编译、测试）就让工具当法官。** LLM 说"成功"不算数，`Build succeeded` 才算。

### 3. 单一终态判定者

- **可验证任务**（require_build=True）：不给 LLM finish 工具，终态由 build/test 决定
- **主观任务**（require_build=False）：给 LLM finish 工具，由 LLM 决定

### 4. 失败累加必经 observe

任何失败路径**必须经过 observe 累加 retry_count**，否则会死循环。

build 失败 → observe（累加）→ fix → plan → build ...
✅ ❌ 直接 build → fix → plan → 死循环

### 5. 失败注入要可控

用 `INJECT_X_FAIL + retry_count < N` 组合，保证修一次就能通过。

```python
if inject and state["retry_count"] < 1:
    return "Build failed..."
return "Build succeeded"
```

### 6. State 必须可序列化

SqliteSaver 要求所有 state 内容能 JSON 化。

❌ OpenAI 的 ChatCompletionMessage 对象

❌ ToolCall 对象

✅ 纯 dict / list / str / int / bool

### 7. 三道护城河

动态 thread_id（时间戳 + UUID）

recursion_limit=50

每个节点首行 print("--- [Node] xxx ---")



### 坑 1：`[...]` 是 `[Ellipsis]` 不是占位符

**python**

```
random.choice([...])   # ❌ 永远返回 Ellipsis
random.choice(["a", "b"])  # ✅
```

 **教训** ：复制代码要理解每一行。

### 坑 2：`ChatCompletionMessage` 不可序列化

 **症状** ：SqliteSaver 报 `TypeError: Object of type ChatCompletionMessage is not JSON serializable`
 **修复** ：`plan_node` 里把 OpenAI 消息转成 dict 再进 state。

### 坑 3：旧 checkpoint 污染新实验

 **症状** ：LLM 不调用工具，直接说"任务失败"（因为旧消息里全是失败记录）
 **修复** ：动态 `thread_id`，或删 `agent_checkpoints.db`

### 坑 4：环境变量硬编码

 **症状** ：`$env:INJECT_ERRORS="false"` 设了没用
 **修复** ：`INJECT_ERRORS = os.getenv("INJECT_ERRORS", "true").lower() == "true"`

### 坑 5：`build_result` 语义不清

 **症状** ：build 只跑一次就永远跳过
 **修复** ：`fix_node` 里清空 `build_result` 和 `test_result`，让下一轮重新 build

### 坑 6：LLM 抢在 build 前调 finish(False)

 **症状** ：require_build=True 时 LLM 说"做不到"，直接结束，build/test 从未执行
 **修复** ：require_build=True 时**不给 LLM finish 工具**

### 坑 7：build → fix 直连导致死循环

 **症状** ：retry_count 永远 0，因为跳过了 observe
 **修复** ：`route_after_build` 失败时必须 `return "observe"`

### 坑 8：observe_node 停在草稿版

 **症状** ：`[Node] observe` 打印从不出现，retry_count 永远 0
 **修复** ：用 `inspect.getsource()` 读实际运行的代码，别凭记忆

### 坑 9：`build_node` 缺 retry_count 检查

 **症状** ：注入失败后永远失败
 **修复** ：`if inject and state["retry_count"] < 1:` 加这个条件
