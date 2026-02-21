"""DataUpdateCoordinator for ZACO integration.

Thin bridge between the Zaco library and Home Assistant's entity update
machinery.  All robot logic (polling, parsing, commands) lives in the
Zaco class; this coordinator exists solely because HA entities require
CoordinatorEntity for automatic state updates.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .zaco import Zaco

_LOGGER = logging.getLogger(__name__)

# Safety-net interval — Zaco's internal poll loop is the real driver.
_SAFETY_NET_INTERVAL = 300  # seconds


class ZacoDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that wraps a Zaco instance for HA entity updates."""

    def __init__(self, hass: HomeAssistant, zaco: Zaco) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"zaco_{zaco.iot_id}",
            update_interval=timedelta(seconds=_SAFETY_NET_INTERVAL),
        )
        self.zaco = zaco

        # Wire Zaco's data-updated callback to push to HA entities.
        zaco.on_data_updated = self._handle_zaco_update

        # Seed coordinator.data from Zaco (may already have data from
        # initial refresh in factory method).
        if zaco.data is not None:
            self.data = zaco.data

    def _handle_zaco_update(self, data: dict[str, Any]) -> None:
        """Called by Zaco after each refresh or MQTT push."""
        self.async_set_updated_data(data)

    async def _async_update_data(self) -> dict[str, Any]:
        """Safety-net poll — just returns current Zaco data.

        The real polling happens inside Zaco._poll_loop at adaptive
        intervals (3s active, 30s idle, 120s with MQTT).  This method
        is called by HA's built-in timer at a very slow cadence as a
        fallback.
        """
        if self.zaco.data is not None:
            return self.zaco.data
        # If Zaco has no data yet (shouldn't happen), do a manual refresh.
        return await self.zaco.refresh()

    # -- Convenience accessors for entity platforms ---------------------------
    # These delegate to zaco.* so entity code can use coordinator.X
    # without reaching through coordinator.zaco.X everywhere.

    @property
    def iot_id(self) -> str:
        return self.zaco.iot_id

    @property
    def device_info(self) -> dict[str, Any]:
        return self.zaco.device_info

    @property
    def rooms(self) -> dict[str, int]:
        return self.zaco.rooms

    @property
    def current_room(self) -> str | None:
        return self.zaco.current_room

    @property
    def active_map_slot(self) -> int | None:
        return self.zaco.active_map_slot

    @property
    def client(self):
        """Exposed for token persistence in __init__.py."""
        return self.zaco.client
