# -*- coding: utf-8 -*-
"""命令行入口：跑 SQL 问数评测并打印指标。

用法：
    PYTHONPATH= .venv/bin/python evals/run_sql_eval.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.sql_eval import run_sql_eval  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="SQL 问数评测")
    parser.add_argument("--dataset", default=None, help="题集 JSON 路径，默认 evals/questions/sql_questions.json")
    parser.add_argument("--out", default=None, help="结果 JSON 输出路径")
    parser.add_argument("--work-dir", default=None, help="临时 SQLite 工作目录（默认临时目录）")
    args = parser.parse_args()

    results = run_sql_eval(dataset_path=args.dataset, work_dir=args.work_dir)

    print(f"题数: {results['questions_total']}")
    print(f"结果一致率: {results['result_correct_rate'] * 100:.2f}%")
    print(f"首次成功率: {results['first_success_rate'] * 100:.2f}%")
    print(f"修正成功率: {results['fix_success_rate'] * 100:.2f}% "
          f"({results['fix_success']}/{results['fix_denominator']})")
    print(f"平均修正轮数: {results['avg_fix_rounds']:.3f}")
    print(f"总 token: {results['total_tokens']:,}，估算费用: ¥{results['estimated_cost']:.4f}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
