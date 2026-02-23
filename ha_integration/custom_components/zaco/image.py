"""Image platform for ZACO integration — last cleaning map snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ZacoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ZACO last cleaning map image."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ZacoDataUpdateCoordinator = data["coordinator"]
    async_add_entities([ZacoLastCleaningImage(coordinator, hass)])


class ZacoLastCleaningImage(CoordinatorEntity, ImageEntity):
    """Image entity showing the last cleaning session's map with path.

    Cannot inherit from ZacoEntity because ImageEntity.__init__(hass)
    and CoordinatorEntity.__init__(coordinator) conflict in the MRO.
    Instead we inherit both directly and replicate device_info here.
    """

    _attr_has_entity_name = True
    _attr_name = "Last Cleaning Map"
    _attr_content_type = "image/png"
    _unrecorded_attributes = frozenset({MATCH_ALL})

    coordinator: ZacoDataUpdateCoordinator

    def __init__(
        self,
        coordinator: ZacoDataUpdateCoordinator,
        hass: HomeAssistant,
    ) -> None:
        # CoordinatorEntity first — sets up self.coordinator and listener
        CoordinatorEntity.__init__(self, coordinator)
        # ImageEntity second — needs hass for access_tokens / http client
        ImageEntity.__init__(self, hass)

        self._iot_id = coordinator.iot_id
        self._attr_unique_id = f"{self._iot_id}_last_cleaning_map"
        self._cached_ts_ms: int = 0

        # Set initial timestamp from persisted snapshot (prefer start time)
        snapshot = coordinator.last_cleaning
        if snapshot:
            ts = snapshot.start_ms or snapshot.end_ms
            if ts:
                self._attr_image_last_updated = datetime.fromtimestamp(
                    ts / 1000, tz=timezone.utc,
                )
                self._cached_ts_ms = ts

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to group under the same device as other entities."""
        dev = self.coordinator.device_info
        return DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=dev.get("nickName", "ZACO Vacuum"),
            manufacturer="ZACO",
            model=dev.get("productModel", "A10"),
        )

    async def async_image(self) -> bytes | None:
        """Return the last cleaning map image."""
        return self.coordinator.last_cleaning_image

    @property
    def available(self) -> bool:
        """Entity is available when we have a snapshot image."""
        return self.coordinator.last_cleaning_image is not None

    @property
    def image_last_updated(self) -> datetime | None:
        """Return when the cleaning started (or ended as fallback)."""
        snapshot = self.coordinator.last_cleaning
        if snapshot:
            ts = snapshot.start_ms or snapshot.end_ms
            if ts and ts != self._cached_ts_ms:
                self._cached_ts_ms = ts
                self._attr_image_last_updated = datetime.fromtimestamp(
                    ts / 1000, tz=timezone.utc,
                )
        return self._attr_image_last_updated

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose cleaning session stats as attributes."""
        snapshot = self.coordinator.last_cleaning
        if snapshot is None:
            return {}
        attrs: dict[str, Any] = {}
        ts = snapshot.start_ms or snapshot.end_ms
        if ts:
            attrs["timestamp"] = datetime.fromtimestamp(
                ts / 1000, tz=timezone.utc,
            ).isoformat()
        if snapshot.clean_time_min is not None:
            attrs["duration_min"] = snapshot.clean_time_min
        if snapshot.clean_area_m2 is not None:
            attrs["area_m2"] = snapshot.clean_area_m2
        return attrs
