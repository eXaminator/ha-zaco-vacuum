"""Sensor platform for ZACO integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL, PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, WORKMODE_IDLE
from .coordinator import ZacoDataUpdateCoordinator
from .entity import ZacoEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class ZacoSensorEntityDescription(SensorEntityDescription):
    """Describes a ZACO sensor entity."""

    property_name: str
    sub_key: str | None = None
    unrecorded: bool = False


SENSOR_DESCRIPTIONS: tuple[ZacoSensorEntityDescription, ...] = (
    ZacoSensorEntityDescription(
        key="battery",
        name="Battery",
        property_name="BatteryState",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ZacoSensorEntityDescription(
        key="clean_time",
        name="Cleaning Time",
        property_name="CleanTime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:timer-outline",
        unrecorded=True,
    ),
    ZacoSensorEntityDescription(
        key="clean_area",
        name="Cleaned Area",
        property_name="CleanArea",
        native_unit_of_measurement="m\u00b2",
        icon="mdi:texture-box",
        unrecorded=True,
    ),
    ZacoSensorEntityDescription(
        key="current_room",
        name="Current Room",
        property_name="CurrentRoom",
        icon="mdi:floor-plan",
    ),
    ZacoSensorEntityDescription(
        key="error_code",
        name="Error Code",
        property_name="ErrorCode",
        icon="mdi:alert-circle",
        unrecorded=True,
    ),
    ZacoSensorEntityDescription(
        key="filter_life",
        name="Filter Life",
        property_name="PartsStatus",
        sub_key="FilterLife",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:air-filter",
    ),
    ZacoSensorEntityDescription(
        key="main_brush_life",
        name="Main Brush Life",
        property_name="PartsStatus",
        sub_key="MainBrushLife",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:brush",
    ),
    ZacoSensorEntityDescription(
        key="side_brush_life",
        name="Side Brush Life",
        property_name="PartsStatus",
        sub_key="SideBrushLife",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:rotate-right",
    ),
    ZacoSensorEntityDescription(
        key="wifi_rssi",
        name="WiFi Signal",
        property_name="WiFiInfo",
        sub_key="RSSI",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ZacoSensorEntityDescription(
        key="last_clean_duration",
        name="Last Clean Duration",
        property_name="CleanHistory",
        sub_key="CleanTotalTime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:history",
    ),
    ZacoSensorEntityDescription(
        key="last_clean_area",
        name="Last Clean Area",
        property_name="CleanHistory",
        sub_key="CleanTotalArea",
        native_unit_of_measurement="m\u00b2",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:texture-box",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ZACO sensor entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ZacoDataUpdateCoordinator = data["coordinator"]
    iot_id = coordinator.iot_id

    entities: list[SensorEntity] = [
        ZacoSensor(coordinator, iot_id, desc) for desc in SENSOR_DESCRIPTIONS
    ]
    entities.append(ZacoLastCleaningSensor(coordinator, iot_id))
    async_add_entities(entities)


class ZacoSensor(ZacoEntity, SensorEntity):
    """A ZACO sensor entity."""

    entity_description: ZacoSensorEntityDescription

    def __init__(
        self,
        coordinator: ZacoDataUpdateCoordinator,
        iot_id: str,
        description: ZacoSensorEntityDescription,
    ) -> None:
        self.entity_description = description
        self._attr_name = description.name
        super().__init__(coordinator, iot_id)
        self._attr_unique_id = f"{iot_id}_sensor_{description.key}"
        if description.unrecorded:
            self._unrecorded_attributes = frozenset({MATCH_ALL})

    @property
    def icon(self) -> str | None:
        """Return a charging icon when the battery sensor detects docked state."""
        if self.entity_description.key != "battery":
            return self.entity_description.icon

        power_switch = self._get_value("PowerSwitch")
        work_mode = self._get_value("WorkMode")
        if power_switch is None or work_mode is None:
            return None

        if int(power_switch) == 0 and int(work_mode) in WORKMODE_IDLE:
            level = self.native_value
            if level is None:
                return "mdi:battery-charging"
            level = max(0, min(100, int(level)))
            if level == 0:
                return "mdi:battery-charging-outline"
            return f"mdi:battery-charging-{(level // 10) * 10}"

        return None

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        value = self._get_value(self.entity_description.property_name)
        _LOGGER.debug(
            "Sensor %s: property=%s, raw_value=%r, type=%s, data_keys=%s",
            self.entity_description.key,
            self.entity_description.property_name,
            value,
            type(value).__name__,
            list(self.coordinator.data.keys())[:10] if self.coordinator.data else "None",
        )
        if value is None:
            return None

        # Handle nested JSON values (e.g., PartsStatus.FilterLife)
        if self.entity_description.sub_key:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    return None
            if isinstance(value, dict):
                return value.get(self.entity_description.sub_key)
            return None

        # Ensure numeric sensors return numeric values
        if isinstance(value, (int, float)):
            return value

        # Cast string numbers to the appropriate type
        if isinstance(value, str) and self.entity_description.native_unit_of_measurement:
            try:
                return float(value) if "." in value else int(value)
            except (ValueError, TypeError):
                return None

        return value


class ZacoLastCleaningSensor(ZacoEntity, SensorEntity):
    """Sensor showing the last cleaning session's start time.

    State is a UTC datetime (device_class=TIMESTAMP); HA and the vacuum
    card auto-format it according to the user's locale.
    Attributes expose duration and area.
    Designed for use as a tile in the xiaomi-vacuum-map-card.
    """

    _attr_has_entity_name = True
    _attr_name = "Last Cleaning"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:map-clock-outline"
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(
        self,
        coordinator: ZacoDataUpdateCoordinator,
        iot_id: str,
    ) -> None:
        super().__init__(coordinator, iot_id)
        self._attr_unique_id = f"{iot_id}_sensor_last_cleaning"

    @property
    def native_value(self) -> datetime | None:
        """Return the last cleaning start time as a UTC datetime."""
        snapshot = self.coordinator.last_cleaning
        if snapshot is None:
            return None
        ts = snapshot.start_ms or snapshot.end_ms
        if not ts:
            return None
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

    @property
    def available(self) -> bool:
        """Available when a last cleaning snapshot exists."""
        return self.coordinator.last_cleaning is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose cleaning duration and area as attributes."""
        snapshot = self.coordinator.last_cleaning
        if snapshot is None:
            return {}
        attrs: dict[str, Any] = {}
        if snapshot.clean_time_min is not None:
            attrs["duration_min"] = snapshot.clean_time_min
        if snapshot.clean_area_m2 is not None:
            attrs["area_m2"] = snapshot.clean_area_m2
        return attrs
