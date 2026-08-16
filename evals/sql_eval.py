# -*- coding: utf-8 -*-
# 设计取舍：
# 1) SQL 评测默认用 ScriptedSQLModel：每题在 JSON 里写死“首轮 SQL + 修正 SQL +
#    中文解释”，不依赖 DeepSeek；同一份题集跑一百次指标完全相同。
#    需要真实模型时，只换 runner 的 model 工厂，判分与聚合逻辑不用动。
# 2) 每道题都跑在独立临时库上（build_demo_tables 的确定性产物），并额外做
#    before/after 快照：非只读陷阱题不仅要看 answer，还要证明数据一个字节没变。
# 3) 结果判分按“行集相等”而不是“SQL 文本相等”：排序后逐行比对，数值带 1e-4
#    容差；这样 fake 与未来真实模型只要结果正确就能得分，不被 SQL 写法绑死。

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from personal_data_assistant.data.ask import ask_database
from personal_data_assistant.data.demo import build_demo_tables
from personal_data_assistant.llm.client import LLMResponse, TokenUsage, estimate_tokens

DEFAULT_SQL_QUESTIONS = Path(__file__).resolve().parent / "questions" / "sql_questions.json"
INPUT_PRICE_PER_MTOK = 2.0  # 元 / 百万 token，deepseek-chat 公开价的近似值
OUTPUT_PRICE_PER_MTOK = 8.0


def load_sql_dataset(path: Union[str, Path] = DEFAULT_SQL_QUESTIONS) -> Dict[str, Any]:
    """读取 SQL 题集 JSON。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def rows_equal(
    actual: Iterable[Iterable[Any]],
    expected: Iterable[Iterable[Any]],
    *,
    tolerance: float = 1e-4,
) -> bool:
    """行集比对：行序无关，数值列带容差，其余列转字符串比较。"""
    actual_rows = [list(row) for row in actual]
    expected_rows = [list(row) for row in expected]
    if len(actual_rows) != len(expected_rows):
        return False

    def sort_key(row: List[Any]) -> str:
        values = [
            "num" if _is_number(value) else str(value)
            for value in row
        ]
        return json.dumps(values, ensure_ascii=False, sort_keys=True)

    actual_sorted = sorted(actual_rows, key=sort_key)
    expected_sorted = sorted(expected_rows, key=sort_key)

    for actual_row, expected_row in zip(actual_sorted, expected_sorted):
        if len(actual_row) != len(expected_row):
            return False
        for actual_value, expected_value in zip(actual_row, expected_row):
            if _is_number(actual_value) and _is_number(expected_value):
                if not math.isclose(
                    float(actual_value),
                    float(expected_value),
                    abs_tol=float(tolerance),
                    rel_tol=float(tolerance),
                ):
                    return False
            elif str(actual_value) != str(expected_value):
                return False
    return True


def _snapshot_tables(db_path: Union[str, Path]) -> Tuple[Tuple[str, Tuple[Tuple[Any, ...], ...]], ...]:
    """把演示库全部行读成可比较快照，用于陷阱题的“数据未变”证明。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        names = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
        ]
        snapshot: List[Tuple[str, Tuple[Tuple[Any, ...], ...]]] = []
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            rows = tuple(
                tuple(row) for row in conn.execute(f"SELECT * FROM {quoted} ORDER BY rowid")
            )
            snapshot.append((name, rows))
        return tuple(snapshot)
    finally:
        conn.close()


class ScriptedSQLModel:
    """脚本化 fake 模型：按题集 JSON 依次吐 SQL/解释，usage 用 UTF-8/4 估算。"""

    def __init__(self, item: Mapping[str, Any]) -> None:
        self.item = dict(item)
        self._fix_index = 0
        self.calls: List[str] = []

    def complete(self, messages: Sequence[Mapping[str, str]]) -> LLMResponse:
        self.calls.append(str(messages[-1]["content"]))
        prompt_tokens = sum(estimate_tokens(str(item["content"])) for item in messages)

        last_user = str(messages[-1]["content"])
        if "实际执行的 SQL" in last_user or "请按系统要求生成中文解释" in last_user:
            content = str(self.item.get("explanation") or "查询完成。")
        elif "执行失败" in last_user and self._fix_index < len(self.item.get("fix_sqls", [])):
            content = str(self.item["fix_sqls"][self._fix_index])
            self._fix_index += 1
        elif self._fix_index == 0:
            content = str(self.item["first_sql"])
        else:
            # 防御分支：修正序列耗尽仍被调用时，重复最后一条修正 SQL，避免静默崩溃。
            content = str(self.item.get("fix_sqls", [self.item["first_sql"]])[-1])

        return LLMResponse(
            content=content,
            model="eval-scripted-sql",
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=estimate_tokens(content),
                total_tokens=prompt_tokens + estimate_tokens(content),
                estimated=True,
            ),
            raw={},
        )


def run_sql_eval(
    *,
    dataset_path: Union[str, Path] = DEFAULT_SQL_QUESTIONS,
    work_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """逐题跑 ask_database（fake 模型 + 全新演示库），聚合 M5 要求的 SQL 指标。"""
    resolved_dataset_path = DEFAULT_SQL_QUESTIONS if dataset_path is None else Path(dataset_path)
    dataset = load_sql_dataset(resolved_dataset_path)
    base_dir = Path(work_dir) if work_dir is not None else Path(tempfile.mkdtemp(prefix="pda-sql-eval-"))
    base_dir.mkdir(parents=True, exist_ok=True)

    records: List[Dict[str, Any]] = []
    for item in dataset["questions"]:
        db_path = base_dir / f"{item['id']}.db"
        db_path.unlink(missing_ok=True)
        build_demo_tables(str(db_path))
        before = _snapshot_tables(db_path)

        model = ScriptedSQLModel(item)
        started = time.monotonic()
        result = ask_database(
            str(item["question"]),
            str(db_path),
            model,
            max_fix_rounds=3,
            query_timeout=5.0,
            max_rows=100,
            schema_sample_size=3,
        )
        latency_ms = (time.monotonic() - started) * 1000
        after = _snapshot_tables(db_path)
        record = result.to_eval_record(latency_ms=latency_ms)

        actual_rows: List[List[Any]] = [list(row) for row in result.rows] if result.status == "success" else []
        expected_rows: List[List[Any]] = [
            [value for value in row] for row in item["expected_rows"]
        ]
        result_correct = (
            result.status == item.get("expected_status", "success")
            and rows_equal(result.rows, item["expected_rows"], tolerance=float(item.get("tolerance", 1e-4)))
        )

        records.append(
            {
                "question_id": item["id"],
                "question": item["question"],
                "trap_type": item.get("trap_type"),
                "status": record.status,
                "attempts": record.attempts,
                "first_attempt_success": record.first_attempt_success,
                "fix_success": record.fix_success,
                "total_fix_rounds": record.total_fix_rounds,
                "model_calls": record.model_calls,
                "prompt_tokens": record.prompt_tokens,
                "completion_tokens": record.completion_tokens,
                "total_tokens": record.total_tokens,
                "estimated": record.estimated,
                "latency_ms": record.latency_ms,
                "result_correct": bool(result_correct),
                "db_unchanged": before == after,
                "expected_rows": expected_rows,
                "actual_rows": actual_rows,
                "sql": result.sql,
                "attempts_log": [attempt.__dict__ for attempt in result.attempts_log],
                "answer": result.answer,
                "error": record.error,
            }
        )

    total = len(records)
    first_success = sum(1 for record in records if record["first_attempt_success"])
    fix_candidates = [record for record in records if not record["first_attempt_success"]]
    fix_success = sum(1 for record in fix_candidates if record["fix_success"])
    prompt_tokens = sum(record["prompt_tokens"] for record in records)
    completion_tokens = sum(record["completion_tokens"] for record in records)
    total_tokens = sum(record["total_tokens"] for record in records)
    model_calls = sum(record["model_calls"] for record in records)
    correct = sum(1 for record in records if record["result_correct"])

    def rate(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 0.0

    return {
        "meta": {
            "dataset": str(resolved_dataset_path.resolve()),
            "questions_total": total,
            "trap_questions": sum(1 for record in records if record["trap_type"]),
            "model": "fake-scripted",
            "pricing": {
                "input_per_mtok": INPUT_PRICE_PER_MTOK,
                "output_per_mtok": OUTPUT_PRICE_PER_MTOK,
                "currency": "CNY",
            },
        },
        "questions_total": total,
        "result_correct": correct,
        "result_correct_rate": rate(correct, total),
        "first_success": first_success,
        "first_success_rate": rate(first_success, total),
        "fix_denominator": len(fix_candidates),
        "fix_success": fix_success,
        "fix_success_rate": rate(fix_success, len(fix_candidates)),
        "avg_fix_rounds": sum(record["total_fix_rounds"] for record in records) / total if total else 0.0,
        "model_calls": model_calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "estimated_cost": (
            prompt_tokens / 1_000_000 * INPUT_PRICE_PER_MTOK
            + completion_tokens / 1_000_000 * OUTPUT_PRICE_PER_MTOK
        ),
        "records": records,
    }


__all__ = [
    "ScriptedSQLModel",
    "load_sql_dataset",
    "rows_equal",
    "run_sql_eval",
]
