# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) ask_database 是 sql_query 工具的大脑，全部用脚本化 fake complete 测试：
#    首轮成功、失败修正、写操作拒绝、修正次数耗尽、解释失败降级、模型崩溃。
# 2) 评测埋点锁字段不锁算法：M3 只要求每个 AskResult 能记录
#    首次是否成功/修正是否成功/修正轮数/token 成本；成功率与平均值留给 M5 聚合。
# 3) 解释失败时，已经查出的数据必须有确定性中文兜底（含数字、口径、SQL 原文），
#    这对应架构文档里“若 SQL 已算出则直接格式化返回数值”的 B 计划。

import sqlite3

import pytest

from personal_data_assistant.data.ask import (
    AskResult,
    SqlAttempt,
    SqlEvalRecord,
    ask_database,
    extract_sql_text,
    format_result_fallback,
)
from personal_data_assistant.llm.client import LLMResponse, TokenUsage


def make_expenses_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE expenses("
        "id INTEGER PRIMARY KEY, date TEXT, category TEXT, item TEXT, amount REAL)"
    )
    conn.executemany(
        "INSERT INTO expenses(date, category, item, amount) VALUES (?, ?, ?, ?)",
        [
            ("2025-08-01", "餐饮", "早餐", 8.0),
            ("2025-08-02", "餐饮", "午餐", 25.0),
            ("2025-08-03", "交通", "地铁", 5.0),
            ("2025-08-04", "购物", "日用品", 99.0),
        ],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def expenses_db(tmp_path):
    return make_expenses_db(tmp_path / "ask.db")


class ScriptedCompleteModel:
    """与 DeepSeekClient.complete 同形的 fake；响应可为 str/LLMResponse/Exception。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, LLMResponse):
            return item
        return LLMResponse(
            content=item,
            model="fake",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            raw={},
        )


GOOD_SQL = "SELECT category, SUM(amount) AS total FROM expenses WHERE category = '餐饮' GROUP BY category"
FENCED_GOOD_SQL = f"```sql\n{GOOD_SQL};\n```"
BAD_SQL = "SELECT amount FROM not_exist_table"
RECURSIVE_SQL = (
    "WITH RECURSIVE cnt(x) AS (VALUES(1) UNION ALL "
    "SELECT x + 1 FROM cnt WHERE x < 100000000) SELECT sum(x) FROM cnt"
)


def test_first_attempt_success_explains_with_numbers_and_sql(expenses_db):
    model = ScriptedCompleteModel(
        [
            FENCED_GOOD_SQL,
            "2025 年 8 月餐饮合计 33.0 元，使用了 expenses.category 与 amount 字段。",
        ]
    )

    result = ask_database("八月餐饮花了多少钱", expenses_db, model)

    assert isinstance(result, AskResult)
    assert result.status == "success"
    assert result.answer == "2025 年 8 月餐饮合计 33.0 元，使用了 expenses.category 与 amount 字段。"
    assert "SELECT" in result.sql.upper()
    assert result.columns == ("category", "total")
    assert result.rows == (("餐饮", 33.0),)
    assert result.row_count == 1

    assert result.attempts == 1
    assert result.first_attempt_success is True
    assert result.fix_success is False
    assert result.total_fix_rounds == 0
    assert result.model_calls == 2  # 生成 SQL + 中文解释
    assert result.usage.total_tokens == 30
    assert len(result.attempts_log) == 1
    assert result.attempts_log[0].ok is True


def test_bad_sql_is_fed_back_and_fixed_once(expenses_db):
    model = ScriptedCompleteModel(
        [
            BAD_SQL,
            GOOD_SQL,
            "餐饮合计 33.0 元。",
        ]
    )

    result = ask_database("八月餐饮花了多少钱", expenses_db, model)

    assert result.status == "success"
    assert result.attempts == 2
    assert result.first_attempt_success is False
    assert result.fix_success is True
    assert result.total_fix_rounds == 1
    assert result.attempts_log[0].ok is False
    assert "not_exist_table" in result.attempts_log[0].error
    assert result.attempts_log[1].ok is True

    # 修正 prompt 必须同时带上前一条 SQL 与真实错误信息
    fix_call = model.calls[1][-1]["content"]
    assert BAD_SQL in fix_call
    assert "no such table" in fix_call


def test_write_sql_is_rejected_and_then_corrected(expenses_db):
    model = ScriptedCompleteModel(
        [
            "DELETE FROM expenses WHERE category = '餐饮';",
            GOOD_SQL,
            "餐饮合计 33.0 元。",
        ]
    )

    result = ask_database("八月餐饮花了多少钱", expenses_db, model)

    assert result.status == "success"
    assert result.fix_success is True
    assert "只允许 SELECT/WITH" in result.attempts_log[0].error
    assert "DELETE" in result.attempts_log[0].sql.upper()


def test_fix_rounds_are_capped_and_answer_shows_attempted_sql(expenses_db):
    bad_sqls = [
        "DELETE FROM expenses",
        "DROP TABLE expenses",
        "UPDATE expenses SET amount = 0",
        "INSERT INTO expenses(date) VALUES ('2025-01-01')",
    ]
    model = ScriptedCompleteModel(bad_sqls)

    result = ask_database("清空餐饮记录", expenses_db, model, max_fix_rounds=3)

    assert result.status == "failed"
    assert result.attempts == 4  # 首次 + 最多 3 次修正
    assert result.first_attempt_success is False
    assert result.fix_success is False
    assert result.total_fix_rounds == 3
    assert "这个问题我没查到" in result.answer
    assert "试过的 SQL" in result.answer
    for bad_sql in bad_sqls:
        assert bad_sql in result.answer
    assert result.error is not None


def test_timeout_error_can_be_corrected_with_cheaper_query(expenses_db):
    model = ScriptedCompleteModel(
        [
            RECURSIVE_SQL,
            GOOD_SQL,
            "餐饮合计 33.0 元。",
        ]
    )

    result = ask_database(
        "八月餐饮花了多少钱", expenses_db, model, query_timeout=0.05
    )

    assert result.status == "success"
    assert result.total_fix_rounds == 1
    assert result.fix_success is True
    assert "SQLTimeoutError" in result.attempts_log[0].error
    assert "超过" in result.attempts_log[0].error


def test_explanation_model_failure_falls_back_to_formatted_data(expenses_db):
    model = ScriptedCompleteModel([GOOD_SQL, RuntimeError("explain down")])

    result = ask_database("八月餐饮花了多少钱", expenses_db, model)

    assert result.status == "success"
    assert result.answer != ""
    assert "33.0" in result.answer
    assert GOOD_SQL in result.answer
    assert result.model_calls == 2  # 解释调用尝试过但失败，也要留痕
    assert result.usage.total_tokens == 15  # 只有生成 SQL 那一次拿到了 usage


def test_sql_model_failure_returns_structured_chinese_error(expenses_db):
    model = ScriptedCompleteModel([RuntimeError("connection refused")])

    result = ask_database("八月餐饮花了多少钱", expenses_db, model)

    assert result.status == "model_error"
    assert result.answer != ""
    assert "模型" in result.answer
    assert "connection refused" in result.error
    assert result.attempts == 0


def test_zero_rows_are_a_success_with_clear_no_data_answer(expenses_db):
    model = ScriptedCompleteModel(
        [
            "SELECT * FROM expenses WHERE category = '不存在的类别'",
            "没有查到符合条件的数据。",
        ]
    )

    result = ask_database("查一个不存在的类别", expenses_db, model)

    assert result.status == "success"
    assert result.row_count == 0
    assert result.first_attempt_success is True


def test_empty_explanation_uses_deterministic_fallback(expenses_db):
    model = ScriptedCompleteModel([GOOD_SQL, "   "])

    result = ask_database("八月餐饮花了多少钱", expenses_db, model)

    assert result.status == "success"
    assert "33.0" in result.answer
    assert GOOD_SQL in result.answer


def test_missing_database_returns_failed_result_not_exception(tmp_path):
    model = ScriptedCompleteModel([])
    result = ask_database("随便问", tmp_path / "missing.db", model)

    assert result.status == "failed"
    assert "数据库" in result.answer
    assert result.model_calls == 0


def test_invalid_question_raises_value_error(expenses_db):
    model = ScriptedCompleteModel([])
    with pytest.raises(ValueError):
        ask_database("   ", expenses_db, model)


def test_eval_record_exposes_m5_fields_without_calculating_rates(expenses_db):
    model = ScriptedCompleteModel([BAD_SQL, GOOD_SQL, "餐饮合计 33.0 元。"])
    result = ask_database("八月餐饮花了多少钱", expenses_db, model)
    record = result.to_eval_record(latency_ms=1234.5)

    assert isinstance(record, SqlEvalRecord)
    assert record.question == "八月餐饮花了多少钱"
    assert record.status == "success"
    assert record.attempts == 2
    assert record.first_attempt_success is False
    assert record.fix_success is True
    assert record.total_fix_rounds == 1
    assert record.prompt_tokens == 30
    assert record.completion_tokens == 15
    assert record.total_tokens == 45
    assert record.latency_ms == 1234.5
    # 计算留到 M5：本模块只负责把单题事实记录完整
    assert not hasattr(record, "first_success_rate")
    assert not hasattr(record, "fix_success_rate")
    assert not hasattr(record, "avg_fix_rounds")


def test_attempt_dataclass_records_sql_and_error_for_eval():
    attempt = SqlAttempt(
        index=1,
        sql="SELECT * FROM no_table",
        ok=False,
        error="no such table: no_table",
        row_count=0,
    )
    assert attempt.index == 1
    assert attempt.ok is False
    assert "no_table" in attempt.error


def test_extract_sql_text_handles_fences_json_and_plain():
    assert extract_sql_text(FENCED_GOOD_SQL).strip().upper().startswith("SELECT")
    assert extract_sql_text(GOOD_SQL).strip().upper().startswith("SELECT")
    assert extract_sql_text('{"sql": "SELECT 1"}').strip() == "SELECT 1"
    assert "SELECT" in extract_sql_text("好的，查询如下：\nSELECT 1").upper()


def test_format_result_fallback_contains_numbers_caliber_and_sql():
    answer = format_result_fallback(
        question="餐饮花了多少",
        sql=GOOD_SQL,
        columns=("category", "total"),
        rows=(("餐饮", 33.0),),
        row_count=1,
        truncated=False,
    )

    assert "餐饮花了多少" in answer
    assert "33.0" in answer
    assert GOOD_SQL in answer
    assert "餐饮" in answer
