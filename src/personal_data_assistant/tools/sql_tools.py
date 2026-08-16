# -*- coding: utf-8 -*-
# 设计取舍：
# 1) sql_query 是普通 M1 Tool，注册进 ToolRegistry 后核心循环零改动即可调用；
#    库路径在 create_sql_query_tool 装配期锁死，模型只能传 question，不能指库。
# 2) 工具内部跑 data.ask 的子循环（schema → SQL → 执行 → 修正 → 解释），
#    对外成功返回 JSON 友好的结构化 dict（答案 + SQL + 数据 + 评测埋点）。
# 3) 子流程失败时 ToolResult(ok=False, result=payload, error=...)，核心循环会把
#    payload 与 error 都回填给外层模型，外层仍可以给出对用户有用的 final 答案。

from __future__ import annotations

from typing import Any

from personal_data_assistant.data.ask import AskResult, ask_database
from personal_data_assistant.tools.base import Tool, ToolResult

_SQL_QUERY_DESCRIPTION = (
    "查询用户本地 SQLite 数据表：输入中文大白话问题，自动探查表结构、生成并执行"
    "只读 SQL（只允许 SELECT/WITH），执行失败会自动修正后重试，最后用中文解释结果。"
)


def create_sql_query_tool(
    *,
    model: Any,
    db_path: str,
    max_fix_rounds: int = 3,
    query_timeout: float = 5.0,
    max_rows: int = 100,
    schema_sample_size: int = 3,
) -> Tool:
    """装配 sql_query 工具。所有安全参数在装配期固定，模型无法覆盖。"""

    def run(args: dict) -> ToolResult:
        result = ask_database(
            args["question"],
            db_path,
            model,
            max_fix_rounds=max_fix_rounds,
            query_timeout=query_timeout,
            max_rows=max_rows,
            schema_sample_size=schema_sample_size,
        )
        payload = result.to_dict()
        if result.status == "success":
            return ToolResult(ok=True, result=payload)
        return ToolResult(ok=False, result=payload, error=payload["error"] or payload["answer"])

    return Tool(
        name="sql_query",
        description=_SQL_QUERY_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "用户关于本地数据表的中文大白话问题，例如：八月餐饮花了多少钱",
                }
            },
            "required": ["question"],
        },
        func=run,
    )


def ensure_sql_query_tool(tool: Any) -> Tool:
    """装配期校验：入参必须是 sql_query 工具，避免把普通工具误接成问数入口。"""
    if not isinstance(tool, Tool) or tool.name != "sql_query":
        raise TypeError("sql_query_tool 必须由 create_sql_query_tool 创建")
    return tool


__all__ = ["create_sql_query_tool", "ensure_sql_query_tool"]
