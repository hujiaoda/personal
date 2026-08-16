# 复现指南索引

> 这是「复现手册（reproduce-playbook.md）」的配套目录：每个里程碑一本独立指南。
> 用法：学完对应知识后（比如 LangChain 学完），按 M0 → M5 顺序，每本都走一遍
> 「读 → 关 → 写 → 测 → 比 → 记」六步。每本末尾的自测题，就是将来面试的预演。

| 里程碑 | 指南 | 主题 | 复现前置知识 |
|---|---|---|---|
| M0 | [M0-skeleton.md](./M0-skeleton.md) | 仓库骨架与契约测试 | 会 git 基础、用过 pytest 即可 |
| M1 | [M1-core-loop.md](./M1-core-loop.md) | Agent 核心循环 | 懂 HTTP 请求、JSON、Python 异常处理 |
| M2 | [M2-memory.md](./M2-memory.md) | 记忆系统 | 略懂 SQLite 与检索/时间衰减概念（LangChain RAG 部分） |
| M3 | [M3-sql.md](./M3-sql.md) | 智能问数 | SQL 基础（SELECT/JOIN/GROUP BY） |
| M4 | [M4-web.md](./M4-web.md) | 应用层 | FastAPI 入门 |
| M5 | [M5-eval.md](./M5-eval.md) | 评测体系 | 会算准确率/平均数即可 |

> M0/M1/M2 指南已生成；M3~M5 由对应里程碑的会话在验收时生成。
> 每本指南末尾的「面试预演」题，学完就口头回答一遍，答不上就回去重读代码。
