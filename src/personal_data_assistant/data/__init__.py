# -*- coding: utf-8 -*-
# data 包负责“用户自己的 SQLite 小表”的只读访问、结构探查与智能问数编排。
# 边界：不 import llm/memory/profile，只接受鸭子协议模型对象，由 app/tools 装配。
"""M3 智能问数：只读 SQLite、schema 探查、Text-to-SQL 编排与演示数据。"""

from personal_data_assistant.data.ask import AskResult, ask_database
from personal_data_assistant.data.schema import DatabaseSchema, discover_schema
from personal_data_assistant.data.sqlite import ReadOnlySQLite

__all__ = ["AskResult", "DatabaseSchema", "ReadOnlySQLite", "ask_database", "discover_schema"]
