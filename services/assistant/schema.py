"""Function-calling parameter schemas built from the API's own Pydantic request models.

The output uses the subset both Gemini and OpenAI-compatible providers accept:
type, description, enum (strings only), items, properties and required.
"""

from typing import Any

from pydantic import BaseModel

_PRIMITIVES = {"string", "integer", "number", "boolean", "array", "object"}


def _resolve(node: dict, defs: dict) -> dict:
    while "$ref" in node:
        node = {**defs[node["$ref"].split("/")[-1]], **{k: v for k, v in node.items() if k != "$ref"}}
    return node


def _hint(node: dict) -> str:
    parts = []
    fmt = node.get("format")
    if fmt == "date":
        parts.append("Date as YYYY-MM-DD.")
    elif fmt == "date-time":
        parts.append("Date and time in ISO 8601.")
    for key, label in (("minimum", "at least"), ("exclusiveMinimum", "greater than"), ("maximum", "at most")):
        if key in node:
            parts.append(f"Must be {label} {node[key]}.")
    if "minLength" in node or "maxLength" in node:
        parts.append(f"Length {node.get('minLength', 0)} to {node.get('maxLength', 'any')} characters.")
    return " ".join(parts)


def convert(node: dict, defs: dict, *, description: str = "") -> dict:
    """Convert one JSON-schema node into the shared function-calling subset."""
    node = _resolve(node, defs)
    for key in ("anyOf", "oneOf"):
        if key in node:
            options = [_resolve(option, defs) for option in node[key] if option.get("type") != "null"]
            merged = {**options[0]} if len(options) == 1 else {"type": "string"}
            for extra in ("description", "title"):
                if extra in node and extra not in merged:
                    merged[extra] = node[extra]
            node = merged
            break
    if "allOf" in node and len(node["allOf"]) == 1:
        node = {**_resolve(node["allOf"][0], defs), **{k: v for k, v in node.items() if k != "allOf"}}

    text = " ".join(part for part in (description or node.get("description", ""), _hint(node)) if part).strip()

    if "enum" in node:
        values = node["enum"]
        if all(isinstance(value, str) for value in values):
            result: dict[str, Any] = {"type": "string", "enum": list(values)}
        else:
            names = node.get("x-enum-names") or []
            listing = ", ".join(
                f"{value} = {names[index]}" if index < len(names) else str(value) for index, value in enumerate(values)
            )
            result = {"type": "integer"}
            text = f"{text} One of: {listing}.".strip()
        if text:
            result["description"] = text
        return result

    kind = node.get("type", "string")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    if kind not in _PRIMITIVES:
        kind = "string"
    result = {"type": kind}
    if kind == "array":
        result["items"] = convert(node.get("items", {"type": "string"}), defs)
    elif kind == "object":
        properties = node.get("properties", {})
        result["properties"] = {name: convert(value, defs) for name, value in properties.items()}
        required = [name for name in node.get("required", []) if name in properties]
        if required:
            result["required"] = required
    if text:
        result["description"] = text
    return result


def _enum_names(schema: dict, model: type[BaseModel]) -> None:
    """Pydantic drops Python Enum member names; keep them so integer codes stay readable."""
    import enum
    import typing

    for field in model.model_fields.values():
        candidates = typing.get_args(field.annotation) or (field.annotation,)
        for candidate in candidates:
            if isinstance(candidate, type) and issubclass(candidate, enum.Enum):
                definition = schema.get("$defs", {}).get(candidate.__name__)
                if definition is not None:
                    definition["x-enum-names"] = [member.name for member in candidate]


def model_parameters(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    _enum_names(schema, model)
    defs = schema.get("$defs", {})
    return convert(schema, defs)


def merge(*objects: dict) -> dict:
    """Join several object schemas into one argument object."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for item in objects:
        properties.update(item.get("properties", {}))
        required.extend(name for name in item.get("required", []) if name not in required)
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def obj(properties: dict[str, dict], required: list[str] | None = None) -> dict:
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def integer(description: str) -> dict:
    return {"type": "integer", "description": description}


def string(description: str, enum: list[str] | None = None) -> dict:
    result: dict[str, Any] = {"type": "string", "description": description}
    if enum:
        result["enum"] = enum
    return result
