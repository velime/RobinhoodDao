"""JSON Schema helpers shared by the tool definitions."""

from __future__ import annotations


def obj(props: dict, required: list[str]) -> dict:
    """Arguments of a tool: an object with exactly these properties."""
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


SYM = {"type": "string", "description": "Тикер без пары: BTC, HYPE, PEPE (можно с $)"}
