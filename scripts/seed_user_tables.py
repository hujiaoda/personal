# -*- coding: utf-8 -*-
"""重新生成 data/user_tables.db 演示数据（删除旧库，确定性重建）。

用法（项目根目录）：
    PYTHONPATH= .venv/bin/python scripts/seed_user_tables.py
"""

from pathlib import Path

from personal_data_assistant.data.demo import build_demo_tables

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "user_tables.db"

if __name__ == "__main__":
    DB_PATH.unlink(missing_ok=True)
    build_demo_tables(DB_PATH)
    print(f"已生成演示数据：{DB_PATH}")
