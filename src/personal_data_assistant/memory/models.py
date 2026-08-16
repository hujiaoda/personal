# -*- coding: utf-8 -*-
# 设计取舍：
# 1) memory 包内四个模块都要用同一套数据形状（消息/摘要/KV 记忆/上下文项），
#    集中放在 models.py，避免 window→long_term→retriever 互相 import 成环。
# 2) 时间统一存带时区的 UTC datetime；落库时转 ISO-8601 字符串，SQLite 里
#    字符串前缀比较即可当时间比较，简单且可读。
# 3) MemoryStrategy 用 str Enum：既能在配置文件里写 "full"，也能直接比较；
#    M5 评测按这个枚举逐个重放同一批对话。

from __future__ import annotations

from dataclasses import dataclass, field
import math
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import List, Optional, Sequence, Tuple, Union


class MemoryStrategy(str, Enum):
    """记忆策略。M5 用同一批对话逐策略重放，比较命中率与成本。"""

    NONE = "none"
    WINDOW = "window"
    WINDOW_SUMMARY = "window_summary"
    FULL = "full"


ALL_MEMORY_STRATEGIES: Tuple[MemoryStrategy, ...] = tuple(MemoryStrategy)
SUMMARY_LEVELS: Tuple[str, ...] = ("session", "daily", "weekly")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def estimate_tokens(text: str) -> int:
    """无服务端 usage 时的 UTF-8 粗估；窗口与落库共用同一公式。"""
    if not text:
        return 0
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))



def to_iso(value: datetime) -> str:
    """统一落库格式：UTC + 微秒；排序和前缀过滤都稳定。naive 时间按 UTC 处理。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def from_iso(value: str) -> datetime:
    """读回落库的 ISO 时间；兼容裸 Z 后缀。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def day_key(value: datetime) -> str:
    """日级摘要的 period_key：YYYY-MM-DD。naive 时间按 UTC 处理。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date().isoformat()


def week_key(value: datetime) -> str:
    """周级摘要的 period_key：ISO 年-周，如 2025-W33。naive 时间按 UTC 处理。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    year, week, _ = value.astimezone(timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(period_key: str) -> Tuple[str, str]:
    """返回某 ISO 周（如 2025-W33）的 [起始, 结束) UTC ISO 字符串。"""
    try:
        year_text, week_text = period_key.split("-W", 1)
        year = int(year_text)
        week = int(week_text)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"week period_key 不合法: {period_key!r}") from exc
    monday = datetime.combine(date.fromisocalendar(year, week, 1), time.min, tzinfo=timezone.utc)
    next_monday = monday + timedelta(days=7)
    return to_iso(monday), to_iso(next_monday)


@dataclass(frozen=True)
class Message:
    """进入滑动窗口的一条会话消息；id 由持久化层回填。"""

    role: str
    content: str
    session_id: str = "default"
    created_at: datetime = field(default_factory=utcnow)
    tokens: int = 0
    id: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", self.role.strip())
        if not self.role:
            raise ValueError("消息 role 不能为空")
        if not isinstance(self.content, str):
            raise ValueError("消息 content 必须是字符串")
        object.__setattr__(self, "tokens", max(0, int(self.tokens or 0)))


@dataclass(frozen=True)
class Summary:
    """分层摘要：session（会话级）→ daily（日级）→ weekly（周级）。"""

    level: str
    period_key: str
    content: str
    session_id: Optional[str] = None
    source_ids: Tuple[int, ...] = ()
    source_text: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False
    created_at: datetime = field(default_factory=utcnow)
    id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.level not in SUMMARY_LEVELS:
            raise ValueError(f"summary level 不合法: {self.level!r}（只能 {SUMMARY_LEVELS}）")
        if not isinstance(self.period_key, str) or not self.period_key.strip():
            raise ValueError("summary period_key 不能为空")
        if not isinstance(self.content, str):
            raise ValueError("summary content 必须是字符串")
        object.__setattr__(self, "source_ids", tuple(self.source_ids))


@dataclass(frozen=True)
class KVMemory:
    """key-value 长期记忆；search 命中后 score 是时间衰减分。"""

    key: str
    value: str
    category: str = ""
    weight: float = 1.0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    access_count: int = 0
    last_accessed_at: Optional[datetime] = None
    score: Optional[float] = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("KV 记忆 key 不能为空")
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("KV 记忆 value 不能为空")
        weight = float(self.weight)
        if weight < 0:
            raise ValueError("KV 记忆 weight 不能为负数")
        object.__setattr__(self, "weight", weight)
        object.__setattr__(self, "access_count", max(0, int(self.access_count)))


@dataclass(frozen=True)
class MemoryContextItem:
    """检索上下文中的一条证据，带来源与可选分数，M5 可逐条统计召回。"""

    source: str
    text: str
    score: Optional[float] = None
    timestamp: Optional[datetime] = None
    key: Optional[str] = None


@dataclass
class MemoryContext:
    """一次记忆检索的完整上下文。text 可直接拼进给核心循环的用户问题。"""

    strategy: str
    items: List[MemoryContextItem] = field(default_factory=list)
    text: str = ""

    def __len__(self) -> int:
        return len(self.items)


__all__ = [
    "ALL_MEMORY_STRATEGIES",
    "KVMemory",
    "MemoryContext",
    "MemoryContextItem",
    "MemoryStrategy",
    "Message",
    "SUMMARY_LEVELS",
    "Summary",
    "day_key",
    "estimate_tokens",
    "from_iso",
    "to_iso",
    "utcnow",
    "week_bounds",
    "week_key",
]
