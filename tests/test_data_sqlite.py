# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) 用户数据库安全是 M3 的地基，先锁死四条底线：只读连接、SELECT/WITH 白名单、
#    禁止多语句、执行超时。每条都用一个真实临时 SQLite 文件验证，不 mock 边界。
# 2) 白名单判断只认“去除前导空白与 SQL 注释后的第一个关键字”，大小写不敏感；
#    分号计数必须理解字符串/标识符/注释，不能简单 str.count(";")。
# 3) 超时用递归 CTE 造一个必跑超时的查询，并断言墙钟时间有上界，防止实现偷偷
#    用线程 sleep 或无限重试把测试拖死。

import sqlite3
import time

import pytest

from personal_data_assistant.data.sqlite import (
    QueryResult,
    ReadOnlySQLite,
    SQLDatabaseError,
    SQLExecutionError,
    SQLSecurityError,
    SQLTimeoutError,
    split_sql_statements,
    validate_readonly_sql,
)


def make_expenses_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE expenses(id INTEGER PRIMARY KEY, item TEXT, amount REAL)"
    )
    conn.executemany(
        "INSERT INTO expenses(item, amount) VALUES (?, ?)",
        [("早餐", 8.0), ("地铁", 5.0), ("午餐", 25.0), ("咖啡", 15.0), ("晚餐", 40.0)],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def expenses_db(tmp_path):
    return make_expenses_db(tmp_path / "user.db")


def test_select_returns_columns_rows_and_row_count(expenses_db):
    db = ReadOnlySQLite(expenses_db)
    try:
        result = db.execute_query("SELECT item, amount FROM expenses ORDER BY id")

        assert isinstance(result, QueryResult)
        assert result.columns == ("item", "amount")
        assert result.rows[0] == ("早餐", 8.0)
        assert result.row_count == 5
        assert result.truncated is False
    finally:
        db.close()


def test_with_query_and_trailing_semicolon_are_accepted(expenses_db):
    db = ReadOnlySQLite(expenses_db)
    try:
        result = db.execute_query(
            "WITH t AS (SELECT SUM(amount) AS total FROM expenses) SELECT total FROM t;"
        )
        assert result.row_count == 1
        assert result.rows[0][0] == 93.0
    finally:
        db.close()


def test_leading_comments_and_lowercase_keyword_are_accepted(expenses_db):
    db = ReadOnlySQLite(expenses_db)
    try:
        result = db.execute_query(
            "-- 用户问：早餐花了多少\n/* 只读查询 */\nselect amount from expenses where item='早餐';"
        )
        assert result.rows == ((8.0,),)
    finally:
        db.close()


def test_semicolon_inside_string_literal_is_not_a_statement_split(expenses_db):
    db = ReadOnlySQLite(expenses_db)
    try:
        result = db.execute_query("SELECT 'a;b' AS text, amount FROM expenses WHERE item='早餐'")
        assert result.rows == (("a;b", 8.0),)
    finally:
        db.close()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO expenses(item, amount) VALUES ('夜宵', 30)",
        "UPDATE expenses SET amount = 1",
        "DELETE FROM expenses",
        "DROP TABLE expenses",
        "CREATE TABLE hacked(x TEXT)",
        "ALTER TABLE expenses ADD COLUMN note TEXT",
        "PRAGMA journal_mode = WAL",
        "EXPLAIN SELECT * FROM expenses",
        "VACUUM",
        "ATTACH DATABASE '/tmp/x.db' AS x",
    ],
)
def test_write_and_non_query_statements_are_rejected(expenses_db, sql):
    db = ReadOnlySQLite(expenses_db)
    try:
        with pytest.raises(SQLSecurityError):
            db.execute_query(sql)
    finally:
        db.close()

    check = sqlite3.connect(expenses_db)
    try:
        assert check.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 5
    finally:
        check.close()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE expenses;",
        "WITH t AS (SELECT 1) SELECT * FROM t; DELETE FROM expenses;",
        "SELECT 1; /* comment */ SELECT 2",
    ],
)
def test_multiple_statements_are_rejected(expenses_db, sql):
    db = ReadOnlySQLite(expenses_db)
    try:
        with pytest.raises(SQLSecurityError):
            db.execute_query(sql)
    finally:
        db.close()


def test_incomplete_or_empty_sql_is_rejected(expenses_db):
    db = ReadOnlySQLite(expenses_db)
    try:
        with pytest.raises(SQLSecurityError):
            db.execute_query("")
        with pytest.raises(SQLSecurityError):
            db.execute_query("   ")
        # 白名单管“是不是只读单条”，语法残缺归执行错误；两条异常都必须结构化
        with pytest.raises(SQLExecutionError):
            db.execute_query("SELECT * FROM")
    finally:
        db.close()


def test_execution_timeout_interrupts_long_running_query(expenses_db):
    db = ReadOnlySQLite(expenses_db, query_timeout=0.05)
    started = time.monotonic()
    try:
        with pytest.raises(SQLTimeoutError):
            db.execute_query(
                "WITH RECURSIVE cnt(x) AS (VALUES(1) UNION ALL "
                "SELECT x + 1 FROM cnt WHERE x < 100000000) SELECT sum(x) FROM cnt"
            )
    finally:
        elapsed = time.monotonic() - started
        db.close()
    assert elapsed < 2.0


def test_max_rows_truncates_results_with_flag(expenses_db):
    db = ReadOnlySQLite(expenses_db, max_rows=2)
    try:
        result = db.execute_query("SELECT item FROM expenses ORDER BY id")
        assert result.row_count == 2
        assert len(result.rows) == 2
        assert result.truncated is True
    finally:
        db.close()


def test_missing_database_raises_database_error_not_crash(tmp_path):
    db = ReadOnlySQLite(tmp_path / "missing.db")
    try:
        with pytest.raises(SQLDatabaseError):
            db.execute_query("SELECT 1")
    finally:
        db.close()


def test_close_is_idempotent(expenses_db):
    db = ReadOnlySQLite(expenses_db)
    db.close()
    db.close()


def test_split_sql_statements_understands_comments_and_identifiers():
    assert split_sql_statements("SELECT 1") == ["SELECT 1"]
    assert split_sql_statements("SELECT 1;") == ["SELECT 1"]
    assert split_sql_statements("SELECT 'a;b' AS x") == ["SELECT 'a;b' AS x"]
    assert split_sql_statements('SELECT "a;b" AS x -- 注释;不是语句') == [
        'SELECT "a;b" AS x -- 注释;不是语句'
    ]
    assert split_sql_statements("SELECT [a;b] FROM t /* x;y */") == [
        "SELECT [a;b] FROM t /* x;y */"
    ]
    assert split_sql_statements("SELECT 1; SELECT 2") == ["SELECT 1", "SELECT 2"]


def test_validate_readonly_sql_returns_trimmed_single_statement():
    assert validate_readonly_sql("  SELECT 1;  ") == "SELECT 1"
    assert validate_readonly_sql("SELECT 1; -- 尾注不是第二条语句") == "SELECT 1"
    assert validate_readonly_sql("/* 注释 */ WITH t AS (SELECT 1) SELECT * FROM t") == (
        "/* 注释 */ WITH t AS (SELECT 1) SELECT * FROM t"
    )
    with pytest.raises(SQLSecurityError):
        validate_readonly_sql("SELECT 1; /* 注释 */ SELECT 2")
