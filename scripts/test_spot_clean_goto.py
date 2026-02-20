#!/usr/bin/env python3
"""
test_spot_clean_goto.py - Live test spot_clean (with coords) and goto (navigate-and-pause)

Uses CleanAreaData (small zone) as workaround since PointToGo doesn't work via REST API.

  - spot_clean: Send small zone at target point, let it clean, watch it return
  - goto: Send small zone at target, pause when zone cleaning starts (WorkMode 19)

Usage:
    # Test spot_clean with coordinates (navigate → zone clean → return):
    python3 scripts/test_spot_clean_goto.py spot_clean 5 10

    # Test goto (navigate → pause on WorkMode 19 → return to dock):
    python3 scripts/test_spot_clean_goto.py goto 10 15

    # Abort: send robot back to dock immediately:
    python3 scripts/test_spot_clean_goto.py abort
"""

import argparse
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

# Import test_aliyun BEFORE adding the zaco path, to avoid zaco's select.py
# shadowing Python's stdlib select module
sys.path.insert(0, str(PROJECT_DIR / "scripts"))
from test_aliyun import AliyunIoTClient, load_tokens, DEFAULT_IOT_ID

sys.path.insert(0, str(PROJECT_DIR / "ha_integration" / "custom_components" / "zaco"))
from zone_utils import encode_clean_area, rect_to_corners

POLL_PROPS = ["WorkMode", "PowerSwitch", "PauseSwitch", "BatteryState"]

WORKMODE_IDLE = {9, 11, 16, 17}


def get_client():
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


def parse_work_mode(props):
    """Extract WorkMode value from properties."""
    raw = props.get("WorkMode", {})
    val = raw.get("value", raw) if isinstance(raw, dict) else raw
    if isinstance(val, (int, float)):
        return int(val)
    return None


def log_state(props):
    """Log current device state."""
    wm = parse_work_mode(props)

    ps_raw = props.get("PowerSwitch", {})
    ps = ps_raw.get("value", ps_raw) if isinstance(ps_raw, dict) else ps_raw

    pause_raw = props.get("PauseSwitch", {})
    pause = pause_raw.get("value", pause_raw) if isinstance(pause_raw, dict) else pause_raw

    bat_raw = props.get("BatteryState", {})
    bat = bat_raw.get("value", bat_raw) if isinstance(bat_raw, dict) else bat_raw

    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] WorkMode={wm}  Power={ps}  Pause={pause}  Bat={bat}")
    return wm


def make_small_zone(x, y, half=3):
    """Create a small CleanAreaData zone centered on (x, y)."""
    corners = rect_to_corners(x - half, y - half, x + half, y + half)
    return encode_clean_area(*corners)


def send_zone_clean(client, iot_id, x, y, half=3):
    """Send CleanAreaData for a small zone at (x, y). Returns success."""
    area_data = make_small_zone(x, y, half)
    print(f"\nSending CleanAreaData zone at ({x}, {y}), half={half}")
    print(f"  Encoded: {area_data}")
    payload = {
        "CleanAreaData": {
            "AreaData": area_data,
            "CleanLoop": 1,
            "Enable": 1,
        }
    }
    result = client.set_properties(iot_id, payload)
    return result is not None


def parse_int_prop(props, key):
    """Extract an integer property value."""
    raw = props.get(key, {})
    val = raw.get("value", raw) if isinstance(raw, dict) else raw
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def poll_work_mode(client, iot_id, timeout=180, poll_interval=3,
                   stop_on_wm=None, action_on_wm_power=None):
    """Poll WorkMode until terminal state or timeout.

    stop_on_wm: set of WorkMode values that trigger early return
    action_on_wm_power: dict {(wm, power): callable(client, iot_id)} — trigger
        when WorkMode AND PowerSwitch match. Fired once then removed.
    """
    deadline = time.time() + timeout
    last_wm = None

    while time.time() < deadline:
        time.sleep(poll_interval)
        props = client.get_properties(iot_id, POLL_PROPS)
        if props is None:
            print(f"  [{time.strftime('%H:%M:%S')}] Failed to fetch properties")
            continue

        wm = log_state(props)
        power = parse_int_prop(props, "PowerSwitch")

        # Execute action on specific (WorkMode, PowerSwitch) pairs
        if action_on_wm_power:
            key = (wm, power)
            if key in action_on_wm_power:
                action_on_wm_power[key](client, iot_id)
                del action_on_wm_power[key]

        if stop_on_wm and wm in stop_on_wm:
            return wm

        # Check for idle/docked states (robot stopped)
        if wm in WORKMODE_IDLE and last_wm not in WORKMODE_IDLE and last_wm is not None:
            print(f"\n  Robot went idle (WorkMode {wm}).")
            return wm

        last_wm = wm

    print(f"\n  Timed out after {timeout}s")
    return -1


def test_spot_clean(args):
    """Test spot_clean: navigate → pause → spot clean (WM 5) → return to dock."""
    client = get_client()
    x, y = args.x, args.y

    print(f"\n=== TEST: spot_clean at ({x}, {y}) ===")
    print("Expected: navigate (WM 19) → pause (WM 2) → spot clean (WM 5) → return (WM 8) → dock\n")

    # Check initial state
    print("Initial state:")
    props = client.get_properties(args.iot_id, POLL_PROPS)
    if props:
        log_state(props)

    # Send small zone at target
    if not send_zone_clean(client, args.iot_id, x, y):
        print("FAILED to send CleanAreaData command!")
        return

    # Phase 1: wait for arrival (WM 19 + Power 1), then pause + switch to spot clean
    def switch_to_spot_clean(client, iot_id):
        print("\n  >>> WM 19 + Power 1 (arrived at zone)! Pausing (WM 2)...")
        client.set_properties(iot_id, {"WorkMode": 2})
        time.sleep(1)
        print("  >>> Switching to spot clean (WM 5)...")
        result = client.set_properties(iot_id, {"WorkMode": 5})
        if result is not None:
            print("  >>> Spot clean started!")
        else:
            print("  >>> FAILED to start spot clean!")

    print("\nPhase 1: Navigating to target (waiting for WM 19 + Power 1)...")
    try:
        result = poll_work_mode(
            client, args.iot_id,
            timeout=args.timeout,
            action_on_wm_power={(19, 1): switch_to_spot_clean},
            stop_on_wm={5},  # stop polling once spot clean is active
        )
    except KeyboardInterrupt:
        print("\n\nAborted by user. Sending robot to dock...")
        client.set_properties(args.iot_id, {"WorkMode": 2})
        time.sleep(1)
        client.set_properties(args.iot_id, {"WorkMode": 8})
        return

    if result != 5:
        print(f"\nFailed to reach spot clean mode (WorkMode={result})")
        return

    # Phase 2: wait for spot clean to finish, then return to dock
    print("\nPhase 2: Spot cleaning (waiting for WM to leave 5)...")
    try:
        result = poll_work_mode(
            client, args.iot_id,
            timeout=args.timeout,
            stop_on_wm=WORKMODE_IDLE | {2, 12},  # stopped or idle
        )
    except KeyboardInterrupt:
        print("\n\nAborted by user. Sending robot to dock...")
        client.set_properties(args.iot_id, {"WorkMode": 2})
        time.sleep(1)
        client.set_properties(args.iot_id, {"WorkMode": 8})
        return

    # Send return to dock if not already returning/docked
    if result not in WORKMODE_IDLE and result != 8:
        print(f"\nSpot clean ended (WM={result}). Sending return to dock (WM 8)...")
        client.set_properties(args.iot_id, {"WorkMode": 8})
    elif result in WORKMODE_IDLE:
        print(f"\nRobot already docked (WM={result}).")
        return

    # Brief poll to confirm return
    print("\nWaiting for robot to dock...")
    try:
        result = poll_work_mode(
            client, args.iot_id,
            timeout=120,
            stop_on_wm=WORKMODE_IDLE,
        )
    except KeyboardInterrupt:
        pass

    if result in WORKMODE_IDLE:
        print(f"\nSUCCESS: spot_clean completed full cycle (WM={result}).")
    else:
        print(f"\nEnded with WorkMode={result}")


def test_goto(args):
    """Test goto: navigate → pause on WorkMode 19 → return to dock."""
    client = get_client()
    x, y = args.x, args.y

    print(f"\n=== TEST: goto ({x}, {y}) — navigate and pause ===")
    print("Expected: navigate → zone clean starts (WM 19) → PAUSE → return to dock\n")

    # Check initial state
    print("Initial state:")
    props = client.get_properties(args.iot_id, POLL_PROPS)
    if props:
        log_state(props)

    # Send small zone at target
    if not send_zone_clean(client, args.iot_id, x, y):
        print("FAILED to send CleanAreaData command!")
        return

    def pause_robot(client, iot_id):
        print("\n  >>> WM 19 + Power 1 (arrived at zone)! Sending WorkMode=2 (standby)...")
        result = client.set_properties(iot_id, {"WorkMode": 2})
        if result is not None:
            print("  >>> Pause command sent successfully!")
        else:
            print("  >>> FAILED to send pause command!")

    print("\nPolling state transitions (waiting for WM 19 + Power 1)...")
    try:
        result = poll_work_mode(
            client, args.iot_id,
            timeout=args.timeout,
            action_on_wm_power={(19, 1): pause_robot},
            stop_on_wm=WORKMODE_IDLE | {2, 12},  # idle, standby, or paused
        )
    except KeyboardInterrupt:
        print("\n\nAborted by user. Sending robot to dock...")
        client.set_properties(args.iot_id, {"WorkMode": 2})
        time.sleep(1)
        client.set_properties(args.iot_id, {"WorkMode": 8})
        return

    # Check final state
    print("\nWaiting 5s then checking final state...")
    time.sleep(5)
    props = client.get_properties(args.iot_id, POLL_PROPS)
    if props:
        wm = log_state(props)
        if wm in (2, 12):
            print("\nSUCCESS: Robot paused at target!")
        elif wm in WORKMODE_IDLE:
            print(f"\nRobot idle (WorkMode={wm}) — may have returned to dock")
        else:
            print(f"\nRobot WorkMode={wm}")

    # Return to dock
    print("\nSending robot back to dock (WorkMode=8)...")
    result = client.set_properties(args.iot_id, {"WorkMode": 8})
    if result is not None:
        print("Return-to-dock command sent.")
        for _ in range(5):
            time.sleep(3)
            props = client.get_properties(args.iot_id, POLL_PROPS)
            if props:
                wm = log_state(props)
                if wm in WORKMODE_IDLE:
                    print("\nRobot docked successfully!")
                    break
    else:
        print("FAILED to send return-to-dock command!")


def test_abort(args):
    """Emergency: send robot back to dock."""
    client = get_client()
    print("\nSending WorkMode=2 (standby)...")
    client.set_properties(args.iot_id, {"WorkMode": 2})
    time.sleep(2)
    print("Sending WorkMode=8 (return to dock)...")
    client.set_properties(args.iot_id, {"WorkMode": 8})
    print("Done. Robot should be returning to dock.")


def main():
    parser = argparse.ArgumentParser(description="Test spot_clean and goto via CleanAreaData")
    parser.add_argument("--iot-id", default=DEFAULT_IOT_ID)
    parser.add_argument("--timeout", type=int, default=180, help="Polling timeout in seconds")

    sub = parser.add_subparsers(dest="command")

    p_spot = sub.add_parser("spot_clean", help="Navigate to point zone, clean, return")
    p_spot.add_argument("x", type=int, help="X coordinate")
    p_spot.add_argument("y", type=int, help="Y coordinate")

    p_goto = sub.add_parser("goto", help="Navigate to point zone, pause, return to dock")
    p_goto.add_argument("x", type=int, help="X coordinate")
    p_goto.add_argument("y", type=int, help="Y coordinate")

    sub.add_parser("abort", help="Emergency: send robot to dock")

    args = parser.parse_args()

    if args.command == "spot_clean":
        test_spot_clean(args)
    elif args.command == "goto":
        test_goto(args)
    elif args.command == "abort":
        test_abort(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
