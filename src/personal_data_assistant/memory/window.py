# -*- coding: utf-8 -*-
# 设计取舍：
# 1) 窗口是纯内存的 FIFO 队列，只负责“最近 N 条 / N token 先到先出”；
#    被淘汰消息通过 add() 的返回值交给调用方（manager），窗口自己绝不调摘要
#    或持久化，因此窗口可以单独测试，也可以被任何策略复用。
# 2) token 估算与 llm/client.estimate_tokens 同公式（UTF-8 每 4 字节算 1 token），
#    但这里刻意不 import llm，守住 memory 不依赖 llm 的模块边界。
# 3) 单条消息超过 token 预算时直接淘汰该条并保持窗口为空：与其破坏“总 token
#    不超预算”的契约，不如让上层把它交给摘要/持久化层兜住。

from __future__ import annotations

from typing import List, Tuple

from personal_data_assistant.memory.models import Message, estimate_tokens


class SlidingWindow:
    """保存最近消息的滑动窗口。淘汰顺序恒为最旧优先。"""

    def __init__(self, max_messages: int = 20, max_tokens: int = 8000) -> None:
        if max_messages < 1:
            raise ValueError(f"max_messages 必须 >= 1，当前: {max_messages}")
        if max_tokens < 1:
            raise ValueError(f"max_tokens 必须 >= 1，当前: {max_tokens}")
        self.max_messages = max_messages
        self.max_tokens = max_tokens
        self._messages: List[Message] = []
        self._tokens: List[int] = []

    def add(self, message: Message) -> List[Message]:
        """加入一条消息，返回被淘汰的消息（可能为空）。调用方负责后续处置。"""
        if not isinstance(message, Message):
            raise TypeError(f"窗口只接受 Message，当前: {type(message).__name__}")
        tokens = message.tokens if message.tokens > 0 else estimate_tokens(message.content)
        self._messages.append(message)
        self._tokens.append(tokens)

        evicted: List[Message] = []
        while self._messages and (
            len(self._messages) > self.max_messages or sum(self._tokens) > self.max_tokens
        ):
            evicted.append(self._messages.pop(0))
            self._tokens.pop(0)
        return evicted

    def remove_session(self, session_id: str) -> List[Message]:
        """移除并返回指定会话还在窗口内的消息（结束会话后窗口只留其它会话）。"""
        removed: List[Message] = []
        kept_messages: List[Message] = []
        kept_tokens: List[int] = []
        for message, tokens in zip(self._messages, self._tokens):
            if message.session_id == session_id:
                removed.append(message)
            else:
                kept_messages.append(message)
                kept_tokens.append(tokens)
        self._messages = kept_messages
        self._tokens = kept_tokens
        return removed

    def messages(self) -> Tuple[Message, ...]:
        return tuple(self._messages)

    @property
    def total_tokens(self) -> int:
        return sum(self._tokens)

    def clear(self) -> List[Message]:
        removed = list(self._messages)
        self._messages = []
        self._tokens = []
        return removed

    def __len__(self) -> int:
        return len(self._messages)


__all__ = ["Message", "SlidingWindow", "estimate_tokens"]

