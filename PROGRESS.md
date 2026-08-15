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
