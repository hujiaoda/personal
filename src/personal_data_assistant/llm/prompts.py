# -*- coding: utf-8 -*-
# 设计取舍：
# 1) JSON 动作协议完全由本文件声明：模型只允许输出 action=tool 或 action=final。
#    不使用 OpenAI 原生 tools/tool_calls，保证任何文本模型都能降级使用。
# 2) 提示词里直接放工具的 JSON Schema，让模型知道参数契约；错误回填消息也由
#    本文件生成，保证循环里所有“对模型说的话”都有固定形态、可单测。
# 3) 工具结果用 user 角色回填，避免原生 function calling 要求的 tool_call_id 配对。

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

_PROTOCOL_RULES = """\
你每轮必须只输出一个 JSON 对象，不要输出 Markdown 代码块、前后解释或任何多余文字。
JSON 动作协议只有两种（注意 action 的值必须是 tool 或 final）：
1. 调用工具：{"action":"tool","tool":"<工具名>","args":{...}}
   args 必须是 JSON 对象，字段类型与数量必须符合上面该工具的 JSON Schema。
   一次只调用一个工具；工具执行结果会由系统在下一轮以 user 消息回填。
2. 给出最终回答：{"action":"final","answer":"<直接给用户的中文回答>"}
   answer 必须是完整、可直接展示的中文回答；信息不足就说明缺什么，不要编造。"""


def build_system_prompt(
    tool_schemas: Sequence[Mapping[str, Any]],
    *,
    extra_instructions: str = "",
) -> str:
    """生成 M1 核心循环的系统提示词。

    tool_schemas 由 ToolRegistry.describe() 提供，里面是工具的 JSON Schema。
    """
    tools_json = json.dumps(list(tool_schemas), ensure_ascii=False, separators=(",", ":"))
    parts = [
        "你是个人数据助手（Personal Data Assistant）。",
        "你会拿到用户问题与一组可用工具；你通过手写 JSON 动作协议决定下一步，"
        "不要使用原生 function calling，也不要输出 OpenAI 的 tool_calls 格式。",
        "## 可用工具",
        "下面是可用工具清单（JSON Schema）。只有清单里存在的工具名可以调用。",
        tools_json,
        "## 输出协议",
        _PROTOCOL_RULES,
    ]
    if extra_instructions:
        parts.extend(["## 补充要求", extra_instructions])
    return "\n".join(parts)


def build_initial_messages(
    question: str,
    tool_schemas: Sequence[Mapping[str, Any]],
    *,
    system_prompt: Optional[str] = None,
) -> List[Dict[str, str]]:
    """拼出第一轮 messages：system + 用户问题。"""
    prompt = system_prompt if system_prompt is not None else build_system_prompt(tool_schemas)
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": question},
    ]


def build_tool_result_message(
    tool_name: str,
    ok: bool,
    result: Any,
    *,
    error: Optional[str] = None,
) -> Dict[str, str]:
    """把工具执行结果回填成 user 消息，不使用原生 tool 角色。"""
    payload = {
        "tool_result": {
            "tool": tool_name,
            "ok": ok,
            "result": result,
            "error": error,
        }
    }
    # 工具结果里带模型原文/错误信息，宁可多几个空格也要便于模型与调试者阅读
    content = json.dumps(payload, ensure_ascii=False, default=str)
    return {"role": "user", "content": content}


def build_parse_error_message(raw_output: str, error: str) -> Dict[str, str]:
    """模型输出不是合法 JSON 动作时，把错误和原文回填，让它修正后重发。"""
    excerpt = raw_output.strip()
    if len(excerpt) > 400:
        excerpt = excerpt[:400] + "…（原文过长已截断）"
    content = (
        f"你上一次回复无法解析为 JSON 动作，解析失败原因：{error}\n"
        f"原始回复：{excerpt}\n"
        "请重新只输出一个 JSON 对象，且 action 只能是 tool 或 final：\n"
        '调用工具：{"action":"tool","tool":"<工具名>","args":{...}}\n'
        '最终回答：{"action":"final","answer":"<中文回答>"}'
    )
    return {"role": "user", "content": content}


def build_unknown_tool_message(tool_name: str, available_names: Sequence[str]) -> Dict[str, str]:
    """模型请求了未注册的工具时回填。"""
    names = "、".join(available_names) or "（当前没有可用工具）"
    content = (
        f"你请求调用未注册的工具 {tool_name!r}；当前可用工具只有：{names}。\n"
        "请改用清单里的工具，或者若信息已足够直接输出 final 最终回答。"
    )
    return {"role": "user", "content": content}


def build_force_final_instruction(max_tool_rounds: int) -> Dict[str, str]:
    """达到最大工具轮数后，禁止再要工具，强制模型收束为 final。"""
    content = (
        f"已达到最大工具调用轮数上限（{max_tool_rounds}），这是强制收束："
        "不能再调用任何工具。请根据上面已经获得的工具结果，立即只输出一个 final 动作，"
        "直接回答用户；若信息不足，也必须在 answer 里说明缺什么，不得再要工具。"
    )
    return {"role": "user", "content": content}
