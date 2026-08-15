# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) 工具协议不依赖任何框架，Tool 就是“名称 + JSON Schema + 可执行函数”。
# 2) Tool.execute 必须把用户函数抛出的异常转成 ToolResult(ok=False)，
#    这是核心循环不崩溃的边界保证。
# 3) 参数校验只覆盖 required 与基础 JSON 类型；M1 不引入 jsonschema 依赖。

import json

import pytest

from personal_data_assistant.tools.base import Tool, ToolResult, validate_arguments
from personal_data_assistant.tools.registry import ToolNotFoundError, ToolRegistry


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


def test_tool_execute_success_returns_tool_result():
    tool = make_echo_tool()
    result = tool.execute({"text": "你好"})

    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.result == "echo: 你好"
    assert result.error is None


def test_tool_execute_wraps_user_function_exception():
    def boom(args):
        raise RuntimeError("工具内部爆炸")

    tool = Tool(
        name="boom",
        description="必定失败",
        parameters={"type": "object", "properties": {}},
        func=boom,
    )

    result = tool.execute({})
    assert result.ok is False
    assert "RuntimeError" in result.error
    assert "工具内部爆炸" in result.error


def test_tool_execute_accepts_tool_result_from_user_function():
    def raw_result(args):
        return ToolResult(ok=False, error="业务失败，但没抛异常")

    tool = Tool(
        name="raw",
        description="返回结构化失败",
        parameters={"type": "object", "properties": {}},
        func=raw_result,
    )

    result = tool.execute({})
    assert result.ok is False
    assert result.error == "业务失败，但没抛异常"


def test_validate_arguments_checks_required_fields():
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    assert validate_arguments(schema, {"text": "ok"}) == []
    errors = validate_arguments(schema, {})
    assert errors, "缺少 required 字段时必须报错"
    assert "text" in errors[0]

    assert validate_arguments(schema, "not-a-dict") != []


def test_validate_arguments_checks_basic_types():
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "flag": {"type": "boolean"},
            "items": {"type": "array"},
        },
    }

    errors = validate_arguments(
        schema,
        {"count": 1.5, "ratio": "高", "flag": 1, "items": "abc"},
    )
    assert len(errors) >= 4


def test_registry_register_get_list_and_describe():
    registry = ToolRegistry()
    tool = make_echo_tool()
    registry.register(tool)

    assert registry.find("echo") is tool
    assert registry.get("echo") is tool
    assert [t.name for t in registry.list_tools()] == ["echo"]

    schemas = registry.describe()
    assert schemas[0]["name"] == "echo"
    assert schemas[0]["description"] == "原样返回输入文本"
    assert schemas[0]["parameters"] == tool.parameters

    prompt_text = registry.describe_for_prompt()
    assert json.loads(prompt_text)[0]["name"] == "echo"


def test_registry_rejects_duplicate_names():
    registry = ToolRegistry()
    registry.register(make_echo_tool())
    with pytest.raises(ValueError):
        registry.register(make_echo_tool())


def test_registry_get_unknown_tool_raises_clear_error():
    registry = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        registry.get("nope")
    assert registry.find("nope") is None
