#!/usr/bin/env python3
"""
test_point_to_go.py - Test PointToGo (go-to-point spot cleaning)

Validates the encode/decode of PointToGo coordinates and sends a target
point to the robot. The robot will navigate to the point, do a spot clean,
and automatically return to the dock.

Usage:
    # Fetch and decode the current PointToGo state:
    python3 scripts/test_point_to_go.py read

    # Encode a point and show the base64 (dry run):
    python3 scripts/test_point_to_go.py encode 150 -30

    # Send the robot to a point (WILL START NAVIGATING):
    python3 scripts/test_point_to_go.py send 150 -30

    # Watch PointToGo state transitions (poll every 3s):
    python3 scripts/test_point_to_go.py watch

    # Run encode/decode round-trip tests:
    python3 scripts/test_point_to_go.py roundtrip
"""

import argparse
import json
import sys
import time
from pathlib import Path

# Add project root so we can import from ha_integration
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "ha_integration" / "custom_components" / "zaco"))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from zone_utils import decode_point_to_go, encode_point_to_go
from test_aliyun import AliyunIoTClient, load_tokens, DEFAULT_IOT_ID

# PointToGoEnum states (from PointToGoEnum.java)
PTG_STATES = {
    0: "invalid (idle/completed)",
    1: "start",
    2: "goingToTarget",
    3: "cleaning",
    4: "returning",
    12: "failedGoToTarget",
    13: "failedGoToTargetAndReturning",
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


def print_ptg_state(val):
    """Pretty-print a PointToGo property value."""
    if isinstance(val, dict):
        enable = val.get("Enable", 0)
        point_data = val.get("PointData", "")
        state_name = PTG_STATES.get(enable, f"unknown({enable})")

        print(f"  Enable: {enable} ({state_name})")
        print(f"  PointData: {point_data}")

        if point_data:
            try:
                x, y = decode_point_to_go(point_data)
                print(f"  Decoded: ({x}, {y})")
            except Exception as e:
                print(f"  Decode error: {e}")
    else:
        print(f"  Raw: {val}")


def cmd_read(args):
    """Fetch and decode the current PointToGo state from the robot."""
    client = get_client()
    val = fetch_ptg_state(client, args.iot_id)
    if val is None:
        print("Failed to fetch PointToGo")
        return

    print("\nPointToGo:")
    print_ptg_state(val)


def cmd_encode(args):
    """Encode a point and show the base64 result."""
    x, y = args.x, args.y
    b64 = encode_point_to_go(x, y)
    print(f"\nInput: ({x}, {y})")
    print(f"Base64: {b64}")

    # Round-trip
    dx, dy = decode_point_to_go(b64)
    print(f"Round-trip decode: ({dx}, {dy})")

    if (dx, dy) == (x, y):
        print("Round-trip: OK")
    else:
        print(f"Round-trip: MISMATCH! Expected ({x}, {y}), got ({dx}, {dy})")


def cmd_send(args):
    """Send PointToGo command to the robot."""
    x, y = args.x, args.y
    b64 = encode_point_to_go(x, y)

    print(f"\nTarget point: ({x}, {y})")
    print(f"PointData: {b64}")

    client = get_client()

    payload = {
        "PointToGo": {
            "Enable": 1,
            "PointData": b64,
        }
    }
    print(f"Sending: {json.dumps(payload)}")
    result = client.set_properties(args.iot_id, payload)

    if result is not None:
        print("\nPointToGo command sent successfully!")
        print("The robot should navigate to the target, spot clean, and return.")
        print("Use 'watch' subcommand to track state transitions.")
    else:
        print("\nFailed to send PointToGo command.")


def cmd_watch(args):
    """Poll PointToGo state and display transitions."""
    client = get_client()
    print("\nWatching PointToGo state (Ctrl+C to stop)...\n")

    last_enable = None
    try:
        while True:
            val = fetch_ptg_state(client, args.iot_id)
            if val is None:
                print(f"  [{time.strftime('%H:%M:%S')}] Failed to fetch")
            elif isinstance(val, dict):
                enable = val.get("Enable", 0)
                state_name = PTG_STATES.get(enable, f"unknown({enable})")
                if enable != last_enable:
                    point_data = val.get("PointData", "")
                    coords = ""
                    if point_data:
                        try:
                            x, y = decode_point_to_go(point_data)
                            coords = f" → ({x}, {y})"
                        except Exception:
                            pass
                    print(f"  [{time.strftime('%H:%M:%S')}] State: {enable} ({state_name}){coords}")
                    last_enable = enable
                    if enable == 0:
                        print("  Task completed (or idle). Still watching...")
            time.sleep(3)
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_roundtrip(_args):
    """Run encode/decode round-trip tests."""
    test_cases = [
        (0, 0),
        (150, -30),
        (-200, 300),
        (32767, -32768),    # int16 extremes
        (-32768, 32767),
        (1, -1),
    ]

    all_ok = True
    for x, y in test_cases:
        b64 = encode_point_to_go(x, y)
        dx, dy = decode_point_to_go(b64)
        ok = (dx, dy) == (x, y)
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] ({x}, {y}) → {b64} → ({dx}, {dy})")
        if not ok:
            all_ok = False

    print()
    if all_ok:
        print("All round-trip tests passed!")
    else:
        print("Some tests FAILED!")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Test PointToGo (go-to-point spot cleaning)"
    )
    parser.add_argument(
        "--iot-id", default=DEFAULT_IOT_ID,
        help=f"Device iotId (default: {DEFAULT_IOT_ID})",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("read", help="Fetch current PointToGo state")

    p_encode = sub.add_parser("encode", help="Encode a point (dry run)")
    p_encode.add_argument("x", type=int, help="X coordinate")
    p_encode.add_argument("y", type=int, help="Y coordinate")

    p_send = sub.add_parser("send", help="Send PointToGo command")
    p_send.add_argument("x", type=int, help="X coordinate")
    p_send.add_argument("y", type=int, help="Y coordinate")

    sub.add_parser("watch", help="Watch PointToGo state transitions")
    sub.add_parser("roundtrip", help="Run encode/decode round-trip tests")

    args = parser.parse_args()

    if args.command == "read":
        cmd_read(args)
    elif args.command == "encode":
        cmd_encode(args)
    elif args.command == "send":
        cmd_send(args)
    elif args.command == "watch":
        cmd_watch(args)
    elif args.command == "roundtrip":
        cmd_roundtrip(args)
    else:
        print("=== Round-trip tests ===")
        cmd_roundtrip(args)
        print()
        print("=== Current PointToGo state ===")
        cmd_read(args)


if __name__ == "__main__":
    main()
