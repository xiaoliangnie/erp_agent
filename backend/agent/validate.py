# -*- coding: utf-8 -*-
"""工具入参轻量校验：type / required / enum / additionalProperties。不引 jsonschema。"""
from __future__ import annotations

from .tools import Tool, ToolError


def validate_arguments(tool: Tool, arguments: dict) -> dict:
    schema = tool.parameters or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    additional = schema.get("additionalProperties", True)
    if not isinstance(arguments, dict):
        raise ToolError("工具入参必须是对象")
    missing = [key for key in required if key not in arguments]
    if missing:
        raise ToolError("缺少必填参数：" + "、".join(missing))
    if additional is False:
        extra = [key for key in arguments if key not in properties]
        if extra:
            raise ToolError("不接受未声明参数：" + "、".join(sorted(extra)))
    for key, value in arguments.items():
        spec = properties.get(key)
        if not spec:
            continue
        expected = spec.get("type")
        if expected and not _type_ok(value, expected):
            raise ToolError(f"参数 {key} 类型应为 {expected}")
        enum = spec.get("enum")
        if enum is not None and value not in enum:
            raise ToolError(f"参数 {key} 只能是 {' / '.join(str(item) for item in enum)}")
    return arguments


def _type_ok(value, expected) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True
