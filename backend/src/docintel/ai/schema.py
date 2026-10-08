"""JSON schema preparation shared by structured-output providers."""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

_MAX_DEPTH = 32


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace local `$ref`s with their `$defs` entries and drop `$defs`.

    Pydantic emits `$defs`/`$ref` for nested models. Structured-output engines differ in how
    much of JSON Schema they accept (Gemini's subset, llama.cpp grammars behind Ollama); a
    self-contained schema without references works everywhere. Recursive models are not used
    for extraction, so the depth limit only guards against mistakes.
    """
    definitions = schema.get("$defs", {})

    def resolve(node: Any, depth: int) -> Any:
        if depth > _MAX_DEPTH:
            msg = "schema nesting too deep (recursive model?)"
            raise ValueError(msg)
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = copy.deepcopy(definitions[ref.removeprefix("#/$defs/")])
                merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
                return resolve(merged, depth + 1)
            return {k: resolve(v, depth + 1) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(item, depth + 1) for item in node]
        return node

    result: dict[str, Any] = resolve(schema, 0)
    return result


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    return inline_refs(model.model_json_schema())


def strip_code_fence(text: str) -> str:
    """Models sometimes wrap JSON in a Markdown code fence despite JSON mode."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.removesuffix("```").strip()
    return stripped
