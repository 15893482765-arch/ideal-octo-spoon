"""轻量 JSON Schema 校验（T22 / PRD §5.3「服务端完成参数校验」）。

只实现工具入参/出参声明所需的最小关键字集合，避免引入额外依赖：

- `type`：object / array / string / integer / number / boolean / null
- `properties` / `required` / `additionalProperties: false`
- `enum` / `const`
- `minLength` / `maxLength` / `minimum` / `maximum`
- `items`

校验失败返回人类可读错误列表，由网关包装为 `MCP_SCHEMA_INVALID`。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_SCALAR_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence):
        return "array"
    return type(value).__name__


def _type_matches(value: Any, expected: str) -> bool:
    actual = _type_of(value)
    if expected == "number":
        return actual in ("number", "integer")
    return actual == expected


def validate(instance: Any, schema: Mapping[str, Any], *, path: str = "$") -> list[str]:
    """校验 `instance` 是否满足 `schema`；返回错误描述列表（空表示通过）。"""
    errors: list[str] = []
    expected = schema.get("type")
    if isinstance(expected, str) and expected in _SCALAR_TYPES:
        if not _type_matches(instance, expected):
            errors.append(f"{path}: 期望 {expected}，实际 {_type_of(instance)}")
            return errors

    if "enum" in schema:
        enum_values = schema["enum"]
        if isinstance(enum_values, Sequence) and instance not in enum_values:
            errors.append(f"{path}: 取值必须是 {list(enum_values)} 之一")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: 取值必须为 {schema['const']!r}")

    if isinstance(instance, str):
        min_len = schema.get("minLength")
        if isinstance(min_len, int) and len(instance) < min_len:
            errors.append(f"{path}: 长度不得小于 {min_len}")
        max_len = schema.get("maxLength")
        if isinstance(max_len, int) and len(instance) > max_len:
            errors.append(f"{path}: 长度不得超过 {max_len}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and instance < minimum:
            errors.append(f"{path}: 不得小于 {minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and instance > maximum:
            errors.append(f"{path}: 不得大于 {maximum}")

    if isinstance(instance, Mapping):
        properties = schema.get("properties") or {}
        if not isinstance(properties, Mapping):
            properties = {}
        required = schema.get("required") or []
        if isinstance(required, Sequence):
            for key in required:
                if key not in instance:
                    errors.append(f"{path}.{key}: 必填字段缺失")
        for key, value in instance.items():
            if key in properties:
                sub_schema = properties[key]
                if isinstance(sub_schema, Mapping):
                    errors.extend(validate(value, sub_schema, path=f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: 未知字段（工具不接受该参数）")

    if isinstance(instance, Sequence) and not isinstance(instance, (str, bytes)):
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(instance):
                errors.extend(validate(item, item_schema, path=f"{path}[{index}]"))

    return errors


def is_well_formed_schema(schema: Any) -> bool:
    """粗略判定工具声明的 Schema 是否well-formed（供注册期校验）。"""
    if not isinstance(schema, Mapping):
        return False
    schema_type = schema.get("type")
    if schema_type not in ("object", "array"):
        return False
    if schema_type == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            return False
        required = schema.get("required", [])
        if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
            return False
        return all(isinstance(k, str) for k in required)
    return True


def schema_property_names(schema: Mapping[str, Any]) -> tuple[str, ...]:
    """返回对象 Schema 声明的字段名（用于注册期比对保留权限字段）。"""
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    return tuple(str(k) for k in properties)
