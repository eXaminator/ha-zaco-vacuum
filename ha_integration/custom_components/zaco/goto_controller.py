"""Goto / spot-clean state machine for ZACO robot vacuum.

Standalone async module with no Home Assistant dependencies.  Both the HA
integration (__init__.py) and standalone test scripts (test_goto.py) import
from here.

The functions accept injectable async callables instead of concrete types:
  - get_data: async () -> dict | None   (reads current property state)
  - set_props: async (dict) -> bool      (sends property changes to device)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any

try:
    from .zone_utils import decode_current_point, encode_clean_area, rect_to_corners
except ImportError:
    from zone_utils import decode_current_point, encode_clean_area, rect_to_corners

_LOGGER = logging.getLogger(__name__)

# UploadDataControl renewal interval (seconds).  The ZACO app re-sends every
# 60s (MapX901Presenter.java Observable.interval(0, 1, MINUTES)).  We use 55s
# to avoid any gap near the boundary.
_UPLOAD_CONTROL_RENEWAL_INTERVAL = 55


async def _renew_upload_control(
    set_props: Callable[[dict], Awaitable[bool]],
    interval: float = _UPLOAD_CONTROL_RENEWAL_INTERVAL,
) -> None:
    """Re-send UploadDataControl periodically to keep fast reporting active.

    The device's ValidityTime=210s is a safety expiry, but the device may
    reset earlier. The ZACO app also immediately re-enables when the device
    pushes Status=0. We approximate that by re-sending on a fixed interval.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await set_props({"UploadDataControl": {"Status": 1, "ValidityTime": 210}})
            _LOGGER.debug("Renewed UploadDataControl")
        except Exception:
            _LOGGER.debug("Failed to renew UploadDataControl", exc_info=True)


# ---------------------------------------------------------------------------
# Property parsing helpers
# ---------------------------------------------------------------------------

def parse_int_prop(props: dict, key: str) -> int | None:
    """Extract an integer property value from a properties dict."""
    raw = props.get(key, {})
    val = raw.get("value", raw) if isinstance(raw, dict) else raw
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def extract_current_point(props: dict) -> tuple[int, int] | None:
    """Extract and decode CurrentPoint from a properties dict.

    Parses RealMapRoadData, extracts CurrentPoint, and decodes it via
    decode_current_point() from zone_utils.

    Returns (x, y) in robot coordinates, or None if unavailable.
    """
    raw = props.get("RealMapRoadData", {})
    val = raw.get("value", raw) if isinstance(raw, dict) else raw
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(val, dict):
        return None
    cp_raw = val.get("CurrentPoint")
    if cp_raw is None:
        return None
    try:
        return decode_current_point(int(cp_raw))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Goto arrival detection
# ---------------------------------------------------------------------------

async def wait_and_pause(
    get_data: Callable[[], Awaitable[dict | None]],
    set_props: Callable[[dict], Awaitable[bool]],
    target_x: int,
    target_y: int,
    original_fan_power: int | None = None,
    on_arrival: Callable[
        [Callable[[], Awaitable[dict | None]], Callable[[dict], Awaitable[bool]], Callable[[], Awaitable[None]] | None],
        Awaitable[None],
    ] | None = None,
    refresh: Callable[[], Awaitable[None]] | None = None,
    arrival_threshold: int = 10,
    timeout: int = 180,
    poll_interval: float = 1.0,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    """Wait for the robot to reach the target point, then pause it.

    Reads property state via get_data() every poll_interval seconds.
    Decodes the robot's CurrentPoint from RealMapRoadData and computes
    Euclidean distance to the target. Pauses (WorkMode 2) when distance
    falls below arrival_threshold.

    Position is checked during both zone cleaning (WM 19) and return to
    dock (WM 8), because the tiny navigation zone may finish before the
    robot reaches a distant target.

    If on_arrival is provided, it is called after pausing (before FanPower
    restore). This allows chaining actions like spot clean after navigation.

    FanPower is restored to its original value on every exit path.

    Parameters:
        get_data: async callable returning current property dict (or None)
        set_props: async callable to set device properties
        target_x, target_y: target coordinates
        original_fan_power: FanPower value to restore on exit (or None)
        on_arrival: optional async callback(get_data, set_props, refresh) after pause
        refresh: optional async callable to trigger data refresh before each poll
        arrival_threshold: distance threshold for arrival detection
        timeout: max seconds to wait
        poll_interval: seconds between polls
        log_fn: optional callable for status output (e.g. print)
    """
    def _log(msg: str) -> None:
        _LOGGER.debug(msg)
        if log_fn is not None:
            log_fn(msg)

    renewal_task = asyncio.create_task(_renew_upload_control(set_props))
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        # Grace period: after sending the zone command, the robot takes a
        # few seconds to transition from idle to WM 19 (zone cleaning).
        # During this window, coordinator.data may still show the old
        # WorkMode (e.g. 9=docked, 16=idle). We must NOT treat these as
        # "abort" signals until the grace period has elapsed.
        grace_until = asyncio.get_event_loop().time() + 10
        seen_active = False

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)

            if refresh is not None:
                await refresh()
            data = await get_data()
            if not data:
                continue

            work_mode = parse_int_prop(data, "WorkMode")
            if work_mode is None:
                continue

            # Track whether we've ever seen an active mode (19, 8, etc.)
            if work_mode in (8, 19):
                seen_active = True

            # Abort if robot went idle/error — but only after we've seen
            # it become active, or after the grace period has elapsed
            if work_mode in (9, 11, 16, 17):
                if seen_active or asyncio.get_event_loop().time() > grace_until:
                    _log(f"Robot idle (WorkMode {work_mode}) before goto arrival")
                    return
                _log(f"WM={work_mode} (grace period, waiting for zone start)")
                continue

            # Check position during zone cleaning (19) AND return to dock (8)
            if work_mode not in (8, 19):
                pos = extract_current_point(data)
                _log(
                    f"WM={work_mode} pos="
                    f"{'N/A' if not pos else f'({pos[0]},{pos[1]})'}"
                )
                continue

            pos = extract_current_point(data)
            if pos is None:
                _log(f"WM={work_mode} pos=N/A")
                continue

            dist = math.hypot(pos[0] - target_x, pos[1] - target_y)
            _log(
                f"WM={work_mode} pos=({pos[0]},{pos[1]}) "
                f"target=({target_x},{target_y}) dist={dist:.1f}"
            )

            if dist <= arrival_threshold:
                await set_props({"WorkMode": 2})
                _log(f"Robot arrived (dist={dist:.1f}), paused")
                # Restore FanPower BEFORE on_arrival so spot clean uses
                # the user's original power, not the quiet navigation power.
                if original_fan_power is not None:
                    await set_props({"FanPower": original_fan_power})
                    _log(f"Restored FanPower to {original_fan_power}")
                    original_fan_power = None  # prevent double-restore in finally
                if on_arrival is not None:
                    await on_arrival(get_data, set_props, refresh)
                return

        _log(f"Timed out after {timeout}s waiting for goto arrival")
    finally:
        renewal_task.cancel()
        # Disable fast data upload (return to normal reporting rate)
        try:
            await set_props({"UploadDataControl": {"Status": 0, "ValidityTime": 210}})
        except Exception:
            _LOGGER.debug("Failed to disable fast upload", exc_info=True)
        # Always restore original fan power
        if original_fan_power is not None:
            try:
                await set_props({"FanPower": original_fan_power})
                _log(f"Restored FanPower to {original_fan_power}")
            except Exception:
                _LOGGER.warning(
                    "Failed to restore FanPower to %s", original_fan_power,
                    exc_info=True,
                )


# ---------------------------------------------------------------------------
# Spot clean after arrival
# ---------------------------------------------------------------------------

async def spot_clean_after_arrival(
    get_data: Callable[[], Awaitable[dict | None]],
    set_props: Callable[[dict], Awaitable[bool]],
    refresh: Callable[[], Awaitable[None]] | None = None,
    repeats: int = 1,
    timeout: int = 600,
    poll_interval: float = 1.0,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    """Switch to spot clean after goto arrival, then return to dock.

    Called as an on_arrival callback from wait_and_pause. The robot is
    already paused at the target point. We start spot clean (WM 5), wait
    for it to finish, then send return to dock (WM 8).

    If repeats > 1, we pause between passes (WM 2) and re-send WM 5.
    Return to dock is only sent after all passes complete.
    """
    def _log(msg: str) -> None:
        _LOGGER.debug(msg)
        if log_fn is not None:
            log_fn(msg)

    for pass_num in range(repeats):
        if pass_num > 0:
            # Pause between passes — let the robot settle before restarting
            await set_props({"WorkMode": 2})
            _log(f"Paused between spot clean passes")
            await asyncio.sleep(2)
        else:
            await asyncio.sleep(1)  # let initial pause settle

        await set_props({"WorkMode": 5})
        _log(f"Started spot clean pass {pass_num + 1}/{repeats}")

        deadline = asyncio.get_event_loop().time() + timeout
        # Grace period: after sending WM 5, the coordinator data may still
        # show WM 2 (paused). We must wait for WM 5 to appear before
        # watching for it to disappear.
        grace_until = asyncio.get_event_loop().time() + 15
        seen_wm5 = False

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)

            if refresh is not None:
                await refresh()
            data = await get_data()
            if not data:
                continue

            wm = parse_int_prop(data, "WorkMode")
            _log(f"Spot clean monitor: WM={wm}")

            if wm == 5:
                seen_wm5 = True
                continue  # still spot cleaning

            # Don't exit until we've seen WM 5, or the grace period elapsed
            if not seen_wm5 and asyncio.get_event_loop().time() < grace_until:
                continue

            # This pass is done
            _log(f"Spot clean pass {pass_num + 1}/{repeats} finished (WM={wm})")
            break
        else:
            _log(f"Timed out after {timeout}s waiting for spot clean")
            break  # don't attempt further passes after timeout

    # All passes done (or timed out) — send return to dock
    if refresh is not None:
        await refresh()
    data = await get_data()
    wm = parse_int_prop(data or {}, "WorkMode") if data else None
    if wm not in (8, 9, 11, 16, 17):
        await set_props({"WorkMode": 8})
        _log("Spot clean done, sent return to dock")
    else:
        _log(f"Spot clean done, robot already WM={wm}")


# ---------------------------------------------------------------------------
# Full goto flow: FanPower + zone + arrival detection
# ---------------------------------------------------------------------------

async def send_goto_zone(
    get_data: Callable[[], Awaitable[dict | None]],
    set_props: Callable[[dict], Awaitable[bool]],
    target_x: int,
    target_y: int,
    on_arrival: Callable[
        [Callable[[], Awaitable[dict | None]], Callable[[dict], Awaitable[bool]], Callable[[], Awaitable[None]] | None],
        Awaitable[None],
    ] | None = None,
    refresh: Callable[[], Awaitable[None]] | None = None,
    arrival_threshold: int = 10,
    timeout: int = 180,
    poll_interval: float = 1.0,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    """Full goto flow: save FanPower, send tiny zone, wait for arrival.

    1. Read current FanPower via get_data()
    2. Set FanPower=1 (quiet navigation)
    3. Send 2x2 CleanAreaData zone at target
    4. Call wait_and_pause() to monitor arrival and restore FanPower

    Returns True if the zone was sent successfully, False otherwise.
    On failure, FanPower is restored before returning.
    """
    def _log(msg: str) -> None:
        _LOGGER.debug(msg)
        if log_fn is not None:
            log_fn(msg)

    # Read current FanPower to restore later
    data = await get_data()
    original_fan_power = parse_int_prop(data or {}, "FanPower") if data else None
    _log(f"FanPower={original_fan_power}, setting to 1 for navigation")

    await set_props({"FanPower": 1})

    # Enable fast data upload — tells the device to report position at high
    # frequency (same as ZACO app's setDeviceDataUpSpeed on map view open).
    # ValidityTime=210 auto-expires after 3.5 min as a safety net.
    await set_props({"UploadDataControl": {"Status": 1, "ValidityTime": 210}})
    await asyncio.sleep(1)

    # Build and send tiny zone
    half = 1  # 2x2 unit zone -- smallest practical size
    corners = rect_to_corners(
        target_x - half, target_y - half, target_x + half, target_y + half,
    )
    area_data = encode_clean_area(*corners)
    _log(f"Sending goto zone at ({target_x}, {target_y})")

    success = await set_props({
        "CleanAreaData": {
            "AreaData": area_data,
            "CleanLoop": 1,
            "Enable": 1,
        },
    })
    if not success:
        _log("Failed to send goto zone")
        if original_fan_power is not None:
            await set_props({"FanPower": original_fan_power})
        return False

    # Monitor arrival (restores FanPower in finally block)
    await wait_and_pause(
        get_data, set_props,
        target_x=target_x, target_y=target_y,
        original_fan_power=original_fan_power,
        on_arrival=on_arrival,
        refresh=refresh,
        arrival_threshold=arrival_threshold,
        timeout=timeout,
        poll_interval=poll_interval,
        log_fn=log_fn,
    )
    return True
