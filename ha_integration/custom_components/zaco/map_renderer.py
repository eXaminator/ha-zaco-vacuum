"""Map rendering for ZACO vacuum — decodes SLAM grid and cleaning path to PNG.

Two data sources:
  1. SaveMapDataX9: RLE-encoded SLAM grid with room partitions (floor plan)
  2. RealMapRoadData: cleaning path coordinates + robot/charger positions

The renderer draws the SLAM grid as colored rooms, then overlays the cleaning
path, charger, and robot position on top.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import struct
from typing import Any

from PIL import Image, ImageDraw

_LOGGER = logging.getLogger(__name__)

# Room colors (indexed by room_type - 1, cycling for IDs > 8)
ROOM_COLORS = [
    (100, 166, 219),  # blue
    (130, 200, 130),  # green
    (219, 166, 100),  # orange
    (180, 130, 200),  # purple
    (200, 200, 100),  # yellow
    (200, 130, 130),  # red
    (100, 200, 200),  # cyan
    (180, 180, 140),  # khaki
]

# General colors
COLOR_BACKGROUND = (0, 0, 0, 0)
COLOR_PATH = (255, 255, 255, 200)
COLOR_ROBOT = (0, 120, 255)
COLOR_CHARGER = (0, 200, 0)
COLOR_ROBOT_OUTLINE = (255, 255, 255)
COLOR_OUTLINE = (40, 40, 40, 220)  # room outline / wall gap cells

# Rendering constants
IMAGE_MAX_SIZE = 800
IMAGE_PADDING = 30
ROBOT_RADIUS = 10
CHARGER_RADIUS = 8
PATH_WIDTH = 2
CELL_SIZE = 2  # pixels per grid cell


# ---------------------------------------------------------------------------
# Byte decoding helpers
# ---------------------------------------------------------------------------

def _bytes_to_int16(b0: int, b1: int) -> int:
    """Decode two bytes as a signed big-endian int16."""
    value = ((b0 & 0xFF) << 8) | (b1 & 0xFF)
    if b0 & 0x80:
        value -= 65536
    return value


def _decode_point_int(value: int) -> tuple[int, int] | None:
    """Decode a 32-bit packed coordinate (CurrentPoint / ChargerPoint.Piont)."""
    if value is None or value == 0:
        return None
    try:
        raw = struct.pack(">i", value)
    except (struct.error, OverflowError):
        return None
    x = _bytes_to_int16(raw[0], raw[1])
    y = -_bytes_to_int16(raw[2], raw[3])
    return (x, y)


# ---------------------------------------------------------------------------
# SLAM grid decoder (from SaveMapDataX9)
# ---------------------------------------------------------------------------

def _decode_slam_grid(
    map_data: dict[str, Any],
) -> tuple[list[tuple[int, int, int]], int, int, int, int] | None:
    """Decode RLE-encoded SLAM grid from SaveMapDataX9 property value.

    Returns (grid_points, min_x, min_y, max_x, max_y) or None on failure.
    Each grid point is (x, y, room_type).
    """
    # Concatenate MapData1-7 base64 chunks
    all_bytes = bytearray()
    for i in range(1, 8):
        chunk_b64 = map_data.get(f"MapData{i}", "")
        if not chunk_b64:
            continue
        try:
            all_bytes.extend(base64.b64decode(chunk_b64))
        except Exception:
            continue

    if len(all_bytes) < 9:
        return None

    # Header: byte 0 = type, bytes 2-3 = originX, bytes 4-5 = originY,
    # bytes 6-7 = gridHeight
    origin_x = _bytes_to_int16(all_bytes[2], all_bytes[3])
    origin_y = _bytes_to_int16(all_bytes[4], all_bytes[5])
    grid_height = _bytes_to_int16(all_bytes[6], all_bytes[7])

    if grid_height <= 0:
        return None

    # RLE decode from byte 9 onward (2-byte pairs: [type, count])
    grid_points: list[tuple[int, int, int]] = []
    grid_x = 0
    grid_y = 0

    idx = 8  # pairs start at byte 8 (0-indexed), reading [idx] and [idx+1]
    while idx + 1 < len(all_bytes):
        cell_type = all_bytes[idx] & 0xFF
        cell_count = all_bytes[idx + 1] & 0xFF
        idx += 2

        for _ in range(cell_count):
            # Skip empty (0) and padding (4) cells; keep border (3) for outline
            if cell_type not in (0, 4):
                x = origin_x - grid_x
                y = grid_y - origin_y
                grid_points.append((x, y, cell_type))

            # Column-major scan: increment Y, wrap to next X column
            if grid_y < grid_height - 1:
                grid_y += 1
            else:
                grid_x += 1
                grid_y = 0

    if not grid_points:
        return None

    min_x = min(p[0] for p in grid_points)
    max_x = max(p[0] for p in grid_points)
    min_y = min(p[1] for p in grid_points)
    max_y = max(p[1] for p in grid_points)

    return grid_points, min_x, min_y, max_x, max_y


# ---------------------------------------------------------------------------
# Road data decoder (from RealMapRoadData)
# ---------------------------------------------------------------------------

def _decode_road_data(b64_string: str) -> list[tuple[int, int]]:
    """Decode base64 RoadData into a list of (x, y) coordinate pairs."""
    try:
        raw = base64.b64decode(b64_string)
    except Exception:
        return []

    points: list[tuple[int, int]] = []
    for i in range(len(raw) // 4):
        offset = i * 4
        if offset + 3 >= len(raw):
            break
        x = _bytes_to_int16(raw[offset], raw[offset + 1])
        y = -_bytes_to_int16(raw[offset + 2], raw[offset + 3])
        points.append((x, y))

    return points


# ---------------------------------------------------------------------------
# Extract nested property values
# ---------------------------------------------------------------------------

def _extract_value(raw: Any) -> Any:
    """Extract inner value from API property wrapper."""
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    return raw


def _parse_json_or_dict(val: Any) -> dict | None:
    """Parse a value that may be a JSON string or already a dict."""
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return None


# ---------------------------------------------------------------------------
# Room color helper
# ---------------------------------------------------------------------------

def _room_color(room_type: int) -> tuple[int, int, int]:
    """Get RGB color for a room type (1-32)."""
    if room_type <= 0:
        return (0, 0, 0)
    idx = (room_type - 1) % len(ROOM_COLORS)
    return ROOM_COLORS[idx]


# ---------------------------------------------------------------------------
# MapRenderer
# ---------------------------------------------------------------------------

class MapRenderer:
    """Renders vacuum map data into PNG images."""

    def render(
        self,
        road_data_value: Any = None,
        charger_value: Any = None,
        slam_map_value: Any = None,
        accumulated_path: list[tuple[int, int]] | None = None,
    ) -> tuple[bytes | None, dict | None]:
        """Render map to PNG bytes with calibration metadata.

        Args:
            road_data_value: RealMapRoadData property value (dict or JSON string).
            charger_value: ChargerPoint property value (dict or JSON string).
            slam_map_value: SaveMapDataX9 property value (dict with MapData1-7).
            accumulated_path: Pre-accumulated cleaning path points from the
                coordinator. When provided (and non-empty), used instead of
                decoding RoadData from the current snapshot. An empty list
                means "no path" (robot idle/docked).

        Returns:
            Tuple of (PNG image bytes, calibration dict) or (None, None).
            Calibration dict contains min_x, min_y, scale, padding for
            mapping robot coordinates to image pixels.
        """
        # --- Decode SLAM grid ---
        slam_result = None
        slam_map = _parse_json_or_dict(slam_map_value)
        if slam_map and slam_map.get("MapData1"):
            slam_result = _decode_slam_grid(slam_map)

        # --- Decode road data ---
        path_points: list[tuple[int, int]] = []
        robot_pos: tuple[int, int] | None = None
        clean_direction = 0

        road_data = _parse_json_or_dict(road_data_value)
        if road_data:
            # Use accumulated path if provided, otherwise fall back to
            # the single RoadData chunk in the current snapshot
            if accumulated_path is not None:
                path_points = accumulated_path
            else:
                road_b64 = road_data.get("RoadData", "")
                if road_b64:
                    path_points = _decode_road_data(road_b64)

            cp_raw = road_data.get("CurrentPoint")
            if cp_raw is not None:
                try:
                    robot_pos = _decode_point_int(int(cp_raw))
                except (ValueError, TypeError):
                    pass

            try:
                clean_direction = int(road_data.get("CleanDirection", 0))
            except (ValueError, TypeError):
                pass

        # --- Decode charger ---
        charger_pos: tuple[int, int] | None = None
        charger_data = _parse_json_or_dict(charger_value)
        if charger_data:
            piont_raw = charger_data.get("Piont", charger_data.get("Point"))
            if piont_raw is not None:
                try:
                    charger_pos = _decode_point_int(int(piont_raw))
                except (ValueError, TypeError):
                    pass

        # --- Decide what to render ---
        if slam_result is None and len(path_points) < 2:
            # No grid and no meaningful path — nothing useful to render
            if charger_pos or robot_pos:
                # At least show robot/charger on a small canvas
                return self._render_minimal(robot_pos, charger_pos, clean_direction), None
            return None, None

        if slam_result is not None:
            return self._render_with_grid(
                slam_result, path_points, robot_pos, charger_pos, clean_direction
            )

        # Fallback: path-only rendering (no SLAM grid)
        return self._render_path_only(
            path_points, robot_pos, charger_pos, clean_direction
        ), None

    def _render_with_grid(
        self,
        slam_result: tuple[list[tuple[int, int, int]], int, int, int, int],
        path_points: list[tuple[int, int]],
        robot_pos: tuple[int, int] | None,
        charger_pos: tuple[int, int] | None,
        clean_direction: int,
    ) -> tuple[bytes, dict]:
        """Render SLAM grid with optional path/robot/charger overlay."""
        grid_points, min_x, min_y, max_x, max_y = slam_result

        # Include robot and charger in bounding box
        all_extra = [p for p in [robot_pos, charger_pos] if p]
        all_extra.extend(path_points)
        if all_extra:
            min_x = min(min_x, min(p[0] for p in all_extra))
            max_x = max(max_x, max(p[0] for p in all_extra))
            min_y = min(min_y, min(p[1] for p in all_extra))
            max_y = max(max_y, max(p[1] for p in all_extra))

        data_w = max_x - min_x + 1
        data_h = max_y - min_y + 1

        # Scale to fit IMAGE_MAX_SIZE
        available = IMAGE_MAX_SIZE - 2 * IMAGE_PADDING
        scale = min(available / max(data_w, 1), available / max(data_h, 1))
        # For grid data, use at least CELL_SIZE pixels per cell
        scale = max(scale, CELL_SIZE)
        scale = min(scale, 6.0)  # cap upscaling

        img_w = int(data_w * scale) + 2 * IMAGE_PADDING
        img_h = int(data_h * scale) + 2 * IMAGE_PADDING
        img_w = max(img_w, 100)
        img_h = max(img_h, 100)

        def to_pixel(x: int, y: int) -> tuple[int, int]:
            px = int((x - min_x) * scale) + IMAGE_PADDING
            py = int((y - min_y) * scale) + IMAGE_PADDING
            return (px, py)

        # Create image
        img = Image.new("RGBA", (img_w, img_h), COLOR_BACKGROUND)
        draw = ImageDraw.Draw(img)

        # Draw grid cells
        for gx, gy, room_type in grid_points:
            if room_type == 3:
                continue  # skip borders — leave transparent
            px, py = to_pixel(gx, gy)
            px2, py2 = to_pixel(gx + 1, gy + 1)
            if room_type == 1:
                fill = COLOR_OUTLINE
            else:
                fill = _room_color(room_type) + (220,)
            draw.rectangle(
                [px, py, px2, py2],
                fill=fill,
            )

        # Draw cleaning path
        if len(path_points) >= 2:
            pixel_path = [to_pixel(p[0], p[1]) for p in path_points]
            draw.line(pixel_path, fill=COLOR_PATH, width=PATH_WIDTH)

        # Draw charger
        if charger_pos:
            cx, cy = to_pixel(charger_pos[0], charger_pos[1])
            draw.ellipse(
                [cx - CHARGER_RADIUS, cy - CHARGER_RADIUS,
                 cx + CHARGER_RADIUS, cy + CHARGER_RADIUS],
                fill=COLOR_CHARGER,
                outline=(255, 255, 255),
                width=2,
            )

        # Draw robot with direction
        if robot_pos:
            rx, ry = to_pixel(robot_pos[0], robot_pos[1])
            draw.ellipse(
                [rx - ROBOT_RADIUS, ry - ROBOT_RADIUS,
                 rx + ROBOT_RADIUS, ry + ROBOT_RADIUS],
                fill=COLOR_ROBOT,
                outline=COLOR_ROBOT_OUTLINE,
                width=2,
            )
            angle_rad = math.radians(clean_direction)
            arrow_len = ROBOT_RADIUS + 6
            ax = rx + int(arrow_len * math.cos(angle_rad))
            ay = ry - int(arrow_len * math.sin(angle_rad))
            draw.line([(rx, ry), (ax, ay)], fill=COLOR_ROBOT_OUTLINE, width=2)

        output = io.BytesIO()
        img.save(output, format="PNG")

        calibration = {
            "min_x": min_x,
            "min_y": min_y,
            "max_x": max_x,
            "max_y": max_y,
            "scale": scale,
            "padding": IMAGE_PADDING,
        }
        return output.getvalue(), calibration

    def _render_path_only(
        self,
        path_points: list[tuple[int, int]],
        robot_pos: tuple[int, int] | None,
        charger_pos: tuple[int, int] | None,
        clean_direction: int,
    ) -> bytes:
        """Fallback: render cleaning path without SLAM grid."""
        all_points = list(path_points)
        if robot_pos:
            all_points.append(robot_pos)
        if charger_pos:
            all_points.append(charger_pos)

        if not all_points:
            return self._render_minimal(robot_pos, charger_pos, clean_direction)

        min_x = min(p[0] for p in all_points)
        max_x = max(p[0] for p in all_points)
        min_y = min(p[1] for p in all_points)
        max_y = max(p[1] for p in all_points)

        data_w = max(max_x - min_x, 1)
        data_h = max(max_y - min_y, 1)

        available = IMAGE_MAX_SIZE - 2 * IMAGE_PADDING
        scale = min(available / max(data_w, 1), available / max(data_h, 1))
        scale = min(scale, 4.0)

        img_w = int(data_w * scale) + 2 * IMAGE_PADDING
        img_h = int(data_h * scale) + 2 * IMAGE_PADDING
        img_w = max(img_w, 100)
        img_h = max(img_h, 100)

        def to_pixel(x: int, y: int) -> tuple[int, int]:
            px = int((x - min_x) * scale) + IMAGE_PADDING
            py = int((y - min_y) * scale) + IMAGE_PADDING
            return (px, py)

        img = Image.new("RGBA", (img_w, img_h), COLOR_BACKGROUND)
        draw = ImageDraw.Draw(img)

        if len(path_points) >= 2:
            pixel_path = [to_pixel(p[0], p[1]) for p in path_points]
            draw.line(pixel_path, fill=COLOR_PATH, width=PATH_WIDTH)

        if charger_pos:
            cx, cy = to_pixel(charger_pos[0], charger_pos[1])
            draw.ellipse(
                [cx - CHARGER_RADIUS, cy - CHARGER_RADIUS,
                 cx + CHARGER_RADIUS, cy + CHARGER_RADIUS],
                fill=COLOR_CHARGER,
                outline=(255, 255, 255),
                width=2,
            )

        if robot_pos:
            rx, ry = to_pixel(robot_pos[0], robot_pos[1])
            draw.ellipse(
                [rx - ROBOT_RADIUS, ry - ROBOT_RADIUS,
                 rx + ROBOT_RADIUS, ry + ROBOT_RADIUS],
                fill=COLOR_ROBOT,
                outline=COLOR_ROBOT_OUTLINE,
                width=2,
            )
            angle_rad = math.radians(clean_direction)
            arrow_len = ROBOT_RADIUS + 6
            ax = rx + int(arrow_len * math.cos(angle_rad))
            ay = ry - int(arrow_len * math.sin(angle_rad))
            draw.line([(rx, ry), (ax, ay)], fill=COLOR_ROBOT_OUTLINE, width=2)

        output = io.BytesIO()
        img.save(output, format="PNG")
        return output.getvalue()

    def _render_minimal(
        self,
        robot_pos: tuple[int, int] | None,
        charger_pos: tuple[int, int] | None,
        clean_direction: int,
    ) -> bytes:
        """Render a minimal image with just robot/charger dots."""
        img = Image.new("RGBA", (200, 200), COLOR_BACKGROUND)
        draw = ImageDraw.Draw(img)
        center = 100

        if charger_pos:
            draw.ellipse(
                [center - CHARGER_RADIUS, center - CHARGER_RADIUS,
                 center + CHARGER_RADIUS, center + CHARGER_RADIUS],
                fill=COLOR_CHARGER,
                outline=(255, 255, 255),
                width=2,
            )

        if robot_pos:
            draw.ellipse(
                [center - ROBOT_RADIUS, center - ROBOT_RADIUS,
                 center + ROBOT_RADIUS, center + ROBOT_RADIUS],
                fill=COLOR_ROBOT,
                outline=COLOR_ROBOT_OUTLINE,
                width=2,
            )

        output = io.BytesIO()
        img.save(output, format="PNG")
        return output.getvalue()
