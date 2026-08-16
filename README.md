# Personal Data Assistant（个人数据助手）

一个用于找实习的作品级项目：把平时收藏的文章、笔记、聊天重点喂给它，它整理成
可检索的个人记忆；再用大白话问它“我上周学了什么”“八月吃饭花了多少钱”，它能
查记忆、查 SQLite 小数据表、算结果并解释。它还会记住你的用词习惯，越用越懂你。

## 项目定位

- 模型：DeepSeek API（OpenAI 兼容接口），密钥放环境变量。
- 栈：Python 3.10+（开发机用 uv + Python 3.10）+ httpx + SQLite + pytest；
  M4 再引入 FastAPI。长期记忆检索手写时间衰减，不依赖向量库。
- 约束：不用 LangChain、不用 openai SDK，不用重量级框架；核心机制（工具循环、
  记忆、检索、Text-to-SQL）全部手写。
- 可靠性：所有对外调用带超时、重试和降级，失败必须有 B 计划，不许崩溃。

## 当前进度

| 里程碑 | 内容 | 状态 |
| ------ | ---- | ---- |
| M0 | 仓库骨架 + 架构设计文档 | 已完成 |
| M1 | 核心循环：模型调用 → 工具执行 → 结果回填 → 循环 | 已完成 |
| M2 | 记忆系统：滑动窗口 + 会话/日/周摘要 + SQLite + KV 时间衰减检索 + 四策略重放 | 已完成 |
| M3 | 智能问数：大白话 → 查表结构 → 写 SQL → 执行 → 自动修正 → 解释 | 未开始 |
| M4 | 网页：FastAPI 接口 + 极简页面 | 未开始 |
| M5 | 评测：50 道记忆问答 + 四种记忆策略对比 + SQL 准确率 + 完整 README | 未开始 |

## 目录速览

```
data-assistant/
├── docs/architecture.md   # 架构设计文档（模块、数据流、目录、评测）
├── src/personal_data_assistant/
│   ├── config.py          # M1/M2 配置与默认值（M2 增加记忆参数）
│   ├── app.py             # M2 装配入口（记忆作为 core 外层组件）
│   ├── llm/               # M1 手写 httpx 客户端 + JSON 动作协议提示词
│   ├── core/loop.py       # M1 工具调用循环状态机
│   ├── memory/            # M2 窗口 / 分层摘要 / SQLite / 时间衰减检索
│   └── tools/             # M1 工具协议 + 注册表
├── tests/                 # pytest 测试（先写测试，再写实现）
├── data/                  # SQLite 系统库、样例数据
├── evals/                 # 评测题集与评测脚本（M5）
├── pyproject.toml         # Python >=3.10,<3.13 工程配置
├── PROGRESS.md            # 每步的进度、坑、复现要点
└── .env.example           # 环境变量模板（复制为 .env 使用）
```

## 快速开始

```bash
cd /home/hujiao/projects/data-assistant
uv venv --python 3.10 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
cp .env.example .env   # 填入 DEEPSEEK_API_KEY
# 本机有 ROS 注入 PYTHONPATH，测试必须清空它，否则 pytest 会加载 ROS 插件报错：
PYTHONPATH= .venv/bin/python -m pytest
```

默认不跑真实 DeepSeek（integration 标记被排除）。确认 `.env` 已配置密钥后，
可以显式冒烟：`PYTHONPATH= .venv/bin/python -m pytest -m integration`。

## 设计取舍

### 为什么不用原生 function calling，两者区别

| 维度 | 原生 function calling（OpenAI tools 参数） | 本项目 JSON 动作协议 |
| ---- | ------------------------------------------- | -------------------- |
| 协议位置 | 请求体带 `tools`/`tool_choice`，响应带 `tool_calls`、`tool_call_id` | 工具 JSON Schema 写进 system prompt，模型输出 `{"action":"tool","tool":...,"args":...}` |
| 回填方式 | 必须用 `role=tool` 并按 `tool_call_id` 配对 | 用 `role=user` 回填 `{"tool_result":...}`，任何聊天模型都接受 |
| 可观测性 | 调用意图藏在 SDK/响应字段里，调试要翻请求日志 | 模型原话就是动作 JSON，轨迹一眼可读、可直接 mock |
| 可降级性 | 模型/服务不支持 function calling 时整条路断掉 | 不支持 function calling 的文本模型也能照常走通 |
| 失败恢复 | 格式错误通常由服务端/SDK 报错，难把错误回填给模型 | 解析失败、未知工具、参数错误都作为消息回填，模型可自行修正 |
| 可控性 | 工具 schema 与提示词由 SDK 混合编码 | 提示词、schema、解析、执行全在自己代码里，评测可逐段覆盖 |
| 成本 | 工具定义放在请求参数，通常不进计费 prompt | 工具 JSON Schema 占 prompt token，工具多时会变长 |
| 并行调用 | 部分服务端支持一次返回多个 tool_calls | M1 约定一次一个工具，换取简单稳定；后续需要再扩展 |

选择 JSON 动作协议的理由：本项目要“可观测、可 mock、可降级、不绑定 SDK”，
而不是追求最小 prompt 开销。M1 先锁住单工具调用；若 M3 问数暴露出并行查多表的
真实收益，再在协议里加批量动作，解析器与轨迹结构同步升级。

### 为什么 M2 长期记忆先做 KV 时间衰减，不做向量检索

M2 的目标是先把「记忆入库 → 分层压缩 → 可检索」的整条链路跑通，并为 M5
铺出四种可重放策略。长期记忆检索公式为
`相关性 × 权重 × exp(-decay_lambda × 年龄天数)`：只依赖标准库 SQLite，
离线可测、结果可解释，个人记忆库几千条以内完全够用。语义向量（embedding +
余弦）作为后续增强通道，替换时只动 `MemoryDatabase.search_memories` 内部，
返回的 KVMemory 形状不变。不引入 NumPy/向量库，也少一个平台依赖和降级分支。

### 为什么手写 httpx 而不是用 openai SDK

openai SDK 在超时、重试、流式与 token 计量上都有自己的一层默认行为，出问题时
很难定位是 SDK 还是服务端；本项目只需要 `POST /chat/completions` 一个端点，
用 httpx 手写一个客户端模块就能同时拿到：连接/读取超时、指数退避重试、SSE 拆包、
流失败自动降级非流式、usage 缺失时的粗估计量。协议只是 HTTP + JSON，
不值得为它引入依赖和黑盒。

## 开发纪律（全程遵守）

1. 测试先行：每个里程碑先写 pytest 测试，看它失败，再写实现让它通过。
2. 每完成一步更新 `PROGRESS.md`：进度、踩过的坑、复现要点。
3. 每个代码文件顶部写 3~5 行中文注释，说明设计取舍。
4. 对外调用必须可超时、可重试、可降级，禁止静默崩溃。
5. 不引入 LangChain 及任何重量级框架。

## 非目标（明确不做）

多智能体、分布式、登录系统、追 benchmark 排名。
