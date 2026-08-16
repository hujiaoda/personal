# -*- coding: utf-8 -*-
# 设计取舍：
# 1) SQLite 只用标准库 sqlite3，不新增第三方依赖；所有表由 schema_version=1
#    一次性建好，重开库幂等，给后续 M3/M5 的迁移测试留出 PRAGMA user_version 入口。
# 2) 时间统一 ISO-8601 UTC 字符串落库：人可读、前缀可过滤（substr 取日期）、
#    fromisoformat 可还原，不需要单独存 epoch。
# 3) KV 长期记忆检索 = 文本相关性 × 权重 × exp(-lambda × 年龄天数)。
#    相关性用手写 token 覆盖度（ASCII 词 + 中文 2-gram），不调 embedding，
#    因此 M2 完全离线可测；以后换向量检索时 search 的返回形状不变。
# 4) get_memory 是真实“读取”：会回写 access_count 与 last_accessed_at；
#    search 不写库，避免一次检索污染评测可重复性。

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
from datetime import date, datetime, time, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple, Union

from personal_data_assistant.memory.models import (
    KVMemory,
    Message,
    Summary,
    estimate_tokens,
    from_iso,
    to_iso,
    utcnow,
    week_key,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages(session_id, created_at, id);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL CHECK(level IN ('session', 'daily', 'weekly')),
    period_key TEXT NOT NULL,
    session_id TEXT,
    content TEXT NOT NULL,
    source_text TEXT NOT NULL DEFAULT '',
    source_ids TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    estimated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_summaries_session
    ON summaries(level, session_id) WHERE level = 'session';
CREATE UNIQUE INDEX IF NOT EXISTS idx_summaries_period
    ON summaries(level, period_key) WHERE level IN ('daily', 'weekly');
CREATE INDEX IF NOT EXISTS idx_summaries_created
    ON summaries(level, created_at, id);

CREATE TABLE IF NOT EXISTS kv_memories (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    weight REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TEXT
);
"""

_WORD_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def tokenize_text(text: str) -> set:
    """离线检索 token：ASCII 单词 + 中文连续串的单字与 2-gram。"""
    lowered = str(text).lower()
    tokens = set(_WORD_RE.findall(lowered))
    for run in _CJK_RUN_RE.findall(lowered):
        # 单字 + 2-gram 都保留：中文单字有独立语义（如“学”），只留 2-gram 会把
        # “我学了什么”这类短查询漏掉；个人记忆库规模小，召回优先于精确。
        tokens.update(run)
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def text_similarity(query: str, text: str) -> float:
    """查询被目标文本覆盖的比例：命中 query token 数 / query token 总数。"""
    query_tokens = tokenize_text(query)
    if not query_tokens:
        return 0.0
    text_tokens = tokenize_text(text)
    overlap = len(query_tokens & text_tokens)
    return overlap / len(query_tokens)


def time_decay_score(
    similarity: float,
    *,
    age_days: float,
    weight: float = 1.0,
    decay_lambda: float = 0.05,
) -> float:
    """时间衰减分：相关性 × 权重 × exp(-lambda × 年龄天数)。公式公开给测试锁死。"""
    if age_days < 0:
        age_days = 0.0
    if weight < 0:
        raise ValueError("weight 不能为负数")
    if decay_lambda < 0:
        raise ValueError("decay_lambda 不能为负数")
    return similarity * float(weight) * math.exp(-decay_lambda * age_days)


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _synchronized(method: Any) -> Any:
    """给数据库公开方法加同一把 RLock；M4 起连接允许跨线程，靠锁保证串行。"""

    @wraps(method)
    def wrapper(self: "MemoryDatabase", *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _synchronize_methods(*names: str) -> Callable[[Any], Any]:
    def decorate(cls: Any) -> Any:
        for name in names:
            attr = getattr(cls, name)
            if isinstance(attr, property):
                setattr(
                    cls,
                    name,
                    property(
                        _synchronized(attr.fget),
                        attr.fset,
                        attr.fdel,
                        attr.__doc__,
                    ),
                )
            else:
                setattr(cls, name, _synchronized(attr))
        return cls

    return decorate


@_synchronize_methods(
    "schema_version",
    "close",
    "save_message",
    "save_messages",
    "load_messages",
    "upsert_summary",
    "get_summary",
    "list_summaries",
    "put_memory",
    "get_memory",
    "search_memories",
    "list_memories",
    "delete_memory",
)
class MemoryDatabase:
    """M2 记忆库：会话消息、分层摘要、KV 长期记忆全部落 SQLite。"""

    def __init__(
        self,
        db_path: Union[str, os.PathLike],
        *,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        path_text = str(db_path)
        self.path = path_text
        self._now_fn = now_fn or utcnow
        if path_text != ":memory:":
            parent = Path(path_text).expanduser().resolve().parent
            parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + 公开方法锁：FastAPI 线程池会在不同线程复用同一个
        # MemoryManager，SQLite 连接默认绑线程会让 /ask 在第二个请求就崩。
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path_text, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(_SCHEMA)
        current_version = self.schema_version
        if current_version == 0:
            self._conn.execute("PRAGMA user_version = 1")
        self._conn.commit()

    @property
    def schema_version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MemoryDatabase":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ 会话消息

    def save_message(self, message: Message) -> Message:
        saved = self._insert_message(message)
        self._conn.commit()
        return saved

    def save_messages(self, messages: Iterable[Message]) -> List[Message]:
        """批量保存：整批成功才提交，失败回滚，不留下半批脏数据。"""
        message_list = list(messages)
        saved: List[Message] = []
        with self._conn:
            for message in message_list:
                saved.append(self._insert_message(message))
        return saved

    def _insert_message(self, message: Message) -> Message:
        tokens = message.tokens if message.tokens > 0 else estimate_tokens(message.content)
        cursor = self._conn.execute(
            "INSERT INTO messages(session_id, role, content, tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (message.session_id, message.role, message.content, tokens, to_iso(message.created_at)),
        )
        return Message(
            id=int(cursor.lastrowid),
            role=message.role,
            content=message.content,
            session_id=message.session_id,
            created_at=message.created_at,
            tokens=tokens,
        )

    def load_messages(
        self,
        session_id: Optional[str] = None,
        *,
        limit: Optional[int] = None,
    ) -> List[Message]:
        sql = (
            "SELECT id, session_id, role, content, tokens, created_at "
            "FROM messages"
        )
        params: List[Any] = []
        if session_id is not None:
            sql += " WHERE session_id = ?"
            params.append(session_id)
        sql += " ORDER BY created_at ASC, id ASC"
        if limit is not None:
            if limit < 0:
                raise ValueError("limit 不能为负数")
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_message(row) for row in rows]

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        return Message(
            id=int(row["id"]),
            session_id=str(row["session_id"]),
            role=str(row["role"]),
            content=str(row["content"]),
            tokens=int(row["tokens"]),
            created_at=from_iso(str(row["created_at"])),
        )

    # ------------------------------------------------------------------ 分层摘要

    def upsert_summary(self, summary: Summary) -> Summary:
        existing = self._find_summary_row(summary)
        if existing is None:
            cursor = self._conn.execute(
                "INSERT INTO summaries("
                "level, period_key, session_id, content, source_text, source_ids, "
                "model, prompt_tokens, completion_tokens, total_tokens, estimated, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._summary_params(summary),
            )
            self._conn.commit()
            return self._summary_with_id(summary, int(cursor.lastrowid))

        existing_id = int(existing["id"])
        self._conn.execute(
            "UPDATE summaries SET period_key = ?, session_id = ?, content = ?, "
            "source_text = ?, source_ids = ?, model = ?, prompt_tokens = ?, "
            "completion_tokens = ?, total_tokens = ?, estimated = ?, created_at = ? "
            "WHERE id = ?",
            (*self._summary_params(summary)[1:], existing_id),
        )
        self._conn.commit()
        return self._summary_with_id(summary, existing_id)

    def get_summary(
        self,
        level: str,
        *,
        session_id: Optional[str] = None,
        period_key: Optional[str] = None,
    ) -> Optional[Summary]:
        row = self._find_summary_row(
            Summary(level=level, period_key=period_key or "-", content="probe", session_id=session_id)
        )
        return self._row_to_summary(row) if row is not None else None

    def list_summaries(
        self,
        level: Optional[str] = None,
        *,
        day: Optional[str] = None,
        week: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Summary]:
        sql = "SELECT * FROM summaries WHERE 1 = 1"
        params: List[Any] = []
        if level is not None:
            sql += " AND level = ?"
            params.append(level)
        sql += " ORDER BY created_at DESC, id DESC"
        rows = self._conn.execute(sql, params).fetchall()
        summaries = [self._row_to_summary(row) for row in rows]
        if day is not None:
            summaries = [s for s in summaries if s.period_key == day]
        if week is not None:
            summaries = [s for s in summaries if self._period_in_week(s.period_key, week)]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit 不能为负数")
            summaries = summaries[: int(limit)]
        return summaries

    @staticmethod
    def _period_in_week(period_key: str, target_week: str) -> bool:
        if period_key == target_week:
            return True
        try:
            value = date.fromisoformat(period_key)
        except ValueError:
            return False
        return week_key(datetime.combine(value, time.min, tzinfo=timezone.utc)) == target_week

    @staticmethod
    def _summary_params(summary: Summary) -> Tuple[Any, ...]:
        return (
            summary.level,
            summary.period_key,
            summary.session_id,
            summary.content,
            summary.source_text,
            json.dumps(list(summary.source_ids), ensure_ascii=False),
            summary.model,
            max(0, int(summary.prompt_tokens)),
            max(0, int(summary.completion_tokens)),
            max(0, int(summary.total_tokens)),
            1 if summary.estimated else 0,
            to_iso(summary.created_at),
        )

    def _find_summary_row(self, summary: Summary) -> Optional[sqlite3.Row]:
        if summary.level == "session":
            if not summary.session_id:
                return None
            return self._conn.execute(
                "SELECT * FROM summaries WHERE level = ? AND session_id = ?",
                (summary.level, summary.session_id),
            ).fetchone()
        return self._conn.execute(
            "SELECT * FROM summaries WHERE level = ? AND period_key = ?",
            (summary.level, summary.period_key),
        ).fetchone()

    @staticmethod
    def _summary_with_id(summary: Summary, summary_id: int) -> Summary:
        return Summary(
            id=summary_id,
            level=summary.level,
            period_key=summary.period_key,
            content=summary.content,
            session_id=summary.session_id,
            source_ids=summary.source_ids,
            source_text=summary.source_text,
            model=summary.model,
            prompt_tokens=summary.prompt_tokens,
            completion_tokens=summary.completion_tokens,
            total_tokens=summary.total_tokens,
            estimated=summary.estimated,
            created_at=summary.created_at,
        )

    @staticmethod
    def _row_to_summary(row: sqlite3.Row) -> Summary:
        return Summary(
            id=int(row["id"]),
            level=str(row["level"]),
            period_key=str(row["period_key"]),
            content=str(row["content"]),
            session_id=None if row["session_id"] is None else str(row["session_id"]),
            source_ids=tuple(int(value) for value in json.loads(str(row["source_ids"] or "[]"))),
            source_text=str(row["source_text"] or ""),
            model=str(row["model"] or ""),
            prompt_tokens=int(row["prompt_tokens"]),
            completion_tokens=int(row["completion_tokens"]),
            total_tokens=int(row["total_tokens"]),
            estimated=bool(row["estimated"]),
            created_at=from_iso(str(row["created_at"])),
        )

    # ------------------------------------------------------------------ KV 长期记忆

    def put_memory(
        self,
        key: str,
        value: str,
        *,
        category: str = "",
        weight: float = 1.0,
        now: Optional[datetime] = None,
    ) -> KVMemory:
        stamp = to_iso(_as_aware_utc(now or self._now_fn()))
        # 先用 KVMemory 做校验再写库，避免空 key/负 weight 写入后读取时才炸。
        probe = KVMemory(key=key, value=value, category=category, weight=weight)
        existing = self._conn.execute(
            "SELECT created_at FROM kv_memories WHERE key = ?", (probe.key,)
        ).fetchone()
        created_at = str(existing["created_at"]) if existing is not None else stamp
        self._conn.execute(
            "INSERT INTO kv_memories(key, value, category, weight, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, category = excluded.category, "
            "weight = excluded.weight, updated_at = excluded.updated_at",
            (probe.key, probe.value, probe.category, probe.weight, created_at, stamp),
        )
        self._conn.commit()
        record = self._fetch_kv(key, score=None)
        assert record is not None  # 刚写入/更新成功，理论必命中
        return record

    def get_memory(self, key: str, *, now: Optional[datetime] = None) -> Optional[KVMemory]:
        row = self._conn.execute("SELECT key FROM kv_memories WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        stamp = to_iso(_as_aware_utc(now or self._now_fn()))
        self._conn.execute(
            "UPDATE kv_memories SET access_count = access_count + 1, last_accessed_at = ? "
            "WHERE key = ?",
            (stamp, key),
        )
        self._conn.commit()
        return self._fetch_kv(key, score=None)

    def search_memories(
        self,
        query: str,
        top_k: int = 8,
        *,
        now: Optional[datetime] = None,
        decay_lambda: float = 0.05,
    ) -> List[KVMemory]:
        if top_k < 1:
            raise ValueError(f"top_k 必须 >= 1，当前: {top_k}")
        if decay_lambda < 0:
            raise ValueError("decay_lambda 不能为负数")
        current = _as_aware_utc(now or self._now_fn())
        rows = self._conn.execute("SELECT * FROM kv_memories").fetchall()
        scored: List[KVMemory] = []
        for row in rows:
            record = self._row_to_kv(row)
            similarity = max(
                text_similarity(query, record.key),
                text_similarity(query, record.value),
            )
            if similarity <= 0:
                continue
            age_days = max(0.0, (current - record.updated_at).total_seconds() / 86400)
            score = time_decay_score(
                similarity,
                age_days=age_days,
                weight=record.weight,
                decay_lambda=decay_lambda,
            )
            scored.append(self._kv_with_score(record, score))
        scored.sort(key=lambda item: (item.score or 0.0, to_iso(item.updated_at)), reverse=True)
        return scored[:top_k]

    def list_memories(self) -> List[KVMemory]:
        rows = self._conn.execute("SELECT * FROM kv_memories ORDER BY updated_at DESC, key ASC").fetchall()
        return [self._row_to_kv(row) for row in rows]

    def delete_memory(self, key: str) -> bool:
        cursor = self._conn.execute("DELETE FROM kv_memories WHERE key = ?", (key,))
        self._conn.commit()
        return cursor.rowcount > 0

    def _fetch_kv(self, key: str, *, score: Optional[float]) -> Optional[KVMemory]:
        row = self._conn.execute(
            "SELECT * FROM kv_memories WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_kv(row) if score is None else self._kv_with_score(self._row_to_kv(row), score)

    @staticmethod
    def _row_to_kv(row: sqlite3.Row) -> KVMemory:
        last_accessed = row["last_accessed_at"]
        return KVMemory(
            key=str(row["key"]),
            value=str(row["value"]),
            category=str(row["category"] or ""),
            weight=float(row["weight"]),
            created_at=from_iso(str(row["created_at"])),
            updated_at=from_iso(str(row["updated_at"])),
            access_count=int(row["access_count"]),
            last_accessed_at=from_iso(str(last_accessed)) if last_accessed else None,
        )

    @staticmethod
    def _kv_with_score(record: KVMemory, score: float) -> KVMemory:
        return KVMemory(
            key=record.key,
            value=record.value,
            category=record.category,
            weight=record.weight,
            created_at=record.created_at,
            updated_at=record.updated_at,
            access_count=record.access_count,
            last_accessed_at=record.last_accessed_at,
            score=score,
        )


__all__ = [
    "KVMemory",
    "MemoryDatabase",
    "Message",
    "Summary",
    "text_similarity",
    "time_decay_score",
    "tokenize_text",
]
