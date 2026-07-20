"""Shared pure helper functions used by multiple zaco sub-modules."""

from __future__ import annotations

import json
from typing import Any

try:
    from .zone_utils import decode_current_point
except ImportError:
    from zone_utils import decode_current_point  # type: ignore[no-redef]


def parse_int_prop(props: dict, key: str) -> int | None:
    """Extract an integer property value from a properties dict."""
    raw = props.get(key, {})
    val = raw.get("value", raw) if isinstance(raw, dict) else raw
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def extract_current_point(props: dict) -> tuple[int, int] | None:
    """Extract and decode CurrentPoint from a properties dict."""
    raw = props.get("RealMapRoadData", {})
    val = raw.get("value", raw) if isinstance(raw, dict) else raw
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(val, dict):
        return None
    cp_raw = val.get("CurrentPoint")
    if cp_raw is None:
        return None
    try:
        return decode_current_point(int(cp_raw))
    except (ValueError, TypeError):
        return None
