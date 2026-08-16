# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) data/ 下的演示库必须可以“删掉重来”：数据不是手建一次就丢进黑洞，
#    而是由 build_demo_tables() 确定性生成，测试和复现都调同一份代码。
# 2) 三张表贴近生活（记账流水/学习记录/观影记录），字段中文语义自解释；
#    每张表都锁“几十行”，避免演示一问就 3 行、修正逻辑根本练不到。
# 3) 生成函数幂等：第二次调用重建同库，行数不变，复现步骤可以反复执行。

import sqlite3

from personal_data_assistant.data.demo import (
    DEMO_TABLE_NAMES,
    build_demo_tables,
)


def test_demo_tables_are_created_with_expected_shape(tmp_path):
    db_path = build_demo_tables(tmp_path / "user.db")
    conn = sqlite3.connect(db_path)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert names == set(DEMO_TABLE_NAMES)
        assert DEMO_TABLE_NAMES == ("expenses", "study_logs", "movie_logs")

        for table in DEMO_TABLE_NAMES:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count >= 20, table

        expenses_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(expenses)")
        }
        assert {"id", "日期", "类别", "项目", "金额", "支付方式", "备注"} <= expenses_columns

        study_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(study_logs)")
        }
        assert {"id", "日期", "科目", "主题", "时长分钟", "渠道", "是否完成", "笔记"} <= study_columns

        movie_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(movie_logs)")
        }
        assert {"id", "观看日期", "片名", "类型", "年份", "评分", "时长分钟", "平台", "短评"} <= movie_columns
    finally:
        conn.close()


def test_demo_data_is_chinese_and_life_like(tmp_path):
    db_path = build_demo_tables(tmp_path / "user.db")
    conn = sqlite3.connect(db_path)
    try:
        categories = {
            row[0] for row in conn.execute("SELECT DISTINCT 类别 FROM expenses")
        }
        assert "餐饮" in categories
        assert len(categories) >= 4

        subjects = {
            row[0] for row in conn.execute("SELECT DISTINCT 科目 FROM study_logs")
        }
        assert "Python" in subjects
        assert len(subjects) >= 3

        genres = {
            row[0] for row in conn.execute("SELECT DISTINCT 类型 FROM movie_logs")
        }
        assert "科幻" in genres or "剧情" in genres
        assert len(genres) >= 3
    finally:
        conn.close()


def test_build_demo_tables_is_idempotent_and_recreates(tmp_path):
    db_path = tmp_path / "user.db"
    build_demo_tables(db_path)
    build_demo_tables(db_path)

    conn = sqlite3.connect(db_path)
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in DEMO_TABLE_NAMES
        }
    finally:
        conn.close()

    assert counts == {"expenses": 32, "study_logs": 26, "movie_logs": 24}


def test_demo_queries_that_m3_will_use_are_answerable(tmp_path):
    db_path = build_demo_tables(tmp_path / "user.db")
    conn = sqlite3.connect(db_path)
    try:
        food_total = conn.execute(
            "SELECT SUM(金额) FROM expenses WHERE 类别='餐饮'"
        ).fetchone()[0]
        assert food_total and food_total > 0

        python_minutes = conn.execute(
            "SELECT SUM(时长分钟) FROM study_logs WHERE 科目='Python'"
        ).fetchone()[0]
        assert python_minutes and python_minutes > 0

        top_movie = conn.execute(
            "SELECT 片名, 评分 FROM movie_logs ORDER BY 评分 DESC, 观看日期 DESC LIMIT 1"
        ).fetchone()
        assert top_movie and top_movie[1] > 0
    finally:
        conn.close()
