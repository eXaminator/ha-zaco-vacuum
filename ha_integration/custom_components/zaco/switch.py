"""Switch platform for ZACO integration — room selection toggles."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Set up ZACO room selection switches."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ZacoDataUpdateCoordinator = data["coordinator"]
    iot_id = coordinator.iot_id

    entities = [
        ZacoRoomSwitch(coordinator, iot_id, room_name, room_id)
        for room_name, room_id in coordinator.rooms.items()
    ]
    async_add_entities(entities)


class ZacoRoomSwitch(ZacoEntity, SwitchEntity):
    """Toggle to select a room for the next cleaning run."""

    _attr_icon = "mdi:floor-plan"

    def __init__(
        self,
        coordinator: ZacoDataUpdateCoordinator,
        iot_id: str,
        room_name: str,
        room_id: int,
    ) -> None:
        super().__init__(coordinator, iot_id)
        self._room_name = room_name
        self._room_id = room_id
        self._attr_name = room_name
        self._attr_unique_id = f"{iot_id}_room_{room_id}"

    @property
    def is_on(self) -> bool:
        """Return True if this room is selected for cleaning."""
        return self.coordinator.is_room_selected(self._room_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Select this room for cleaning."""
        self.coordinator.select_room(self._room_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Deselect this room from cleaning."""
        self.coordinator.deselect_room(self._room_id)
