# M1 复现指南：Agent 核心循环（命根子模块）

> 这是整个项目最重要的一本指南。M1 复现透了，面试题单里
> ReAct / 死循环防护 / 工具失败处理 / 超时降级 四类题闭眼答。

## 本里程碑是什么

`core/loop.py`（474 行）+ `llm/client.py`（523 行）等 6 个文件，实现：

**模型调用 → 解析输出 → 执行工具 → 结果回填 → 再调模型 → 直到模型说「答完了」**

## 先理解 6 个核心概念（复现的钥匙）

1. **ReAct 循环**：Reasoning + Acting 交替。模型不直接答，而是输出「我要调用
   哪个工具、参数是什么」，程序执行后把结果喂回去。`{"action":"tool",...}`
   与 `{"action":"final","answer":...}` 两种动作就是整个协议。
2. **JSON 动作协议 vs 原生 function calling**：本项目的模型请求**不带**
   `tools` 字段，全靠提示词教模型输出 JSON。好处：可 mock、可降级、不依赖
   服务端功能；代价：要自己处理「模型输出不是合法 JSON」的情况
   （围栏剥离 → 子串提取 → 回填错误消息让模型重来）。
3. **鸭子协议**：循环只认模型对象有 `complete()`（必需）和 `stream_chat()`
   （可选），不关心它是什么类。所以测试用 `ScriptedModel`（预设台词队列），
   生产用 `DeepSeekClient`，循环代码一行不改。
4. **最大轮数语义**：预算是「允许的非 final 回复数」。解析错误、未知工具、
   工具调用**都占预算**。越界后不再执行工具，追加「强制收束」指令做最后一次
   调用；模型还不肯 final，就用轨迹里已成功的工具结果拼一个确定性中文兜底。
5. **重试与退避**：429/5xx/传输错误按指数退避重试（0.5s→1s→2s），
   `retry_sleeper` 可注入所以测试不真 sleep。**降级只降一次**：流式打开失败
   → 改非流式请求一次；流已经吐字后失败则不再降级（半截话不能吞）。
6. **SSE 流式**：HTTP 响应是逐块到达的文本流，自己拆 `data:` 行拼起来；
   `stream_options={"include_usage":true}` 让最后一个块带 token 用量。

## 数据流（对着源码走一遍）

```
run_tool_loop(question, tools, model)
  → build_initial_messages(question, registry.describe())   # 系统提示+工具清单+问题
  → while True:
       response = _call_model(...)          # 流式优先，失败降级 complete
       parsed = parse_action(response)      # 失败→回填错误消息→continue（占预算）
       if parsed.action == "final": 返回答案
       tool = registry.find(parsed.tool)    # 找不到→回填可用工具清单→continue
       result = tool.execute(parsed.args)   # 异常→回填错误→continue
       messages += [assistant输出, 工具结果(role=user)]   # 结果用 user 角色回填
  → 预算耗尽: forced_final()（一次强制收束）→ 仍失败→中文兜底答案
```

## 复现步骤（浓缩版，细节见 PROGRESS.md 的复现要点）

1. 先写 6 个测试文件（config/tools/prompts/core_loop/llm_client/integration），
   跑出 6 个 collection error 的红
2. 自底向上实现：config → tools/base → tools/registry → llm/prompts →
   llm/client → core/loop，每文件顶部写中文取舍注释
3. 客户端顺序：非流式 complete → 重试发送 → SSE 拆包 → usage 尾块 →
   流式降级（严格单次）→ `StreamedChat.result`
4. `PYTHONPATH= .venv/bin/python -m pytest` 到 55 passed、2 deselected

## 自测题（不看代码答一遍，错了就回去看）

1. 模型输出了 `{"action":"tool","tool":"bash","args":[]}`，但 bash 工具抛了
   `ZeroDivisionError`——循环会发生什么？会崩吗？
2. 模型连续 3 次输出不合法 JSON，预算 6——还剩多少预算？为什么这么设计？
3. 流式已经吐了 100 个字后网络断了——代码为什么不降级非流式重来？
4. `parse_action` 为什么先剥 Markdown 围栏，还要再做「找第一个 { 到最后一个 }」
   的子串提取？
5. 工具结果为什么用 `role=user` 回填，而不是 `role=assistant`？

## 面试预演

1. ReAct 的 Thought/Action/Observation 对应你代码里的什么？
2. 工具调用失败你分几类处理？超时和参数错误为什么不同对待？
3. 怎么防止 Agent 死循环？你的三层兜底分别是什么？
4. 为什么不用原生 function calling？什么场景下你会换回它？
5. 流式输出怎么做 token 计量？服务端不给 usage 时怎么办？
