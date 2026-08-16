# -*- coding: utf-8 -*-
# 设计取舍：
# 1) app 只做装配，不写业务：记忆是 core.loop 的外层组件，这里先把
#    manager.augment_question() 拼好再调 run_tool_loop；loop 代码零改动。
# 2) PersonalAssistant 的 remember/retrieve/close 都是薄代理，使用方只需要
#    一个入口对象，不需要同时理解 core 与 memory 两套生命周期。
# 3) M3 的 sql_query 也在这里注册进工具表：可以传现成 sql_query_tool，
#    也可以只给 user_db_path 让 app 用同一个模型自动装配；M1/M2 既有参数不变。

from __future__ import annotations

from typing import Any, Iterable, Optional, Union

from personal_data_assistant.core.loop import LoopResult, run_tool_loop
from personal_data_assistant.memory.retriever import MemoryManager
from personal_data_assistant.profile.habits import HabitAliasStore
from personal_data_assistant.tools.base import Tool
from personal_data_assistant.tools.registry import ToolRegistry
from personal_data_assistant.tools.sql_tools import create_sql_query_tool, ensure_sql_query_tool


class PersonalAssistant:
    """个人数据助手装配入口：core 循环 + 外层记忆系统 + M3 sql_query 工具。"""

    def __init__(
        self,
        *,
        model: Any,
        tools: Union[ToolRegistry, Iterable[Tool]],
        memory_manager: Optional[MemoryManager] = None,
        max_tool_rounds: int = 6,
        sql_query_tool: Optional[Tool] = None,
        user_db_path: Optional[str] = None,
        max_sql_fix_rounds: int = 3,
        sql_query_timeout: float = 5.0,
        sql_row_limit: int = 100,
        habits: Optional[HabitAliasStore] = None,
    ) -> None:
        if memory_manager is None:
            raise TypeError("memory_manager 不能为 None；请先装配 MemoryManager")
        if sql_query_tool is not None and user_db_path is not None:
            raise ValueError("sql_query_tool 与 user_db_path 只能二选一")
        if sql_query_tool is not None:
            ensure_sql_query_tool(sql_query_tool)

        registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(list(tools))
        if sql_query_tool is not None:
            registry.register(sql_query_tool)
        elif user_db_path is not None:
            registry.register(
                create_sql_query_tool(
                    model=model,
                    db_path=user_db_path,
                    max_fix_rounds=max_sql_fix_rounds,
                    query_timeout=sql_query_timeout,
                    max_rows=sql_row_limit,
                )
            )

        self._model = model
        self._tools = registry
        self._memory_manager: Optional[MemoryManager] = memory_manager
        self._max_tool_rounds = max_tool_rounds
        self._habits = habits

    def ask(
        self,
        question: str,
        *,
        stream: bool = False,
        on_chunk: Any = None,
        session_id: Optional[str] = None,
    ) -> LoopResult:
        """问答入口：先做习惯别名改写，再注入记忆上下文，最后进入 M1 核心循环。"""
        rewritten = question
        if self._habits is not None:
            rewritten = self._habits.rewrite_question(question).text
        augmented = self._memory_manager.augment_question(rewritten, session_id=session_id)
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

    def learn_alias(self, raw_term: str, canonical_term: str) -> Any:
        """把“用户说法 → 标准说法”写进习惯别名（M3 加分项，复用 KV 记忆）。"""
        if self._habits is None:
            raise RuntimeError("当前助手未装配 HabitAliasStore，无法学习别名")
        return self._habits.record_alias(raw_term, canonical_term)

    def close(self) -> None:
        manager = self._memory_manager
        self._memory_manager = None
        if manager is not None:
            manager.close()


__all__ = ["PersonalAssistant"]
