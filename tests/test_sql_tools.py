# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) sql_query 必须是一个普通 M1 Tool：注册进 ToolRegistry 后，核心循环不用改
#    一行就能调用它；工具内部的“生成 SQL → 执行 → 修正 → 解释”是子流程，
#    对外只暴露 question 一个参数（库路径在装配期锁死，模型不能随便指库）。
# 2) 工具成功返回结构化 dict（答案 + SQL + 行数 + 评测埋点），失败返回
#    ToolResult(ok=False)；两条路径都要能被 M1 循环回填与继续。
# 3) M2 记忆系统继续作为 core 外层组件工作，本文件专门锁住“记忆上下文 +
#    sql_query 工具”同一条链路的接缝。

import json
import sqlite3

import pytest

from personal_data_assistant.app import PersonalAssistant
from personal_data_assistant.core.loop import run_tool_loop
from personal_data_assistant.llm.client import LLMResponse, TokenUsage
from personal_data_assistant.memory.retriever import MemoryManager
from personal_data_assistant.memory.window import Message
from personal_data_assistant.tools.base import ToolResult
from personal_data_assistant.tools.registry import ToolRegistry
from personal_data_assistant.tools.sql_tools import create_sql_query_tool


def make_expenses_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE expenses(id INTEGER PRIMARY KEY, category TEXT, amount REAL)"
    )
    conn.executemany(
        "INSERT INTO expenses(category, amount) VALUES (?, ?)",
        [("餐饮", 8.0), ("餐饮", 25.0), ("交通", 5.0)],
    )
    conn.commit()
    conn.close()
    return path


class InnerScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResponse(
            content=item,
            model="fake-inner",
            usage=TokenUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            raw={},
        )


class OuterScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages):
        self.calls.append([dict(message) for message in messages])
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResponse(
            content=item,
            model="fake-outer",
            usage=TokenUsage(prompt_tokens=7, completion_tokens=4, total_tokens=11),
            raw={},
        )


GOOD_SQL = "SELECT category, SUM(amount) AS total FROM expenses WHERE category='餐饮'"
EXPLAIN = "餐饮合计 33.0 元。"


class SharedScriptedModel:
    """user_db_path 自动装配时，主循环与 sql_query 子流程共用同一个模型。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages):
        self.calls.append([dict(message) for message in messages])
        return self.responses.pop(0)


def make_tool(tmp_path, inner_responses):
    return create_sql_query_tool(
        model=InnerScriptedModel(inner_responses),
        db_path=make_expenses_db(tmp_path / "user.db"),
        max_fix_rounds=3,
    )


def test_sql_query_tool_has_expected_schema_and_registers(tmp_path):
    tool = make_tool(tmp_path, [GOOD_SQL, EXPLAIN])
    registry = ToolRegistry()
    registry.register(tool)

    assert tool.name == "sql_query"
    assert "sql_query" in registry
    assert registry.describe_for_prompt()
    assert tool.parameters["properties"]["question"]["type"] == "string"
    assert tool.parameters["required"] == ["question"]

    with pytest.raises(ValueError):
        registry.register(tool)  # 工具表去重契约来自 M1，继续生效


def test_tool_execute_returns_structured_payload_with_eval_fields(tmp_path):
    tool = make_tool(tmp_path, [GOOD_SQL, EXPLAIN])

    result = tool.execute({"question": "餐饮花了多少钱"})

    assert isinstance(result, ToolResult)
    assert result.ok is True
    payload = result.result
    assert payload["answer"] == EXPLAIN
    assert payload["status"] == "success"
    assert "SELECT" in payload["sql"].upper()
    assert payload["row_count"] == 1
    assert payload["first_attempt_success"] is True
    assert payload["fix_success"] is False
    assert payload["total_fix_rounds"] == 0
    assert payload["usage"]["total_tokens"] == 10
    # 工具结果保持 JSON 友好，M1 回填时不需要 default=str 特殊处理
    json.dumps(payload, ensure_ascii=False)


def test_tool_missing_question_is_rejected_by_schema(tmp_path):
    tool = make_tool(tmp_path, [GOOD_SQL, EXPLAIN])
    result = tool.execute({})

    assert result.ok is False
    assert "缺少必填字段" in result.error


def test_tool_failure_returns_ok_false_but_keeps_structured_error(tmp_path):
    tool = make_tool(tmp_path, ["DELETE FROM expenses"] * 4)
    result = tool.execute({"question": "清空记录"})

    assert result.ok is False
    assert result.error is not None
    assert "只允许 SELECT/WITH" in result.error
    assert result.result["status"] == "failed"


def test_core_loop_calls_sql_query_tool_without_any_loop_changes(tmp_path):
    inner = InnerScriptedModel([GOOD_SQL, EXPLAIN])
    tool = create_sql_query_tool(model=inner, db_path=make_expenses_db(tmp_path / "user.db"))
    registry = ToolRegistry([tool])

    outer = OuterScriptedModel(
        [
            '{"action":"tool","tool":"sql_query","args":{"question":"餐饮花了多少钱"}}',
            '{"action":"final","answer":"你八月餐饮花了 33 元。"}',
        ]
    )
    result = run_tool_loop("八月餐饮花了多少钱", registry, outer)

    assert result.status == "final"
    assert result.answer == "你八月餐饮花了 33 元。"
    assert result.tool_rounds == 1
    assert result.steps[0].tool == "sql_query"
    assert result.steps[0].tool_ok is True
    assert "餐饮合计" in result.steps[0].tool_result["answer"]

    # 子流程与主循环各用各的模型：主循环模型第二轮的 user 回填里有工具结果
    assert len(inner.calls) == 2
    backfilled = [m for m in outer.calls[1] if m["role"] == "user"]
    assert any('"tool_result"' in m["content"] for m in backfilled)


def test_personal_assistant_auto_registers_sql_query_from_user_db_path(tmp_path):
    expenses_db = make_expenses_db(tmp_path / "user.db")
    shared = SharedScriptedModel(
        [
            '{"action":"tool","tool":"sql_query","args":{"question":"餐饮花了多少钱"}}',
            GOOD_SQL,
            EXPLAIN,
            '{"action":"final","answer":"餐饮合计 33 元。"}',
        ]
    )
    manager = MemoryManager(strategy="none", db_path=tmp_path / "pda.db")
    assistant = PersonalAssistant(
        model=shared,
        tools=[],
        memory_manager=manager,
        user_db_path=expenses_db,
    )

    result = assistant.ask("餐饮花了多少钱")

    assert result.status == "final"
    assert result.tool_rounds == 1
    assert result.steps[0].tool == "sql_query"
    assert len(shared.calls) == 4  # 主循环 2 次 + 问数子流程 2 次

    assistant.close()
    manager.close()


def test_personal_assistant_registers_sql_tool_and_memory_still_works(tmp_path):
    expenses_db = make_expenses_db(tmp_path / "user.db")
    inner = InnerScriptedModel([GOOD_SQL, EXPLAIN])
    outer = OuterScriptedModel(
        [
            '{"action":"tool","tool":"sql_query","args":{"question":"我八月餐饮花了多少钱"}}',
            '{"action":"final","answer":"结合记忆与数据，餐饮合计 33 元。"}',
        ]
    )

    manager = MemoryManager(
        strategy="full",
        db_path=tmp_path / "pda.db",
        max_window_messages=10,
    )
    manager.remember("餐饮", "用户常问餐饮开销", category="sql_alias")
    manager.ingest_messages(
        [Message(role="user", content="我常把餐饮叫饭钱", session_id="s1")]
    )

    assistant = PersonalAssistant(
        model=outer,
        tools=[],
        memory_manager=manager,
        sql_query_tool=create_sql_query_tool(model=inner, db_path=expenses_db),
    )
    result = assistant.ask("我八月饭钱花了多少")

    assert result.status == "final"
    assert result.tool_rounds == 1
    # 记忆上下文先拼进主循环用户问题（M2 接缝未破坏）
    first_user_message = outer.calls[0][-1]["content"]
    assert "我常把餐饮叫饭钱" in first_user_message
    assert first_user_message.endswith("我八月饭钱花了多少")

    assistant.close()
    manager.close()
