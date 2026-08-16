# -*- coding: utf-8 -*-
# 设计取舍：
# 1) memory 包只暴露稳定入口：MemoryManager 管编排，MemoryDatabase 管落库，
#    LLMSummarizer 管 LLM 摘要；内部模块细节不从这里散出去。
# 2) M2 起 memory 是 core.loop 的外层组件：core 不 import memory，
#    app 负责把 manager.augment_question() 的结果交给 run_tool_loop。

from personal_data_assistant.memory.long_term import MemoryDatabase
from personal_data_assistant.memory.models import (
    ALL_MEMORY_STRATEGIES,
    KVMemory,
    MemoryContext,
    MemoryContextItem,
    MemoryStrategy,
    Message,
    Summary,
)
from personal_data_assistant.memory.retriever import MemoryManager, replay_memory_strategies
from personal_data_assistant.memory.summarizer import LLMSummarizer
from personal_data_assistant.memory.window import SlidingWindow

__all__ = [
    "ALL_MEMORY_STRATEGIES",
    "KVMemory",
    "LLMSummarizer",
    "MemoryContext",
    "MemoryContextItem",
    "MemoryDatabase",
    "MemoryManager",
    "MemoryStrategy",
    "Message",
    "SlidingWindow",
    "Summary",
    "replay_memory_strategies",
]
