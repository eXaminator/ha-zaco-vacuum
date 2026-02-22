"""DataUpdateCoordinator for ZACO integration.

Drives all polling via HA's standard DataUpdateCoordinator pattern:
- 30s full polls (ALL_PROPERTIES) when idle
- Continuous fast polls (FAST_PROPERTIES) when robot is active,
  with a full poll every 30s interleaved
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_SCAN_INTERVAL,
    FAST_POLL_INTERVAL,
    MAP_POLL_INTERVAL,
)
from .zaco import Zaco
from .zaco.api_client import AliyunConnectionError, AliyunTokenExpiredError

_LOGGER = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 3


class ZacoDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls device properties via the Zaco library."""

    def __init__(self, hass: HomeAssistant, zaco: Zaco) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"zaco_{zaco.iot_id}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.zaco = zaco
        self._last_full_poll: float = 0.0
        self._last_map_poll: float = 0.0
        self._consecutive_errors: int = 0
        self._pending_refresh: asyncio.Task[None] | None = None
        self._refresh_target: float = 0.0

        # Wire MQTT pushes to HA entity updates.
        zaco.on_data_updated = self._handle_mqtt_push

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch device properties with two-tier polling.

        When active: continuous fast polls with a full poll every 30s.
        When idle: full poll every 30s.
        """
        now = time.monotonic()
        needs_full = (
            now - self._last_full_poll >= DEFAULT_SCAN_INTERVAL
            or self.data is None
        )
        include_maps = (
            now - self._last_map_poll >= MAP_POLL_INTERVAL
            or self.data is None
        )

        poll_type = "fast" if not needs_full else ("full+maps" if include_maps else "full")
        _LOGGER.debug(
            "Poll start: type=%s, interval=%s, errors=%d",
            poll_type, self.update_interval, self._consecutive_errors,
        )

        try:
            data = await self.zaco.refresh(
                fast=not needs_full, include_maps=include_maps,
            )
            if needs_full:
                self._last_full_poll = now
            if include_maps:
                self._last_map_poll = now
        except AliyunTokenExpiredError as err:
            _LOGGER.debug("Token expired during poll")
            raise ConfigEntryAuthFailed(
                "Authentication expired, please reconfigure"
            ) from err
        except AliyunConnectionError as err:
            self._consecutive_errors += 1
            if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                raise UpdateFailed(
                    f"Connection error ({self._consecutive_errors} consecutive): {err}"
                ) from err
            _LOGGER.debug(
                "Connection error (attempt %d/%d), will retry: %s",
                self._consecutive_errors, MAX_CONSECUTIVE_ERRORS, err,
            )
            if self.data is not None:
                return self.data
            raise UpdateFailed(f"Connection error: {err}") from err
        except ConfigEntryAuthFailed:
            raise
        except UpdateFailed:
            raise
        except Exception as err:
            self._consecutive_errors += 1
            _LOGGER.exception("Unexpected error during refresh")
            raise UpdateFailed(f"Unexpected error: {err}") from err

        self._consecutive_errors = 0

        # Adjust poll interval based on activity state
        if self.zaco.is_active:
            self.update_interval = timedelta(seconds=FAST_POLL_INTERVAL)
        else:
            self.update_interval = timedelta(seconds=DEFAULT_SCAN_INTERVAL)

        _LOGGER.debug(
            "Poll done: type=%s, props=%d, is_active=%s, next_interval=%ss",
            poll_type, len(data) if data else 0,
            self.zaco.is_active, self.update_interval.total_seconds(),
        )

        return data

    def optimistic_update(self, properties: dict[str, Any]) -> None:
        """Optimistically update data with values we just sent.

        Patches coordinator data immediately so entities reflect the
        new value without waiting for the next poll. The delayed refresh
        will confirm (or revert) from the cloud ~3s later.
        """
        if self.data is None:
            _LOGGER.debug("Optimistic update skipped: no data yet")
            return
        _LOGGER.debug("Optimistic update: %s", list(properties.keys()))
        now_ms = int(time.time() * 1000)
        updated = dict(self.data)
        for key, value in properties.items():
            updated[key] = {"value": value, "time": now_ms}
        self.zaco._data = updated
        self.async_set_updated_data(updated)

    def async_request_delayed_refresh(self, delay: float = 3.0) -> None:
        """Schedule a refresh after a set operation, debounced.

        Safe for rapid calls — uses a monotonic timestamp target instead
        of task cancellation. Multiple calls just push the target forward;
        only one background loop runs at a time.
        """
        self._refresh_target = time.monotonic() + delay
        if self._pending_refresh is None or self._pending_refresh.done():
            _LOGGER.debug("Scheduling delayed refresh in %.1fs", delay)
            self._pending_refresh = self.hass.async_create_task(
                self._delayed_refresh_loop()
            )

    async def _delayed_refresh_loop(self) -> None:
        """Wait until the refresh target time, then poll once."""
        try:
            while True:
                now = time.monotonic()
                remaining = self._refresh_target - now
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            _LOGGER.debug("Delayed refresh executing now")
            await self.async_request_refresh()
        except asyncio.CancelledError:
            _LOGGER.debug("Delayed refresh cancelled (shutdown)")
        except Exception:
            _LOGGER.debug("Delayed refresh failed", exc_info=True)
        finally:
            self._pending_refresh = None

    def _handle_mqtt_push(self, data: dict[str, Any]) -> None:
        """Called by Zaco.merge_mqtt_push — push data to HA entities."""
        _LOGGER.debug("MQTT push received, updating %d entities", len(self._listeners))
        self.async_set_updated_data(data)

    # -- Convenience accessors for entity platforms ---------------------------

    @property
    def iot_id(self) -> str:
        return self.zaco.iot_id

    @property
    def device_info(self) -> dict[str, Any]:
        return self.zaco.device_info

    @property
    def rooms(self) -> dict[str, int]:
        return self.zaco.rooms

    @property
    def current_room(self) -> str | None:
        return self.zaco.current_room

    @property
    def active_map_slot(self) -> int | None:
        return self.zaco.active_map_slot

    @property
    def client(self):
        """Exposed for token persistence in __init__.py."""
        return self.zaco.client
