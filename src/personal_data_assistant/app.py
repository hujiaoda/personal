# -*- coding: utf-8 -*-
# 设计取舍：
# 1) app 只做装配，不写业务：记忆是 core.loop 的外层组件，这里先把
#    manager.augment_question() 拼好再调 run_tool_loop；loop 代码零改动。
# 2) PersonalAssistant 的 remember/retrieve/close 都是薄代理，使用方只需要
#    一个入口对象，不需要同时理解 core 与 memory 两套生命周期。
# 3) M3/M4 的工具注册、SQL 只读访问、FastAPI 路由都从这一个装配点扩出去。

from __future__ import annotations

from typing import Any, Iterable, Optional, Union

from personal_data_assistant.core.loop import LoopResult, run_tool_loop
from personal_data_assistant.memory.retriever import MemoryManager
from personal_data_assistant.tools.base import Tool
from personal_data_assistant.tools.registry import ToolRegistry


class PersonalAssistant:
    """个人数据助手装配入口：core 循环 + 外层记忆系统。"""

    def __init__(
        self,
        *,
        model: Any,
        tools: Union[ToolRegistry, Iterable[Tool]],
        memory_manager: Optional[MemoryManager] = None,
        max_tool_rounds: int = 6,
    ) -> None:
        if memory_manager is None:
            raise TypeError("memory_manager 不能为 None；请先装配 MemoryManager")
        self._model = model
        self._tools = tools
        self._memory_manager: Optional[MemoryManager] = memory_manager
        self._max_tool_rounds = max_tool_rounds

    def ask(
        self,
        question: str,
        *,
        stream: bool = False,
        on_chunk: Any = None,
        session_id: Optional[str] = None,
    ) -> LoopResult:
        """问答入口：注入记忆上下文后进入 M1 核心循环。"""
        augmented = self._memory_manager.augment_question(question, session_id=session_id)
        return run_tool_loop(
            augmented,
            self._tools,
            self._model,
            max_tool_rounds=self._max_tool_rounds,
            stream=stream,
            on_chunk=on_chunk,
        )

    def remember(self, key: str, value: str, **kwargs: Any) -> Any:
        return self._memory_manager.remember(key, value, **kwargs)

    def retrieve(self, question: str, **kwargs: Any) -> Any:
        return self._memory_manager.retrieve(question, **kwargs)

    def close(self) -> None:
        manager = self._memory_manager
        self._memory_manager = None
        if manager is not None:
            manager.close()


__all__ = ["PersonalAssistant"]
