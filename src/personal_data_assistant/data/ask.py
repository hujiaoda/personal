# -*- coding: utf-8 -*-
# 设计取舍：
# 1) 问数编排是 sql_query 工具内部的子流程，只依赖模型 complete(messages) 鸭子
#    协议；刻意不 import llm/client，守住 data 不依赖 llm 的模块边界。
# 2) 修正子循环：schema 只探查一次，SQL 生成失败/执行失败都把「失败 SQL + 真实
#    错误」回填模型重写；最多 max_fix_rounds 次修正（首次生成不计入修正）。
# 3) 评测埋点 M3 只记录单次事实（首次是否成功、修正是否成功、修正轮数、token
#    成本、每次尝试日志），不做成功率/均值聚合——聚合是 M5 的职责。
# 4) 解释结果失败不推翻已经查到的数据：用确定性中文格式化兜底，数字、口径、
#    SQL 原文都在答案里，对应架构文档的降级 B 计划。

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Sequence, Tuple, Union

from personal_data_assistant.data.schema import discover_schema, render_schema_summary
from personal_data_assistant.data.sqlite import (
    QueryResult,
    ReadOnlySQLite,
    SQLDatabaseError,
    SQLExecutionError,
    SQLSecurityError,
)

_SQL_KEYWORD_RE = re.compile(r"(SELECT|WITH)\b", re.IGNORECASE)

_SQL_SYSTEM_PROMPT = (
    "你是 SQLite 问数专家。根据给定的表结构 JSON 回答用户问题，只输出一条 SQL 语句。\n"
    "硬性规则：\n"
    "1. SQL 必须以 SELECT 或 WITH 开头，只做只读查询，禁止 INSERT/UPDATE/DELETE/DROP/PRAGMA。\n"
    "2. 只输出 SQL 本身：不要 Markdown 围栏、不要 JSON、不要解释、不要分号后的第二条语句。\n"
    "3. 表名和列名必须来自表结构 JSON；中文字段名可以直接使用。\n"
    "4. 聚合问题写清口径（GROUP BY、筛选条件），金额/时长求和用 SUM，比例和平均值要写明。"
)

_EXPLAIN_SYSTEM_PROMPT = (
    "你是个人数据助手的问数解释器。请用中文向用户解释 SQLite 查询结果。\n"
    "要求：\n"
    "1. 先给结论数字，再说明统计口径（用了哪张表、哪些字段、什么筛选条件）。\n"
    "2. 查询结果为空时，明确说“没有查到符合条件的数据”，并给出可能原因。\n"
    "3. 如果结果被行数上限截断，必须说明“以下只是前 N 行”。\n"
    "4. 结尾附上 SQL 原文，便于用户核对。只输出回答正文，不要 JSON 或 Markdown 标题。"
)


@dataclass
class AskUsage:
    """一次问数全流程的 token 成本；字段与 llm.TokenUsage 对齐但 data 包不 import llm。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False

    def add(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        estimated: bool,
    ) -> None:
        self.prompt_tokens += max(0, int(prompt_tokens))
        self.completion_tokens += max(0, int(completion_tokens))
        self.total_tokens += max(0, int(total_tokens))
        self.estimated = self.estimated or bool(estimated)


@dataclass(frozen=True)
class SqlAttempt:
    """一次 SQL 执行尝试；M5 判首轮成功率/修正成功率时逐条读取。"""

    index: int
    sql: str
    ok: bool
    error: str = ""
    row_count: int = 0


@dataclass(frozen=True)
class SqlEvalRecord:
    """单题评测事实记录。M5 用一批 record 聚合成功率/平均修正轮数/成本。"""

    question: str
    db_path: str
    status: str
    attempts: int
    first_attempt_success: bool
    fix_success: bool
    total_fix_rounds: int
    model_calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated: bool
    latency_ms: float
    error: str = ""


@dataclass
class AskResult:
    """ask_database 的完整结果：答案 + 数据 + SQL + 评测埋点。"""

    question: str
    db_path: str
    answer: str
    status: str  # success / failed / model_error
    sql: str = ""
    columns: Tuple[str, ...] = ()
    rows: Tuple[Tuple[Any, ...], ...] = ()
    row_count: int = 0
    truncated: bool = False
    attempts: int = 0
    first_attempt_success: bool = False
    fix_success: bool = False
    total_fix_rounds: int = 0
    model_calls: int = 0
    usage: AskUsage = field(default_factory=AskUsage)
    error: str = ""
    attempts_log: Tuple[SqlAttempt, ...] = ()

    def to_eval_record(self, *, latency_ms: float = 0.0) -> SqlEvalRecord:
        return SqlEvalRecord(
            question=self.question,
            db_path=self.db_path,
            status=self.status,
            attempts=self.attempts,
            first_attempt_success=self.first_attempt_success,
            fix_success=self.fix_success,
            total_fix_rounds=self.total_fix_rounds,
            model_calls=self.model_calls,
            prompt_tokens=self.usage.prompt_tokens,
            completion_tokens=self.usage.completion_tokens,
            total_tokens=self.usage.total_tokens,
            estimated=self.usage.estimated,
            latency_ms=float(latency_ms),
            error=self.error,
        )

    def to_dict(self) -> dict:
        """工具回填给核心循环的 JSON 友好结构；M1 的 json.dumps 无需 default=str。"""
        return {
            "question": self.question,
            "db_path": self.db_path,
            "answer": self.answer,
            "status": self.status,
            "sql": self.sql,
            "columns": list(self.columns),
            "rows": [list(row) for row in self.rows],
            "row_count": self.row_count,
            "truncated": self.truncated,
            "attempts": self.attempts,
            "first_attempt_success": self.first_attempt_success,
            "fix_success": self.fix_success,
            "total_fix_rounds": self.total_fix_rounds,
            "model_calls": self.model_calls,
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
                "estimated": self.usage.estimated,
            },
            "error": self.error,
        }


def _estimate_tokens(text: str) -> int:
    """usage 缺失时的 UTF-8 粗估，与 M1/M2 同公式，只保证成本日志有数可记。"""
    if not text:
        return 0
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


def _usage_value(usage: Any, name: str, default: int = 0) -> int:
    if isinstance(usage, Mapping):
        raw = usage.get(name, default)
    else:
        raw = getattr(usage, name, default)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _normalize_model_response(response: Any) -> Tuple[str, str, Any, bool]:
    """把 str / mapping / 鸭子对象统一成 (content, model, usage, estimated)。"""
    if isinstance(response, str):
        content = response
        model = ""
        usage = None
    elif isinstance(response, Mapping):
        content = response.get("content")
        if content is None:
            raise ValueError("模型响应缺少 content")
        model = str(response.get("model") or "")
        usage = response.get("usage")
    else:
        content = getattr(response, "content", None)
        if content is None:
            raise ValueError(
                f"模型 complete 返回值必须含 content 字段或为字符串，当前: {type(response).__name__}"
            )
        model = str(getattr(response, "model", "") or "")
        usage = getattr(response, "usage", None)

    text = str(content)
    if usage is None:
        completion = _estimate_tokens(text)
        return text, model, (0, completion, completion), True
    return (
        text,
        model,
        (
            _usage_value(usage, "prompt_tokens"),
            _usage_value(usage, "completion_tokens"),
            _usage_value(
                usage,
                "total_tokens",
                default=_usage_value(usage, "prompt_tokens") + _usage_value(usage, "completion_tokens"),
            ),
        ),
        False,
    )


def _call_model_complete(model: Any, messages: Sequence[Mapping[str, str]]) -> Tuple[str, Any, bool]:
    complete = getattr(model, "complete", None)
    if not callable(complete):
        raise TypeError("模型对象必须提供可调用的 complete(messages) 方法")
    response = complete(list(messages))
    content, _, usage, estimated = _normalize_model_response(response)
    return content, usage, estimated


def build_sql_generation_messages(question: str, schema_text: str) -> List[dict]:
    return [
        {"role": "system", "content": _SQL_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"数据库表结构（紧凑 JSON）：\n{schema_text}\n\n"
                f"用户问题：{question}\n请只输出解决该问题的一条 SQL。"
            ),
        },
    ]


def build_sql_fix_messages(
    question: str,
    schema_text: str,
    failed_sql: str,
    error: str,
) -> List[dict]:
    return [
        {"role": "system", "content": _SQL_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"数据库表结构（紧凑 JSON）：\n{schema_text}\n\n"
                f"用户问题：{question}\n\n"
                f"你上一次生成的 SQL 执行失败：\nSQL：{failed_sql}\n错误：{error}\n"
                "请分析错误原因，只输出修正后的一条 SQL；仍只能 SELECT/WITH 开头。"
            ),
        },
    ]


def build_explanation_messages(
    question: str,
    sql: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    row_count: int,
    truncated: bool,
) -> List[dict]:
    rows_text = format_rows_text(columns, rows, truncated)
    return [
        {"role": "system", "content": _EXPLAIN_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"用户问题：{question}\n\n"
                f"实际执行的 SQL：{sql}\n"
                f"查询到 {row_count} 行结果"
                f"{'（结果超过上限，已经截断显示）' if truncated else ''}：\n{rows_text}\n"
                "请按系统要求生成中文解释。"
            ),
        },
    ]


def extract_sql_text(raw: str) -> str:
    """从模型输出里提取 SQL：支持 Markdown 围栏、JSON 的 sql 字段、前后零星说明。"""
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            pass
        else:
            if isinstance(payload, Mapping):
                sql_value = payload.get("sql")
                if isinstance(sql_value, str):
                    return sql_value.strip()
    match = _SQL_KEYWORD_RE.search(text)
    if match:
        return text[match.start() :].strip()
    return text


def format_rows_text(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    truncated: bool,
) -> str:
    """把结果行渲染成提示词与兜底答案共用的可读文本。"""
    lines = ["列：" + " | ".join(str(column) for column in columns)]
    for row in rows:
        lines.append(" | ".join(str(value) for value in row))
    if truncated:
        lines.append(f"（结果超过上限，仅显示前 {len(rows)} 行）")
    return "\n".join(lines)


def format_result_fallback(
    question: str,
    sql: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    row_count: int,
    truncated: bool,
) -> str:
    """解释模型不可用时的确定性兜底：数字、口径、SQL 原文都在。"""
    if row_count == 0:
        head = "没有查到符合条件的数据。"
    else:
        head = f"查询完成，共查到 {row_count} 行结果。"
        if truncated:
            head += f"（结果超过上限，仅显示前 {row_count} 行）"
    body = format_rows_text(columns, rows, truncated)
    return (
        f"{head}\n\n"
        f"统计口径：针对问题「{question}」，使用下面的 SQLite 查询计算得到。\n"
        f"SQL：{sql}\n\n"
        f"查询结果：\n{body}"
    )


def _build_exhausted_answer(question: str, last_error: str, attempts_log: Sequence[SqlAttempt]) -> str:
    lines = [
        f"这个问题我没查到，原因如下：{last_error}",
        "试过的 SQL 如下：",
    ]
    for attempt in attempts_log:
        lines.append(f"{attempt.index}. {attempt.sql}")
        lines.append(f"   错误：{attempt.error or '执行成功'}")
    return "\n".join(lines)


def _build_model_error_answer(error: str, attempts_log: Sequence[SqlAttempt]) -> str:
    lines = [f"模型暂时不可用，无法继续生成或解释 SQL。原因：{error}。请稍后重试。"]
    if attempts_log:
        lines.append("此前试过的 SQL：")
        for attempt in attempts_log:
            lines.append(f"{attempt.index}. {attempt.sql}（错误：{attempt.error or '执行成功'}）")
    return "\n".join(lines)


def _format_attempt_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def ask_database(
    question: str,
    db_path: Union[str, bytes],
    model: Any,
    *,
    max_fix_rounds: int = 3,
    query_timeout: float = 5.0,
    max_rows: int = 100,
    schema_sample_size: int = 3,
) -> AskResult:
    """自然语言问数主流程：schema → 生成 SQL → 只读执行 → 失败修正 → 中文解释。"""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question 必须是非空字符串")
    if max_fix_rounds < 1:
        raise ValueError("max_fix_rounds 必须 >= 1")
    if query_timeout <= 0:
        raise ValueError("query_timeout 必须 > 0")
    if max_rows < 1:
        raise ValueError("max_rows 必须 >= 1")

    started = time.monotonic()
    usage = AskUsage()
    model_calls = 0
    attempts_log: List[SqlAttempt] = []
    question = question.strip()

    # 第一手：schema 探查失败直接结构化失败，不浪费模型调用。
    try:
        schema = discover_schema(
            db_path,
            sample_size=schema_sample_size,
            query_timeout=query_timeout,
        )
    except (SQLDatabaseError, SQLExecutionError, SQLSecurityError, ValueError) as exc:
        return AskResult(
            question=question,
            db_path=str(db_path),
            answer=f"数据库不可用，暂时无法回答这个问题。原因：{_format_attempt_error(exc)}",
            status="failed",
            model_calls=0,
            usage=usage,
            error=_format_attempt_error(exc),
        )
    schema_text = render_schema_summary(schema)

    final_sql = ""
    last_error = ""
    query_result: Optional[QueryResult] = None
    fix_rounds = 0
    status = "failed"

    while True:
        if fix_rounds == 0:
            messages = build_sql_generation_messages(question, schema_text)
        else:
            messages = build_sql_fix_messages(question, schema_text, final_sql, last_error)

        model_calls += 1
        try:
            content, usage_tuple, estimated = _call_model_complete(model, messages)
        except Exception as exc:  # noqa: BLE001 —— 模型边界收口，不崩溃
            return AskResult(
                question=question,
                db_path=str(db_path),
                answer=_build_model_error_answer(str(exc), attempts_log),
                status="model_error",
                sql=final_sql,
                attempts=len(attempts_log),
                first_attempt_success=False,
                fix_success=False,
                total_fix_rounds=fix_rounds,
                model_calls=model_calls,
                usage=usage,
                error=str(exc),
                attempts_log=tuple(attempts_log),
            )
        usage.add(*usage_tuple, estimated=estimated)

        raw_sql = extract_sql_text(content)
        executor = ReadOnlySQLite(
            db_path,
            query_timeout=query_timeout,
            max_rows=max_rows,
        )
        try:
            try:
                query_result = executor.execute_query(raw_sql)
            except (SQLSecurityError, SQLExecutionError, SQLDatabaseError) as exc:
                final_sql = raw_sql
                last_error = _format_attempt_error(exc)
                attempts_log.append(
                    SqlAttempt(
                        index=len(attempts_log) + 1,
                        sql=raw_sql,
                        ok=False,
                        error=last_error,
                    )
                )
                if fix_rounds >= max_fix_rounds:
                    status = "failed"
                    break
                fix_rounds += 1
                continue
            else:
                attempts_log.append(
                    SqlAttempt(
                        index=len(attempts_log) + 1,
                        sql=raw_sql,
                        ok=True,
                        row_count=query_result.row_count,
                    )
                )
                final_sql = raw_sql
                status = "success"
                break
        finally:
            executor.close()

    attempts = len(attempts_log)
    first_attempt_success = attempts == 1 and status == "success"
    fix_success = status == "success" and attempts > 1

    if status != "success":
        assert last_error
        return AskResult(
            question=question,
            db_path=str(db_path),
            answer=_build_exhausted_answer(question, last_error, attempts_log),
            status=status,
            sql=final_sql,
            attempts=attempts,
            first_attempt_success=False,
            fix_success=False,
            total_fix_rounds=fix_rounds,
            model_calls=model_calls,
            usage=usage,
            error=last_error,
            attempts_log=tuple(attempts_log),
        )

    assert query_result is not None
    model_calls += 1
    try:
        content, usage_tuple, estimated = _call_model_complete(
            model,
            build_explanation_messages(
                question,
                final_sql,
                query_result.columns,
                query_result.rows,
                query_result.row_count,
                query_result.truncated,
            ),
        )
    except Exception as exc:  # noqa: BLE001 —— 数据已到手，解释失败走确定性兜底
        explanation = ""
        usage.add(0, 0, 0, estimated=False)
    else:
        usage.add(*usage_tuple, estimated=estimated)
        explanation = content.strip()

    if not explanation:
        explanation = format_result_fallback(
            question=question,
            sql=final_sql,
            columns=query_result.columns,
            rows=query_result.rows,
            row_count=query_result.row_count,
            truncated=query_result.truncated,
        )

    return AskResult(
        question=question,
        db_path=str(db_path),
        answer=explanation,
        status="success",
        sql=final_sql,
        columns=query_result.columns,
        rows=query_result.rows,
        row_count=query_result.row_count,
        truncated=query_result.truncated,
        attempts=attempts,
        first_attempt_success=first_attempt_success,
        fix_success=fix_success,
        total_fix_rounds=fix_rounds,
        model_calls=model_calls,
        usage=usage,
        error="",
        attempts_log=tuple(attempts_log),
    )


__all__ = [
    "AskResult",
    "AskUsage",
    "SqlAttempt",
    "SqlEvalRecord",
    "ask_database",
    "build_explanation_messages",
    "build_sql_fix_messages",
    "build_sql_generation_messages",
    "extract_sql_text",
    "format_result_fallback",
    "format_rows_text",
]
