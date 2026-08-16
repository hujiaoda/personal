# data 目录说明

- `pda.db`：系统记忆库（默认路径，可通过 `PDA_MEMORY_DB_PATH` 修改）。
  包含 `messages`（会话消息）、`summaries`（会话/日/周三级摘要）、
  `kv_memories`（key-value 长期记忆，带时间衰减检索）。
- `user_tables.db`：M3 智能问数使用的用户小数据表（记账、背单词等），
  与系统库物理隔离；问数工具永远只读访问这里。
- 评测数据放在 `evals/*.db`，绝不写入本目录的真实库。
- 数据文件已被 `.gitignore` 忽略；重新生成方式见 `docs/reproduce/M2-memory.md`。
