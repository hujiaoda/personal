# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) schema 探查只允许产生“表名/字段/类型/少量样例值”的紧凑摘要，绝不允许把
#    整库内容塞给模型；测试锁死输出里没有全表数据、样例值有数量与长度上限。
# 2) 样例值要可演示也要可脱敏：金额日期原样保留，邮箱/手机号必须打码，避免
#    把个人联系方式带进模型 prompt。
# 3) render_schema_summary 必须是合法紧凑 JSON，提示词可以直接反解与嵌用。

import json
import sqlite3

import pytest

from personal_data_assistant.data.schema import (
    DatabaseSchema,
    SchemaColumn,
    SchemaTable,
    discover_schema,
    redact_sample_value,
    render_schema_summary,
)


def make_rich_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE 餐饮明细 (
            id INTEGER PRIMARY KEY,
            日期 TEXT NOT NULL,
            类别 TEXT,
            金额 REAL,
            备注 TEXT
        );
        CREATE TABLE study_logs (
            id INTEGER PRIMARY KEY,
            subject TEXT,
            minutes INTEGER,
            email TEXT,
            phone TEXT
        );
        CREATE VIEW food_view AS SELECT 日期, 类别, 金额 FROM 餐饮明细;
        """
    )
    conn.executemany(
        "INSERT INTO 餐饮明细(日期, 类别, 金额, 备注) VALUES (?, ?, ?, ?)",
        [
            ("2025-08-01", "餐饮", 12.5, "早餐"),
            ("2025-08-02", "交通", 6.0, "地铁"),
            ("2025-08-03", "餐饮", 28.0, "午餐"),
            ("2025-08-04", "购物", 99.0, "日用品"),
        ],
    )
    conn.execute(
        "INSERT INTO study_logs(subject, minutes, email, phone) VALUES (?, ?, ?, ?)",
        ("Python", 60, "alice@example.com", "13812345678"),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def rich_db(tmp_path):
    return make_rich_db(tmp_path / "rich.db")


def test_discover_schema_lists_tables_and_views_without_sqlite_internals(rich_db):
    schema = discover_schema(rich_db)

    assert isinstance(schema, DatabaseSchema)
    assert [table.name for table in schema.tables] == ["餐饮明细", "study_logs", "food_view"]
    assert {table.kind for table in schema.tables} == {"table", "view"}


def test_columns_carry_name_type_pk_and_samples(rich_db):
    schema = discover_schema(rich_db, sample_size=2)
    food = schema.table("餐饮明细")
    assert food is not None

    columns = {column.name: column for column in food.columns}
    assert columns["金额"].type in {"REAL", "real"}
    assert columns["id"].primary_key is True
    # 样例值只取前 2 行，且日期/金额原样可见，方便模型理解口径
    assert columns["日期"].samples == ("2025-08-01", "2025-08-02")
    assert set(columns["金额"].samples) == {"12.5", "6.0"}


def test_sample_values_are_redacted_but_not_numbers(rich_db):
    schema = discover_schema(rich_db)
    study = schema.table("study_logs")
    columns = {column.name: column for column in study.columns}

    assert "@example.com" not in columns["email"].samples[0]
    assert "***" in columns["email"].samples[0]
    assert "13812345678" not in columns["phone"].samples[0]
    assert "138****5678" in columns["phone"].samples[0]
    # 金额/日期这类结构化数字不能误伤
    assert columns["email"].samples == ("al***@***.com",)


def test_sample_values_are_truncated_to_bounded_width(rich_db):
    conn = sqlite3.connect(rich_db)
    conn.execute(
        "INSERT INTO study_logs(subject, minutes, email, phone) VALUES (?, ?, ?, ?)",
        ("X" * 80, 30, "", ""),
    )
    conn.commit()
    conn.close()

    schema = discover_schema(rich_db, sample_size=3, max_text_width=12)
    study = schema.table("study_logs")
    subject = next(column for column in study.columns if column.name == "subject")

    assert any(len(sample) <= 15 for sample in subject.samples)
    assert any(sample.endswith("…") for sample in subject.samples)


def test_empty_database_yields_empty_schema(tmp_path):
    db_path = tmp_path / "empty.db"
    sqlite3.connect(db_path).close()

    schema = discover_schema(db_path)
    assert schema.tables == ()
    assert schema.table("whatever") is None


def test_render_schema_summary_is_compact_json_with_samples(rich_db):
    schema = discover_schema(rich_db, sample_size=1)
    text = render_schema_summary(schema)

    payload = json.loads(text)
    assert payload["tables"]
    food = next(item for item in payload["tables"] if item["name"] == "餐饮明细")
    assert food["kind"] == "table"
    assert [column["name"] for column in food["columns"]] == [
        "id",
        "日期",
        "类别",
        "金额",
        "备注",
    ]
    assert food["columns"][2]["samples"] == ["餐饮"]


def test_redact_sample_value_only_touches_contact_like_strings():
    assert redact_sample_value("alice@example.com") == "al***@***.com"
    assert redact_sample_value("13812345678") == "138****5678"
    assert redact_sample_value("2025-08-01") == "2025-08-01"
    assert redact_sample_value(12.5) == "12.5"


def test_schema_dataclasses_are_usable_directly():
    column = SchemaColumn(cid=0, name="id", type="INTEGER", primary_key=True, samples=("1",))
    table = SchemaTable(name="t", kind="table", columns=(column,), sample_rows=(("1",),))
    schema = DatabaseSchema(path=":memory:", tables=(table,))

    assert schema.table("t") is table
    assert schema.table("missing") is None
