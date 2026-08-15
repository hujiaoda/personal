# Personal Data Assistant 架构设计文档

- 版本：v0.1（M0，M1 评审时按环境决策修订）
- 状态：已确认；作为 M1~M5 的实现依据
- 约束：Python 3.10+（开发机 3.10.12 + uv；见 PROGRESS「环境决策」）；
  不用 LangChain 与重量级框架；模型走 DeepSeek API（OpenAI 兼容）；核心机制全部手写

---

## 1. 目标与边界

### 1.1 要解决的问题

1. 记忆整理与问答：喂入文章、笔记、聊天重点，系统整理成记忆；之后用大白话提问，
   能答出“我上周学了什么”“那篇关于 XX 的笔记讲了啥”。
2. 智能问数：用户有记账流水、背单词记录等 SQLite 小表；用大白话提问，系统自己
   查表结构、写 SQL、执行、算结果、解释。
3. 习惯记忆：系统记住用户用语习惯（如把“餐饮”说成“饭钱”），查询前自动改写，
   越用越懂用户。

### 1.2 非目标（明确不做）

多智能体、分布式、登录系统、追 benchmark 排名。不做向量数据库，不做
LangChain/LlamaIndex 等编排框架。

---

## 2. 总体设计原则

- **小而可替换**：每个模块只依赖协议（输入输出类型），模型、存储、检索都可替换；
  测试时用 fake 注入，不真实花钱调 API。
- **一切外呼三件套**：超时、重试、降级；任何外部调用失败都必须有 B 计划，
  最差也要返回结构化的中文错误，不允许进程崩溃。
- **先测后写**：每个里程碑先写 pytest 测试看它失败，再实现到通过。
- **可观测**：每次模型调用记录 token 用量、耗时、重试次数，M5 的“成本”评测
  直接复用这份日志。
- **M0 只立契约不写业务**：本阶段只建骨架、文档、最小工程配置和仓库契约测试。

---

## 模块划分

按依赖从底到上排列。模块路径为 M1~M5 的目标形态，M0 只建目录不写实现。

| 模块 | 目标路径 | 职责 | 主要输入 → 输出 |
| ---- | -------- | ---- | --------------- |
| 配置 `config` | `src/personal_data_assistant/config.py` | 读环境变量、校验必需项、给出超时/轮数/模型默认值 | 环境变量 → 配置 dataclass |
| 模型客户端 `llm.client` | `src/personal_data_assistant/llm/client.py` | 手写 OpenAI 兼容 HTTP 调用 DeepSeek；支持非流式/流式、超时、指数退避重试、用量记录 | messages, tools 描述 → 文本或工具调用（含 token 统计） |
| 提示词 `llm.prompts` | `src/personal_data_assistant/llm/prompts.py` | 集中管理系统提示词与 JSON 输出模板，版本化 | 上下文 → prompt |
| 工具协议 `tools.base` | `src/personal_data_assistant/tools/base.py` | 定义工具 schema：名称、描述、参数、执行函数；统一成功/失败回填格式 | 工具定义 → 执行结果字符串 |
| 工具注册 `tools.registry` | `src/personal_data_assistant/tools/registry.py` | 注册/查找/列出工具；循环据此拼接可用工具说明 | 工具对象 → 描述与分派 |
| 核心循环 `core.loop` | `src/personal_data_assistant/core/loop.py` | M1 核心：模型调用 → 解析动作 → 工具执行 → 结果回填 → 再调用模型，直到模型给出最终答案或达到最大轮数；支持流式输出 | 用户问题, 可用工具 → 最终答案, 轨迹日志 |
| 滑动窗口 `memory.window` | `src/personal_data_assistant/memory/window.py` | 保存最近 N 条消息/素材，超窗后淘汰并触发摘要 | 消息流 → 窗口内容 |
| 分层摘要 `memory.summarizer` | `src/personal_data_assistant/memory/summarizer.py` | 对窗口内容做层级压缩（L0 原文 → L1 段落摘要 → L2 主题摘要），旧信息只保留上层摘要 | 长文本 → 分层摘要 |
| 长期记忆 `memory.long_term` | `src/personal_data_assistant/memory/long_term.py` | 记忆块、摘要与向量的 SQLite 持久化 | 记忆对象 → 落库 |
| 检索 `memory.retriever` | `src/personal_data_assistant/memory/retriever.py` | 用 NumPy 手写余弦相似度取 top-k；无向量时回退关键词 | query, k → 记忆片段 |
| 数据访问 `data.sqlite` | `src/personal_data_assistant/data/sqlite.py` | 只读连接管理、安全 SQL 执行、错误捕获 | SQL → 行集/错误 |
| 表结构探查 `data.schema` | `src/personal_data_assistant/data/schema.py` | 读 `sqlite_master`，产出紧凑表结构摘要供模型使用 | 库文件 → schema 摘要 |
| 智能问数 `data.ask` | `src/personal_data_assistant/data/ask.py` | 编排：问题 → schema → 生成 SQL → 只读执行 → 错误回填修正 → 解释结果 | 大白话, 库文件 → 答案, SQL 轨迹 |
| 习惯记忆 `profile.habits` | `src/personal_data_assistant/profile/habits.py` | 记录“用户说法 → 标准词”映射与置信度；查询前改写；用户纠正时更新 | 用语证据 → 别名表 |
| 应用装配 `app` | `src/personal_data_assistant/app.py` | 把配置、模型、记忆、工具、循环组装成一个入口 | 配置 → 助手实例 |
| Web 接口 `api` | `src/personal_data_assistant/api/` | FastAPI 路由 + 极简静态页面；SSE 或分块输出流式答案 | HTTP 请求 → 回答/事件流 |
| 评测 `evals/` | `evals/` | 题集、判分、成本统计、报告；独立于业务代码 | 评测配置 → 指标报告 |

### 模块间的依赖规则

- `llm`、`memory`、`data`、`profile` 不得互相 import，只能由 `core` 与 `app` 装配。
- 工具实现只能调用 `memory`/`data`/`profile` 的公开函数，不得直接访问数据库文件。
- 所有可失败的边界（模型、磁盘、SQLite）都通过异常 + 降级结果传递，不打印后吞掉。

---

## 数据流

### 3.1 记忆入库流（喂素材）

```
文章/笔记/聊天重点
   │ 切块、去重（内容哈希）
   ▼
分层摘要器：原文(窗口 L0) → L1 摘要 → L2 主题摘要
   │
   ├── 原文块/摘要 → SQLite memory 表
   └── 文本 → embedding（API 成功用 API；失败降级本地 n-gram 哈希向量）
                        │
                        ▼
                 向量存 SQLite，检索时 NumPy 算余弦
```

### 3.2 记忆问答流（大白话问记忆）

```
用户问题
   ▼
习惯改写（“饭钱”→“餐饮”等）
   ▼
双通道召回：
   A. 滑动窗口最近消息精确扫描（快，覆盖近期）
   B. 长期记忆向量检索：embedding → NumPy 余弦 top-k（覆盖久远）
   ▼
拼接上下文（问题 + 命中的记忆片段 + 来源提示）
   ▼
核心循环：模型阅读上下文 → 直接作答（若信息不足则说明缺什么）
```

### 3.3 智能问数流（大白话问 SQLite 表）

```
用户问题
   ▼
习惯改写（术语、表名别名）
   ▼
表结构探查：sqlite_master → 紧凑 schema（表名/字段/类型/样例值）
   ▼
核心循环第 1 轮：模型拿到 schema + 问题 → 生成 SQL
   ▼
只读安全执行（白名单 SELECT/WITH，禁止多语句，带超时）
   ├─ 成功 → 结果行集
   └─ 失败 → 错误信息回填模型 → 自动修正 SQL → 再执行（最多 3 轮）
   ▼
最终结果交给模型生成人话解释（数字、口径、依据字段）
   ▼
若用户在下一轮纠正说法/口径 → 写回习惯表
```

### 3.4 核心循环通用形态（M1 实现，记忆与问数共用）

```
        ┌──────────────────────────────────────────────┐
        │               core.loop（状态机）             │
        ▼                                              │
 拼装 messages + 工具清单                               │
        ▼                                              │
 DeepSeek 调用（超时/重试/降级）                         │
        ▼                                              │
 解析响应                                              │
   ├── 最终答案 → 结束（可选流式逐字输出）                 │
   └── 工具调用 → 执行工具 → 结果回填 messages ──────────┘
                    （超过最大轮数 → 用已有信息强制收束）
```

---

## 4. 存储设计

M0 只设计不建表；M2/M3 由各自的迁移测试先锁行为，再写实现。

### 4.1 SQLite 文件划分

| 文件 | 用途 |
| ---- | ---- |
| `data/pda.db` | 系统记忆、摘要、向量、会话、习惯画像 |
| `data/user_tables.db` | 用户自己的小数据（记账、背单词等），智能问数只读访问 |
| `evals/*.db` | 评测专用库，与真实数据物理隔离 |

### 4.2 系统表（规划）

- `memory_chunks(id, content_hash, content, source, created_at)`
- `memory_summaries(id, level, parent_id, content, chunk_ids, created_at)`
- `embeddings(id, target_kind, target_id, model, dim, vector_blob)`
- `messages(id, session_id, role, content, tool_calls_json, token_usage, created_at)`
- `user_aliases(id, raw_term, canonical_term, kind, weight, evidence_count, updated_at)`
- `user_profile(key, value, updated_at)`
- `eval_runs(run_id, strategy, question_id, answer, judge, tokens, cost, latency_ms)`

### 4.3 取舍

- 向量以 `BLOB` 存 SQLite，不引入向量数据库：项目规模小，几千条向量用 NumPy
  全量算余弦即可，简单且满足“手写检索”的技术要求。
- 习惯、摘要都落 SQLite：可复现、可导出、可被评测单独打开。
- 用户小数据表与系统表分文件：问数工具永远拿不到系统库，避免误查和越权。

---

## 目录安排

M0 现在就建好的部分标 ✅；其余为 M1~M5 计划位置。

```
data-assistant/
├── README.md                         ✅ 项目说明、里程碑、纪律
├── PROGRESS.md                       ✅ 每步进度/坑/复现要点
├── pyproject.toml                    ✅ Python >=3.10,<3.13 + pytest 工程配置
├── .gitignore                        ✅ 忽略 .env、缓存、数据文件
├── .env.example                      ✅ 环境变量模板（不含真实密钥）
├── docs/
│   └── architecture.md               ✅ 本架构文档
├── src/personal_data_assistant/
│   ├── .gitkeep                      ✅ M0 占位（保留兼容 M0 契约）
│   ├── config.py                     ✅ M1 配置与默认值
│   ├── app.py                           M2 起装配入口
│   ├── llm/
│   │   ├── client.py                  ✅ M1 DeepSeek 客户端
│   │   └── prompts.py                 ✅ M1 提示词模板
│   ├── core/
│   │   └── loop.py                    ✅ M1 工具循环状态机
│   ├── tools/
│   │   ├── base.py                    ✅ M1 工具协议
│   │   ├── registry.py                ✅ M1 注册表
│   │   ├── memory_tools.py              M2 记忆读写工具
│   │   └── sql_tools.py                 M3 问数工具
│   ├── memory/
│   │   ├── window.py                    M2 滑动窗口
│   │   ├── summarizer.py                M2 分层摘要
│   │   ├── long_term.py                 M2 长期记忆
│   │   └── retriever.py                 M2 NumPy 余弦检索
│   ├── data/
│   │   ├── sqlite.py                    M3 只读 SQL 执行
│   │   ├── schema.py                    M3 表结构探查
│   │   └── ask.py                       M3 问数编排
│   ├── profile/
│   │   └── habits.py                    M3 习惯记忆
│   └── api/
│       ├── main.py                      M4 FastAPI 入口
│       ├── routes.py                    M4 接口路由
│       └── static/index.html            M4 极简页面
├── tests/
│   ├── test_project_skeleton.py      ✅ M0 仓库契约测试
│   ├── test_config.py                 ✅ M1 配置契约
│   ├── test_llm_client.py             ✅ M1（fake HTTP/SSE/重试/降级）
│   ├── test_core_loop.py              ✅ M1（脚本化 fake 模型）
│   ├── test_prompts.py                ✅ M1 协议提示词
│   ├── test_tools.py                  ✅ M1 工具协议与注册表
│   ├── test_integration_deepseek.py   ✅ M1 真实 API 冒烟（默认跳过）
│   ├── test_memory_*.py                 M2
│   ├── test_data_ask.py                 M3（固定 SQLite 测试库）
│   ├── test_profile_habits.py           M3
│   ├── test_api.py                      M4（TestClient）
│   └── test_evals.py                    M5
├── data/
│   ├── .gitkeep                      ✅ 数据目录占位
│   └── README.md                        M2 数据文件说明
└── evals/
    ├── .gitkeep                      ✅ 评测目录占位
    ├── questions/memory_50.json         M5 50 道记忆问答
    ├── questions/sql_questions.json     M5 SQL 题集
    ├── run_memory_eval.py               M5 记忆策略对比
    ├── run_sql_eval.py                  M5 SQL 准确率
    └── reports/                         M5 评测报告
```

---

## 5. 关键机制设计（各里程碑要点）

### 5.1 M1 核心循环

- 工具协议手写 JSON：模型输出
  `{"action":"tool","tool":"<工具名>","args":{...}}` 或
  `{"action":"final","answer":"..."}`；工具清单以 JSON Schema 写进 system prompt，
  自己解析，失败时把解析错误/未知工具/参数错误作为 user 消息回填模型重试。
  不依赖框架的 function-calling 封装，保证可观测、可降级。回填工具结果也走
  user 角色（`{"tool_result":...}`），不使用原生 `role=tool` + `tool_call_id`。
- 最大轮数默认 6，SQL 修正子循环默认 3 轮；可配置。
- 流式输出：优先 SSE 逐 token；若流式通道失败，自动降级为一次性返回完整结果。
- 测试策略：用脚本化 fake 模型（预设响应队列）测试循环分支，真实 DeepSeek 调用
  放 `@pytest.mark.integration`，默认不跑。

### 5.2 M2 记忆系统

- 滑动窗口：最近 20 条消息或 8k token 先到先出；被淘汰的内容先交给分层摘要器。
- 分层摘要：L0 原文块 → L1 段落摘要 → L2 主题摘要；查询优先用高层摘要定位，
  命中了再下钻原文，省 token。
- 长期记忆：摘要 + 原文块入库；向量检索用 NumPy 手写余弦相似度，不引入
  scikit-learn/向量库。
- embedding 双通道：优先调 OpenAI 兼容 embedding 端点；DeepSeek 当前若无可用
  embedding 端点，立即走本地降级——字符 3-gram + 哈希签名向量，同样用 NumPy
  余弦检索，保证 M2 不因模型能力缺口卡死。

### 5.3 M3 智能问数

- 先给模型紧凑 schema（表名、字段、类型、去标识的样例值），再让它写 SQL，
  不把整库内容塞进提示词。
- SQL 安全：只读连接；只允许 `SELECT`/`WITH` 开头；禁止多语句；默认 `LIMIT` 保护；
  执行带超时。
- 执行失败把“SQL + 错误信息”回填，模型修正后再执行；修正轮数用尽则对用户
  说明“这个问题我没查到，原因和试过的 SQL 如下”。
- 答案必须包含：结果数字、统计口径（用了哪张表哪个字段）、SQL 原文，便于用户核对。
- 习惯学习：当用户纠正“我说的饭钱就是餐饮”，写入 `user_aliases` 并加权重；
  下次问数前用最高权重别名改写问题。

### 5.4 M4 网页

- FastAPI 只做薄壳：HTTP 请求 → 参数校验 → 调 `app` → 流式返回；业务逻辑不在
  路由里写。
- 极简单页：一个输入框 + 流式回答区 + 工具调用折叠展示；无登录、无账号体系。
- 健康检查 `/health` 返回模型与数据库连通状态，失败也返回 200 + 状态字段，
  方便排查而不是直接 500。

---

## 降级与 B 计划

总原则：**任何对外调用失败都有下一手；再失败就给结构化中文错误，绝不崩溃。**

| 失效点 | 第一手 | B 计划 | 最后兜底 |
| ------ | ------ | ------ | -------- |
| DeepSeek 聊天 API | 连接/读超时（10s/30s），指数退避重试 2 次 | 切换非流式重试一次 | 返回“模型暂时不可用”+原因的结构化错误；若 SQL 已算出则直接格式化返回数值 |
| DeepSeek 流式响应 | SSE 逐 token | 自动改非流式一次拿全量 | 同聊天兜底 |
| embedding 端点 | 调 API 取向量 | 本地字符 3-gram 哈希向量（NumPy 余弦） | 纯关键词包含匹配 |
| 记忆检索空结果 | 扩大 k / 降低阈值再查一次 | 回退滑动窗口原文 | 明确告知“长期记忆未命中，只有近期窗口线索” |
| SQL 生成/执行失败 | 错误回填模型修正，最多 3 轮 | 换更小 schema（只给相关表）重问一次 | 返回“没查到 + 原因 + 试过的 SQL” |
| SQLite 锁或损坏 | busy_timeout 后重试 | 打开只读副本 | 返回明确错误，不动用户原始库 |
| FastAPI 启动时模型/数据库不可用 | 启动告警 | `/health` 报降级状态 | 请求返回 503 JSON 而非堆栈 |

配置默认值：HTTP 连接超时 10s、读取超时 30s、模型重试 2 次、工具循环 6 轮、
SQL 修正 3 轮、检索 top-k=8。全部集中在 `config.py`，评测可覆盖。

---

## 评测方案

### 6.1 记忆问答评测（M5）

- 题集：`evals/questions/memory_50.json`，50 道中文问题，覆盖时间（“上周学了
  什么”）、主题（“关于 XX 的笔记”）、数字细节、跨素材综合；每题带参考答案关键点。
- 喂入固定素材包（同一批文章/笔记/聊天，固定顺序），保证可复现。
- 三种策略对比：
  - S0 仅滑动窗口（窗口外的内容必须答不出，作为下界）
  - S1 滑动窗口 + 分层摘要
  - S2 完整系统：滑动窗口 + 分层摘要 + 长期记忆 + NumPy 检索（S2 为生产策略）
- 指标：
  1. 准确率：答案关键点覆盖率。主判分用 DeepSeek 固定 prompt 做 LLM-as-judge，
     同时提供规则判分脚本（关键词 + 数字匹配）兜底，避免“模型给自己打分”的偏差。
  2. 成本：每题输入/输出 token、检索注入 token、估算费用；汇总三种策略总成本。
  3. 召回：长期记忆检索命中率（应命中的题是否进了 top-k）。
  4. 延迟与重试次数。
- 输出：`evals/reports/memory_*.json` + 一张三策略对比表。
- 成本控制：DeepSeek 单题成本低；评测先跑小样本（10 题）验证流程，再跑全量；
  失败重试计入重试次数，不无限烧钱。

### 6.2 SQL 准确率评测（M5）

- 题集：`evals/questions/sql_questions.json`，每条含问题、表结构定义、标准答案
  （结果行集或聚合数值 + 浮点容差）、禁止事项（如禁止写库）。
- 指标：首次 SQL 可执行率、结果一致率、错误后自动修正成功率、平均修正轮数、
  token 成本；另设 5 道陷阱题（问法歧义、字段名陷阱），单独报告。
- 判分：执行结果与标准答案做行集比对（排序、浮点容差 1e-6）；每道题都跑在
  全新的临时 SQLite 测试库上，保证可复现。

### 6.3 评测与开发的关系

- 评测脚本本身也要有 pytest 测试（题集完整性、判分函数正确性）。
- M5 之前，M2/M3 的单元测试就是“小评测”：固定样例库、固定问题、断言答案关键点，
  不让回归积累到 M5。

---

## 7. 开发顺序与测试策略

1. M0：骨架 + 架构 + 契约测试（当前）。
2. M1：先写 `test_core_loop.py`（fake 模型脚本）→ 实现 `config/llm/core/tools`。
3. M2：先写记忆检索与摘要的样例库测试 → 实现 `memory` 各模块与工具。
4. M3：先写固定 SQLite 测试库的问数测试 → 实现 `data`、`profile`。
5. M4：先写 FastAPI TestClient 测试 → 实现路由与极简页面。
6. M5：先写题集校验与判分测试 → 跑评测 → 写完整 README。

每个里程碑结束都更新 `PROGRESS.md` 并停下来汇报，等确认后再继续。

---

## 8. 已记录的架构决策（ADR）

- ADR-1：用 `httpx` 手写 OpenAI 兼容请求，不引入 openai SDK。理由：完全掌控
  超时、重试、流式与 token 计量；协议只是 HTTP + JSON，不值得为它引入依赖。
- ADR-2：工具调用统一走“提示词约束的 JSON 动作协议”，不依赖 function-calling
  封装。协议为 `{"action":"tool","tool":"<名>","args":{...}}` 与
  `{"action":"final","answer":"..."}`，工具结果用 user 角色回填。理由：可观测、
  可 mock、可把解析错误回填模型自纠、模型不支持 function calling 时也能降级。
- ADR-3：向量存 SQLite BLOB + NumPy 余弦，不引入向量数据库。理由：规模小、
  满足手写检索要求、部署简单。
- ADR-4：用户小数据库只读 + SQL 白名单。理由：个人记账数据不可被模型幻觉
  写成灾难。
- ADR-5：评测数据与真实数据物理隔离。理由：评测可重复，不污染个人记忆。

---

## 9. 非目标重申

不做多智能体、不做分布式、不做登录系统、不追 benchmark 排名。本项目唯一
“排行榜”是 M5 自己三策略的对比表，目的是展示工程判断，不是刷分。
