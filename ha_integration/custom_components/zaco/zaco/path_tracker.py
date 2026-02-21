"""Cleaning path accumulation and timeline backfill."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

try:
    from .map_renderer import _decode_road_data
except ImportError:
    from map_renderer import _decode_road_data  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)


class PathTracker:
    """Owns cleaning path accumulation and timeline backfill.

    Uses lazy callables to access the API client and iot_id, avoiding
    direct references to the facade.
    """

    def __init__(
        self,
        get_client: Callable[[], Any],
        get_iot_id: Callable[[], str],
    ) -> None:
        self._get_client = get_client
        self._get_iot_id = get_iot_id
        self.accumulated_path: list[tuple[int, int]] = []
        self._cleaning_start_ms: int | None = None
        self._last_road_data_b64: str | None = None

    async def accumulate(self, data: dict[str, Any]) -> None:
        """Accumulate cleaning path from RealMapRoadData."""
        raw = data.get("RealMapRoadData", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                return
        if not isinstance(val, dict):
            return

        road_b64 = val.get("RoadData", "")

        if self._cleaning_start_ms is None:
            road_start_raw = data.get("RealTimeRoadStart", {})
            if isinstance(road_start_raw, dict):
                start_time = road_start_raw.get("time")
                if start_time is not None:
                    self._cleaning_start_ms = int(start_time)

        if not self.accumulated_path and self._cleaning_start_ms:
            await self._backfill_from_timeline()
            self._last_road_data_b64 = road_b64
            return

        if road_b64 and road_b64 != self._last_road_data_b64:
            new_points = _decode_road_data(road_b64)
            if new_points:
                self.accumulated_path.extend(new_points)
            self._last_road_data_b64 = road_b64

    def append_from_mqtt(self, items: dict[str, Any]) -> None:
        """Extract and append RoadData from MQTT push items."""
        raw = items.get("RealMapRoadData", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                return
        if not isinstance(val, dict):
            return
        road_b64 = val.get("RoadData", "")
        if road_b64 and road_b64 != self._last_road_data_b64:
            new_points = _decode_road_data(road_b64)
            if new_points:
                self.accumulated_path.extend(new_points)
            self._last_road_data_b64 = road_b64

    def reset(self) -> None:
        """Clear path on cleaning session end."""
        self.accumulated_path.clear()
        self._cleaning_start_ms = None
        self._last_road_data_b64 = None

    async def _backfill_from_timeline(self) -> None:
        """Fetch the full cleaning path from the timeline API."""
        if self._cleaning_start_ms is None:
            return
        client = self._get_client()
        if client is None:
            return
        iot_id = self._get_iot_id()
        now_ms = int(time.time() * 1000)
        end_ms = now_ms + 60_000
        try:
            items = await client.get_property_timeline(
                iot_id, "RealMapRoadData",
                self._cleaning_start_ms, end_ms,
            )
        except Exception:
            _LOGGER.warning("Timeline backfill failed", exc_info=True)
            return
        if not items:
            return
        path: list[tuple[int, int]] = []
        for item in items:
            item_data = item.get("data")
            if isinstance(item_data, str):
                try:
                    item_data = json.loads(item_data)
                except (json.JSONDecodeError, ValueError):
                    continue
            if not isinstance(item_data, dict):
                continue
            chunk_b64 = item_data.get("RoadData", "")
            if chunk_b64:
                path.extend(_decode_road_data(chunk_b64))
        self.accumulated_path = path
