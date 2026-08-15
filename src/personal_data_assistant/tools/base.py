# -*- coding: utf-8 -*-
# 设计取舍：
# 1) 工具协议 = 名称 + 描述 + JSON Schema + 可执行函数；不依赖任何框架。
#    提示词里展示 JSON Schema，循环里用同一份 schema 做基础参数校验。
# 2) Tool.execute 是“永不抛出业务异常”的边界：用户函数抛错必须转成
#    ToolResult(ok=False)，核心循环因此不会因工具崩溃。
# 3) M1 不引入 jsonschema：只校验 required 与基础 JSON 类型，足够兜住模型幻觉；
#    深层约束（格式、依赖、枚举）留在具体工具函数内部再查。

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


@dataclass
class ToolResult:
    """工具执行结果。成功用 result 承载，失败用 error 说明原因。"""

    ok: bool
    result: Any = None
    error: Optional[str] = None


@dataclass(frozen=True)
class Tool:
    """一个可被核心循环调用的工具。

    parameters 必须是 JSON Schema 的 object 描述，例如:
        {"type": "object", "properties": {...}, "required": [...]}
    """

    name: str
    description: str
    parameters: Dict[str, Any]
    func: Callable[[Dict[str, Any]], Any] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"工具名不合法: {self.name!r}（需匹配 {_NAME_RE.pattern}）")
        if not isinstance(self.parameters, dict):
            raise ValueError("工具 parameters 必须是 JSON Schema 对象描述")
        if not callable(self.func):
            raise ValueError(f"工具 {self.name!r} 的 func 必须可调用")

    def to_schema(self) -> Dict[str, Any]:
        """输出注入提示词/注册表描述用的 JSON Schema。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def execute(self, args: Any) -> ToolResult:
        """校验参数并执行工具，所有异常都收口为 ToolResult。"""
        if not isinstance(args, dict):
            return ToolResult(ok=False, error=f"args 必须是 JSON 对象，当前类型: {type(args).__name__}")

        errors = validate_arguments(self.parameters, args)
        if errors:
            return ToolResult(ok=False, error="参数不符合 JSON Schema: " + "; ".join(errors))

        try:
            raw = self.func(args)
        except Exception as exc:  # noqa: BLE001 —— 边界收口，不允许工具异常击穿循环
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        if isinstance(raw, ToolResult):
            return raw
        return ToolResult(ok=True, result=raw)


_TYPE_CHECKS = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
}


def validate_arguments(parameters: Mapping[str, Any], args: Any) -> List[str]:
    """按 JSON Schema 的 object 描述做最小参数校验，返回错误列表（空列表 = 通过）。

    覆盖：顶层必须为 object、required 字段存在、基础 type 匹配。
    明确不覆盖：嵌套 schema、enum、format 等，M1 保持轻量。
    """
    if not isinstance(parameters, Mapping):
        return ["工具 parameters 必须是 JSON Schema 对象描述"]
    if not isinstance(args, dict):
        return ["args 必须是 JSON 对象"]

    errors: List[str] = []

    properties = parameters.get("properties") or {}
    if not isinstance(properties, Mapping):
        return ["parameters.properties 必须是对象"]

    required = parameters.get("required") or []
    if isinstance(required, list):
        for name in required:
            if name not in args:
                errors.append(f"缺少必填字段: {name}")

    for name, value in args.items():
        prop_schema = properties.get(name)
        if not isinstance(prop_schema, Mapping):
            continue  # 未知字段放行；具体工具函数负责自己的白名单
        expected_type = prop_schema.get("type")
        if not expected_type:
            continue
        checker = _TYPE_CHECKS.get(expected_type)
        if checker is None:
            continue
        if not checker(value):
            errors.append(f"字段 {name} 期望类型 {expected_type}，实际为 {type(value).__name__}")

    return errors
