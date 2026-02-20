"""ZACO Robot Vacuum integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_client import AliyunApiClient, AliyunTokenExpiredError
from .const import (
    CONF_IDENTITY_ID,
    CONF_IOT_HOST,
    CONF_IOT_ID,
    CONF_IOT_TOKEN,
    CONF_IOT_TOKEN_EXPIRY,
    CONF_OA_HOST,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_EXPIRY,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import ZacoDataUpdateCoordinator
from .zone_utils import encode_clean_area, rect_to_corners

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ZACO from a config entry."""
    session = async_get_clientsession(hass)
    client = AliyunApiClient(session)

    # Restore auth state from config entry
    client.iot_host = entry.data[CONF_IOT_HOST]
    client.oa_host = entry.data[CONF_OA_HOST]
    client.iot_token = entry.data.get(CONF_IOT_TOKEN)
    client.refresh_token = entry.data.get(CONF_REFRESH_TOKEN)
    client.identity_id = entry.data.get(CONF_IDENTITY_ID)
    client.iot_token_expiry = entry.data.get(CONF_IOT_TOKEN_EXPIRY, 0)
    client.refresh_token_expiry = entry.data.get(CONF_REFRESH_TOKEN_EXPIRY, 0)

    # Ensure valid token (may trigger refresh)
    try:
        await client.ensure_token_valid()
    except AliyunTokenExpiredError as err:
        raise ConfigEntryAuthFailed(
            "Authentication expired, please reconfigure"
        ) from err

    # Update stored tokens if they changed during refresh
    _update_entry_tokens(hass, entry, client)

    # Find configured device
    iot_id = entry.data[CONF_IOT_ID]
    devices = await client.list_devices()
    device_info = next((d for d in devices if d.get("iotId") == iot_id), {})

    # Create coordinator
    coordinator = ZacoDataUpdateCoordinator(hass, client, iot_id, device_info)
    await coordinator.async_config_entry_first_refresh()

    # Store for platforms
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "client": client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register custom services
    _register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        # Remove services if no more entries
        if not hass.data[DOMAIN]:
            for service_name in ("start", "spot_clean", "edge_clean", "goto"):
                hass.services.async_remove(DOMAIN, service_name)
    return unload_ok


def _update_entry_tokens(
    hass: HomeAssistant, entry: ConfigEntry, client: AliyunApiClient
) -> None:
    """Persist updated tokens back to the config entry."""
    new_data = dict(entry.data)
    changed = False

    for attr, key in [
        ("iot_token", CONF_IOT_TOKEN),
        ("refresh_token", CONF_REFRESH_TOKEN),
        ("identity_id", CONF_IDENTITY_ID),
        ("iot_token_expiry", CONF_IOT_TOKEN_EXPIRY),
        ("refresh_token_expiry", CONF_REFRESH_TOKEN_EXPIRY),
    ]:
        val = getattr(client, attr)
        if val is not None and val != new_data.get(key):
            new_data[key] = val
            changed = True

    if changed:
        hass.config_entries.async_update_entry(entry, data=new_data)


def _resolve_coordinator(
    hass: HomeAssistant, entity_id: str
) -> tuple[ZacoDataUpdateCoordinator, AliyunApiClient]:
    """Look up the coordinator and client for a given entity ID."""
    registry = er.async_get(hass)
    entity_entry = registry.async_get(entity_id)
    if not entity_entry or not entity_entry.config_entry_id:
        raise HomeAssistantError(f"Entity {entity_id} not found")

    domain_data: dict[str, Any] | None = hass.data.get(DOMAIN, {}).get(
        entity_entry.config_entry_id
    )
    if not domain_data:
        raise HomeAssistantError("ZACO integration not set up")

    return domain_data["coordinator"], domain_data["client"]


def _parse_int_prop(props: dict, key: str) -> int | None:
    """Extract an integer property value from the get_properties result."""
    raw = props.get(key, {})
    val = raw.get("value", raw) if isinstance(raw, dict) else raw
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


async def _wait_and_edge_clean(
    coordinator: ZacoDataUpdateCoordinator,
    client: AliyunApiClient,
    timeout: int = 180,
    poll_interval: int = 3,
) -> None:
    """Poll WorkMode; switch to edge clean when robot arrives at zone.

    Runs as a background task fired by handle_edge_clean when a room target
    is provided. A small zone is sent at the room center — when WorkMode
    is 19 AND PowerSwitch is 1 (robot arrived and actively cleaning), we
    switch to WorkMode 4 (edge clean).
    """
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(poll_interval)

        props = await client.get_properties(
            coordinator.iot_id, ["WorkMode", "PowerSwitch"]
        )
        if props is None:
            continue

        work_mode = _parse_int_prop(props, "WorkMode")
        power = _parse_int_prop(props, "PowerSwitch")
        if work_mode is None:
            continue

        if work_mode == 19 and power == 1:
            # Robot arrived at zone and is cleaning → switch to edge clean
            await client.set_properties(coordinator.iot_id, {"WorkMode": 4})
            await coordinator.async_request_refresh()
            _LOGGER.info("Robot arrived at room zone, switched to edge clean")
            return

        if work_mode in (9, 11, 16, 17):
            _LOGGER.warning(
                "Robot idle (WorkMode %s) before edge clean switch", work_mode
            )
            return

    _LOGGER.warning(
        "Timed out after %ss waiting for zone arrival", timeout
    )


async def _wait_and_spot_clean(
    coordinator: ZacoDataUpdateCoordinator,
    client: AliyunApiClient,
    timeout: int = 600,
    poll_interval: int = 3,
) -> None:
    """Navigate via zone clean, switch to spot clean, then return to dock.

    Two-phase background task fired by handle_spot_clean when x/y coords
    are provided:

    Phase 1 (navigating): Wait for WorkMode 19 AND PowerSwitch 1 (robot
    arrived and actively cleaning), then pause (WM 2) and switch to spot
    clean (WM 5).

    Phase 2 (spot_cleaning): Wait for WorkMode to leave 5 (spot clean
    finished), then send WM 8 (return to dock). WorkMode 5 does not
    auto-return, so we must trigger it explicitly.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    phase = "navigating"

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(poll_interval)

        props = await client.get_properties(
            coordinator.iot_id, ["WorkMode", "PowerSwitch"]
        )
        if props is None:
            continue

        work_mode = _parse_int_prop(props, "WorkMode")
        power = _parse_int_prop(props, "PowerSwitch")
        if work_mode is None:
            continue

        if phase == "navigating":
            if work_mode == 19 and power == 1:
                # Robot arrived at zone and is cleaning → pause, then spot clean.
                # Direct WM 5 during zone clean is silently ignored.
                await client.set_properties(
                    coordinator.iot_id, {"WorkMode": 2}
                )
                await asyncio.sleep(1)
                await client.set_properties(
                    coordinator.iot_id, {"WorkMode": 5}
                )
                await coordinator.async_request_refresh()
                _LOGGER.info("Arrived at target, switched to spot clean")
                phase = "spot_cleaning"
                continue

            if work_mode in (9, 11, 16, 17):
                _LOGGER.warning(
                    "Robot idle (WorkMode %s) before spot clean", work_mode
                )
                return

        elif phase == "spot_cleaning":
            if work_mode == 5:
                continue  # still spot cleaning

            # Spot clean finished — send return to dock
            if work_mode not in (8, 9, 11, 16, 17):
                await client.set_properties(
                    coordinator.iot_id, {"WorkMode": 8}
                )
                _LOGGER.info("Spot clean done, sent return to dock")
            else:
                _LOGGER.info(
                    "Spot clean done, robot already WM %s", work_mode
                )
            await coordinator.async_request_refresh()
            return

    _LOGGER.warning("Timed out after %ss in spot_clean task", timeout)


async def _wait_and_pause(
    coordinator: ZacoDataUpdateCoordinator,
    client: AliyunApiClient,
    timeout: int = 180,
    poll_interval: int = 3,
) -> None:
    """Poll WorkMode; pause the robot when it arrives at the zone.

    Runs as a background task fired by handle_goto. A small zone is sent
    at the target point — when WorkMode is 19 AND PowerSwitch is 1 (robot
    arrived and actively cleaning), we switch to WorkMode 2 (standby).
    """
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(poll_interval)

        props = await client.get_properties(
            coordinator.iot_id, ["WorkMode", "PowerSwitch"]
        )
        if props is None:
            continue

        work_mode = _parse_int_prop(props, "WorkMode")
        power = _parse_int_prop(props, "PowerSwitch")
        if work_mode is None:
            continue

        if work_mode == 19 and power == 1:
            # Robot arrived at zone and is cleaning → pause
            await client.set_properties(coordinator.iot_id, {"WorkMode": 2})
            await coordinator.async_request_refresh()
            _LOGGER.info("Robot arrived at target zone, paused")
            return

        if work_mode in (9, 11, 16, 17):
            _LOGGER.warning(
                "Robot idle (WorkMode %s) before goto pause", work_mode
            )
            return

    _LOGGER.warning(
        "Timed out after %ss waiting for zone arrival", timeout
    )


def _register_services(hass: HomeAssistant) -> None:
    """Register custom ZACO services."""
    if hass.services.has_service(DOMAIN, "start"):
        return

    # --- zaco.start: unified cleaning service ---
    async def handle_start(call: ServiceCall) -> None:
        """Handle the start service call.

        Supports three modes (zone takes priority over rooms):
          - zone: Clean a rectangular area defined by [x1, y1, x2, y2]
          - rooms: Clean named rooms (resolved to bitmask IDs)
          - neither: Full auto-clean with saved map
        """
        entity_id = call.data["entity_id"]
        rooms: list[str] | None = call.data.get("rooms")
        zone: list[int | float] | None = call.data.get("zone")
        passes: int = call.data.get("passes", 1)

        coordinator, client = _resolve_coordinator(hass, entity_id)
        await client.ensure_token_valid()

        if zone:
            # Zone cleaning takes priority
            x1, y1, x2, y2 = zone[:4]
            corners = rect_to_corners(int(x1), int(y1), int(x2), int(y2))
            area_data = encode_clean_area(*corners)
            success = await client.set_properties(
                coordinator.iot_id,
                {
                    "CleanAreaData": {
                        "AreaData": area_data,
                        "CleanLoop": min(max(passes, 1), 3),
                        "Enable": 1,
                    }
                },
            )
            if not success:
                raise HomeAssistantError("Failed to send zone clean command")
        elif rooms:
            # Room cleaning by name or numeric bitmask ID
            room_ids: list[int] = []
            known_ids = set(coordinator.rooms.values())
            for entry in rooms:
                # Try as numeric bitmask ID first
                try:
                    numeric_id = int(entry)
                    if numeric_id in known_ids:
                        room_ids.append(numeric_id)
                        continue
                except (ValueError, TypeError):
                    pass
                # Fall back to name lookup
                room_id = coordinator.get_room_id_by_name(entry)
                if room_id is None:
                    available = ", ".join(
                        f"{name} ({rid})"
                        for name, rid in coordinator.rooms.items()
                    )
                    raise HomeAssistantError(
                        f"Room '{entry}' not found. Available: {available}"
                    )
                room_ids.append(room_id)
            success = await client.set_properties(
                coordinator.iot_id,
                {
                    "CleanPartitionData": {
                        "PartitionData": sum(room_ids),
                        "CleanLoop": min(max(passes, 1), 3),
                        "Enable": 1,
                    }
                },
            )
            if not success:
                raise HomeAssistantError("Failed to send room clean command")
        else:
            # Full auto-clean
            success = await client.set_properties(
                coordinator.iot_id, {"WorkMode": 6}
            )
            if not success:
                raise HomeAssistantError("Failed to send auto-clean command")

        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        "start",
        handle_start,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): str,
                vol.Optional("rooms"): vol.All(vol.Coerce(list), [vol.Any(str, int)]),
                vol.Optional("zone"): vol.All(
                    vol.Coerce(list), [vol.Coerce(float)]
                ),
                vol.Optional("passes", default=1): vol.All(
                    int, vol.Range(min=1, max=3)
                ),
            }
        ),
    )

    # --- spot_clean service ---
    async def handle_spot_clean(call: ServiceCall) -> None:
        """Handle the spot_clean service call.

        Without x/y: spot cleans in place (WorkMode 5).
        With x/y: navigates to the target via a small zone (CleanAreaData),
        then switches to spot clean mode (WorkMode 5) when zone cleaning
        starts. The robot spot cleans at that location and auto-returns.
        """
        entity_id = call.data["entity_id"]
        x = call.data.get("x")
        y = call.data.get("y")

        coordinator, client = _resolve_coordinator(hass, entity_id)
        await client.ensure_token_valid()

        if x is not None and y is not None:
            half = 3  # 6×6 unit zone (~30cm × 30cm)
            cx, cy = int(x), int(y)
            corners = rect_to_corners(cx - half, cy - half, cx + half, cy + half)
            area_data = encode_clean_area(*corners)
            _LOGGER.debug("spot_clean at (%s, %s), zone=%s", x, y, area_data)
            success = await client.set_properties(
                coordinator.iot_id,
                {
                    "CleanAreaData": {
                        "AreaData": area_data,
                        "CleanLoop": 1,
                        "Enable": 1,
                    }
                },
            )
            if not success:
                raise HomeAssistantError("Failed to send spot clean command")
            await coordinator.async_request_refresh()
            # Navigate via zone, then switch to spot clean on arrival
            hass.async_create_task(
                _wait_and_spot_clean(coordinator, client)
            )
            return

        success = await client.set_properties(coordinator.iot_id, {"WorkMode": 5})
        if not success:
            raise HomeAssistantError("Failed to send spot clean command")
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        "spot_clean",
        handle_spot_clean,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): str,
                vol.Optional("x"): vol.Coerce(float),
                vol.Optional("y"): vol.Coerce(float),
            }
        ),
    )

    # --- edge_clean service ---
    async def handle_edge_clean(call: ServiceCall) -> None:
        """Handle the edge_clean service call.

        Without `room`: starts edge cleaning immediately from wherever the
        robot is (existing behaviour).

        With `room`: sends a small zone at the room center, waits for the
        robot to start zone cleaning (WorkMode 19), then switches to edge
        clean mode (WorkMode 4).
        """
        entity_id = call.data["entity_id"]
        room: str | None = call.data.get("room")

        coordinator, client = _resolve_coordinator(hass, entity_id)
        await client.ensure_token_valid()

        if room is not None:
            center = coordinator.get_room_center_by_name(room)
            if center is None:
                available = ", ".join(coordinator.rooms.keys())
                raise HomeAssistantError(
                    f"Room '{room}' not found. Available: {available}"
                )
            x, y = center
            half = 3
            corners = rect_to_corners(x - half, y - half, x + half, y + half)
            area_data = encode_clean_area(*corners)
            success = await client.set_properties(
                coordinator.iot_id,
                {
                    "CleanAreaData": {
                        "AreaData": area_data,
                        "CleanLoop": 1,
                        "Enable": 1,
                    }
                },
            )
            if not success:
                raise HomeAssistantError("Failed to send edge clean command")
            await coordinator.async_request_refresh()

            hass.async_create_task(
                _wait_and_edge_clean(coordinator, client)
            )
        else:
            success = await client.set_properties(coordinator.iot_id, {"WorkMode": 4})
            if not success:
                raise HomeAssistantError("Failed to send edge clean command")
            await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        "edge_clean",
        handle_edge_clean,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): str,
                vol.Optional("room"): str,
            }
        ),
    )

    # --- goto service (navigate to point → pause) ---
    async def handle_goto(call: ServiceCall) -> None:
        """Handle the goto service call.

        Sends the robot to a specific point on the map. A small zone
        (CleanAreaData) is sent at the target — when the robot arrives and
        starts zone cleaning (WorkMode 19), a background task pauses it
        (WorkMode 2) so it stops near the target point.
        """
        entity_id = call.data["entity_id"]
        x = call.data["x"]
        y = call.data["y"]

        coordinator, client = _resolve_coordinator(hass, entity_id)
        await client.ensure_token_valid()

        half = 3
        cx, cy = int(x), int(y)
        corners = rect_to_corners(cx - half, cy - half, cx + half, cy + half)
        area_data = encode_clean_area(*corners)
        _LOGGER.debug("goto zone at (%s, %s), area=%s", x, y, area_data)
        success = await client.set_properties(
            coordinator.iot_id,
            {
                "CleanAreaData": {
                    "AreaData": area_data,
                    "CleanLoop": 1,
                    "Enable": 1,
                }
            },
        )
        if not success:
            raise HomeAssistantError("Failed to send go-to-point command")
        await coordinator.async_request_refresh()

        # Wait for zone cleaning to start in background, then pause
        hass.async_create_task(
            _wait_and_pause(coordinator, client)
        )

    hass.services.async_register(
        DOMAIN,
        "goto",
        handle_goto,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): str,
                vol.Required("x"): vol.Coerce(float),
                vol.Required("y"): vol.Coerce(float),
            }
        ),
    )
