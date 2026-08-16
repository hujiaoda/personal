# -*- coding: utf-8 -*-
# 设计取舍：
# 1) 摘要器与核心循环一样只认模型鸭子协议 complete(messages)；真实 API 用
#    DeepSeekClient，测试用一个记录调用的 fake 对象即可，不 import llm。
# 2) 层级语义固化在提示词里：会话级压掉寒暄、日级合并会话、周级合并日级；
#    period_key 和 session_id 都显式写进 prompt，方便调试与评测溯源。
# 3) 模型异常包成 SummarizerError 而不是吞掉：manager 在边界处再决定如何
#    降级（例如保留截断原文），摘要器本身不假装成功。

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, List, Mapping, Optional, Sequence

from personal_data_assistant.memory.models import SUMMARY_LEVELS, Summary, utcnow

_LEVEL_LABELS = {
    "session": "会话级摘要",
    "daily": "日级摘要",
    "weekly": "周级摘要",
}


class SummarizerError(RuntimeError):
    """LLM 摘要失败；上层可据此降级为原文暂存。"""


def format_messages_for_summary(messages: Sequence[Any]) -> str:
    """把消息序列格式化成摘要器的输入文本。接受 Message 或 role/content 映射。"""
    lines: List[str] = []
    for message in messages:
        if isinstance(message, Mapping):
            role = str(message.get("role", "unknown"))
            content = str(message.get("content", ""))
            session_id = str(message.get("session_id", "") or "")
            created_at = str(message.get("created_at", "") or "")
        else:
            role = str(getattr(message, "role", "unknown"))
            content = str(getattr(message, "content", ""))
            session_id = str(getattr(message, "session_id", "") or "")
            created_at = str(getattr(message, "created_at", "") or "")
        prefix = f"[{created_at}]" if created_at else ""
        suffix = f" (session={session_id})" if session_id else ""
        lines.append(f"{prefix}{role}{suffix}: {content}")
    return "\n".join(lines)


def build_summary_messages(
    level: str,
    source_text: str,
    *,
    period_key: str,
    session_id: Optional[str] = None,
) -> List[dict]:
    """构造摘要调用的 messages；测试与实现共用同一份 prompt 契约。"""
    label = _LEVEL_LABELS[level]
    if level == "session":
        task = (
            f"请把下面这段会话消息压缩成一份{label}。保留时间线索、涉及主题、"
            "关键事实、数字、结论和用户偏好；去掉寒暄、重复与工具执行细节。"
            f"会话 ID：{session_id or 'unknown'}；归属日期：{period_key}。"
        )
    elif level == "daily":
        task = (
            f"请把下面这些会话级摘要合并成一份{label}。同主题内容要合并，"
            "保留每个主题的关键事实、数字与结论，并标注主题出现的时间线索。"
            f"归属日期：{period_key}。"
        )
    else:
        task = (
            f"请把下面这些日级摘要合并成一份{label}。按主题归纳一周的进展，"
            "保留关键事实、数字与结论，去掉重复表述。"
            f"归属周：{period_key}。"
        )
    return [
        {
            "role": "system",
            "content": (
                f"你是个人数据助手的记忆整理器。本次任务：生成{label}。"
                "你的摘要会代替原始消息进入长期记忆，"
                "必须只输出摘要正文，不要输出 JSON、Markdown 标题、前缀或任何解释。"
            ),
        },
        {
            "role": "user",
            "content": f"{task}\n\n待压缩内容：\n{source_text}",
        },
    ]


def _usage_value(usage: Any, name: str, default: int = 0) -> int:
    if isinstance(usage, Mapping):
        raw = usage.get(name, default)
    else:
        raw = getattr(usage, name, default)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


class LLMSummarizer:
    """用鸭子协议模型做分层摘要；模型对象只需要 complete(messages)。"""

    def __init__(
        self,
        model: Any,
        *,
        max_source_chars: int = 12000,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._model = model
        if max_source_chars < 1:
            raise ValueError(f"max_source_chars 必须 >= 1，当前: {max_source_chars}")
        self._max_source_chars = max_source_chars
        self._now_fn = now_fn or utcnow

    def summarize(
        self,
        level: str,
        source_text: str,
        *,
        period_key: str,
        session_id: Optional[str] = None,
        source_ids: Sequence[int] = (),
        created_at: Optional[datetime] = None,
    ) -> Summary:
        if level not in SUMMARY_LEVELS:
            raise ValueError(f"summary level 不合法: {level!r}（只能 {SUMMARY_LEVELS}）")
        if not isinstance(period_key, str) or not period_key.strip():
            raise ValueError("period_key 不能为空")
        if not isinstance(source_text, str) or not source_text.strip():
            raise ValueError("source_text 不能为空")

        truncated = source_text
        if len(truncated) > self._max_source_chars:
            truncated = truncated[: self._max_source_chars] + "\n…（原文过长已截断）"

        complete = getattr(self._model, "complete", None)
        if not callable(complete):
            raise SummarizerError("摘要模型必须提供可调用的 complete(messages) 方法")

        try:
            response = complete(build_summary_messages(level, truncated, period_key=period_key, session_id=session_id))
        except Exception as exc:  # noqa: BLE001 —— 摘要边界统一收口
            raise SummarizerError(f"LLM 摘要失败: {exc}") from exc

        content, model_name, usage, estimated = _normalize_summary_response(response)
        if not content.strip():
            raise SummarizerError("LLM 摘要返回了空内容")

        return Summary(
            level=level,
            period_key=period_key,
            content=content.strip(),
            session_id=session_id,
            source_ids=tuple(source_ids),
            source_text=source_text,
            model=model_name,
            prompt_tokens=_usage_value(usage, "prompt_tokens"),
            completion_tokens=_usage_value(usage, "completion_tokens"),
            total_tokens=_usage_value(
                usage,
                "total_tokens",
                default=_usage_value(usage, "prompt_tokens") + _usage_value(usage, "completion_tokens"),
            ),
            estimated=estimated,
            created_at=created_at or self._now_fn(),
        )


def _normalize_summary_response(response: Any) -> tuple:
    """把 fake 对象 / str / mapping 统一成 (content, model, usage, estimated)。"""
    if isinstance(response, str):
        return response, "", None, True
    if isinstance(response, Mapping):
        content = response.get("content")
        if content is None:
            raise SummarizerError("摘要模型响应缺少 content")
        usage = response.get("usage")
        model = str(response.get("model") or "")
        return str(content), model, usage, usage is None
    content = getattr(response, "content", None)
    if content is None:
        raise SummarizerError(
            f"摘要模型 complete 返回值必须含 content 字段或为字符串，当前: {type(response).__name__}"
        )
    usage = getattr(response, "usage", None)
    model = str(getattr(response, "model", "") or "")
    return str(content), model, usage, usage is None


__all__ = [
    "LLMSummarizer",
    "SummarizerError",
    "Summary",
    "build_summary_messages",
    "format_messages_for_summary",
]
