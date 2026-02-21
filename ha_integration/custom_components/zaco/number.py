"""Number platform for ZACO integration."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
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
    """Set up ZACO number entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ZacoDataUpdateCoordinator = data["coordinator"]
    iot_id = coordinator.iot_id

    async_add_entities([
        ZacoSuctionPowerNumber(coordinator, iot_id),
        ZacoSideBrushSpeedNumber(coordinator, iot_id),
    ])


class ZacoSuctionPowerNumber(ZacoEntity, NumberEntity):
    """Suction power slider (1-100%)."""

    _attr_name = "Suction Power"
    _attr_native_min_value = 1
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:fan"

    def __init__(self, coordinator: ZacoDataUpdateCoordinator, iot_id: str) -> None:
        super().__init__(coordinator, iot_id)
        self._attr_unique_id = f"{iot_id}_suction_power"

    @property
    def native_value(self) -> float | None:
        val = self._get_value("FanPower")
        if val is not None:
            return int(val)
        return None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.zaco.set_fan_power(int(value))


class ZacoSideBrushSpeedNumber(ZacoEntity, NumberEntity):
    """Side brush speed slider (1-100%)."""

    _attr_name = "Side Brush Speed"
    _attr_native_min_value = 1
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:rotate-right"

    def __init__(self, coordinator: ZacoDataUpdateCoordinator, iot_id: str) -> None:
        super().__init__(coordinator, iot_id)
        self._attr_unique_id = f"{iot_id}_side_brush_speed"

    @property
    def native_value(self) -> float | None:
        settings = self.coordinator.zaco.get_clean_settings_bytes()
        if settings is None or len(settings) < 4:
            return None
        val = settings[3]
        return val if val > 0 else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.zaco.set_clean_setting(3, max(int(value), 1))
