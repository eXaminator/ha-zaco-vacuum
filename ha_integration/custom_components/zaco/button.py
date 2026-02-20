"""Button platform for ZACO integration."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
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
    """Set up ZACO button entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ZacoDataUpdateCoordinator = data["coordinator"]
    iot_id = coordinator.iot_id

    async_add_entities([
        ZacoSpotCleanButton(coordinator, iot_id),
        ZacoEdgeCleanButton(coordinator, iot_id),
    ])


class ZacoSpotCleanButton(ZacoEntity, ButtonEntity):
    """Button to start spot cleaning."""

    _attr_name = "Spot Clean"
    _attr_icon = "mdi:target"

    def __init__(
        self,
        coordinator: ZacoDataUpdateCoordinator,
        iot_id: str,
    ) -> None:
        super().__init__(coordinator, iot_id)
        self._attr_unique_id = f"{iot_id}_spot_clean"

    async def async_press(self) -> None:
        """Start spot cleaning."""
        await self.coordinator.client.set_properties(
            self._iot_id, {"WorkMode": 5}
        )
        await self.coordinator.async_request_refresh()


class ZacoEdgeCleanButton(ZacoEntity, ButtonEntity):
    """Button to start edge/wall-follow cleaning."""

    _attr_name = "Edge Clean"
    _attr_icon = "mdi:border-outside"

    def __init__(
        self,
        coordinator: ZacoDataUpdateCoordinator,
        iot_id: str,
    ) -> None:
        super().__init__(coordinator, iot_id)
        self._attr_unique_id = f"{iot_id}_edge_clean"

    async def async_press(self) -> None:
        """Start edge cleaning."""
        await self.coordinator.client.set_properties(
            self._iot_id, {"WorkMode": 4}
        )
        await self.coordinator.async_request_refresh()
