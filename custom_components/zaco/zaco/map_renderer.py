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

from PIL import Image, ImageDraw, ImageFont

_LOGGER = logging.getLogger(__name__)

# Room colors — 16-color palette from the ZACO app (colors.xml pt_1..pt_16).
# Alternates warm/cool hues so adjacent rooms are visually distinct.
ROOM_COLORS = [
    (155, 206, 246),  # pt_1  light blue
    (254, 183, 166),  # pt_2  light salmon
    (122, 221, 228),  # pt_3  cyan
    (255, 238, 181),  # pt_4  pale yellow
    (152, 160, 233),  # pt_5  periwinkle
    (255, 209, 168),  # pt_6  peach
    (197, 162, 236),  # pt_7  lavender
    (129, 235, 172),  # pt_8  light green
    (79, 176, 249),   # pt_9  bright blue
    (254, 114, 81),   # pt_10 orange-red
    (65, 175, 183),   # pt_11 teal
    (248, 209, 80),   # pt_12 golden
    (115, 120, 167),  # pt_13 muted purple
    (251, 168, 60),   # pt_14 orange
    (139, 115, 100),  # pt_15 brown
    (107, 114, 132),  # pt_16 slate
]

# General colors
COLOR_BACKGROUND = (0, 0, 0, 0)
COLOR_PATH = (255, 255, 255, 200)
COLOR_ROBOT = (70, 130, 200)
COLOR_CHARGER = (60, 170, 80)
COLOR_ROBOT_OUTLINE = (210, 220, 230)
COLOR_OUTLINE = (40, 40, 40, 220)  # room outline / wall gap cells

# Rendering constants
IMAGE_MAX_SIZE = 800
IMAGE_PADDING = 30
ROBOT_RADIUS = 12
CHARGER_RADIUS = 10
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
            _LOGGER.debug("SLAM: failed to decode MapData%d base64", i)
            continue

    _LOGGER.debug("SLAM: decoded %d bytes from MapData chunks", len(all_bytes))

    if len(all_bytes) < 9:
        _LOGGER.debug("SLAM: too few bytes (%d), need at least 9", len(all_bytes))
        return None

    # Header: byte 0 = type, bytes 2-3 = originX, bytes 4-5 = originY,
    # bytes 6-7 = gridHeight
    origin_x = _bytes_to_int16(all_bytes[2], all_bytes[3])
    origin_y = _bytes_to_int16(all_bytes[4], all_bytes[5])
    grid_height = _bytes_to_int16(all_bytes[6], all_bytes[7])

    _LOGGER.debug(
        "SLAM: origin=(%d,%d), gridHeight=%d", origin_x, origin_y, grid_height,
    )

    if grid_height <= 0:
        _LOGGER.debug("SLAM: invalid gridHeight %d", grid_height)
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
        _LOGGER.debug("SLAM: no grid points decoded")
        return None

    min_x = min(p[0] for p in grid_points)
    max_x = max(p[0] for p in grid_points)
    min_y = min(p[1] for p in grid_points)
    max_y = max(p[1] for p in grid_points)

    _LOGGER.debug(
        "SLAM: %d grid points, bbox=(%d,%d)-(%d,%d)",
        len(grid_points), min_x, min_y, max_x, max_y,
    )

    return grid_points, min_x, min_y, max_x, max_y


# ---------------------------------------------------------------------------
# Road data decoder (from RealMapRoadData)
# ---------------------------------------------------------------------------

def _decode_road_data(b64_string: str) -> list[tuple[int, int]]:
    """Decode base64 RoadData into a list of (x, y) coordinate pairs."""
    try:
        raw = base64.b64decode(b64_string)
    except Exception:
        _LOGGER.debug("RoadData: failed to decode base64 (%d chars)", len(b64_string))
        return []

    points: list[tuple[int, int]] = []
    for i in range(len(raw) // 4):
        offset = i * 4
        if offset + 3 >= len(raw):
            break
        x = _bytes_to_int16(raw[offset], raw[offset + 1])
        y = -_bytes_to_int16(raw[offset + 2], raw[offset + 3])
        points.append((x, y))

    _LOGGER.debug("RoadData: decoded %d points from %d bytes", len(points), len(raw))
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

def _room_color(
    room_type: int,
    partition_to_bitmask: dict[int, int] | None = None,
) -> tuple[int, int, int]:
    """Get RGB color for a room partition.

    When *partition_to_bitmask* is available the bitmask ID (a power-of-2)
    is converted to a bit position (0, 1, 2, …) which gives every room a
    guaranteed-unique palette index.  Without the mapping we fall back to
    the raw SLAM partition ID.
    """
    if room_type <= 0:
        return (0, 0, 0)
    if partition_to_bitmask and room_type in partition_to_bitmask:
        bitmask_id = partition_to_bitmask[room_type]
        idx = (bitmask_id.bit_length() - 1) % len(ROOM_COLORS)
    else:
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
        partition_to_bitmask: dict[int, int] | None = None,
        stats_text: str | None = None,
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
            partition_to_bitmask: SLAM partition ID → room bitmask ID mapping
                from MapState, used for stable per-room color assignment.
            stats_text: Optional text (e.g. "5 min | 3.2 m²") to overlay
                at the bottom of the image.

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

        # Use accumulated path if provided (even if empty — empty means
        # "no path", which is distinct from None meaning "use RoadData").
        if accumulated_path is not None:
            path_points = accumulated_path

        road_data = _parse_json_or_dict(road_data_value)
        if road_data:
            # Fall back to RoadData only when no accumulated path was given
            if not path_points and accumulated_path is None:
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

        _LOGGER.debug(
            "Render: slam=%s, path=%d pts, robot=%s, charger=%s",
            slam_result is not None, len(path_points),
            robot_pos is not None, charger_pos is not None,
        )

        # --- Decide what to render ---
        if slam_result is None and len(path_points) < 2:
            # No grid and no meaningful path — nothing useful to render
            if charger_pos or robot_pos:
                # At least show robot/charger on a small canvas
                _LOGGER.debug("Render: minimal (robot/charger only)")
                return self._render_minimal(robot_pos, charger_pos, clean_direction), None
            _LOGGER.debug("Render: nothing to render")
            return None, None

        if slam_result is not None:
            _LOGGER.debug("Render: full grid render")
            return self._render_with_grid(
                slam_result, path_points, robot_pos, charger_pos,
                clean_direction, partition_to_bitmask, stats_text,
            )

        # Fallback: path-only rendering (no SLAM grid)
        _LOGGER.debug("Render: path-only render")
        return self._render_path_only(
            path_points, robot_pos, charger_pos, clean_direction, stats_text,
        ), None

    # ------------------------------------------------------------------
    # Icon drawing helpers
    # ------------------------------------------------------------------

    def _draw_robot(
        self,
        draw: ImageDraw.ImageDraw,
        cx: int,
        cy: int,
        heading_deg: float,
        radius: int = ROBOT_RADIUS,
    ) -> None:
        """Draw a stylized robot vacuum icon at (cx, cy) with given heading.

        The icon consists of:
        - Round body with outline
        - LiDAR turret (small circle toward heading)
        - Bin cover line across the back
        - Button dot near the front
        All sub-features scale proportionally to *radius*.
        """
        r = radius
        s = r / 10.0  # scale factor
        angle = math.radians(heading_deg)

        # 1. Main body
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=COLOR_ROBOT,
            outline=COLOR_ROBOT_OUTLINE,
            width=max(1, int(s * 1.5)),
        )

        # 2. Bin cover: line across the back of the robot
        back_angle = angle + math.pi
        spread = math.radians(76)  # how wide the cover spans
        r_bin = int(s * 8.5)
        x1 = cx + r_bin * math.cos(back_angle + spread)
        y1 = cy - r_bin * math.sin(back_angle + spread)
        x2 = cx + r_bin * math.cos(back_angle - spread)
        y2 = cy - r_bin * math.sin(back_angle - spread)
        draw.line(
            [(int(x1), int(y1)), (int(x2), int(y2))],
            fill=COLOR_ROBOT_OUTLINE,
            width=max(1, int(s)),
        )

        # 3. LiDAR turret: small circle offset toward heading
        lidar_dist = int(s * 3)
        lidar_r = max(2, int(s * 3))
        lx = cx + int(lidar_dist * math.cos(angle))
        ly = cy - int(lidar_dist * math.sin(angle))
        draw.ellipse(
            [lx - lidar_r, ly - lidar_r, lx + lidar_r, ly + lidar_r],
            fill=COLOR_ROBOT,
            outline=COLOR_ROBOT_OUTLINE,
            width=max(1, int(s)),
        )

        # 4. Button dot near the front
        btn_dist = int(s * 6)
        btn_r = max(1, int(s * 1.5))
        bx = cx + int(btn_dist * math.cos(angle))
        by = cy - int(btn_dist * math.sin(angle))
        btn_color = (
            (COLOR_ROBOT_OUTLINE[0] + COLOR_ROBOT[0]) // 2,
            (COLOR_ROBOT_OUTLINE[1] + COLOR_ROBOT[1]) // 2,
            (COLOR_ROBOT_OUTLINE[2] + COLOR_ROBOT[2]) // 2,
        )
        draw.ellipse(
            [bx - btn_r, by - btn_r, bx + btn_r, by + btn_r],
            fill=btn_color,
        )

    def _draw_charger(
        self,
        draw: ImageDraw.ImageDraw,
        cx: int,
        cy: int,
        radius: int = CHARGER_RADIUS,
    ) -> None:
        """Draw a charger/dock icon at (cx, cy).

        Green circle with a lightning bolt inside.
        """
        r = radius
        s = r / 8.0

        # Base circle
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=COLOR_CHARGER,
            outline=COLOR_ROBOT_OUTLINE,
            width=2,
        )

        # Lightning bolt (4-point zigzag polyline)
        bolt = [
            (cx + int(1 * s), cy - int(4 * s)),
            (cx - int(2 * s), cy + int(0.5 * s)),
            (cx + int(1 * s), cy - int(0.5 * s)),
            (cx - int(1 * s), cy + int(4 * s)),
        ]
        draw.line(bolt, fill=COLOR_ROBOT_OUTLINE, width=max(1, int(s * 1.5)))

    @staticmethod
    def _draw_stats_overlay(
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        text: str,
    ) -> None:
        """Draw a stats text with compact background at the bottom of the image."""
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
        except (OSError, IOError):
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        margin_x = 12
        margin_y = 8
        bg_w = text_w + 2 * margin_x
        bg_h = text_h + 2 * margin_y
        bg_x = (img.width - bg_w) // 2
        bg_y = img.height - bg_h - 8  # 8px from bottom edge
        draw.rectangle(
            [bg_x, bg_y, bg_x + bg_w, bg_y + bg_h],
            fill=(0, 0, 0, 128),
        )
        text_x = bg_x + margin_x
        text_y = bg_y + margin_y
        draw.text((text_x, text_y), text, fill=(255, 255, 255, 230), font=font)

    def _render_with_grid(
        self,
        slam_result: tuple[list[tuple[int, int, int]], int, int, int, int],
        path_points: list[tuple[int, int]],
        robot_pos: tuple[int, int] | None,
        charger_pos: tuple[int, int] | None,
        clean_direction: int,
        partition_to_bitmask: dict[int, int] | None = None,
        stats_text: str | None = None,
    ) -> tuple[bytes, dict]:
        """Render SLAM grid with optional path/robot/charger overlay."""
        grid_points, min_x, min_y, max_x, max_y = slam_result

        # Include robot, charger, and path in bounding box — but only if
        # they're within a reasonable margin of the SLAM grid.  Garbage
        # sentinel values (e.g. y=-19980 when docked) must not blow up
        # the bbox, which would cause a multi-GB Pillow allocation.
        grid_w = max_x - min_x
        grid_h = max_y - min_y
        margin = max(grid_w, grid_h, 50)  # allow up to 1x grid size outside
        sane_min_x, sane_max_x = min_x - margin, max_x + margin
        sane_min_y, sane_max_y = min_y - margin, max_y + margin

        all_extra = [p for p in [robot_pos, charger_pos] if p]
        all_extra.extend(path_points)
        for px, py in all_extra:
            if sane_min_x <= px <= sane_max_x and sane_min_y <= py <= sane_max_y:
                min_x = min(min_x, px)
                max_x = max(max_x, px)
                min_y = min(min_y, py)
                max_y = max(max_y, py)
            else:
                _LOGGER.debug(
                    "Render: ignoring out-of-range coord (%d,%d), "
                    "grid bbox=(%d,%d)-(%d,%d)",
                    px, py, sane_min_x, sane_min_y, sane_max_x, sane_max_y,
                )

        data_w = max_x - min_x + 1
        data_h = max_y - min_y + 1

        # Sanity check: reject absurdly large bounding boxes (garbage coords)
        if data_w > 2000 or data_h > 2000:
            _LOGGER.warning(
                "Render: bounding box too large (%dx%d), clamping to 2000. "
                "Possible garbage coordinates in path/robot/charger data.",
                data_w, data_h,
            )
            data_w = min(data_w, 2000)
            data_h = min(data_h, 2000)
            max_x = min_x + data_w - 1
            max_y = min_y + data_h - 1

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

        _LOGGER.debug(
            "Render grid: %d cells, %d path pts, bbox=(%d,%d)-(%d,%d), "
            "img=%dx%d, scale=%.2f",
            len(grid_points), len(path_points),
            min_x, min_y, max_x, max_y, img_w, img_h, scale,
        )

        def to_pixel(x: int, y: int) -> tuple[int, int]:
            px = int((x - min_x) * scale) + IMAGE_PADDING
            py = int((y - min_y) * scale) + IMAGE_PADDING
            return (px, py)

        # Create image
        try:
            img = Image.new("RGBA", (img_w, img_h), COLOR_BACKGROUND)
        except Exception:
            _LOGGER.exception(
                "Render: failed to create %dx%d image", img_w, img_h,
            )
            return b"", {}
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
                fill = _room_color(room_type, partition_to_bitmask) + (220,)
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
            self._draw_charger(draw, cx, cy)

        # Draw robot
        if robot_pos:
            rx, ry = to_pixel(robot_pos[0], robot_pos[1])
            self._draw_robot(draw, rx, ry, clean_direction)

        # Draw stats overlay
        if stats_text:
            self._draw_stats_overlay(img, draw, stats_text)

        output = io.BytesIO()
        try:
            img.save(output, format="PNG")
            result = output.getvalue()
        finally:
            output.close()
            img.close()

        _LOGGER.debug("Render grid: produced %d bytes PNG", len(result))

        calibration = {
            "min_x": min_x,
            "min_y": min_y,
            "max_x": max_x,
            "max_y": max_y,
            "scale": scale,
            "padding": IMAGE_PADDING,
        }
        return result, calibration

    def _render_path_only(
        self,
        path_points: list[tuple[int, int]],
        robot_pos: tuple[int, int] | None,
        charger_pos: tuple[int, int] | None,
        clean_direction: int,
        stats_text: str | None = None,
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

        # Sanity check: clamp absurdly large bounding boxes
        if data_w > 2000 or data_h > 2000:
            _LOGGER.warning(
                "Render path-only: bounding box too large (%dx%d), clamping",
                data_w, data_h,
            )
            data_w = min(data_w, 2000)
            data_h = min(data_h, 2000)

        available = IMAGE_MAX_SIZE - 2 * IMAGE_PADDING
        scale = min(available / max(data_w, 1), available / max(data_h, 1))
        scale = min(scale, 4.0)

        img_w = int(data_w * scale) + 2 * IMAGE_PADDING
        img_h = int(data_h * scale) + 2 * IMAGE_PADDING
        img_w = max(img_w, 100)
        img_h = max(img_h, 100)

        _LOGGER.debug(
            "Render path-only: %d pts, img=%dx%d, scale=%.2f",
            len(path_points), img_w, img_h, scale,
        )

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
            self._draw_charger(draw, cx, cy)

        if robot_pos:
            rx, ry = to_pixel(robot_pos[0], robot_pos[1])
            self._draw_robot(draw, rx, ry, clean_direction)

        if stats_text:
            self._draw_stats_overlay(img, draw, stats_text)

        output = io.BytesIO()
        try:
            img.save(output, format="PNG")
            return output.getvalue()
        finally:
            output.close()
            img.close()

    def _render_minimal(
        self,
        robot_pos: tuple[int, int] | None,
        charger_pos: tuple[int, int] | None,
        clean_direction: int,
    ) -> bytes:
        """Render a minimal image with just robot/charger dots."""
        _LOGGER.debug("Render minimal: robot=%s, charger=%s", robot_pos, charger_pos)
        img = Image.new("RGBA", (200, 200), COLOR_BACKGROUND)
        draw = ImageDraw.Draw(img)
        center = 100

        if charger_pos:
            self._draw_charger(draw, center, center)

        if robot_pos:
            self._draw_robot(draw, center, center, clean_direction)

        output = io.BytesIO()
        try:
            img.save(output, format="PNG")
            return output.getvalue()
        finally:
            output.close()
            img.close()
