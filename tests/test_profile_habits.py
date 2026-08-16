# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) 习惯别名是 M3 的加分项，直接复用 M2 的 KV 长期记忆：key 固定为
#    sql_alias:<用户说法>，value 是标准说法，category=sql_alias，weight 当置信度。
#    profile 模块不 import memory，只接受一个鸭子协议 kv_backend，由 app 注入。
# 2) 改写规则必须可解释：长说法优先、高权重优先；同一个说法被纠正多次要加权。
# 3) 与 sql_query 的接缝锁在 app：先习惯改写，再记忆上下文增强，最后进核心循环。

import pytest

from personal_data_assistant.app import PersonalAssistant
from personal_data_assistant.memory.retriever import MemoryManager
from personal_data_assistant.profile.habits import AliasRule, HabitAliasStore, RewriteResult
from personal_data_assistant.tools.sql_tools import create_sql_query_tool


class InnerScriptedModel:
    def __init__(self):
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return (
            "SELECT category, SUM(amount) FROM expenses WHERE category='餐饮'"
            if len(self.calls) == 1
            else "餐饮合计 33 元。"
        )


class OuterScriptedModel:
    def __init__(self):
        self.calls = []

    def complete(self, messages):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            return (
                '{"action":"tool","tool":"sql_query",'
                '"args":{"question":"我八月餐饮花了多少钱"}}'
            )
        return '{"action":"final","answer":"餐饮合计 33 元。"}' 


@pytest.fixture()
def manager(tmp_path):
    return MemoryManager(strategy="full", db_path=tmp_path / "pda.db")


def test_record_alias_uses_kv_memory_with_category_and_weight(manager):
    store = HabitAliasStore(manager.db)
    rule = store.record_alias("饭钱", "餐饮")

    assert isinstance(rule, AliasRule)
    assert rule.raw == "饭钱"
    assert rule.canonical == "餐饮"
    assert rule.weight == 1.0

    # 直接落进 M2 的 kv_memories 表，category 隔离出来，检索通道也能看见
    records = manager.db.list_memories()
    assert any(record.key == "sql_alias:饭钱" for record in records)
    assert any(record.category == "sql_alias" for record in records)


def test_repeated_correction_increases_weight_and_rewrites(manager):
    store = HabitAliasStore(manager.db)
    store.record_alias("饭钱", "餐饮")
    store.record_alias("饭钱", "餐饮")

    result = store.rewrite_question("我八月饭钱花了多少")

    assert isinstance(result, RewriteResult)
    assert result.text == "我八月餐饮花了多少"
    assert result.applied == (("饭钱", "餐饮"),)
    assert store.get_alias("饭钱").weight == 2.0


def test_longest_alias_wins_before_shorter_one(manager):
    store = HabitAliasStore(manager.db)
    store.record_alias("饭", "米饭")  # 短说法先注册
    store.record_alias("饭钱", "餐饮")

    assert store.rewrite_question("饭钱多少").text == "餐饮多少"


def test_same_raw_term_is_upserted_and_latest_correction_wins(manager):
    store = HabitAliasStore(manager.db)
    store.record_alias("片子", "电影")
    store.record_alias("片子", "电影")
    store.record_alias("片子", "视频")

    assert store.rewrite_question("最近看了什么片子").text == "最近看了什么视频"
    assert store.get_alias("片子").weight == 3.0


def test_unknown_question_is_unchanged_and_applied_is_empty(manager):
    store = HabitAliasStore(manager.db)
    result = store.rewrite_question("今天天气怎么样")

    assert result.text == "今天天气怎么样"
    assert result.applied == ()


def test_get_alias_returns_none_for_missing_term(manager):
    store = HabitAliasStore(manager.db)
    assert store.get_alias("不存在") is None


def test_invalid_alias_terms_raise_value_error(manager):
    store = HabitAliasStore(manager.db)
    with pytest.raises(ValueError):
        store.record_alias("  ", "餐饮")
    with pytest.raises(ValueError):
        store.record_alias("饭钱", "  ")
    with pytest.raises(ValueError):
        store.record_alias("饭钱", "饭钱")


def test_personal_assistant_learn_alias_proxy(tmp_path, manager):
    habits = HabitAliasStore(manager.db)
    assistant = PersonalAssistant(model=OuterScriptedModel(), tools=[], memory_manager=manager, habits=habits)

    assistant.learn_alias("饭钱", "餐饮")

    assert habits.get_alias("饭钱").canonical == "餐饮"
    assistant.close()
    manager.close()


def test_personal_assistant_rewrites_before_memory_augment_and_tool_call(tmp_path, manager):
    import sqlite3

    user_db = tmp_path / "user.db"
    conn = sqlite3.connect(user_db)
    conn.execute("CREATE TABLE expenses(id INTEGER PRIMARY KEY, category TEXT, amount REAL)")
    conn.executemany(
        "INSERT INTO expenses(category, amount) VALUES (?, ?)",
        [("餐饮", 8.0), ("餐饮", 25.0)],
    )
    conn.commit()
    conn.close()

    habits = HabitAliasStore(manager.db)
    habits.record_alias("饭钱", "餐饮")

    inner = InnerScriptedModel()
    outer = OuterScriptedModel()
    assistant = PersonalAssistant(
        model=outer,
        tools=[],
        memory_manager=manager,
        sql_query_tool=create_sql_query_tool(model=inner, db_path=user_db),
        habits=habits,
    )

    result = assistant.ask("我八月饭钱花了多少")

    assert result.status == "final"
    first_user_message = outer.calls[0][-1]["content"]
    # 末尾的用户问题必须已被改写为标准说法；上下文里的别名来自 KV 记忆原文，允许共存
    assert first_user_message.endswith("我八月餐饮花了多少")
    # SQL 子流程拿到的也是改写后的标准说法
    assert "我八月餐饮花了多少钱" in inner.calls[0][-1]["content"]

    assistant.close()
    manager.close()
