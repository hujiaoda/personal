# -*- coding: utf-8 -*-
# 设计取舍：
# 1) MemoryManager 是记忆系统的编排入口，也是 core.loop 的外层组件：
#    ingest 负责窗口淘汰 → 会话摘要 → 日/周 rollup；retrieve 按策略拼上下文；
#    core.loop 的 complete/stream_chat 鸭子协议零改动。
# 2) 策略是显式枚举而不是 if 堆在检索函数里：none/window/window_summary/full
#    共享同一条 ingest 路径，只在检索通道上分叉——这正是 M5 能对同一批对话
#    重放对比的原因。
# 3) 摘要模型失败或未配置时降级为“暂存截断原文”，消息不会因 LLM 不可用而丢失；
#    长期记忆检索失败/无命中只影响 full 通道，不阻断窗口与摘要通道。
# 4) replay_memory_strategies 为每个策略建独立 SQLite 库，重放同一批消息，
#    保证 M5 的 S0~S2 对比不互相污染。

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from personal_data_assistant.memory.long_term import MemoryDatabase
from personal_data_assistant.memory.models import (
    ALL_MEMORY_STRATEGIES,
    KVMemory,
    MemoryContext,
    MemoryContextItem,
    MemoryStrategy,
    Message,
    Summary,
    day_key,
    utcnow,
    week_key,
)
from personal_data_assistant.memory.summarizer import (
    LLMSummarizer,
    format_messages_for_summary,
)
from personal_data_assistant.memory.window import SlidingWindow

_SUMMARY_ENABLED_STRATEGIES = frozenset({MemoryStrategy.WINDOW_SUMMARY, MemoryStrategy.FULL})
_FALLBACK_SUMMARY_PREFIX = "（摘要模型不可用，暂存原文）"


class MemoryManager:
    """M2 记忆系统编排器。同一批消息可按 strategy 重放并得到不同上下文。"""

    def __init__(
        self,
        strategy: str = MemoryStrategy.FULL,
        db_path: Union[str, os.PathLike] = "data/pda.db",
        *,
        model: Any = None,
        summarizer: Optional[LLMSummarizer] = None,
        window: Optional[SlidingWindow] = None,
        max_window_messages: int = 20,
        max_window_tokens: int = 8000,
        long_term_top_k: int = 8,
        decay_lambda: float = 0.05,
        summary_fallback_chars: int = 2000,
        summary_session_limit: int = 5,
        summary_daily_limit: int = 5,
        summary_weekly_limit: int = 3,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        try:
            self.strategy = MemoryStrategy(strategy)
        except ValueError as exc:
            valid = "、".join(item.value for item in ALL_MEMORY_STRATEGIES)
            raise ValueError(f"memory strategy 不合法: {strategy!r}（只能 {valid}）") from exc
        if summary_fallback_chars < 1:
            raise ValueError("summary_fallback_chars 必须 >= 1")
        if summary_session_limit < 0 or summary_daily_limit < 0 or summary_weekly_limit < 0:
            raise ValueError("summary session/daily/weekly limit 不能为负数")

        self._now_fn = now_fn or utcnow
        self.db = MemoryDatabase(db_path, now_fn=self._now_fn)
        self._summarized_message_ids: Dict[str, set] = {}
        self.window = window or SlidingWindow(max_window_messages, max_window_tokens)
        self.long_term_top_k = long_term_top_k
        self.decay_lambda = decay_lambda
        self._summary_fallback_chars = summary_fallback_chars
        self._summary_session_limit = summary_session_limit
        self._summary_daily_limit = summary_daily_limit
        self._summary_weekly_limit = summary_weekly_limit

        if summarizer is not None:
            self._summarizer: Optional[LLMSummarizer] = summarizer
        elif model is not None:
            self._summarizer = LLMSummarizer(model, now_fn=self._now_fn)
        else:
            self._summarizer = None

    @classmethod
    def from_settings(cls, settings: Any, model: Any = None, **kwargs: Any) -> "MemoryManager":
        """从 config.Settings 装配；M5 评测直接覆盖 Settings 即可换策略。"""
        values = dict(kwargs)
        strategy = values.pop("strategy", getattr(settings, "memory_strategy", "full"))
        db_path = values.pop("db_path", getattr(settings, "memory_db_path", "data/pda.db"))
        values.setdefault("max_window_messages", getattr(settings, "memory_window_size", 20))
        values.setdefault("max_window_tokens", getattr(settings, "memory_window_tokens", 8000))
        values.setdefault("long_term_top_k", getattr(settings, "memory_top_k", 8))
        values.setdefault("decay_lambda", getattr(settings, "memory_decay_lambda", 0.05))
        return cls(strategy=strategy, db_path=db_path, model=model, **values)

    @property
    def summaries_enabled(self) -> bool:
        return self.strategy in _SUMMARY_ENABLED_STRATEGIES

    # ------------------------------------------------------------------ 入库

    def ingest(self, message: Message) -> List[Summary]:
        """保存一条消息并进窗口；被淘汰消息立即交给会话摘要层，绝不直接丢弃。"""
        saved = self.db.save_message(message)
        evicted = self.window.add(saved)
        created: List[Summary] = []
        if self.summaries_enabled and evicted:
            fresh = self._fresh_for_summary(message.session_id, evicted)
            if fresh:
                summary = self._update_session_summary(message.session_id, fresh)
                if summary is not None:
                    created.append(summary)
                self._mark_summarized(message.session_id, fresh)
        return created

    def ingest_messages(
        self,
        messages: Iterable[Message],
        *,
        finalize: bool = True,
    ) -> List[Summary]:
        """按给定顺序喂入一批消息（顺序影响窗口与摘要，评测必须保序）。"""
        message_list = list(messages)
        created: List[Summary] = []
        for message in message_list:
            created.extend(self.ingest(message))
        if finalize and self.summaries_enabled:
            session_ids: List[str] = []
            for message in message_list:
                if message.session_id not in session_ids:
                    session_ids.append(message.session_id)
            for session_id in session_ids:
                summary = self.end_session(session_id)
                if summary is not None:
                    created.append(summary)
            day_keys: List[str] = []
            week_keys: List[str] = []
            for message in message_list:
                dk = day_key(message.created_at)
                wk = week_key(message.created_at)
                if dk not in day_keys:
                    day_keys.append(dk)
                if wk not in week_keys:
                    week_keys.append(wk)
            for dk in day_keys:
                summary = self.rollup_daily(dk)
                if summary is not None:
                    created.append(summary)
            for wk in week_keys:
                summary = self.rollup_weekly(wk)
                if summary is not None:
                    created.append(summary)
        return created

    def end_session(self, session_id: str) -> Optional[Summary]:
        """结束会话：把窗口内该会话尚未摘要的消息收进会话摘要。

        消息不离开窗口：检索时“窗口原文 + 会话摘要”两个通道都要可用；
        已摘要的消息 id 会记下来，未来被淘汰时不会重复压进摘要。
        """
        if not self.summaries_enabled:
            return None
        pending = [message for message in self.window.messages() if message.session_id == session_id]
        fresh = self._fresh_for_summary(session_id, pending)
        existing = self.db.get_summary("session", session_id=session_id)
        if not fresh:
            return existing
        summary = self._update_session_summary(session_id, fresh)
        self._mark_summarized(session_id, fresh)
        return summary

    def _fresh_for_summary(self, session_id: str, batch: Sequence[Message]) -> List[Message]:
        seen = self._summarized_message_ids.get(session_id, set())
        return [
            message for message in batch
            if message.id is None or message.id not in seen
        ]

    def _mark_summarized(self, session_id: str, batch: Sequence[Message]) -> None:
        seen = self._summarized_message_ids.setdefault(session_id, set())
        seen.update(message.id for message in batch if message.id is not None)

    def _update_session_summary(self, session_id: str, batch: Sequence[Message]) -> Optional[Summary]:
        previous = self.db.get_summary("session", session_id=session_id)
        parts: List[str] = []
        if previous is not None:
            parts.append(f"已有会话级摘要（请在其基础上补充更新）：\n{previous.content}")
        parts.append(format_messages_for_summary(batch))
        source_text = "\n\n".join(parts)
        period_key = previous.period_key if previous is not None else day_key(batch[0].created_at)
        source_ids = list(previous.source_ids if previous is not None else ())
        source_ids.extend(message.id for message in batch if message.id is not None)

        return self._make_summary(
            level="session",
            source_text=source_text,
            period_key=period_key,
            session_id=session_id,
            source_ids=tuple(source_ids),
            raw_fallback_text=format_messages_for_summary(batch),
        )

    def rollup_daily(self, period_key: Optional[str] = None) -> Optional[Summary]:
        """把某天的会话摘要合并成一份日级摘要。"""
        if not self.summaries_enabled:
            return None
        day = period_key or day_key(self._now_fn())
        sessions = self.db.list_summaries(level="session", day=day)
        if not sessions:
            return None
        source_text = "\n".join(
            f"[会话 {summary.session_id or '-'}] {summary.content}" for summary in sessions
        )
        return self._make_summary(
            level="daily",
            source_text=source_text,
            period_key=day,
            source_ids=tuple(summary.id for summary in sessions if summary.id is not None),
        )

    def rollup_weekly(self, period_key: Optional[str] = None) -> Optional[Summary]:
        """把某周内的日级摘要合并成一份周级摘要。"""
        if not self.summaries_enabled:
            return None
        week = period_key or week_key(self._now_fn())
        dailies = self.db.list_summaries(level="daily", week=week)
        if not dailies:
            return None
        source_text = "\n".join(
            f"[{summary.period_key}] {summary.content}" for summary in dailies
        )
        return self._make_summary(
            level="weekly",
            source_text=source_text,
            period_key=week,
            source_ids=tuple(summary.id for summary in dailies if summary.id is not None),
        )

    def _make_summary(
        self,
        *,
        level: str,
        source_text: str,
        period_key: str,
        session_id: Optional[str] = None,
        source_ids: Sequence[int] = (),
        raw_fallback_text: Optional[str] = None,
    ) -> Summary:
        if self._summarizer is not None:
            try:
                summary = self._summarizer.summarize(
                    level,
                    source_text,
                    period_key=period_key,
                    session_id=session_id,
                    source_ids=source_ids,
                    created_at=self._now_fn(),
                )
            except Exception:  # noqa: BLE001 —— 摘要边界统一降级，消息不能因摘要失败而丢
                summary = None
        else:
            summary = None

        if summary is None:
            fallback_text = raw_fallback_text or source_text
            if len(fallback_text) > self._summary_fallback_chars:
                fallback_text = fallback_text[: self._summary_fallback_chars] + "…（已截断）"
            summary = Summary(
                level=level,
                period_key=period_key,
                content=f"{_FALLBACK_SUMMARY_PREFIX}\n{fallback_text}",
                session_id=session_id,
                source_ids=tuple(source_ids),
                source_text=source_text,
                model="",
                estimated=True,
                created_at=self._now_fn(),
            )
        return self.db.upsert_summary(summary)

    # ------------------------------------------------------------------ KV 写入

    def remember(
        self,
        key: str,
        value: str,
        *,
        category: str = "",
        weight: float = 1.0,
        now: Optional[datetime] = None,
    ) -> KVMemory:
        """写一条 key-value 长期记忆（full 策略检索时会参与时间衰减排序）。"""
        return self.db.put_memory(key, value, category=category, weight=weight, now=now)

    # ------------------------------------------------------------------ 检索

    def retrieve(
        self,
        question: str,
        *,
        session_id: Optional[str] = None,
        top_k: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> MemoryContext:
        """按当前策略返回上下文。无记忆/仅窗口/窗口+摘要/完整系统只在这里分叉。"""
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question 必须是非空字符串")
        items: List[MemoryContextItem] = []

        if self.strategy is not MemoryStrategy.NONE:
            for message in self.window.messages():
                items.append(
                    MemoryContextItem(
                        source="window",
                        text=message.content,
                        timestamp=message.created_at,
                    )
                )

        if self.summaries_enabled:
            if session_id:
                session_summaries = []
                session_summary = self.db.get_summary("session", session_id=session_id)
                if session_summary is not None:
                    session_summaries.append(session_summary)
            else:
                session_summaries = self.db.list_summaries(
                    level="session", limit=self._summary_session_limit
                )
            for summary in session_summaries:
                items.append(
                    MemoryContextItem(
                        source="session_summary",
                        text=f"[{summary.session_id or '-'}] {summary.content}",
                        timestamp=summary.created_at,
                    )
                )
            for summary in self.db.list_summaries(level="daily", limit=self._summary_daily_limit):
                items.append(
                    MemoryContextItem(
                        source="daily_summary",
                        text=f"[{summary.period_key}] {summary.content}",
                        timestamp=summary.created_at,
                    )
                )
            for summary in self.db.list_summaries(level="weekly", limit=self._summary_weekly_limit):
                items.append(
                    MemoryContextItem(
                        source="weekly_summary",
                        text=f"[{summary.period_key}] {summary.content}",
                        timestamp=summary.created_at,
                    )
                )

        if self.strategy is MemoryStrategy.FULL:
            records = self.db.search_memories(
                question,
                top_k=top_k or self.long_term_top_k,
                now=now or self._now_fn(),
                decay_lambda=self.decay_lambda,
            )
            for record in records:
                items.append(
                    MemoryContextItem(
                        source="long_term",
                        text=f"{record.key}: {record.value}",
                        score=record.score,
                        timestamp=record.updated_at,
                        key=record.key,
                    )
                )

        return MemoryContext(
            strategy=self.strategy.value,
            items=items,
            text=render_context_items(items),
        )

    def augment_question(self, question: str, **retrieve_kwargs: Any) -> str:
        """外层组件接缝：把记忆上下文拼进用户问题，再交给 M1 run_tool_loop。"""
        context = self.retrieve(question, **retrieve_kwargs)
        if not context.items:
            return question
        return (
            "以下是记忆系统提供的上下文，供回答时参考；与问题无关的上下文可以忽略。\n\n"
            f"{context.text}\n\n用户问题：{question}"
        )

    def list_summaries(self, level: Optional[str] = None) -> List[Summary]:
        return self.db.list_summaries(level=level)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "MemoryManager":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def render_context_items(items: Sequence[MemoryContextItem]) -> str:
    """把检索命中的证据渲染成可读文本，供提示词与调试日志复用。"""
    lines: List[str] = []
    for item in items:
        if item.source == "window":
            lines.append(f"[滑动窗口] {item.text}")
        elif item.source == "session_summary":
            lines.append(f"[会话摘要] {item.text}")
        elif item.source == "daily_summary":
            lines.append(f"[日级摘要] {item.text}")
        elif item.source == "weekly_summary":
            lines.append(f"[周级摘要] {item.text}")
        elif item.source == "long_term":
            score_text = "" if item.score is None else f"（时间衰减分: {item.score:.6f}）"
            lines.append(f"[长期记忆] {item.text}{score_text}")
        else:
            lines.append(f"[{item.source}] {item.text}")
    return "\n".join(lines)


@dataclass(frozen=True)
class ReplayResult:
    """同一批对话在一个策略下重放得到的结果。"""

    strategy: str
    db_path: str
    context: MemoryContext
    summary_levels: Tuple[str, ...] = ()


def replay_memory_strategies(
    messages: Sequence[Message],
    question: str,
    *,
    strategies: Sequence[MemoryStrategy] = ALL_MEMORY_STRATEGIES,
    db_dir: Optional[Union[str, os.PathLike]] = None,
    model: Any = None,
    setup: Optional[Callable[[MemoryManager], None]] = None,
    manager_kwargs: Optional[Dict[str, Any]] = None,
) -> Dict[str, ReplayResult]:
    """M5 评测的地基：同一批对话用不同策略重放，每个策略独立落库。"""
    base_dir = Path(db_dir) if db_dir is not None else Path(tempfile.mkdtemp(prefix="pda-memory-replay-"))
    base_dir.mkdir(parents=True, exist_ok=True)
    kwargs = dict(manager_kwargs or {})
    results: Dict[str, ReplayResult] = {}

    for strategy in strategies:
        strategy_value = MemoryStrategy(strategy).value
        db_path = base_dir / f"memory_{strategy_value}.db"
        db_path.unlink(missing_ok=True)
        manager = MemoryManager(strategy=strategy_value, db_path=db_path, model=model, **kwargs)
        try:
            if setup is not None:
                setup(manager)
            manager.ingest_messages(messages, finalize=True)
            context = manager.retrieve(question)
            summaries = manager.list_summaries()
            levels = tuple(sorted({summary.level for summary in summaries}))
            results[strategy_value] = ReplayResult(
                strategy=strategy_value,
                db_path=str(db_path),
                context=context,
                summary_levels=levels,
            )
        finally:
            manager.close()
    return results


__all__ = [
    "MemoryManager",
    "ReplayResult",
    "render_context_items",
    "replay_memory_strategies",
]
