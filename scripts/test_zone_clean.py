#!/usr/bin/env python3
"""
test_zone_clean.py - Test zone/area cleaning via CleanAreaData

Validates the encode/decode of CleanAreaData coordinates and sends
a test zone to the robot.

Usage:
    # Fetch and decode the current CleanAreaData:
    python3 scripts/test_zone_clean.py --read

    # Encode a test zone and show the base64 (dry run):
    python3 scripts/test_zone_clean.py --encode 100 -50 300 100

    # Send a zone to the robot (WILL START CLEANING):
    python3 scripts/test_zone_clean.py --send 100 -50 300 100

    # Send with 2 passes:
    python3 scripts/test_zone_clean.py --send 100 -50 300 100 --passes 2

    # Round-trip test (encode → decode → verify):
    python3 scripts/test_zone_clean.py --roundtrip
"""

import argparse
import json
import sys
from pathlib import Path

# Add project root so we can import from ha_integration
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "ha_integration" / "custom_components" / "zaco"))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from zone_utils import (
    CLEAN_AREA_EMPTY,
    decode_clean_area,
    encode_clean_area,
    rect_to_corners,
)
from test_aliyun import AliyunIoTClient, load_tokens, DEFAULT_IOT_ID


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


def cmd_read(args):
    """Fetch and decode the current CleanAreaData from the robot."""
    client = get_client()
    data = client.get_properties(args.iot_id, ["CleanAreaData"])
    if not data:
        print("Failed to fetch CleanAreaData")
        return

    raw = data.get("CleanAreaData", {})
    val = raw.get("value", raw) if isinstance(raw, dict) else raw
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (json.JSONDecodeError, ValueError):
            pass

    if isinstance(val, dict):
        area_data = val.get("AreaData", "")
        clean_loop = val.get("CleanLoop", 0)
        enable = val.get("Enable", 0)

        print(f"\nCleanAreaData:")
        print(f"  AreaData: {area_data}")
        print(f"  CleanLoop: {clean_loop}")
        print(f"  Enable: {enable}")

        if area_data and area_data != CLEAN_AREA_EMPTY:
            points = decode_clean_area(area_data)
            print(f"\n  Decoded corners:")
            for i, (x, y) in enumerate(points):
                print(f"    Point {i+1}: ({x}, {y})")
        else:
            print(f"\n  (empty / no zone set)")
    else:
        print(f"\nCleanAreaData raw: {val}")


def cmd_encode(args):
    """Encode a rectangle and show the base64 result."""
    x1, y1, x2, y2 = args.coords
    corners = rect_to_corners(x1, y1, x2, y2)
    b64 = encode_clean_area(*corners)

    print(f"\nInput rectangle: ({x1}, {y1}) to ({x2}, {y2})")
    print(f"4 corners (clockwise): {corners}")
    print(f"Base64: {b64}")

    # Verify round-trip
    decoded = decode_clean_area(b64)
    print(f"\nRound-trip decode:")
    for i, (x, y) in enumerate(decoded):
        print(f"  Point {i+1}: ({x}, {y})")

    expected = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    if decoded == expected:
        print("\nRound-trip: OK")
    else:
        print(f"\nRound-trip: MISMATCH!")
        print(f"  Expected: {expected}")
        print(f"  Got:      {decoded}")


def cmd_send(args):
    """Send a zone cleaning command to the robot."""
    x1, y1, x2, y2 = args.coords
    passes = max(1, min(3, args.passes))
    corners = rect_to_corners(x1, y1, x2, y2)
    b64 = encode_clean_area(*corners)

    print(f"\nZone: ({x1}, {y1}) to ({x2}, {y2})")
    print(f"Passes: {passes}")
    print(f"AreaData: {b64}")

    client = get_client()

    payload = {
        "CleanAreaData": {
            "AreaData": b64,
            "CleanLoop": passes,
            "Enable": 1,
        }
    }
    print(f"\nSending: {json.dumps(payload)}")
    result = client.set_properties(args.iot_id, payload)

    if result is not None:
        print("\nZone cleaning command sent successfully!")
        print("Check if the robot starts cleaning the defined area.")
    else:
        print("\nFailed to send zone cleaning command.")


def cmd_roundtrip(_args):
    """Run encode/decode round-trip tests."""
    test_cases = [
        (100, -50, 300, 100),
        (-200, -300, -100, -200),
        (0, 0, 500, 500),
        (-1000, -1000, 1000, 1000),
        (32767, -32768, -32768, 32767),  # int16 extremes
    ]

    all_ok = True
    for x1, y1, x2, y2 in test_cases:
        corners = rect_to_corners(x1, y1, x2, y2)
        b64 = encode_clean_area(*corners)
        decoded = decode_clean_area(b64)
        expected = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]

        ok = decoded == expected
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] ({x1}, {y1}) to ({x2}, {y2})  →  {b64}")
        if not ok:
            print(f"         Expected: {expected}")
            print(f"         Got:      {decoded}")
            all_ok = False

    print()
    if all_ok:
        print("All round-trip tests passed!")
    else:
        print("Some tests FAILED!")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Test zone/area cleaning via CleanAreaData"
    )
    parser.add_argument(
        "--iot-id", default=DEFAULT_IOT_ID,
        help=f"Device iotId (default: {DEFAULT_IOT_ID})",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("read", help="Fetch and decode current CleanAreaData")

    p_encode = sub.add_parser("encode", help="Encode a rectangle (dry run)")
    p_encode.add_argument("coords", type=int, nargs=4, metavar=("X1", "Y1", "X2", "Y2"))

    p_send = sub.add_parser("send", help="Send zone cleaning command")
    p_send.add_argument("coords", type=int, nargs=4, metavar=("X1", "Y1", "X2", "Y2"))
    p_send.add_argument("--passes", type=int, default=1, help="Cleaning passes (1-3)")

    sub.add_parser("roundtrip", help="Run encode/decode round-trip tests")

    # Also support --read, --encode, --send, --roundtrip for convenience
    parser.add_argument("--read", action="store_true", help="Fetch current CleanAreaData")
    parser.add_argument("--encode", type=int, nargs=4, metavar=("X1", "Y1", "X2", "Y2"))
    parser.add_argument("--send", type=int, nargs=4, metavar=("X1", "Y1", "X2", "Y2"))
    parser.add_argument("--passes", type=int, default=1)
    parser.add_argument("--roundtrip", action="store_true")

    args = parser.parse_args()

    # Handle subcommand style
    if args.command == "read":
        cmd_read(args)
    elif args.command == "encode":
        cmd_encode(args)
    elif args.command == "send":
        cmd_send(args)
    elif args.command == "roundtrip":
        cmd_roundtrip(args)
    # Handle flag style
    elif args.read:
        cmd_read(args)
    elif args.encode:
        args.coords = args.encode
        cmd_encode(args)
    elif args.send:
        args.coords = args.send
        cmd_send(args)
    elif args.roundtrip:
        cmd_roundtrip(args)
    else:
        # Default: read + roundtrip
        print("=== Round-trip tests ===")
        cmd_roundtrip(args)
        print()
        print("=== Current CleanAreaData from robot ===")
        cmd_read(args)


if __name__ == "__main__":
    main()
