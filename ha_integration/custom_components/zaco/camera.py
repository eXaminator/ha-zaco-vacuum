"""Camera platform for ZACO integration — displays the vacuum map."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ZacoDataUpdateCoordinator
from .entity import ZacoEntity
from .map_renderer import MapRenderer

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ZACO map camera."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ZacoDataUpdateCoordinator = data["coordinator"]
    async_add_entities([ZacoMapCamera(coordinator, coordinator.iot_id)])


class ZacoMapCamera(ZacoEntity, Camera):
    """Camera entity that renders the vacuum's floor map."""

    _attr_name = "Map"
    _attr_is_streaming = False
    _attr_content_type = "image/png"

    def __init__(
        self,
        coordinator: ZacoDataUpdateCoordinator,
        iot_id: str,
    ) -> None:
        ZacoEntity.__init__(self, coordinator, iot_id)
        Camera.__init__(self)
        self._attr_unique_id = f"{iot_id}_map"
        self._renderer = MapRenderer()
        self._last_image: bytes | None = None
        self._calibration: dict | None = None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the current map image as PNG bytes."""
        data = self.coordinator.data
        if data is None:
            return self._last_image

        # Extract RealMapRoadData and accumulated cleaning path
        road_data_raw = data.get("RealMapRoadData", {})
        road_data_val = (
            road_data_raw.get("value")
            if isinstance(road_data_raw, dict)
            else road_data_raw
        )
        accumulated_path = data.get("_accumulated_path")

        # Extract ChargerPoint
        charger_raw = data.get("ChargerPoint", {})
        charger_val = (
            charger_raw.get("value")
            if isinstance(charger_raw, dict)
            else charger_raw
        )

        # Extract active SLAM map (SaveMapDataX9)
        slam_map_val = None
        slot = self.coordinator.active_map_slot
        if slot:
            slam_raw = data.get(f"SaveMapDataX9_{slot}", {})
            slam_map_val = (
                slam_raw.get("value")
                if isinstance(slam_raw, dict)
                else slam_raw
            )

        if not road_data_val and not slam_map_val:
            return self._last_image

        # Render in executor (Pillow is CPU-bound)
        image_bytes, calibration = await self.hass.async_add_executor_job(
            self._renderer.render, road_data_val, charger_val, slam_map_val,
            accumulated_path,
        )

        if image_bytes:
            self._last_image = image_bytes
        if calibration:
            self._calibration = calibration

        return self._last_image

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return calibration points and room data for xiaomi-vacuum-map-card.

        Exposes:
        - calibration_points: 3 non-collinear points mapping robot coords to
          pixel positions (for coordinate translation).
        - rooms: per-room bounding boxes in pixel coordinates, enabling the
          map card's "Generate rooms config" button to auto-create room
          selections.
        """
        if self._calibration is None:
            return {}

        min_x = self._calibration["min_x"]
        min_y = self._calibration["min_y"]
        max_x = self._calibration.get("max_x", min_x + 10)
        max_y = self._calibration.get("max_y", min_y + 10)
        scale = self._calibration["scale"]
        pad = self._calibration["padding"]

        # Use the full data extent for calibration points so the map card
        # derives an accurate scale factor.  A small offset (e.g. 10 units)
        # causes int() truncation to quantise the scale, producing up to
        # 25 px drift at the far edge of the map.
        span_x = max(max_x - min_x, 10)
        span_y = max(max_y - min_y, 10)

        def to_pixel(rx: int, ry: int) -> dict[str, int]:
            return {
                "x": int((rx - min_x) * scale) + pad,
                "y": int((ry - min_y) * scale) + pad,
            }

        attrs: dict[str, Any] = {
            "calibration_points": [
                {"vacuum": {"x": min_x, "y": min_y}, "map": to_pixel(min_x, min_y)},
                {"vacuum": {"x": min_x + span_x, "y": min_y}, "map": to_pixel(min_x + span_x, min_y)},
                {"vacuum": {"x": min_x, "y": min_y + span_y}, "map": to_pixel(min_x, min_y + span_y)},
            ],
        }

        # Build rooms dict for map card room generation.
        # The card expects coordinates in the vacuum's robot coordinate system
        # (it uses calibration_points to convert to pixels for display).
        # Provide convex hull outlines derived from the SLAM grid so the card
        # renders actual room shapes aligned with the map image.
        coordinator = self.coordinator
        if coordinator._room_outlines and coordinator._room_map:
            rooms_attr: dict[str, dict[str, Any]] = {}
            for name, bitmask_id in coordinator._room_map.items():
                outline = coordinator._room_outlines.get(bitmask_id)
                if not outline or len(outline) < 3:
                    continue
                rooms_attr[str(bitmask_id)] = {
                    "outline": [[x, y] for x, y in outline],
                    "name": name,
                }
            if rooms_attr:
                attrs["rooms"] = rooms_attr

        return attrs
