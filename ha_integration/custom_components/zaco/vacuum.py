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
    _attr_supported_features = (
        VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.BATTERY
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
        """Return the current vacuum activity."""
        if self.coordinator.data is None:
            return None

        work_mode = int(self._get_value("WorkMode", 0))
        power_switch = int(self._get_value("PowerSwitch", 1))

        # Docked: PowerSwitch == 0 AND idle WorkMode.
        # PowerSwitch is also 0 during pause and return, so WorkMode
        # must confirm the robot is actually idle (e.g. WorkMode 9).
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
    def battery_level(self) -> int | None:
        """Return the battery level."""
        val = self._get_value("BatteryState")
        if val is not None:
            return int(val)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
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

        # Room info
        rooms = self.coordinator.rooms
        if rooms:
            attrs["available_rooms"] = list(rooms.keys())

        return attrs

    # -- Commands -------------------------------------------------------------

    async def async_start(self, **kwargs: Any) -> None:
        """Start cleaning.

        If rooms are selected via room switches, cleans only those rooms
        using the configured number of passes. Otherwise starts a full
        auto-clean with the saved map.
        """
        room_ids = self.coordinator.selected_room_ids
        if room_ids:
            partition_data = sum(room_ids)
            passes = self.coordinator.cleaning_passes
            await self.coordinator.client.set_properties(
                self._iot_id,
                {
                    "CleanPartitionData": {
                        "PartitionData": partition_data,
                        "CleanLoop": min(max(passes, 1), 3),
                        "Enable": 1,
                    }
                },
            )
        else:
            await self.coordinator.client.set_properties(
                self._iot_id, {"WorkMode": 6}
            )
        await self.coordinator.async_request_refresh()

    async def async_stop(self, **kwargs: Any) -> None:
        """Stop cleaning (standby)."""
        await self.coordinator.client.set_properties(
            self._iot_id, {"WorkMode": 2}
        )
        await self.coordinator.async_request_refresh()

    async def async_pause(self, **kwargs: Any) -> None:
        """Pause cleaning."""
        await self.coordinator.client.set_properties(
            self._iot_id, {"PauseSwitch": 1}
        )
        await self.coordinator.async_request_refresh()

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Return to charging dock."""
        await self.coordinator.client.set_properties(
            self._iot_id, {"WorkMode": 8}
        )
        await self.coordinator.async_request_refresh()

    async def async_locate(self, **kwargs: Any) -> None:
        """Locate the vacuum (beep)."""
        await self.coordinator.client.set_properties(
            self._iot_id, {"SoundLocate": {"SoundDir": 0}}
        )

    async def async_send_command(
        self,
        command: str,
        params: dict[str, Any] | list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Handle generic commands.

        Supported commands:
          - set_properties: Pass a dict of property key/value pairs
          - clean_rooms: Pass {"room_ids": [1, 4, 32], "passes": 1}
          - edge_clean: Start edge/wall-follow cleaning
        """
        if params is None:
            params = {}

        if command == "set_properties" and isinstance(params, dict):
            await self.coordinator.client.set_properties(self._iot_id, params)

        elif command == "clean_rooms" and isinstance(params, dict):
            room_ids = params.get("room_ids", [])
            passes = params.get("passes", 1)
            partition_data = sum(room_ids)
            await self.coordinator.client.set_properties(
                self._iot_id,
                {
                    "CleanPartitionData": {
                        "PartitionData": partition_data,
                        "CleanLoop": min(max(passes, 1), 3),
                        "Enable": 1,
                    }
                },
            )

        elif command == "edge_clean":
            await self.coordinator.client.set_properties(
                self._iot_id, {"WorkMode": 20}
            )

        else:
            _LOGGER.warning("Unknown command: %s", command)
            return

        await self.coordinator.async_request_refresh()
