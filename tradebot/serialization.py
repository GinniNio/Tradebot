from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


def api_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: api_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [api_value(item) for item in value]
    return value


def json_text(value: Any) -> str:
    """Encode values for asyncpg JSONB parameters without losing exact numerics."""
    return json.dumps(value, default=str, separators=(",", ":"))
