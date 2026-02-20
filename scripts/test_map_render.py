#!/usr/bin/env python3
"""Test SLAM grid decoding and map rendering with real device data.

Fetches SaveMapDataX9, RealMapRoadData, and ChargerPoint from the API,
then renders a PNG image using the HA integration's map_renderer module.

Usage:
    python3 scripts/test_map_render.py
    # Saves output to test_map_output.png
"""

import importlib.util
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_FILE = PROJECT_DIR / "test_map_output.png"

# Import test_aliyun directly (it's in the same scripts/ directory)
from test_aliyun import AliyunIoTClient, load_tokens, DEFAULT_IOT_ID

# Import map_renderer without adding the HA integration dir to sys.path
# (that would shadow stdlib 'select' with HA's select.py)
_renderer_path = PROJECT_DIR / "ha_integration" / "custom_components" / "zaco" / "map_renderer.py"
spec = importlib.util.spec_from_file_location("map_renderer", _renderer_path)
map_renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(map_renderer)

MapRenderer = map_renderer.MapRenderer
_decode_slam_grid = map_renderer._decode_slam_grid


def main():
    # Load saved tokens
    saved = load_tokens()
    if not saved:
        print("No saved tokens. Run test_aliyun.py --username ... first.")
        sys.exit(1)

    client = AliyunIoTClient(verbose=False)
    client.iot_token = saved.get("iotToken")
    client.refresh_token = saved.get("refreshToken")
    client.identity_id = saved.get("identityId")

    saved_host = saved.get("host")
    if saved_host:
        client.host = saved_host

    # Auto-refresh if needed
    if saved.get("_iot_expired") and not saved.get("_refresh_expired"):
        if not client.refresh_session():
            print("Token refresh failed.")
            sys.exit(1)
    elif saved.get("_refresh_expired"):
        print("All tokens expired. Re-login required.")
        sys.exit(1)

    # Fetch map-related properties
    map_props = [
        "SaveMapDataX9_1", "SaveMapDataX9_2", "SaveMapDataX9_3",
        "RealMapRoadData", "ChargerPoint",
        "SaveMap", "MapRoomInfo1", "MapRoomInfo2", "MapRoomInfo3",
    ]

    print("\nFetching map properties...")
    data = client.get_properties(DEFAULT_IOT_ID, map_props)
    if not data:
        print("Failed to fetch properties.")
        sys.exit(1)

    # Determine active map slot
    save_map_raw = data.get("SaveMap", {})
    save_map_val = save_map_raw.get("value", save_map_raw) if isinstance(save_map_raw, dict) else save_map_raw
    if isinstance(save_map_val, str):
        try:
            save_map_val = json.loads(save_map_val)
        except (json.JSONDecodeError, ValueError):
            pass

    selected_map_id = None
    if isinstance(save_map_val, dict):
        selected_map_id = save_map_val.get("SelectedMapId")
    print(f"\nSelectedMapId: {selected_map_id}")

    # Find the SaveMapDataX9 slot whose MapId matches SelectedMapId
    slam_map_val = None
    active_slot = None
    for slot in [1, 2, 3]:
        key = f"SaveMapDataX9_{slot}"
        raw = data.get(key, {})
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        if not isinstance(val, dict) or not val.get("MapData1"):
            continue
        map_id = val.get("MapId")
        print(f"\n  {key}: MapId={map_id}")
        if selected_map_id is not None and map_id == selected_map_id:
            slam_map_val = val
            active_slot = slot
            break
        if slam_map_val is None:
            slam_map_val = val
            active_slot = slot

    if slam_map_val:
        print(f"\nUsing SaveMapDataX9_{active_slot} (MapId={slam_map_val.get('MapId')})")
        for k in sorted(slam_map_val.keys()):
            v = slam_map_val[k]
            if isinstance(v, str) and len(v) > 50:
                print(f"  {k}: {len(v)} chars (base64)")
            else:
                print(f"  {k}: {v}")

    if slam_map_val is None:
        print("\nNo SaveMapDataX9 data found!")
    else:
        # Test the decoder directly
        print("\nDecoding SLAM grid...")
        result = _decode_slam_grid(slam_map_val)
        if result:
            grid_points, min_x, min_y, max_x, max_y = result
            print(f"  Grid points: {len(grid_points)}")
            print(f"  Bounding box: ({min_x}, {min_y}) to ({max_x}, {max_y})")
            print(f"  Size: {max_x - min_x + 1} x {max_y - min_y + 1}")

            # Count cells by room type
            room_counts = {}
            for _, _, rt in grid_points:
                room_counts[rt] = room_counts.get(rt, 0) + 1
            print(f"  Room types: {dict(sorted(room_counts.items()))}")
        else:
            print("  FAILED to decode grid!")

    # Extract road data and charger
    road_raw = data.get("RealMapRoadData", {})
    road_val = road_raw.get("value", road_raw) if isinstance(road_raw, dict) else road_raw

    charger_raw = data.get("ChargerPoint", {})
    charger_val = charger_raw.get("value", charger_raw) if isinstance(charger_raw, dict) else charger_raw

    print(f"\nRealMapRoadData: {road_val}")
    print(f"ChargerPoint: {charger_val}")

    # Render the map
    print("\nRendering map...")
    renderer = MapRenderer()
    image_bytes, calibration = renderer.render(road_val, charger_val, slam_map_val)

    if image_bytes:
        OUTPUT_FILE.write_bytes(image_bytes)
        print(f"\nSaved map image to {OUTPUT_FILE} ({len(image_bytes)} bytes)")
        if calibration:
            print(f"Calibration: {calibration}")
        print("Open it to verify the rendering!")
    else:
        print("\nRenderer returned None — no image generated")


if __name__ == "__main__":
    main()
