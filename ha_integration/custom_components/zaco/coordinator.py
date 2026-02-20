"""DataUpdateCoordinator for ZACO integration."""

from __future__ import annotations

import base64
import json
import logging
import struct
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_client import (
    AliyunApiClient,
    AliyunConnectionError,
    AliyunTokenExpiredError,
)
from .const import (
    ALL_PROPERTIES,
    DEFAULT_SCAN_INTERVAL,
    FAST_POLL_INTERVAL,
    FAST_PROPERTIES,
    WORKMODE_CLEANING,
    WORKMODE_PAUSED,
    WORKMODE_RETURNING,
)
from .map_renderer import (
    _bytes_to_int16,
    _decode_point_int,
    _decode_road_data,
    _decode_slam_grid,
    _parse_json_or_dict,
)

_LOGGER = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 3  # tolerate transient failures before marking unavailable


def _collect_boundary_edges(
    cells: set[tuple[int, int]],
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Collect directed boundary edges for a set of grid cells.

    Each cell (x, y) is a unit square from (x, y) to (x+1, y+1).
    An edge is emitted when a neighbor is absent from the cell set.
    Edges are oriented clockwise (room interior on the right side).
    """
    edges: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for x, y in cells:
        if (x, y - 1) not in cells:  # top
            edges.append(((x, y), (x + 1, y)))
        if (x + 1, y) not in cells:  # right
            edges.append(((x + 1, y), (x + 1, y + 1)))
        if (x, y + 1) not in cells:  # bottom
            edges.append(((x + 1, y + 1), (x, y + 1)))
        if (x - 1, y) not in cells:  # left
            edges.append(((x, y + 1), (x, y)))
    return edges


def _chain_edges(
    edges: list[tuple[tuple[int, int], tuple[int, int]]],
) -> list[list[tuple[int, int]]]:
    """Chain directed edges into closed polygon loops."""
    from collections import defaultdict

    outgoing: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for start, end in edges:
        outgoing[start].append(end)

    visited: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    polygons: list[list[tuple[int, int]]] = []

    for start, end in edges:
        if (start, end) in visited:
            continue

        poly = [start]
        visited.add((start, end))
        current = end

        while current != start:
            poly.append(current)
            # Pick an unvisited outgoing edge
            found = False
            for candidate in outgoing[current]:
                if (current, candidate) not in visited:
                    visited.add((current, candidate))
                    current = candidate
                    found = True
                    break
            if not found:
                break

        if len(poly) >= 3:
            polygons.append(poly)

    return polygons


def _simplify_polygon(
    polygon: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Remove collinear intermediate vertices from an axis-aligned polygon."""
    n = len(polygon)
    if n < 3:
        return polygon
    result: list[tuple[int, int]] = []
    for i in range(n):
        prev = polygon[(i - 1) % n]
        curr = polygon[i]
        nxt = polygon[(i + 1) % n]
        if (prev[0] == curr[0] == nxt[0]) or (prev[1] == curr[1] == nxt[1]):
            continue
        result.append(curr)
    return result


def _polygon_area(polygon: list[tuple[int, int]]) -> float:
    """Compute absolute area via shoelace formula."""
    n = len(polygon)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += polygon[i][0] * polygon[j][1]
        area -= polygon[j][0] * polygon[i][1]
    return abs(area) / 2.0


class ZacoDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls all device properties."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AliyunApiClient,
        iot_id: str,
        device_info: dict[str, Any],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"zaco_{iot_id}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.iot_id = iot_id
        self.device_info = device_info
        self._room_map: dict[str, int] = {}
        self._active_map_slot: int | None = None
        # SLAM grid lookup for current-room detection
        self._grid_lookup: dict[tuple[int, int], int] = {}
        self._grid_map_id: Any = None  # track which MapId the grid was built from
        self._partition_to_bitmask: dict[int, int] = {}  # slam_partition_id → room bitmask_id
        self._room_centers: dict[int, tuple[int, int]] = {}  # bitmask_id → (x, y)
        self._room_walls: dict[int, list[tuple[int, int]]] = {}  # bitmask_id → wall outline
        self._room_outlines: dict[int, list[tuple[int, int]]] = {}  # bitmask_id → boundary polygon
        self._current_room: str | None = None
        # Room selection state (for dashboard room cleaning)
        self._selected_rooms: set[int] = set()  # set of room bitmask IDs
        self._cleaning_passes: int = 1
        self._was_active: bool = False
        self._last_full_poll: float = 0.0
        self._consecutive_errors: int = 0
        # Accumulated cleaning path (grows during active cleaning)
        self._accumulated_path: list[tuple[int, int]] = []
        self._cleaning_start_ms: int | None = None
        self._last_road_data_b64: str | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch device properties with two-tier polling.

        During active cleaning: fetch FAST_PROPERTIES every 3s (robot position,
        battery, work mode) and ALL_PROPERTIES every 30s (SLAM grid, rooms).
        When idle: always fetch ALL_PROPERTIES at 30s intervals.
        """
        try:
            await self.client.ensure_token_valid()
        except AliyunTokenExpiredError as err:
            raise ConfigEntryAuthFailed(
                "Authentication expired, please reconfigure"
            ) from err

        now = time.monotonic()
        needs_full = (
            not self._was_active
            or now - self._last_full_poll >= DEFAULT_SCAN_INTERVAL
            or self.data is None
        )

        try:
            if needs_full:
                data = await self.client.get_properties(
                    self.iot_id, ALL_PROPERTIES
                )
                self._last_full_poll = now
            else:
                fast_data = await self.client.get_properties(
                    self.iot_id, FAST_PROPERTIES
                )
                # Merge fast data into previous full data so SLAM grid,
                # room info, etc. are preserved for camera / room detection
                data = dict(self.data) if self.data else {}
                data.update(fast_data)
        except AliyunConnectionError as err:
            self._consecutive_errors += 1
            if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                raise UpdateFailed(
                    f"Connection error ({self._consecutive_errors} consecutive failures): {err}"
                ) from err
            _LOGGER.debug(
                "Connection error (attempt %d/%d), will retry: %s",
                self._consecutive_errors,
                MAX_CONSECUTIVE_ERRORS,
                err,
            )
            if self.data is not None:
                return self.data
            raise UpdateFailed(f"Connection error: {err}") from err

        self._consecutive_errors = 0

        if data is None:
            raise UpdateFailed("Failed to get device properties")

        # Parse rooms and grid only on full polls (they need map data)
        if needs_full:
            self._parse_rooms(data)
            self._build_grid_lookup(data)

        self._extract_realtime_stats(data)

        # Determine activity state
        work_mode_raw = data.get("WorkMode", {})
        work_mode = (
            work_mode_raw.get("value", work_mode_raw)
            if isinstance(work_mode_raw, dict)
            else work_mode_raw
        )
        active_modes = WORKMODE_CLEANING | WORKMODE_PAUSED | WORKMODE_RETURNING
        is_active = work_mode is not None and int(work_mode) in active_modes

        _LOGGER.debug("WorkMode raw=%s, is_active=%s, full_poll=%s", work_mode, is_active, needs_full)

        # Compute current room only while actively cleaning
        is_cleaning = work_mode is not None and int(work_mode) in WORKMODE_CLEANING
        self._compute_current_room(data, is_cleaning)

        if is_active:
            self.update_interval = timedelta(seconds=FAST_POLL_INTERVAL)
        else:
            self.update_interval = timedelta(seconds=DEFAULT_SCAN_INTERVAL)

        # --- Path accumulation ---
        if is_active:
            await self._accumulate_path(data)
        data["_accumulated_path"] = list(self._accumulated_path)

        # Auto-reset room selections and path when cleaning finishes
        if self._was_active and not is_active:
            self._selected_rooms.clear()
            self._cleaning_passes = 1
            self._accumulated_path.clear()
            self._cleaning_start_ms = None
            self._last_road_data_b64 = None
            data["_accumulated_path"] = []
        self._was_active = is_active

        return data

    def _parse_rooms(self, data: dict[str, Any]) -> None:
        """Extract room name-to-ID mapping and active map slot.

        SaveMap.SelectedMapId is a MapId (e.g. 1734269369), NOT a slot index.
        We match it against the MapId decoded from each MapRoomInfo slot and
        the MapId inside each SaveMapDataX9 slot.
        """
        # Determine selected MapId from SaveMap
        save_map_raw = data.get("SaveMap", {})
        save_map_val = save_map_raw.get("value", save_map_raw) if isinstance(save_map_raw, dict) else save_map_raw
        if isinstance(save_map_val, str):
            try:
                save_map_val = json.loads(save_map_val)
            except (json.JSONDecodeError, ValueError):
                pass

        selected_map_id = None
        if isinstance(save_map_val, dict):
            selected_map_id = save_map_val.get("SelectedMapId")

        # Parse each MapRoomInfo slot, match by MapId
        best_slot = None
        for i in range(1, 4):
            key = f"MapRoomInfo{i}"
            raw = data.get(key, {})
            val = raw.get("value", raw) if isinstance(raw, dict) else raw
            if not val or not isinstance(val, str):
                continue

            map_id, rooms = AliyunApiClient.parse_map_room_info(val)
            if not rooms:
                continue

            # Match by MapId, or use first slot with rooms as fallback
            if selected_map_id is not None and map_id == selected_map_id:
                self._room_map = {name: room_id for room_id, name in rooms}
                best_slot = i
                break
            if best_slot is None:
                self._room_map = {name: room_id for room_id, name in rooms}
                best_slot = i

        # Determine active SLAM map slot by matching SelectedMapId to
        # SaveMapDataX9_N.MapId
        slam_slot = None
        for i in range(1, 4):
            key = f"SaveMapDataX9_{i}"
            raw = data.get(key, {})
            val = raw.get("value", raw) if isinstance(raw, dict) else raw
            if isinstance(val, dict):
                map_id = val.get("MapId")
                if selected_map_id is not None and map_id == selected_map_id:
                    slam_slot = i
                    break
                if slam_slot is None and val.get("MapData1"):
                    slam_slot = i  # fallback: first slot with data

        self._active_map_slot = slam_slot if slam_slot else best_slot

    def _extract_realtime_stats(self, data: dict[str, Any]) -> None:
        """Extract CleanTime/CleanArea from RealMapRoadData.

        The device embeds real-time cleaning stats inside the RealMapRoadData
        property (alongside RoadData, CurrentPoint, etc.). The top-level
        CleanTime/CleanArea properties are stale or absent on many firmware
        versions, so we extract from RealMapRoadData and inject as synthetic
        top-level keys so the sensor entities pick them up.

        Units from APK (BaseMapPresenter): CleanTime in seconds (÷60 → min),
        CleanArea in cm² (÷100 → m²).
        """
        raw = data.get("RealMapRoadData", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                return
        if not isinstance(val, dict):
            return

        clean_time_s = val.get("CleanTime")
        clean_area_raw = val.get("CleanArea")

        if clean_time_s is not None and int(clean_time_s) > 0:
            data["CleanTime"] = {"value": round(int(clean_time_s) / 60, 1)}

        if clean_area_raw is not None and int(clean_area_raw) > 0:
            data["CleanArea"] = {"value": round(int(clean_area_raw) / 100, 2)}

    async def _accumulate_path(self, data: dict[str, Any]) -> None:
        """Accumulate cleaning path from RealMapRoadData during active cleaning.

        On the first call of a cleaning session (no accumulated path yet),
        uses the timeline API to backfill the full path since cleaning started.
        On subsequent calls, appends only the new RoadData chunk.
        """
        # Extract the current RoadData chunk
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

        # Determine cleaning start time from RealTimeRoadStart
        if self._cleaning_start_ms is None:
            road_start_raw = data.get("RealTimeRoadStart", {})
            if isinstance(road_start_raw, dict):
                # The "time" field is the server timestamp (ms) when the
                # property was last updated — this marks cleaning start.
                start_time = road_start_raw.get("time")
                if start_time is not None:
                    self._cleaning_start_ms = int(start_time)

        # First poll of a cleaning session — backfill from timeline API
        if not self._accumulated_path and self._cleaning_start_ms:
            await self._backfill_path_from_timeline()
            # After backfill, mark the current chunk as seen so we don't
            # duplicate it on the next incremental append
            self._last_road_data_b64 = road_b64
            return

        # Incremental: append new chunk if it differs from the last one
        if road_b64 and road_b64 != self._last_road_data_b64:
            new_points = _decode_road_data(road_b64)
            if new_points:
                self._accumulated_path.extend(new_points)
                _LOGGER.debug(
                    "Appended %d path points (total: %d)",
                    len(new_points),
                    len(self._accumulated_path),
                )
            self._last_road_data_b64 = road_b64

    async def _backfill_path_from_timeline(self) -> None:
        """Fetch the full cleaning path from the timeline API.

        Called once at the start of a cleaning session (or on HA restart
        while cleaning is in progress) to recover the complete path.
        """
        if self._cleaning_start_ms is None:
            return

        now_ms = int(time.time() * 1000)
        end_ms = now_ms + 60_000  # +60s margin, same as ZACO app

        _LOGGER.debug(
            "Backfilling path from timeline: start=%d, end=%d",
            self._cleaning_start_ms,
            end_ms,
        )

        try:
            items = await self.client.get_property_timeline(
                self.iot_id, "RealMapRoadData",
                self._cleaning_start_ms, end_ms,
            )
        except Exception:
            _LOGGER.warning("Timeline backfill failed", exc_info=True)
            return

        if not items:
            _LOGGER.debug("Timeline backfill: no items returned")
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

        self._accumulated_path = path
        _LOGGER.debug(
            "Timeline backfill: %d items → %d total path points",
            len(items),
            len(path),
        )

    def _build_grid_lookup(self, data: dict[str, Any]) -> None:
        """Build (x, y) → partition_id lookup from the active SLAM grid.

        Also parses SaveMapDataInfoX9 to build the slam_partition_id → bitmask_id
        mapping by looking up each room's center point in the grid.

        Only rebuilds when the map data changes (tracked by MapId).
        """
        slot = self._active_map_slot
        if not slot:
            return

        key = f"SaveMapDataX9_{slot}"
        raw = data.get(key, {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        slam_map = _parse_json_or_dict(val)
        if not slam_map or not slam_map.get("MapData1"):
            return

        # Check if map changed
        map_id = slam_map.get("MapId")
        if map_id == self._grid_map_id and self._grid_lookup:
            return  # already built for this map

        result = _decode_slam_grid(slam_map)
        if result is None:
            return

        grid_points, _, _, _, _ = result
        self._grid_lookup = {(x, y): room_type for x, y, room_type in grid_points}
        self._grid_map_id = map_id

        # Build partition-to-bitmask mapping from SaveMapDataInfoX9
        room_data = self._parse_map_info_x9(data)
        if room_data:
            mapping: dict[int, int] = {}
            for bitmask_id, cx, cy, walls in room_data:
                # Look up the SLAM partition at the room center point
                slam_pid = self._grid_lookup.get((cx, cy))
                if slam_pid is not None and slam_pid not in (0, 3, 4):
                    mapping[slam_pid] = bitmask_id
                else:
                    # Center might not land exactly on grid; search nearby
                    found = False
                    for dx in range(-3, 4):
                        for dy in range(-3, 4):
                            pid = self._grid_lookup.get((cx + dx, cy + dy))
                            if pid is not None and pid not in (0, 3, 4):
                                mapping[pid] = bitmask_id
                                found = True
                                break
                        if found:
                            break
            self._partition_to_bitmask = mapping
            self._room_centers = {
                bitmask_id: (cx, cy)
                for bitmask_id, cx, cy, _walls in room_data
            }
            self._room_walls = {
                bitmask_id: walls
                for bitmask_id, _cx, _cy, walls in room_data
                if walls
            }
            _LOGGER.debug(
                "Built partition→bitmask mapping: %s", self._partition_to_bitmask
            )

        # Compute room outlines from SLAM grid cells
        self._compute_room_outlines()

    def _compute_room_outlines(self) -> None:
        """Compute exact boundary outlines for each room from the SLAM grid.

        Traces the outer boundary of each room's grid cells as an ordered
        polygon. Each cell (x, y) occupies a 1x1 unit square, so the
        boundary edges connect grid-corner vertices. The result is a
        simplified polygon (collinear points removed) that exactly matches
        the room shape rendered in the map image.
        """
        if not self._grid_lookup or not self._partition_to_bitmask:
            return

        # Collect grid cells per bitmask_id
        cells_by_room: dict[int, set[tuple[int, int]]] = {}
        for (x, y), partition_id in self._grid_lookup.items():
            if partition_id in (0, 3, 4):
                continue
            bitmask_id = self._partition_to_bitmask.get(partition_id)
            if bitmask_id is None:
                continue
            cells_by_room.setdefault(bitmask_id, set()).add((x, y))

        outlines: dict[int, list[tuple[int, int]]] = {}
        for bitmask_id, cells in cells_by_room.items():
            edges = _collect_boundary_edges(cells)
            if not edges:
                continue
            polygons = _chain_edges(edges)
            # Simplify each polygon and pick the largest (outer boundary)
            simplified = [_simplify_polygon(p) for p in polygons]
            simplified = [p for p in simplified if len(p) >= 3]
            if not simplified:
                continue
            simplified.sort(key=_polygon_area, reverse=True)
            outlines[bitmask_id] = simplified[0]

        self._room_outlines = outlines
        _LOGGER.debug(
            "Computed room outlines: %s",
            {bid: len(pts) for bid, pts in outlines.items()},
        )

    def _parse_map_info_x9(
        self, data: dict[str, Any]
    ) -> list[tuple[int, int, int, list[tuple[int, int]]]] | None:
        """Parse SaveMapDataInfoX9 binary data for the active map slot.

        Returns list of (bitmask_id, center_x, center_y, wall_points) per room,
        or None.  wall_points is a list of (x, y) pairs defining the room outline.

        Binary format (from DataUtils.parseSaveMapInfoX9):
          Bytes 0-1:  charger X (int16 BE signed)
          Bytes 2-3:  charger Y (int16 BE signed, negated)
          Byte 4:     number of rooms
          Per room:
            Bytes 0-3:   bitmask_id (int32 BE)
            Bytes 4-5:   center X (int16 BE signed)
            Bytes 6-7:   center Y (int16 BE signed, negated)
            Bytes 8-11:  surround_partition (int32 BE) — skip
            Bytes 12-13: num_wall_points (int16 BE unsigned)
            Per wall point: 2 bytes X (int16 BE) + 2 bytes Y (int16 BE, negated)
        """
        slot = self._active_map_slot
        if not slot:
            return None

        key = f"SaveMapDataInfoX9_{slot}"
        raw = data.get(key, {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        info_map = _parse_json_or_dict(val)
        if not info_map:
            return None

        # Concatenate MapInfo1-7 base64 chunks (same pattern as MapData1-7)
        all_bytes = bytearray()
        for i in range(1, 8):
            chunk_b64 = info_map.get(f"MapInfo{i}", "")
            if not chunk_b64:
                continue
            try:
                all_bytes.extend(base64.b64decode(chunk_b64))
            except Exception:
                continue

        if len(all_bytes) < 5:
            return None

        # Parse header: charger point + room count
        idx = 4  # skip charger X/Y (bytes 0-3)
        num_rooms = all_bytes[idx] & 0xFF
        idx += 1

        rooms: list[tuple[int, int, int, list[tuple[int, int]]]] = []
        for _ in range(num_rooms):
            if idx + 14 > len(all_bytes):
                break

            # bitmask_id (4 bytes, big-endian int32)
            bitmask_id = struct.unpack_from(">i", all_bytes, idx)[0]
            idx += 4

            # center X (int16 BE signed)
            center_x = _bytes_to_int16(all_bytes[idx], all_bytes[idx + 1])
            idx += 2

            # center Y (int16 BE signed, negated)
            center_y = -_bytes_to_int16(all_bytes[idx], all_bytes[idx + 1])
            idx += 2

            # surround partition (4 bytes) — skip
            idx += 4

            # num wall points (int16 BE unsigned)
            if idx + 2 > len(all_bytes):
                break
            num_walls = (all_bytes[idx] << 8) | all_bytes[idx + 1]
            idx += 2

            # Parse wall boundary points
            wall_points: list[tuple[int, int]] = []
            for _ in range(num_walls):
                if idx + 4 > len(all_bytes):
                    break
                wx = _bytes_to_int16(all_bytes[idx], all_bytes[idx + 1])
                wy = -_bytes_to_int16(all_bytes[idx + 2], all_bytes[idx + 3])
                wall_points.append((wx, wy))
                idx += 4

            rooms.append((bitmask_id, center_x, center_y, wall_points))

        if rooms:
            _LOGGER.debug(
                "Parsed SaveMapDataInfoX9_%s: %d rooms — %s",
                slot,
                len(rooms),
                [(bid, cx, cy, len(wp)) for bid, cx, cy, wp in rooms],
            )

        return rooms if rooms else None

    def _compute_current_room(
        self, data: dict[str, Any], is_cleaning: bool
    ) -> None:
        """Determine which room the robot is currently in.

        Only reports a room while actively cleaning; otherwise sets to None.
        Uses the robot's CurrentPoint from RealMapRoadData, looks it up in the
        SLAM grid, converts the SLAM partition ID to a bitmask ID via the
        partition_to_bitmask mapping, then resolves the room name.
        """
        if not is_cleaning or not self._grid_lookup or not self._room_map:
            self._current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        if not self._partition_to_bitmask:
            self._current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        # Extract CurrentPoint from RealMapRoadData
        raw = data.get("RealMapRoadData", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                self._current_room = None
                data["CurrentRoom"] = {"value": None}
                return
        if not isinstance(val, dict):
            self._current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        cp_raw = val.get("CurrentPoint")
        if cp_raw is None:
            self._current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        try:
            pos = _decode_point_int(int(cp_raw))
        except (ValueError, TypeError):
            self._current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        if pos is None:
            self._current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        x, y = pos
        partition_id = self._grid_lookup.get((x, y))

        if partition_id is None or partition_id in (0, 3, 4):
            # Not on a room cell — try nearby cells (robot may be between cells)
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    pid = self._grid_lookup.get((x + dx, y + dy))
                    if pid is not None and pid not in (0, 3, 4):
                        partition_id = pid
                        break
                if partition_id is not None and partition_id not in (0, 3, 4):
                    break

        if partition_id is None or partition_id in (0, 3, 4):
            self._current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        # Convert SLAM partition_id to room bitmask_id via dynamic mapping
        bitmask_id = self._partition_to_bitmask.get(partition_id)
        if bitmask_id is None:
            self._current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        # Look up room name (room_map is name→bitmask, so reverse lookup)
        room_name = None
        for name, rid in self._room_map.items():
            if rid == bitmask_id:
                room_name = name
                break

        self._current_room = room_name
        data["CurrentRoom"] = {"value": room_name}

    @property
    def current_room(self) -> str | None:
        """Return the name of the room the robot is currently in."""
        return self._current_room

    def get_room_id_by_name(self, name: str) -> int | None:
        """Resolve a room name to its bitmask ID (case-insensitive)."""
        name_lower = name.lower()
        for room_name, room_id in self._room_map.items():
            if room_name.lower() == name_lower:
                return room_id
        return None

    def get_room_center_by_name(self, name: str) -> tuple[int, int] | None:
        """Resolve a room name to a navigable point inside the room.

        Uses the firmware-provided center point from SaveMapDataInfoX9.
        If the center falls outside the room (possible for L-shaped rooms),
        searches outward in the SLAM grid for the nearest cell belonging
        to the room.
        """
        room_id = self.get_room_id_by_name(name)
        if room_id is None:
            return None
        center = self._room_centers.get(room_id)
        if center is None:
            return None

        cx, cy = center
        expected_partitions = {
            pid for pid, bid in self._partition_to_bitmask.items() if bid == room_id
        }
        if not expected_partitions:
            return center  # no grid data, trust firmware

        pid_at_center = self._grid_lookup.get((cx, cy))
        if pid_at_center in expected_partitions:
            return center  # center is inside the room

        # Center is outside room — search outward for nearest room cell
        for radius in range(1, 30):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if abs(dx) != radius and abs(dy) != radius:
                        continue  # only check perimeter of each ring
                    pid = self._grid_lookup.get((cx + dx, cy + dy))
                    if pid in expected_partitions:
                        return (cx + dx, cy + dy)

        return center  # fallback to firmware center

    @property
    def active_map_slot(self) -> int | None:
        """Return the active map slot (1, 2, or 3)."""
        return self._active_map_slot

    def get_clean_settings_bytes(self) -> bytearray | None:
        """Decode CleanSettings.DefaultSetting into a 10-byte array.

        The DefaultSetting is a base64-encoded 10-byte blob where each byte
        represents a different cleaning parameter (from NormalCleanSettings.java):
          Byte 1: suction/fan power (0-100%)
          Byte 2: water level (0-3)
          Byte 3: side brush speed (0-100%)
          Byte 5: main brush speed (0-100%)
          Byte 6: wheel speed
        """
        if self.data is None:
            return None
        raw = self.data.get("CleanSettings", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                return None
        if not isinstance(val, dict):
            return None
        default_b64 = val.get("DefaultSetting", "")
        if not default_b64:
            return None
        try:
            data = bytearray(base64.b64decode(default_b64))
        except Exception:
            return None
        # Pad to 10 bytes if shorter (matching getDefaultBytesSafety())
        while len(data) < 10:
            data.append(0)
        return data

    async def async_set_clean_setting(self, byte_index: int, value: int) -> None:
        """Modify a single byte in CleanSettings.DefaultSetting and write back.

        Implements the read-modify-write pattern from CustomCleanFragment.java:
        decode bytes → modify target byte → re-encode to base64 → set property.
        """
        settings = self.get_clean_settings_bytes()
        if settings is None:
            return
        settings[byte_index] = value & 0xFF
        new_b64 = base64.b64encode(bytes(settings)).decode("ascii")

        # Read current CleanSettings to preserve other fields (MapId, etc.)
        raw = self.data.get("CleanSettings", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                val = {}
        if not isinstance(val, dict):
            val = {}

        val["DefaultSetting"] = new_b64
        await self.client.set_properties(self.iot_id, {"CleanSettings": val})
        await self.async_request_refresh()

    @property
    def rooms(self) -> dict[str, int]:
        """Return current room name → bitmask ID mapping."""
        return dict(self._room_map)

    # -- Room selection state for dashboard cleaning --------------------------

    def is_room_selected(self, room_id: int) -> bool:
        """Check if a room is selected for cleaning."""
        return room_id in self._selected_rooms

    def select_room(self, room_id: int) -> None:
        """Mark a room as selected for cleaning."""
        self._selected_rooms.add(room_id)
        self.async_set_updated_data(self.data)

    def deselect_room(self, room_id: int) -> None:
        """Unmark a room from the cleaning selection."""
        self._selected_rooms.discard(room_id)
        self.async_set_updated_data(self.data)

    @property
    def selected_room_ids(self) -> set[int]:
        """Return the set of selected room bitmask IDs."""
        return set(self._selected_rooms)

    @property
    def cleaning_passes(self) -> int:
        """Return the configured number of cleaning passes."""
        return self._cleaning_passes

    @cleaning_passes.setter
    def cleaning_passes(self, value: int) -> None:
        """Set the number of cleaning passes (1-3)."""
        self._cleaning_passes = min(max(value, 1), 3)
        self.async_set_updated_data(self.data)
