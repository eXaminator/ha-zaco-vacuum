"""Zaco — unified robot vacuum control library.

Single class that encapsulates all device communication, state management,
and high-level operations. Used identically by Home Assistant and standalone
test scripts.

Usage::

    zaco = await Zaco.from_tokens(
        iot_host="eu-central-1.api-iot.aliyuncs.com",
        iot_token="...", refresh_token="...",
        identity_id="...", iot_id="...",
        verbose=True, log_fn=print,
    )
    try:
        await zaco.spot_clean_room("Büro")
    finally:
        await zaco.close()
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore[assignment]

try:
    from .api_client import (
        AliyunApiClient,
        AliyunApiError,
        AliyunConnectionError,
        AliyunTokenExpiredError,
    )
    from ..const import (
        ALL_PROPERTIES,
        DEFAULT_SCAN_INTERVAL,
        FAST_POLL_INTERVAL,
        FAST_PROPERTIES,
        MQTT_IDLE_POLL_INTERVAL,
        WORKMODE_CLEANING,
        WORKMODE_PAUSED,
        WORKMODE_RETURNING,
    )
    from .map_renderer import _decode_road_data
    from .mqtt_client import ZacoMqttClient
    from .zone_utils import encode_clean_area, rect_to_corners
    from ._helpers import parse_int_prop
    from .map_state import MapState
    from .navigation import NavigationController
    from .path_tracker import PathTracker
except ImportError:
    import os as _os, sys as _sys
    _pkg_dir = _os.path.dirname(_os.path.abspath(__file__))
    if _pkg_dir not in _sys.path:
        _sys.path.insert(0, _pkg_dir)

    from api_client import (  # type: ignore[no-redef]
        AliyunApiClient,
        AliyunApiError,
        AliyunConnectionError,
        AliyunTokenExpiredError,
    )
    from const import (  # type: ignore[no-redef]
        ALL_PROPERTIES,
        DEFAULT_SCAN_INTERVAL,
        FAST_POLL_INTERVAL,
        FAST_PROPERTIES,
        MQTT_IDLE_POLL_INTERVAL,
        WORKMODE_CLEANING,
        WORKMODE_PAUSED,
        WORKMODE_RETURNING,
    )
    from map_renderer import _decode_road_data  # type: ignore[no-redef]
    from mqtt_client import ZacoMqttClient  # type: ignore[no-redef]
    from zone_utils import encode_clean_area, rect_to_corners  # type: ignore[no-redef]
    from _helpers import parse_int_prop  # type: ignore[no-redef]
    from map_state import MapState  # type: ignore[no-redef]
    from navigation import NavigationController  # type: ignore[no-redef]
    from path_tracker import PathTracker  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)


class Zaco:
    """Unified ZACO robot vacuum controller.

    Encapsulates authentication, data polling, state parsing, and all
    device commands.  Used identically by Home Assistant and standalone
    test scripts.

    Composes:
    - MapState: room/grid/outline parsing
    - PathTracker: cleaning path accumulation
    - NavigationController: goto/spot/edge state machines
    """

    # -- Construction (private) -----------------------------------------------

    def __init__(self) -> None:
        # Injected by factory methods
        self._client: AliyunApiClient | None = None
        self._session: aiohttp.ClientSession | None = None
        self._owns_session: bool = False
        self._iot_id: str = ""
        self._device_info: dict[str, Any] = {}

        # Data state
        self._data: dict[str, Any] | None = None

        # Composed helpers
        self._map = MapState()
        self._path = PathTracker(
            get_client=lambda: self._client,
            get_iot_id=lambda: self._iot_id,
        )
        self._nav = NavigationController(
            set_props=self._set_props,
            refresh=lambda: self.refresh(fast=True),
            get_data=lambda: self._data,
            log=self._log,
        )

        # Activity tracking
        self._was_active: bool = False
        self._last_full_poll: float = 0.0
        self._consecutive_errors: int = 0

        # Background polling
        self._poll_task: asyncio.Task | None = None
        self._poll_interval: float = DEFAULT_SCAN_INTERVAL

        # MQTT
        self._mqtt_client: ZacoMqttClient | None = None
        self._mqtt_connected: bool = False

        # Data-updated callback (set by HA coordinator)
        self._on_data_updated: Any = None

        # Logging
        self._verbose: bool = False
        self._log_fn: Any = None

    # -- Factory methods ------------------------------------------------------

    @classmethod
    async def connect(
        cls,
        email: str,
        password: str,
        *,
        iot_id: str | None = None,
        verbose: bool = False,
        log_fn: Any = None,
    ) -> "Zaco":
        """Create a Zaco instance via full email/password login."""
        import aiohttp as _aiohttp

        zaco = cls()
        zaco._verbose = verbose
        zaco._log_fn = log_fn

        session = _aiohttp.ClientSession()
        zaco._session = session
        zaco._owns_session = True

        client = AliyunApiClient(session)
        zaco._client = client

        await client.lookup_region(email)
        sid = await client.oa_login(email, password)
        await client.create_session(sid)

        devices = await client.list_devices()
        if iot_id:
            zaco._iot_id = iot_id
            zaco._device_info = next(
                (d for d in devices if d.get("iotId") == iot_id), {}
            )
        elif devices:
            zaco._iot_id = devices[0].get("iotId", "")
            zaco._device_info = devices[0]
        else:
            raise AliyunApiError("No devices found on this account")

        await zaco.refresh()
        zaco._start_poll_loop()
        return zaco

    @classmethod
    async def from_tokens(
        cls,
        *,
        iot_host: str,
        iot_token: str,
        refresh_token: str,
        identity_id: str,
        iot_id: str,
        iot_token_expiry: float = 0,
        refresh_token_expiry: float = 0,
        oa_host: str | None = None,
        device_info: dict[str, Any] | None = None,
        session: Any = None,
        verbose: bool = False,
        log_fn: Any = None,
    ) -> "Zaco":
        """Create a Zaco instance from saved authentication tokens."""
        zaco = cls()
        zaco._verbose = verbose
        zaco._log_fn = log_fn
        zaco._iot_id = iot_id
        zaco._device_info = device_info or {}

        if session is None:
            import aiohttp as _aiohttp
            session = _aiohttp.ClientSession()
            zaco._owns_session = True
        else:
            zaco._owns_session = False

        zaco._session = session

        client = await AliyunApiClient.from_saved_tokens(
            session,
            iot_host=iot_host,
            iot_token=iot_token,
            refresh_token=refresh_token,
            identity_id=identity_id,
            iot_token_expiry=iot_token_expiry,
            refresh_token_expiry=refresh_token_expiry,
        )
        if oa_host:
            client.oa_host = oa_host
        zaco._client = client

        if not zaco._device_info:
            devices = await client.list_devices()
            zaco._device_info = next(
                (d for d in devices if d.get("iotId") == iot_id), {}
            )

        await zaco.refresh()
        zaco._start_poll_loop()
        return zaco

    # -- Public properties ----------------------------------------------------

    @property
    def data(self) -> dict[str, Any] | None:
        return self._data

    @property
    def rooms(self) -> dict[str, int]:
        return dict(self._map.room_map)

    @property
    def current_room(self) -> str | None:
        return self._map.current_room

    @property
    def active_map_slot(self) -> int | None:
        return self._map.active_map_slot

    @property
    def room_outlines(self) -> dict[int, list[tuple[int, int]]]:
        return self._map.room_outlines

    @property
    def room_centers(self) -> dict[int, tuple[int, int]]:
        return self._map.room_centers

    @property
    def room_walls(self) -> dict[int, list[tuple[int, int]]]:
        return self._map.room_walls

    @property
    def accumulated_path(self) -> list[tuple[int, int]]:
        return list(self._path.accumulated_path)

    @property
    def grid_lookup(self) -> dict[tuple[int, int], int]:
        return self._map.grid_lookup

    @property
    def partition_to_bitmask(self) -> dict[int, int]:
        return self._map.partition_to_bitmask

    @property
    def iot_id(self) -> str:
        return self._iot_id

    @property
    def client(self) -> AliyunApiClient:
        """Exposed for HA token persistence only."""
        assert self._client is not None
        return self._client

    @property
    def device_info(self) -> dict[str, Any]:
        return self._device_info

    @property
    def is_active(self) -> bool:
        """True if robot is currently cleaning, paused, or returning."""
        if self._data is None:
            return False
        wm = parse_int_prop(self._data, "WorkMode")
        if wm is None:
            return False
        active_modes = WORKMODE_CLEANING | WORKMODE_PAUSED | WORKMODE_RETURNING
        return wm in active_modes

    @property
    def mqtt_connected(self) -> bool:
        return self._mqtt_connected

    @property
    def on_data_updated(self) -> Any:
        return self._on_data_updated

    @on_data_updated.setter
    def on_data_updated(self, callback: Any) -> None:
        self._on_data_updated = callback

    # -- Data refresh ---------------------------------------------------------

    async def refresh(self, fast: bool = False) -> dict[str, Any]:
        """Fetch device properties via REST API and update internal state."""
        assert self._client is not None

        try:
            await self._client.ensure_token_valid()
        except AliyunTokenExpiredError:
            raise

        now = time.monotonic()
        needs_full = (
            not fast
            or not self._was_active
            or now - self._last_full_poll >= DEFAULT_SCAN_INTERVAL
            or self._data is None
        )

        try:
            if needs_full:
                data = await self._client.get_properties(
                    self._iot_id, ALL_PROPERTIES
                )
                self._last_full_poll = now
            else:
                fast_data = await self._client.get_properties(
                    self._iot_id, FAST_PROPERTIES
                )
                data = dict(self._data) if self._data else {}
                data.update(fast_data)
        except AliyunConnectionError as err:
            self._consecutive_errors += 1
            if self._consecutive_errors >= 3:
                _LOGGER.error("Connection error (%d consecutive): %s",
                              self._consecutive_errors, err)
            if self._data is not None:
                return self._data
            raise

        self._consecutive_errors = 0

        if data is None:
            if self._data is not None:
                return self._data
            raise AliyunApiError("Failed to get device properties")

        if needs_full:
            self._map.parse_rooms(data)
            self._map.build_grid_lookup(data)

        self._map.extract_realtime_stats(data)

        wm = parse_int_prop(data, "WorkMode")
        active_modes = WORKMODE_CLEANING | WORKMODE_PAUSED | WORKMODE_RETURNING
        is_active = wm is not None and wm in active_modes
        is_cleaning = wm is not None and wm in WORKMODE_CLEANING

        self._map.compute_current_room(data, is_cleaning)

        if is_active:
            self._poll_interval = FAST_POLL_INTERVAL
        elif self._mqtt_connected:
            self._poll_interval = MQTT_IDLE_POLL_INTERVAL
        else:
            self._poll_interval = DEFAULT_SCAN_INTERVAL

        if is_active:
            await self._path.accumulate(data)
        data["_accumulated_path"] = list(self._path.accumulated_path)

        if self._was_active and not is_active:
            self._path.reset()
            data["_accumulated_path"] = []
        self._was_active = is_active

        self._data = data

        if self._on_data_updated is not None:
            self._on_data_updated(data)

        return data

    def merge_mqtt_push(self, items: dict[str, Any]) -> None:
        """Merge MQTT property push into current data and notify listeners."""
        if self._data is None:
            return

        merged = dict(self._data)
        merged.update(items)

        self._map.extract_realtime_stats(merged)

        if "RealMapRoadData" in items:
            self._path.append_from_mqtt(items)
            merged["_accumulated_path"] = list(self._path.accumulated_path)

        wm = parse_int_prop(merged, "WorkMode")
        is_cleaning = wm is not None and wm in WORKMODE_CLEANING
        self._map.compute_current_room(merged, is_cleaning)

        active_modes = WORKMODE_CLEANING | WORKMODE_PAUSED | WORKMODE_RETURNING
        is_active = wm is not None and wm in active_modes

        if is_active:
            self._poll_interval = FAST_POLL_INTERVAL
        elif self._mqtt_connected:
            self._poll_interval = MQTT_IDLE_POLL_INTERVAL
        else:
            self._poll_interval = DEFAULT_SCAN_INTERVAL

        if self._was_active and not is_active:
            self._path.reset()
            merged["_accumulated_path"] = []
        self._was_active = is_active

        self._data = merged

        if self._on_data_updated is not None:
            self._on_data_updated(merged)

    # -- Simple commands ------------------------------------------------------

    async def start(self) -> bool:
        """Start a full auto-clean with the saved map (WorkMode 6)."""
        return await self._set_props({"WorkMode": 6})

    async def stop(self) -> bool:
        """Stop cleaning / standby (WorkMode 2)."""
        return await self._set_props({"WorkMode": 2})

    async def pause(self) -> bool:
        """Pause current cleaning (WorkMode 12)."""
        return await self._set_props({"WorkMode": 12})

    async def resume(self) -> bool:
        """Resume cleaning from paused state (WorkMode 21)."""
        return await self._set_props({"WorkMode": 21})

    async def return_to_base(self) -> bool:
        """Return to charging dock (WorkMode 8)."""
        return await self._set_props({"WorkMode": 8})

    async def locate(self) -> bool:
        """Make the robot beep (SoundLocate)."""
        return await self._set_props({"SoundLocate": {"SoundDir": 0}})

    async def set_fan_power(self, power: int) -> bool:
        """Set fan/suction power (0-100)."""
        return await self._set_props({"FanPower": power})

    async def set_properties(self, items: dict[str, Any]) -> bool:
        """Set arbitrary device properties."""
        return await self._set_props(items)

    # -- Cleaning commands ----------------------------------------------------

    async def clean_rooms(
        self,
        rooms: list[str | int],
        passes: int = 1,
    ) -> bool:
        """Clean specific rooms by name or bitmask ID."""
        room_ids: list[int] = []
        known_ids = set(self._map.room_map.values())
        for entry in rooms:
            try:
                numeric_id = int(entry)
                if numeric_id in known_ids:
                    room_ids.append(numeric_id)
                    continue
            except (ValueError, TypeError):
                pass
            room_id = self.get_room_id(str(entry))
            if room_id is None:
                available = ", ".join(
                    f"{name} ({rid})" for name, rid in self._map.room_map.items()
                )
                raise ValueError(f"Room '{entry}' not found. Available: {available}")
            room_ids.append(room_id)

        return await self._set_props({
            "CleanPartitionData": {
                "PartitionData": sum(room_ids),
                "CleanLoop": min(max(passes, 1), 3),
                "Enable": 1,
            }
        })

    async def clean_zone(
        self,
        x1: int, y1: int, x2: int, y2: int,
        passes: int = 1,
    ) -> bool:
        """Clean a rectangular zone defined by opposite corners."""
        corners = rect_to_corners(x1, y1, x2, y2)
        area_data = encode_clean_area(*corners)
        return await self._set_props({
            "CleanAreaData": {
                "AreaData": area_data,
                "CleanLoop": min(max(passes, 1), 3),
                "Enable": 1,
            }
        })

    # -- Stateful flows -------------------------------------------------------

    async def goto(self, x: int, y: int) -> None:
        """Navigate to a point on the map and pause on arrival."""
        await self._nav.goto_zone(target_x=x, target_y=y)

    async def goto_room(self, room_name: str) -> None:
        """Navigate to a room's center point and pause."""
        center = self.get_room_center(room_name)
        if center is None:
            available = ", ".join(self._map.room_map.keys())
            raise ValueError(f"Room '{room_name}' not found. Available: {available}")
        await self.goto(center[0], center[1])

    async def spot_clean(self, x: int, y: int, repeats: int = 1) -> None:
        """Navigate to a point, spot clean there, then return to dock."""
        await self._nav.goto_zone(
            target_x=x, target_y=y,
            on_arrival=lambda: self._nav.spot_clean_after_arrival(repeats=repeats),
        )

    async def spot_clean_room(self, room_name: str, repeats: int = 1) -> None:
        """Navigate to a room's center, spot clean there, then return to dock."""
        center = self.get_room_center(room_name)
        if center is None:
            available = ", ".join(self._map.room_map.keys())
            raise ValueError(f"Room '{room_name}' not found. Available: {available}")
        await self.spot_clean(center[0], center[1], repeats=repeats)

    async def spot_clean_in_place(self) -> bool:
        """Start spot cleaning at the robot's current position (WorkMode 5)."""
        return await self._set_props({"WorkMode": 5})

    async def edge_clean(
        self,
        room: str | None = None,
        x: int | None = None,
        y: int | None = None,
        passes: int = 1,
    ) -> None:
        """Start edge/wall-follow cleaning.

        If x/y are given, navigates to that point first.  If room is
        given, navigates to the room center first.  Otherwise starts
        edge cleaning at the current position.
        """
        if x is not None and y is not None:
            target = (x, y)
        elif room is not None:
            target = self.get_room_center(room)
            if target is None:
                available = ", ".join(self._map.room_map.keys())
                raise ValueError(f"Room '{room}' not found. Available: {available}")
        else:
            await self._set_props({"WorkMode": 4})
            return

        await self._nav.goto_zone(
            target_x=target[0],
            target_y=target[1],
            on_arrival=lambda: self._nav.edge_clean_after_arrival(
                repeats=passes,
                room_center=target,
            ),
        )

    # -- Settings -------------------------------------------------------------

    def get_clean_settings_bytes(self) -> bytearray | None:
        """Decode CleanSettings.DefaultSetting into a 10-byte array."""
        if self._data is None:
            return None
        raw = self._data.get("CleanSettings", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                return None
        if not isinstance(val, dict):
            return None
        default_b64 = val.get("DefaultSetting", "")
        if not default_b64:
            return None
        try:
            data = bytearray(base64.b64decode(default_b64))
        except Exception:
            return None
        while len(data) < 10:
            data.append(0)
        return data

    async def set_clean_setting(self, byte_index: int, value: int) -> None:
        """Modify a single byte in CleanSettings.DefaultSetting and write back."""
        settings = self.get_clean_settings_bytes()
        if settings is None:
            return
        settings[byte_index] = value & 0xFF
        new_b64 = base64.b64encode(bytes(settings)).decode("ascii")

        raw = self._data.get("CleanSettings", {}) if self._data else {}
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                val = {}
        if not isinstance(val, dict):
            val = {}

        val["DefaultSetting"] = new_b64
        await self._set_props({"CleanSettings": val})
        await self.refresh()

    # -- Room queries (delegate to MapState) ----------------------------------

    def get_room_id(self, name: str) -> int | None:
        """Resolve a room name to its bitmask ID (case-insensitive)."""
        return self._map.get_room_id(name)

    def get_room_center(self, name: str) -> tuple[int, int] | None:
        """Resolve a room name to a navigable point inside the room."""
        return self._map.get_room_center(name)

    # -- MQTT -----------------------------------------------------------------

    async def start_mqtt(self) -> bool:
        """Start MQTT real-time push. Returns True if connected."""
        assert self._client is not None
        try:
            creds = await self._client.get_mqtt_credentials()
        except (AliyunApiError, Exception):
            _LOGGER.warning("Failed to get MQTT credentials", exc_info=True)
            return False

        self._mqtt_client = ZacoMqttClient(
            on_properties=self.merge_mqtt_push,
        )
        try:
            await self._mqtt_client.start(creds, self._client.iot_token)
        except Exception:
            _LOGGER.warning("MQTT connection failed", exc_info=True)
            self._mqtt_client = None
            return False

        self._mqtt_connected = True
        _LOGGER.info("MQTT real-time push active")
        return True

    async def stop_mqtt(self) -> None:
        """Stop MQTT client."""
        if self._mqtt_client:
            await self._mqtt_client.stop()
            self._mqtt_client = None
        self._mqtt_connected = False

    def update_mqtt_token(self, new_token: str) -> None:
        """Update MQTT iotToken after token refresh."""
        if self._mqtt_client and self._mqtt_connected:
            self._mqtt_client.update_iot_token(new_token)

    # -- Lifecycle ------------------------------------------------------------

    async def close(self) -> None:
        """Stop polling, MQTT, and optionally close HTTP session."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        await self.stop_mqtt()

        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    # -- Internal: polling loop -----------------------------------------------

    def _start_poll_loop(self) -> None:
        """Start the background polling task."""
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        """Background loop that calls refresh() at adaptive intervals."""
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self.refresh(fast=self._was_active)
            except asyncio.CancelledError:
                raise
            except AliyunTokenExpiredError:
                _LOGGER.error("Token expired during background poll")
                break
            except Exception:
                _LOGGER.debug("Background poll failed", exc_info=True)

    # -- Internal: set properties helper --------------------------------------

    async def _set_props(self, items: dict[str, Any]) -> bool:
        """Set device properties via REST API."""
        assert self._client is not None
        await self._client.ensure_token_valid()
        return await self._client.set_properties(self._iot_id, items)

    # -- Internal: logging ----------------------------------------------------

    def _log(self, msg: str) -> None:
        """Log a message to both the logger and optional log_fn."""
        _LOGGER.debug(msg)
        if self._log_fn is not None:
            self._log_fn(msg)
