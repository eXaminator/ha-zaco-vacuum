"""Sensor platform for ZACO integration."""

from __future__ import annotations

from dataclasses import dataclass
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
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ZacoDataUpdateCoordinator
from .entity import ZacoEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class ZacoSensorEntityDescription(SensorEntityDescription):
    """Describes a ZACO sensor entity."""

    property_name: str
    sub_key: str | None = None


SENSOR_DESCRIPTIONS: tuple[ZacoSensorEntityDescription, ...] = (
    ZacoSensorEntityDescription(
        key="clean_time",
        name="Cleaning Time",
        property_name="CleanTime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-outline",
    ),
    ZacoSensorEntityDescription(
        key="clean_area",
        name="Cleaned Area",
        property_name="CleanArea",
        native_unit_of_measurement="m\u00b2",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:texture-box",
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

    async_add_entities(
        ZacoSensor(coordinator, iot_id, desc) for desc in SENSOR_DESCRIPTIONS
    )


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

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        value = self._get_value(self.entity_description.property_name)
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
