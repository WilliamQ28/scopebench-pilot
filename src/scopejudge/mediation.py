"""Explicit actor-to-judge commentary, separate from executable tool arguments."""

import copy
import dataclasses
import json
from typing import cast

from pydantic_ai import ModelRetry, ToolDefinition

JUSTIFICATION_KEY = "scope_justification"
MAX_JUSTIFICATION_CHARS = 1000


def mediation_tools(tools: list[ToolDefinition]) -> list[ToolDefinition]:
    """Add the same required external justification field in every explicit condition."""
    prepared: list[ToolDefinition] = []
    for tool in tools:
        schema = copy.deepcopy(tool.parameters_json_schema)
        properties = cast("dict[str, object]", schema.setdefault("properties", {}))
        if JUSTIFICATION_KEY in properties:
            raise ValueError("Tool already uses reserved scope_justification metadata")
        properties[JUSTIFICATION_KEY] = {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_JUSTIFICATION_CHARS,
            "description": "Brief external justification of authorization for this action.",
        }
        required = cast("list[str]", schema.setdefault("required", []))
        required.append(JUSTIFICATION_KEY)
        prepared.append(dataclasses.replace(tool, parameters_json_schema=schema))
    return prepared


def split_justification(args: str | dict[str, object]) -> tuple[dict[str, object], str]:
    """Extract validated mediator metadata before native tool validation/execution."""
    parsed: object = json.loads(args) if isinstance(args, str) else args
    if not isinstance(parsed, dict):
        raise ModelRetry("Tool arguments must be an object with scope_justification.")
    cleaned = dict(cast("dict[str, object]", parsed))
    justification = cleaned.pop(JUSTIFICATION_KEY, None)
    if not isinstance(justification, str) or not justification.strip():
        raise ModelRetry("Provide a nonempty scope_justification for this tool call.")
    if len(justification) > MAX_JUSTIFICATION_CHARS:
        raise ModelRetry("scope_justification must be at most 1000 characters.")
    return cleaned, justification


def evidence_history(history: object) -> object:
    """Keep tool calls/results, excluding actor prose/thinking and mediator claims."""
    messages = cast("list[dict[str, object]]", history)
    output: list[dict[str, object]] = []
    for message in messages:
        parts: list[dict[str, object]] = []
        for part in cast("list[dict[str, object]]", message.get("parts", [])):
            kind = part.get("part_kind")
            if kind not in {"tool-call", "tool-return"}:
                continue
            kept = dict(part)
            if kind == "tool-call":
                args = kept.get("args")
                if isinstance(args, str):
                    # Invalid calls are marked without exposing unparsed commentary.
                    try:
                        decoded = cast("object", json.loads(args))
                    except ValueError:
                        decoded = None
                        kept["args"] = {"invalid_arguments": True}
                    if isinstance(decoded, dict):
                        args = cast("dict[str, object]", decoded)
                if isinstance(args, dict):
                    cleaned = dict(cast("dict[str, object]", args))
                    _ = cleaned.pop(JUSTIFICATION_KEY, None)
                    kept["args"] = cleaned
            parts.append(kept)
        if parts:
            output.append({**message, "parts": parts})
    return output
