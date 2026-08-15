# -*- coding: utf-8 -*-
# 真实 DeepSeek 调用测试：整文件标记 integration，默认被 pyproject 的
# `-m "not integration"` 跳过；显式 `-m integration` 且配置了密钥才跑。
# 目的只是冒烟验证协议可用，不做业务断言，避免烧钱与网络抖动造成假红。

import os

import pytest

from personal_data_assistant.config import load_settings
from personal_data_assistant.llm.client import DeepSeekClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DEEPSEEK_API_KEY"),
        reason="未配置 DEEPSEEK_API_KEY，跳过真实 API 冒烟测试",
    ),
]


@pytest.fixture(scope="module")
def client():
    settings = load_settings()
    return DeepSeekClient(settings)


def test_real_complete_smoke(client):
    response = client.complete(
        [{"role": "user", "content": "只回复四个字：集成通过"}]
    )
    assert response.content
    assert response.usage.total_tokens > 0


def test_real_stream_smoke(client):
    streamed = client.stream_chat(
        [{"role": "user", "content": "只回复四个字：流式通过"}]
    )
    chunks = list(streamed)
    assert "".join(chunks)
    assert streamed.result is not None
