# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) M5 评测体系的测试先锁“题集契约”再锁“指标算法”：题集数量、参考答案要点、
#    陷阱类型与重复 id 在跑评测前就失败，避免坏数据生成坏报告。
# 2) 离线可复现是硬约束：记忆与 SQL 两个 runner 都只用 fake 模型，同一批输入
#    跑两次必须得到完全相同的指标；真实 DeepSeek 路径不作为默认测试依赖。
# 3) 指标口径直接写死在测试里：记忆命中率按“题目所有参考要点都被答案覆盖”计，
#    SQL 首轮成功率按“首次执行即成功且无需修正”计，修正成功率的分母是“需要
#    修正的题数”。这样报告、脚本与测试三方不会各说各话。

from pathlib import Path

import pytest

from evals.memory_eval import (
    evaluate_answer,
    load_memory_dataset,
    run_memory_eval,
)
from evals.reporting import build_final_report, rows_equal, render_markdown_table
from evals.sql_eval import load_sql_dataset, run_sql_eval

ROOT = Path(__file__).resolve().parents[1]
MEMORY_QUESTIONS = ROOT / "evals" / "questions" / "memory_50.json"
SQL_QUESTIONS = ROOT / "evals" / "questions" / "sql_questions.json"


# --------------------------------------------------------------------------- 题集契约

def test_memory_dataset_has_at_least_50_answerable_questions_with_key_points():
    dataset = load_memory_dataset(MEMORY_QUESTIONS)

    assert len(dataset["questions"]) >= 50
    assert len(dataset["corpus"]) >= 40
    assert len(dataset["kv_memories"]) >= 5

    ids = [item["id"] for item in dataset["questions"]]
    assert len(ids) == len(set(ids))

    allowed_sources = {"window", "summary", "kv"}
    for item in dataset["questions"]:
        assert item["id"].strip()
        assert item["question"].strip()
        assert item["category"] in {"recent", "summary_only", "kv_only", "cross"}
        assert item["reference_points"], item["id"]
        for point in item["reference_points"]:
            assert point["point"].strip()
            assert point["evidence"], item["id"]
            assert set(point["evidence"]) <= allowed_sources, item["id"]
        assert set(item["expected_strategies"]) <= {
            "window", "window_summary", "full"
        }, item["id"]

    for message in dataset["corpus"]:
        assert message["role"] == "user"
        assert message["content"].strip()
        assert message["session_id"].strip()
        assert message["created_at"].strip()


def test_sql_dataset_has_20_questions_and_5_trap_types():
    dataset = load_sql_dataset(SQL_QUESTIONS)

    assert len(dataset["questions"]) >= 20
    ids = [item["id"] for item in dataset["questions"]]
    assert len(ids) == len(set(ids))

    trap_types = [item.get("trap_type") for item in dataset["questions"] if item.get("trap_type")]
    assert len(trap_types) >= 5
    for required in {"non_readonly", "missing_table", "time_range_out_of_bounds"}:
        assert required in trap_types

    for item in dataset["questions"]:
        assert item["id"].strip()
        assert item["question"].strip()
        assert item["first_sql"].strip()
        assert isinstance(item["fix_sqls"], list)
        assert "expected_rows" in item
        assert "expected_status" in item


# --------------------------------------------------------------------------- 记忆评测

def _expected_memory_hits(dataset):
    counts = {"none": 0, "window": 0, "window_summary": 0, "full": 0}
    for item in dataset["questions"]:
        for strategy in item["expected_strategies"]:
            counts[strategy] += 1
    return counts


def test_memory_eval_matches_dataset_expectation_and_is_deterministic(tmp_path):
    dataset = load_memory_dataset(MEMORY_QUESTIONS)
    expected = _expected_memory_hits(dataset)

    first = run_memory_eval(dataset_path=None, db_dir=tmp_path / "run1")  # None 走默认题集，防 CLI 默认路径回归
    second = run_memory_eval(dataset_path=MEMORY_QUESTIONS, db_dir=tmp_path / "run2")

    assert first["questions_total"] == second["questions_total"] == len(dataset["questions"])
    assert first["questions_total"] >= 50

    for strategy in ("none", "window", "window_summary", "full"):
        actual = first["strategies"][strategy]
        assert actual["hits"] == expected[strategy]
        assert actual["hits"] == second["strategies"][strategy]["hits"]
        assert 0.0 <= actual["hit_rate"] <= 1.0
        assert actual["prompt_tokens"] >= 0
        assert actual["completion_tokens"] > 0
        assert actual["context_tokens"] >= 0
        assert actual["estimated_cost"] >= 0

    assert expected["none"] == 0
    assert expected["window"] > 0
    assert expected["window_summary"] > expected["window"]
    assert expected["full"] > expected["window_summary"]


def test_memory_answer_scoring_covers_all_key_points():
    question = {
        "reference_points": [
            {"point": "学了 Python 装饰器", "evidence": ["window"]},
            {"point": "花了 45 分钟", "evidence": ["window"]},
        ]
    }

    result = evaluate_answer("今天学了 Python 装饰器，花了 45 分钟。", question)
    assert result["matched"] == 2
    assert result["total"] == 2
    assert result["hit"] is True
    assert result["coverage"] == 1.0

    result = evaluate_answer("今天学了 Python 装饰器。", question)
    assert result["matched"] == 1
    assert result["hit"] is False
    assert result["coverage"] == 0.5


# --------------------------------------------------------------------------- SQL 评测

def _expected_sql_metrics(dataset):
    total = len(dataset["questions"])
    fix_needed = [item for item in dataset["questions"] if item["fix_sqls"]]
    first_success = total - len(fix_needed)
    fix_rounds = sum(len(item["fix_sqls"]) for item in fix_needed)
    return {
        "questions_total": total,
        "first_success": first_success,
        "fix_denominator": len(fix_needed),
        "fix_rounds": fix_rounds,
    }


def test_sql_eval_metrics_match_dataset_and_are_deterministic(tmp_path):
    dataset = load_sql_dataset(SQL_QUESTIONS)
    expected = _expected_sql_metrics(dataset)

    first = run_sql_eval(dataset_path=None, work_dir=tmp_path / "run1")  # None 走默认题集，防 CLI 默认路径回归
    second = run_sql_eval(dataset_path=SQL_QUESTIONS, work_dir=tmp_path / "run2")

    assert first["questions_total"] == expected["questions_total"] >= 20
    assert first["first_success"] == expected["first_success"]
    assert first["first_success_rate"] == pytest.approx(
        expected["first_success"] / expected["questions_total"]
    )
    assert first["fix_denominator"] == expected["fix_denominator"] >= 5
    assert first["fix_success"] == first["fix_denominator"]
    assert first["fix_success_rate"] == 1.0
    assert first["avg_fix_rounds"] == pytest.approx(
        expected["fix_rounds"] / expected["questions_total"]
    )
    assert first["prompt_tokens"] > 0
    assert first["completion_tokens"] > 0
    assert first["estimated_cost"] >= 0

    for key in (
        "questions_total",
        "first_success",
        "fix_success",
        "avg_fix_rounds",
        "prompt_tokens",
        "completion_tokens",
    ):
        assert first[key] == second[key], key

    # 非只读陷阱必须保证：数据一条没少、一条没改。
    non_readonly = [
        item for item in dataset["questions"] if item.get("trap_type") == "non_readonly"
    ]
    assert non_readonly
    trap_record = next(
        record for record in first["records"]
        if record["question_id"] == non_readonly[0]["id"]
    )
    assert trap_record["db_unchanged"] is True


# --------------------------------------------------------------------------- 判分与报告

def test_rows_equal_sorts_rows_and_uses_float_tolerance():
    assert rows_equal([], [], tolerance=1e-6) is True
    assert rows_equal([("餐饮", 33.0)], [("餐饮", 33.0)], tolerance=1e-6) is True
    assert rows_equal([("餐饮", 33.0)], [("餐饮", 33.0 + 1e-7)], tolerance=1e-6) is True
    assert rows_equal([("餐饮", 33.0)], [("餐饮", 33.01)], tolerance=1e-6) is False
    assert rows_equal([(1, "a"), (2, "b")], [("b", 2), ("a", 1)], tolerance=1e-6) is False


def test_markdown_table_rendering_and_final_report(tmp_path):
    table = render_markdown_table(["策略", "命中"], [["none", "0"], ["full", "56"]])
    assert "| 策略 | 命中 |" in table
    assert "| none | 0 |" in table
    assert "| full | 56 |" in table

    memory_results = run_memory_eval(dataset_path=MEMORY_QUESTIONS, db_dir=tmp_path / "m")
    sql_results = run_sql_eval(dataset_path=SQL_QUESTIONS, work_dir=tmp_path / "s")
    report_path = tmp_path / "M5-eval.md"
    build_final_report(memory_results, sql_results, report_path)

    text = report_path.read_text(encoding="utf-8")
    assert "# M5 评测报告" in text
    assert "记忆评测" in text
    assert "SQL 评测" in text
    assert "|" in text
    assert "结论" in text
    assert "遗留问题" in text or "局限" in text
