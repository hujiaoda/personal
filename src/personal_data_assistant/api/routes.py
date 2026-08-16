# -*- coding: utf-8 -*-
# 设计取舍：
# 1) 路由只做薄壳：校验请求 → 按 timeout 包一层线程级超时 → 调 app / 问数服务 →
#    序列化；SQL、记忆、工具调用等业务逻辑一律不落在这里。
# 2) 错误统一成 {"error": {"code", "message", "detail?"}}；校验、404、模型不可用、
#    超时、未知异常全部走同一形状，前端只认这一个结构。
# 3) 流式选 SSE（POST + fetch 读流，前端不用 EventSource，因为 EventSource 不能
#    带 POST body）。/ask 的 on_chunk 由核心循环驱动；/ask_sql 是确定性问数
#    子流程，先拿到 AskResult 再按块吐字，保证 SQL 路径一定查库。
# 4) /ask 流式时加一个增量过滤器：把 {"action":"tool",...} 原样折进 tool 事件，
#    只把 final.answer 的正文按块发给页面，呼应架构里的“工具调用折叠展示”。

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from fastapi import APIRouter, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

_MAX_QUESTION_CHARS = 4000
_DEFAULT_TIMEOUT_SECONDS = 60.0
_MAX_TIMEOUT_SECONDS = 300.0


class APIError(Exception):
    """带 HTTP 状态码的业务错误；全局 handler 把它转成统一错误结构。"""

    def __init__(self, status_code: int, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=_MAX_QUESTION_CHARS)
    stream: bool = False
    timeout: float = Field(default=_DEFAULT_TIMEOUT_SECONDS, gt=0.0, le=_MAX_TIMEOUT_SECONDS)

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空白")
        return value


class AskSqlRequest(AskRequest):
    """/ask_sql 与 /ask 使用同一请求形状，前端两个入口无需两套表单逻辑。"""


def error_body(code: str, message: str, detail: Any = None) -> dict:
    body = {"error": {"code": code, "message": message}}
    if detail is not None:
        body["error"]["detail"] = detail
    return body


def install_error_handlers(app: FastAPI) -> None:
    """把 FastAPI 默认的各类错误全部收口成统一中文错误结构。"""

    @app.exception_handler(APIError)
    async def handle_api_error(request: Any, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Any, exc: RequestValidationError) -> JSONResponse:
        parts: List[str] = []
        for item in exc.errors()[:5]:
            loc = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
            msg = str(item.get("msg", "参数不合法"))
            parts.append(f"{loc}: {msg}" if loc else msg)
        detail = "；".join(parts)
        return JSONResponse(
            status_code=422,
            content=error_body("validation_error", f"请求参数不合法：{detail}", detail),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Any, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        message = "接口不存在" if exc.status_code == 404 else f"HTTP 请求失败：{exc.detail}"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, message, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(request: Any, exc: Exception) -> JSONResponse:
        logger.exception("未捕获的接口异常")
        return JSONResponse(
            status_code=500,
            content=error_body("internal_error", "服务内部错误，请稍后重试；如果问题持续出现，请查看服务端日志。"),
        )


def _run_with_timeout(func: Callable[[], Any], timeout: float) -> Any:
    """在守护线程里执行同步业务，主线程按 deadline 等待；超时只中断 HTTP 等待。

    底层模型客户端还有自己的连接/读取超时与重试，因此这里的守护线程最迟会被
    那些超时自然回收，不会永久悬挂；测试里的 fake 由测试自己负责放行。
    """
    result_queue: queue.Queue = queue.Queue(maxsize=1)

    def runner() -> None:
        try:
            result_queue.put(("ok", func()))
        except BaseException as exc:  # noqa: BLE001 —— 边界收口，异常原样交给路由/handler
            result_queue.put(("error", exc))

    thread = threading.Thread(target=runner, name="pda-http-worker", daemon=True)
    thread.start()
    try:
        kind, value = result_queue.get(timeout=timeout)
    except queue.Empty as exc:
        raise APIError(
            504,
            "timeout",
            f"处理超时（超过 {timeout:g} 秒），已中断本次请求。请缩小问题范围或调大 timeout 后重试。",
        ) from exc

    if kind == "error":
        raise value  # type: ignore[misc]
    return value


def _sse(event: str, data: Dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def _usage_dict(usage: Any) -> dict:
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        "estimated": bool(getattr(usage, "estimated", False)),
    }


def _ask_response_body(result: Any, question: str) -> dict:
    return {
        "question": question,
        "answer": result.answer,
        "status": result.status,
        "streamed": False,
        "rounds": int(getattr(result, "rounds", 0) or 0),
        "tool_rounds": int(getattr(result, "tool_rounds", 0) or 0),
        "usage": _usage_dict(getattr(result, "total_usage", None)),
        "error": getattr(result, "error", None),
    }


def _ask_sql_response_body(outcome: Any, *, streamed: bool) -> dict:
    result = outcome.result
    usage = getattr(result, "usage", None)
    return {
        "question": outcome.question,
        "rewritten_question": outcome.rewritten,
        "answer": result.answer,
        "status": result.status,
        "sql": result.sql,
        "columns": list(getattr(result, "columns", ())),
        "rows": [list(row) for row in getattr(result, "rows", ())],
        "row_count": int(getattr(result, "row_count", 0) or 0),
        "truncated": bool(getattr(result, "truncated", False)),
        "streamed": streamed,
        "alias_applied": [list(pair) for pair in getattr(outcome, "aliases", ())],
        "attempts": int(getattr(result, "attempts", 0) or 0),
        "usage": {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            "estimated": bool(getattr(usage, "estimated", False)),
        },
        "error": getattr(result, "error", None),
    }


# ---------------------------------------------------------------------------
# 增量流过滤器：把核心循环 on_chunk 的原始文本分成「用户答案」与「工具调用 JSON」。

_TOOL_ACTION_RE = re.compile(r'\{\s*"action"\s*:\s*"tool"')
_FINAL_ACTION_RE = re.compile(r'\{\s*"action"\s*:\s*"final"\s*,\s*"answer"\s*:\s*"')

_JSON_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    '"': '"',
    "\\": "\\",
    "/": "/",
}


def _find_complete_json_end(text: str) -> Optional[int]:
    """返回第一个完整 JSON 对象的右花括号下标；不完整或未开始返回 None。"""
    in_string = False
    escape = False
    depth = 0
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


class StreamActionFilter:
    """流式状态机：prefix → tool / final；tool 原样转发，final 只转发 answer 正文。"""

    def __init__(self) -> None:
        self._buffer = ""
        self._mode = "prefix"
        self._raw_pos = 0
        self._tool_emitted = 0
        self._tool_parts: List[str] = []
        self._decoded: List[str] = []
        self.completed_tool_calls: List[dict] = []

    def feed(self, text: str) -> Iterator[Dict[str, Any]]:
        if not text:
            return
        self._buffer += text
        while True:
            if self._mode == "prefix":
                tool_match = _TOOL_ACTION_RE.search(self._buffer)
                final_match = _FINAL_ACTION_RE.search(self._buffer)
                if tool_match is None and final_match is None:
                    # 还没看到协议前缀。只保留尾巴，避免模型前摇文本无限占内存。
                    if len(self._buffer) > 512:
                        self._buffer = self._buffer[-512:]
                    return
                if final_match is None or (
                    tool_match is not None and tool_match.start() < final_match.start()
                ):
                    self._buffer = self._buffer[tool_match.start() :]
                    self._mode = "tool"
                    self._tool_emitted = 0
                    self._tool_parts = []
                    continue
                answer_start = final_match.end()
                self._buffer = self._buffer[answer_start:]
                self._mode = "final"
                self._raw_pos = 0
                self._decoded = []
                continue

            if self._mode == "tool":
                end = _find_complete_json_end(self._buffer)
                if end is None:
                    if len(self._buffer) > self._tool_emitted:
                        delta = self._buffer[self._tool_emitted :]
                        self._tool_emitted = len(self._buffer)
                        self._tool_parts.append(delta)
                        yield {"type": "tool", "delta": delta}
                    return
                if end + 1 > self._tool_emitted:
                    delta = self._buffer[self._tool_emitted : end + 1]
                    self._tool_emitted = end + 1
                    self._tool_parts.append(delta)
                    yield {"type": "tool", "delta": delta}
                raw = self._buffer[: end + 1]
                try:
                    self.completed_tool_calls.append(json.loads(raw))
                except ValueError:
                    self.completed_tool_calls.append({"raw": raw})
                self._buffer = self._buffer[end + 1 :]
                self._mode = "prefix"
                continue

            if self._mode == "final":
                index = self._raw_pos
                while index < len(self._buffer):
                    char = self._buffer[index]
                    if char == '"':
                        if self._decoded:
                            yield {"type": "chunk", "delta": "".join(self._decoded)}
                            self._decoded = []
                        # answer 已完成；后面的 "}" 等协议尾巴不再有意义，直接丢弃，
                        # 避免 flush 把它误报成 tool 事件。
                        self._buffer = ""
                        self._mode = "prefix"
                        self._raw_pos = 0
                        return
                    if char == "\\":
                        decoded, next_index = self._decode_escape(index)
                        if decoded is None:
                            # 转义只来了一半：先把已确认的正文放行，再等下一块。
                            if self._decoded:
                                yield {"type": "chunk", "delta": "".join(self._decoded)}
                                self._decoded = []
                            self._raw_pos = index
                            return
                        self._decoded.append(decoded)
                        index = next_index + 1
                        continue
                    self._decoded.append(char)
                    index += 1
                if self._decoded:
                    yield {"type": "chunk", "delta": "".join(self._decoded)}
                    self._decoded = []
                self._raw_pos = index
                return

    def _decode_escape(self, slash_index: int) -> Tuple[Optional[str], int]:
        """解析一个 JSON 转义；数据不完整时返回 (None, 当前反斜杠下标)。"""
        if slash_index + 1 >= len(self._buffer):
            return None, slash_index
        kind = self._buffer[slash_index + 1]
        if kind == "u":
            if slash_index + 5 >= len(self._buffer):
                return None, slash_index
            hex_text = self._buffer[slash_index + 2 : slash_index + 6]
            try:
                unit = int(hex_text, 16)
            except ValueError:
                return "\ufffd", slash_index + 5
            # 代理对跨块到达时，先等 low surrogate 一起到，避免拆坏 emoji。
            if 0xD800 <= unit <= 0xDBFF:
                low_start = slash_index + 6
                if (
                    low_start + 6 <= len(self._buffer)
                    and self._buffer[low_start : low_start + 2] == "\\u"
                ):
                    try:
                        low = int(self._buffer[low_start + 2 : low_start + 6], 16)
                    except ValueError:
                        return "\ufffd", low_start + 5
                    if 0xDC00 <= low <= 0xDFFF:
                        combined = 0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)
                        return chr(combined), low_start + 5
                return None, slash_index
            return chr(unit), slash_index + 5
        escaped = _JSON_ESCAPES.get(kind)
        if escaped is None:
            return kind, slash_index + 1
        return escaped, slash_index + 1

    def flush(self) -> Iterator[Dict[str, Any]]:
        """流结束前把残余内容放行；正常流程里通常没有残余。"""
        if self._mode == "final":
            if self._decoded:
                yield {"type": "chunk", "delta": "".join(self._decoded)}
                self._decoded = []
        elif self._mode == "tool":
            if len(self._buffer) > self._tool_emitted:
                delta = self._buffer[self._tool_emitted :]
                self._tool_emitted = len(self._buffer)
                self._tool_parts.append(delta)
                yield {"type": "tool", "delta": delta}
        elif self._mode == "prefix" and self._buffer.strip():
            # 未识别协议的前摇按工具过程折叠，避免页面突然冒出 JSON/围栏。
            yield {"type": "tool", "delta": self._buffer}
        self._buffer = ""


# ---------------------------------------------------------------------------
# SSE 生成器


def _ask_stream_events(assistant: Any, question: str, timeout: float) -> Iterator[str]:
    yield _sse("status", {"type": "status", "message": "正在思考…"})

    chunk_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    classifier = StreamActionFilter()

    def on_chunk(text: str) -> None:
        if stop_event.is_set():
            return
        chunk_queue.put(("chunk", str(text)))

    def worker() -> None:
        try:
            result = assistant.ask(question, stream=True, on_chunk=on_chunk)
            chunk_queue.put(("done", result))
        except Exception as exc:  # noqa: BLE001 —— 边界收口，转成 SSE error 事件
            chunk_queue.put(("error", exc))

    thread = threading.Thread(target=worker, name="pda-ask-stream", daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop_event.set()
            yield _sse(
                "error",
                {
                    "type": "error",
                    "code": "timeout",
                    "message": f"处理超时（超过 {timeout:g} 秒），已中断本次回答。请缩小问题范围或调大 timeout 后重试。",
                },
            )
            return
        try:
            kind, value = chunk_queue.get(timeout=remaining)
        except queue.Empty:
            stop_event.set()
            yield _sse(
                "error",
                {
                    "type": "error",
                    "code": "timeout",
                    "message": f"处理超时（超过 {timeout:g} 秒），已中断本次回答。请缩小问题范围或调大 timeout 后重试。",
                },
            )
            return

        if kind == "chunk":
            for event in classifier.feed(value):
                yield _sse(event["type"], event)
            continue

        for event in classifier.flush():
            yield _sse(event["type"], event)

        if kind == "error":
            yield _sse(
                "error",
                {"type": "error", "code": "internal_error", "message": f"回答过程出错：{value}"},
            )
            return

        result = value
        if getattr(result, "status", "") == "model_error":
            yield _sse(
                "error",
                {
                    "type": "error",
                    "code": "model_unavailable",
                    "message": getattr(result, "answer", "") or "模型暂不可用，请稍后重试。",
                    "detail": getattr(result, "error", None),
                },
            )
            return

        done_payload: Dict[str, Any] = {
            "type": "done",
            "status": result.status,
            "answer": result.answer,
            "rounds": int(getattr(result, "rounds", 0) or 0),
            "tool_rounds": int(getattr(result, "tool_rounds", 0) or 0),
            "usage": _usage_dict(getattr(result, "total_usage", None)),
        }
        if classifier.completed_tool_calls:
            done_payload["tool_calls"] = classifier.completed_tool_calls
        if getattr(result, "error", None):
            done_payload["error"] = result.error
        yield _sse("done", done_payload)
        return


def _chunk_text(text: str, size: int = 24) -> Iterator[str]:
    for index in range(0, len(text), size):
        yield text[index : index + size]


def _ask_sql_stream_events(sql_ask: Callable[[str], Any], question: str, timeout: float) -> Iterator[str]:
    yield _sse("status", {"type": "status", "message": "正在查数…"})

    try:
        outcome = _run_with_timeout(lambda: sql_ask(question), timeout)
    except APIError as exc:
        yield _sse(
            "error",
            {
                "type": "error",
                "code": exc.code,
                "message": exc.message,
                "detail": exc.detail,
            },
        )
        return
    except Exception as exc:  # noqa: BLE001 —— 路由边界收口，统一错误结构
        yield _sse(
            "error",
            {"type": "error", "code": "internal_error", "message": f"问数过程出错：{exc}"},
        )
        return

    result = outcome.result
    if result.status == "model_error":
        yield _sse(
            "error",
            {
                "type": "error",
                "code": "model_unavailable",
                "message": f"问数模型暂不可用：{result.answer or result.error or '请稍后重试。'}",
                "detail": getattr(result, "error", None),
            },
        )
        return

    answer = result.answer or ""
    for delta in _chunk_text(answer):
        yield _sse("chunk", {"type": "chunk", "delta": delta})

    done_payload = {
        "type": "done",
        "status": result.status,
        "answer": answer,
        "sql": result.sql,
        "columns": list(getattr(result, "columns", ())),
        "rows": [list(row) for row in getattr(result, "rows", ())],
        "row_count": int(getattr(result, "row_count", 0) or 0),
        "truncated": bool(getattr(result, "truncated", False)),
        "alias_applied": [list(pair) for pair in getattr(outcome, "aliases", ())],
        "attempts": int(getattr(result, "attempts", 0) or 0),
        "usage": {
            "prompt_tokens": int(getattr(result.usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(result.usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(result.usage, "total_tokens", 0) or 0),
            "estimated": bool(getattr(result.usage, "estimated", False)),
        },
    }
    if getattr(result, "error", None):
        done_payload["error"] = result.error
    yield _sse("done", done_payload)


# ---------------------------------------------------------------------------
# 路由


def build_router(
    *,
    assistant: Any,
    sql_ask: Callable[[str], Any],
    health_checker: Any,
) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict:
        return health_checker.snapshot()

    @router.post("/ask")
    def ask(payload: AskRequest) -> Any:
        question = payload.question.strip()
        if payload.stream:
            return StreamingResponse(
                _ask_stream_events(assistant, question, payload.timeout),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        result = _run_with_timeout(lambda: assistant.ask(question), payload.timeout)
        if getattr(result, "status", "") == "model_error":
            raise APIError(
                503,
                "model_unavailable",
                getattr(result, "answer", "") or "模型暂不可用，请稍后重试。",
                getattr(result, "error", None),
            )
        return _ask_response_body(result, question)

    @router.post("/ask_sql")
    def ask_sql(payload: AskSqlRequest) -> Any:
        question = payload.question.strip()
        if payload.stream:
            return StreamingResponse(
                _ask_sql_stream_events(sql_ask, question, payload.timeout),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        outcome = _run_with_timeout(lambda: sql_ask(question), payload.timeout)
        result = outcome.result
        if getattr(result, "status", "") == "model_error":
            raise APIError(
                503,
                "model_unavailable",
                f"问数模型暂不可用：{getattr(result, 'answer', '') or getattr(result, 'error', '请稍后重试')}",
                getattr(result, "error", None),
            )
        return _ask_sql_response_body(outcome, streamed=False)

    return router


__all__ = [
    "APIError",
    "AskRequest",
    "AskSqlRequest",
    "StreamActionFilter",
    "build_router",
    "error_body",
    "install_error_handlers",
]
