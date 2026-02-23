"""Cleaning path accumulation and timeline backfill."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import math
import time
from typing import Any, Callable

try:
    from .map_renderer import _decode_road_data
    from ._helpers import parse_int_prop, extract_current_point
except ImportError:
    from map_renderer import _decode_road_data  # type: ignore[no-redef]
    from _helpers import parse_int_prop, extract_current_point  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

MAX_PATH_POINTS = 20_000


def _generate_spot_spiral(cx: int, cy: int) -> list[tuple[int, int]]:
    """Generate spiral points centered on (cx, cy) for spot clean visualization.

    Produces ~3 turns with radius growing from 3 to 7 map units.
    """
    points: list[tuple[int, int]] = []
    for i in range(40):
        angle = i * (2 * math.pi / 13)  # ~3 full turns in 40 steps
        r = 3 + i * 0.1  # radius grows from 3 to ~7
        x = cx + int(r * math.cos(angle))
        y = cy + int(r * math.sin(angle))
        points.append((x, y))
    return points


@dataclass
class CleaningSnapshot:
    """Snapshot of a completed cleaning session."""

    path: list[tuple[int, int]] = field(default_factory=list)
    start_ms: int | None = None
    end_ms: int = 0
    clean_time_min: float = 0.0
    clean_area_m2: float = 0.0


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
        self._spot_clean_injected: bool = False
        self.last_cleaning: CleaningSnapshot | None = None

    async def accumulate(self, data: dict[str, Any]) -> None:
        """Accumulate cleaning path from RealMapRoadData."""
        raw = data.get("RealMapRoadData", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                _LOGGER.debug("PathTracker: failed to parse RealMapRoadData JSON")
                return
        if not isinstance(val, dict):
            return

        road_b64 = val.get("RoadData", "")

        if self._cleaning_start_ms is None:
            self._cleaning_start_ms = int(time.time() * 1000)
            _LOGGER.debug(
                "PathTracker: cleaning start timestamp=%d (wall clock)",
                self._cleaning_start_ms,
            )

        if not self.accumulated_path and self._cleaning_start_ms:
            _LOGGER.debug(
                "PathTracker: empty path, triggering timeline backfill "
                "(start=%d)", self._cleaning_start_ms,
            )
            await self._backfill_from_timeline()
            self._maybe_downsample()
            self._last_road_data_b64 = road_b64
            return

        if road_b64 and road_b64 != self._last_road_data_b64:
            new_points = _decode_road_data(road_b64)
            if new_points:
                self.accumulated_path.extend(new_points)
                _LOGGER.debug(
                    "PathTracker: +%d points, total=%d",
                    len(new_points), len(self.accumulated_path),
                )
                self._maybe_downsample()
            self._last_road_data_b64 = road_b64

        # Inject synthetic spiral when spot cleaning starts (WM=5).
        # The robot spins in place and reports nearly identical coordinates,
        # so we add a visible spiral marker at the spot clean location.
        if not self._spot_clean_injected:
            wm = parse_int_prop(data, "WorkMode")
            if wm == 5:
                pos = extract_current_point(data)
                if pos:
                    spiral = _generate_spot_spiral(pos[0], pos[1])
                    self.accumulated_path.extend(spiral)
                    self._spot_clean_injected = True
                    _LOGGER.debug(
                        "PathTracker: injected spot spiral at (%d,%d), "
                        "+%d points, total=%d",
                        pos[0], pos[1], len(spiral),
                        len(self.accumulated_path),
                    )

    def append_from_mqtt(self, items: dict[str, Any]) -> None:
        """Extract and append RoadData from MQTT push items."""
        raw = items.get("RealMapRoadData", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                _LOGGER.debug("PathTracker MQTT: failed to parse RoadData JSON")
                return
        if not isinstance(val, dict):
            return
        road_b64 = val.get("RoadData", "")
        if road_b64 and road_b64 != self._last_road_data_b64:
            new_points = _decode_road_data(road_b64)
            if new_points:
                self.accumulated_path.extend(new_points)
                _LOGGER.debug(
                    "PathTracker MQTT: +%d points, total=%d",
                    len(new_points), len(self.accumulated_path),
                )
                self._maybe_downsample()
            self._last_road_data_b64 = road_b64

    def _maybe_downsample(self) -> None:
        """Halve the path if it exceeds MAX_PATH_POINTS."""
        if len(self.accumulated_path) > MAX_PATH_POINTS:
            old_len = len(self.accumulated_path)
            self.accumulated_path = self.accumulated_path[::2]
            _LOGGER.debug(
                "PathTracker: downsampled %d -> %d points",
                old_len, len(self.accumulated_path),
            )

    def reset(
        self,
        clean_time_min: float = 0.0,
        clean_area_m2: float = 0.0,
    ) -> None:
        """Snapshot the cleaning session, then clear the path."""
        _LOGGER.debug(
            "PathTracker: reset (was %d points)", len(self.accumulated_path),
        )
        if self.accumulated_path:
            self.last_cleaning = CleaningSnapshot(
                path=list(self.accumulated_path),
                start_ms=self._cleaning_start_ms,
                end_ms=int(time.time() * 1000),
                clean_time_min=clean_time_min,
                clean_area_m2=clean_area_m2,
            )
            _LOGGER.debug(
                "PathTracker: snapshot saved (%d points, %.1f min, %.2f m²)",
                len(self.last_cleaning.path),
                clean_time_min,
                clean_area_m2,
            )
        self.accumulated_path.clear()
        self._cleaning_start_ms = None
        self._last_road_data_b64 = None
        self._spot_clean_injected = False

    async def _backfill_from_timeline(self) -> None:
        """Fetch the full cleaning path from the timeline API."""
        if self._cleaning_start_ms is None:
            return
        client = self._get_client()
        if client is None:
            _LOGGER.debug("PathTracker: backfill skipped, no client")
            return
        iot_id = self._get_iot_id()
        now_ms = int(time.time() * 1000)
        end_ms = now_ms + 60_000
        _LOGGER.debug(
            "PathTracker: backfill start=%d, end=%d",
            self._cleaning_start_ms, end_ms,
        )
        try:
            items = await client.get_property_timeline(
                iot_id, "RealMapRoadData",
                self._cleaning_start_ms, end_ms,
            )
        except Exception:
            _LOGGER.warning("Timeline backfill failed", exc_info=True)
            return
        if not items:
            _LOGGER.debug("PathTracker: backfill returned 0 items")
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
        _LOGGER.debug(
            "PathTracker: backfill done, %d items -> %d points",
            len(items), len(path),
        )
