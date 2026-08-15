# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) 提示词是工具调用协议的唯一“编译器”，协议 JSON 一旦写进 prompt 就要有测试锁住。
# 2) 只断言关键约束存在（action 取值、只输出 JSON、JSON Schema 注入），
#    不逐字比较整段 prompt，给措辞润色留空间。

import json

from personal_data_assistant.llm.prompts import (
    build_force_final_instruction,
    build_parse_error_message,
    build_system_prompt,
    build_tool_result_message,
)

TOOL_SCHEMAS = [
    {
        "name": "echo",
        "description": "原样返回输入文本",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
]


def test_system_prompt_injects_tool_json_schema_and_protocol():
    prompt = build_system_prompt(TOOL_SCHEMAS)

    assert "echo" in prompt
    assert "原样返回输入文本" in prompt
    # 工具清单使用紧凑 JSON（省 token），因此这里锁紧凑形态而不是带空格的形态
    assert '"type":"object"' in prompt
    assert '{"action":"tool"' in prompt
    assert '{"action":"final"' in prompt
    assert "JSON Schema" in prompt
    assert "只输出" in prompt and "JSON" in prompt
    # 明确不用原生 function calling，避免模型退回 OpenAI tool_calls 格式
    assert "function calling" in prompt


def test_system_prompt_is_valid_json_in_tools_section():
    prompt = build_system_prompt(TOOL_SCHEMAS)
    marker = "可用工具"
    assert marker in prompt
    start = prompt.index(marker)
    # 提示词里的工具清单必须能被程序反解（保持 prompt 与 registry 同源）；
    # 用 raw_decode 解析从第一个 "[" 开始的完整 JSON，不被参数里的嵌套 [] 干扰
    tail = prompt[start:]
    left = tail.index("[")
    tools, _end = json.JSONDecoder().raw_decode(tail[left:])
    assert tools == TOOL_SCHEMAS


def test_tool_result_message_uses_user_role_not_native_tool_role():
    message = build_tool_result_message("echo", True, "echo: 你好")
    assert message["role"] == "user"
    payload = json.loads(message["content"])
    assert payload["tool_result"]["tool"] == "echo"
    assert payload["tool_result"]["ok"] is True
    assert payload["tool_result"]["result"] == "echo: 你好"


def test_parse_error_message_feeds_error_back_to_model():
    message = build_parse_error_message("随便一段话", "不是合法 JSON 动作")
    assert message["role"] == "user"
    assert "随便一段话" in message["content"]
    assert "不是合法 JSON 动作" in message["content"]
    assert '{"action":"tool"' in message["content"]
    assert '{"action":"final"' in message["content"]


def test_force_final_instruction_forbids_more_tool_calls():
    message = build_force_final_instruction(6)
    content = message["content"]
    assert "6" in content
    assert "final" in content
    assert "不得" in content or "不要" in content or "禁止" in content
