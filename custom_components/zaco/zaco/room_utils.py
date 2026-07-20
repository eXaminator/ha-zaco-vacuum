"""Room data parsing utilities for ZACO robot vacuum.

Standalone module with no Home Assistant dependencies. Provides functions
to parse MapRoomInfo and SaveMapDataInfoX9 properties into room names,
bitmask IDs, and center points.

Used by both the HA coordinator and standalone test scripts.
"""

from __future__ import annotations

import base64
import json
import struct
from typing import Any


def parse_map_room_info(
    b64_string: str,
) -> tuple[int | str | None, list[tuple[int, str]]]:
    """Parse a base64-encoded MapRoomInfo string into room ID/name pairs.

    Format: base64(mapId,,roomId1,roomName1,roomId2,roomName2,...)
    Returns (map_id, [(room_id, room_name), ...]).
    """
    try:
        decoded = base64.b64decode(b64_string).decode("utf-8")
    except Exception:
        return None, []

    fields = decoded.split(",")
    if len(fields) < 2:
        return None, []

    try:
        map_id: int | str = int(fields[0])
    except ValueError:
        map_id = fields[0]

    rooms: list[tuple[int, str]] = []
    i = 2
    while i + 1 < len(fields):
        try:
            room_id = int(fields[i])
            room_name = fields[i + 1]
            rooms.append((room_id, room_name))
        except ValueError:
            pass
        i += 2

    return map_id, rooms


def _extract_prop_value(props: dict, key: str) -> Any:
    """Unwrap a property value from the standard {key: {value: ...}} format."""
    raw = props.get(key, {})
    val = raw.get("value", raw) if isinstance(raw, dict) else raw
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (json.JSONDecodeError, ValueError):
            pass
    return val


def _get_selected_map_id(props: dict) -> int | str | None:
    """Extract SelectedMapId from the SaveMap property."""
    save_map = _extract_prop_value(props, "SaveMap")
    if isinstance(save_map, dict):
        return save_map.get("SelectedMapId")
    return None


def parse_map_data_info_x9_centers(
    data_dict: dict,
) -> list[tuple[int, int, int]] | None:
    """Parse SaveMapDataInfoX9 binary data into room center points.

    Takes a dict with MapInfo1-7 base64 chunks (the property value).
    Returns [(bitmask_id, center_x, center_y), ...] or None.

    This is the simplified version -- only extracts bitmask IDs and centers,
    skipping wall point data. The coordinator's _parse_map_info_x9 handles
    the full binary including walls for outline rendering.
    """
    all_bytes = bytearray()
    for i in range(1, 8):
        chunk_b64 = data_dict.get(f"MapInfo{i}", "")
        if not chunk_b64:
            continue
        try:
            all_bytes.extend(base64.b64decode(chunk_b64))
        except Exception:
            continue

    if len(all_bytes) < 5:
        return None

    idx = 4  # skip charger X/Y (bytes 0-3)
    num_rooms = all_bytes[idx] & 0xFF
    idx += 1

    rooms: list[tuple[int, int, int]] = []
    for _ in range(num_rooms):
        if idx + 14 > len(all_bytes):
            break
        bitmask_id = struct.unpack_from(">i", all_bytes, idx)[0]
        idx += 4
        center_x = struct.unpack_from(">h", all_bytes, idx)[0]
        idx += 2
        center_y = -struct.unpack_from(">h", all_bytes, idx)[0]
        idx += 2
        idx += 4  # surround partition
        if idx + 2 > len(all_bytes):
            break
        num_walls = (all_bytes[idx] << 8) | all_bytes[idx + 1]
        idx += 2
        idx += num_walls * 4  # skip wall points

        rooms.append((bitmask_id, center_x, center_y))

    return rooms if rooms else None


def get_room_centers(props: dict) -> dict[str, tuple[int, int]]:
    """Combine MapRoomInfo + SaveMapDataInfoX9 into {room_name: (center_x, center_y)}.

    Works on a raw properties dict (from get_properties or coordinator.data).
    Matches the active map slot using SaveMap.SelectedMapId.
    """
    selected_map_id = _get_selected_map_id(props)

    # Parse MapRoomInfo slots to get bitmask_id -> room_name
    bitmask_to_name: dict[int, str] = {}
    for i in range(1, 4):
        val = _extract_prop_value(props, f"MapRoomInfo{i}")
        if not val or not isinstance(val, str):
            continue
        map_id, rooms = parse_map_room_info(val)
        if not rooms:
            continue
        if selected_map_id is not None and map_id == selected_map_id:
            bitmask_to_name = {rid: name for rid, name in rooms}
            break
        if not bitmask_to_name:
            bitmask_to_name = {rid: name for rid, name in rooms}

    if not bitmask_to_name:
        return {}

    # Parse SaveMapDataInfoX9 slots to get bitmask_id -> (center_x, center_y)
    bitmask_to_center: dict[int, tuple[int, int]] = {}
    for i in range(1, 4):
        val = _extract_prop_value(props, f"SaveMapDataInfoX9_{i}")
        if not isinstance(val, dict):
            continue
        centers = parse_map_data_info_x9_centers(val)
        if centers:
            bitmask_to_center = {bid: (cx, cy) for bid, cx, cy in centers}
            break

    # Combine: room_name -> (center_x, center_y)
    result: dict[str, tuple[int, int]] = {}
    for bitmask_id, name in bitmask_to_name.items():
        center = bitmask_to_center.get(bitmask_id)
        if center:
            result[name] = center
    return result
