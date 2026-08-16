# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) config 是 M1 所有默认值的唯一来源，测试必须锁死架构文档里的默认值，
#    避免默认值散落到 loop/client 里以后改不动。
# 2) load_settings 显式接收 env 映射，不碰真实环境，测试可重复。
# 3) 只断言行为和默认值，不锁死报错文案细节（文案会打磨）。

import pytest

from personal_data_assistant.config import ConfigError, Settings, load_settings


def test_defaults_match_architecture_document():
    settings = load_settings({"DEEPSEEK_API_KEY": "test-key"})

    assert settings.api_key == "test-key"
    assert settings.base_url == "https://api.deepseek.com/v1"
    assert settings.model == "deepseek-chat"
    assert settings.http_connect_timeout == 10.0
    assert settings.http_read_timeout == 30.0
    assert settings.max_retries == 2
    assert settings.max_tool_rounds == 6
    assert settings.max_sql_fix_rounds == 3
    assert settings.memory_strategy == "full"
    assert settings.memory_window_size == 20
    assert settings.memory_window_tokens == 8000
    assert settings.memory_top_k == 8
    assert settings.memory_decay_lambda == 0.05
    assert settings.memory_db_path == "data/pda.db"


def test_environment_variables_override_defaults():
    settings = load_settings(
        {
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_BASE_URL": "https://example.test/v1/",
            "DEEPSEEK_MODEL": "deepseek-chat-test",
            "PDA_HTTP_CONNECT_TIMEOUT": "2.5",
            "PDA_HTTP_READ_TIMEOUT": "8",
            "PDA_MAX_RETRIES": "3",
            "PDA_MAX_TOOL_ROUNDS": "4",
            "PDA_MAX_SQL_FIX_ROUNDS": "5",
            "PDA_MEMORY_STRATEGY": "window",
            "PDA_MEMORY_WINDOW_SIZE": "7",
            "PDA_MEMORY_WINDOW_TOKENS": "900",
            "PDA_MEMORY_TOP_K": "5",
            "PDA_MEMORY_DECAY_LAMBDA": "0.25",
            "PDA_MEMORY_DB_PATH": "data/test-memory.db",
        }
    )

    assert settings.base_url == "https://example.test/v1"
    assert settings.model == "deepseek-chat-test"
    assert settings.http_connect_timeout == 2.5
    assert settings.http_read_timeout == 8.0
    assert settings.max_retries == 3
    assert settings.max_tool_rounds == 4
    assert settings.max_sql_fix_rounds == 5
    assert settings.memory_strategy == "window"
    assert settings.memory_window_size == 7
    assert settings.memory_window_tokens == 900
    assert settings.memory_top_k == 5
    assert settings.memory_decay_lambda == 0.25
    assert settings.memory_db_path == "data/test-memory.db"


def test_missing_api_key_raises_config_error():
    with pytest.raises(ConfigError):
        load_settings({"DEEPSEEK_API_KEY": ""})


def test_invalid_timeout_raises_config_error():
    for value in ["-1", "0", "abc", "1,5"]:
        with pytest.raises(ConfigError):
            load_settings(
                {"DEEPSEEK_API_KEY": "k", "PDA_HTTP_CONNECT_TIMEOUT": value}
            )


def test_invalid_retry_and_round_counts_raise_config_error():
    for key in ["PDA_MAX_RETRIES", "PDA_MAX_TOOL_ROUNDS", "PDA_MAX_SQL_FIX_ROUNDS"]:
        with pytest.raises(ConfigError):
            load_settings({"DEEPSEEK_API_KEY": "k", key: "-1"})

    with pytest.raises(ConfigError):
        load_settings({"DEEPSEEK_API_KEY": "k", "PDA_MAX_TOOL_ROUNDS": "0"})
    with pytest.raises(ConfigError):
        load_settings({"DEEPSEEK_API_KEY": "k", "PDA_MAX_SQL_FIX_ROUNDS": "0"})


def test_invalid_memory_strategy_raises_config_error():
    with pytest.raises(ConfigError):
        load_settings({"DEEPSEEK_API_KEY": "k", "PDA_MEMORY_STRATEGY": "fancy"})


def test_invalid_memory_window_topk_and_decay_raise_config_error():
    with pytest.raises(ConfigError):
        load_settings({"DEEPSEEK_API_KEY": "k", "PDA_MEMORY_WINDOW_SIZE": "0"})
    with pytest.raises(ConfigError):
        load_settings({"DEEPSEEK_API_KEY": "k", "PDA_MEMORY_WINDOW_TOKENS": "0"})
    with pytest.raises(ConfigError):
        load_settings({"DEEPSEEK_API_KEY": "k", "PDA_MEMORY_TOP_K": "0"})
    with pytest.raises(ConfigError):
        load_settings({"DEEPSEEK_API_KEY": "k", "PDA_MEMORY_DECAY_LAMBDA": "-1"})


def test_settings_are_immutable_frozen_dataclass():
    settings = Settings(api_key="k")
    with pytest.raises(Exception):
        settings.max_tool_rounds = 1
