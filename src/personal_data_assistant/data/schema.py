# -*- coding: utf-8 -*-
# 设计取舍：
# 1) schema 探查产出“最小可用上下文”：表名、字段名、声明类型、主键、每列最多
#    3 个样例值；不把全表数据塞给模型，既省 token 也避免模型“背数据”代替写 SQL。
# 2) 样例值先脱敏再截断：邮箱/11 位手机号打码，普通数字（金额、日期、时长）
#    原样保留——问数场景里这些数字正是模型判断口径的依据，不能误伤。
# 3) 只走 ReadOnlySQLite 的只读通道；表和视图都列出，内部表 sqlite_% 排除。

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple, Union

from personal_data_assistant.data.sqlite import (
    ReadOnlySQLite,
    SQLDatabaseError,
    SQLExecutionError,
    quote_identifier,
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^1\d{10}$")


@dataclass(frozen=True)
class SchemaColumn:
    """表的一列；samples 是脱敏、截断后的字符串样例（最多 sample_size 个）。"""

    cid: int
    name: str
    type: str
    notnull: bool = False
    primary_key: bool = False
    samples: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SchemaTable:
    """一张用户表或视图。sample_rows 只用于调试留痕，不进入渲染摘要。"""

    name: str
    kind: str  # table / view
    columns: Tuple[SchemaColumn, ...] = ()
    sample_rows: Tuple[Tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class DatabaseSchema:
    """一个用户库的完整结构摘要。"""

    path: str
    tables: Tuple[SchemaTable, ...] = ()

    def table(self, name: str) -> Optional[SchemaTable]:
        for table in self.tables:
            if table.name == name:
                return table
        return None


def redact_sample_value(value: Any) -> str:
    """样例值转字符串并脱敏：邮箱/手机号打码，其余保持可读原值。"""
    text = str(value)
    if _EMAIL_RE.match(text):
        local, _, domain = text.partition("@")
        tail = domain.rsplit(".", 1)[-1]
        return f"{local[:2]}***@***.{tail}"
    if _PHONE_RE.match(text):
        return f"{text[:3]}****{text[-4:]}"
    return text


def _truncate(text: str, max_text_width: int) -> str:
    if len(text) <= max_text_width:
        return text
    return text[:max_text_width] + "…"


def _normalized_table_rows(db: ReadOnlySQLite, table: SchemaTable, sample_size: int) -> Tuple[Tuple[str, ...], ...]:
    """取前 sample_size 行做样例；视图/空表失败时返回空，不阻断 schema 探查。"""
    columns_sql = ", ".join(quote_identifier(column.name) for column in table.columns)
    if not columns_sql:
        return ()
    try:
        result = db.execute_query(
            f"SELECT {columns_sql} FROM {quote_identifier(table.name)} LIMIT ?",
            (sample_size,),
        )
    except (SQLExecutionError, SQLDatabaseError):
        return ()
    return tuple(tuple(str(value) for value in row) for row in result.rows)


def discover_schema(
    db_path: Union[str, bytes],
    *,
    sample_size: int = 3,
    max_text_width: int = 40,
    query_timeout: float = 5.0,
) -> DatabaseSchema:
    """读 sqlite_master + pragma_table_info，生成紧凑表结构摘要。"""
    if sample_size < 1:
        raise ValueError(f"sample_size 必须 >= 1，当前: {sample_size}")
    if max_text_width < 1:
        raise ValueError(f"max_text_width 必须 >= 1，当前: {max_text_width}")

    db = ReadOnlySQLite(db_path, query_timeout=query_timeout)
    try:
        result = db.execute_query(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY rowid"
        )
        tables: List[SchemaTable] = []
        for name_value, kind_value in result.rows:
            table_name = str(name_value)
            kind = str(kind_value)
            columns: List[SchemaColumn] = []
            try:
                info = db.execute_query(
                    "SELECT cid, name, type, \"notnull\", pk FROM pragma_table_info(?)",
                    (table_name,),
                )
            except (SQLExecutionError, SQLDatabaseError):
                continue
            for cid, column_name, column_type, notnull, pk in info.rows:
                columns.append(
                    SchemaColumn(
                        cid=int(cid),
                        name=str(column_name),
                        type=str(column_type or ""),
                        notnull=bool(notnull),
                        primary_key=bool(pk),
                        samples=(),
                    )
                )
            table = SchemaTable(name=table_name, kind=kind, columns=tuple(columns))
            sample_rows = _normalized_table_rows(db, table, sample_size)
            column_samples: List[List[str]] = [[] for _ in table.columns]
            for row in sample_rows:
                for index, value in enumerate(row):
                    if index >= len(column_samples):
                        break
                    if value is None or value == "None":
                        continue
                    samples = column_samples[index]
                    if len(samples) < sample_size:
                        samples.append(
                            _truncate(redact_sample_value(value), max_text_width)
                        )
            enriched_columns = tuple(
                SchemaColumn(
                    cid=column.cid,
                    name=column.name,
                    type=column.type,
                    notnull=column.notnull,
                    primary_key=column.primary_key,
                    samples=tuple(column_samples[index]),
                )
                for index, column in enumerate(table.columns)
            )
            tables.append(
                SchemaTable(
                    name=table.name,
                    kind=table.kind,
                    columns=enriched_columns,
                    sample_rows=sample_rows,
                )
            )
        return DatabaseSchema(path=str(db_path), tables=tuple(tables))
    finally:
        db.close()


def render_schema_summary(schema: DatabaseSchema) -> str:
    """把 schema 渲染成可嵌入提示词的紧凑 JSON；无缩进形态有测试锁住。"""
    payload = {
        "tables": [
            {
                "name": table.name,
                "kind": table.kind,
                "columns": [
                    {
                        "name": column.name,
                        "type": column.type,
                        "primary_key": column.primary_key,
                        "samples": list(column.samples),
                    }
                    for column in table.columns
                ],
            }
            for table in schema.tables
        ]
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "DatabaseSchema",
    "SchemaColumn",
    "SchemaTable",
    "discover_schema",
    "redact_sample_value",
    "render_schema_summary",
]
