# M4 复现指南：网页应用层（FastAPI + SSE + 极简前端）

> 这本指南对应 `src/personal_data_assistant/api/`（`main.py`、`routes.py`、
> `static/index.html`）以及为 Web 并发做的 `memory/long_term.py` 线程安全补丁。
> 复现透了，「FastAPI 工程化、SSE 流式输出、统一错误处理、SQLite 线程安全」
> 四类面试题闭眼答。

## 本里程碑是什么

在 M1~M3 全部业务能力之上加一个应用层：

**浏览器单页 → POST /ask（记忆问答）或 /ask_sql（问数）→ FastAPI 薄壳 →
请求体 timeout 包装 → 调既有业务对象 → 统一中文错误结构或 SSE 事件流 →
原生 JS 按块渲染。** 业务逻辑不在路由里写；core/llm/data 的公开接口一行未改。

## 先理解 7 个核心概念（复现的钥匙）

1. **create_app 是工厂，不是模块级 app 对象**：`create_app(settings=...)` 负责把
   `DeepSeekClient`、`MemoryManager`、`HabitAliasStore`、`PersonalAssistant`、
   `SqlAskService` 装配成一个 FastAPI 实例；测试注入 fake 对象，生产 `create_app()`
   自动读环境变量。模块级 `app = FastAPI()` 会让测试一 import 就找 API key，没法 fake。
2. **两条 POST 业务路径分工明确**：
   - `/ask` → `PersonalAssistant.ask(question, stream=..., on_chunk=...)`，
     即习惯改写 → 记忆增强 → core 循环；模型可以自己决定是否调 `sql_query`。
   - `/ask_sql` → `SqlAskService.ask(question)`，即习惯改写 → `ask_database`
     确定性问数子流程。它保证一定查库、一定拿回 SQL/行集/中文解释，不依赖外层
     模型“愿不愿意调工具”。
3. **POST + SSE 必须用 fetch 读流，不能用 EventSource**：SSE 本身是 HTTP 文本流，
   但 `EventSource` 只支持 GET；接口要带 `{question, stream, timeout}` 请求体，
   所以前端用 `fetch` + `response.body.getReader()`，按 `\n\n` 切事件块，再解析
   `data:` 行里的 JSON。服务端事件只分 `status / chunk / tool / done / error`。
4. **`on_chunk` 吐的是模型原话，工具调用 JSON 要折叠**：M1 循环为了可观测性，
   把所有增量（含 `{"action":"tool",...}`）原样转发给 `on_chunk`。M4 在 API 层加
   `StreamActionFilter` 增量状态机：prefix → tool/final。tool 模式原样发
   `tool` 事件给前端折叠；final 模式从 `answer` 字符串里逐字符解码（含 `\n`、
   `\"`、`\uXXXX` 和代理对），只发正文 `chunk`。这样用户永远看不到半截 JSON。
5. **统一错误结构是前端唯一契约**：校验失败 422、超时 504、模型不可用 503、
   404、500 全部返回 `{"error":{"code","message","detail?"}}`，message 全中文。
   前端只要读 `body.error.message`，不用给每种异常写分支。
6. **请求超时用“守护线程 + 队列 deadline”实现**：同步业务函数在守护线程跑，
   主线程 `queue.get(timeout=...)` 到点就返回 504。底层 DeepSeek 客户端还有自己的
   连接/读取超时和重试，所以遗留线程会被自然回收；不会无限悬挂。
7. **FastAPI 线程池会跨线程复用 MemoryManager**：`sqlite3` 连接默认
   `check_same_thread=True`，第二个请求可能直接 `ProgrammingError`。M4 给
   `MemoryDatabase` 补了 `check_same_thread=False` + 公开方法统一 RLock；
   `schema_version` 是 property，装饰时要包 `fget` 而不是替换整个 property。

## 数据流（对着源码走一遍）

### 装配流

```
load_settings()  ── api key / 记忆路径 / 问数路径 / 超时与轮数
  ├─ DeepSeekClient(settings)
  ├─ MemoryManager.from_settings(settings, model=client)
  ├─ HabitAliasStore(memory_manager.db)          # 与 app 共享同一 KV 库
  ├─ PersonalAssistant(model, tools=[], memory_manager, user_db_path, habits)
  └─ SqlAskService(model, db_path, habits, ...)  # /ask_sql 确定性入口
create_app() → 注册异常 handler → include_router(build_router(...))
             → mount("/", StaticFiles(static_dir, html=True))
```

### `/ask` 非流式

```
POST /ask {question, stream=false, timeout}
  → FastAPI 参数校验（Pydantic；空白问题 422）
  → _run_with_timeout(lambda: assistant.ask(question), timeout)
  → LoopResult.status == "model_error" ? 503 统一错误
  → 200 {question, answer, status, streamed, rounds, tool_rounds, usage, error}
```

### `/ask` 流式（SSE）

```
POST /ask {question, stream=true, timeout}
  → StreamingResponse(_ask_stream_events(...), media_type=text/event-stream)
  → 先发 status 事件
  → 守护线程跑 assistant.ask(stream=True, on_chunk=把原始增量放进 queue)
  → 主循环从 queue 取原始增量 → StreamActionFilter.feed()
       ├─ 未识别协议 → 暂存
       ├─ {"action":"tool"...}  → 发 tool 事件，前端折进 <details>
       └─ {"action":"final","answer":"..."} → 只发 chunk 事件（正文按块）
  → LoopResult 回来 → 发 done 事件 {status, answer, tool_rounds, usage}
  → 中途模型错误 → error 事件 code=model_unavailable；超时 → error code=timeout
```

### `/ask_sql`

```
POST /ask_sql {question, stream, timeout}
  → SqlAskService.ask(question)
       ├─ HabitAliasStore.rewrite_question("八月份饭钱花了多少")
       │    → "八月份餐饮花了多少"，记录 alias_applied=[["饭钱","餐饮"]]
       └─ ask_database(rewritten, db_path, model, ...)   # M3 子流程原样复用
            → AskResult(answer=中文解释, sql, rows, usage, attempts, ...)
  → 非流式：200 返回 answer/sql/rows/status/alias_applied/usage
  → 流式：status 事件 → 把 answer 按块发 chunk → done 带 sql/columns/rows
```

### 前端渲染流

```
index.html
  ├─ 两个模式按钮 data-endpoint="/ask" / "/ask_sql"
  ├─ 提交 → fetch(POST, {question, stream:true, timeout:120})
  │     ├─ response.ok=false → 读 JSON 的 error.message → 红色错误泡泡
  │     └─ response.body.getReader() → TextDecoder 累积 buffer
  │          → 按 "\n\n" 切 SSE 块 → 逐行取 data: JSON
  │          → chunk: bubble.textContent += delta
  │          → tool:  <details> 里的 <code> 追加工具 JSON
  │          → done:  补状态/token；问数模式渲染 SQL 与结果表
  │          → error: 红色错误泡泡
  └─ /health 启动时 fetch 一次，把组件状态显示在页眉
```

## 复现步骤（浓缩版，细节见 PROGRESS.md 的复现要点）

1. 读 `docs/architecture.md` 5.4、ADR-8 和本指南；确认唯一测试命令
   `PYTHONPATH= .venv/bin/python -m pytest`。
2. **先写 2 个测试文件**：
   - `tests/test_api.py`：TestClient + fake 模型，覆盖 `/health` 存活与组件
     degraded、`/ask` 非流式走 app.ask、`/ask` SSE 按块渲染、工具调用折叠、
     `/ask_sql` 习惯改写 + 中文解释、`/ask_sql` SSE + SQL 元数据、422 校验、
     504 超时、503 模型不可用、404 统一错误；
   - `tests/test_frontend.py`：GET `/` 返回 HTML，断言聊天区/输入框/发送按钮、
     两个入口 `data-endpoint`、原生 `fetch` + `getReader`、三种事件类型字符串。
3. 更新 `pyproject.toml` 加 `fastapi>=0.115,<0.116` 与 `uvicorn>=0.30,<1`；
   此时先跑测试应看到 **2 个 collection error**（`No module named 'fastapi'`），
   这是红阶段证据；然后 `uv pip install --python .venv/bin/python -e '.[dev]'`
   安装依赖。
4. 按依赖顺序实现：
   - `api/routes.py`：Pydantic 请求模型 → `APIError`/统一错误 handler →
     `_run_with_timeout` → `_sse` → `StreamActionFilter`（prefix/tool/final
     三态 + JSON 转义解码 + tool 完整对象切分）→ SSE 生成器 → `build_router`；
   - `api/main.py`：`AskSqlOutcome`/`SqlAskService`/`HealthChecker` →
     `_build_default_stack` → `create_app` 工厂 + lifespan 关资源 + StaticFiles；
   - `api/static/index.html`：单文件页面，先能非流式收 JSON，再接 fetch 读流；
   - `memory/long_term.py`：`check_same_thread=False` + `_synchronize_methods`
     （property 要包 fget），这是 Web 并发必需的补丁。
5. 重点复现三个接缝：
   - `/ask` 流式：`on_chunk` 只负责把原始增量送队列；分类逻辑全部在
     `StreamActionFilter`，核心循环零改动；
   - `/ask_sql`：一定先 `habits.rewrite_question` 再 `ask_database`，顺序与
     `app.ask` 保持一致；流式是结果分块，不是模型 token 流，文档里写清楚取舍；
   - 统一错误：FastAPI 默认校验错误、Starlette HTTPException、未知异常都要挂
     handler，缺一个前端就会拿到非 JSON 的默认响应。
6. 跑 `PYTHONPATH= .venv/bin/python -m pytest`，目标 **172 passed,
   5 deselected**；再跑 `-m integration`，无密钥应为 **5 skipped**。
7. 启动冒烟：`PYTHONPATH= .venv/bin/python -m personal_data_assistant.api.main`，
   浏览器开 `http://127.0.0.1:8000`，分别点「记忆问答」「问数」各问一句，
   确认答案逐块出现、工具调用是折叠的、`/health` 组件状态正常。
8. 更新 README（进度表/快速开始/设计取舍）、architecture.md（v0.4/5.4/
   目录标 ✅/ADR-8）、`PROGRESS.md`；最后写本指南。

## 自测题（不看代码答一遍，错了就回去看）

1. `POST /ask` 和 `POST /ask_sql` 分别调用哪个业务入口？为什么 `/ask_sql`
   不直接复用 `/ask` 的模型自主决策，而要单独走 `SqlAskService`？
2. `EventSource` 实现 SSE 最省事，这个项目为什么不用它？前端如何从 POST 的
   fetch 响应里切出一个个事件？服务端约定哪五种事件类型？
3. M1 的 `on_chunk` 会把 `{"action":"tool",...}` 也吐出来，`StreamActionFilter`
   是怎么做到“工具 JSON 进 tool 事件、最终答案进 chunk 事件”的？如果 final
   答案里含有 `\"` 或 `\n` 转义，分块恰好把转义切成两半，状态机怎么办？
4. `_run_with_timeout` 超时后为什么还可能有后台线程在跑？这个设计为什么在本
   项目可接受？底层模型客户端的哪些超时配置保证线程不会永久悬挂？
5. FastAPI 线程池复用 `MemoryManager` 时，原版 `MemoryDatabase` 会在什么操作上
   抛什么错误？M4 的线程安全补丁改了什么？为什么 `schema_version` 这个 property
   不能像普通方法一样直接包一层函数装饰器？

## 面试预演

1. 让你给一个既有 Agent 项目加 Web 服务，你会把业务逻辑放在 FastAPI 路由里吗？
   讲讲你的路由分层、工厂注入和 fake 测试策略；为什么模块级 `app = FastAPI()`
   不利于测试？
2. 流式输出有哪些实现选择（WebSocket、SSE、轮询）？你为什么选 SSE？POST 请求
   的 SSE 前端要怎么写？如果用户中途关掉页面，服务端线程和模型调用会怎样？
3. 你的统一错误结构是什么？FastAPI 默认的 422 校验错误长什么样，你为什么要
   覆盖它？超时、模型 503、数据库损坏分别返回什么 code 和中文 message？
4. 工具调用 JSON 和最终答案混在一个 `on_chunk` 流里，你如何做到前端折叠工具
   调用、只显示答案？为什么不在 core loop 里改，而在 API 层过滤？如果模型
   输出的 JSON 格式带前导说明文字、Markdown 围栏或转义换行，你的状态机还能
   工作吗？
5. SQLite 连接默认绑定创建线程，FastAPI 线程池下会发生什么？你有哪几种解法
   （每请求新建连接、单线程执行器、check_same_thread=False+锁）？各自的取舍
   是什么，你为什么选最后一种？
