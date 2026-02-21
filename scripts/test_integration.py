#!/usr/bin/env python3
"""
test_integration.py - Run 4 sequential integration tests against the real robot.

Each test mirrors the exact code path triggered by Home Assistant services.
The robot must be docked before starting. Between tests, the script waits
for the robot to return to its dock.

Tests:
    1. Goto kitchen, wait 5s, return to dock       (zaco.goto + vacuum.return_to_base)
    2. Spot clean in kitchen, 2 passes              (zaco.spot_clean with repeats=2)
    3. Edge clean in kitchen                        (zaco.edge_clean with room)
    4. Room clean - kitchen, 2 passes               (zaco.start with rooms + passes=2)

Usage:
    python3 scripts/test_integration.py
    python3 scripts/test_integration.py --room Büro
    python3 scripts/test_integration.py --test 3      # run only test 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Optional

# Import load_tokens BEFORE adding HA path (avoids select.py shadow on asyncio)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_aliyun import load_tokens, DEFAULT_IOT_ID

# Import Zaco from the HA integration
HA_PATH = str(
    Path(__file__).resolve().parent.parent
    / "ha_integration"
    / "custom_components"
    / "zaco"
)
sys.path.insert(0, HA_PATH)
from zaco import Zaco
from const import WORKMODE_IDLE, WORKMODE_RETURNING

_LOGGER = logging.getLogger(__name__)


async def create_zaco(iot_id: str) -> Zaco:
    """Create a Zaco instance from saved tokens (same as test_goto.py)."""
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


def _get_wm(zaco: Zaco) -> int | None:
    """Read current WorkMode from cached data."""
    data = zaco.data
    if not data:
        return None
    raw = data.get("WorkMode", {})
    val = raw.get("value", raw) if isinstance(raw, dict) else raw
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _get_battery(zaco: Zaco) -> int | None:
    """Read current BatteryState from cached data."""
    data = zaco.data
    if not data:
        return None
    raw = data.get("BatteryState", {})
    val = raw.get("value", raw) if isinstance(raw, dict) else raw
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


async def wait_for_dock(
    zaco: Zaco,
    timeout: int = 600,
    poll_interval: int = 10,
    grace_period: int = 30,
) -> bool:
    """Poll until robot is docked. Returns True if docked, False on timeout."""
    t0 = time.monotonic()
    deadline = t0 + timeout

    # Grace period: robot may still be transitioning (e.g. from cleaning to returning)
    print(f"  (grace period: {grace_period}s before checking dock status)")
    await asyncio.sleep(grace_period)

    while time.monotonic() < deadline:
        await zaco.refresh(fast=False)
        wm = _get_wm(zaco)
        battery = _get_battery(zaco)
        elapsed = time.monotonic() - t0

        state = "IDLE" if wm in WORKMODE_IDLE else (
            "RETURNING" if wm in WORKMODE_RETURNING else f"ACTIVE"
        )
        print(f"  [{elapsed:5.0f}s] WorkMode={wm} ({state})  Battery={battery}%")

        if wm in WORKMODE_IDLE:
            print(f"  Robot docked after {elapsed:.0f}s")
            # Extra settle time - let the robot fully dock and report stable state
            await asyncio.sleep(5)
            return True

        await asyncio.sleep(poll_interval)

    print(f"  TIMEOUT after {timeout}s - robot not docked (WM={_get_wm(zaco)})")
    return False


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

async def run_test_1(zaco: Zaco, room: str) -> None:
    """Test 1: Goto kitchen, wait 5s, return to dock.

    Mirrors HA: zaco.goto service → zaco.goto_room(room)
              + vacuum.return_to_base → zaco.return_to_base()
    """
    center = zaco.get_room_center(room)
    print(f"  Target: {room} at ({center[0]}, {center[1]})")
    print(f"  Calling: goto_room({room!r})")
    await zaco.goto_room(room)
    print("  Arrived at target. Waiting 5 seconds...")
    await asyncio.sleep(5)
    print("  Calling: return_to_base()")
    await zaco.return_to_base()


async def run_test_2(zaco: Zaco, room: str) -> None:
    """Test 2: Spot clean in kitchen, 2 passes.

    Mirrors HA: zaco.spot_clean service with x, y, repeats=2
              → zaco.spot_clean(x, y, repeats=2)
    """
    center = zaco.get_room_center(room)
    print(f"  Target: {room} at ({center[0]}, {center[1]})")
    print(f"  Calling: spot_clean_room({room!r}, repeats=2)")
    await zaco.spot_clean_room(room, repeats=2)
    print("  Spot clean flow completed (includes auto-return to dock)")


async def run_test_3(zaco: Zaco, room: str) -> None:
    """Test 3: Edge clean in kitchen, 2 passes.

    Mirrors HA: zaco.edge_clean service with room=room, passes=2
              → zaco.edge_clean(room=room, passes=2)
    """
    center = zaco.get_room_center(room)
    print(f"  Target: {room} at ({center[0]}, {center[1]})")
    print(f"  Calling: edge_clean(room={room!r}, passes=2)")
    await zaco.edge_clean(room=room, passes=2)
    print("  Edge clean flow completed (includes auto-return to dock)")


async def run_test_4(zaco: Zaco, room: str) -> None:
    """Test 4: Room clean - kitchen, 2 passes.

    Mirrors HA: zaco.start service with rooms=[room], passes=2
              → zaco.clean_rooms([room], passes=2)
    Note: This returns immediately after sending the API command.
    """
    print(f"  Target: {room}")
    print(f"  Calling: clean_rooms([{room!r}], passes=2)")
    result = await zaco.clean_rooms([room], passes=2)
    print(f"  API response: {'success' if result else 'FAILED'}")
    print("  Room clean command sent (robot is now cleaning)")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

ALL_TESTS = [
    ("1: Goto kitchen -> wait 5s -> return to dock", run_test_1),
    ("2: Spot clean in kitchen (2 passes)", run_test_2),
    ("3: Edge clean in kitchen (2 passes)", run_test_3),
    ("4: Room clean - kitchen (2 passes)", run_test_4),
]


async def run(iot_id: str, room: str, test_num: int | None = None) -> None:
    zaco = await create_zaco(iot_id)
    try:
        # Verify room exists
        center = zaco.get_room_center(room)
        if center is None:
            print(f"\nRoom '{room}' not found.")
            print(f"Available rooms: {', '.join(zaco.rooms.keys())}")
            return

        print(f"\nTarget room: {room} (id={zaco.get_room_id(room)}, "
              f"center=({center[0]}, {center[1]}))")

        # Verify robot is docked
        wm = _get_wm(zaco)
        battery = _get_battery(zaco)
        print(f"Robot state: WorkMode={wm}, Battery={battery}%")
        if wm not in WORKMODE_IDLE:
            print(f"WARNING: Robot is not idle (WM={wm}). "
                  "It should be docked before starting tests.")
            return

        # Select tests
        if test_num is not None:
            if test_num < 1 or test_num > len(ALL_TESTS):
                print(f"Invalid test number: {test_num}. Valid: 1-{len(ALL_TESTS)}")
                return
            tests = [ALL_TESTS[test_num - 1]]
        else:
            tests = ALL_TESTS

        for i, (label, fn) in enumerate(tests):
            label_room = label.replace("kitchen", room)
            print(f"\n{'=' * 60}")
            print(f"TEST {label_room}")
            print(f"{'=' * 60}\n")

            t0 = time.monotonic()
            await fn(zaco, room)
            elapsed = time.monotonic() - t0
            print(f"\n  Test action completed in {elapsed:.0f}s")

            # Wait for dock between tests (and after last test)
            print(f"\n  Waiting for Friday to return to dock...")
            docked = await wait_for_dock(zaco)
            if not docked:
                print("  TIMEOUT waiting for dock. Aborting remaining tests.")
                break
            if i < len(tests) - 1:
                print("  Docked. Starting next test in 10 seconds...")
                await asyncio.sleep(10)
            else:
                print("  Docked. All tests complete.")

    finally:
        await zaco.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run integration tests against the real robot"
    )
    parser.add_argument("--iot-id", default=DEFAULT_IOT_ID, help="Device iotId")
    parser.add_argument("--room", default="Küche", help="Target room (default: Küche)")
    parser.add_argument(
        "--test", type=int, default=None,
        help="Run only this test number (1-4)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    asyncio.run(run(args.iot_id, args.room, args.test))


if __name__ == "__main__":
    main()
