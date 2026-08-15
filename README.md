# Personal Data Assistant（个人数据助手）

一个用于找实习的作品级项目：把平时收藏的文章、笔记、聊天重点喂给它，它整理成
可检索的个人记忆；再用大白话问它“我上周学了什么”“八月吃饭花了多少钱”，它能
查记忆、查 SQLite 小数据表、算结果并解释。它还会记住你的用词习惯，越用越懂你。

## 项目定位

- 模型：DeepSeek API（OpenAI 兼容接口），密钥放环境变量。
- 栈：Python 3.12 + SQLite + NumPy（手写余弦检索）+ FastAPI + pytest。
- 约束：不用 LangChain，不用重量级框架，核心机制（工具循环、记忆、检索、
  Text-to-SQL）全部手写。
- 可靠性：所有对外调用带超时、重试和降级，失败必须有 B 计划，不许崩溃。

## 当前进度

| 里程碑 | 内容 | 状态 |
| ------ | ---- | ---- |
| M0 | 仓库骨架 + 架构设计文档 | 进行中 |
| M1 | 核心循环：模型调用 → 工具执行 → 结果回填 → 循环 | 未开始 |
| M2 | 记忆系统：滑动窗口 + 分层摘要 + 长期记忆 + 检索 | 未开始 |
| M3 | 智能问数：大白话 → 查表结构 → 写 SQL → 执行 → 自动修正 → 解释 | 未开始 |
| M4 | 网页：FastAPI 接口 + 极简页面 | 未开始 |
| M5 | 评测：50 道记忆问答 + 三种记忆策略对比 + SQL 准确率 + 完整 README | 未开始 |

## 目录速览

```
data-assistant/
├── docs/architecture.md   # 架构设计文档（模块、数据流、目录、评测）
├── src/personal_data_assistant/  # 后续业务代码（M1 起填充）
├── tests/                 # pytest 测试（先写测试，再写实现）
├── data/                  # SQLite 数据、向量、样例数据
├── evals/                 # 评测题集与评测脚本（M5）
├── pyproject.toml         # Python 3.12 工程配置
├── PROGRESS.md            # 每步的进度、坑、复现要点
└── .env.example           # 环境变量模板（复制为 .env 使用）
```

## 快速开始（M0 只验证骨架）

```bash
cd /home/hujiao/projects/data-assistant
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env   # 填入 DEEPSEEK_API_KEY
pytest
```

M0 没有业务代码，`pytest` 只验证仓库契约：目录、文档、配置是否齐全。

## 开发纪律（全程遵守）

1. 测试先行：每个里程碑先写 pytest 测试，看它失败，再写实现让它通过。
2. 每完成一步更新 `PROGRESS.md`：进度、踩过的坑、复现要点。
3. 每个代码文件顶部写 3~5 行中文注释，说明设计取舍。
4. 对外调用必须可超时、可重试、可降级，禁止静默崩溃。
5. 不引入 LangChain 及任何重量级框架。

## 非目标（明确不做）

多智能体、分布式、登录系统、追 benchmark 排名。
