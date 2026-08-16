# M0 复现指南：仓库骨架与契约测试

## 本里程碑是什么

没有任何业务代码。目标只有一个：把「项目该怎么长」的规矩立起来——
目录结构、文档纪律（PROGRESS 三段式）、配置声明、密钥安全、以及
**用测试来锁住这些规矩**（仓库契约测试）。

## 核心概念（复现前理解这 4 个）

1. **TDD 红绿循环**：先写会失败的测试（红），再写代码让它通过（绿）。
   本里程碑的红证据是「7 项全失败」。
2. **契约测试**：测试不测逻辑，只测「约定」——文件在不在、文档章节齐不齐、
   密钥模板里有没有真密钥。好处：文档改内容不会误伤测试。
3. **`.gitignore` 与密钥安全**：`.env`（真实密钥）、`.venv/`、`__pycache__/`、
   `.pytest_cache/` 永远不进 git；`.env.example` 只放空模板。
4. **环境决策要落档**：为什么放弃 3.12 用 3.10、为什么测试命令必须清空
   `PYTHONPATH`（ROS 污染），这些决定写进 PROGRESS 而不是口头。

## 复现步骤

1. `mkdir -p src/personal_data_assistant tests docs data evals`，空目录放 `.gitkeep`
2. 先写 `tests/test_project_skeleton.py`，断言 7 件事：
   目录存在 / README 有 M0~M5 / 架构文档 5 章节 / PROGRESS 三段式 /
   pyproject 声明 Python 与 pytest / .gitignore 三项 / .env.example 有键无真密钥
3. 跑 `PYTHONPATH= .venv/bin/python -m pytest -q` → 确认全红
4. 补齐 `README.md`、`docs/architecture.md`、`PROGRESS.md`、`pyproject.toml`、
   `.gitignore`、`.env.example` → 再跑 → 7 passed
5. `git add -A && git commit -m "M0: ..."`

## 面试预演（能口头答出来才算复现成功）

1. 为什么用测试锁文档结构，而不是靠自觉？如果文档每周都改，测试会怎样？
2. 真密钥如果被 commit 进了历史，删掉文件就安全了吗？（提示：要清历史）
3. 为什么 pyproject 里 Python 版本从 3.12 改成了 3.10+？这个改动影响了哪些文件？
