# -*- coding: utf-8 -*-
"""命令行入口：跑记忆四策略评测并打印对比表。

用法：
    PYTHONPATH= .venv/bin/python evals/run_memory_eval.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.memory_eval import run_memory_eval  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="记忆四策略评测")
    parser.add_argument("--dataset", default=None, help="题集 JSON 路径，默认 evals/questions/memory_50.json")
    parser.add_argument("--out", default=None, help="结果 JSON 输出路径")
    parser.add_argument("--db-dir", default=None, help="重放 SQLite 目录（默认临时目录）")
    args = parser.parse_args()

    results = run_memory_eval(dataset_path=args.dataset, db_dir=args.db_dir)

    print(f"题数: {results['questions_total']}")
    print(f"{'策略':<16}{'命中':>4}{'命中率':>10}{'上下文tok':>12}{'总tok':>10}{'估算费用':>12}")
    for name, bucket in results["strategies"].items():
        print(
            f"{name:<16}{bucket['hits']:>4}{bucket['hit_rate'] * 100:>9.2f}%"
            f"{bucket['context_tokens']:>12,}{bucket['total_tokens']:>10,}"
            f"{bucket['estimated_cost']:>11.4f}¥"
        )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
