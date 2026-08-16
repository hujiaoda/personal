# -*- coding: utf-8 -*-
"""一键复现 M5 全部离线评测并生成 markdown 汇总报告。

用法：
    PYTHONPATH= .venv/bin/python evals/run_all_evals.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.memory_eval import run_memory_eval  # noqa: E402
from evals.reporting import build_final_report  # noqa: E402
from evals.sql_eval import run_sql_eval  # noqa: E402

REPORT_DIR = Path(__file__).resolve().parent / "reports"
MEMORY_JSON = REPORT_DIR / "memory_eval_results.json"
SQL_JSON = REPORT_DIR / "sql_eval_results.json"
REPORT_MD = REPORT_DIR / "M5-eval.md"


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    print("== 记忆评测 ==")
    memory_results = run_memory_eval()
    MEMORY_JSON.write_text(json.dumps(memory_results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"memory JSON -> {MEMORY_JSON}")

    print("== SQL 评测 ==")
    sql_results = run_sql_eval()
    SQL_JSON.write_text(json.dumps(sql_results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"sql JSON -> {SQL_JSON}")

    print("== 汇总报告 ==")
    report = build_final_report(memory_results, sql_results, REPORT_MD)
    print(f"report -> {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
