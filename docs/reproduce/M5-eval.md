# M5 复现指南：评测体系（记忆四策略 + SQL 准确率 + 汇总报告）

> 这本指南对应 `evals/` 下的题集、runner、判分、报告生成器，以及
> `tests/test_evals.py`。复现透了，「怎么给 Agent 项目搭一套可重复的离线评测」
> 这类面试题闭眼答。

## 本里程碑是什么

给 M2 记忆系统和 M3 智能问数各搭一套**不依赖真实 API 的评测**，并汇成一份
markdown 报告：

- 记忆：56 道中文问答，固定素材包（48 条消息 + 8 条 KV 长期记忆），用 M2
  `replay_memory_strategies()` 对同一批对话重放 none/window/window_summary/full
  四种策略，产出命中率与 token 成本对比。
- SQL：25 道问数题（含 5 道陷阱题），每题跑在全新临时演示库上，用脚本化 fake
  模型保证首轮 SQL、修正 SQL、解释文案全部确定；产出结果一致率、首次成功率、
  修正成功率、平均修正轮数与成本。
- 汇总：`evals/reports/M5-eval.md`，指标表格 + 结论段落 + 局限说明。

## 先理解 7 个核心概念（复现的钥匙）

1. **评测与单测分层，但共用同一纪律**：`tests/` 锁行为契约（这个函数不能返回
   什么、那个字段必须存在）；`evals/` 锁端到端指标（56 道题里命中多少、修正
   成功多少）。两者都离线、可重复、零密钥依赖；真实 DeepSeek 是可选第二档。
2. **记忆命中率 = 参考要点全覆盖**：每题有多个 `reference_points`，每个要点带
   `evidence`（需要哪些通道：window/summary/kv）与 `evidence_texts`（上下文里
   必须出现的原文证据）。答案覆盖该题全部要点才计 1 次命中；命中率是严格口径，
   不是“沾边得分”。
3. **四策略的可比性来自重放隔离**：`replay_memory_strategies()` 为每个策略建
   独立 SQLite 库（`memory_<strategy>.db` 先 unlink 再重放），同一批消息、同一
   个固定 `now`、同一个 KV setup。所以策略差异只来自检索通道，不来自数据残留。
4. **离线记忆 oracle 测的是“检索完备性上界”**：`ContextOracleAnswerModel` 只
   从拼进 prompt 的上下文里找证据，找齐才把参考要点写进 final 答案。它模拟的是
   “一个 100% 听话、只使用上下文、不幻觉”的模型，因此 full 拿到 100% 不是
   “模型满分”，而是“full 的通道覆盖全部题”。这个口径必须写进报告，否则会被
   面试官质疑指标注水。
5. **SQL 判分是行集比对，不是 SQL 文本比对**：真实模型可能写
   `SELECT a, b FROM t` 或 `SELECT b, a FROM t`，只要结果行集相同都算对。
   实现是：排序后逐行比，数值列 `math.isclose`（容差 1e-4），非数值转字符串比。
6. **陷阱题有两层判据**：非只读题不仅要看 answer，还要做 before/after 全库
   快照，证明 `DELETE` 被白名单挡回后数据一个字节没变；时间越界题标准答案是
   0 行，考察模型是否“没数据就编数据”。
7. **成本统计只算答题阶段，且公式统一**：记忆评测把“检索注入 token”单独列，
   模型 prompt/completion 用 M1 同一套 UTF-8/4 粗估；SQL 评测聚合
   `SqlEvalRecord` 里的 token 字段。价格按 deepseek-chat 约 ¥2/百万输入、
   ¥8/百万输出估算。摘要生成成本与答题成本分开口径，不在同一张表混加。

## 数据流（对着源码走一遍）

### 记忆评测 `evals/memory_eval.py`

```
memory_50.json
  ├─ corpus（48 条 Message，最近 20 条在 window 内）
  ├─ kv_memories（8 条，只进 full 通道）
  └─ questions（56 道，reference_points + expected_strategies）
        │
        ▼
每题调 replay_memory_strategies(messages, question, strategies=ALL)
  ├─ none             → 空上下文
  ├─ window           → 滑动窗口原文
  ├─ window_summary   → 窗口原文 + 会话/日/周摘要
  └─ full             → 窗口 + 摘要 + KV 时间衰减 top-k
        │ 各自独立 SQLite
        ▼
ContextOracleAnswerModel.complete(messages)
  └─ 从 augment_question 的上下文块找 evidence_texts → 拼要点 → JSON final
        ▼
run_tool_loop(augmented_question, ToolRegistry([]), oracle)   # 走 M1 真实路径
        ▼
evaluate_answer(answer, question) → 命中 / 覆盖率 / token
        ▼
四策略聚合 → memory_eval_results.json
```

### SQL 评测 `evals/sql_eval.py`

```
sql_questions.json
  ├─ 25 题，每题 first_sql / fix_sqls / explanation / expected_rows
  └─ trap_type（non_readonly / missing_table / time_range_out_of_bounds /
                ambiguous_column / nonexistent_column）
        │
        ▼
每题：unlink 旧库 → build_demo_tables(temp/<id>.db) → before 快照
        ▼
ScriptedSQLModel.complete(messages)
  ├─ “实际执行的 SQL”/“请按系统要求生成中文解释” → explanation
  ├─ “执行失败” + 还有 fix_sqls             → 下一条修正 SQL
  └─ 首次调用                                 → first_sql
        ▼
ask_database(question, db, fake_model)     # M3 真实子流程：schema→SQL→执行→修正→解释
        ▼
after 快照 + rows_equal(result.rows, expected_rows)
        ▼
SqlEvalRecord → 聚合首次成功率 / 修正成功率 / 平均修正轮数 / token 成本
```

### 报告 `evals/reporting.py`

```
run_all_evals.py
  ├─ run_memory_eval() → evals/reports/memory_eval_results.json
  ├─ run_sql_eval()    → evals/reports/sql_eval_results.json
  └─ build_final_report(memory, sql) → evals/reports/M5-eval.md
         ├─ 一、记忆评测：命中率表 + token 成本表 + 结论
         ├─ 二、SQL 评测：总指标表 + 陷阱题明细 + 结论
         └─ 三、总结论 + 四、局限与后续
```

## 复现步骤（浓缩版，细节见 PROGRESS.md 的复现要点）

1. 读 `docs/architecture.md` 5.5/6.1/6.2/ADR-9 和本指南；确认唯一测试命令
   `PYTHONPATH= .venv/bin/python -m pytest`。
2. **先写 `tests/test_evals.py`**，锁 7 件事：
   - 记忆题集 ≥50、消息 ≥40、KV ≥5、id 唯一、每题参考要点与预期策略合法；
   - SQL 题集 ≥20、陷阱 ≥5 且包含非只读/缺表/时间越界三类；
   - 记忆 runner 实际命中数 == 题集预期数，跑两次结果一致；
   - SQL runner 首轮/修正/平均修正轮数与题集脚本序列一致，跑两次结果一致；
   - 非只读陷阱 before/after 快照一致；
   - `rows_equal` 的排序与浮点容差行为；
   - 汇总 markdown 含记忆/SQL 指标表与结论段落。
   先跑应看到 **1 个 collection error**（`No module named 'evals.memory_eval'`），
   这是红阶段证据。
3. 按依赖顺序实现 `evals/`：
   - 先用手写脚本生成两份题集 JSON：记忆素材要先算好“最近 20 条”的窗口边界，
     旧事实与近期事实不能有相同的 evidence 原文，否则 window 会“作弊”命中；
   - `memory_eval.py`：题集加载 → Message 构建 → KV setup → 保真摘要模型 →
     oracle 模型 → 规则判分 → 四策略聚合；
   - `sql_eval.py`：题集加载 → `ScriptedSQLModel` → `rows_equal` → 独立临时库 +
     快照 → `ask_database` → 聚合；
   - `reporting.py` 与三个 CLI 脚本。
4. 跑 `PYTHONPATH= .venv/bin/python -m pytest`，目标 **179 passed,
   5 deselected**；再跑 `-m integration`，无密钥应为 **5 skipped**。
5. 跑 `PYTHONPATH= .venv/bin/python evals/run_all_evals.py`，确认生成
   `evals/reports/` 下两份 JSON + `M5-eval.md`；核对报告数字与测试里从题集
   推导出的预期完全一致。
6. 更新 README（最终版：mermaid 架构图、演示占位、指标总表）、
   `docs/architecture.md`（v0.5/5.5/ADR-9）、`docs/reproduce/README.md`、
   `PROGRESS.md`；最后写本指南。

## 自测题（不看代码答一遍，错了就回去看）

1. 记忆命中率为什么用“全部参考要点覆盖”而不是“关键词出现即命中”？这样定义后，
   一道跨三通道的题在 window_summary 和 full 下分别会怎样？
2. `replay_memory_strategies` 里每个策略库先 `unlink(missing_ok=True)` 的意义
   是什么？如果忘记 unlink，第二次跑指标会怎么污染？
3. `ContextOracleAnswerModel` 为什么要把问题文本和上下文块分开？如果把 evidence
   匹配直接做在整条 user 消息上，none 策略的 0% 下界会被什么破坏？
4. SQL 判分为什么用行集比对而不是 SQL 文本比对？`rows_equal` 对
   `[("餐饮", 33.0)]` 与 `[("餐饮", 33.0 + 1e-7)]` 在容差 1e-6 下为什么相等？
5. 非只读陷阱题的两个判据是什么？如果只判 answer 不判快照，可能漏掉什么事故？

## 面试预演

1. 你要给一个 LLM Agent 项目做评测体系，会分几层？为什么单元测试通过不等于
   端到端指标可信？你的评测怎么做到别人没有 API key 也能跑出一模一样的数字？
2. 记忆增强有 none/window/window_summary/full 四档，你会设计怎样的题集分布
   来证明每一档的边际收益？token 成本如何分摊才公平，哪些成本必须单列？
3. 如果老板说“评测用 fake 模型没有说服力”，你怎么回答？什么时候 fake 评测
   比真模型评测更可信？接真实模型时你的哪部分代码不用改？
4. Text-to-SQL 的“首次成功率”和“修正成功率”分母分别是什么？为什么不能把
   修正成功的题也计进首次成功？平均修正轮数为什么用总题数做分母而不是只用
   需要修正的题？
5. 你怎么防止评测集腐化（模型刷分、指标只涨不跌）？题集、判分、报告三者的
   职责如何拆分？如果要加 5 道新陷阱题，需要改哪几个文件、补哪类测试？
