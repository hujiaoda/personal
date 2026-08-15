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


def _require_positive_float(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是数字，当前值: {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} 必须大于 0，当前值: {value}")
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

    return Settings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        http_connect_timeout=connect_timeout,
        http_read_timeout=read_timeout,
        max_retries=max_retries,
        max_tool_rounds=max_tool_rounds,
        max_sql_fix_rounds=max_sql_fix_rounds,
    )
