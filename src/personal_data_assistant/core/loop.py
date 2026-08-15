# -*- coding: utf-8 -*-
# 设计取舍：
# 1) 循环只依赖鸭子协议：模型对象有 complete()（必须）与 stream_chat()（可选），
#    与 DeepSeekClient 同形；测试用 ScriptedModel，生产用 DeepSeekClient，循环代码不变。
# 2) JSON 动作协议在这里解析与执行：{"action":"tool",...} / {"action":"final",...}。
#    解析失败、未知工具、参数错误都作为 user 消息回填模型，不崩溃。
# 3) 最大轮数指“允许的非 final 回复数”（解析错误和工具调用都占预算）。预算耗尽后
#    禁止再执行工具，追加“强制收束”指令请模型做最后一次 final；若模型仍不 final，
#    则用轨迹里的工具结果拼一个确定性的中文兜底答案。
# 4) 流式输出：优先模型自己的 stream_chat 逐块回调；无 stream_chat 或流在吐字前
#    失败时自动降级为 complete 一次性输出。所有模型异常都收口为结构化中文错误。

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from personal_data_assistant.llm.client import LLMResponse, TokenUsage
from personal_data_assistant.llm.prompts import (
    build_force_final_instruction,
    build_initial_messages,
    build_parse_error_message,
    build_tool_result_message,
    build_unknown_tool_message,
)
from personal_data_assistant.tools.base import Tool, ToolResult
from personal_data_assistant.tools.registry import ToolRegistry

ActionKind = str  # 实际只有 "tool" / "final"


class ActionParseError(ValueError):
    """模型输出不是合法 JSON 动作协议。"""


@dataclass
class ParsedAction:
    action: ActionKind
    tool: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    answer: Optional[str] = None


def parse_action(text: str) -> ParsedAction:
    """从模型输出中解析 JSON 动作协议；允许 Markdown 围栏与前后零星说明文字。"""
    if not isinstance(text, str):
        raise ActionParseError("模型输出不是字符串")

    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    data: Any = None
    errors: List[str] = []
    try:
        data = json.loads(candidate)
    except ValueError as exc:
        errors.append(f"整体解析失败: {exc}")
        left = candidate.find("{")
        right = candidate.rfind("}")
        if left != -1 and right > left:
            try:
                data = json.loads(candidate[left : right + 1])
            except ValueError as exc2:
                errors.append(f"提取 JSON 子串也失败: {exc2}")

    if not isinstance(data, dict):
        raise ActionParseError(
            f"模型输出必须能解析为 JSON 对象；{'；'.join(errors) if errors else '当前类型不是对象'}"
        )

    action = data.get("action")
    if action == "final":
        answer = data.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise ActionParseError('action=final 时 answer 必须是非空字符串')
        return ParsedAction(action="final", answer=answer.strip())
    if action == "tool":
        tool = data.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise ActionParseError('action=tool 时 tool 必须是非空字符串')
        args = data.get("args", data.get("input"))
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ActionParseError("action=tool 时 args 必须是 JSON 对象")
        return ParsedAction(action="tool", tool=tool.strip(), args=args)

    raise ActionParseError(f"未知 action: {action!r}（只能是 tool 或 final）")


@dataclass
class TrajectoryStep:
    """轨迹中的一步；模型输出、解析结果、工具结果全部留痕，便于观测与评测。"""

    kind: str  # final / tool_call / tool_error / parse_error / round_limit
    round: int = 0
    model_output: Optional[str] = None
    action: Optional[str] = None
    tool: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    tool_ok: Optional[bool] = None
    tool_result: Any = None
    error: Optional[str] = None
    usage: Optional[TokenUsage] = None


@dataclass
class LoopResult:
    answer: str
    status: str  # final / forced_final / model_error
    rounds: int
    tool_rounds: int
    steps: List[TrajectoryStep]
    messages: List[Dict[str, str]]
    total_usage: TokenUsage = field(default_factory=TokenUsage)
    error: Optional[str] = None


def _add_usage(total: TokenUsage, usage: Optional[TokenUsage]) -> None:
    if usage is None:
        return
    total.prompt_tokens += usage.prompt_tokens
    total.completion_tokens += usage.completion_tokens
    total.total_tokens += usage.total_tokens


def _model_error_answer(exc: Exception) -> str:
    return f"模型暂时不可用，无法完成这次回答。原因：{exc}。请稍后重试，或检查网络与 API 配置。"


def _call_model(
    model: Any,
    messages: Sequence[Dict[str, str]],
    *,
    stream: bool,
    on_chunk: Optional[Callable[[str], None]],
) -> LLMResponse:
    """调用模型鸭子协议；带“流式在吐字前失败就降级 complete”的兜底。"""
    streamer = getattr(model, "stream_chat", None)
    if stream and callable(streamer):
        parts: List[str] = []
        try:
            stream_obj = streamer(messages)
            for chunk in stream_obj:
                if chunk is None:
                    continue
                text = str(chunk)
                if text:
                    parts.append(text)
                    if on_chunk is not None:
                        on_chunk(text)
            stream_result = getattr(stream_obj, "result", None)
            usage = getattr(stream_result, "usage", None) if stream_result is not None else None
            model_name = getattr(stream_result, "model", None) if stream_result is not None else ""
            return LLMResponse(
                content="".join(parts),
                model=model_name or "stream-model",
                usage=usage if usage is not None else TokenUsage(),
                raw={},
                streamed=True,
            )
        except Exception as exc:  # noqa: BLE001 —— 循环边界，收口为降级/错误
            if parts:
                raise
            complete = getattr(model, "complete", None)
            if callable(complete):
                response = complete(messages)
                normalized = _normalize_response(response)
                if normalized.content and on_chunk is not None:
                    on_chunk(normalized.content)
                return normalized
            raise

    complete = getattr(model, "complete", None)
    if not callable(complete):
        raise TypeError("模型对象必须提供可调用的 complete(messages) 方法")
    response = complete(messages)
    normalized = _normalize_response(response)
    if stream and normalized.content and on_chunk is not None:
        on_chunk(normalized.content)
    return normalized


def _normalize_response(response: Any) -> LLMResponse:
    if isinstance(response, LLMResponse):
        return response
    if isinstance(response, str):
        return LLMResponse(content=response, model="", usage=TokenUsage(), raw={})
    content = getattr(response, "content", None)
    if content is None:
        raise TypeError(f"模型 complete 返回值必须含 content 字段或为字符串，当前: {type(response).__name__}")
    usage = getattr(response, "usage", TokenUsage())
    if usage is None:
        usage = TokenUsage()
    model = getattr(response, "model", "") or ""
    return LLMResponse(
        content=str(content),
        model=str(model),
        usage=usage,
        raw=getattr(response, "raw", {}) or {},
        streamed=bool(getattr(response, "streamed", False)),
        retry_count=int(getattr(response, "retry_count", 0) or 0),
    )


def _make_step(
    kind: str,
    model_round: int,
    output: str,
    *,
    parsed: Optional[ParsedAction] = None,
    usage: Optional[TokenUsage] = None,
    tool_ok: Optional[bool] = None,
    tool_result: Any = None,
    error: Optional[str] = None,
) -> TrajectoryStep:
    return TrajectoryStep(
        kind=kind,
        round=model_round,
        model_output=output,
        action=parsed.action if parsed else None,
        tool=parsed.tool if parsed else None,
        args=parsed.args if parsed else None,
        tool_ok=tool_ok,
        tool_result=tool_result,
        error=error,
        usage=usage,
    )


def _forced_final_fallback_answer(steps: Sequence[TrajectoryStep], max_tool_rounds: int) -> str:
    """模型在强制收束时仍不 final，或收束调用失败：用已有工具结果拼确定性中文兜底。"""
    successes = [
        step
        for step in steps
        if step.kind == "tool_call" and step.tool_ok is True and step.tool_result is not None
    ]
    head = f"已达到最大工具调用轮数（{max_tool_rounds}），模型未给出可用的最终答案。"
    if successes:
        shown = successes[-3:]
        details = "\n".join(
            f"- {step.tool}: {step.tool_result}" for step in shown
        )
        return f"{head}\n以下是已成功获取的工具结果，请先据此判断：\n{details}\n若仍不足以回答，请稍后重试或换一种问法。"
    return f"{head}没有获得足够的工具结果来回答问题，请稍后重试或补充更多信息。"


def run_tool_loop(
    question: str,
    tools: Union[ToolRegistry, Iterable[Tool]],
    model: Any,
    *,
    max_tool_rounds: int = 6,
    stream: bool = False,
    on_chunk: Optional[Callable[[str], None]] = None,
    system_prompt: Optional[str] = None,
) -> LoopResult:
    """M1 核心循环：模型调用 → JSON 动作解析 → 工具执行 → 结果回填 → 直到 final。

    max_tool_rounds 是允许的“非 final 回复”预算（解析错误、未知工具、工具调用都占）。
    预算耗尽后追加一次强制收束模型调用；若仍失败，用轨迹工具结果拼中文兜底答案。
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question 必须是非空字符串")
    if max_tool_rounds < 1:
        raise ValueError("max_tool_rounds 必须 >= 1")

    registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(list(tools))
    messages = build_initial_messages(question, registry.describe(), system_prompt=system_prompt)

    steps: List[TrajectoryStep] = []
    total_usage = TokenUsage()
    tool_rounds = 0
    non_final_rounds = 0
    model_round = 0

    def finish(
        answer: str,
        status: str,
        *,
        error: Optional[str] = None,
    ) -> LoopResult:
        return LoopResult(
            answer=answer,
            status=status,
            rounds=model_round,
            tool_rounds=tool_rounds,
            steps=list(steps),
            messages=[dict(m) for m in messages],
            total_usage=total_usage,
            error=error,
        )

    def forced_final(final_error: Optional[str] = None) -> LoopResult:
        nonlocal model_round
        messages.append(build_force_final_instruction(max_tool_rounds))
        model_round += 1
        final_output: Optional[str] = None
        final_usage: Optional[TokenUsage] = None
        final_parse_error: Optional[str] = final_error

        try:
            response = _call_model(
                model,
                messages,
                stream=stream,
                on_chunk=on_chunk,
            )
            final_output = response.content
            final_usage = response.usage
            _add_usage(total_usage, final_usage)
            try:
                parsed = parse_action(final_output or "")
            except ActionParseError as exc:
                final_parse_error = str(exc)
            else:
                if parsed.action == "final" and parsed.answer:
                    steps.append(
                        _make_step(
                            "final",
                            model_round,
                            final_output,
                            parsed=parsed,
                            usage=final_usage,
                        )
                    )
                    return finish(parsed.answer, "forced_final")
                final_parse_error = "模型在强制收束时仍没有输出 action=final"
        except Exception as exc:  # noqa: BLE001 —— 强制收束也不允许崩溃
            final_parse_error = str(exc)

        fallback_answer = _forced_final_fallback_answer(steps, max_tool_rounds)
        steps.append(
            _make_step(
                "final",
                model_round,
                final_output,
                usage=final_usage,
                error=final_parse_error,
            )
        )
        return finish(fallback_answer, "forced_final", error=final_parse_error)

    while True:
        model_round += 1
        try:
            response = _call_model(
                model,
                messages,
                stream=stream,
                on_chunk=on_chunk,
            )
        except Exception as exc:  # noqa: BLE001 —— 模型边界收口
            steps.append(
                _make_step(
                    "model_error",
                    model_round,
                    None,
                    error=str(exc),
                )
            )
            messages.append({"role": "user", "content": _model_error_answer(exc)})
            return finish(_model_error_answer(exc), "model_error", error=str(exc))

        output = response.content or ""
        _add_usage(total_usage, response.usage)

        try:
            parsed = parse_action(output)
        except ActionParseError as exc:
            parse_error = str(exc)
            non_final_rounds += 1
            if non_final_rounds > max_tool_rounds:
                steps.append(
                    _make_step(
                        "round_limit",
                        model_round,
                        output,
                        usage=response.usage,
                        error=parse_error,
                    )
                )
                messages.append({"role": "assistant", "content": output})
                return forced_final(parse_error)
            steps.append(
                _make_step(
                    "parse_error",
                    model_round,
                    output,
                    usage=response.usage,
                    error=parse_error,
                )
            )
            messages.append({"role": "assistant", "content": output})
            messages.append(build_parse_error_message(output, parse_error))
            continue

        if parsed.action == "final":
            steps.append(
                _make_step(
                    "final",
                    model_round,
                    output,
                    parsed=parsed,
                    usage=response.usage,
                )
            )
            return finish(parsed.answer or "", "final")

        # 下面是 tool 动作：先把预算 +1；越界则拒绝执行并强制收束。
        non_final_rounds += 1
        if non_final_rounds > max_tool_rounds:
            steps.append(
                _make_step(
                    "round_limit",
                    model_round,
                    output,
                    parsed=parsed,
                    usage=response.usage,
                    error=f"已达到最大工具调用轮数 {max_tool_rounds}，拒绝执行 {parsed.tool}",
                )
            )
            messages.append({"role": "assistant", "content": output})
            return forced_final()

        tool = registry.find(parsed.tool or "")
        if tool is None:
            steps.append(
                _make_step(
                    "tool_error",
                    model_round,
                    output,
                    parsed=parsed,
                    usage=response.usage,
                    error=f"模型请求了未注册的工具: {parsed.tool!r}",
                )
            )
            messages.append({"role": "assistant", "content": output})
            messages.append(
                build_unknown_tool_message(parsed.tool or "", [t.name for t in registry.list_tools()])
            )
            continue

        tool_result: ToolResult = tool.execute(parsed.args or {})
        tool_rounds += 1
        steps.append(
            _make_step(
                "tool_call",
                model_round,
                output,
                parsed=parsed,
                usage=response.usage,
                tool_ok=tool_result.ok,
                tool_result=tool_result.result if tool_result.ok else tool_result.error,
                error=None if tool_result.ok else tool_result.error,
            )
        )
        messages.append({"role": "assistant", "content": output})
        messages.append(
            build_tool_result_message(
                tool.name,
                tool_result.ok,
                tool_result.result,
                error=tool_result.error,
            )
        )
        continue
