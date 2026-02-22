"""Vacuum platform for ZACO integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    WATER_LEVELS_REVERSE,
    WORKMODE_CLEANING,
    WORKMODE_ERROR,
    WORKMODE_IDLE,
    WORKMODE_PAUSED,
    WORKMODE_RETURNING,
)
from .coordinator import ZacoDataUpdateCoordinator
from .entity import ZacoEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ZACO vacuum entity."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ZacoDataUpdateCoordinator = data["coordinator"]
    async_add_entities([ZacoVacuum(coordinator, coordinator.iot_id)])


class ZacoVacuum(ZacoEntity, StateVacuumEntity):
    """ZACO robot vacuum entity."""

    _attr_name = None  # Uses device name
    _unrecorded_attributes = frozenset({MATCH_ALL})
    _attr_supported_features = (
        VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.LOCATE
        | VacuumEntityFeature.SEND_COMMAND
        | VacuumEntityFeature.STATE
    )

    def __init__(
        self,
        coordinator: ZacoDataUpdateCoordinator,
        iot_id: str,
    ) -> None:
        super().__init__(coordinator, iot_id)
        self._attr_unique_id = f"{iot_id}_vacuum"

    # -- State properties -----------------------------------------------------

    @property
    def activity(self) -> VacuumActivity | None:
        if self.coordinator.data is None:
            return None

        work_mode = int(self._get_value("WorkMode", 0))
        power_switch = int(self._get_value("PowerSwitch", 1))

        if power_switch == 0 and work_mode in WORKMODE_IDLE:
            return VacuumActivity.DOCKED
        if work_mode in WORKMODE_CLEANING:
            return VacuumActivity.CLEANING
        if work_mode in WORKMODE_PAUSED:
            return VacuumActivity.PAUSED
        if work_mode in WORKMODE_RETURNING:
            return VacuumActivity.RETURNING
        if work_mode in WORKMODE_ERROR:
            return VacuumActivity.ERROR
        if work_mode in WORKMODE_IDLE:
            return VacuumActivity.IDLE

        return VacuumActivity.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}

        fault = self._get_value("Fault")
        if fault is not None:
            attrs["fault"] = int(fault)

        water_level = self._get_value("WaterTankContrl")
        if water_level is not None:
            attrs["water_level"] = WATER_LEVELS_REVERSE.get(int(water_level), str(water_level))

        work_mode = self._get_value("WorkMode")
        if work_mode is not None:
            attrs["work_mode"] = int(work_mode)

        rooms = self.coordinator.rooms
        if rooms:
            attrs["available_rooms"] = list(rooms.keys())

        return attrs

    # -- Commands -------------------------------------------------------------

    async def async_start(self, **kwargs: Any) -> None:
        _LOGGER.debug("Vacuum: async_start")
        await self.coordinator.zaco.start()
        self.coordinator.async_request_delayed_refresh()

    async def async_stop(self, **kwargs: Any) -> None:
        _LOGGER.debug("Vacuum: async_stop")
        await self.coordinator.zaco.stop()
        self.coordinator.async_request_delayed_refresh()

    async def async_pause(self, **kwargs: Any) -> None:
        if self.activity == VacuumActivity.PAUSED:
            _LOGGER.debug("Vacuum: async_pause -> resume (was paused)")
            await self.coordinator.zaco.resume()
        else:
            _LOGGER.debug("Vacuum: async_pause -> pause")
            await self.coordinator.zaco.pause()
        self.coordinator.async_request_delayed_refresh()

    async def async_return_to_base(self, **kwargs: Any) -> None:
        _LOGGER.debug("Vacuum: async_return_to_base")
        await self.coordinator.zaco.return_to_base()
        self.coordinator.async_request_delayed_refresh()

    async def async_locate(self, **kwargs: Any) -> None:
        _LOGGER.debug("Vacuum: async_locate")
        await self.coordinator.zaco.locate()
        self.coordinator.async_request_delayed_refresh()

    async def async_send_command(
        self,
        command: str,
        params: dict[str, Any] | list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Handle generic commands."""
        _LOGGER.debug("Vacuum: send_command(%s, %s)", command, params)
        if params is None:
            params = {}

        zaco = self.coordinator.zaco

        if command == "set_properties" and isinstance(params, dict):
            await zaco.set_properties(params)
        elif command == "clean_rooms" and isinstance(params, dict):
            room_ids = params.get("room_ids", [])
            passes = params.get("passes", 1)
            await zaco.clean_rooms(room_ids, passes=passes)
        elif command == "edge_clean":
            await zaco.edge_clean()
        else:
            _LOGGER.warning("Unknown command: %s", command)
            return
        self.coordinator.async_request_delayed_refresh()
