"""Camera platform for ZACO integration — displays the vacuum map."""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, WORKMODE_IDLE
from .coordinator import ZacoDataUpdateCoordinator
from .entity import ZacoEntity
from .zaco.map_renderer import MapRenderer, _decode_point_int
from .zaco._helpers import parse_int_prop

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
    _unrecorded_attributes = frozenset({MATCH_ALL})

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
        self._last_fingerprint: tuple | None = None

    @staticmethod
    def _charger_heading(
        charger_pos: tuple[int, int],
        grid_lookup: dict[tuple[int, int], int],
    ) -> int:
        """Compute the heading for a docked robot facing into the room.

        Casts rays in 8 directions from the charger; the direction that
        hits a non-room cell (empty/border) soonest is the wall behind
        the dock.  The robot faces the opposite direction (into the room).
        Returns heading in degrees (0=east, 90=north in map coords).
        """
        # 8 directions: (dx, dy, angle_deg) — angles match map_renderer convention
        directions = [
            (1, 0, 0),      # east
            (1, -1, 45),    # north-east
            (0, -1, 90),    # north
            (-1, -1, 135),  # north-west
            (-1, 0, 180),   # west
            (-1, 1, 225),   # south-west
            (0, 1, 270),    # south
            (1, 1, 315),    # south-east
        ]
        best_dist = 999
        wall_angle = 0
        cx, cy = charger_pos
        for dx, dy, angle in directions:
            for step in range(1, 30):
                nx, ny = cx + dx * step, cy + dy * step
                cell = grid_lookup.get((nx, ny))
                # None = outside grid, 0 = empty, 3 = border — all "wall"
                if cell is None or cell in (0, 3):
                    if step < best_dist:
                        best_dist = step
                        wall_angle = angle
                    break
        # Robot faces away from the wall (into the room)
        return (wall_angle + 180) % 360

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the current map image as PNG bytes."""
        data = self.coordinator.data
        if data is None:
            _LOGGER.debug("Camera: no coordinator data, returning cached image")
            return self._last_image

        # Extract RealMapRoadData and accumulated cleaning path
        road_data_raw = data.get("RealMapRoadData", {})
        road_data_val = (
            road_data_raw.get("value")
            if isinstance(road_data_raw, dict)
            else road_data_raw
        )

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
            _LOGGER.debug("Camera: no road data or SLAM map, returning cached image")
            return self._last_image

        # When docked, snap robot position to charger and compute heading
        # toward the room interior (away from nearest wall).  The API
        # reports garbage CurrentPoint values when docked.
        wm = parse_int_prop(data, "WorkMode")
        ps = parse_int_prop(data, "PowerSwitch")
        is_docked = ps == 0 and wm is not None and wm in WORKMODE_IDLE
        if is_docked and charger_val:
            charger_data_tmp = charger_val if isinstance(charger_val, dict) else None
            piont_raw = (charger_data_tmp or {}).get("Piont")
            if piont_raw is not None:
                # Override CurrentPoint with charger position
                if isinstance(road_data_val, dict):
                    road_data_val = dict(road_data_val)
                elif isinstance(road_data_val, str):
                    try:
                        road_data_val = json.loads(road_data_val)
                    except (json.JSONDecodeError, ValueError):
                        road_data_val = {}
                else:
                    road_data_val = {}
                road_data_val["CurrentPoint"] = piont_raw

                # Compute heading from SLAM grid
                grid = self.coordinator.zaco.grid_lookup
                if grid:
                    charger_pos = _decode_point_int(int(piont_raw))
                    if charger_pos:
                        heading = self._charger_heading(charger_pos, grid)
                        road_data_val["CleanDirection"] = heading

        # Skip rendering if nothing changed since the last render
        road_data = road_data_val if isinstance(road_data_val, dict) else None
        charger_data = charger_val if isinstance(charger_val, dict) else None
        fingerprint = (
            road_data.get("CurrentPoint") if road_data else None,
            road_data.get("CleanDirection") if road_data else None,
            road_data.get("RoadData") if road_data else None,
            charger_data.get("Piont") if charger_data else None,
            self.coordinator.zaco.path_length,
            slot,
        )
        if fingerprint == self._last_fingerprint and self._last_image is not None:
            _LOGGER.debug("Camera: fingerprint unchanged, skipping render")
            return self._last_image

        accumulated_path = self.coordinator.zaco.accumulated_path or []
        _LOGGER.debug(
            "Camera: rendering map (slot=%s, has_road=%s, has_slam=%s, "
            "has_charger=%s, path_len=%s)",
            slot, road_data is not None, slam_map_val is not None,
            charger_data is not None,
            len(accumulated_path) if accumulated_path else 0,
        )

        # Render in executor (Pillow is CPU-bound)
        p2b = self.coordinator.zaco.partition_to_bitmask or None
        try:
            image_bytes, calibration = await self.hass.async_add_executor_job(
                self._renderer.render, road_data_val, charger_val, slam_map_val,
                accumulated_path, p2b,
            )
        except Exception:
            _LOGGER.exception("Camera: map render failed")
            return self._last_image

        if image_bytes:
            _LOGGER.debug("Camera: rendered %d bytes", len(image_bytes))
            self._last_image = image_bytes
            self._last_fingerprint = fingerprint
        else:
            _LOGGER.debug("Camera: render returned None")
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

        # Room data for the map card.  Sources (by priority):
        # 1. HA area mapping (name + icon from the vacuum entity's segment config)
        # 2. MapRoomInfo from cloud API (name only)
        # 3. SLAM map partitions (bitmask IDs, generic names)
        zaco = self.coordinator.zaco
        outlines = zaco.room_outlines
        room_map = zaco.rooms  # {name: bitmask_id} from MapRoomInfo
        room_centers = zaco.room_centers  # {bitmask_id: (cx, cy)} from SLAM

        # Build segment_id -> (area_name, area_icon) from HA area mapping
        area_info = self._get_area_info_map()

        # Collect all known bitmask IDs from both sources
        all_bitmask_ids: set[int] = set(room_map.values()) | set(room_centers.keys())
        id_to_name: dict[int, str] = {v: k for k, v in room_map.items()}

        if all_bitmask_ids:
            # Assign letters A-Z by bitmask order (matches ZACO app)
            sorted_ids = sorted(all_bitmask_ids)
            id_to_letter = {
                bid: chr(ord("A") + i) for i, bid in enumerate(sorted_ids)
            }

            rooms_attr: dict[str, dict[str, Any]] = {}
            for bitmask_id in sorted_ids:
                seg_id = str(bitmask_id)
                letter = id_to_letter[bitmask_id]
                area_name, area_icon = area_info.get(seg_id, (None, None))
                name = area_name or id_to_name.get(bitmask_id) or f"Room {letter}"
                room_data: dict[str, Any] = {
                    "name": name,
                    "letter": letter,
                    "outline": outlines.get(bitmask_id, []),
                }
                if area_icon:
                    room_data["icon"] = area_icon
                center = room_centers.get(bitmask_id)
                if center:
                    # Provide x/y as top-level fields — the xiaomi-vacuum-map-card
                    # reads these for icon/label positioning.
                    # Shift up slightly (smaller Y = higher on screen in robot coords)
                    # so the icon+label combo is visually centered in the room.
                    room_data["x"] = center[0]
                    room_data["y"] = center[1] - 3
                rooms_attr[seg_id] = room_data
            attrs["rooms"] = rooms_attr

        return attrs

    def _get_area_info_map(self) -> dict[str, tuple[str | None, str | None]]:
        """Build {segment_id: (area_name, area_icon)} from vacuum entity's area mapping."""
        try:
            ent_reg = er.async_get(self.hass)
            area_reg = ar.async_get(self.hass)
        except Exception:
            return {}

        # Find the vacuum entity for this config entry
        entry = None
        for ent in ent_reg.entities.get_entries_for_config_entry_id(
            self.coordinator.config_entry.entry_id
        ):
            if ent.domain == "vacuum":
                entry = ent
                break
        if entry is None:
            return {}

        area_mapping: dict[str, list[str]] = (
            entry.options.get("vacuum", {}).get("area_mapping", {})
        )
        result: dict[str, tuple[str | None, str | None]] = {}
        for area_id, seg_ids in area_mapping.items():
            area_entry = area_reg.async_get_area(area_id)
            if area_entry:
                for seg_id in seg_ids:
                    result[seg_id] = (area_entry.name, area_entry.icon)
        return result
