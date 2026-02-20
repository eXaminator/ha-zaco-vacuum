#!/usr/bin/env python3
"""
test_edge_clean_goto.py - Test navigate-then-edge-clean flow

Sends the robot to a room center via PointToGo, waits for arrival,
then switches to edge cleaning mode (WorkMode 4).

Usage:
    # Read current PointToGo and WorkMode state:
    python3 scripts/test_edge_clean_goto.py read

    # List available rooms and their center points:
    python3 scripts/test_edge_clean_goto.py rooms

    # Navigate to a room center, then start edge cleaning:
    python3 scripts/test_edge_clean_goto.py send Schlafzimmer
"""

import argparse
import base64
import json
import sys
import time
from pathlib import Path

# Add project root so we can import from ha_integration
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "ha_integration" / "custom_components" / "zaco"))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from zone_utils import encode_point_to_go, decode_point_to_go
from test_aliyun import AliyunIoTClient, load_tokens, DEFAULT_IOT_ID
from api_client import AliyunApiClient

# PointToGoEnum states
PTG_STATES = {
    0: "idle/completed",
    1: "start",
    2: "goingToTarget",
    3: "cleaning (arrived)",
    4: "returning",
    12: "failedGoToTarget",
    13: "failedAndReturning",
    14: "failedReturn",
}


def get_client():
    """Create an authenticated AliyunIoTClient from saved tokens."""
    saved = load_tokens()
    if not saved:
        print("No saved tokens. Run test_aliyun.py --username/--password first.")
        sys.exit(1)

    client = AliyunIoTClient(host=saved.get("host"))
    client.iot_token = saved.get("iotToken")
    client.refresh_token = saved.get("refreshToken")
    client.identity_id = saved.get("identityId")

    if saved.get("_iot_expired") and not saved.get("_refresh_expired"):
        if not client.refresh_session():
            print("Token refresh failed.")
            sys.exit(1)
    elif saved.get("_refresh_expired"):
        print("All tokens expired. Re-login with test_aliyun.py.")
        sys.exit(1)

    return client


def fetch_ptg_state(client, iot_id):
    """Fetch and parse current PointToGo state."""
    data = client.get_properties(iot_id, ["PointToGo"])
    if not data:
        return None
    raw = data.get("PointToGo", {})
    val = raw.get("value", raw) if isinstance(raw, dict) else raw
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (json.JSONDecodeError, ValueError):
            pass
    return val


def fetch_work_mode(client, iot_id):
    """Fetch current WorkMode."""
    data = client.get_properties(iot_id, ["WorkMode"])
    if not data:
        return None
    raw = data.get("WorkMode", {})
    return raw.get("value", raw) if isinstance(raw, dict) else raw


def get_room_centers(client, iot_id):
    """Fetch room names and center points from MapRoomInfo + SaveMapDataInfoX9.

    Returns dict of room_name → (center_x, center_y).
    """
    import struct

    props = client.get_properties(iot_id, [
        "SaveMap", "MapRoomInfo1", "MapRoomInfo2", "MapRoomInfo3",
        "SaveMapDataInfoX9_1", "SaveMapDataInfoX9_2", "SaveMapDataInfoX9_3",
    ])
    if not props:
        return {}

    # Determine selected map ID
    save_map_raw = props.get("SaveMap", {})
    save_map_val = save_map_raw.get("value", save_map_raw) if isinstance(save_map_raw, dict) else save_map_raw
    if isinstance(save_map_val, str):
        try:
            save_map_val = json.loads(save_map_val)
        except (json.JSONDecodeError, ValueError):
            save_map_val = {}
    selected_map_id = save_map_val.get("SelectedMapId") if isinstance(save_map_val, dict) else None

    # Parse MapRoomInfo to get bitmask_id → room_name
    bitmask_to_name = {}
    for i in range(1, 4):
        raw = props.get(f"MapRoomInfo{i}", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if not val or not isinstance(val, str):
            continue
        map_id, rooms = AliyunApiClient.parse_map_room_info(val)
        if not rooms:
            continue
        if selected_map_id is not None and map_id == selected_map_id:
            bitmask_to_name = {rid: name for rid, name in rooms}
            break
        if not bitmask_to_name:
            bitmask_to_name = {rid: name for rid, name in rooms}

    if not bitmask_to_name:
        return {}

    # Parse SaveMapDataInfoX9 to get bitmask_id → (center_x, center_y)
    bitmask_to_center = {}
    for i in range(1, 4):
        raw = props.get(f"SaveMapDataInfoX9_{i}", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                continue
        if not isinstance(val, dict):
            continue

        # Concatenate MapInfo chunks
        all_bytes = bytearray()
        for j in range(1, 8):
            chunk_b64 = val.get(f"MapInfo{j}", "")
            if not chunk_b64:
                continue
            try:
                all_bytes.extend(base64.b64decode(chunk_b64))
            except Exception:
                continue

        if len(all_bytes) < 5:
            continue

        idx = 4  # skip charger X/Y
        num_rooms = all_bytes[idx] & 0xFF
        idx += 1

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
            idx += num_walls * 4

            bitmask_to_center[bitmask_id] = (center_x, center_y)

        if bitmask_to_center:
            break

    # Combine: room_name → (center_x, center_y)
    result = {}
    for bitmask_id, name in bitmask_to_name.items():
        center = bitmask_to_center.get(bitmask_id)
        if center:
            result[name] = center

    return result


def cmd_read(args):
    """Read current PointToGo and WorkMode state."""
    client = get_client()

    ptg = fetch_ptg_state(client, args.iot_id)
    wm = fetch_work_mode(client, args.iot_id)

    print(f"\nWorkMode: {wm}")
    print("PointToGo:")
    if isinstance(ptg, dict):
        enable = ptg.get("Enable", 0)
        state = PTG_STATES.get(enable, f"unknown({enable})")
        print(f"  Enable: {enable} ({state})")
        point_data = ptg.get("PointData", "")
        if point_data:
            try:
                x, y = decode_point_to_go(point_data)
                print(f"  PointData: {point_data} → ({x}, {y})")
            except Exception:
                print(f"  PointData: {point_data}")
    else:
        print(f"  Raw: {ptg}")


def cmd_rooms(args):
    """List available rooms and their center points."""
    client = get_client()
    rooms = get_room_centers(client, args.iot_id)

    if not rooms:
        print("No rooms found.")
        return

    print(f"\nFound {len(rooms)} room(s):\n")
    for name, (cx, cy) in sorted(rooms.items()):
        print(f"  {name}: center=({cx}, {cy})")


def cmd_send(args):
    """Navigate to room center, wait for arrival, switch to edge clean."""
    client = get_client()
    room_name = args.room

    # Resolve room center
    rooms = get_room_centers(client, args.iot_id)
    if not rooms:
        print("No rooms found. Make sure a map is saved.")
        sys.exit(1)

    # Case-insensitive lookup
    center = None
    matched_name = None
    for name, coords in rooms.items():
        if name.lower() == room_name.lower():
            center = coords
            matched_name = name
            break

    if center is None:
        print(f"Room '{room_name}' not found.")
        print(f"Available rooms: {', '.join(rooms.keys())}")
        sys.exit(1)

    x, y = center
    b64 = encode_point_to_go(x, y)

    print(f"\nRoom: {matched_name}")
    print(f"Center: ({x}, {y})")
    print(f"PointData: {b64}")

    # Send PointToGo
    payload = {"PointToGo": {"Enable": 1, "PointData": b64}}
    print(f"\nSending PointToGo: {json.dumps(payload)}")
    result = client.set_properties(args.iot_id, payload)
    if result is None:
        print("Failed to send PointToGo!")
        sys.exit(1)
    print("PointToGo sent. Waiting for arrival...")

    # Poll until arrival (Enable=3) or failure
    last_enable = None
    timeout = 180
    deadline = time.time() + timeout

    try:
        while time.time() < deadline:
            time.sleep(3)
            ptg = fetch_ptg_state(client, args.iot_id)
            if not isinstance(ptg, dict):
                print(f"  [{time.strftime('%H:%M:%S')}] Unexpected response: {ptg}")
                continue

            enable = ptg.get("Enable", 0)
            if enable != last_enable:
                state = PTG_STATES.get(enable, f"unknown({enable})")
                print(f"  [{time.strftime('%H:%M:%S')}] State: {enable} ({state})")
                last_enable = enable

            if enable == 3:
                # Arrived! Switch to edge clean
                print(f"\nRobot arrived at {matched_name}. Switching to edge clean (WorkMode 4)...")
                result = client.set_properties(args.iot_id, {"WorkMode": 4})
                if result is not None:
                    print("Edge clean started!")
                else:
                    print("Failed to switch to edge clean mode!")
                return

            if enable in (0, 4, 12, 13, 14):
                print(f"\nPointToGo ended with state {enable} — aborting.")
                return

        print(f"\nTimed out after {timeout}s waiting for arrival.")

    except KeyboardInterrupt:
        print("\n\nInterrupted. Sending stop (WorkMode 2)...")
        client.set_properties(args.iot_id, {"WorkMode": 2})
        print("Stop sent.")


def main():
    parser = argparse.ArgumentParser(
        description="Test navigate-then-edge-clean flow"
    )
    parser.add_argument(
        "--iot-id", default=DEFAULT_IOT_ID,
        help=f"Device iotId (default: {DEFAULT_IOT_ID})",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("read", help="Read current PointToGo and WorkMode state")
    sub.add_parser("rooms", help="List rooms and center points")

    p_send = sub.add_parser("send", help="Navigate to room, then edge clean")
    p_send.add_argument("room", type=str, help="Room name")

    args = parser.parse_args()

    if args.command == "read":
        cmd_read(args)
    elif args.command == "rooms":
        cmd_rooms(args)
    elif args.command == "send":
        cmd_send(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
