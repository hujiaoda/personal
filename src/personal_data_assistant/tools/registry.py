# -*- coding: utf-8 -*-
# 设计取舍：
# 1) 注册表是工具清单的唯一事实源：核心循环用它拼提示词，也用它分派执行，
#    绝不允许提示词里的工具和真正可执行的工具来自两份数据。
# 2) 未知工具抛 ToolNotFoundError 而不是返回 None，循环据此生成可回填模型的错误。
# 3) describe_for_prompt 输出紧凑 JSON，省 token；不带缩进的形态有测试锁住。

from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Tuple

from personal_data_assistant.tools.base import Tool


class ToolNotFoundError(LookupError):
    """请求了未注册的工具。"""


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: Dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name!r}")
        self._tools[tool.name] = tool

    def find(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"未注册的工具: {name!r}")
        return tool

    def list_tools(self) -> Tuple[Tool, ...]:
        return tuple(self._tools.values())

    def describe(self) -> List[Dict[str, object]]:
        return [tool.to_schema() for tool in self._tools.values()]

    def describe_for_prompt(self) -> str:
        """输出可直接嵌进系统提示词的紧凑 JSON 工具清单。"""
        return json.dumps(self.describe(), ensure_ascii=False, separators=(",", ":"))

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
