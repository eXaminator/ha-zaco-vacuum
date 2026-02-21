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
        ZacoDoNotDisturbSwitch(coordinator, iot_id),
    ])


def _decode_dnd_time(time_int: int) -> tuple[int, int, int, int]:
    """Decode a packed DND time integer into (start_h, start_m, end_h, end_m).

    The ZACO app packs 4 bytes as a big-endian int32:
      byte 0 = start hour, byte 1 = start minute,
      byte 2 = end hour,   byte 3 = end minute.
    See DataUtils.intToBytes4 / DataUtils.bytesToInt(byte[]).
    """
    start_h = (time_int >> 24) & 0xFF
    start_m = (time_int >> 16) & 0xFF
    end_h = (time_int >> 8) & 0xFF
    end_m = time_int & 0xFF
    return start_h, start_m, end_h, end_m


class ZacoDoNotDisturbSwitch(ZacoEntity, SwitchEntity):
    """Do Not Disturb toggle — silences robot beeps during a time window."""

    _attr_name = "Do Not Disturb"
    _attr_icon = "mdi:bell-off"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: ZacoDataUpdateCoordinator,
        iot_id: str,
    ) -> None:
        super().__init__(coordinator, iot_id)
        self._attr_unique_id = f"{iot_id}_do_not_disturb"

    def _get_dnd(self) -> dict | None:
        """Return the parsed BeepNoDisturb dict, or None."""
        return self._get_json_value("BeepNoDisturb")

    @property
    def is_on(self) -> bool | None:
        """Return True if DND is enabled."""
        dnd = self._get_dnd()
        if dnd is None:
            return None
        return dnd.get("Switch") == 1

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the DND time window as readable attributes."""
        dnd = self._get_dnd()
        if dnd is None:
            return None
        time_val = dnd.get("Time", 0)
        try:
            sh, sm, eh, em = _decode_dnd_time(int(time_val))
            return {
                "dnd_start": f"{sh:02d}:{sm:02d}",
                "dnd_end": f"{eh:02d}:{em:02d}",
            }
        except (ValueError, TypeError):
            return None

    async def _async_set_switch(self, switch_val: int) -> None:
        """Set DND switch while preserving the existing time window."""
        dnd = self._get_dnd()
        time_val = dnd.get("Time", 0) if dnd else 0
        await self.coordinator.zaco.set_properties(
            {"BeepNoDisturb": {"Switch": switch_val, "Time": time_val}},
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable Do Not Disturb."""
        await self._async_set_switch(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable Do Not Disturb."""
        await self._async_set_switch(0)
