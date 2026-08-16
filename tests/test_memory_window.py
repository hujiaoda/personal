# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) 窗口只负责“保留最近 N 条 / N token”，淘汰结果必须原样返回给调用方；
#    窗口自己不得调用摘要或持久化，这个接缝用返回 evicted 列表来锁死。
# 2) token 预算用显式 tokens 字段构造用例，避免测试依赖估算公式的具体值。

from datetime import datetime, timedelta, timezone

import pytest

from personal_data_assistant.memory.window import Message, SlidingWindow, estimate_tokens


def make_message(content: str, session_id: str = "s1", minutes: int = 0) -> Message:
    return Message(
        role="user",
        content=content,
        session_id=session_id,
        created_at=datetime(2025, 8, 16, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes),
    )


def test_window_keeps_last_n_messages_and_returns_evicted_in_order():
    window = SlidingWindow(max_messages=3, max_tokens=10**9)

    evicted = []
    for index in range(5):
        evicted.extend(window.add(make_message(f"消息{index + 1}", minutes=index)))

    assert [m.content for m in window.messages()] == ["消息3", "消息4", "消息5"]
    assert [m.content for m in evicted] == ["消息1", "消息2"]
    assert len(window) == 3


def test_window_evicts_by_token_budget_first_in_first_out():
    window = SlidingWindow(max_messages=10, max_tokens=10)

    evicted = []
    for index in range(4):
        evicted.extend(
            window.add(
                Message(
                    role="assistant",
                    content=f"token消息{index + 1}",
                    session_id="s1",
                    tokens=3,
                    created_at=make_message("x", minutes=index).created_at,
                )
            )
        )

    # 加第 4 条时总量 12 > 10，先淘汰最早的第 1 条；窗口最终剩 3 条、9 token。
    assert [m.content for m in evicted] == ["token消息1"]
    assert [m.content for m in window.messages()] == ["token消息2", "token消息3", "token消息4"]
    assert window.total_tokens == 9


def test_evicted_messages_are_returned_to_caller_not_discarded():
    window = SlidingWindow(max_messages=2, max_tokens=10**9)
    first = make_message("旧消息")
    window.add(first)
    window.add(make_message("中间消息"))
    evicted = window.add(make_message("新消息"))

    assert evicted == [first]
    assert [m.content for m in window.messages()] == ["中间消息", "新消息"]


def test_oversized_single_message_evicts_itself_and_leaves_window_empty():
    window = SlidingWindow(max_messages=2, max_tokens=4)
    huge = Message(role="user", content="huge", session_id="s1", tokens=50)

    evicted = window.add(huge)

    assert evicted == [huge]
    assert window.messages() == ()
    assert window.total_tokens == 0


def test_estimate_tokens_uses_utf8_heuristic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("中文") == 2


def test_invalid_window_limits_raise_value_error():
    with pytest.raises(ValueError):
        SlidingWindow(max_messages=0, max_tokens=10)
    with pytest.raises(ValueError):
        SlidingWindow(max_messages=10, max_tokens=0)
