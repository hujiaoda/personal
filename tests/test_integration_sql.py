# -*- coding: utf-8 -*-
# 真实 DeepSeek 智能问数冒烟：整文件 integration，默认被 addopts 排除；
# 显式 -m integration 且配置了 DEEPSEEK_API_KEY 才跑。
# 只验证“问一句演示库的问题能走通全流程且没写库”，不锁具体文案与数字。

import os
import sqlite3

import pytest

from personal_data_assistant.config import load_settings
from personal_data_assistant.data.ask import ask_database
from personal_data_assistant.data.demo import build_demo_tables
from personal_data_assistant.llm.client import DeepSeekClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DEEPSEEK_API_KEY"),
        reason="未配置 DEEPSEEK_API_KEY，跳过真实 API 智能问数冒烟测试",
    ),
]


@pytest.fixture(scope="module")
def client():
    settings = load_settings()
    return DeepSeekClient(settings)


def test_real_ask_database_smoke(client, tmp_path):
    db_path = build_demo_tables(tmp_path / "user.db")
    before = {
        table: sqlite3.connect(db_path).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("expenses", "study_logs", "movie_logs")
    }

    result = ask_database("2025 年 8 月餐饮一共花了多少钱", db_path, client)

    assert result.status in {"success", "failed"}
    assert result.answer
    if result.status == "success":
        assert result.row_count >= 0
        assert "SELECT" in result.sql.upper()
    assert result.usage.total_tokens >= 0

    conn = sqlite3.connect(db_path)
    try:
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
    finally:
        conn.close()
    assert before == after
