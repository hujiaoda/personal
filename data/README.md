# data 目录说明

- `pda.db`：系统记忆库（默认路径，可通过 `PDA_MEMORY_DB_PATH` 修改）。
  包含 `messages`（会话消息）、`summaries`（会话/日/周三级摘要）、
  `kv_memories`（key-value 长期记忆，带时间衰减检索；M3 习惯别名也存这里，
  key 形如 `sql_alias:饭钱`、category=`sql_alias`）。
- `user_tables.db`：M3 智能问数的中文演示库，包含三张贴近生活的表：
  - `expenses`：记账流水（32 行；日期/类别/项目/金额/支付方式/备注）
  - `study_logs`：学习记录（26 行；日期/科目/主题/时长/渠道/完成/笔记）
  - `movie_logs`：观影记录（24 行；观看日期/片名/类型/年份/评分/时长/平台/短评）
  问数工具只以只读方式打开它；重新生成命令：
  `PYTHONPATH= .venv/bin/python scripts/seed_user_tables.py`。
- 评测数据放在 `evals/*.db`，绝不写入本目录的真实库。
- 数据文件已被 `.gitignore` 忽略；生成器在 `src/personal_data_assistant/data/demo.py`。
