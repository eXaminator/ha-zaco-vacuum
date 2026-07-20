"""Base entity for ZACO integration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import ZacoDataUpdateCoordinator


class ZacoEntity(CoordinatorEntity):
    """Base class for ZACO entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ZacoDataUpdateCoordinator,
        iot_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._iot_id = iot_id

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to group entities under one device."""
        dev = self.coordinator.device_info
        return DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=dev.get("nickName", "ZACO Vacuum"),
            manufacturer="ZACO",
            model=dev.get("productModel", "A10"),
            sw_version=self._get_value("SoftwareVer"),
            hw_version=self._get_value("HardwareVer"),
        )

    def _get_value(self, property_name: str, default: Any = None) -> Any:
        """Extract a property value from coordinator data.

        The API returns properties as {"PropertyName": {"value": X, "time": T}}.
        This helper extracts the inner 'value' field.
        """
        if self.coordinator.data is None:
            return default
        raw = self.coordinator.data.get(property_name)
        if raw is None:
            return default
        if isinstance(raw, dict):
            return raw.get("value", default)
        return raw

    def _get_json_value(self, property_name: str) -> dict | None:
        """Extract a property value and parse it as JSON if it's a string."""
        val = self._get_value(property_name)
        if val is None:
            return None
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, ValueError):
                return None
        if isinstance(val, dict):
            return val
        return None
