# -*- coding: utf-8 -*-
# 设计取舍：
# 1) 所有可调默认值只放在这一个 dataclass 里，loop/client/tools 一律从这里取，
#    评测和测试可以整体覆盖默认值，而不是去各个模块里翻常量。
# 2) load_settings 显式接收 env 映射（缺省读 os.environ），测试不依赖真实环境。
# 3) 配置非法直接抛 ConfigError：宁可启动时失败，也不要带着负数重试次数跑出诡异行为。

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional


class ConfigError(ValueError):
    """配置缺失或取值非法。"""


_MEMORY_STRATEGIES = frozenset({"none", "window", "window_summary", "full"})


@dataclass(frozen=True)
class Settings:
    """项目全局配置。默认值与 docs/architecture.md「降级与 B 计划」保持一致。"""

    api_key: str
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    http_connect_timeout: float = 10.0
    http_read_timeout: float = 30.0
    max_retries: int = 2
    max_tool_rounds: int = 6
    max_sql_fix_rounds: int = 3
    memory_strategy: str = "full"
    memory_window_size: int = 20
    memory_window_tokens: int = 8000
    memory_top_k: int = 8
    memory_decay_lambda: float = 0.05
    memory_db_path: str = "data/pda.db"
    sql_user_db_path: str = "data/user_tables.db"
    sql_query_timeout: float = 5.0
    sql_row_limit: int = 100
    sql_schema_sample_size: int = 3


def _require_positive_float(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是数字，当前值: {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} 必须大于 0，当前值: {value}")
    return value


def _require_non_negative_float(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是数字，当前值: {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{name} 必须 >= 0，当前值: {value}")
    return value


def _require_non_negative_int(raw: str, name: str, *, minimum: int = 0) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是整数，当前值: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} 必须 >= {minimum}，当前值: {value}")
    return value


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """从环境变量加载配置；env 为空时读 os.environ。

    需要的环境变量见 .env.example。缺少密钥、超时非正数、轮数非法都会抛 ConfigError。
    """
    values = dict(os.environ if env is None else env)

    api_key = (values.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise ConfigError("缺少 DEEPSEEK_API_KEY；请复制 .env.example 为 .env 并填入密钥")

    base_url = (values.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1").strip()
    base_url = base_url.rstrip("/")

    model = (values.get("DEEPSEEK_MODEL") or "deepseek-chat").strip()

    connect_timeout = _require_positive_float(
        values.get("PDA_HTTP_CONNECT_TIMEOUT") or "10", "PDA_HTTP_CONNECT_TIMEOUT"
    )
    read_timeout = _require_positive_float(
        values.get("PDA_HTTP_READ_TIMEOUT") or "30", "PDA_HTTP_READ_TIMEOUT"
    )
    max_retries = _require_non_negative_int(
        values.get("PDA_MAX_RETRIES") or "2", "PDA_MAX_RETRIES"
    )
    max_tool_rounds = _require_non_negative_int(
        values.get("PDA_MAX_TOOL_ROUNDS") or "6",
        "PDA_MAX_TOOL_ROUNDS",
        minimum=1,
    )
    max_sql_fix_rounds = _require_non_negative_int(
        values.get("PDA_MAX_SQL_FIX_ROUNDS") or "3",
        "PDA_MAX_SQL_FIX_ROUNDS",
        minimum=1,
    )

    memory_strategy = (values.get("PDA_MEMORY_STRATEGY") or "full").strip().lower()
    if memory_strategy not in _MEMORY_STRATEGIES:
        raise ConfigError(
            f"PDA_MEMORY_STRATEGY 必须是 {'/'.join(sorted(_MEMORY_STRATEGIES))} 之一，当前: {memory_strategy!r}"
        )
    memory_window_size = _require_non_negative_int(
        values.get("PDA_MEMORY_WINDOW_SIZE") or "20",
        "PDA_MEMORY_WINDOW_SIZE",
        minimum=1,
    )
    memory_window_tokens = _require_non_negative_int(
        values.get("PDA_MEMORY_WINDOW_TOKENS") or "8000",
        "PDA_MEMORY_WINDOW_TOKENS",
        minimum=1,
    )
    memory_top_k = _require_non_negative_int(
        values.get("PDA_MEMORY_TOP_K") or "8",
        "PDA_MEMORY_TOP_K",
        minimum=1,
    )
    memory_decay_lambda = _require_non_negative_float(
        values.get("PDA_MEMORY_DECAY_LAMBDA") or "0.05",
        "PDA_MEMORY_DECAY_LAMBDA",
    )
    memory_db_path = (values.get("PDA_MEMORY_DB_PATH") or "data/pda.db").strip()

    sql_user_db_path = (values.get("PDA_SQL_USER_DB_PATH") or "data/user_tables.db").strip()
    if not sql_user_db_path:
        raise ConfigError("PDA_SQL_USER_DB_PATH 不能为空")
    sql_query_timeout = _require_positive_float(
        values.get("PDA_SQL_QUERY_TIMEOUT") or "5",
        "PDA_SQL_QUERY_TIMEOUT",
    )
    sql_row_limit = _require_non_negative_int(
        values.get("PDA_SQL_ROW_LIMIT") or "100",
        "PDA_SQL_ROW_LIMIT",
        minimum=1,
    )
    sql_schema_sample_size = _require_non_negative_int(
        values.get("PDA_SQL_SCHEMA_SAMPLE_SIZE") or "3",
        "PDA_SQL_SCHEMA_SAMPLE_SIZE",
        minimum=1,
    )

    return Settings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        http_connect_timeout=connect_timeout,
        http_read_timeout=read_timeout,
        max_retries=max_retries,
        max_tool_rounds=max_tool_rounds,
        max_sql_fix_rounds=max_sql_fix_rounds,
        memory_strategy=memory_strategy,
        memory_window_size=memory_window_size,
        memory_window_tokens=memory_window_tokens,
        memory_top_k=memory_top_k,
        memory_decay_lambda=memory_decay_lambda,
        memory_db_path=memory_db_path,
        sql_user_db_path=sql_user_db_path,
        sql_query_timeout=sql_query_timeout,
        sql_row_limit=sql_row_limit,
        sql_schema_sample_size=sql_schema_sample_size,
    )
