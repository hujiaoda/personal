# M2 复现指南：记忆系统（滑动窗口 + 分层摘要 + 长期记忆）

> 这本指南对应 `memory/` 包的 5 个模块 + `app.py` 装配入口。
> 复现透了，「短期记忆/长期记忆、记忆压缩、RAG 检索、多策略评测」四类
> 面试题闭眼答。

## 本里程碑是什么

`memory/window.py` + `memory/summarizer.py` + `memory/long_term.py` +
`memory/retriever.py` + `memory/models.py` + `app.py`，实现：

**消息入窗口 → 超窗不丢、先压成会话摘要 → 按天/周逐层合并 → 全部落 SQLite
→ 回答问题时按策略（none/window/window_summary/full）拼上下文 → 作为外层组件
把上下文喂给 M1 核心循环。**

M1 的 `core/loop.py`、`llm/client.py` 一行不改；记忆完全在循环外面接入。

## 先理解 6 个核心概念（复现的钥匙）

1. **滑动窗口 = 双预算 FIFO**：最近 20 条消息或 8k token，先到先出。窗口本身
   只做一件事：`add()` 返回被淘汰消息，**不负责摘要、不负责落库**。谁调用窗口，
   谁处理 evicted——这个接缝是“超窗不直接丢”的实现关键。
2. **分层摘要的三级语义**：会话级（session）压掉寒暄保留事实 → 日级（daily）
   合并当天多个会话 → 周级（weekly）合并一周的日级。period_key 分别是
   `YYYY-MM-DD` 和 `2025-W33`（ISO 周），每层都可以独立 rollup 和单测。
3. **增量更新会话摘要**：一条会话可能多次超窗。每次超窗时把“已有会话摘要 +
   新淘汰消息”一起交给 LLM 更新，而不是每条消息单独生成一个摘要。已摘要过的
   消息 id 要记在 manager 里，避免结束会话后再被淘汰时重复摘要。
4. **时间衰减检索公式**：
   `score = 文本相关性 × weight × exp(-decay_lambda × 年龄天数)`。
   相关性是“查询 token 被目标文本覆盖的比例”；中文同时取单字 + 2-gram，
   否则“我学了什么”这种短查询连“学”都匹配不到。`decay_lambda=0.05` 表示
   记忆每过约 14 天相关性减半。
5. **策略是显式枚举，不是 if 堆在检索里**：
   `none` 不注入、`window` 只注入近期原文、`window_summary` 加三层摘要、
   `full` 再加 KV 长期记忆。四条策略共享同一条 ingest 路径，只在 `retrieve`
   分叉；因此同一批对话可以用 `replay_memory_strategies` 建四个独立 SQLite 库
   重放，M5 直接复用。
6. **外层组件接缝**：`MemoryManager.augment_question(question)` 把检索证据拼成
   “上下文 + 用户问题”，再交给 `run_tool_loop`。循环的模型鸭子协议
   （complete/stream_chat）完全不变；app 只做装配。

## 数据流（对着源码走一遍）

### 入库：消息 → 摘要 → 落库

```
MemoryManager.ingest(message)
  → MemoryDatabase.save_message(message)      # messages 表，拿回自增 id
  → SlidingWindow.add(saved_message)          # 超窗返回 evicted
  → 有 evicted 且策略支持摘要:
       fresh = 过滤掉已经摘要过的消息 id
       _update_session_summary(session_id, fresh)
         → db.get_summary("session")           # 取旧摘要
         → LLMSummarizer.summarize(            # fake/真实都只认 complete()
             "session", 旧摘要 + 新消息原文)
         → db.upsert_summary(...)              # summaries 表 session 行
       _mark_summarized(session_id, fresh)     # 防重复摘要
```

批量结束时 `ingest_messages(..., finalize=True)`：
`end_session`（窗口剩余消息也收进会话摘要，但**不把消息移出窗口**）
→ 按天 `rollup_daily` → 按 ISO 周 `rollup_weekly`。

### 检索：策略分叉 → 拼上下文

```
MemoryManager.retrieve(question, strategy)
  ├─ none:            items = []
  ├─ window:          window.messages() → 原文
  ├─ window_summary:  window + session/daily/weekly summaries
  └─ full:            window + summaries
                        + db.search_memories(question, top_k)
                            = 相关性 × weight × exp(-λ × 年龄天数)
  → render_context_items(items) → MemoryContext.text
  → augment_question 包成用户问题 → app.ask → run_tool_loop
```

### M5 重放：四个策略同一批对话

```
replay_memory_strategies(messages, question, db_dir=..., model=..., setup=...)
  for strategy in (none, window, window_summary, full):
      独立 db: memory_<strategy>.db（先 unlink 旧文件）
      MemoryManager(strategy, db_path, model)
      setup(manager)              # 例如写入同样的 KV 长期记忆
      ingest_messages(..., finalize=True)   # 顺序完全一致
      retrieve(question)
  → {strategy: ReplayResult(db_path, context, summary_levels)}
```

## 复现步骤（浓缩版，细节见 PROGRESS.md 的复现要点）

1. 读 `docs/architecture.md` 的 5.2 与 M1 复现指南；确认唯一测试命令
   `PYTHONPATH= .venv/bin/python -m pytest`。
2. **先写 6 个测试文件**：
   - `test_memory_window.py`：N 条/N token 淘汰、evicted 返回、超大单条；
   - `test_memory_summarizer.py`：fake complete、session/daily/weekly prompt、
     截断、异常包成 `SummarizerError`；
   - `test_memory_long_term.py`：建表幂等、消息 roundtrip、KV upsert/读取计数、
     时间衰减公式、摘要 upsert；
   - `test_memory_retriever.py`：四种策略、超窗→会话摘要、finalize 三级链、
     full 的 long_term 通道、`replay_memory_strategies` 隔离、core 外层接缝；
   - `test_app.py`：`PersonalAssistant` 注入上下文再进 `run_tool_loop`；
   - `test_integration_memory.py`：整文件 `integration` + 无 key skip。
   同时给 `test_config.py` 增加记忆默认值与非法值用例。
3. 跑测试看红：应为 **6 个 collection error**（memory/app 模块不存在）。
4. 按依赖自底向上实现：`memory/models.py` → `window.py` → `summarizer.py` →
   `long_term.py` → `retriever.py` → `app.py`；再扩展 `config.py` 的记忆字段。
5. 实现顺序三块：
   - 窗口：先双预算 FIFO，再 `remove_session`；
   - 摘要：先 prompt 三层级，再 duck 归一化，再 manager 增量更新与降级原文；
   - long_term：先 schema，再 messages/summaries，最后 KV `search_memories`
     与衰减公式。
6. 跑 `PYTHONPATH= .venv/bin/python -m pytest`，目标 **90 passed, 4 deselected**；
   再跑 `-m integration`，无密钥应为 **4 skipped**。
7. 更新 README、architecture.md、`.env.example`、`data/README.md`、
   `PROGRESS.md`；最后写本指南。

## 自测题（不看代码答一遍，错了就回去看）

1. `SlidingWindow.add()` 淘汰消息后为什么自己不调摘要器？如果它自己调了，
   哪个测试会立即红？
2. 同一条会话先超窗 3 条、再 `end_session`、之后窗口又被别的会话挤掉 5 条——
   哪些消息会被送进摘要器？哪些不会？依据是什么？
3. 时间衰减公式里为什么相关性用「查询 token 覆盖比例」而不用「两边 token 的交集
   大小」？`decay_lambda` 调成 0 会发生什么？
4. `replay_memory_strategies` 为什么必须给每个策略建独立 SQLite 文件，而且开始前
   先 unlink？如果四个策略共用一个库会怎样？
5. 记忆系统如何做到不改 M1 核心循环？`complete/stream_chat` 鸭子协议在这条链路
   里出现在哪几个边界？

## 面试预演

1. 说说你的滑动窗口和分层摘要怎么配合？为什么被淘汰的消息不能直接丢？
2. 会话级、日级、周级摘要分别什么时候触发？跨天的会话怎么归属？你的实现有什么
   简化，生产环境会怎么改？
3. 长期记忆检索不用向量库，你的时间衰减公式是什么？和 BM25 / 余弦相似度相比，
   优点和局限分别是什么？
4. M5 要做四种记忆策略对比，你的代码怎么保证“同一批对话、不同策略、互不污染”？
   评测指标会从哪些字段取数？
5. 如果 DeepSeek 在生成会话摘要时超时失败，整条入库链路会怎样？消息会丢吗？
   你的 B 计划是什么，恢复后怎么补齐摘要？
