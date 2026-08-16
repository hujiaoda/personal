# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) app 只做装配，不写业务：把记忆上下文拼进用户问题，再交给 M1 的
#    run_tool_loop；测试用 fake 模型断言它看到的 user 消息确实带记忆上下文。
# 2) PersonalAssistant 的 remember 只是 MemoryManager 的薄代理，保证使用方
#    不需要同时操作两个对象。

from datetime import datetime, timezone

import pytest

from personal_data_assistant.app import PersonalAssistant
from personal_data_assistant.memory.retriever import MemoryManager
from personal_data_assistant.memory.window import Message

NOW = datetime(2025, 8, 16, 12, 0, tzinfo=timezone.utc)


class FinalModel:
    def __init__(self):
        self.seen = []

    def complete(self, messages):
        self.seen.append(messages[-1]["content"])
        return '{"action":"final","answer":"回答完成"}'


def test_assistant_injects_memory_context_before_core_loop(tmp_path):
    manager = MemoryManager(
        strategy="full",
        db_path=tmp_path / "app.db",
        max_window_messages=10,
        now_fn=lambda: NOW,
    )
    manager.remember("python", "Python 装饰器学习记录", now=NOW)
    manager.ingest_messages(
        [Message(role="user", content="今天学了装饰器", session_id="s1", created_at=NOW)]
    )
    model = FinalModel()
    assistant = PersonalAssistant(model=model, tools=[], memory_manager=manager)

    result = assistant.ask("我学了什么")

    assert result.status == "final"
    assert result.answer == "回答完成"
    assert "Python 装饰器学习记录" in model.seen[0]
    assert model.seen[0].endswith("我学了什么")

    assistant.remember("sqlite", "SQLite 时间衰减检索", now=NOW)
    assert manager.db.get_memory("sqlite") is not None
    assistant.close()
    manager.close()


def test_assistant_requires_memory_manager(tmp_path):
    with pytest.raises(TypeError):
        PersonalAssistant(model=FinalModel(), tools=[], memory_manager=None)
