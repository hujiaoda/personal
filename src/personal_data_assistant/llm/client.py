# -*- coding: utf-8 -*-
# 设计取舍：
# 1) ADR-1：用 httpx 手写 OpenAI 兼容请求，不引入 openai SDK。超时、重试、
#    SSE 拆包、token 计量全部在自己代码里，测试时用 MockTransport 直接注入。
# 2) ADR-2：本客户端只发 messages，不发原生 tools/tool_choice。工具调用完全走
#   prompts.py 的 JSON 动作协议，因此服务端不支持 function calling 也能工作。
# 3) 流式失败降级：SSE 通道重试耗尽或读取中断时，自动改非流式请求一次拿全量；
#    retry_sleeper 可注入，单元测试绝不真实 sleep。
# 4) token 计量：优先采用服务端 usage；服务端不给时用 UTF-8 字节数粗估 completion
#   token 并标记 estimated=True，保证成本日志永远有数可记。

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import httpx

from personal_data_assistant.config import Settings


class LLMClientError(RuntimeError):
    """所有模型客户端错误的基类；核心循环按它做结构化降级。"""


class LLMTimeoutError(LLMClientError):
    """连接或读取超时。"""


class LLMTransportError(LLMClientError):
    """底层网络传输错误（连接失败、DNS、TLS 等）。"""


class LLMHttpStatusError(LLMClientError):
    def __init__(self, message: str, status_code: int, response_body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class LLMResponseFormatError(LLMClientError):
    """HTTP 成功但响应体不是预期的 OpenAI 兼容格式。"""


class LLMStreamProtocolError(LLMClientError):
    """SSE 流内容不符合协议。"""


class LLMStreamInterruptedError(LLMClientError):
    """流已经吐出一部分内容后中断；调用方可用 partial_content 做收尾。"""

    def __init__(self, message: str, partial_content: str = "") -> None:
        super().__init__(message)
        self.partial_content = partial_content


class LLMStreamFallbackError(LLMClientError):
    """流式失败，且非流式降级也失败。"""


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False


def estimate_tokens(text: str) -> int:
    """无服务端 usage 时的粗估：UTF-8 每 4 字节算 1 token，至少 1。

    中文会低估、英文略高，但 M1 只需要成本日志“有数可记”；真实 API 正常路径
    会优先使用服务端 usage，不会走到这里。
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


def _estimated_usage(content: str) -> TokenUsage:
    tokens = estimate_tokens(content)
    return TokenUsage(
        prompt_tokens=0,
        completion_tokens=tokens,
        total_tokens=tokens,
        estimated=True,
    )


@dataclass
class LLMResponse:
    """一次模型调用的统一结果；stream=True 时在流全部消费完后由 StreamedChat.result 提供。"""

    content: str
    model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: Dict[str, Any] = field(default_factory=dict)
    streamed: bool = False
    retry_count: int = 0
    fallback_reason: Optional[str] = None


_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_VALID_ROLES = frozenset({"system", "user", "assistant"})


def _usage_from_payload(payload: Mapping[str, Any]) -> Optional[TokenUsage]:
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        return None
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    total = raw.get("total_tokens")

    def as_int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    prompt_n = as_int(prompt)
    completion_n = as_int(completion)
    total_n = as_int(total) if total is not None else prompt_n + completion_n
    return TokenUsage(
        prompt_tokens=prompt_n,
        completion_tokens=completion_n,
        total_tokens=total_n,
        estimated=False,
    )


class DeepSeekClient:
    """手写 OpenAI 兼容聊天客户端（DeepSeek 使用 /chat/completions）。"""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: Optional[httpx.Client] = None,
        retry_sleeper: Optional[Callable[[float], None]] = None,
        backoff_base: float = 0.5,
    ) -> None:
        self._settings = settings
        self._owns_http = http_client is None
        self._backoff_base = backoff_base
        self._retry_sleeper = retry_sleeper or time.sleep

        if http_client is not None:
            self._http = http_client
        else:
            timeout = httpx.Timeout(
                connect=settings.http_connect_timeout,
                read=settings.http_read_timeout,
                write=settings.http_read_timeout,
                pool=settings.http_connect_timeout,
            )
            self._http = httpx.Client(timeout=timeout)

        self._endpoint = f"{settings.base_url.rstrip('/')}/chat/completions"

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "DeepSeekClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ 公开 API

    def complete(self, messages: Sequence[Mapping[str, str]]) -> LLMResponse:
        """非流式聊天：带重试，返回完整回复与 usage。"""
        messages = self._validate_messages(messages)

        def build_request() -> httpx.Request:
            return self._build_request(messages, stream=False)

        response, retry_count = self._send_with_retries(build_request, stream=False)
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseFormatError(f"聊天响应不是合法 JSON: {exc}") from exc
        finally:
            response.close()

        return self._parse_completion(data, retry_count=retry_count)

    def stream_chat(self, messages: Sequence[Mapping[str, str]]) -> "StreamedChat":
        """SSE 流式聊天；流失败自动降级为非流式一次。

        返回对象可迭代文本增量，全部消费后通过 .result 取 LLMResponse。
        """
        messages = self._validate_messages(messages)
        return StreamedChat(self, messages)

    # ------------------------------------------------------------------ 请求构造

    def _build_request(self, messages: Sequence[Mapping[str, str]], *, stream: bool) -> httpx.Request:
        payload: Dict[str, Any] = {
            "model": self._settings.model,
            "messages": [dict(message) for message in messages],
            "stream": stream,
        }
        if stream:
            # OpenAI 兼容接口需要显式声明，才在流末尾返回 usage；DeepSeek 兼容该字段。
            payload["stream_options"] = {"include_usage": True}
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }
        return httpx.Request("POST", self._endpoint, headers=headers, json=payload)

    @staticmethod
    def _validate_messages(messages: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            raise ValueError("messages 必须是消息序列")
        if not messages:
            raise ValueError("messages 不能为空")
        normalized = []
        for message in messages:
            if not isinstance(message, Mapping):
                raise ValueError(f"消息必须是 dict，当前: {type(message).__name__}")
            role = message.get("role")
            content = message.get("content")
            if role not in _VALID_ROLES:
                raise ValueError(f"不支持的消息角色: {role!r}")
            if not isinstance(content, str):
                raise ValueError(f"消息 content 必须是字符串，role={role!r}")
            normalized.append({"role": role, "content": content})
        return normalized

    # ------------------------------------------------------------------ 重试/发送

    def _send_with_retries(
        self,
        build_request: Callable[[], httpx.Request],
        *,
        stream: bool,
    ) -> Tuple[httpx.Response, int]:
        """按配置指数退避重试瞬时错误。成功时返回打开的 response（由调用方关闭）。"""
        max_retries = self._settings.max_retries
        last_error: Optional[LLMClientError] = None

        for attempt in range(max_retries + 1):
            request = build_request()
            try:
                response = self._http.send(request, stream=stream)
            except httpx.TimeoutException as exc:
                last_error = LLMTimeoutError(f"请求超时: {exc}")
            except httpx.TransportError as exc:
                last_error = LLMTransportError(f"网络传输错误: {exc}")
            else:
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    body = self._read_error_body(response)
                    last_error = LLMHttpStatusError(
                        f"模型服务返回 HTTP {response.status_code}: {body}",
                        status_code=response.status_code,
                        response_body=body,
                    )
                elif response.status_code >= 400:
                    body = self._read_error_body(response)
                    raise LLMHttpStatusError(
                        f"模型服务返回 HTTP {response.status_code}: {body}",
                        status_code=response.status_code,
                        response_body=body,
                    )
                else:
                    return response, attempt

            if attempt >= max_retries:
                assert last_error is not None
                raise last_error
            self._sleep_for_retry(attempt, last_error)

        # 理论不可达；保留给类型检查与防御性编程
        assert last_error is not None
        raise last_error

    def _sleep_for_retry(self, attempt: int, error: Optional[LLMClientError]) -> None:
        delay = self._backoff_base * (2 ** attempt)
        self._retry_sleeper(delay)

    @staticmethod
    def _read_error_body(response: httpx.Response) -> str:
        try:
            body = response.text
        except Exception:  # noqa: BLE001 —— 错误读取失败不影响主错误
            body = ""
        finally:
            response.close()
        body = body.strip()
        return body[:500] if len(body) > 500 else body

    def _complete_single_attempt(self, messages: Sequence[Mapping[str, str]]) -> LLMResponse:
        """流式降级专用：只发一次非流式请求，不再重试，避免反复烧钱。"""
        request = self._build_request(messages, stream=False)
        response = self._http.send(request, stream=False)
        try:
            if response.status_code >= 400:
                body = self._read_error_body(response)
                raise LLMHttpStatusError(
                    f"非流式降级失败，HTTP {response.status_code}: {body}",
                    status_code=response.status_code,
                    response_body=body,
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise LLMResponseFormatError(f"非流式降级响应不是合法 JSON: {exc}") from exc
        finally:
            response.close()
        return self._parse_completion(data, retry_count=0)

    def _parse_completion(self, data: Any, *, retry_count: int) -> LLMResponse:
        if not isinstance(data, Mapping):
            raise LLMResponseFormatError(f"聊天响应顶层必须是 JSON 对象，实际: {type(data).__name__}")

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseFormatError("聊天响应缺少 choices")

        first = choices[0]
        if not isinstance(first, Mapping):
            raise LLMResponseFormatError("choices[0] 必须是对象")
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise LLMResponseFormatError("choices[0].message 必须是对象")

        content = message.get("content")
        if content is None:
            raise LLMResponseFormatError("choices[0].message.content 缺失")

        usage = _usage_from_payload(data) or _estimated_usage(str(content))
        model = str(data.get("model") or self._settings.model)
        return LLMResponse(
            content=str(content),
            model=model,
            usage=usage,
            raw=dict(data),
            streamed=False,
            retry_count=retry_count,
        )

    # ------------------------------------------------------------------ 流式生成器

    def _stream_fallback(
        self,
        messages: Sequence[Mapping[str, str]],
        retry_count: int,
        stream_error: str,
    ) -> Tuple[LLMResponse, str]:
        """流式失败后的唯一降级路径：非流式单次请求，不重试。"""
        try:
            fallback = self._complete_single_attempt(messages)
        except LLMClientError as exc:
            raise LLMStreamFallbackError(
                f"流式请求失败且非流式降级也失败。流式错误: {stream_error}；降级错误: {exc}"
            ) from exc
        reason = f"流式请求失败，已降级为非流式: {stream_error}"
        return fallback, reason

    @staticmethod
    def _set_fallback_result(
        state: Dict[str, Any],
        fallback: LLMResponse,
        retry_count: int,
        reason: str,
    ) -> None:
        state["result"] = LLMResponse(
            content=fallback.content,
            model=fallback.model,
            usage=fallback.usage,
            raw=fallback.raw,
            streamed=False,
            retry_count=retry_count,
            fallback_reason=reason,
        )

    def _iter_stream(self, messages: Sequence[Mapping[str, str]], state: Dict[str, Any]) -> Iterator[str]:
        parts: List[str] = []
        usage: Optional[TokenUsage] = None
        model = self._settings.model
        retry_count = 0

        # 第一步：打开 SSE 通道（自带配置次数的重试）。
        # 打开失败时还没给用户吐出任何字符，直接降级为非流式一次。
        try:
            response, retry_count = self._send_with_retries(
                lambda: self._build_request(messages, stream=True),
                stream=True,
            )
        except LLMClientError as exc:
            # 打开失败意味着首次 + 全部重试都已耗尽，降级结果里记录的是重试次数
            retry_count = self._settings.max_retries
            fallback, reason = self._stream_fallback(
                messages, retry_count, f"打开流式通道失败: {exc}"
            )
            self._set_fallback_result(state, fallback, retry_count, reason)
            if fallback.content:
                yield fallback.content
            return

        # 第二步：逐事件解析 SSE。读取中途失败时：
        # - 已吐出部分内容：抛 LLMStreamInterruptedError，由上层决定如何收尾，不重复输出；
        # - 一个字符都没吐出：同样降级为非流式一次。
        done = False
        try:
            for data_text in self._iter_sse_data_events(response):
                if data_text == "[DONE]":
                    done = True
                    break
                try:
                    event = json.loads(data_text)
                except ValueError as exc:
                    raise LLMStreamProtocolError(f"SSE data 不是合法 JSON: {exc}") from exc
                if not isinstance(event, Mapping):
                    raise LLMStreamProtocolError("SSE data 顶层必须是 JSON 对象")

                event_usage = _usage_from_payload(event)
                if event_usage is not None:
                    usage = event_usage
                event_model = event.get("model")
                if event_model:
                    model = str(event_model)

                choices = event.get("choices") or []
                if isinstance(choices, list) and choices:
                    first = choices[0]
                    if isinstance(first, Mapping):
                        delta = first.get("delta") or {}
                        if isinstance(delta, Mapping):
                            delta_content = delta.get("content")
                            if isinstance(delta_content, str) and delta_content:
                                parts.append(delta_content)
                                yield delta_content
        except (
            LLMClientError,
            httpx.TimeoutException,
            httpx.TransportError,
            httpx.StreamError,
        ) as exc:
            response.close()
            if parts:
                raise LLMStreamInterruptedError(
                    f"流式输出中途失败（已输出 {len(parts)} 个增量块）: {exc}",
                    partial_content="".join(parts),
                ) from exc
            fallback, reason = self._stream_fallback(
                messages, retry_count, f"读取流式内容失败: {exc}"
            )
            self._set_fallback_result(state, fallback, retry_count, reason)
            if fallback.content:
                yield fallback.content
            return
        finally:
            response.close()

        if not done and not parts:
            raise LLMStreamProtocolError("SSE 流提前结束且没有任何内容")

        content = "".join(parts)
        final_usage = usage or _estimated_usage(content)
        state["result"] = LLMResponse(
            content=content,
            model=model,
            usage=final_usage,
            raw={"streamed": True, "model": model, "usage": final_usage.__dict__},
            streamed=True,
            retry_count=retry_count,
        )

    @staticmethod
    def _iter_sse_data_events(response: httpx.Response) -> Iterator[str]:
        """把 SSE 原始行流聚合成 data 事件字符串。

        SSE 协议按空行分隔事件，多行 data: 会被拼成一个 JSON 文本。
        """
        data_lines: List[str] = []
        for raw_line in response.iter_lines():
            if raw_line is None:
                continue
            line = raw_line.strip()
            if line == "":
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines = []
                continue
            if line.startswith(":"):
                continue  # SSE 注释/keep-alive
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            yield "\n".join(data_lines)


class StreamedChat:
    """stream_chat 的返回对象：可迭代文本增量，全部消费后 .result 有最终 LLMResponse。"""

    def __init__(self, client: DeepSeekClient, messages: Sequence[Mapping[str, str]]) -> None:
        self._client = client
        self._messages = messages
        self._state: Dict[str, Any] = {}
        self._generator: Optional[Iterator[str]] = None

    def _ensure_generator(self) -> Iterator[str]:
        if self._generator is None:
            self._generator = self._client._iter_stream(self._messages, self._state)
        return self._generator

    def __iter__(self) -> Iterator[str]:
        return self._ensure_generator()

    def __next__(self) -> str:
        return next(self._ensure_generator())

    @property
    def result(self) -> Optional[LLMResponse]:
        return self._state.get("result")
