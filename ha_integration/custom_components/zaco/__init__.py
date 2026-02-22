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
    REMOTE_DIRECTIONS,
)
from .coordinator import ZacoDataUpdateCoordinator
from .zaco import Zaco

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ZACO from a config entry."""
    _LOGGER.debug("Setting up ZACO entry %s", entry.entry_id)
    session = async_get_clientsession(hass)

    iot_id = entry.data[CONF_IOT_ID]
    try:
        zaco = await Zaco.from_tokens(
            iot_host=entry.data[CONF_IOT_HOST],
            iot_token=entry.data.get(CONF_IOT_TOKEN, ""),
            refresh_token=entry.data.get(CONF_REFRESH_TOKEN, ""),
            identity_id=entry.data.get(CONF_IDENTITY_ID, ""),
            iot_id=iot_id,
            iot_token_expiry=entry.data.get(CONF_IOT_TOKEN_EXPIRY, 0),
            refresh_token_expiry=entry.data.get(CONF_REFRESH_TOKEN_EXPIRY, 0),
            oa_host=entry.data.get(CONF_OA_HOST),
            device_info={"iotId": iot_id},
            session=session,
        )
    except Exception as err:
        _LOGGER.debug("ZACO auth failed during setup: %s", err)
        raise ConfigEntryAuthFailed(
            "Authentication failed, please reconfigure"
        ) from err

    try:
        # Create coordinator — drives all polling via HA's standard lifecycle
        coordinator = ZacoDataUpdateCoordinator(hass, zaco)
        _LOGGER.debug("Coordinator created, starting first refresh")

        # First data fetch (raises ConfigEntryNotReady on failure)
        await coordinator.async_config_entry_first_refresh()
        _LOGGER.debug("First refresh done")

        # Update stored tokens (may have been refreshed during setup)
        _update_entry_tokens(hass, entry, zaco)

        # Store for platforms
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            "coordinator": coordinator,
            "nav_tasks": [],  # Track navigation tasks for cleanup
        }

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        _LOGGER.debug("Platforms loaded")

        # Register custom services
        _register_services(hass)

        # Start MQTT real-time push (non-blocking)
        hass.async_create_task(zaco.start_mqtt())
        _LOGGER.debug("ZACO setup complete for %s", iot_id)
    except Exception:
        _LOGGER.exception("ZACO setup failed, cleaning up")
        await zaco.close()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading ZACO entry %s", entry.entry_id)
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

    # Cancel any running navigation tasks
    nav_tasks: list[asyncio.Task] = domain_data.get("nav_tasks", [])
    for task in nav_tasks:
        if not task.done():
            _LOGGER.debug("Cancelling navigation task %s", task.get_name())
            task.cancel()
    nav_tasks.clear()

    coordinator: ZacoDataUpdateCoordinator | None = domain_data.get("coordinator")
    if coordinator:
        _LOGGER.debug("Closing Zaco instance")
        await coordinator.zaco.close()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            for service_name in ("start", "spot_clean", "edge_clean", "goto", "remote_control"):
                hass.services.async_remove(DOMAIN, service_name)
    _LOGGER.debug("ZACO unload complete: %s", unload_ok)
    return unload_ok


def _update_entry_tokens(
    hass: HomeAssistant, entry: ConfigEntry, zaco: Zaco,
) -> None:
    """Persist updated tokens back to the config entry."""
    client = zaco.client
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
        _LOGGER.debug("Persisting refreshed tokens to config entry")
        hass.config_entries.async_update_entry(entry, data=new_data)
        zaco.update_mqtt_token(client.iot_token)


def _resolve_zaco(hass: HomeAssistant, entity_id: str) -> Zaco:
    """Look up the Zaco instance for a given entity ID."""
    registry = er.async_get(hass)
    entity_entry = registry.async_get(entity_id)
    if not entity_entry or not entity_entry.config_entry_id:
        raise HomeAssistantError(f"Entity {entity_id} not found")

    domain_data: dict[str, Any] | None = hass.data.get(DOMAIN, {}).get(
        entity_entry.config_entry_id
    )
    if not domain_data:
        raise HomeAssistantError("ZACO integration not set up")

    return domain_data["coordinator"].zaco


def _track_nav_task(
    hass: HomeAssistant, entity_id: str, coro: Any,
) -> None:
    """Create a tracked navigation task that is cancelled on unload."""
    registry = er.async_get(hass)
    entity_entry = registry.async_get(entity_id)
    config_entry_id = entity_entry.config_entry_id if entity_entry else None
    task = hass.async_create_task(coro)

    if config_entry_id:
        domain_data = hass.data.get(DOMAIN, {}).get(config_entry_id, {})
        nav_tasks: list = domain_data.get("nav_tasks", [])
        # Clean up completed tasks
        nav_tasks[:] = [t for t in nav_tasks if not t.done()]
        nav_tasks.append(task)
        _LOGGER.debug(
            "Tracked nav task (active=%d)", len(nav_tasks),
        )


def _register_services(hass: HomeAssistant) -> None:
    """Register custom ZACO services."""
    if hass.services.has_service(DOMAIN, "start"):
        return

    # --- zaco.start ---
    async def handle_start(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        zaco = _resolve_zaco(hass, entity_id)
        rooms: list[str] | None = call.data.get("rooms")
        zone: list[int | float] | None = call.data.get("zone")
        passes: int = call.data.get("passes", 1)

        if zone:
            _LOGGER.debug("Service start: zone=%s, passes=%d", zone, passes)
            x1, y1, x2, y2 = zone[:4]
            await zaco.clean_zone(int(x1), int(y1), int(x2), int(y2), passes=passes)
        elif rooms:
            _LOGGER.debug("Service start: rooms=%s, passes=%d", rooms, passes)
            await zaco.clean_rooms(rooms, passes=passes)
        else:
            _LOGGER.debug("Service start: full auto-clean")
            await zaco.start()

    hass.services.async_register(
        DOMAIN,
        "start",
        handle_start,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): str,
                vol.Optional("rooms"): vol.All(vol.Coerce(list), [vol.Any(str, int)]),
                vol.Optional("zone"): vol.All(vol.Coerce(list), [vol.Coerce(float)]),
                vol.Optional("passes", default=1): vol.All(int, vol.Range(min=1, max=3)),
            }
        ),
    )

    # --- zaco.spot_clean ---
    async def handle_spot_clean(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        zaco = _resolve_zaco(hass, entity_id)
        x = call.data.get("x")
        y = call.data.get("y")
        repeats: int = call.data.get("repeats", 1)

        if x is not None and y is not None:
            _LOGGER.debug("Service spot_clean: x=%s, y=%s, repeats=%d", x, y, repeats)
            _track_nav_task(hass, entity_id, zaco.spot_clean(int(x), int(y), repeats=repeats))
        else:
            _LOGGER.debug("Service spot_clean: in-place")
            await zaco.spot_clean_in_place()

    hass.services.async_register(
        DOMAIN,
        "spot_clean",
        handle_spot_clean,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): str,
                vol.Optional("x"): vol.Coerce(float),
                vol.Optional("y"): vol.Coerce(float),
                vol.Optional("repeats", default=1): vol.All(int, vol.Range(min=1, max=5)),
            }
        ),
    )

    # --- zaco.edge_clean ---
    async def handle_edge_clean(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        zaco = _resolve_zaco(hass, entity_id)
        room: str | None = call.data.get("room")
        x = call.data.get("x")
        y = call.data.get("y")
        passes: int = call.data.get("passes", 1)

        if x is not None and y is not None:
            _LOGGER.debug("Service edge_clean: x=%s, y=%s, passes=%d", x, y, passes)
            _track_nav_task(hass, entity_id, zaco.edge_clean(x=int(x), y=int(y), passes=passes))
        elif room is not None:
            _LOGGER.debug("Service edge_clean: room=%s, passes=%d", room, passes)
            _track_nav_task(hass, entity_id, zaco.edge_clean(room=room, passes=passes))
        else:
            _LOGGER.debug("Service edge_clean: in-place, passes=%d", passes)
            await zaco.edge_clean(passes=passes)

    hass.services.async_register(
        DOMAIN,
        "edge_clean",
        handle_edge_clean,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): str,
                vol.Optional("room"): str,
                vol.Optional("x"): vol.Coerce(float),
                vol.Optional("y"): vol.Coerce(float),
                vol.Optional("passes", default=1): vol.All(
                    int, vol.Range(min=1, max=3)
                ),
            }
        ),
    )

    # --- zaco.goto ---
    async def handle_goto(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        zaco = _resolve_zaco(hass, entity_id)
        x = call.data["x"]
        y = call.data["y"]
        _LOGGER.debug("Service goto: x=%s, y=%s", x, y)
        _track_nav_task(hass, entity_id, zaco.goto(int(x), int(y)))

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

    # --- zaco.remote_control ---
    async def handle_remote_control(call: ServiceCall) -> None:
        zaco = _resolve_zaco(hass, call.data["entity_id"])
        direction: str = call.data["direction"]
        _LOGGER.debug("Service remote_control: direction=%s", direction)
        value = REMOTE_DIRECTIONS.get(direction)
        if value is None:
            raise HomeAssistantError(
                f"Invalid direction '{direction}'. "
                f"Valid: {', '.join(REMOTE_DIRECTIONS)}"
            )
        await zaco.remote_control(value)

    hass.services.async_register(
        DOMAIN,
        "remote_control",
        handle_remote_control,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): str,
                vol.Required("direction"): vol.In(list(REMOTE_DIRECTIONS)),
            }
        ),
    )
