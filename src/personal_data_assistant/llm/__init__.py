# -*- coding: utf-8 -*-
# 设计取舍：
# 1) llm 包只负责“怎么和模型说话”，不反向依赖 core/tools 的执行逻辑。
# 2) client 管 HTTP/重试/流式；prompts 管 JSON 动作协议的提示词模板。
# 3) 工具清单由 core 从 ToolRegistry 取来注入 prompts，避免 llm 直接依赖 tools。

from personal_data_assistant.llm.client import (
    LLMClientError,
    LLMHttpStatusError,
    LLMResponse,
    LLMResponseFormatError,
    LLMStreamFallbackError,
    DeepSeekClient,
    TokenUsage,
)

__all__ = [
    "DeepSeekClient",
    "LLMClientError",
    "LLMHttpStatusError",
    "LLMResponse",
    "LLMResponseFormatError",
    "LLMStreamFallbackError",
    "TokenUsage",
]
