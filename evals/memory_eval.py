# -*- coding: utf-8 -*-
# 设计取舍：
# 1) 记忆评测的“可复现”优先于“像真人”：默认用 ContextOracleAnswerModel
#    做确定性回答——上下文里凑齐某参考要点的证据，就把该要点写进最终答案。
#    因此命中率测的是“各策略检索通道的完备性”，而不是真实模型的语言能力；
#    真实 DeepSeek 与 LLM-as-judge 留作可选增强，不阻塞离线验收。
# 2) 每道题都调用 M2 的 replay_memory_strategies() 重新喂同一批对话，并让每个
#    策略落独立 SQLite 库；答题阶段走 M1 run_tool_loop()，评测链路与生产一致。
# 3) token 成本只统计答题阶段：检索注入 token 单独列、模型 prompt/completion
#    由 oracle 按与 M1 相同的 UTF-8/4 公式估算；摘要生成成本与答题成本分开口径，
#    不在同一张表里混加。

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from personal_data_assistant.core.loop import LoopResult, run_tool_loop
from personal_data_assistant.llm.client import LLMResponse, TokenUsage, estimate_tokens
from personal_data_assistant.memory.models import Message
from personal_data_assistant.memory.retriever import (
    MemoryManager,
    replay_memory_strategies,
)
from personal_data_assistant.tools.registry import ToolRegistry

DEFAULT_MEMORY_QUESTIONS = Path(__file__).resolve().parent / "questions" / "memory_50.json"
INPUT_PRICE_PER_MTOK = 2.0  # 元 / 百万 token，deepseek-chat 公开价的近似值
OUTPUT_PRICE_PER_MTOK = 8.0
_CONTEXT_HEADER = (
    "以下是记忆系统提供的上下文，供回答时参考；与问题无关的上下文可以忽略。\n\n"
)
_CONTEXT_FOOTER = "\n\n用户问题："


def load_memory_dataset(path: Union[str, Path] = DEFAULT_MEMORY_QUESTIONS) -> Dict[str, Any]:
    """读取题集 JSON；所有字段校验交给 tests/test_evals.py，这里只保证能解析。"""
    raw = Path(path).read_text(encoding="utf-8")
    return json.loads(raw)


def build_messages(dataset: Mapping[str, Any]) -> List[Message]:
    """把题集里的消息素材转成 MemoryManager 认识的 Message 序列（保序）。"""
    messages: List[Message] = []
    for item in dataset["corpus"]:
        created_at = datetime.fromisoformat(item["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        messages.append(
            Message(
                role=item["role"],
                content=item["content"],
                session_id=item["session_id"],
                created_at=created_at,
            )
        )
    return messages


def make_kv_setup(dataset: Mapping[str, Any]) -> Callable[[MemoryManager], None]:
    """生成 replay 的 setup 回调：把长期记忆写入当前策略的独立库里。"""

    def setup(manager: MemoryManager) -> None:
        for item in dataset["kv_memories"]:
            updated_at = datetime.fromisoformat(item.get("updated_at") or item.get("created_at"))
            manager.remember(
                item["key"],
                item["value"],
                category=item.get("category", ""),
                weight=float(item.get("weight", 1.0)),
                now=updated_at,
            )

    return setup


class PreservingSummaryModel:
    """确定性摘要模型：原样保留待压缩内容，保证旧消息事实能进入摘要通道。

    真实 LLM 摘要会丢数字；M5 的离线模式故意用“无损摘要”来测检索通道上限，
    报告里会写明这不是摘要质量评测。
    """

    def complete(self, messages: Sequence[Mapping[str, str]]) -> LLMResponse:
        prompt_text = "\n".join(str(item["content"]) for item in messages)
        user_text = str(messages[-1]["content"])
        marker = "待压缩内容：\n"
        content = user_text.split(marker, 1)[1] if marker in user_text else user_text
        return LLMResponse(
            content=content,
            model="eval-preserving-summarizer",
            usage=TokenUsage(
                prompt_tokens=estimate_tokens(prompt_text),
                completion_tokens=estimate_tokens(content),
                total_tokens=estimate_tokens(prompt_text) + estimate_tokens(content),
                estimated=True,
            ),
            raw={},
        )


def extract_context_text(augmented_question: str) -> str:
    """从 augment_question 的拼装文本里取回上下文；无上下文时返回空串。"""
    text = str(augmented_question or "")
    if _CONTEXT_HEADER not in text:
        return ""
    after_header = text.split(_CONTEXT_HEADER, 1)[1]
    before_footer = after_header.split(_CONTEXT_FOOTER, 1)[0]
    return before_footer.strip()


def augment_question_for_eval(question: str, context_text: str) -> str:
    """与 MemoryManager.augment_question 保持同一格式，避免评测和生产提示词漂移。"""
    if not context_text.strip():
        return question
    return f"{_CONTEXT_HEADER}{context_text}{_CONTEXT_FOOTER}{question}"


@dataclass
class QuestionScore:
    question_id: str
    matched: int
    total: int
    hit: bool
    coverage: float


class ContextOracleAnswerModel:
    """确定性答题 oracle：只根据“上下文”里的证据拼答案，不看问题本身的关键词。

    complete() 返回 M1 JSON 动作协议，因此答题阶段走 run_tool_loop 的真实路径。
    """

    def __init__(self, question_item: Mapping[str, Any]) -> None:
        self.question_item = dict(question_item)

    def complete(self, messages: Sequence[Mapping[str, str]]) -> LLMResponse:
        context = extract_context_text(str(messages[-1]["content"]))
        points: List[str] = []
        for point in self.question_item["reference_points"]:
            texts = point.get("evidence_texts") or []
            if texts and all(str(t) in context for t in texts):
                points.append(str(point["point"]))

        if points:
            answer = "。".join(points) + "。"
        else:
            answer = "根据当前记忆上下文，我暂时无法回答这个问题。"

        output = json.dumps({"action": "final", "answer": answer}, ensure_ascii=False)
        prompt_tokens = sum(estimate_tokens(str(item["content"])) for item in messages)
        completion_tokens = estimate_tokens(output)
        return LLMResponse(
            content=output,
            model="eval-context-oracle",
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                estimated=True,
            ),
            raw={},
        )


def evaluate_answer(answer: str, question_item: Mapping[str, Any]) -> Dict[str, Any]:
    """规则判分：参考答案要点逐条检查是否出现在最终答案里。"""
    answer_text = str(answer or "")
    matched = 0
    total = 0
    for point in question_item["reference_points"]:
        total += 1
        terms = point.get("answer_terms") or [point["point"]]
        if any(str(term) in answer_text for term in terms):
            matched += 1
    return {
        "matched": matched,
        "total": total,
        "hit": total > 0 and matched == total,
        "coverage": matched / total if total else 0.0,
    }


def _strategy_bucket() -> Dict[str, Any]:
    return {
        "hits": 0,
        "points_hit": 0,
        "points_total": 0,
        "context_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "model_calls": 0,
        "questions": [],
    }


def run_memory_eval(
    *,
    dataset_path: Union[str, Path] = DEFAULT_MEMORY_QUESTIONS,
    db_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """同一批对话 × 四种策略 × 全部记忆题，产出命中率与 token 成本。"""
    resolved_dataset_path = DEFAULT_MEMORY_QUESTIONS if dataset_path is None else Path(dataset_path)
    dataset = load_memory_dataset(resolved_dataset_path)
    messages = build_messages(dataset)
    settings = dataset.get("meta", {}).get("strategy_settings", {})
    now = datetime.fromisoformat(dataset.get("meta", {}).get("now", "2025-08-24T12:00:00+00:00"))

    base_dir = Path(db_dir) if db_dir is not None else Path(tempfile.mkdtemp(prefix="pda-memory-eval-"))
    base_dir.mkdir(parents=True, exist_ok=True)

    strategies: Dict[str, Any] = {
        "none": _strategy_bucket(),
        "window": _strategy_bucket(),
        "window_summary": _strategy_bucket(),
        "full": _strategy_bucket(),
    }
    per_question: List[Dict[str, Any]] = []

    manager_kwargs = {
        "max_window_messages": int(settings.get("max_window_messages", 20)),
        "max_window_tokens": int(settings.get("max_window_tokens", 8000)),
        "long_term_top_k": int(settings.get("long_term_top_k", 8)),
        "decay_lambda": float(settings.get("decay_lambda", 0.05)),
        "summary_session_limit": int(settings.get("summary_session_limit", 5)),
        "summary_daily_limit": int(settings.get("summary_daily_limit", 5)),
        "summary_weekly_limit": int(settings.get("summary_weekly_limit", 3)),
        "now_fn": lambda: now,
    }

    for question_item in dataset["questions"]:
        question = str(question_item["question"])
        replays = replay_memory_strategies(
            messages,
            question,
            db_dir=base_dir / "replay",
            model=PreservingSummaryModel(),
            setup=make_kv_setup(dataset),
            manager_kwargs=manager_kwargs,
        )
        question_record: Dict[str, Any] = {
            "question_id": question_item["id"],
            "category": question_item.get("category"),
            "question": question,
            "expected_strategies": list(question_item.get("expected_strategies", [])),
            "strategies": {},
        }

        for strategy, bucket in strategies.items():
            replay = replays[strategy]
            context_text = replay.context.text
            context_tokens = estimate_tokens(context_text)
            oracle = ContextOracleAnswerModel(question_item)
            augmented = augment_question_for_eval(question, context_text)
            loop_result: LoopResult = run_tool_loop(
                augmented,
                ToolRegistry([]),
                oracle,
                max_tool_rounds=1,
            )
            score = evaluate_answer(loop_result.answer, question_item)

            bucket["hits"] += int(score["hit"])
            bucket["points_hit"] += int(score["matched"])
            bucket["points_total"] += int(score["total"])
            bucket["context_tokens"] += context_tokens
            bucket["prompt_tokens"] += loop_result.total_usage.prompt_tokens
            bucket["completion_tokens"] += loop_result.total_usage.completion_tokens
            bucket["total_tokens"] += loop_result.total_usage.total_tokens
            bucket["model_calls"] += loop_result.rounds

            question_record["strategies"][strategy] = {
                "hit": bool(score["hit"]),
                "matched": score["matched"],
                "total": score["total"],
                "coverage": score["coverage"],
                "context_tokens": context_tokens,
                "prompt_tokens": loop_result.total_usage.prompt_tokens,
                "completion_tokens": loop_result.total_usage.completion_tokens,
                "total_tokens": loop_result.total_usage.total_tokens,
                "loop_status": loop_result.status,
                "answer": loop_result.answer,
            }

        per_question.append(question_record)

    total = len(dataset["questions"])
    for strategy, bucket in strategies.items():
        bucket["questions"] = total
        bucket["hit_rate"] = bucket["hits"] / total if total else 0.0
        bucket["point_coverage"] = (
            bucket["points_hit"] / bucket["points_total"] if bucket["points_total"] else 0.0
        )
        bucket["estimated_cost"] = _cost(
            bucket["prompt_tokens"], bucket["completion_tokens"]
        )

    return {
        "meta": {
            "dataset": str(resolved_dataset_path.resolve()),
            "questions_total": total,
            "corpus_messages": len(messages),
            "kv_memories": len(dataset["kv_memories"]),
            "now": now.isoformat(),
            "answer_mode": "deterministic-context-oracle",
            "pricing": {
                "input_per_mtok": INPUT_PRICE_PER_MTOK,
                "output_per_mtok": OUTPUT_PRICE_PER_MTOK,
                "currency": "CNY",
            },
            "settings": {
                "max_window_messages": int(settings.get("max_window_messages", 20)),
                "max_window_tokens": int(settings.get("max_window_tokens", 8000)),
                "long_term_top_k": int(settings.get("long_term_top_k", 8)),
                "decay_lambda": float(settings.get("decay_lambda", 0.05)),
                "summary_session_limit": int(settings.get("summary_session_limit", 5)),
                "summary_daily_limit": int(settings.get("summary_daily_limit", 5)),
                "summary_weekly_limit": int(settings.get("summary_weekly_limit", 3)),
                "now": now.isoformat(),
            },
        },
        "questions_total": total,
        "strategies": strategies,
        "per_question": per_question,
    }


def _cost(prompt_tokens: int, completion_tokens: int) -> float:
    return (
        prompt_tokens / 1_000_000 * INPUT_PRICE_PER_MTOK
        + completion_tokens / 1_000_000 * OUTPUT_PRICE_PER_MTOK
    )


__all__ = [
    "ContextOracleAnswerModel",
    "PreservingSummaryModel",
    "augment_question_for_eval",
    "build_messages",
    "evaluate_answer",
    "extract_context_text",
    "load_memory_dataset",
    "make_kv_setup",
    "run_memory_eval",
]
