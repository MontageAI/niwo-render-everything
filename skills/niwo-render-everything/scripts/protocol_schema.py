"""Offline validation of the JSON Schema exported by Niwo's submit endpoint.

Only schema keywords emitted by that endpoint are supported. Unknown validation
keywords fail closed so a schema upgrade cannot silently disable a check.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "references/bundle-submit.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
LIMITS = SCHEMA["x-niwo"]
SUPPORTED = {
    "$defs", "$ref", "title", "description", "default", "x-niwo", "type",
    "properties", "additionalProperties", "required", "anyOf", "enum", "const",
    "items", "minItems", "maxItems", "minLength", "maxLength", "minimum",
    "maximum", "exclusiveMinimum", "exclusiveMaximum", "pattern",
}


def validate(value: Any, schema: dict | None = None, path: str = "bundle") -> list[str]:
    """Return field-addressed errors without coercing or modifying user content."""
    schema = SCHEMA if schema is None else schema
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise ValueError(f"Unsupported schema keywords: {sorted(unknown)}; update validator")
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            raise ValueError(f"Unsupported schema reference: {ref}")
        return validate(value, SCHEMA["$defs"][ref.split("/")[-1]], path)
    if "anyOf" in schema:
        choices = [validate(value, branch, path) for branch in schema["anyOf"]]
        if any(not errors for errors in choices):
            return []
        # For an invalid object in Optional[Object], report its missing fields,
        # not the misleading shorter error that it should have been null.
        for branch, errors in zip(schema["anyOf"], choices):
            if (branch.get("type") == "null") == (value is None):
                return errors
        return choices[0]
    expected = schema.get("type")
    valid_type = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "boolean": isinstance(value, bool),
        "null": value is None,
        "number": type(value) is int or (type(value) is float and math.isfinite(value)),
        "integer": type(value) is int,
    }
    if expected and not valid_type[expected]:
        return [f"{path} 必须是 {expected}（非有限数值也不接受）"]
    errors = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} 必须是 {schema['enum']} 之一")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path} 必须是 {schema['const']!r}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}.{name} 缺少必填字段")
        for name, item in value.items():
            child = props.get(name, schema.get("additionalProperties", True))
            if child is False:
                errors.append(f"{path}.{name} 是未知字段")
            elif isinstance(child, dict):
                errors.extend(validate(item, child, f"{path}.{name}"))
    if isinstance(value, list):
        for key, compare in (("minItems", lambda n: len(value) < n), ("maxItems", lambda n: len(value) > n)):
            if key in schema and compare(schema[key]):
                errors.append(f"{path} 有 {len(value)} 项，要求 {key}={schema[key]}")
        for i, item in enumerate(value):
            errors.extend(validate(item, schema.get("items", {}), f"{path}[{i}]"))
    if isinstance(value, str):
        for key, compare in (("minLength", lambda n: len(value) < n), ("maxLength", lambda n: len(value) > n)):
            if key in schema and compare(schema[key]):
                errors.append(f"{path} 有 {len(value)} 字符，要求 {key}={schema[key]}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path} 不符合格式 {schema['pattern']}")
    if type(value) in (int, float):
        for key, bad in (("minimum", lambda n: value < n), ("maximum", lambda n: value > n),
                         ("exclusiveMinimum", lambda n: value <= n), ("exclusiveMaximum", lambda n: value >= n)):
            if key in schema and bad(schema[key]):
                errors.append(f"{path}={value}，要求 {key}={schema[key]}")
    return errors
