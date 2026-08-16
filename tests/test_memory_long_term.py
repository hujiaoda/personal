# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) 长期记忆落 SQLite 只依赖标准库 sqlite3，不新增第三方依赖；测试全部用 tmp_path
#    临时库，证明模块可独立测试、可重放。
# 2) 时间衰减公式要锁死：相关性 × 权重 × exp(-lambda × 年龄天数)。用例固定 now，
#    因此旧记忆、新记忆的排序是确定性的。
# 3) 读取 get 要增加访问计数与最近访问时间，证明“写入/读取”都真实落库。

import math
from datetime import datetime, timezone

import pytest

from personal_data_assistant.memory.long_term import MemoryDatabase
from personal_data_assistant.memory.models import Summary
from personal_data_assistant.memory.window import Message

DAY = datetime(2025, 8, 16, 12, 0, tzinfo=timezone.utc)
OLD = datetime(2025, 1, 1, tzinfo=timezone.utc)
RECENT = datetime(2025, 8, 1, tzinfo=timezone.utc)


def make_message(content: str, session_id: str = "s1") -> Message:
    return Message(role="user", content=content, session_id=session_id, created_at=DAY)


def test_schema_is_created_and_reopen_is_idempotent(tmp_path):
    path = tmp_path / "memory.db"
    db = MemoryDatabase(path)
    assert db.schema_version == 1
    db.close()

    reopened = MemoryDatabase(path)
    assert reopened.schema_version == 1
    assert reopened.list_summaries() == []
    reopened.close()


def test_save_and_load_messages_roundtrip_in_order(tmp_path):
    db = MemoryDatabase(tmp_path / "memory.db")
    first = db.save_message(make_message("第一条"))
    second = db.save_message(make_message("第二条"))

    loaded = db.load_messages(session_id="s1")

    assert [m.id for m in loaded] == [first.id, second.id]
    assert [m.content for m in loaded] == ["第一条", "第二条"]
    assert loaded[0].role == "user"
    assert loaded[0].session_id == "s1"
    assert loaded[0].tokens == loaded[0].tokens
    assert db.load_messages(session_id="missing") == []
    assert [m.content for m in db.load_messages(limit=1)] == ["第一条"]
    db.close()


def test_put_get_and_update_kv_memory(tmp_path):
    db = MemoryDatabase(tmp_path / "memory.db")
    db.put_memory("python", "Python 装饰器笔记", category="学习", weight=2.0, now=DAY)

    first = db.get_memory("python", now=DAY)
    assert first is not None
    assert first.key == "python"
    assert first.value == "Python 装饰器笔记"
    assert first.category == "学习"
    assert first.weight == 2.0
    assert first.created_at == DAY
    assert first.updated_at == DAY
    assert first.access_count == 1
    assert first.last_accessed_at == DAY

    db.put_memory("python", "Python 装饰器复习", now=RECENT)
    updated = db.get_memory("python", now=RECENT)
    assert updated is not None
    assert updated.value == "Python 装饰器复习"
    assert updated.created_at == DAY  # upsert 不改首次写入时间
    assert updated.updated_at == RECENT
    assert updated.access_count == 2

    assert db.get_memory("missing") is None
    db.close()


def test_search_decays_older_memories_with_exponential_formula(tmp_path):
    db = MemoryDatabase(tmp_path / "memory.db")
    db.put_memory("old_python", "Python 学习笔记", now=OLD)
    db.put_memory("new_python", "Python 学习笔记", now=RECENT)

    results = db.search_memories("Python 学习", top_k=5, now=DAY, decay_lambda=0.05)

    assert [r.key for r in results] == ["new_python", "old_python"]
    assert results[0].score is not None and results[1].score is not None
    assert results[0].score > results[1].score
    # 两条文本相关性相同、权重相同：分数比应等于 exp(-lambda * 年龄差)。
    age_new_days = (DAY - RECENT).total_seconds() / 86400
    age_old_days = (DAY - OLD).total_seconds() / 86400
    expected_ratio = math.exp(-0.05 * (age_new_days - age_old_days))
    assert results[0].score / results[1].score == pytest.approx(expected_ratio, rel=1e-6)
    db.close()


def test_search_top_k_and_zero_relevance_behavior(tmp_path):
    db = MemoryDatabase(tmp_path / "memory.db")
    db.put_memory("python", "Python 学习", now=RECENT)
    db.put_memory("sql", "SQLite 学习", now=RECENT)
    db.put_memory("cooking", "做饭记录", now=RECENT)

    results = db.search_memories("Python", top_k=2, now=DAY)
    assert [r.key for r in results] == ["python"]
    assert all(r.score is not None and r.score > 0 for r in results)
    assert db.search_memories("完全不相关", now=DAY) == []
    db.close()


def test_upsert_summary_is_unique_by_hierarchy_key(tmp_path):
    db = MemoryDatabase(tmp_path / "memory.db")
    summary = Summary(
        level="session",
        period_key="2025-08-16",
        content="会话摘要 v1",
        session_id="s1",
        source_ids=(1, 2),
    )

    first = db.upsert_summary(summary)
    second = db.upsert_summary(
        Summary(
            level="session",
            period_key="2025-08-16",
            content="会话摘要 v2",
            session_id="s1",
            source_ids=(3,),
        )
    )

    assert first.id == second.id
    sessions = db.list_summaries(level="session")
    assert len(sessions) == 1
    assert sessions[0].content == "会话摘要 v2"
    assert sessions[0].source_ids == (3,)

    daily = db.upsert_summary(Summary(level="daily", period_key="2025-08-16", content="日摘要"))
    weekly = db.upsert_summary(Summary(level="weekly", period_key="2025-W33", content="周摘要"))
    assert daily.id != weekly.id
    assert db.get_summary("session", session_id="s1") is not None
    assert db.get_summary("daily", period_key="2025-08-16") is not None
    db.close()


def test_list_summaries_filters_by_level_and_week(tmp_path):
    db = MemoryDatabase(tmp_path / "memory.db")
    db.upsert_summary(Summary(level="daily", period_key="2025-08-10", content="上周日"))
    db.upsert_summary(Summary(level="daily", period_key="2025-08-16", content="本周六"))

    days = db.list_summaries(level="daily", week="2025-W33")

    assert [s.period_key for s in days] == ["2025-08-16"]
    db.close()
