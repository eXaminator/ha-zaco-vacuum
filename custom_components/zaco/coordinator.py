"""DataUpdateCoordinator for ZACO integration.

Drives all polling via HA's standard DataUpdateCoordinator pattern:
- 30s full polls (ALL_PROPERTIES) when idle
- Continuous fast polls (FAST_PROPERTIES) when robot is active,
  with a full poll every 30s interleaved
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_IDENTITY_ID,
    CONF_IOT_HOST,
    CONF_IOT_TOKEN,
    CONF_IOT_TOKEN_EXPIRY,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_EXPIRY,
    DEFAULT_SCAN_INTERVAL,
    FAST_POLL_INTERVAL,
    MAP_POLL_INTERVAL,
)
from .zaco import Zaco, MapRenderer
from .zaco.api_client import AliyunConnectionError, AliyunTokenExpiredError
from .zaco.path_tracker import CleaningSnapshot

_LOGGER = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 3


class ZacoDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls device properties via the Zaco library."""

    def __init__(
        self, hass: HomeAssistant, zaco: Zaco, entry: ConfigEntry | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"zaco_{zaco.iot_id}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.zaco = zaco
        self._entry = entry
        self._last_full_poll: float = 0.0
        self._last_map_poll: float = 0.0
        self._consecutive_errors: int = 0
        self._pending_refresh: asyncio.Task[None] | None = None
        self._refresh_target: float = 0.0

        # Last cleaning snapshot image (PNG bytes) — rendered once on session end
        self.last_cleaning_image: bytes | None = None
        self._last_snapshot_end_ms: int = 0
        self._snapshot_renderer = MapRenderer()

        # Load persisted snapshot from disk on startup
        self._load_persisted_snapshot()

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
            # Token may have been invalidated server-side (e.g. code 29003)
            # even though ensure_token_valid() thought it was fine.
            # Try a full re-auth and retry once.
            _LOGGER.warning("Auth error during poll, attempting re-auth: %s", err)
            client = self.zaco.client
            if await client.reauth():
                _LOGGER.info("Re-auth succeeded, retrying poll")
                self._persist_tokens()
                try:
                    data = await self.zaco.refresh(
                        fast=not needs_full, include_maps=include_maps,
                    )
                    if needs_full:
                        self._last_full_poll = now
                    if include_maps:
                        self._last_map_poll = now
                except AliyunTokenExpiredError as err2:
                    _LOGGER.error("Auth still failing after re-auth: %s", err2)
                    raise ConfigEntryAuthFailed(
                        "Authentication expired, please reconfigure"
                    ) from err2
            else:
                _LOGGER.error("Re-auth failed, giving up")
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

        # Persist tokens if they were refreshed during this poll
        self._persist_tokens()

        # Render last cleaning snapshot if a new one appeared
        await self._maybe_render_snapshot()

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

    def _persist_tokens(self) -> None:
        """Persist current API tokens to the config entry."""
        if self._entry is None:
            return
        client = self.zaco.client
        new_data = dict(self._entry.data)
        changed = False
        for attr, key in [
            ("iot_token", CONF_IOT_TOKEN),
            ("refresh_token", CONF_REFRESH_TOKEN),
            ("identity_id", CONF_IDENTITY_ID),
            ("iot_token_expiry", CONF_IOT_TOKEN_EXPIRY),
            ("refresh_token_expiry", CONF_REFRESH_TOKEN_EXPIRY),
            ("iot_host", CONF_IOT_HOST),
        ]:
            val = getattr(client, attr)
            if val is not None and val != new_data.get(key):
                new_data[key] = val
                changed = True
        if changed:
            _LOGGER.debug("Persisting refreshed tokens to config entry")
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)
            self.zaco.update_mqtt_token(client.iot_token)

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
    def last_cleaning(self) -> CleaningSnapshot | None:
        return self.zaco.last_cleaning

    @property
    def client(self):
        """Exposed for token persistence in __init__.py."""
        return self.zaco.client

    # -- Last cleaning snapshot persistence -----------------------------------

    def _snapshot_path(self, ext: str) -> str:
        """Return path for persisted snapshot files."""
        return self.hass.config.path(f"zaco_last_clean_{self.zaco.iot_id}.{ext}")

    def _load_persisted_snapshot(self) -> None:
        """Load last cleaning snapshot from disk (called once at startup)."""
        png_path = self._snapshot_path("png")
        json_path = self._snapshot_path("json")
        try:
            if os.path.exists(png_path) and os.path.exists(json_path):
                with open(png_path, "rb") as f:
                    self.last_cleaning_image = f.read()
                with open(json_path, "r") as f:
                    meta = json.load(f)
                self._last_snapshot_end_ms = meta.get("end_ms", 0)
                # Restore the snapshot on the PathTracker so entities can read it
                self.zaco._path.last_cleaning = CleaningSnapshot(
                    path=[],  # path not needed for display — we have the rendered image
                    start_ms=meta.get("start_ms"),
                    end_ms=meta.get("end_ms", 0),
                    clean_time_min=meta.get("clean_time_min", 0.0),
                    clean_area_m2=meta.get("clean_area_m2", 0.0),
                )
                _LOGGER.debug(
                    "Loaded persisted last cleaning snapshot (%d bytes)",
                    len(self.last_cleaning_image),
                )
        except Exception:
            _LOGGER.debug("No persisted snapshot to load", exc_info=True)

    async def _maybe_render_snapshot(self) -> None:
        """Render and persist a new snapshot if the last_cleaning changed."""
        snapshot = self.zaco.last_cleaning
        if snapshot is None or not snapshot.path:
            return
        if snapshot.end_ms == self._last_snapshot_end_ms:
            return  # already rendered this one

        _LOGGER.debug(
            "Rendering last cleaning snapshot (%d points)", len(snapshot.path),
        )

        # Get SLAM map data for the background
        slot = self.zaco.active_map_slot
        slam_map_val = None
        charger_val = None
        if self.data:
            if slot:
                slam_raw = self.data.get(f"SaveMapDataX9_{slot}", {})
                slam_map_val = (
                    slam_raw.get("value")
                    if isinstance(slam_raw, dict)
                    else slam_raw
                )
            charger_raw = self.data.get("ChargerPoint", {})
            charger_val = (
                charger_raw.get("value")
                if isinstance(charger_raw, dict)
                else charger_raw
            )

        # Build stats text overlay
        stats_parts: list[str] = []
        if snapshot.clean_time_min:
            mins = snapshot.clean_time_min
            if mins >= 60:
                stats_parts.append(f"{int(mins // 60)}h {int(mins % 60)}min")
            else:
                stats_parts.append(f"{int(mins)} min")
        if snapshot.clean_area_m2:
            stats_parts.append(f"{snapshot.clean_area_m2:.1f} qm")
        stats_text = "  |  ".join(stats_parts) if stats_parts else None

        p2b = self.zaco.partition_to_bitmask or None
        try:
            image_bytes, _calibration = await self.hass.async_add_executor_job(
                self._snapshot_renderer.render,
                None,  # no road_data_value (robot pos not relevant for snapshot)
                charger_val,
                slam_map_val,
                snapshot.path,
                p2b,
                stats_text,
            )
        except Exception:
            _LOGGER.exception("Failed to render last cleaning snapshot")
            return

        if not image_bytes:
            _LOGGER.debug("Snapshot render returned empty")
            return

        self.last_cleaning_image = image_bytes
        self._last_snapshot_end_ms = snapshot.end_ms

        # Persist to disk
        meta = {
            "end_ms": snapshot.end_ms,
            "start_ms": snapshot.start_ms,
            "clean_time_min": snapshot.clean_time_min,
            "clean_area_m2": snapshot.clean_area_m2,
        }
        try:
            png_path = self._snapshot_path("png")
            json_path = self._snapshot_path("json")
            await self.hass.async_add_executor_job(self._write_snapshot, png_path, image_bytes, json_path, meta)
            _LOGGER.debug("Persisted last cleaning snapshot (%d bytes)", len(image_bytes))
        except Exception:
            _LOGGER.warning("Failed to persist snapshot to disk", exc_info=True)

    @staticmethod
    def _write_snapshot(png_path: str, image_bytes: bytes, json_path: str, meta: dict) -> None:
        """Write snapshot files to disk (runs in executor)."""
        with open(png_path, "wb") as f:
            f.write(image_bytes)
        with open(json_path, "w") as f:
            json.dump(meta, f)
