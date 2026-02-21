#!/usr/bin/env python3
"""
test_goto.py - Test the goto and spot_clean flows using the Zaco library.

Uses the EXACT same Zaco class as Home Assistant — no duplicated logic.

Usage:
    # List available rooms:
    python3 scripts/test_goto.py rooms

    # Navigate to a room center:
    python3 scripts/test_goto.py send Büro

    # Navigate to arbitrary coordinates:
    python3 scripts/test_goto.py point 81 70

    # Navigate to a room center and spot clean there:
    python3 scripts/test_goto.py spot Büro
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Import load_tokens from test_aliyun BEFORE adding HA path (avoids select.py shadow)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_aliyun import load_tokens, DEFAULT_IOT_ID

# Import Zaco from the HA integration
HA_PATH = str(Path(__file__).resolve().parent.parent / "ha_integration" / "custom_components" / "zaco")
sys.path.insert(0, HA_PATH)
from zaco import Zaco


async def create_zaco(iot_id: str) -> Zaco:
    """Create a Zaco instance from saved tokens."""
    saved = load_tokens()
    if not saved:
        print("No saved tokens. Run test_aliyun.py --username/--password first.")
        sys.exit(1)
    if saved.get("_refresh_expired"):
        print("All tokens expired. Re-login with test_aliyun.py.")
        sys.exit(1)

    saved_at = saved.get("savedAt", 0)
    return await Zaco.from_tokens(
        iot_host=saved.get("host", ""),
        iot_token=saved.get("iotToken", ""),
        refresh_token=saved.get("refreshToken", ""),
        identity_id=saved.get("identityId", ""),
        iot_id=iot_id,
        iot_token_expiry=saved_at + saved.get("iotTokenExpire", 7200),
        refresh_token_expiry=saved_at + saved.get("refreshTokenExpire", 2592000),
        verbose=True,
        log_fn=print,
    )


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

async def cmd_rooms(args):
    zaco = await create_zaco(args.iot_id)
    try:
        rooms = zaco.rooms
        if not rooms:
            print("No rooms found.")
            return
        print(f"\nFound {len(rooms)} room(s):\n")
        for name, bitmask_id in sorted(rooms.items()):
            center = zaco.get_room_center(name)
            if center:
                print(f"  {name}: id={bitmask_id}, center=({center[0]}, {center[1]})")
            else:
                print(f"  {name}: id={bitmask_id}, center=N/A")
    finally:
        await zaco.close()


async def cmd_send(args):
    zaco = await create_zaco(args.iot_id)
    try:
        center = zaco.get_room_center(args.room)
        if not center:
            print(f"Room '{args.room}' not found.")
            print(f"Available: {', '.join(zaco.rooms.keys())}")
            return
        print(f"Room: {args.room}, Center: ({center[0]}, {center[1]})")
        await zaco.goto_room(args.room)
        print("\nDone.")
    finally:
        await zaco.close()


async def cmd_point(args):
    print(f"Target: ({args.x}, {args.y})")
    zaco = await create_zaco(args.iot_id)
    try:
        await zaco.goto(args.x, args.y)
        print("\nDone.")
    finally:
        await zaco.close()


async def cmd_spot(args):
    zaco = await create_zaco(args.iot_id)
    try:
        center = zaco.get_room_center(args.room)
        if not center:
            print(f"Room '{args.room}' not found.")
            print(f"Available: {', '.join(zaco.rooms.keys())}")
            return
        print(f"Room: {args.room}, Center: ({center[0]}, {center[1]})")
        print(f"Mode: goto + spot clean ({args.repeats} pass(es))")
        await zaco.spot_clean_room(args.room, repeats=args.repeats)
        print("\nDone.")
    finally:
        await zaco.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Test goto and spot clean flows")
    parser.add_argument("--iot-id", default=DEFAULT_IOT_ID, help="Device iotId")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("rooms", help="List rooms and center points")

    p_send = sub.add_parser("send", help="Navigate to room center (goto only)")
    p_send.add_argument("room", type=str, help="Room name")

    p_point = sub.add_parser("point", help="Navigate to coordinates (goto only)")
    p_point.add_argument("x", type=int, help="X coordinate")
    p_point.add_argument("y", type=int, help="Y coordinate")

    p_spot = sub.add_parser("spot", help="Navigate to room center + spot clean")
    p_spot.add_argument("room", type=str, help="Room name")
    p_spot.add_argument("--repeats", type=int, default=1, help="Number of spot clean passes (1-5)")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    if args.command == "rooms":
        asyncio.run(cmd_rooms(args))
    elif args.command == "send":
        asyncio.run(cmd_send(args))
    elif args.command == "point":
        asyncio.run(cmd_point(args))
    elif args.command == "spot":
        asyncio.run(cmd_spot(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
