# PROGRESS —— 项目进度手记

> 规则：每完成一步，更新一条记录，固定包含三样东西：
> 进度（做了什么）、踩过的坑（哪里会翻车）、复现要点（关掉我的代码后重写一遍的复习材料）。

---

## M0 仓库骨架 + 架构设计文档

### 进度

- 在 `/home/hujiao/projects/data-assistant` 建立仓库骨架：
  `src/personal_data_assistant/`、`tests/`、`docs/`、`data/`、`evals/`，空目录用 `.gitkeep` 占位。
- 完成 `README.md`：项目定位、M0~M5 里程碑表、目录速览、快速开始、开发纪律、非目标。
- 完成 `docs/architecture.md`（372 行）：模块划分、四条数据流（记忆入库、记忆问答、
  智能问数、核心循环）、SQLite 存储设计、目录安排、M1~M4 关键机制、降级 B 计划总表、
  评测方案、ADR、非目标。
- 完成 `pyproject.toml`（声明 Python 3.12 + pytest）、`.gitignore`、`.env.example`。
- 严格按 TDD：先写 `tests/test_project_skeleton.py` 仓库契约测试，跑出 7 项全红；
  再补齐骨架与文档，测试转绿（7 passed）。
- 本步没有写任何业务代码。

### 踩过的坑

1. **开发机默认 Python 是 3.10.12，不是项目要求的 3.12**；Ubuntu 22.04 的 apt 源里
   没有 python3.12。所以 M0 的测试刻意避开 `tomllib`（3.11+ 才有），用纯文本断言，
   保证契约测试在 3.10 开发机上也能跑。后续 M1 起要落实真实的 Python 3.12 环境
   （venv/pyenv/uv），不要让“声明 3.12、实际 3.10 开发”继续拖到 M5。
2. **pytest 运行会产生 `.pytest_cache` 和 `__pycache__`**，必须写进 `.gitignore`，
   否则骨架一跑测试就变脏。
3. **契约测试不要逐字锁死文档**：文档以后每个里程碑都会改，测试只断言“文件存在 +
   关键章节/关键词存在”，否则文档润色一次就要改一次测试，测试会变成累赘。
4. **`.env.example` 绝不能放真实密钥**，测试里专门加了一条断言 `sk-` 不能出现；
   这个习惯要一路带到 M1 的模型测试里（fake 优先，真实 API 只走 integration 标记）。
5. **本机 pytest 是 6.2.5，而 pyproject 声明 dev 依赖 pytest>=8**；M0 测试语法
   同时兼容两者。后续在 3.12 虚拟环境里按 pyproject 安装，版本会自然对齐，
   但“本机直接跑”和“按声明安装”两条路径都要能过。

### 复现要点

关掉代码后重写 M0，按这个顺序来：

1. 建目录：
   `mkdir -p src/personal_data_assistant tests docs data evals`，
   并给 `src/personal_data_assistant`、`data`、`evals` 放 `.gitkeep`。
2. **先写测试** `tests/test_project_skeleton.py`，断言 7 件事：
   目录存在、README 有 M0~M5、架构文档有“模块划分/数据流/目录安排/评测方案/
   降级与 B 计划”五个章节、PROGRESS 有“进度/踩过的坑/复现要点”三个小节、
   pyproject 声明 `requires-python = ">=3.12"` 和 pytest、.gitignore 忽略
   `.env`、`__pycache__/`、`.venv/`、.env.example 含 `DEEPSEEK_API_KEY=` 且无 `sk-`。
3. 跑 `python3 -m pytest tests/ -v`，确认 7 项全红（TDD 的红阶段证据）。
4. 补 `README.md`、`docs/architecture.md`、`PROGRESS.md`、`pyproject.toml`、
   `.gitignore`、`.env.example`。
5. 再跑 `pytest`，确认 7 passed；最后更新本文件。
6. 检查命令：`find . -maxdepth 3 -not -path '*/.pytest_cache*' -not -path '*/__pycache__*' | sort`。
7. 停下汇报，等确认后再进 M1。

## 环境决策（评审后补充，M1 前落实）

- 开发机系统 Python 为 3.10.12，且外网无法拉取 python-build-standalone，
  决定**放弃 3.12，全项目以 Python 3.10+ 兼容开发**；`requires-python = ">=3.10,<3.13"`。
- 使用 uv 管理环境：`~/.local/bin/uv venv --python 3.10 .venv`，依赖走清华 PyPI 镜像。
- **ROS 坑**：本机 `.bashrc` 注入 `PYTHONPATH=/opt/ros/humble/...`，会污染任何 venv，
  导致 pytest 加载 ROS 的第三方插件而报 `import yaml` / `from lark import Lark`。
  运行测试必须清空：`PYTHONPATH= .venv/bin/python -m pytest`。

---

## M1 核心循环（工具注册表 + JSON Schema 工具调用 + 流式 + 重试降级）

### 进度

- 严格 TDD：先写 `test_config.py`、`test_tools.py`、`test_prompts.py`、
  `test_core_loop.py`、`test_llm_client.py`、`test_integration_deepseek.py`，
  红阶段为 6 个 collection error（模块缺失 + httpx 未装）；再逐层实现转绿。
- 新增实现：
  - `config.py`：`Settings` 冻结 dataclass，默认值锁架构文档
    （超时 10/30s、重试 2、工具轮数 6、SQL 修正 3），环境变量校验。
  - `tools/base.py`：`Tool`（名称 + 描述 + JSON Schema + func）、`ToolResult`、
    `validate_arguments`（required + 基础 JSON 类型，不引入 jsonschema）。
  - `tools/registry.py`：注册/去重/查找/列清单，`describe_for_prompt()` 输出紧凑 JSON。
  - `llm/prompts.py`：JSON 动作协议提示词
    `{"action":"tool","tool":...,"args":...}` / `{"action":"final","answer":...}`；
    解析错误、未知工具、工具结果、强制收束指令全部有固定消息模板。
  - `llm/client.py`：httpx 手写 DeepSeek 客户端。非流式/流式 SSE 解析、连接/读取
    超时、429/5xx/传输错误指数退避重试、usage 采集（流式带
    `stream_options={"include_usage": true}`，缺失时粗估并标记 estimated）、
    流式失败自动降级非流式一次、`StreamedChat.result` 在流结束后给完整 `LLMResponse`。
  - `core/loop.py`：状态机。模型调用 → 解析动作 → 执行工具 → user 角色回填结果 →
    再调用；解析错误/未知工具/工具异常都回填并继续；非 final 回复占最大轮数预算，
    越界拒绝执行并“强制收束”一次，模型仍不 final 时用轨迹工具结果拼中文兜底；
    模型崩溃返回 `status="model_error"` 的结构化中文错误，不抛进程。
- 工程配置：pyproject 依赖加 `httpx>=0.27,<1`；pytest `addopts` 默认
  `-m "not integration"`，真实 API 测试默认不跑。
- 测试结果：`PYTHONPATH= .venv/bin/python -m pytest` → **55 passed, 2 deselected**；
  `-m integration` 无密钥时 2 skipped（符合 fake 优先）。
- README 新增「设计取舍」：原生 function calling vs JSON 动作协议对比表、为什么
  手写 httpx 不用 openai SDK；快速开始改为 uv + ROS PYTHONPATH 清空命令。
- docs/architecture.md 同步修订：Python 3.10+、M1 文件/测试标 ✅、ADR-2 协议定稿。

### 踩过的坑

1. **httpx.Response 不是上下文管理器**：`with response:` 直接 `AttributeError: __enter__`。
   流式响应要用 `try/finally: response.close()`；MockTransport 返回的 Response 也一样。
2. **降级路径容易“重试套重试”**：流式打开失败后如果 fallback 非流式也失败，异常若
   继续落进外层 `except LLMClientError`，会再降级一次。M1 重构为“打开 SSE”和
   “读取 SSE”两个独立阶段，各自失败只在吐字前降级一次；`test_llm_client` 锁死
   `attempts["complete"] == 1`。
3. **OpenAI 流式默认不返回 usage**：必须发 `stream_options={"include_usage": true}`；
   且最后的 usage chunk 形态是 `{"choices": [], "usage": {...}}`，SSE 解析器必须
   容忍空 choices，否则会把收尾块当异常。
4. **紧凑 JSON 会让朴素测试翻车**：工具清单为省 token 用 `json.dumps(..., separators=(",",":"))`，
   测试里不能用 `tail.index("]")` 找数组结尾（会命中 `required":["text"]` 的内层 `]`），
   要用 `json.JSONDecoder().raw_decode` 从第一个 `[` 解析完整值。
5. **测试注入 retry_sleeper 时不要写条件表达式 lambda**：`lambda d: (log.append(d) or f(d) if f else None)`
   的优先级会让 `append` 在 sleeper 为空时根本不执行。写成具名嵌套函数最稳。
6. **不能使用原生 `role=tool` 回填**：没有走 function calling 就没有 `tool_call_id`，
   硬塞 `role=tool` 会被 OpenAI 兼容服务端拒绝；统一用 `role=user` 包
   `{"tool_result":...}`，并在 prompt 里告诉模型“下一轮会以 user 消息回填”。
7. **最大轮数有 off-by-one 陷阱**：必须“先把非 final 回复计入预算，越界就拒绝执行
   工具”，而不是执行完再判断；否则 `max_tool_rounds=2` 会执行 3 次工具。越界那一步
   记成 `round_limit` 轨迹步，既好调试也好写测试。
8. **默认 addopts 排除 integration 的机制**：`-m "not integration"` 只负责默认不跑；
   显式 `-m integration` 时还要靠 `skipif(没有 DEEPSEEK_API_KEY)` 再挡一次，
   两道闸都不能少。
9. **核心循环的流式语义**：M1 为可观测性把模型所有增量块（含工具调用 JSON）原样
   转发给 `on_chunk`；页面层（M4）再负责折叠工具调用。不要让 loop 自己去猜
   “这半块是不是最终答案”，猜错会把答案/工具 JSON 都截坏。
10. **Python 3.10 兼容性**：本里程碑全部用 3.10 原生语法（`X | None`、`dataclass`、
    `from __future__ import annotations`），未引入 3.11+ 特性；uv venv 里仍是 3.10.12。

### 复现要点

关掉代码后重写 M1，按这个顺序：

1. 读 `PROGRESS.md` 环境决策与 `docs/architecture.md` 的 M1 设计；确认
   `PYTHONPATH= .venv/bin/python -m pytest` 是唯一测试命令。
2. **先写 6 个测试文件**：
   - config：默认值、覆盖、非法值；
   - tools：成功/异常/参数校验/注册表；
   - prompts：协议词、工具 JSON 可反解、错误回填、强制收束；
   - core_loop：fake 模型队列测 final、tool→final、解析错误恢复、未知工具、
     工具异常、最大轮数、强制收束、模型崩溃、流式与 complete 降级；
   - llm_client：MockTransport 测 payload 无原生 tools、429 退避、传输错误重试、
     SSE 拆包、usage 尾块、流失败降级一次、全失败异常；
   - integration：整文件 `pytest.mark.integration` + 无 key skip。
3. 跑 `PYTHONPATH= .venv/bin/python -m pytest -q` 看 6 个 collection error 红。
4. 改 `pyproject.toml`（httpx 依赖 + addopts 排除 integration），
   `uv pip install --python .venv/bin/python -e '.[dev]'`。
5. 按依赖自底向上实现：`config` → `tools/base` → `tools/registry` →
   `llm/prompts` → `llm/client` → `core/loop`；每个文件顶部写 3~5 行中文取舍注释。
6. 流式客户端实现顺序：非流式 complete → 重试发送 → SSE `_iter_sse_data_events` →
   usage 尾块 → `_stream_fallback`（严格单次）→ `StreamedChat.result`。
7. 跑 `PYTHONPATH= .venv/bin/python -m pytest`，目标 55 passed、2 deselected；
   再跑 `-m integration` 确认无密钥 2 skipped。
8. 更新 README（进度表 + 设计取舍表）、architecture.md（Python 3.10、M1 标 ✅）、
   PROGRESS.md；停下汇报，等确认后再进 M2。


---

## M2 记忆系统（滑动窗口 + 分层摘要 + SQLite + KV 时间衰减 + 四策略重放）

### 进度

- 严格 TDD：先写 6 个测试文件（`test_memory_window.py`、`test_memory_summarizer.py`、
  `test_memory_long_term.py`、`test_memory_retriever.py`、`test_app.py`、
  `test_integration_memory.py`）并扩展 `test_config.py` 的记忆默认值用例；
  红阶段为 6 个 collection error（memory 包与 app 模块不存在）。
- 新增实现（每个文件顶部都有中文取舍注释）：
  - `memory/models.py`：共享 `Message`/`Summary`/`KVMemory`/`MemoryContextItem`/
    `MemoryStrategy` 与 UTC 时间/日/周工具，避免包内循环 import。
  - `memory/window.py`：双预算 FIFO 窗口（默认 20 条 / 8k token）；`add()` 只返回
    淘汰消息，不自己调摘要/落库；支持单条超预算自淘汰。
  - `memory/summarizer.py`：LLM 分层摘要会话级→日级→周级；只依赖模型
    `complete(messages)` 鸭子协议；异常包成 `SummarizerError`；源文本超长截断。
  - `memory/long_term.py`：标准库 `sqlite3` 落库（`messages`/`summaries`/
    `kv_memories`，`PRAGMA user_version=1`）；消息、摘要 upsert（session 按
    level+session_id，daily/weekly 按 level+period_key）；KV 写入/读取（读取回写
    access_count/last_accessed_at）/时间衰减检索：
    `相关性 × weight × exp(-decay_lambda × 年龄天数)`。
  - `memory/retriever.py`：`MemoryManager` 编排。超窗淘汰消息立即增量更新会话摘要；
    `ingest_messages(finalize=True)` 自动 end_session→rollup_daily→rollup_weekly；
    检索按 `none/window/window_summary/full` 分叉；摘要失败降级“暂存截断原文”。
    提供 `replay_memory_strategies()`：同一批对话 × 四策略 × 独立 SQLite 重放，
    为 M5 铺路。
  - `app.py`：`PersonalAssistant` 装配入口；`ask()` 先
    `memory_manager.augment_question()` 再进 `run_tool_loop`，M1 循环零改动。
  - `config.py` 增加记忆默认值：strategy=full、窗口 20/8000、top-k=8、
    decay_lambda=0.05、db=`data/pda.db`，全部支持 `PDA_MEMORY_*` 环境变量覆盖。
- 工程配置：**无新第三方依赖**（M2 只用标准库 sqlite3/math/re）；`.env.example`
  补 `PDA_MEMORY_*`；新增 `data/README.md` 数据文件说明。
- 文档：`docs/architecture.md` 修订 v0.2（摘要层级由 L0/L1/L2 定稿为
  会话/日/周；长期记忆检索定稿为 KV 时间衰减，向量改为后续增强；目录与测试
  标 ✅；ADR-3 修订 + ADR-6 新增）；README 更新 M2 状态与设计取舍；
  新增 `docs/reproduce/M2-memory.md`（核心概念/数据流/复现步骤/自测 5 题/
  面试预演 5 题）。
- 测试结果：`PYTHONPATH= .venv/bin/python -m pytest` → **90 passed, 4 deselected**；
  `-m integration` 无密钥 → **4 skipped**（2 道 M1 聊天 + 2 道 M2 记忆，符合
  fake 优先、真实 API 默认跳过）。

### 踩过的坑

1. **“结束会话要不要清窗口”想当然会翻车**：第一版 `end_session` 把该会话消息从
   窗口移除，结果 finalize 之后窗口是空的，`window_summary/full` 检索只剩摘要，
   丢掉“近期原文精确命中”通道，三个测试同时红。最终改为：end_session 只做摘要、
   消息留在窗口；manager 用 `_summarized_message_ids` 记已摘要 id，未来消息被
   淘汰时先过滤，避免重复压进摘要。
2. **中文短查询会被 2-gram token 漏掉**：`“我学了什么”` 的 2-gram 是
   我学/学了/了什/什么，和 `“Python 装饰器学习记录”` 没有任何交集，full 策略
   检索为空。tokenizer 改为“中文单字 + 2-gram 都保留”；个人记忆库规模小，
   召回优先于精确，测试专门锁了这类短查询。
3. **开发机真实时间不可信，时间衰减测试必须全程注入时钟**：本机当前日期与测试
   固定的 2025 年不一致，`get_memory()` 忘记传 `now` 时 last_accessed_at 直接
   变成 2026 年，红得莫名其妙。教训：所有时间相关 API（put/get/search/manager/
   summary）都提供 `now` 或 `now_fn` 注入，测试里一条都不要依赖系统时间。
4. **ISO 周边界不能写 `fromisocalendar(year, week+1, 1)`**：遇到 53 周年份
   （如 2020）week+1=54 直接 ValueError；正确写法是
   `monday + timedelta(days=7)`。
5. **摘要 prompt 的层级词放错位置会锁死脆弱断言**：最初层级词只写在 user 消息，
   测试只想在 system 里找，红。实现上把“本次任务：生成 X 级摘要”同时放进 system，
   调试一眼可读；测试改为断言 system+user 合并文本，不逐字锁文案。
6. **重放必须每个策略一个库且先 unlink**：SQLite 文件复用会让第二次重放读到
   上一轮的 messages/summaries/kv，策略对比被污染。`replay_memory_strategies`
   对 `memory_<strategy>.db` 先 `unlink(missing_ok=True)` 再建 manager。
7. **config 校验策略不要反向 import memory 包**：`config.py` 是底层模块，用
   本地 `_MEMORY_STRATEGIES` 集合校验，保持依赖方向；`MemoryManager` 的枚举
   校验是第二道闸，两边允许的值必须同步。

### 复现要点

关掉代码后重写 M2，按这个顺序来：

1. 读 `docs/architecture.md` 5.2 与 `docs/reproduce/M2-memory.md`；确认
   `PYTHONPATH= .venv/bin/python -m pytest` 是唯一测试命令。
2. **先写 6 个测试文件 + config 扩展**：窗口（双预算/evicted 返回/超大单条）、
   摘要（fake complete/三级 prompt/截断/SummarizerError）、long_term（schema 幂等/
   消息 roundtrip/KV upsert 与读取计数/衰减公式比值/摘要 upsert 唯一）、
   retriever（四策略/超窗进摘要/finalize 三级链/full 时间衰减通道/重放隔离/
   core 外层接缝）、app（上下文注入 run_tool_loop）、integration（整文件 mark +
   无 key skip）。
3. 跑 `PYTHONPATH= .venv/bin/python -m pytest -q` 看 6 个 collection error 红。
4. 按依赖自底向上实现：`memory/models.py`（UTC/ISO 时间、日/周 key、策略枚举）
   → `window.py`（双预算 FIFO，evicted 只返回）→ `summarizer.py`（三级 prompt、
   duck complete、usage 归一化、截断）→ `long_term.py`（建表→消息→摘要 upsert→
   KV put/get/search）→ `retriever.py`（ingest/end_session/rollup/retrieve/
   replay）→ `app.py`（薄装配）→ `config.py` 记忆字段。
5. 重点复现三个接缝：
   - 超窗不丢：`ingest` 里 evicted → `_fresh_for_summary` → 增量会话摘要 →
     `_mark_summarized`；
   - 策略分叉只在 `retrieve`：none/window/window_summary/full 的通道组合；
   - 外层接入：`augment_question` 拼上下文 → `run_tool_loop`，loop/client 零改动。
6. 跑 `PYTHONPATH= .venv/bin/python -m pytest`，目标 90 passed、4 deselected；
   再跑 `-m integration` 确认无密钥 4 skipped。
7. 更新 README（进度表/设计取舍）、architecture.md（v0.2/5.2/存储表/ADR）、
   `.env.example`、`data/README.md`、`docs/reproduce/M2-memory.md`、本文件；
   停下汇报，等确认后再进 M3。
