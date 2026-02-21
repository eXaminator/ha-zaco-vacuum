"""Navigation state machines: goto, spot clean, edge clean."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Callable, Coroutine

try:
    from .zone_utils import encode_clean_area, rect_to_corners
    from ._helpers import extract_current_point, parse_int_prop
except ImportError:
    import os as _os, sys as _sys
    _pkg_dir = _os.path.dirname(_os.path.abspath(__file__))
    if _pkg_dir not in _sys.path:
        _sys.path.insert(0, _pkg_dir)

    from zone_utils import encode_clean_area, rect_to_corners  # type: ignore[no-redef]
    from _helpers import extract_current_point, parse_int_prop  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# UploadDataControl renewal interval (seconds).
_UPLOAD_CONTROL_RENEWAL_INTERVAL = 55


class NavigationController:
    """Owns the long-running goto/spot-clean/edge-clean state machines.

    Stateless — no persistent instance state; all state is local to each
    running flow.  Callbacks are injected from the facade.
    """

    def __init__(
        self,
        set_props: Callable[[dict[str, Any]], Coroutine[Any, Any, bool]],
        refresh: Callable[[], Coroutine[Any, Any, dict[str, Any]]],
        get_data: Callable[[], dict[str, Any] | None],
        log: Callable[[str], None],
    ) -> None:
        self._set_props = set_props
        self._refresh = refresh
        self._get_data = get_data
        self._log = log

    async def goto_zone(
        self,
        target_x: int,
        target_y: int,
        on_arrival: Any = None,
        arrival_threshold: int = 10,
        timeout: int = 180,
        poll_interval: float = 1.0,
    ) -> None:
        """Full goto flow: save FanPower, send tiny zone, wait for arrival.

        on_arrival: optional async callable (no args) to run after pausing
        at the target (e.g. spot_clean_after_arrival).
        """
        data = self._get_data()
        original_fan_power = parse_int_prop(data or {}, "FanPower") if data else None
        self._log(f"FanPower={original_fan_power}, setting to 1 for navigation")

        await self._set_props({"FanPower": 1})
        await self._set_props(
            {"UploadDataControl": {"Status": 1, "ValidityTime": 210}}
        )
        await asyncio.sleep(1)

        half = 1
        corners = rect_to_corners(
            target_x - half, target_y - half, target_x + half, target_y + half,
        )
        area_data = encode_clean_area(*corners)
        self._log(f"Sending goto zone at ({target_x}, {target_y})")

        success = await self._set_props({
            "CleanAreaData": {
                "AreaData": area_data,
                "CleanLoop": 1,
                "Enable": 1,
            },
        })
        if not success:
            self._log("Failed to send goto zone")
            if original_fan_power is not None:
                await self._set_props({"FanPower": original_fan_power})
            return

        await self._wait_for_arrival(
            target_x=target_x, target_y=target_y,
            original_fan_power=original_fan_power,
            on_arrival=on_arrival,
            arrival_threshold=arrival_threshold,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    async def spot_clean_after_arrival(
        self,
        repeats: int = 1,
        timeout: int = 600,
        poll_interval: float = 1.0,
    ) -> None:
        """Switch to spot clean after goto arrival, then return to dock."""
        for pass_num in range(repeats):
            if pass_num > 0:
                await self._set_props({"WorkMode": 2})
                self._log("Paused between spot clean passes")
                await asyncio.sleep(2)
            else:
                await asyncio.sleep(1)

            await self._set_props({"WorkMode": 5})
            self._log(f"Started spot clean pass {pass_num + 1}/{repeats}")

            deadline = asyncio.get_event_loop().time() + timeout
            grace_until = asyncio.get_event_loop().time() + 15
            seen_wm5 = False

            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(poll_interval)

                await self._refresh()
                data = self._get_data()
                if not data:
                    continue

                wm = parse_int_prop(data, "WorkMode")
                self._log(f"Spot clean monitor: WM={wm}")

                if wm == 5:
                    seen_wm5 = True
                    continue

                if not seen_wm5 and asyncio.get_event_loop().time() < grace_until:
                    continue

                self._log(
                    f"Spot clean pass {pass_num + 1}/{repeats} finished (WM={wm})"
                )
                break
            else:
                self._log(f"Timed out after {timeout}s waiting for spot clean")
                break

        await self._refresh()
        data = self._get_data()
        wm = parse_int_prop(data or {}, "WorkMode") if data else None
        if wm not in (8, 9, 11, 16, 17):
            await self._set_props({"WorkMode": 8})
            self._log("Spot clean done, sent return to dock")
        else:
            self._log(f"Spot clean done, robot already WM={wm}")

    async def edge_clean_after_arrival(
        self,
        repeats: int = 1,
        room_center: tuple[int, int] | None = None,
        timeout: int = 600,
        poll_interval: float = 3.0,
    ) -> None:
        """Switch to edge clean after goto arrival, monitor, multi-pass.

        For pass > 0, re-navigates to room_center using goto_zone (same
        full goto flow with FanPower, UploadDataControl, position tracking).
        """
        for pass_num in range(repeats):
            if pass_num > 0:
                # Pause first to cancel any auto-return (WM=8)
                await self._set_props({"WorkMode": 2})
                self._log("Paused between edge clean passes")
                await asyncio.sleep(2)

                # Re-navigate to room center for next pass
                if room_center is not None:
                    self._log(
                        f"Re-navigating to room center for pass {pass_num + 1}"
                    )
                    await self.goto_zone(
                        target_x=room_center[0],
                        target_y=room_center[1],
                    )
            else:
                await asyncio.sleep(1)

            await self._set_props({"WorkMode": 4})
            self._log(f"Started edge clean pass {pass_num + 1}/{repeats}")

            # Monitor: wait for edge clean to finish
            deadline = asyncio.get_event_loop().time() + timeout
            grace_until = asyncio.get_event_loop().time() + 15
            seen_active = False

            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(poll_interval)

                await self._refresh()
                data = self._get_data()
                if not data:
                    continue

                wm = parse_int_prop(data, "WorkMode")
                self._log(f"Edge clean monitor: WM={wm}")

                # Track any non-idle WM as "active"
                if wm not in (None, 2, 9, 11, 16, 17):
                    seen_active = True

                # Robot went idle — pass finished
                if wm in (2, 9, 11, 16, 17):
                    if (
                        not seen_active
                        and asyncio.get_event_loop().time() < grace_until
                    ):
                        continue
                    self._log(
                        f"Edge clean pass {pass_num + 1}/{repeats} "
                        f"finished (WM={wm})"
                    )
                    break

                # Robot started returning on its own
                if wm == 8:
                    self._log(
                        f"Edge clean pass {pass_num + 1}/{repeats} "
                        f"done, returning (WM=8)"
                    )
                    break
            else:
                self._log(f"Timed out after {timeout}s waiting for edge clean")
                break

        # After all passes, return to dock if not already
        await self._refresh()
        data = self._get_data()
        wm = parse_int_prop(data or {}, "WorkMode") if data else None
        if wm not in (8, 9, 11, 16, 17):
            await self._set_props({"WorkMode": 8})
            self._log("Edge clean done, sent return to dock")
        else:
            self._log(f"Edge clean done, robot already WM={wm}")

    # -- Private --------------------------------------------------------------

    async def _wait_for_arrival(
        self,
        target_x: int,
        target_y: int,
        original_fan_power: int | None = None,
        on_arrival: Any = None,
        arrival_threshold: int = 10,
        timeout: int = 180,
        poll_interval: float = 1.0,
    ) -> None:
        """Wait for the robot to reach the target, then pause it."""
        renewal_task = asyncio.create_task(self._renew_upload_control())
        try:
            deadline = asyncio.get_event_loop().time() + timeout
            grace_until = asyncio.get_event_loop().time() + 10
            seen_active = False

            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(poll_interval)

                await self._refresh()
                data = self._get_data()
                if not data:
                    continue

                work_mode = parse_int_prop(data, "WorkMode")
                if work_mode is None:
                    continue

                if work_mode in (8, 19):
                    seen_active = True

                if work_mode in (9, 11, 16, 17):
                    if seen_active or asyncio.get_event_loop().time() > grace_until:
                        self._log(
                            f"Robot idle (WorkMode {work_mode}) before goto arrival"
                        )
                        return
                    self._log(f"WM={work_mode} (grace period, waiting for zone start)")
                    continue

                if work_mode not in (8, 19):
                    pos = extract_current_point(data)
                    self._log(
                        f"WM={work_mode} pos="
                        f"{'N/A' if not pos else f'({pos[0]},{pos[1]})'}"
                    )
                    continue

                pos = extract_current_point(data)
                if pos is None:
                    self._log(f"WM={work_mode} pos=N/A")
                    continue

                dist = math.hypot(pos[0] - target_x, pos[1] - target_y)
                self._log(
                    f"WM={work_mode} pos=({pos[0]},{pos[1]}) "
                    f"target=({target_x},{target_y}) dist={dist:.1f}"
                )

                if dist <= arrival_threshold:
                    await self._set_props({"WorkMode": 2})
                    self._log(f"Robot arrived (dist={dist:.1f}), paused")
                    if original_fan_power is not None:
                        await self._set_props({"FanPower": original_fan_power})
                        self._log(f"Restored FanPower to {original_fan_power}")
                        original_fan_power = None
                    if on_arrival is not None:
                        await on_arrival()
                    return

            self._log(f"Timed out after {timeout}s waiting for goto arrival")
        finally:
            renewal_task.cancel()
            try:
                await self._set_props(
                    {"UploadDataControl": {"Status": 0, "ValidityTime": 210}}
                )
            except Exception:
                _LOGGER.debug("Failed to disable fast upload", exc_info=True)
            if original_fan_power is not None:
                try:
                    await self._set_props({"FanPower": original_fan_power})
                    self._log(f"Restored FanPower to {original_fan_power}")
                except Exception:
                    _LOGGER.warning(
                        "Failed to restore FanPower to %s",
                        original_fan_power, exc_info=True,
                    )

    async def _renew_upload_control(self) -> None:
        """Re-send UploadDataControl periodically to keep fast reporting active."""
        while True:
            await asyncio.sleep(_UPLOAD_CONTROL_RENEWAL_INTERVAL)
            try:
                await self._set_props(
                    {"UploadDataControl": {"Status": 1, "ValidityTime": 210}}
                )
                self._log("Renewed UploadDataControl")
            except Exception:
                _LOGGER.debug("Failed to renew UploadDataControl", exc_info=True)
