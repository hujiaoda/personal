# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) 核心循环必须能用脚本化 fake 模型测遍所有分支，绝不依赖真实 DeepSeek。
# 2) fake 模型实现 complete/stream_chat 两个入口，与 DeepSeekClient 同形；
#    循环只依赖这个鸭子协议，因此客户端可整体替换。
# 3) 测试锁住边界：最大轮数、解析失败回填、工具异常兜底、模型崩溃返回结构化错误。

import json

import pytest

from personal_data_assistant.core.loop import (
    LoopResult,
    parse_action,
    run_tool_loop,
)
from personal_data_assistant.llm.client import LLMResponse, TokenUsage
from personal_data_assistant.tools.base import Tool
from personal_data_assistant.tools.registry import ToolRegistry


def make_echo_tool():
    def echo(args):
        return f"echo: {args['text']}"

    return Tool(
        name="echo",
        description="原样返回输入文本",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        func=echo,
    )


def make_boom_tool():
    def boom(args):
        raise RuntimeError("工具内部爆炸")

    return Tool(
        name="boom",
        description="必定失败",
        parameters={"type": "object", "properties": {}},
        func=boom,
    )


def make_registry():
    registry = ToolRegistry()
    registry.register(make_echo_tool())
    return registry


class ChunkStream:
    """与 DeepSeekClient.stream_chat 同形的脚本化流对象。"""

    def __init__(self, chunks, usage=None):
        self._chunks = list(chunks)
        self._usage = usage
        self.result = None

    def __iter__(self):
        return iter(self._chunks)


class ScriptedModel:
    def __init__(self, responses):
        # responses 元素：字符串（一次回复内容）、LLMResponse、Exception（抛错）
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages):
        self.calls.append([dict(m) for m in messages])
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, LLMResponse):
            return item
        return LLMResponse(
            content=item,
            model="fake",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            raw={},
        )

    def stream_chat(self, messages):
        self.calls.append([dict(m) for m in messages])
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, LLMResponse):
            return ChunkStream([item.content], item.usage)
        if isinstance(item, str):
            return ChunkStream([item])
        return ChunkStream(item)


TOOL_CALL = '{"action":"tool","tool":"echo","args":{"text":"你好"}}'
FINAL = '{"action":"final","answer":"最终答案"}'
TOOL_RESULT_MARKER = '"tool_result"'


def test_parse_action_accepts_plain_fenced_and_wrapped_json():
    assert parse_action(FINAL).action == "final"
    assert parse_action(f"```json\n{FINAL}\n```").action == "final"
    assert parse_action(f"好的，以下是我的回答：{FINAL}").action == "final"

    parsed = parse_action(TOOL_CALL)
    assert parsed.action == "tool"
    assert parsed.tool == "echo"
    assert parsed.args == {"text": "你好"}


def test_parse_action_rejects_unknown_action_and_empty_final_answer():
    with pytest.raises(ValueError):
        parse_action('{"action":"dance"}')
    with pytest.raises(ValueError):
        parse_action('{"action":"final","answer":""}')
    with pytest.raises(ValueError):
        parse_action("完全不是 JSON")


def test_final_answer_directly_ends_loop():
    model = ScriptedModel([FINAL])
    result = run_tool_loop("你好", make_registry(), model)

    assert isinstance(result, LoopResult)
    assert result.status == "final"
    assert result.answer == "最终答案"
    assert result.tool_rounds == 0
    assert result.rounds == 1
    assert [step.kind for step in result.steps] == ["final"]


def test_tool_call_then_final_executes_tool_and_backfills_result():
    model = ScriptedModel([TOOL_CALL, FINAL])
    result = run_tool_loop("你好", make_registry(), model)

    assert result.status == "final"
    assert result.answer == "最终答案"
    assert result.tool_rounds == 1
    assert result.rounds == 2
    assert [step.kind for step in result.steps] == ["tool_call", "final"]
    assert result.steps[0].tool == "echo"
    assert result.steps[0].args == {"text": "你好"}
    assert result.steps[0].tool_ok is True
    assert result.steps[0].tool_result == "echo: 你好"

    # 工具结果必须以 user 角色回填，不依赖原生 function calling 的 tool 角色
    backfilled = [m for m in result.messages if m["role"] == "user"]
    assert any(TOOL_RESULT_MARKER in m["content"] for m in backfilled)
    # 模型确实看到了 assistant 的工具调用消息与 user 的工具结果消息
    second_call = model.calls[1]
    assert second_call[-2]["role"] == "assistant"
    assert TOOL_CALL in second_call[-2]["content"]
    assert second_call[-1]["role"] == "user"
    assert TOOL_RESULT_MARKER in second_call[-1]["content"]


def test_parse_error_is_fed_back_and_loop_recovers():
    model = ScriptedModel(["抱歉，我不是 JSON", FINAL])
    result = run_tool_loop("你好", make_registry(), model)

    assert result.status == "final"
    assert result.answer == "最终答案"
    assert [step.kind for step in result.steps] == ["parse_error", "final"]
    assert result.steps[0].error is not None
    assert result.steps[0].tool is None

    recovery_message = model.calls[1][-1]["content"]
    assert "无法解析" in recovery_message or "解析失败" in recovery_message
    assert "抱歉，我不是 JSON" in recovery_message


def test_unknown_tool_is_fed_back_without_execution():
    bad_call = '{"action":"tool","tool":"nope","args":{}}'
    model = ScriptedModel([bad_call, FINAL])
    result = run_tool_loop("你好", make_registry(), model)

    assert result.status == "final"
    assert result.tool_rounds == 0
    assert result.steps[0].kind == "tool_error"
    assert "nope" in result.steps[0].error
    assert result.steps[0].tool_ok is None


def test_tool_exception_becomes_failed_result_and_loop_continues():
    registry = ToolRegistry()
    registry.register(make_boom_tool())
    model = ScriptedModel(
        ['{"action":"tool","tool":"boom","args":{}}', FINAL]
    )

    result = run_tool_loop("触发失败", registry, model)

    assert result.status == "final"
    assert result.tool_rounds == 1
    assert result.steps[0].tool_ok is False
    assert "工具内部爆炸" in result.steps[0].tool_result
    backfilled = model.calls[1][-1]["content"]
    assert '"ok": false' in backfilled
    assert "工具内部爆炸" in backfilled


def test_max_tool_rounds_forces_final_answer():
    # 前两轮要工具；第三轮还想越界要工具，触发强制收束；收束调用给出 final
    model = ScriptedModel(
        [
            TOOL_CALL,
            TOOL_CALL,
            TOOL_CALL,
            '{"action":"final","answer":"按已有信息给出的答案"}',
        ]
    )
    result = run_tool_loop("你好", make_registry(), model, max_tool_rounds=2)

    assert result.status == "forced_final"
    assert result.answer == "按已有信息给出的答案"
    assert result.tool_rounds == 2
    assert [step.kind for step in result.steps] == [
        "tool_call",
        "tool_call",
        "round_limit",
        "final",
    ]
    assert result.steps[-2].tool == "echo"  # 越界请求被记录但未执行
    assert "强制" in model.calls[-1][-1]["content"] or "上限" in model.calls[-1][-1]["content"]


def test_forced_final_falls_back_to_tool_results_when_model_still_wont_final():
    model = ScriptedModel([TOOL_CALL, TOOL_CALL, TOOL_CALL, TOOL_CALL])
    result = run_tool_loop("你好", make_registry(), model, max_tool_rounds=2)

    assert result.status == "forced_final"
    assert result.tool_rounds == 2
    assert "最大" in result.answer and "轮" in result.answer
    assert "echo: 你好" in result.answer


def test_parse_errors_consume_round_budget_and_force_final():
    model = ScriptedModel(["坏输出", "还是坏输出", "继续坏"])
    result = run_tool_loop("你好", make_registry(), model, max_tool_rounds=2)

    assert result.status == "forced_final"
    assert result.tool_rounds == 0
    assert [step.kind for step in result.steps] == [
        "parse_error",
        "parse_error",
        "round_limit",
        "final",
    ]
    assert "最大" in result.answer


def test_model_failure_returns_structured_chinese_error_not_crash():
    model = ScriptedModel([RuntimeError("connection refused")])
    result = run_tool_loop("你好", make_registry(), model)

    assert result.status == "model_error"
    assert "模型" in result.answer
    assert "connection refused" in result.answer or "connection refused" in result.error
    assert result.error is not None
    assert result.tool_rounds == 0


def test_streaming_forwards_chunks_and_still_parses_action():
    chunks = ['{"action":"final","answ', 'er":"流式答案"}']
    model = ScriptedModel([chunks])
    emitted = []

    result = run_tool_loop(
        "你好",
        make_registry(),
        model,
        stream=True,
        on_chunk=emitted.append,
    )

    assert result.status == "final"
    assert result.answer == "流式答案"
    assert emitted == chunks


def test_streaming_with_complete_only_model_falls_back_to_one_shot():
    class CompleteOnlyModel(ScriptedModel):
        def stream_chat(self, messages):
            raise NotImplementedError

    model = CompleteOnlyModel([FINAL])
    emitted = []

    result = run_tool_loop(
        "你好",
        make_registry(),
        model,
        stream=True,
        on_chunk=emitted.append,
    )

    # 有 stream_chat 方法但不可用：循环必须降级到 complete，并把整段回答作为一个块输出
    assert result.status == "final"
    assert result.answer == "最终答案"
    assert emitted == [FINAL]


def test_stream_failure_before_any_chunk_falls_back_to_complete():
    class FlakyStreamModel(ScriptedModel):
        def stream_chat(self, messages):
            raise RuntimeError("stream broken")

    model = FlakyStreamModel([FINAL])
    emitted = []

    result = run_tool_loop(
        "你好",
        make_registry(),
        model,
        stream=True,
        on_chunk=emitted.append,
    )

    assert result.status == "final"
    assert result.answer == "最终答案"
    assert emitted == [FINAL]
    assert len(model.calls) == 1  # 失败的 stream 尝试不算数，complete 成功一次


def test_tool_result_message_remains_valid_json():
    registry = ToolRegistry()
    registry.register(make_boom_tool())
    model = ScriptedModel(
        ['{"action":"tool","tool":"boom","args":{}}', FINAL]
    )
    result = run_tool_loop("触发失败", registry, model)

    user_messages = [m for m in result.messages if m["role"] == "user"]
    payload = json.loads(user_messages[-1]["content"])
    assert payload["tool_result"]["ok"] is False


def test_question_must_be_non_empty_string():
    with pytest.raises(ValueError):
        run_tool_loop("", make_registry(), ScriptedModel([]))
