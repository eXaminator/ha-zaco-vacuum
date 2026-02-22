"""Map/room state: SLAM grid parsing, room mapping, outlines, current-room detection."""

from __future__ import annotations

import base64
import json
import logging
import struct
from collections import defaultdict
from typing import Any

_LOGGER = logging.getLogger(__name__)

try:
    from .map_renderer import (
        _bytes_to_int16,
        _decode_point_int,
        _decode_slam_grid,
        _parse_json_or_dict,
    )
    from .room_utils import parse_map_room_info
except ImportError:
    from map_renderer import (  # type: ignore[no-redef]
        _bytes_to_int16,
        _decode_point_int,
        _decode_slam_grid,
        _parse_json_or_dict,
    )
    from room_utils import parse_map_room_info  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Polygon helpers
# ---------------------------------------------------------------------------

def _collect_boundary_edges(
    cells: set[tuple[int, int]],
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Collect directed boundary edges for a set of grid cells."""
    edges: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for x, y in cells:
        if (x, y - 1) not in cells:
            edges.append(((x, y), (x + 1, y)))
        if (x + 1, y) not in cells:
            edges.append(((x + 1, y), (x + 1, y + 1)))
        if (x, y + 1) not in cells:
            edges.append(((x + 1, y + 1), (x, y + 1)))
        if (x - 1, y) not in cells:
            edges.append(((x, y + 1), (x, y)))
    return edges


def _chain_edges(
    edges: list[tuple[tuple[int, int], tuple[int, int]]],
) -> list[list[tuple[int, int]]]:
    """Chain directed edges into closed polygon loops."""
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


def _simplify_polygon(polygon: list[tuple[int, int]]) -> list[tuple[int, int]]:
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


def _compute_room_outlines(
    grid_lookup: dict[tuple[int, int], int],
    partition_to_bitmask: dict[int, int],
) -> dict[int, list[tuple[int, int]]]:
    """Compute exact boundary outlines for each room from the SLAM grid."""
    cells_by_room: dict[int, set[tuple[int, int]]] = {}
    for (x, y), partition_id in grid_lookup.items():
        if partition_id in (0, 3, 4):
            continue
        bitmask_id = partition_to_bitmask.get(partition_id)
        if bitmask_id is None:
            continue
        cells_by_room.setdefault(bitmask_id, set()).add((x, y))

    outlines: dict[int, list[tuple[int, int]]] = {}
    for bitmask_id, cells in cells_by_room.items():
        edges = _collect_boundary_edges(cells)
        if not edges:
            continue
        polygons = _chain_edges(edges)
        simplified = [_simplify_polygon(p) for p in polygons]
        simplified = [p for p in simplified if len(p) >= 3]
        if not simplified:
            continue
        simplified.sort(key=_polygon_area, reverse=True)
        outlines[bitmask_id] = simplified[0]

    return outlines


# ---------------------------------------------------------------------------
# MapState
# ---------------------------------------------------------------------------

class MapState:
    """Owns all SLAM grid parsing, room mapping, outline computation,
    and current-room detection.  Purely synchronous — no async I/O."""

    def __init__(self) -> None:
        self.room_map: dict[str, int] = {}
        self.active_map_slot: int | None = None
        self.grid_lookup: dict[tuple[int, int], int] = {}
        self.partition_to_bitmask: dict[int, int] = {}
        self.room_centers: dict[int, tuple[int, int]] = {}
        self.room_walls: dict[int, list[tuple[int, int]]] = {}
        self.room_outlines: dict[int, list[tuple[int, int]]] = {}
        self.current_room: str | None = None
        self._grid_map_id: Any = None

    def parse_rooms(self, data: dict[str, Any]) -> None:
        """Extract room name-to-ID mapping and active map slot."""
        save_map_raw = data.get("SaveMap", {})
        save_map_val = (
            save_map_raw.get("value", save_map_raw)
            if isinstance(save_map_raw, dict) else save_map_raw
        )
        if isinstance(save_map_val, str):
            try:
                save_map_val = json.loads(save_map_val)
            except (json.JSONDecodeError, ValueError):
                _LOGGER.debug("MapState: failed to parse SaveMap JSON")
                pass

        selected_map_id = None
        if isinstance(save_map_val, dict):
            selected_map_id = save_map_val.get("SelectedMapId")

        _LOGGER.debug("MapState: parse_rooms selected_map_id=%s", selected_map_id)

        best_slot = None
        for i in range(1, 4):
            key = f"MapRoomInfo{i}"
            raw = data.get(key, {})
            val = raw.get("value", raw) if isinstance(raw, dict) else raw
            if not val or not isinstance(val, str):
                continue
            map_id, rooms = parse_map_room_info(val)
            if not rooms:
                continue
            if selected_map_id is not None and map_id == selected_map_id:
                self.room_map = {name: room_id for room_id, name in rooms}
                best_slot = i
                break
            if best_slot is None:
                self.room_map = {name: room_id for room_id, name in rooms}
                best_slot = i

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
                    slam_slot = i

        self.active_map_slot = slam_slot if slam_slot else best_slot
        _LOGGER.debug(
            "MapState: rooms=%d (%s), active_slot=%s",
            len(self.room_map), list(self.room_map.keys()), self.active_map_slot,
        )

    def build_grid_lookup(self, data: dict[str, Any]) -> None:
        """Build (x, y) -> partition_id lookup from the active SLAM grid."""
        slot = self.active_map_slot
        if not slot:
            _LOGGER.debug("MapState: build_grid_lookup skipped, no active slot")
            return

        key = f"SaveMapDataX9_{slot}"
        raw = data.get(key, {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        slam_map = _parse_json_or_dict(val)
        if not slam_map or not slam_map.get("MapData1"):
            _LOGGER.debug("MapState: build_grid_lookup skipped, no MapData1 in slot %d", slot)
            return

        map_id = slam_map.get("MapId")
        if map_id == self._grid_map_id and self.grid_lookup:
            _LOGGER.debug("MapState: grid unchanged (map_id=%s), skipping rebuild", map_id)
            return

        _LOGGER.debug("MapState: building grid for map_id=%s (slot %d)", map_id, slot)

        result = _decode_slam_grid(slam_map)
        if result is None:
            _LOGGER.warning("MapState: SLAM grid decode failed for slot %d", slot)
            return

        grid_points, _, _, _, _ = result
        self.grid_lookup = {(x, y): room_type for x, y, room_type in grid_points}
        self._grid_map_id = map_id

        _LOGGER.debug("MapState: grid has %d cells", len(self.grid_lookup))

        room_data = self._parse_map_info_x9(data)
        if room_data:
            mapping: dict[int, int] = {}
            for bitmask_id, cx, cy, walls in room_data:
                slam_pid = self.grid_lookup.get((cx, cy))
                if slam_pid is not None and slam_pid not in (0, 3, 4):
                    mapping[slam_pid] = bitmask_id
                else:
                    found = False
                    for dx in range(-3, 4):
                        for dy in range(-3, 4):
                            pid = self.grid_lookup.get((cx + dx, cy + dy))
                            if pid is not None and pid not in (0, 3, 4):
                                mapping[pid] = bitmask_id
                                found = True
                                break
                        if found:
                            break
            self.partition_to_bitmask = mapping
            self.room_centers = {
                bitmask_id: (cx, cy)
                for bitmask_id, cx, cy, _walls in room_data
            }
            self.room_walls = {
                bitmask_id: walls
                for bitmask_id, _cx, _cy, walls in room_data
                if walls
            }
            _LOGGER.debug(
                "MapState: partition mapping: %d entries, centers: %s",
                len(mapping), {bid: center for bid, center in self.room_centers.items()},
            )
        else:
            _LOGGER.debug("MapState: no room data from SaveMapDataInfoX9_%d", slot)

        self.room_outlines = _compute_room_outlines(
            self.grid_lookup, self.partition_to_bitmask,
        )
        _LOGGER.debug(
            "MapState: computed outlines for %d rooms", len(self.room_outlines),
        )

    def extract_realtime_stats(self, data: dict[str, Any]) -> None:
        """Extract CleanTime/CleanArea from RealMapRoadData."""
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

    def compute_current_room(
        self, data: dict[str, Any], *, is_cleaning: bool, is_docked: bool,
    ) -> None:
        """Determine which room the robot is currently in."""
        if is_docked:
            if self.current_room != "Dock":
                _LOGGER.debug("MapState: current room changed: %s -> Dock (docked)", self.current_room)
            self.current_room = "Dock"
            data["CurrentRoom"] = {"value": "Dock"}
            return

        if not self.grid_lookup or not self.room_map:
            self.current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        if not self.partition_to_bitmask:
            self.current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        raw = data.get("RealMapRoadData", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                self.current_room = None
                data["CurrentRoom"] = {"value": None}
                return
        if not isinstance(val, dict):
            self.current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        cp_raw = val.get("CurrentPoint")
        if cp_raw is None:
            self.current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        try:
            pos = _decode_point_int(int(cp_raw))
        except (ValueError, TypeError):
            self.current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        if pos is None:
            self.current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        x, y = pos
        partition_id = self.grid_lookup.get((x, y))

        if partition_id is None or partition_id in (0, 3, 4):
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    pid = self.grid_lookup.get((x + dx, y + dy))
                    if pid is not None and pid not in (0, 3, 4):
                        partition_id = pid
                        break
                if partition_id is not None and partition_id not in (0, 3, 4):
                    break

        if partition_id is None or partition_id in (0, 3, 4):
            _LOGGER.debug(
                "MapState: pos=(%d,%d) not in any room partition", x, y,
            )
            self.current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        bitmask_id = self.partition_to_bitmask.get(partition_id)
        if bitmask_id is None:
            _LOGGER.debug(
                "MapState: partition %d not mapped to any room", partition_id,
            )
            self.current_room = None
            data["CurrentRoom"] = {"value": None}
            return

        room_name = None
        for name, rid in self.room_map.items():
            if rid == bitmask_id:
                room_name = name
                break

        if room_name != self.current_room:
            _LOGGER.debug(
                "MapState: current room changed: %s -> %s (pos=%d,%d, pid=%d, bid=%d)",
                self.current_room, room_name, x, y, partition_id, bitmask_id,
            )
        self.current_room = room_name
        data["CurrentRoom"] = {"value": room_name}

    def get_room_id(self, name: str) -> int | None:
        """Resolve a room name to its bitmask ID (case-insensitive)."""
        name_lower = name.lower()
        for room_name, room_id in self.room_map.items():
            if room_name.lower() == name_lower:
                return room_id
        return None

    def get_room_center(self, name: str) -> tuple[int, int] | None:
        """Resolve a room name to a navigable point inside the room."""
        room_id = self.get_room_id(name)
        if room_id is None:
            return None
        center = self.room_centers.get(room_id)
        if center is None:
            return None

        cx, cy = center
        expected_partitions = {
            pid for pid, bid in self.partition_to_bitmask.items() if bid == room_id
        }
        if not expected_partitions:
            return center

        pid_at_center = self.grid_lookup.get((cx, cy))
        if pid_at_center in expected_partitions:
            return center

        for radius in range(1, 30):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if abs(dx) != radius and abs(dy) != radius:
                        continue
                    pid = self.grid_lookup.get((cx + dx, cy + dy))
                    if pid in expected_partitions:
                        return (cx + dx, cy + dy)

        return center

    # -- Private --------------------------------------------------------------

    def _parse_map_info_x9(
        self, data: dict[str, Any],
    ) -> list[tuple[int, int, int, list[tuple[int, int]]]] | None:
        """Parse SaveMapDataInfoX9 binary data for the active map slot."""
        slot = self.active_map_slot
        if not slot:
            return None

        key = f"SaveMapDataInfoX9_{slot}"
        raw = data.get(key, {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        info_map = _parse_json_or_dict(val)
        if not info_map:
            _LOGGER.debug("MapState: no SaveMapDataInfoX9_%d data", slot)
            return None

        all_bytes = bytearray()
        for i in range(1, 8):
            chunk_b64 = info_map.get(f"MapInfo{i}", "")
            if not chunk_b64:
                continue
            try:
                all_bytes.extend(base64.b64decode(chunk_b64))
            except Exception:
                _LOGGER.debug("MapState: failed to decode MapInfo%d base64", i)
                continue

        if len(all_bytes) < 5:
            _LOGGER.debug("MapState: MapInfoX9 too short (%d bytes)", len(all_bytes))
            return None

        idx = 4
        num_rooms = all_bytes[idx] & 0xFF
        idx += 1

        _LOGGER.debug(
            "MapState: parsing MapInfoX9 slot %d: %d bytes, %d rooms",
            slot, len(all_bytes), num_rooms,
        )

        rooms: list[tuple[int, int, int, list[tuple[int, int]]]] = []
        for _ in range(num_rooms):
            if idx + 14 > len(all_bytes):
                _LOGGER.debug("MapState: truncated room data at byte %d", idx)
                break
            bitmask_id = struct.unpack_from(">i", all_bytes, idx)[0]
            idx += 4
            center_x = _bytes_to_int16(all_bytes[idx], all_bytes[idx + 1])
            idx += 2
            center_y = -_bytes_to_int16(all_bytes[idx], all_bytes[idx + 1])
            idx += 2
            idx += 4  # surround partition
            if idx + 2 > len(all_bytes):
                break
            num_walls = (all_bytes[idx] << 8) | all_bytes[idx + 1]
            idx += 2
            wall_points: list[tuple[int, int]] = []
            for _ in range(num_walls):
                if idx + 4 > len(all_bytes):
                    break
                wx = _bytes_to_int16(all_bytes[idx], all_bytes[idx + 1])
                wy = -_bytes_to_int16(all_bytes[idx + 2], all_bytes[idx + 3])
                wall_points.append((wx, wy))
                idx += 4
            _LOGGER.debug(
                "MapState: room bid=%d center=(%d,%d) walls=%d",
                bitmask_id, center_x, center_y, len(wall_points),
            )
            rooms.append((bitmask_id, center_x, center_y, wall_points))

        return rooms if rooms else None
