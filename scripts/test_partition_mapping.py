#!/usr/bin/env python3
"""Test script to verify SaveMapDataInfoX9 parsing and partition→room mapping.

Fetches SaveMapDataInfoX9, SaveMapDataX9, and MapRoomInfo from the device,
parses the binary room info, builds the SLAM partition→bitmask mapping by
looking up each room's center point in the SLAM grid, and shows the result.

Usage:
    python3 scripts/test_partition_mapping.py
"""

import base64
import json
import struct
import sys
from pathlib import Path

# Add project root so we can import from scripts
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_aliyun import AliyunIoTClient, load_tokens, parse_map_room_info

# Reuse map_renderer's SLAM decoder
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ha_integration" / "custom_components" / "zaco"))
from map_renderer import _bytes_to_int16, _decode_slam_grid, _decode_point_int


DEFAULT_IOT_ID = "KReBFAPbEXU5Yk31mDep000000"


def parse_map_info_x9_binary(info_map: dict) -> list[tuple[int, int, int]]:
    """Parse SaveMapDataInfoX9 binary data.

    Returns list of (bitmask_id, center_x, center_y) per room.
    """
    # Concatenate MapInfo1-7 base64 chunks
    all_bytes = bytearray()
    for i in range(1, 8):
        chunk_b64 = info_map.get(f"MapInfo{i}", "")
        if not chunk_b64:
            continue
        all_bytes.extend(base64.b64decode(chunk_b64))

    if len(all_bytes) < 5:
        print(f"  Binary data too short: {len(all_bytes)} bytes")
        return []

    print(f"  Total binary data: {len(all_bytes)} bytes")
    print(f"  First 20 bytes: {all_bytes[:20].hex()}")

    # Parse charger point
    charger_x = _bytes_to_int16(all_bytes[0], all_bytes[1])
    charger_y = -_bytes_to_int16(all_bytes[2], all_bytes[3])
    print(f"  Charger point: ({charger_x}, {charger_y})")

    # Number of rooms
    num_rooms = all_bytes[4] & 0xFF
    print(f"  Number of rooms: {num_rooms}")
    idx = 5

    rooms = []
    for room_idx in range(num_rooms):
        if idx + 14 > len(all_bytes):
            print(f"  Room {room_idx}: incomplete data at offset {idx}")
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

        # surround partition (4 bytes)
        surround = struct.unpack_from(">i", all_bytes, idx)[0]
        idx += 4

        # num wall points (2 bytes)
        if idx + 2 > len(all_bytes):
            print(f"  Room {room_idx}: incomplete wall data at offset {idx}")
            break
        num_walls = (all_bytes[idx] << 8) | all_bytes[idx + 1]
        idx += 2

        print(f"  Room {room_idx}: bitmask={bitmask_id}, center=({center_x},{center_y}), "
              f"surround={surround}, walls={num_walls}")

        # skip wall point data (4 bytes per point)
        idx += num_walls * 4

        rooms.append((bitmask_id, center_x, center_y))

    return rooms


def main():
    # Load saved tokens
    saved = load_tokens()
    if not saved:
        print("No saved tokens. Run test_aliyun.py --username ... first.")
        sys.exit(1)

    client = AliyunIoTClient(verbose=False)
    client.iot_token = saved.get("iotToken")
    client.refresh_token = saved.get("refreshToken")
    client.identity_id = saved.get("identityId")
    saved_host = saved.get("host")
    if saved_host:
        client.host = saved_host

    if saved.get("_iot_expired") and not saved.get("_refresh_expired"):
        if not client.refresh_session():
            print("Token refresh failed.")
            sys.exit(1)

    iot_id = DEFAULT_IOT_ID

    # Fetch all needed properties
    props = [
        "SaveMap",
        "MapRoomInfo1", "MapRoomInfo2", "MapRoomInfo3",
        "SaveMapDataX9_1", "SaveMapDataX9_2", "SaveMapDataX9_3",
        "SaveMapDataInfoX9_1", "SaveMapDataInfoX9_2", "SaveMapDataInfoX9_3",
        "RealMapRoadData",
    ]
    data = client.get_properties(iot_id, props)
    if not data:
        print("Failed to get properties.")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("PARTITION MAPPING TEST")
    print("=" * 60)

    # 1. Determine active map slot
    save_map_raw = data.get("SaveMap", {})
    save_map_val = save_map_raw.get("value", save_map_raw) if isinstance(save_map_raw, dict) else save_map_raw
    if isinstance(save_map_val, str):
        save_map_val = json.loads(save_map_val)
    selected_map_id = save_map_val.get("SelectedMapId") if isinstance(save_map_val, dict) else None
    print(f"\nSelectedMapId: {selected_map_id}")

    # Find active slot
    active_slot = None
    for i in range(1, 4):
        key = f"SaveMapDataX9_{i}"
        raw = data.get(key, {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except:
                continue
        if isinstance(val, dict):
            mid = val.get("MapId")
            if mid == selected_map_id:
                active_slot = i
                break
            if active_slot is None and val.get("MapData1"):
                active_slot = i

    print(f"Active SLAM slot: {active_slot}")

    # 2. Parse rooms from MapRoomInfo
    room_map = {}  # name -> bitmask_id
    for i in range(1, 4):
        key = f"MapRoomInfo{i}"
        raw = data.get(key, {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if not val or not isinstance(val, str):
            continue
        map_id, rooms = parse_map_room_info(val)
        if rooms and (selected_map_id is None or map_id == selected_map_id):
            room_map = {name: rid for rid, name in rooms}
            print(f"\nMapRoomInfo{i} (MapId={map_id}):")
            for rid, name in rooms:
                print(f"  bitmask {rid:>3} = {name}")
            break

    # 3. Build SLAM grid lookup
    if not active_slot:
        print("No active SLAM slot found!")
        sys.exit(1)

    slam_key = f"SaveMapDataX9_{active_slot}"
    slam_raw = data.get(slam_key, {})
    slam_val = slam_raw.get("value", slam_raw) if isinstance(slam_raw, dict) else slam_raw
    if isinstance(slam_val, str):
        slam_val = json.loads(slam_val)

    result = _decode_slam_grid(slam_val)
    if result is None:
        print("Failed to decode SLAM grid!")
        sys.exit(1)

    grid_points, min_x, min_y, max_x, max_y = result
    grid_lookup = {(x, y): room_type for x, y, room_type in grid_points}
    partition_types = set(grid_lookup.values())
    print(f"\nSLAM grid: {len(grid_points)} cells, partitions: {sorted(partition_types)}")

    # 4. Parse SaveMapDataInfoX9
    info_key = f"SaveMapDataInfoX9_{active_slot}"
    info_raw = data.get(info_key, {})
    info_val = info_raw.get("value", info_raw) if isinstance(info_raw, dict) else info_raw
    if isinstance(info_val, str):
        try:
            info_val = json.loads(info_val)
        except:
            print(f"\n{info_key}: not JSON, raw type: {type(info_val)}")
            print(f"  First 200 chars: {str(info_val)[:200]}")
            sys.exit(1)

    if not isinstance(info_val, dict):
        print(f"\n{info_key}: not a dict, type: {type(info_val)}")
        print(f"  Value: {info_val}")
        sys.exit(1)

    # Show what keys are present
    print(f"\n{info_key} keys: {sorted(info_val.keys())}")

    print(f"\nParsing {info_key} binary data:")
    room_centers = parse_map_info_x9_binary(info_val)

    if not room_centers:
        print("No rooms parsed from SaveMapDataInfoX9!")
        sys.exit(1)

    # 5. Build partition → bitmask mapping
    print("\n" + "-" * 60)
    print("BUILDING PARTITION MAPPING (center-point lookup)")
    print("-" * 60)

    partition_to_bitmask = {}
    for bitmask_id, cx, cy in room_centers:
        # Find room name
        room_name = None
        for name, rid in room_map.items():
            if rid == bitmask_id:
                room_name = name
                break

        # Look up center in grid
        slam_pid = grid_lookup.get((cx, cy))
        if slam_pid is not None and slam_pid not in (0, 3, 4):
            partition_to_bitmask[slam_pid] = bitmask_id
            print(f"  bitmask {bitmask_id:>3} ({room_name or '?':>15}) "
                  f"center=({cx:>4},{cy:>4}) -> SLAM partition {slam_pid}")
        else:
            # Search nearby
            found = False
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    pid = grid_lookup.get((cx + dx, cy + dy))
                    if pid is not None and pid not in (0, 3, 4):
                        partition_to_bitmask[pid] = bitmask_id
                        print(f"  bitmask {bitmask_id:>3} ({room_name or '?':>15}) "
                              f"center=({cx:>4},{cy:>4}) -> SLAM partition {pid} "
                              f"(nearby at offset {dx},{dy})")
                        found = True
                        break
                if found:
                    break
            if not found:
                print(f"  bitmask {bitmask_id:>3} ({room_name or '?':>15}) "
                      f"center=({cx:>4},{cy:>4}) -> NOT FOUND in grid!")

    print(f"\nFinal mapping ({len(partition_to_bitmask)} entries):")
    for slam_pid, bitmask_id in sorted(partition_to_bitmask.items()):
        room_name = None
        for name, rid in room_map.items():
            if rid == bitmask_id:
                room_name = name
                break
        print(f"  SLAM partition {slam_pid:>3} -> bitmask {bitmask_id:>3} = {room_name or '?'}")

    # Check coverage
    unmapped = partition_types - {0, 3, 4} - set(partition_to_bitmask.keys())
    if unmapped:
        print(f"\nWARNING: Unmapped SLAM partitions: {sorted(unmapped)}")
    else:
        print(f"\nAll room partitions mapped successfully!")

    # 6. Test current robot position
    road_raw = data.get("RealMapRoadData", {})
    road_val = road_raw.get("value", road_raw) if isinstance(road_raw, dict) else road_raw
    if isinstance(road_val, str):
        try:
            road_val = json.loads(road_val)
        except:
            road_val = {}

    if isinstance(road_val, dict):
        cp_raw = road_val.get("CurrentPoint")
        if cp_raw is not None:
            pos = _decode_point_int(int(cp_raw))
            if pos:
                x, y = pos
                pid = grid_lookup.get((x, y))
                bitmask = partition_to_bitmask.get(pid) if pid else None
                room = None
                if bitmask:
                    for name, rid in room_map.items():
                        if rid == bitmask:
                            room = name
                            break
                print(f"\nRobot position: ({x}, {y})")
                print(f"  SLAM partition: {pid}")
                print(f"  Bitmask ID: {bitmask}")
                print(f"  Room: {room or 'Unknown'}")


if __name__ == "__main__":
    main()
