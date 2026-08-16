# -*- coding: utf-8 -*-
# 真实 DeepSeek 记忆冒烟测试：默认被 addopts 排除；显式 -m integration 且
# 配置了 DEEPSEEK_API_KEY 才跑。只做最小业务断言，不锁具体摘要文案。

import os

import pytest

from personal_data_assistant.config import load_settings
from personal_data_assistant.llm.client import DeepSeekClient
from personal_data_assistant.memory.retriever import MemoryManager
from personal_data_assistant.memory.summarizer import LLMSummarizer
from personal_data_assistant.memory.window import Message

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DEEPSEEK_API_KEY"),
        reason="未配置 DEEPSEEK_API_KEY，跳过真实 API 记忆冒烟测试",
    ),
]


@pytest.fixture(scope="module")
def client():
    settings = load_settings()
    return DeepSeekClient(settings)


def test_real_session_summary_smoke(client):
    summarizer = LLMSummarizer(client)
    summary = summarizer.summarize(
        "session",
        "user: 我今天学了 Python 装饰器\nassistant: 装饰器是接收函数并返回新函数的可调用对象",
        period_key="2025-08-16",
        session_id="integration-session",
    )

    assert summary.level == "session"
    assert summary.content
    assert summary.total_tokens > 0


def test_real_memory_manager_full_smoke(client, tmp_path):
    manager = MemoryManager(
        strategy="full",
        db_path=tmp_path / "pda.db",
        model=client,
        max_window_messages=1,
        max_window_tokens=10**9,
    )
    manager.remember("python", "我在学 Python 装饰器")
    manager.ingest(
        Message(role="user", content="我今天学了 Python 装饰器", session_id="s1")
    )
    manager.ingest(
        Message(role="assistant", content="装饰器接收函数并返回新函数", session_id="s1")
    )
    manager.end_session("s1")

    context = manager.retrieve("Python 装饰器")
    assert any(item.source == "session_summary" for item in context.items)
    assert any(item.source == "long_term" for item in context.items)
    assert context.text
    manager.close()
