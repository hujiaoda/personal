# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) MemoryManager 是 M2 唯一编排入口：同一批消息用不同 strategy 重放，
#    必须得到相互隔离的检索上下文——这就是 M5 策略对比的地基。
# 2) fake 模型只实现 complete(messages)，证明摘要层与核心循环一样走鸭子协议。
# 3) manager 与 core.loop 的接缝只通过 augment_question() 拼进用户问题，
#    核心循环的 complete/stream_chat 协议一行不改。

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_data_assistant.core.loop import run_tool_loop
from personal_data_assistant.memory.long_term import MemoryDatabase
from personal_data_assistant.memory.retriever import MemoryManager, replay_memory_strategies
from personal_data_assistant.memory.window import Message

DAY = datetime(2025, 8, 16, 10, 0, tzinfo=timezone.utc)
NOW = datetime(2025, 8, 16, 12, 0, tzinfo=timezone.utc)


def make_message(index: int, content: str | None = None) -> Message:
    return Message(
        role="user",
        content=content or f"消息{index}",
        session_id="s1",
        created_at=DAY + timedelta(minutes=index),
    )


class FakeSummaryModel:
    """根据 system prompt 里的层级返回带层级的摘要，证明提示词确实分层调用。"""

    def __init__(self) -> None:
        self.calls = []

    def complete(self, messages):
        self.calls.append([dict(m) for m in messages])
        system = messages[0]["content"]
        if "会话级摘要" in system:
            level = "SESS"
        elif "日级摘要" in system:
            level = "DAILY"
        elif "周级摘要" in system:
            level = "WEEKLY"
        else:
            level = "SUMMARY"
        return SimpleNamespace(content=f"{level}:ok", model="fake", usage=None)


class FinalEchoModel:
    """核心循环的 fake：记录自己看到的 user 消息，并按 JSON 动作协议给 final。"""

    def __init__(self) -> None:
        self.seen_user_contents = []

    def complete(self, messages):
        self.seen_user_contents.append(messages[-1]["content"])
        return '{"action":"final","answer":"已收到上下文"}'


@pytest.fixture()
def fake_summary_model():
    return FakeSummaryModel()


def test_strategy_none_returns_empty_context_but_still_persists_messages(tmp_path):
    manager = MemoryManager(
        strategy="none",
        db_path=tmp_path / "none.db",
        max_window_messages=2,
        now_fn=lambda: NOW,
    )
    manager.ingest_messages([make_message(i) for i in range(1, 6)], finalize=True)

    context = manager.retrieve("消息")

    assert context.items == []
    assert context.text == ""
    assert len(manager.db.load_messages(session_id="s1")) == 5
    assert manager.db.list_summaries() == []
    manager.close()


def test_window_strategy_returns_only_recent_messages(tmp_path):
    manager = MemoryManager(
        strategy="window",
        db_path=tmp_path / "window.db",
        max_window_messages=3,
        now_fn=lambda: NOW,
    )
    manager.ingest_messages([make_message(i) for i in range(1, 6)])

    context = manager.retrieve("消息")

    assert [item.source for item in context.items] == ["window"] * 3
    assert [item.text for item in context.items] == ["消息3", "消息4", "消息5"]
    assert manager.db.list_summaries() == []
    manager.close()


def test_overflow_is_handed_to_session_summarizer_not_discarded(tmp_path, fake_summary_model):
    manager = MemoryManager(
        strategy="window_summary",
        db_path=tmp_path / "summary.db",
        model=fake_summary_model,
        max_window_messages=2,
        max_window_tokens=10**9,
        now_fn=lambda: NOW,
    )
    manager.ingest_messages([make_message(i) for i in range(1, 5)])

    session_summary = manager.db.get_summary("session", session_id="s1")

    assert session_summary is not None
    assert session_summary.content == "SESS:ok"
    assert [m.content for m in manager.window.messages()] == ["消息3", "消息4"]
    # 第 3、4 条各触发一次超窗，session 摘要被增量更新两次（可多但绝不能为零）。
    assert len(fake_summary_model.calls) >= 2
    assert fake_summary_model.calls[0][1]["content"].find("消息1") != -1
    manager.close()


def test_finalize_builds_session_daily_weekly_summary_chain(tmp_path, fake_summary_model):
    manager = MemoryManager(
        strategy="window_summary",
        db_path=tmp_path / "chain.db",
        model=fake_summary_model,
        max_window_messages=10,
        now_fn=lambda: NOW,
    )
    manager.ingest_messages([make_message(i) for i in range(1, 4)], finalize=True)

    levels = {s.level for s in manager.list_summaries()}
    assert levels == {"session", "daily", "weekly"}

    daily = manager.db.get_summary("daily", period_key="2025-08-16")
    weekly = manager.db.get_summary("weekly", period_key="2025-W33")
    assert daily is not None and daily.content == "DAILY:ok"
    assert weekly is not None and weekly.content == "WEEKLY:ok"
    assert any("日级摘要" in call[0]["content"] for call in fake_summary_model.calls)
    assert any("周级摘要" in call[0]["content"] for call in fake_summary_model.calls)
    manager.close()


def test_end_session_summarizes_pending_but_keeps_window_recent_context(tmp_path, fake_summary_model):
    manager = MemoryManager(
        strategy="window_summary",
        db_path=tmp_path / "end.db",
        model=fake_summary_model,
        max_window_messages=10,
        now_fn=lambda: NOW,
    )
    manager.ingest(make_message(1))
    manager.ingest(make_message(2))

    summary = manager.end_session("s1")

    assert summary is not None and summary.level == "session"
    # 摘要通道要建，窗口原文通道也要保留：结束会话不等于丢掉近期原文。
    assert [m.content for m in manager.window.messages()] == ["消息1", "消息2"]
    manager.close()


def test_full_strategy_adds_time_decay_long_term_results(tmp_path, fake_summary_model):
    manager = MemoryManager(
        strategy="full",
        db_path=tmp_path / "full.db",
        model=fake_summary_model,
        max_window_messages=2,
        long_term_top_k=5,
        now_fn=lambda: NOW,
    )
    manager.remember("python", "Python 装饰器学习记录", category="学习", now=DAY)
    manager.ingest_messages([make_message(i) for i in range(1, 4)], finalize=True)

    context = manager.retrieve("Python 装饰器", now=NOW)

    sources = {item.source for item in context.items}
    assert sources == {"window", "session_summary", "daily_summary", "weekly_summary", "long_term"}
    long_term_items = [item for item in context.items if item.source == "long_term"]
    assert len(long_term_items) == 1
    assert "Python 装饰器学习记录" in long_term_items[0].text
    assert long_term_items[0].score is not None and long_term_items[0].score > 0
    manager.close()


def test_window_summary_strategy_does_not_search_long_term(tmp_path, fake_summary_model):
    manager = MemoryManager(
        strategy="window_summary",
        db_path=tmp_path / "no-kv.db",
        model=fake_summary_model,
        max_window_messages=2,
        now_fn=lambda: NOW,
    )
    manager.remember("python", "Python 装饰器学习记录", now=DAY)
    manager.ingest_messages([make_message(1)], finalize=True)

    context = manager.retrieve("Python")

    assert "long_term" not in {item.source for item in context.items}
    manager.close()


def test_replay_same_conversation_across_strategies_is_isolated(tmp_path):
    messages = [make_message(i) for i in range(1, 6)]

    def setup(manager):
        manager.remember("python", "Python 装饰器学习记录", now=DAY)

    results = replay_memory_strategies(
        messages,
        "Python 装饰器",
        db_dir=tmp_path,
        model=FakeSummaryModel(),
        setup=setup,
        manager_kwargs={
            "max_window_messages": 2,
            "max_window_tokens": 10**9,
            "now_fn": lambda: NOW,
        },
    )

    assert set(results) == {"none", "window", "window_summary", "full"}

    assert results["none"].context.items == []
    assert [item.source for item in results["window"].context.items] == ["window", "window"]
    assert {item.source for item in results["window_summary"].context.items} == {
        "window",
        "session_summary",
        "daily_summary",
        "weekly_summary",
    }
    assert {item.source for item in results["full"].context.items} == {
        "window",
        "session_summary",
        "daily_summary",
        "weekly_summary",
        "long_term",
    }

    paths = [Path(result.db_path) for result in results.values()]
    assert len(set(paths)) == 4
    assert all(path.exists() for path in paths)
    # 每个策略库只装了自己的那 5 条消息，重放互不污染。
    for path in paths:
        probe = MemoryDatabase(path)
        assert len(probe.load_messages(session_id="s1")) == 5
        probe.close()


def test_augment_question_keeps_context_and_original_question(tmp_path):
    manager = MemoryManager(
        strategy="window",
        db_path=tmp_path / "augment.db",
        max_window_messages=2,
        now_fn=lambda: NOW,
    )
    manager.ingest_messages([make_message(1), make_message(2)])

    augmented = manager.augment_question("我学过什么")

    assert augmented.startswith("以下是记忆系统提供的上下文")
    assert "消息1" in augmented
    assert augmented.endswith("我学过什么")
    manager.close()


def test_memory_manager_connects_to_core_loop_as_outer_component(tmp_path):
    manager = MemoryManager(
        strategy="full",
        db_path=tmp_path / "loop.db",
        model=FakeSummaryModel(),
        max_window_messages=10,
        now_fn=lambda: NOW,
    )
    manager.remember("python", "Python 装饰器学习记录", now=DAY)
    manager.ingest_messages([make_message(1)], finalize=True)
    loop_model = FinalEchoModel()

    result = run_tool_loop(manager.augment_question("Python 是什么"), tools=[], model=loop_model)

    assert result.status == "final"
    assert result.answer == "已收到上下文"
    assert "Python 装饰器学习记录" in loop_model.seen_user_contents[0]
    manager.close()


def test_invalid_strategy_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        MemoryManager(strategy="fancy", db_path=tmp_path / "bad.db")
