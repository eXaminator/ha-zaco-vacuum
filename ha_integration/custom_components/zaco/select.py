"""Select platform for ZACO integration."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    WATER_LEVELS,
    WATER_LEVELS_REVERSE,
)
from .coordinator import ZacoDataUpdateCoordinator
from .entity import ZacoEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ZACO select entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ZacoDataUpdateCoordinator = data["coordinator"]
    iot_id = coordinator.iot_id

    async_add_entities([ZacoWaterLevelSelect(coordinator, iot_id)])


class ZacoWaterLevelSelect(ZacoEntity, SelectEntity):
    """Water / mop level select."""

    _attr_name = "Water Level"
    _attr_options = list(WATER_LEVELS.keys())
    _attr_icon = "mdi:water"

    def __init__(self, coordinator: ZacoDataUpdateCoordinator, iot_id: str) -> None:
        super().__init__(coordinator, iot_id)
        self._attr_unique_id = f"{iot_id}_water_level"

    @property
    def current_option(self) -> str | None:
        val = self._get_value("WaterTankContrl")
        if val is not None:
            return WATER_LEVELS_REVERSE.get(int(val))
        return None

    async def async_select_option(self, option: str) -> None:
        level = WATER_LEVELS.get(option)
        if level is not None:
            await self.coordinator.zaco.set_properties({"WaterTankContrl": level})
