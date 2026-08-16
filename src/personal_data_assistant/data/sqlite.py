# -*- coding: utf-8 -*-
# 设计取舍：
# 1) ADR-4 的落地层：用户库只用 SQLite URI 的 mode=ro 打开，并额外执行
#    PRAGMA query_only=ON 作为第二道闸；SQL 在进 SQLite 前先过白名单。
# 2) 白名单不是简单 startswith：先按状态机把注释/字符串/引号标识符里的分号
#    识别掉，确认只有一条语句，再剥掉前导注释看第一个关键字是否 SELECT/WITH。
# 3) 执行超时用 SQLite progress handler + 单调时钟：同线程、无需杀线程，到点
#    直接让 SQLite 中断，返回 SQLTimeoutError；上层拿到结构化错误可回填模型修正。
# 4) 行数保护用 fetchmany(max_rows + 1)：不改写用户 SQL（避免 CTE/UNION 下
#    盲目拼 LIMIT 出错），只限制实际拿回内存并标记 truncated。

from __future__ import annotations

import os
import re
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple, Union


class SQLSecurityError(ValueError):
    """SQL 未通过只读白名单或不是单条语句。"""


class SQLExecutionError(RuntimeError):
    """SQL 执行失败（语法、缺表、类型错误等）。"""


class SQLTimeoutError(SQLExecutionError):
    """SQL 执行超过超时时间被中断。"""


class SQLDatabaseError(SQLExecutionError):
    """数据库打开/连接失败。"""


@dataclass(frozen=True)
class QueryResult:
    """一次只读查询的结果；rows 已按 max_rows 截断。"""

    columns: Tuple[str, ...]
    rows: Tuple[Tuple[Any, ...], ...]
    row_count: int
    truncated: bool = False


def split_sql_statements(sql: str) -> List[str]:
    """把 SQL 文本按语句分隔；分号只认字符串/引号标识符/注释之外的裸分号。

    返回的语句已经 strip，末尾常规的分号结束符被去掉；中间的空语句会保留，
    供 validate_readonly_sql 判定“多语句/夹带空语句”。
    """
    statements: List[str] = []
    buffer: List[str] = []
    state = "normal"  # normal / single / double / backtick / bracket / line / block
    i = 0
    length = len(sql)

    while i < length:
        char = sql[i]

        if state == "normal":
            if char == "'":
                state = "single"
            elif char == '"':
                state = "double"
            elif char == "`":
                state = "backtick"
            elif char == "[":
                state = "bracket"
            elif char == "-" and i + 1 < length and sql[i + 1] == "-":
                state = "line"
            elif char == "/" and i + 1 < length and sql[i + 1] == "*":
                state = "block"
            elif char == ";":
                statements.append("".join(buffer))
                buffer = []
                i += 1
                continue
            buffer.append(char)
            i += 1
            continue

        if state == "single":
            buffer.append(char)
            if char == "'":
                if i + 1 < length and sql[i + 1] == "'":
                    buffer.append(sql[i + 1])
                    i += 2
                    continue
                state = "normal"
            i += 1
            continue

        if state == "double":
            buffer.append(char)
            if char == '"':
                if i + 1 < length and sql[i + 1] == '"':
                    buffer.append(sql[i + 1])
                    i += 2
                    continue
                state = "normal"
            i += 1
            continue

        if state == "backtick":
            buffer.append(char)
            if char == "`":
                if i + 1 < length and sql[i + 1] == "`":
                    buffer.append(sql[i + 1])
                    i += 2
                    continue
                state = "normal"
            i += 1
            continue

        if state == "bracket":
            buffer.append(char)
            if char == "]":
                state = "normal"
            i += 1
            continue

        if state == "line":
            buffer.append(char)
            if char == "\n":
                state = "normal"
            i += 1
            continue

        # block comment
        buffer.append(char)
        if char == "*" and i + 1 < length and sql[i + 1] == "/":
            buffer.append(sql[i + 1])
            i += 2
            state = "normal"
            continue
        i += 1

    statements.append("".join(buffer))
    result = [statement.strip() for statement in statements]
    # 允许末尾有一个普通分号结束符；中间的/开头的空语句保留，让校验层拒绝。
    while len(result) >= 2 and result[-1] == "":
        result.pop()
    return result


def _strip_leading_comments(text: str) -> str:
    """剥掉前导空白、-- 行注释与块注释，用于取第一个关键字。"""
    while True:
        text = text.lstrip()
        if text.startswith("--"):
            newline = text.find("\n")
            text = "" if newline == -1 else text[newline + 1 :]
            continue
        if text.startswith("/*"):
            end = text.find("*/", 2)
            if end == -1:
                return text  # 未闭合注释：后面拿不到合法关键字，自然拒绝
            text = text[end + 2 :]
            continue
        return text


_ALLOWED_FIRST_KEYWORD = re.compile(r"(SELECT|WITH)\b", re.IGNORECASE)


def validate_readonly_sql(sql: str) -> str:
    """只读白名单：单条语句 + 剥掉前导注释后以 SELECT/WITH 开头。"""
    if not isinstance(sql, str) or not sql.strip():
        raise SQLSecurityError("SQL 不能为空")

    statements = split_sql_statements(sql)
    # 只有“剥掉注释后仍有内容”的片段才算语句；纯注释尾注（如 SELECT 1; -- done）
    # 不是第二条语句，但注释后面再跟 SELECT 就是真正的多语句。
    meaningful = [item for item in statements if _strip_leading_comments(item).strip()]
    if len(meaningful) != 1:
        raise SQLSecurityError(
            f"只允许单条 SQL 语句，当前识别到 {len(meaningful)} 条；"
            "多语句即使中间隔着注释也会被拒绝"
        )
    statement = meaningful[0].strip()
    if not statement:
        raise SQLSecurityError("SQL 不能为空")

    first_token_start = _strip_leading_comments(statement)
    if not _ALLOWED_FIRST_KEYWORD.match(first_token_start):
        raise SQLSecurityError("只允许 SELECT/WITH 开头的只读查询，写操作与 PRAGMA 等一律拒绝")
    return statement


def _readonly_uri(db_path: Union[str, os.PathLike]) -> str:
    path_text = os.fspath(db_path)
    if path_text == ":memory:":
        return "file::memory:?mode=memory&cache=private"
    # 路径必须 URI 编码，避免空格、中文、?、# 等字符破坏只读参数。
    encoded = urllib.parse.quote(path_text, safe="/")
    return f"file:{encoded}?mode=ro"


def quote_identifier(name: str) -> str:
    """把表名/列名安全地放进双引号标识符。"""
    return '"' + str(name).replace('"', '""') + '"'


class ReadOnlySQLite:
    """用户数据库的只读执行器：白名单 + mode=ro + query_only + 超时 + 行数保护。"""

    def __init__(
        self,
        db_path: Union[str, os.PathLike],
        *,
        query_timeout: float = 5.0,
        max_rows: int = 100,
        busy_timeout: float = 5.0,
    ) -> None:
        if query_timeout <= 0:
            raise ValueError(f"query_timeout 必须 > 0，当前: {query_timeout}")
        if max_rows < 1:
            raise ValueError(f"max_rows 必须 >= 1，当前: {max_rows}")
        if busy_timeout <= 0:
            raise ValueError(f"busy_timeout 必须 > 0，当前: {busy_timeout}")
        self.path = os.fspath(db_path)
        self.query_timeout = float(query_timeout)
        self.max_rows = int(max_rows)
        self.busy_timeout = float(busy_timeout)
        self._conn: Optional[sqlite3.Connection] = None
        self._closed = False

    def _ensure_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise SQLDatabaseError("数据库连接已关闭")
        if self._conn is not None:
            return self._conn
        try:
            conn = sqlite3.connect(
                _readonly_uri(self.path),
                uri=True,
                timeout=self.busy_timeout,
            )
            conn.execute("PRAGMA query_only = ON")
            conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout * 1000)}")
        except sqlite3.Error as exc:
            raise SQLDatabaseError(f"无法打开只读数据库 {self.path!r}: {exc}") from exc
        self._conn = conn
        return conn

    def execute_query(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> QueryResult:
        """执行一条只读 SQL；任何安全/超时/执行错误都转成结构化异常。"""
        statement = validate_readonly_sql(sql)
        conn = self._ensure_connection()
        deadline = time.monotonic() + self.query_timeout

        def _progress() -> int:
            return 1 if time.monotonic() >= deadline else 0

        conn.set_progress_handler(_progress, 1000)
        elapsed = 0.0
        try:
            started = time.monotonic()
            cursor = conn.execute(statement, tuple(params))
            raw_rows = cursor.fetchmany(self.max_rows + 1)
            elapsed = time.monotonic() - started
            columns = tuple(str(item[0]) for item in (cursor.description or ()))
            truncated = len(raw_rows) > self.max_rows
            rows = tuple(tuple(row) for row in raw_rows[: self.max_rows])
            return QueryResult(
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
            )
        except sqlite3.OperationalError as exc:
            elapsed = time.monotonic() - started
            if "interrupted" in str(exc) and elapsed >= self.query_timeout:
                raise SQLTimeoutError(
                    f"SQL 执行超过 {self.query_timeout:g} 秒被中断: {exc}"
                ) from exc
            if "readonly" in str(exc).lower() or "attempt to write" in str(exc).lower():
                raise SQLSecurityError(f"只读连接拒绝了写操作: {exc}") from exc
            raise SQLExecutionError(f"SQL 执行失败: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            if "readonly" in str(exc).lower() or "attempt to write" in str(exc).lower():
                raise SQLSecurityError(f"只读连接拒绝了写操作: {exc}") from exc
            raise SQLExecutionError(f"SQL 执行失败: {exc}") from exc
        finally:
            if self._conn is not None:
                try:
                    self._conn.set_progress_handler(None, 1000)
                except sqlite3.Error:
                    pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "ReadOnlySQLite":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


__all__ = [
    "QueryResult",
    "ReadOnlySQLite",
    "SQLDatabaseError",
    "SQLExecutionError",
    "SQLSecurityError",
    "SQLTimeoutError",
    "quote_identifier",
    "split_sql_statements",
    "validate_readonly_sql",
]
