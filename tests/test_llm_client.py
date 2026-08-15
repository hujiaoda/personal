# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) 全部走 httpx.MockTransport，网络行为可脚本化；retry_sleeper 注入空函数，
#    单元测试绝不真实 sleep，也绝不真实花钱。
# 2) 锁死 ADR-1/ADR-2：HTTP 手写、请求体不出现原生 tools/tool_choice 字段，
#    工具调用只走提示词 JSON 动作协议。
# 3) 流式测试覆盖 SSE 拆包、usage 收尾块、流失败降级非流式三条路径。

import json

import httpx
import pytest

from personal_data_assistant.config import Settings
from personal_data_assistant.llm.client import (
    LLMClientError,
    LLMHttpStatusError,
    LLMResponseFormatError,
    LLMStreamFallbackError,
    DeepSeekClient,
)

BASE_URL = "https://api.test/v1"


def make_settings(**overrides):
    values = {
        "api_key": "test-key",
        "base_url": BASE_URL,
        "http_connect_timeout": 1.0,
        "http_read_timeout": 2.0,
    }
    values.update(overrides)
    return Settings(**values)


def make_client(handler, settings=None, sleeper=None):
    delays = []
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)

    def record_and_sleep(delay):
        delays.append(delay)
        if sleeper is not None:
            sleeper(delay)

    client = DeepSeekClient(
        settings or make_settings(),
        http_client=http,
        retry_sleeper=record_and_sleep,
    )
    return client, delays


def request_payload(request):
    return json.loads(request.content.decode("utf-8"))


_DEFAULT_USAGE = {
    "prompt_tokens": 11,
    "completion_tokens": 7,
    "total_tokens": 18,
}


def ok_response(content="你好", usage=_DEFAULT_USAGE):
    body = {
        "id": "chatcmpl-test",
        "model": "deepseek-chat",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body, request=httpx.Request("POST", BASE_URL))


def test_complete_parses_content_usage_and_sends_expected_payload():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["payload"] = request_payload(request)
        return ok_response("你好，世界")

    client, _ = make_client(handler)
    response = client.complete([{"role": "user", "content": "你好"}])

    assert response.content == "你好，世界"
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 7
    assert response.usage.total_tokens == 18
    assert response.streamed is False
    assert response.retry_count == 0

    assert seen["url"].endswith("/chat/completions")
    assert seen["auth"] == "Bearer test-key"
    payload = seen["payload"]
    assert payload["model"] == "deepseek-chat"
    assert payload["stream"] is False
    assert payload["messages"] == [{"role": "user", "content": "你好"}]
    # ADR-2：不允许偷偷改回原生 function calling
    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_complete_retries_transient_429_with_exponential_backoff():
    attempts = {"count": 0}

    def handler(request):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            return httpx.Response(
                429,
                json={"error": {"message": "rate limited"}},
                request=request,
            )
        return ok_response("重试成功")

    client, delays = make_client(handler, settings=make_settings(max_retries=2))
    response = client.complete([{"role": "user", "content": "hi"}])

    assert attempts["count"] == 3
    assert response.retry_count == 2
    assert response.content == "重试成功"
    assert delays == [0.5, 1.0]


def test_complete_retries_transport_error_then_succeeds():
    attempts = {"count": 0}

    def handler(request):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return ok_response("网络恢复")

    client, delays = make_client(handler, settings=make_settings(max_retries=1))
    response = client.complete([{"role": "user", "content": "hi"}])

    assert response.content == "网络恢复"
    assert response.retry_count == 1
    assert delays == [0.5]


def test_complete_raises_after_retries_exhausted():
    attempts = {"count": 0}

    def handler(request):
        attempts["count"] += 1
        return httpx.Response(500, json={"error": {"message": "bad"}}, request=request)

    client, _ = make_client(handler, settings=make_settings(max_retries=2))
    with pytest.raises(LLMHttpStatusError):
        client.complete([{"role": "user", "content": "hi"}])
    assert attempts["count"] == 3


def test_complete_does_not_retry_client_errors():
    attempts = {"count": 0}

    def handler(request):
        attempts["count"] += 1
        return httpx.Response(
            401,
            json={"error": {"message": "invalid api key"}},
            request=request,
        )

    client, _ = make_client(handler, settings=make_settings(max_retries=2))
    with pytest.raises(LLMHttpStatusError):
        client.complete([{"role": "user", "content": "hi"}])
    assert attempts["count"] == 1


def test_complete_rejects_malformed_success_body_without_retry():
    attempts = {"count": 0}

    def handler(request):
        attempts["count"] += 1
        return httpx.Response(200, text="not-json", request=request)

    client, _ = make_client(handler, settings=make_settings(max_retries=2))
    with pytest.raises(LLMResponseFormatError):
        client.complete([{"role": "user", "content": "hi"}])
    assert attempts["count"] == 1


def test_complete_estimates_usage_when_provider_omits_usage():
    def handler(request):
        return ok_response("这是一段没有 usage 的回复", usage=None)

    client, _ = make_client(handler)
    response = client.complete([{"role": "user", "content": "hi"}])

    assert response.usage.estimated is True
    assert response.usage.completion_tokens > 0
    assert response.usage.total_tokens == response.usage.completion_tokens


def _sse_bytes(events, include_usage=True):
    body = b""
    for event in events:
        body += b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n"
    if include_usage:
        body += (
            b'data: {"choices":[],"usage":{"prompt_tokens":5,'
            b'"completion_tokens":2,"total_tokens":7}}\n\n'
        )
    body += b"data: [DONE]\n\n"
    return body


def test_stream_chat_parses_sse_deltas_and_final_usage_chunk():
    events = [
        {"choices": [{"delta": {"content": "你"}}]},
        {"choices": [{"delta": {"content": "好"}}]},
    ]

    def handler(request):
        assert request_payload(request)["stream"] is True
        # 要求服务端在流末尾返回 usage，保证 token 计量不断链
        assert request_payload(request)["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200,
            content=_sse_bytes(events),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    client, _ = make_client(handler)
    streamed = client.stream_chat([{"role": "user", "content": "hi"}])

    assert list(streamed) == ["你", "好"]
    assert streamed.result.content == "你好"
    assert streamed.result.streamed is True
    assert streamed.result.usage.total_tokens == 7
    assert streamed.result.retry_count == 0


def test_stream_chat_estimates_usage_when_provider_sends_no_usage():
    def handler(request):
        return httpx.Response(
            200,
            content=_sse_bytes(
                [{"choices": [{"delta": {"content": "流式没有 usage"}}]}],
                include_usage=False,
            ),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    client, _ = make_client(handler)
    streamed = client.stream_chat([{"role": "user", "content": "hi"}])
    list(streamed)

    assert streamed.result.content == "流式没有 usage"
    assert streamed.result.usage.estimated is True
    assert streamed.result.usage.completion_tokens > 0


def test_stream_failure_falls_back_to_non_stream_once():
    attempts = {"stream": 0, "complete": 0}

    def handler(request):
        payload = request_payload(request)
        if payload["stream"] is True:
            attempts["stream"] += 1
            return httpx.Response(
                500, json={"error": {"message": "stream down"}}, request=request
            )
        attempts["complete"] += 1
        return ok_response("完整答案")

    client, _ = make_client(handler, settings=make_settings(max_retries=1))
    streamed = client.stream_chat([{"role": "user", "content": "hi"}])

    assert list(streamed) == ["完整答案"]
    assert streamed.result.content == "完整答案"
    assert streamed.result.streamed is False
    assert streamed.result.retry_count == 1  # 流式首次失败 + 1 次重试，共 2 次尝试
    assert streamed.result.fallback_reason is not None
    assert "降级" in streamed.result.fallback_reason
    assert "非流式" in streamed.result.fallback_reason
    assert attempts["stream"] == 2  # 首次 + 重试 1 次
    assert attempts["complete"] == 1  # 降级只试一次，不重复烧钱


def test_stream_failure_and_non_stream_fallback_also_fails_raises():
    attempts = {"stream": 0, "complete": 0}

    def handler(request):
        payload = request_payload(request)
        if payload["stream"] is True:
            attempts["stream"] += 1
        else:
            attempts["complete"] += 1
        return httpx.Response(
            500, json={"error": {"message": "all down"}}, request=request
        )

    client, _ = make_client(handler, settings=make_settings(max_retries=1))
    streamed = client.stream_chat([{"role": "user", "content": "hi"}])

    with pytest.raises(LLMStreamFallbackError):
        list(streamed)
    assert attempts["stream"] == 2  # 流式：首次 + 重试 1 次
    assert attempts["complete"] == 1  # 非流式降级严格只试一次


def test_client_errors_subclass_llm_client_error():
    assert issubclass(LLMHttpStatusError, LLMClientError)
    assert issubclass(LLMResponseFormatError, LLMClientError)
    assert issubclass(LLMStreamFallbackError, LLMClientError)
