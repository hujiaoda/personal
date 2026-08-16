# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) 摘要器只依赖模型鸭子协议 complete(messages)，不 import DeepSeekClient；
#    测试用一个记录调用的 fake 对象锁死这个边界。
# 2) 只断言提示词包含层级语义与源文本，不逐字锁死文案，后续润色提示词不误伤测试。
# 3) 模型异常必须包成 SummarizerError，让上层 manager 能统一降级。

from types import SimpleNamespace

import pytest

from personal_data_assistant.memory.summarizer import (
    LLMSummarizer,
    SummarizerError,
    Summary,
    format_messages_for_summary,
)


class RecordingModel:
    def __init__(self, reply: str = "fake 摘要"):
        self.reply = reply
        self.calls = []

    def complete(self, messages):
        self.calls.append([dict(m) for m in messages])
        return SimpleNamespace(
            content=self.reply,
            model="fake-model",
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=22, total_tokens=33),
        )


def test_session_summary_uses_complete_duck_protocol():
    model = RecordingModel("这是一份会话摘要")
    summarizer = LLMSummarizer(model)

    summary = summarizer.summarize(
        "session",
        "user: 我学了 Python\nassistant: 你学了装饰器",
        period_key="2025-08-16",
        session_id="s1",
    )

    assert isinstance(summary, Summary)
    assert summary.level == "session"
    assert summary.period_key == "2025-08-16"
    assert summary.session_id == "s1"
    assert summary.content == "这是一份会话摘要"
    assert summary.model == "fake-model"
    assert summary.total_tokens == 33

    assert len(model.calls) == 1
    messages = model.calls[0]
    assert messages[0]["role"] == "system"
    assert "会话级摘要" in messages[0]["content"]
    assert "我学了 Python" in messages[1]["content"]
    assert messages[1]["role"] == "user"


def test_daily_and_weekly_prompts_carry_hierarchy_and_period_key():
    model = RecordingModel()
    summarizer = LLMSummarizer(model, max_source_chars=2000)

    daily = summarizer.summarize("daily", "会话摘要A\n会话摘要B", period_key="2025-08-16")
    weekly = summarizer.summarize("weekly", "日级摘要A\n日级摘要B", period_key="2025-W33")

    assert daily.level == "daily" and daily.session_id is None
    assert weekly.level == "weekly" and weekly.period_key == "2025-W33"
    daily_prompt = model.calls[0][0]["content"] + "\n" + model.calls[0][1]["content"]
    weekly_prompt = model.calls[1][0]["content"] + "\n" + model.calls[1][1]["content"]
    assert "日级摘要" in daily_prompt
    assert "周级摘要" in weekly_prompt
    assert "2025-08-16" in daily_prompt
    assert "2025-W33" in weekly_prompt


def test_source_text_is_truncated_before_sending_to_model():
    model = RecordingModel()
    summarizer = LLMSummarizer(model, max_source_chars=20)

    summarizer.summarize("session", "一二三四五六七八九十" * 4, period_key="2025-08-16")

    source_text = model.calls[0][1]["content"]
    assert "已截断" in source_text
    assert len(source_text) < 300


def test_format_messages_for_summary_keeps_roles_order_and_session():
    from personal_data_assistant.memory.window import Message

    text = format_messages_for_summary(
        [
            Message(role="user", content="问题", session_id="s1"),
            Message(role="assistant", content="回答", session_id="s1"),
        ]
    )

    assert "user" in text and "问题" in text
    assert text.index("问题") < text.index("回答")


def test_model_failure_is_wrapped_in_summarizer_error():
    class BoomModel:
        def complete(self, messages):
            raise RuntimeError("模型爆炸")

    summarizer = LLMSummarizer(BoomModel())

    with pytest.raises(SummarizerError, match="模型爆炸"):
        summarizer.summarize("session", "原文", period_key="2025-08-16")


def test_missing_complete_method_raises_summarizer_error():
    summarizer = LLMSummarizer(object())

    with pytest.raises(SummarizerError, match="complete"):
        summarizer.summarize("session", "原文", period_key="2025-08-16")


def test_invalid_level_or_period_raises_value_error():
    model = RecordingModel()
    summarizer = LLMSummarizer(model)

    with pytest.raises(ValueError):
        summarizer.summarize("monthly", "原文", period_key="2025-08-16")
    with pytest.raises(ValueError):
        summarizer.summarize("session", "原文", period_key="")
