#!/usr/bin/env python3
"""
test_goto.py - Test the goto and spot_clean flows

Uses the EXACT same integration stack as Home Assistant:
  AliyunApiClient + ZacoDataUpdateCoordinator + ZacoMqttClient

The test script is a thin CLI wrapper. All robot communication
(authentication, property reads, MQTT real-time push, goto state machine)
goes through imported integration code.

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
import time
from pathlib import Path

# Import load_tokens from test_aliyun BEFORE adding HA path (avoids select.py shadow)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_aliyun import load_tokens, DEFAULT_IOT_ID

# Import shared modules from HA integration
HA_PATH = str(Path(__file__).resolve().parent.parent / "ha_integration" / "custom_components" / "zaco")
sys.path.insert(0, HA_PATH)
from api_client import AliyunApiClient
from coordinator import ZacoDataUpdateCoordinator
from goto_controller import (
    parse_int_prop,
    send_goto_zone,
    spot_clean_after_arrival,
)
from mqtt_client import ZacoMqttClient


# ---------------------------------------------------------------------------
# Integration stack setup (mirrors __init__.py:async_setup_entry + _start_mqtt)
# ---------------------------------------------------------------------------

async def get_async_client():
    """Create an authenticated AliyunApiClient from saved tokens.

    Returns (client, session) tuple. Caller must close session when done.
    """
    import aiohttp

    saved = load_tokens()
    if not saved:
        print("No saved tokens. Run test_aliyun.py --username/--password first.")
        sys.exit(1)

    if saved.get("_refresh_expired"):
        print("All tokens expired. Re-login with test_aliyun.py.")
        sys.exit(1)

    saved_at = saved.get("savedAt", 0)
    session = aiohttp.ClientSession()
    client = await AliyunApiClient.from_saved_tokens(
        session,
        iot_host=saved.get("host"),
        iot_token=saved.get("iotToken"),
        refresh_token=saved.get("refreshToken"),
        identity_id=saved.get("identityId"),
        iot_token_expiry=saved_at + saved.get("iotTokenExpire", 7200),
        refresh_token_expiry=saved_at + saved.get("refreshTokenExpire", 2592000),
    )
    return client, session


async def setup_stack(iot_id):
    """Set up the full integration stack: client + coordinator + MQTT.

    Mirrors async_setup_entry + _start_mqtt from __init__.py.
    Returns (client, coordinator, mqtt_client, session).
    """
    client, session = await get_async_client()

    # Create coordinator in standalone mode (hass=None)
    coordinator = ZacoDataUpdateCoordinator(None, client, iot_id, {})
    await coordinator.async_config_entry_first_refresh()

    # Start MQTT real-time push (same as __init__.py:_start_mqtt)
    mqtt_client = None
    try:
        creds = await client.get_mqtt_credentials()
        mqtt_client = ZacoMqttClient(
            on_properties=coordinator.handle_mqtt_push,
        )
        await mqtt_client.start(creds, client.iot_token)
        coordinator.mqtt_connected = True
        # Give MQTT time to connect + subscribe + bind
        await asyncio.sleep(2)
        print(f"MQTT: {'connected + bound' if mqtt_client.connected else 'connecting...'}")
    except Exception as e:
        print(f"MQTT failed ({e}), falling back to REST polling")
        mqtt_client = None

    return client, coordinator, mqtt_client, session


async def teardown_stack(mqtt_client, session):
    """Clean up: stop MQTT and HTTP session."""
    if mqtt_client:
        await mqtt_client.stop()
    await session.close()


def _make_callables(client, coordinator, iot_id):
    """Build the get_data/set_props/refresh callables for goto_controller.

    get_data returns coordinator.data (updated by MQTT + refresh).
    set_props sends properties via REST API.
    refresh triggers a coordinator data refresh (REST poll).
    """
    async def get_data():
        return coordinator.data

    async def set_props(props):
        return await client.set_properties(iot_id, props)

    async def refresh():
        await coordinator.async_request_refresh()

    return get_data, set_props, refresh


def _log_fn(msg):
    """Print timestamped log messages."""
    print(f"    [{time.strftime('%H:%M:%S')}] {msg}")


# ---------------------------------------------------------------------------
# CLI commands (async)
# ---------------------------------------------------------------------------

async def cmd_rooms(args):
    """List available rooms and their center points."""
    client, coordinator, mqtt, session = await setup_stack(args.iot_id)
    try:
        rooms = coordinator.rooms
        if not rooms:
            print("No rooms found.")
            return
        print(f"\nFound {len(rooms)} room(s):\n")
        for name, bitmask_id in sorted(rooms.items()):
            center = coordinator.get_room_center_by_name(name)
            if center:
                print(f"  {name}: id={bitmask_id}, center=({center[0]}, {center[1]})")
            else:
                print(f"  {name}: id={bitmask_id}, center=N/A")
    finally:
        await teardown_stack(mqtt, session)



async def cmd_send(args):
    """Navigate to a room center."""
    client, coordinator, mqtt, session = await setup_stack(args.iot_id)
    try:
        center = coordinator.get_room_center_by_name(args.room)
        if not center:
            print(f"Room '{args.room}' not found.")
            print(f"Available: {', '.join(coordinator.rooms.keys())}")
            return
        x, y = center
        print(f"Room: {args.room}")
        print(f"Center: ({x}, {y})")

        get_data, set_props, refresh = _make_callables(client, coordinator, args.iot_id)

        await send_goto_zone(
            get_data, set_props,
            target_x=x, target_y=y,
            refresh=refresh,
            log_fn=_log_fn,
        )

        if coordinator.data:
            print(f"\nFinal: WorkMode={parse_int_prop(coordinator.data, 'WorkMode')}, "
                  f"FanPower={parse_int_prop(coordinator.data, 'FanPower')}")
    finally:
        await teardown_stack(mqtt, session)


async def cmd_point(args):
    """Navigate to arbitrary coordinates."""
    print(f"Target: ({args.x}, {args.y})")
    client, coordinator, mqtt, session = await setup_stack(args.iot_id)
    try:
        get_data, set_props, refresh = _make_callables(client, coordinator, args.iot_id)

        await send_goto_zone(
            get_data, set_props,
            target_x=args.x, target_y=args.y,
            refresh=refresh,
            log_fn=_log_fn,
        )

        if coordinator.data:
            print(f"\nFinal: WorkMode={parse_int_prop(coordinator.data, 'WorkMode')}, "
                  f"FanPower={parse_int_prop(coordinator.data, 'FanPower')}")
    finally:
        await teardown_stack(mqtt, session)


async def cmd_spot(args):
    """Navigate to a room center and spot clean there."""
    client, coordinator, mqtt, session = await setup_stack(args.iot_id)
    try:
        center = coordinator.get_room_center_by_name(args.room)
        if not center:
            print(f"Room '{args.room}' not found.")
            print(f"Available: {', '.join(coordinator.rooms.keys())}")
            return
        x, y = center
        print(f"Room: {args.room}")
        print(f"Center: ({x}, {y})")
        print("Mode: goto + spot clean")

        get_data, set_props, refresh = _make_callables(client, coordinator, args.iot_id)

        async def _spot_on_arrival(gd, sp, rf):
            await spot_clean_after_arrival(gd, sp, refresh=rf, repeats=args.repeats, log_fn=_log_fn)

        await send_goto_zone(
            get_data, set_props,
            target_x=x, target_y=y,
            on_arrival=_spot_on_arrival,
            refresh=refresh,
            log_fn=_log_fn,
        )

        if coordinator.data:
            print(f"\nFinal: WorkMode={parse_int_prop(coordinator.data, 'WorkMode')}, "
                  f"FanPower={parse_int_prop(coordinator.data, 'FanPower')}")
    finally:
        await teardown_stack(mqtt, session)


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
