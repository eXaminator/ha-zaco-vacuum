"""Switch platform for ZACO integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ZacoDataUpdateCoordinator
from .entity import ZacoEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ZACO switch entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ZacoDataUpdateCoordinator = data["coordinator"]
    iot_id = coordinator.iot_id

    async_add_entities([
        ZacoCarpetControlSwitch(coordinator, iot_id),
        ZacoContinueCleanSwitch(coordinator, iot_id),
    ])


class ZacoCarpetControlSwitch(ZacoEntity, SwitchEntity):
    """Carpet auto-boost toggle — increases suction on carpets."""

    _attr_name = "Carpet Auto-Boost"
    _attr_icon = "mdi:rug"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: ZacoDataUpdateCoordinator, iot_id: str) -> None:
        super().__init__(coordinator, iot_id)
        self._attr_unique_id = f"{iot_id}_carpet_control"

    @property
    def is_on(self) -> bool | None:
        val = self._get_value("CarpetControl")
        if val is None:
            return None
        return int(val) == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.debug("CarpetControl: turning on")
        await self.coordinator.zaco.set_properties({"CarpetControl": 1})
        self.coordinator.optimistic_update({"CarpetControl": 1})
        self.coordinator.async_request_delayed_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.debug("CarpetControl: turning off")
        await self.coordinator.zaco.set_properties({"CarpetControl": 0})
        self.coordinator.optimistic_update({"CarpetControl": 0})
        self.coordinator.async_request_delayed_refresh()


class ZacoContinueCleanSwitch(ZacoEntity, SwitchEntity):
    """Breakpoint resume toggle — resumes cleaning after recharging."""

    _attr_name = "Breakpoint Resume"
    _attr_icon = "mdi:play-pause"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: ZacoDataUpdateCoordinator, iot_id: str) -> None:
        super().__init__(coordinator, iot_id)
        self._attr_unique_id = f"{iot_id}_continue_clean"

    @property
    def is_on(self) -> bool | None:
        val = self._get_value("ContinueCleanSwitch")
        if val is None:
            return None
        return int(val) == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.debug("ContinueCleanSwitch: turning on")
        await self.coordinator.zaco.set_properties({"ContinueCleanSwitch": 1})
        self.coordinator.optimistic_update({"ContinueCleanSwitch": 1})
        self.coordinator.async_request_delayed_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.debug("ContinueCleanSwitch: turning off")
        await self.coordinator.zaco.set_properties({"ContinueCleanSwitch": 0})
        self.coordinator.optimistic_update({"ContinueCleanSwitch": 0})
        self.coordinator.async_request_delayed_refresh()
