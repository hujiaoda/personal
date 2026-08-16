# M3 复现指南：智能问数（sql_query + 只读安全 + 自纠错 + 评测埋点）

> 这本指南对应 `data/sqlite.py`、`data/schema.py`、`data/ask.py`、
> `tools/sql_tools.py`、`profile/habits.py` 以及 `data/demo.py` 的中文演示数据。
> 复现透了，「Text-to-SQL、SQL 注入/越权防护、Agent 自纠错、评测指标设计」
> 四类面试题闭眼答。

## 本里程碑是什么

在 M1 核心循环和 M2 记忆系统之上，新增一个普通工具 `sql_query`：

**用户用大白话问数 → 先做习惯别名改写 → 外层循环调用 `sql_query` 工具 →
工具内探查表结构 → 模型生成 SQL → 只读安全执行 → 失败自动修正（最多 3 次）→
中文解释结果 → 工具结果回填外层循环。** 全程记下单次评测事实
（首次是否成功 / 修正是否成功 / 修正轮数 / token 成本），聚合留给 M5。

`core/loop.py`、`llm/client.py`、`memory/` 一行不改；记忆系统继续作为外层组件
正常工作。

## 先理解 7 个核心概念（复现的钥匙）

1. **sql_query 是普通工具，不是第二个 Agent**：`create_sql_query_tool()` 返回
   M1 的 `Tool(name="sql_query", ...)`。数据库路径在装配期锁死，模型的工具参数
   只有一个 `question`——不能让模型说“帮我查另一个库”。工具内部的
   `ask_database()` 才是问数子循环，它只依赖模型 `complete(messages)` 鸭子协议。
2. **SQL 安全是三层独立闸，不是只靠提示词**：
   - 白名单：把前导空白和 SQL 注释剥掉，第一条（唯一一条）语句必须以
     `SELECT`/`WITH` 开头；多语句直接拒绝。
   - 只读连接：SQLite URI `file:...?mode=ro` + `PRAGMA query_only=ON`。这条闸
     专门兜住 `WITH x AS (...) DELETE FROM t` 这种“CTE 前缀 + 写操作”的漏网形态。
   - 资源闸：progress handler 执行超时（默认 5s）+ `fetchmany(max_rows+1)`
     行数上限（默认 100），长查询/笛卡尔积不会拖死进程。
3. **分号计数必须理解 SQL 词法，不能 `str.count(";")`**：`SELECT 'a;b'` 的分号在
   字符串里，`-- 注释;` 的分号在注释里，`[a;b]` 在方括号标识符里。实现用状态机
   遍历 normal/single/double/backtick/bracket/line/block 七种状态，只有 normal
   状态下的裸分号才是语句边界。
4. **schema 给“最小可用上下文”，样例值先脱敏**：`sqlite_master` + 表值函数
   `pragma_table_info(?)` 产出紧凑 JSON：表名、字段名、声明类型、主键、每列最多
   3 个样例。邮箱/11 位手机号打码，但金额、日期、时长原样保留——这些数字正是
   模型判断“餐饮求和”口径的依据。不把整库内容塞进 prompt。
5. **修正子循环的账要算清**：首次生成 SQL 不计入修正；每失败一次，把
   “失败 SQL + 真实错误”回填模型重写，`max_fix_rounds=3` 表示最多重写 3 次，
   所以最多执行 4 次 SQL。修正轮数用尽后返回“原因 + 试过的 SQL 列表”，
   绝不把失败包装成成功。首次成功记为 `first_attempt_success=True`。
6. **解释失败不推翻已查到的数据**：数据已经拿到后，解释模型超时/崩溃只影响
   “人话层”，`format_result_fallback()` 用确定性模板输出结论数字、统计口径和
   SQL 原文，状态仍是 success。这就是架构文档“SQL 已算出则直接格式化返回”的
   B 计划。
7. **评测埋点 M3 只记事实，M5 才算指标**：`AskResult` 只保存单次
   `first_attempt_success`、`fix_success`、`total_fix_rounds`、`model_calls`、
   usage、`attempts_log`；不做成功率/平均修正轮数。这样 M5 对一批题聚合时，
   分母、口径都能重新定义，不会被写死的算法坑。

## 数据流（对着源码走一遍）

### 工具装配与主循环接缝

```
create_sql_query_tool(model=..., db_path=..., max_fix_rounds=3, ...)
  → Tool(name="sql_query", parameters={"question": string})
  → ToolRegistry.register(...)
  → run_tool_loop(question, registry, outer_model)   # M1 循环零改动
       outer_model 输出 {"action":"tool","tool":"sql_query","args":{...}}
       tool.execute(args)
         → ask_database(args["question"], db_path, model, ...)
         → ToolResult(ok=True/False, result=AskResult.to_dict(), error=...)
       run_tool_loop 把工具结果以 user 角色回填 → outer_model 输出 final
```

### ask_database 子流程

```
ask_database(question, db_path, model, max_fix_rounds=3)
  ├─ discover_schema(db_path)
  │    ReadOnlySQLite.execute_query("SELECT ... FROM sqlite_master ...")
  │    → 每表/视图 pragma_table_info → 每列最多 3 个样例（脱敏/截断）
  │    → render_schema_summary() 紧凑 JSON
  ├─ loop（首次 + 最多 3 次修正）:
  │    生成：model.complete(build_sql_generation_messages(...))
  │          或 build_sql_fix_messages(... 失败 SQL + 错误 ...)
  │    提取：extract_sql_text()（去围栏 / 解 JSON / 找 SELECT|WITH）
  │    执行：ReadOnlySQLite.execute_query(sql)
  │      ├─ 成功 → attempts_log 记 ok=True → 退出循环
  │      └─ 失败（安全/语法/缺表/超时）→ attempts_log 记 ok=False
  │           → fix_rounds < 3 ? 继续循环 : 返回 failed + 试过的 SQL
  ├─ 成功后的解释：model.complete(build_explanation_messages(...))
  │      ├─ 正常 → 中文解释（数字 + 口径 + SQL）
  │      └─ 异常/空 → format_result_fallback(...) 确定性兜底
  └─ AskResult(answer, sql, columns, rows, row_count, usage, attempts_log, ...)
```

### 习惯别名（加分项，复用 M2 KV）

```
HabitAliasStore(kv_backend=manager.db)
  record_alias("饭钱", "餐饮")
    → key="sql_alias:饭钱", value="餐饮", category="sql_alias", weight=1
    → 同一说法再记录一次 weight+1（封顶 5）
  rewrite_question("我八月饭钱花了多少")
    → list_memories() 过滤 category=sql_alias 且 key 前缀 sql_alias:
    → 排序：长说法优先 → 高权重优先 → 稳定字符串
    → "我八月餐饮花了多少"
app.ask() 的顺序：habit 改写 → memory.augment_question → run_tool_loop
```

## 复现步骤（浓缩版，细节见 PROGRESS.md 的复现要点）

1. 读 `docs/architecture.md` 5.3 与 M1/M2 复现指南；确认唯一测试命令
   `PYTHONPATH= .venv/bin/python -m pytest`。
2. **先写 6 个 M3 主测试文件**：
   - `test_data_sqlite.py`：SELECT/WITH 白名单、前导注释、字符串内分号、
     写操作拒绝、多语句拒绝、递归 CTE 超时、行数截断、缺失库错误；
   - `test_data_schema.py`：表/视图发现、字段类型与主键、样例值、邮箱/手机脱敏、
     样例截断、紧凑 JSON 渲染；
   - `test_data_ask.py`：fake complete 队列测首轮成功、失败修正一次、写操作拒绝
     后修正、修正 3 次耗尽、超时修正、解释失败兜底、模型崩溃、空结果、评测字段；
   - `test_data_demo.py`：三张演示表字段/行数/中文真实感/幂等重建；
   - `test_sql_tools.py`：工具 schema 与注册、结构化回填、core 循环零改动接缝、
     PersonalAssistant 同时装配记忆与 sql_query；
   - `test_integration_sql.py`：整文件 `integration` + 无 key skip。
   同时给 `test_config.py` 增加 `sql_user_db_path/sql_query_timeout/sql_row_limit/
   sql_schema_sample_size` 默认值与非法值用例。
   主流程转绿后，加分项再补 `test_profile_habits.py`（别名落 KV、重复加权、
   长说法优先、app 改写顺序）并单独看它红一次。
3. 跑测试看红：主流程应为 **6 个 collection error**（data 包、tools.sql_tools
   不存在）；profile 加分项随后单独 1 个 collection error。
4. 按依赖自底向上实现：
   `data/sqlite.py`（白名单/只读/超时）→ `data/schema.py`（探查/脱敏/渲染）→
   `data/demo.py`（演示数据）→ `data/ask.py`（子循环/修正/解释/埋点）→
   `tools/sql_tools.py`（Tool 包装）→ `app.py`（注册 sql_query + 习惯改写）→
   `config.py`（M3 默认值）；加分项最后写 `profile/habits.py`。
5. 重点复现三个接缝：
   - `ReadOnlySQLite.execute_query`：validate → 惰性只读连接 → progress handler
     （记得取消 handler 要传 n）→ fetchmany；
   - `ask_database`：attempts_log 与 fix_rounds 同步，模型调用计数含失败的
     解释调用，usage 只在成功响应上累加；
   - `app.ask`：先 habit 改写，再 memory augment，最后 run_tool_loop；
     M1/M2 模块本身不动。
6. 跑 `PYTHONPATH= .venv/bin/python -m pytest`，目标 **159 passed,
   5 deselected**；再跑 `-m integration`，无密钥应为 **5 skipped**。
7. 生成演示库：`PYTHONPATH= .venv/bin/python scripts/seed_user_tables.py`。
8. 更新 README、architecture.md、`.env.example`、`data/README.md`、
   `PROGRESS.md`；最后写本指南。

## 自测题（不看代码答一遍，错了就回去看）

1. `validate_readonly_sql` 为什么不能只写 `sql.strip().upper().startswith("SELECT")`？
   请各举一个它会漏放、会误杀的例子；`WITH ... DELETE FROM t` 最后被哪一道闸拦住？
2. `ReadOnlySQLite.execute_query` 为什么不直接改写用户 SQL 拼 `LIMIT 100`，
   而是 `fetchmany(max_rows+1)` 再截断？两种做法各有什么坑？
3. `max_fix_rounds=3` 时，一条坏 SQL 最多被执行几次？`total_fix_rounds`、
   `attempts`、`first_attempt_success`、`fix_success` 四者怎么从
   `attempts_log` 推出来？
4. 解释模型在第 2 次调用时抛异常，`AskResult.status` 是什么？`model_calls`
   是多少？答案从哪来？为什么不能把 status 改成 failed？
5. `HabitAliasStore` 为什么把别名存在 KV 的 `sql_alias:*` key 而不是新建
   `user_aliases` 表？“饭钱→餐饮”和“饭→米饭”同时存在时，改写顺序依据什么？

## 面试预演

1. 你做了一个 Text-to-SQL Agent，模型幻觉出一条 `DELETE` 怎么办？讲讲你的
   白名单、只读连接、多语句检测分别防什么，为什么单靠 prompt 不够。
2. SQL 执行失败后你怎么让模型自纠错？错误信息里会带什么？最多修几次？
   如果修了 3 次还失败，用户看到什么？为什么这是比“无限重试”更好的产品决策？
3. 大表上一条 `SELECT *` 可能把进程内存打爆，也可能跑 30 秒。你的执行超时和
   行数保护具体怎么实现？progress handler 的原理和局限是什么？
4. M5 要评测“首次成功率、修正成功率、平均修正轮数、token 成本”，你现在代码里
   已经埋了哪些字段？为什么 M3 不直接算比率，留到 M5 再聚合？
5. 这个项目的记忆系统和问数工具是怎么并存的？如果用户说“我八月饭钱花了多少”，
   从 `app.ask()` 进去到最终答案，经过哪些组件？哪些组件是 M1/M2 一行没改的？
