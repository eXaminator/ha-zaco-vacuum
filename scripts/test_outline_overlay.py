#!/usr/bin/env python3
"""Diagnostic: overlay room outlines on the rendered map image.

Fetches live data, renders the map PNG, computes room outlines (same as the
HA integration), then draws the outlines on top in two colors:

  GREEN = outline vertices transformed via the renderer's exact to_pixel()
  RED   = outline vertices transformed via the map card's affine
          (derived from 3 calibration points with 10-unit offset)

If green aligns with the image but red doesn't → calibration quantization bug.
If green doesn't align → the outline coordinates themselves are wrong.

Usage:
    python3 scripts/test_outline_overlay.py
"""

import base64
import importlib.util
import io
import json
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_FILE = PROJECT_DIR / "test_outline_overlay.png"

# Import test_aliyun
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_aliyun import AliyunIoTClient, load_tokens, parse_map_room_info

DEFAULT_IOT_ID = "KReBFAPbEXU5Yk31mDep000000"

# Import map_renderer without adding HA dir to sys.path (avoids shadowing stdlib)
_renderer_path = PROJECT_DIR / "ha_integration" / "custom_components" / "zaco" / "map_renderer.py"
spec = importlib.util.spec_from_file_location("map_renderer", _renderer_path)
map_renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(map_renderer)

MapRenderer = map_renderer.MapRenderer
_decode_slam_grid = map_renderer._decode_slam_grid
_bytes_to_int16 = map_renderer._bytes_to_int16
_parse_json_or_dict = map_renderer._parse_json_or_dict

# Import outline functions from coordinator.py
# We can't import the module directly (it imports homeassistant), so we
# copy the standalone functions here.
from collections import defaultdict


def _collect_boundary_edges(cells):
    edges = []
    for x, y in cells:
        if (x, y - 1) not in cells:
            edges.append(((x, y), (x + 1, y)))
        if (x + 1, y) not in cells:
            edges.append(((x + 1, y), (x + 1, y + 1)))
        if (x, y + 1) not in cells:
            edges.append(((x + 1, y + 1), (x, y + 1)))
        if (x - 1, y) not in cells:
            edges.append(((x, y + 1), (x, y)))
    return edges


def _chain_edges(edges):
    outgoing = defaultdict(list)
    for start, end in edges:
        outgoing[start].append(end)
    visited = set()
    polygons = []
    for start, end in edges:
        if (start, end) in visited:
            continue
        poly = [start]
        visited.add((start, end))
        current = end
        while current != start:
            poly.append(current)
            found = False
            for candidate in outgoing[current]:
                if (current, candidate) not in visited:
                    visited.add((current, candidate))
                    current = candidate
                    found = True
                    break
            if not found:
                break
        if len(poly) >= 3:
            polygons.append(poly)
    return polygons


def _simplify_polygon(polygon):
    n = len(polygon)
    if n < 3:
        return polygon
    result = []
    for i in range(n):
        prev = polygon[(i - 1) % n]
        curr = polygon[i]
        nxt = polygon[(i + 1) % n]
        if (prev[0] == curr[0] == nxt[0]) or (prev[1] == curr[1] == nxt[1]):
            continue
        result.append(curr)
    return result


def _polygon_area(polygon):
    n = len(polygon)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += polygon[i][0] * polygon[j][1]
        area -= polygon[j][0] * polygon[i][1]
    return abs(area) / 2.0


def parse_map_info_x9(info_map: dict):
    """Parse SaveMapDataInfoX9 → list of (bitmask_id, center_x, center_y)."""
    all_bytes = bytearray()
    for i in range(1, 8):
        chunk_b64 = info_map.get(f"MapInfo{i}", "")
        if not chunk_b64:
            continue
        all_bytes.extend(base64.b64decode(chunk_b64))

    if len(all_bytes) < 5:
        return []

    num_rooms = all_bytes[4] & 0xFF
    idx = 5
    rooms = []
    for _ in range(num_rooms):
        if idx + 14 > len(all_bytes):
            break
        bitmask_id = struct.unpack_from(">i", all_bytes, idx)[0]
        idx += 4
        cx = _bytes_to_int16(all_bytes[idx], all_bytes[idx + 1])
        idx += 2
        cy = -_bytes_to_int16(all_bytes[idx], all_bytes[idx + 1])
        idx += 2
        idx += 4  # surround
        if idx + 2 > len(all_bytes):
            break
        num_walls = (all_bytes[idx] << 8) | all_bytes[idx + 1]
        idx += 2
        idx += num_walls * 4  # skip wall data
        rooms.append((bitmask_id, cx, cy))
    return rooms


def compute_outlines(grid_lookup, partition_to_bitmask):
    """Replicate coordinator._compute_room_outlines()."""
    cells_by_room = {}
    for (x, y), pid in grid_lookup.items():
        if pid in (0, 3, 4):
            continue
        bid = partition_to_bitmask.get(pid)
        if bid is None:
            continue
        cells_by_room.setdefault(bid, set()).add((x, y))

    outlines = {}
    for bid, cells in cells_by_room.items():
        edges = _collect_boundary_edges(cells)
        if not edges:
            continue
        polygons = _chain_edges(edges)
        simplified = [_simplify_polygon(p) for p in polygons]
        simplified = [p for p in simplified if len(p) >= 3]
        if not simplified:
            continue
        simplified.sort(key=_polygon_area, reverse=True)
        outlines[bid] = simplified[0]
    return outlines


def main():
    # --- Connect and fetch data ---
    saved = load_tokens()
    if not saved:
        print("No saved tokens. Run test_aliyun.py --username ... first.")
        sys.exit(1)

    client = AliyunIoTClient(verbose=False)
    client.iot_token = saved.get("iotToken")
    client.refresh_token = saved.get("refreshToken")
    client.identity_id = saved.get("identityId")
    if saved.get("host"):
        client.host = saved["host"]

    if saved.get("_iot_expired") and not saved.get("_refresh_expired"):
        if not client.refresh_session():
            print("Token refresh failed.")
            sys.exit(1)

    props = [
        "SaveMap",
        "MapRoomInfo1", "MapRoomInfo2", "MapRoomInfo3",
        "SaveMapDataX9_1", "SaveMapDataX9_2", "SaveMapDataX9_3",
        "SaveMapDataInfoX9_1", "SaveMapDataInfoX9_2", "SaveMapDataInfoX9_3",
        "RealMapRoadData", "ChargerPoint",
    ]
    print("Fetching properties...")
    data = client.get_properties(DEFAULT_IOT_ID, props)
    if not data:
        print("Failed to fetch properties.")
        sys.exit(1)

    # --- Determine active map slot ---
    save_map_raw = data.get("SaveMap", {})
    save_map_val = save_map_raw.get("value", save_map_raw) if isinstance(save_map_raw, dict) else save_map_raw
    if isinstance(save_map_val, str):
        try:
            save_map_val = json.loads(save_map_val)
        except:
            pass
    selected_map_id = save_map_val.get("SelectedMapId") if isinstance(save_map_val, dict) else None
    print(f"SelectedMapId: {selected_map_id}")

    active_slot = None
    slam_val = None
    for i in range(1, 4):
        raw = data.get(f"SaveMapDataX9_{i}", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except:
                continue
        if isinstance(val, dict) and val.get("MapData1"):
            mid = val.get("MapId")
            if selected_map_id is not None and mid == selected_map_id:
                active_slot = i
                slam_val = val
                break
            if active_slot is None:
                active_slot = i
                slam_val = val

    if not slam_val:
        print("No SLAM data found!")
        sys.exit(1)
    print(f"Active slot: {active_slot}")

    # --- Parse rooms ---
    room_map = {}
    for i in range(1, 4):
        raw = data.get(f"MapRoomInfo{i}", {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if not val or not isinstance(val, str):
            continue
        map_id, rooms = parse_map_room_info(val)
        if rooms and (selected_map_id is None or map_id == selected_map_id):
            room_map = {name: rid for rid, name in rooms}
            break

    print(f"Rooms: {room_map}")

    # --- Decode SLAM grid ---
    result = _decode_slam_grid(slam_val)
    if not result:
        print("Failed to decode SLAM grid!")
        sys.exit(1)
    grid_points, grid_min_x, grid_min_y, grid_max_x, grid_max_y = result
    grid_lookup = {(x, y): rt for x, y, rt in grid_points}
    print(f"Grid: {len(grid_points)} cells, bounds ({grid_min_x},{grid_min_y}) to ({grid_max_x},{grid_max_y})")

    # --- Parse SaveMapDataInfoX9 for room centers ---
    info_raw = data.get(f"SaveMapDataInfoX9_{active_slot}", {})
    info_val = info_raw.get("value", info_raw) if isinstance(info_raw, dict) else info_raw
    if isinstance(info_val, str):
        try:
            info_val = json.loads(info_val)
        except:
            info_val = {}
    room_centers = parse_map_info_x9(info_val) if isinstance(info_val, dict) else []
    print(f"Room centers: {room_centers}")

    # --- Build partition→bitmask mapping ---
    partition_to_bitmask = {}
    for bitmask_id, cx, cy in room_centers:
        slam_pid = grid_lookup.get((cx, cy))
        if slam_pid and slam_pid not in (0, 3, 4):
            partition_to_bitmask[slam_pid] = bitmask_id
        else:
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    pid = grid_lookup.get((cx + dx, cy + dy))
                    if pid and pid not in (0, 3, 4):
                        partition_to_bitmask[pid] = bitmask_id
                        break
                else:
                    continue
                break
    print(f"Partition mapping: {partition_to_bitmask}")

    # --- Compute room outlines (same as coordinator) ---
    outlines = compute_outlines(grid_lookup, partition_to_bitmask)
    print(f"Outlines: { {k: len(v) for k, v in outlines.items()} }")

    # --- Render the map PNG ---
    road_raw = data.get("RealMapRoadData", {})
    road_val = road_raw.get("value", road_raw) if isinstance(road_raw, dict) else road_raw
    charger_raw = data.get("ChargerPoint", {})
    charger_val = charger_raw.get("value", charger_raw) if isinstance(charger_raw, dict) else charger_raw

    renderer = MapRenderer()
    image_bytes, calibration = renderer.render(road_val, charger_val, slam_val)
    if not image_bytes or not calibration:
        print("Render failed!")
        sys.exit(1)

    min_x = calibration["min_x"]
    min_y = calibration["min_y"]
    scale = calibration["scale"]
    pad = calibration["padding"]
    print(f"\nCalibration: min=({min_x},{min_y}), scale={scale}, pad={pad}")

    # --- Compute data extents (what camera.py would compute for max_x/max_y) ---
    # The renderer's bounding box after expansion:
    # We can derive it from the image size
    img = Image.open(io.BytesIO(image_bytes))
    img_w, img_h = img.size
    # img_w = int(data_w * scale) + 2*pad  →  data_w = (img_w - 2*pad) / scale
    data_w_approx = (img_w - 2 * pad) / scale
    data_h_approx = (img_h - 2 * pad) / scale
    max_x_approx = min_x + data_w_approx
    max_y_approx = min_y + data_h_approx
    print(f"Image: {img_w}x{img_h}, data extent ~{data_w_approx:.1f}x{data_h_approx:.1f}")
    print(f"Approx max: ({max_x_approx:.1f}, {max_y_approx:.1f})")

    # --- Define transforms ---

    # Transform A: renderer's exact to_pixel (ground truth for image)
    def to_pixel_exact(x, y):
        px = int((x - min_x) * scale) + pad
        py = int((y - min_y) * scale) + pad
        return (px, py)

    # Transform B (OLD): map card's affine with 10-unit offset
    p0_x = pad
    p0_y = pad
    p1_x_old = int(10 * scale) + pad
    p2_y_old = int(10 * scale) + pad
    card_scale_x_old = (p1_x_old - p0_x) / 10.0
    card_scale_y_old = (p2_y_old - p0_y) / 10.0

    print(f"\nActual scale: {scale}")
    print(f"OLD card scale (10-unit): {card_scale_x_old} (int(10*{scale:.4f})={int(10*scale)})")
    print(f"OLD scale error per unit: {abs(scale - card_scale_x_old):.6f}")
    max_drift_x_old = abs(scale - card_scale_x_old) * (grid_max_x - min_x + 1)
    max_drift_y_old = abs(scale - card_scale_y_old) * (grid_max_y - min_y + 1)
    print(f"OLD max drift: ({max_drift_x_old:.1f}, {max_drift_y_old:.1f}) pixels")

    # Transform C (FIXED): map card's affine with full-extent span
    span_x = max(grid_max_x - min_x, 10)
    span_y = max(grid_max_y - min_y, 10)
    p1_x_new = int(span_x * scale) + pad
    p2_y_new = int(span_y * scale) + pad
    card_scale_x_new = (p1_x_new - p0_x) / span_x
    card_scale_y_new = (p2_y_new - p0_y) / span_y

    print(f"\nFIXED card scale (span {span_x}/{span_y}): {card_scale_x_new:.6f} / {card_scale_y_new:.6f}")
    print(f"FIXED scale error per unit: {abs(scale - card_scale_x_new):.6f}")
    max_drift_x_new = abs(scale - card_scale_x_new) * (grid_max_x - min_x + 1)
    max_drift_y_new = abs(scale - card_scale_y_new) * (grid_max_y - min_y + 1)
    print(f"FIXED max drift: ({max_drift_x_new:.1f}, {max_drift_y_new:.1f}) pixels")

    def to_pixel_card_old(x, y):
        """OLD: map card affine with 10-unit offset."""
        px = (x - min_x) * card_scale_x_old + p0_x
        py = (y - min_y) * card_scale_y_old + p0_y
        return (px, py)

    def to_pixel_card_new(x, y):
        """FIXED: map card affine with full-extent span."""
        px = (x - min_x) * card_scale_x_new + p0_x
        py = (y - min_y) * card_scale_y_new + p0_y
        return (px, py)

    # --- Draw outlines on the image ---
    img = img.convert("RGBA")
    draw = ImageDraw.Draw(img)

    room_idx = 0
    for bid, outline in sorted(outlines.items()):
        name = None
        for rname, rid in room_map.items():
            if rid == bid:
                name = rname
                break
        room_idx += 1

        # Close the polygon
        closed = list(outline) + [outline[0]]

        # Green: exact to_pixel (ground truth)
        green_pts = [to_pixel_exact(x, y) for x, y in closed]
        draw.line(green_pts, fill=(0, 255, 0, 255), width=2)

        # Red: OLD card affine (10-unit offset — the bug)
        red_pts = [to_pixel_card_old(x, y) for x, y in closed]
        red_pts_int = [(int(round(px)), int(round(py))) for px, py in red_pts]
        draw.line(red_pts_int, fill=(255, 0, 0, 255), width=2)

        # Blue: FIXED card affine (full-extent span)
        blue_pts = [to_pixel_card_new(x, y) for x, y in closed]
        blue_pts_int = [(int(round(px)), int(round(py))) for px, py in blue_pts]
        draw.line(blue_pts_int, fill=(0, 100, 255, 255), width=2)

        # Label the room
        if name:
            center_x = sum(p[0] for p in outline) / len(outline)
            center_y = sum(p[1] for p in outline) / len(outline)
            label_px = to_pixel_exact(center_x, center_y)
            draw.text(label_px, f"{name}\n(bid={bid})", fill=(255, 255, 255, 255))

        # Print vertex comparison
        print(f"\nRoom {bid} ({name or '?'}): {len(outline)} vertices")
        vx, vy = outline[0]
        ex = to_pixel_exact(vx, vy)
        old_x, old_y = to_pixel_card_old(vx, vy)
        new_x, new_y = to_pixel_card_new(vx, vy)
        print(f"  v0: exact=({ex[0]},{ex[1]}), old=({old_x:.1f},{old_y:.1f}) Δ({old_x-ex[0]:.1f},{old_y-ex[1]:.1f}), fixed=({new_x:.1f},{new_y:.1f}) Δ({new_x-ex[0]:.1f},{new_y-ex[1]:.1f})")

    # --- Save ---
    output = io.BytesIO()
    img.save(output, format="PNG")
    OUTPUT_FILE.write_bytes(output.getvalue())
    print(f"\nSaved overlay to {OUTPUT_FILE}")
    print("Open it and check:")
    print("  GREEN = renderer's exact to_pixel (ground truth)")
    print("  RED   = OLD card affine (10-unit offset — the bug)")
    print("  BLUE  = FIXED card affine (full-extent span)")
    print("  Blue should overlap green. Red should be visibly shifted.")


if __name__ == "__main__":
    main()
