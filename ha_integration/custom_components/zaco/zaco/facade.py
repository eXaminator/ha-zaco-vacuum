"""Zaco — unified robot vacuum control library.

Single class that encapsulates all device communication, state management,
and high-level operations. Used identically by Home Assistant and standalone
test scripts.

Polling is NOT handled by this class — callers are responsible for calling
refresh() at the appropriate intervals. In HA this is DataUpdateCoordinator;
in test scripts it's an explicit poll loop.

Usage::

    zaco = await Zaco.from_tokens(
        iot_host="eu-central-1.api-iot.aliyuncs.com",
        iot_token="...", refresh_token="...",
        identity_id="...", iot_id="...",
    )
    try:
        data = await zaco.refresh()          # initial data load
        await zaco.spot_clean_room("Büro")
    finally:
        await zaco.close()
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
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
        FAST_PROPERTIES,
        STATE_PROPERTIES,
        WORKMODE_CLEANING,
        WORKMODE_IDLE,
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
        FAST_PROPERTIES,
        STATE_PROPERTIES,
        WORKMODE_CLEANING,
        WORKMODE_IDLE,
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

    Encapsulates authentication, state parsing, and all device commands.
    Does NOT poll — callers are responsible for calling refresh().

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

        # MQTT
        self._mqtt_client: ZacoMqttClient | None = None
        self._mqtt_connected: bool = False

        # Data-updated callback (used by MQTT push to notify coordinator)
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
        """Create a Zaco instance via full email/password login.

        Does NOT call refresh() or start polling — caller must do that.
        """
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
        """Create a Zaco instance from saved authentication tokens.

        Does NOT call refresh() or start polling — caller must do that.
        """
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
    def path_length(self) -> int:
        """Length of accumulated path without copying the list."""
        return len(self._path.accumulated_path)

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

    async def refresh(
        self, fast: bool = False, include_maps: bool = False,
    ) -> dict[str, Any]:
        """Fetch device properties via REST API and update internal state.

        Args:
            fast: If True, fetch only FAST_PROPERTIES and merge with
                  existing data. If False, fetch state properties.
            include_maps: If True (and not fast), also fetch heavy SLAM
                  map properties (SaveMapDataX9 etc.).

        Returns:
            The updated data dict.

        Raises:
            AliyunTokenExpiredError: All tokens expired.
            AliyunConnectionError: Network error.
            AliyunApiError: API returned no data.

        Note: This method does NOT call on_data_updated. Callers that
        need to notify listeners (e.g. HA coordinator) should do so
        themselves after calling refresh().
        """
        assert self._client is not None

        _LOGGER.debug(
            "Zaco.refresh: fast=%s, include_maps=%s", fast, include_maps,
        )

        await self._client.ensure_token_valid()

        if fast and self._data is not None:
            fast_data = await self._client.get_properties(
                self._iot_id, FAST_PROPERTIES
            )
            if fast_data is None:
                raise AliyunApiError("Failed to get fast properties")
            self._data.update(fast_data)
            data = self._data
        else:
            props = ALL_PROPERTIES if include_maps else STATE_PROPERTIES
            data = await self._client.get_properties(self._iot_id, props)
            if data is None:
                raise AliyunApiError("Failed to get device properties")
            # Preserve map data from previous full poll when not fetching maps
            if not include_maps and self._data is not None:
                for key in ("SaveMapDataX9_1", "SaveMapDataX9_2",
                            "SaveMapDataX9_3", "SaveMapDataInfoX9_1",
                            "SaveMapDataInfoX9_2", "SaveMapDataInfoX9_3"):
                    if key in self._data and key not in data:
                        data[key] = self._data[key]
            self._map.parse_rooms(data)
            if include_maps:
                self._map.build_grid_lookup(data)

        self._map.extract_realtime_stats(data)

        wm = parse_int_prop(data, "WorkMode")
        ps = parse_int_prop(data, "PowerSwitch")
        is_active = wm is not None and wm in (WORKMODE_CLEANING | WORKMODE_PAUSED | WORKMODE_RETURNING)
        is_cleaning = wm is not None and wm in WORKMODE_CLEANING
        is_docked = ps == 0 and wm is not None and wm in WORKMODE_IDLE

        self._map.compute_current_room(data, is_cleaning=is_cleaning, is_docked=is_docked)

        was_active = self._data is not None and self.is_active

        if is_active:
            await self._path.accumulate(data)

        if was_active and not is_active:
            _LOGGER.debug("Zaco.refresh: active->idle transition, resetting path")
            self._path.reset()

        self._data = data

        _LOGGER.debug(
            "Zaco.refresh: done, WM=%s, is_active=%s, props=%d",
            wm, is_active, len(data),
        )
        return data

    def merge_mqtt_push(self, items: dict[str, Any]) -> None:
        """Merge MQTT property push into current data and notify listeners."""
        if self._data is None:
            _LOGGER.debug("Zaco.merge_mqtt_push: no data yet, ignoring")
            return

        _LOGGER.debug("Zaco.merge_mqtt_push: keys=%s", list(items.keys()))

        merged = dict(self._data)
        merged.update(items)

        self._map.extract_realtime_stats(merged)

        if "RealMapRoadData" in items:
            self._path.append_from_mqtt(items)

        wm = parse_int_prop(merged, "WorkMode")
        ps = parse_int_prop(merged, "PowerSwitch")
        is_cleaning = wm is not None and wm in WORKMODE_CLEANING
        is_docked = ps == 0 and wm is not None and wm in WORKMODE_IDLE
        self._map.compute_current_room(merged, is_cleaning=is_cleaning, is_docked=is_docked)

        is_active = wm is not None and wm in (WORKMODE_CLEANING | WORKMODE_PAUSED | WORKMODE_RETURNING)
        was_active = self.is_active

        if was_active and not is_active:
            self._path.reset()

        self._data = merged

        if self._on_data_updated is not None:
            self._on_data_updated(merged)

    # -- Simple commands ------------------------------------------------------

    async def start(self) -> bool:
        """Start a full auto-clean with the saved map (WorkMode 6)."""
        _LOGGER.debug("Zaco: start (WorkMode 6)")
        return await self._set_props({"WorkMode": 6})

    async def stop(self) -> bool:
        """Stop cleaning / standby (WorkMode 2)."""
        _LOGGER.debug("Zaco: stop (WorkMode 2)")
        return await self._set_props({"WorkMode": 2})

    async def pause(self) -> bool:
        """Pause current cleaning (WorkMode 12)."""
        _LOGGER.debug("Zaco: pause (WorkMode 12)")
        return await self._set_props({"WorkMode": 12})

    async def resume(self) -> bool:
        """Resume cleaning from paused state (WorkMode 21)."""
        _LOGGER.debug("Zaco: resume (WorkMode 21)")
        return await self._set_props({"WorkMode": 21})

    async def return_to_base(self) -> bool:
        """Return to charging dock (WorkMode 8)."""
        _LOGGER.debug("Zaco: return_to_base (WorkMode 8)")
        return await self._set_props({"WorkMode": 8})

    async def locate(self) -> bool:
        """Make the robot beep (SoundLocate)."""
        _LOGGER.debug("Zaco: locate (SoundLocate)")
        return await self._set_props({"SoundLocate": {"SoundDir": 0}})

    async def set_fan_power(self, power: int) -> bool:
        """Set fan/suction power (0-100)."""
        _LOGGER.debug("Zaco: set_fan_power(%d)", power)
        # Keep CleanSettings byte[1] in sync so set_clean_setting won't clobber
        settings = self.get_clean_settings_bytes()
        if settings is not None:
            settings[1] = power & 0xFF
            new_b64 = base64.b64encode(bytes(settings)).decode("ascii")
            self._update_clean_settings_cache(new_b64)
        return await self._set_props({"FanPower": power})

    async def set_properties(self, items: dict[str, Any]) -> bool:
        """Set arbitrary device properties."""
        _LOGGER.debug("Zaco: set_properties(%s)", list(items.keys()))
        return await self._set_props(items)

    async def remote_control(self, direction: int) -> bool:
        """Send a directional movement command (CleanDirection 1-5).

        1=forward, 2=back, 3=left, 4=right, 5=stop/pause.
        """
        _LOGGER.debug("Zaco: remote_control(%d)", direction)
        return await self._set_props({"CleanDirection": direction})

    # -- Cleaning commands ----------------------------------------------------

    async def clean_rooms(
        self,
        rooms: list[str | int],
        passes: int = 1,
    ) -> bool:
        """Clean specific rooms by name or bitmask ID."""
        _LOGGER.debug("Zaco: clean_rooms(%s, passes=%d)", rooms, passes)
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
        _LOGGER.debug(
            "Zaco: clean_zone(%d,%d,%d,%d, passes=%d)", x1, y1, x2, y2, passes,
        )
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

    def _get_clean_settings_val(self, new_b64: str) -> dict:
        """Build the CleanSettings value dict with updated DefaultSetting."""
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
        return val

    def _update_clean_settings_cache(self, new_b64: str) -> None:
        """Update the cached CleanSettings.DefaultSetting in self._data."""
        if self._data is None:
            return
        val = self._get_clean_settings_val(new_b64)
        self._data["CleanSettings"] = {"value": json.dumps(val)}

    async def set_clean_setting(self, byte_index: int, value: int) -> None:
        """Modify a single byte in CleanSettings.DefaultSetting and write back."""
        settings = self.get_clean_settings_bytes()
        if settings is None:
            return
        settings[byte_index] = value & 0xFF
        # Sync byte[1] with current FanPower to prevent clobbering
        fan = parse_int_prop(self._data, "FanPower") if self._data else None
        if fan is not None:
            settings[1] = fan & 0xFF
        new_b64 = base64.b64encode(bytes(settings)).decode("ascii")
        self._update_clean_settings_cache(new_b64)
        await self._set_props({"CleanSettings": self._get_clean_settings_val(new_b64)})

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
        _LOGGER.debug("Zaco: starting MQTT")
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
        _LOGGER.debug("Zaco: stopping MQTT")
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
        """Stop MQTT and optionally close HTTP session."""
        _LOGGER.debug("Zaco: closing (owns_session=%s)", self._owns_session)
        await self.stop_mqtt()

        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

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
