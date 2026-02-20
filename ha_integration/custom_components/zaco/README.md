_# ZACO Robot Vacuum - Home Assistant Integration

Custom Home Assistant integration for ZACO robot vacuums (A10 and other models based on the iRobotics/3irobotix platform). Communicates via the Aliyun IoT Living Platform cloud API — the same backend used by the official ZACOHome app.

## Installation

1. Copy the `custom_components/zaco/` folder into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings > Devices & Services > Add Integration** and search for **ZACO**.

## Configuration

During setup you will be prompted for:

| Field | Description |
|-------|-------------|
| **Email** | The email address you use to log in to the ZACOHome app. |
| **Password** | Your ZACOHome app password. |

The integration automatically detects your account region and discovers all ZACO devices linked to your account. If you have multiple devices, you will be asked to select which one to configure.

Each device can be added as a separate integration entry.

---

## Entities

### Vacuum

The main entity. Appears with your device name (e.g. "Friday").

**States:**

| State | Meaning |
|-------|---------|
| Cleaning | Robot is actively cleaning (auto, room, or edge mode) |
| Paused | Cleaning is paused mid-run |
| Returning | Robot is travelling back to the charging dock |
| Docked | Robot is on the charging dock |
| Idle | Robot is not on the dock and not cleaning |
| Error | Robot has encountered an error |

**Supported actions:**

| Action | Description |
|--------|-------------|
| Start | Start cleaning. If rooms are selected via room switches, cleans only those rooms with the configured number of passes. Otherwise starts a full auto-clean using the saved map. |
| Stop | Stop cleaning and enter standby |
| Pause | Pause the current cleaning run |
| Return to dock | Send the robot back to the charging station |
| Locate | Make the robot beep so you can find it |

**Extra state attributes:**

| Attribute | Description |
|-----------|-------------|
| `work_mode` | Raw WorkMode value from the device firmware |
| `fault` | Numeric fault code |
| `water_level` | Current water tank level (Off / Low / Medium / High) |
| `available_rooms` | List of room names discovered from the saved map |

---

### Sensors

Battery level is displayed natively on the vacuum entity card (no separate sensor needed).

| Sensor | Unit | Description |
|--------|------|-------------|
| Cleaning Time | min | Duration of the current or last cleaning run |
| Cleaned Area | m^2 | Area covered in the current or last cleaning run |
| Current Room | — | Name of the room the robot is currently in (only available while cleaning) |
| Error Code | — | Numeric error code from the device |
| Filter Life | % | Remaining HEPA filter life |
| Main Brush Life | % | Remaining main brush life |
| Side Brush Life | % | Remaining side brush life |

The consumable sensors (filter, main brush, side brush) show the remaining life percentage. Replace the part when it reaches 0%.

---

### Camera (Map)

Displays a live floor map rendered as a PNG image with transparent background. The map shows:

- **Floor plan** — Room boundaries and partitions from the saved SLAM map, with dark outlines
- **Robot position** — Current location of the robot (updates every 3 seconds during cleaning)
- **Charger position** — Location of the charging dock
- **Cleaning path** — Trail showing where the robot has cleaned during the current run

The map updates every 3 seconds while cleaning and every 30 seconds when idle.

The camera entity also exposes a `calibration_points` attribute for use with the [xiaomi-vacuum-map-card](#map-card). This allows the card to translate pixel positions on the map image to robot coordinates automatically.

---

### Number Controls

| Entity | Range | Step | Description |
|--------|-------|------|-------------|
| Suction Power | 1-100% | 1 | Fan suction power level |
| Side Brush Speed | 1-100% | 1 | Side brush rotation speed |
| Cleaning Passes | 1-3 | 1 | Number of passes for room cleaning (used when starting with rooms selected, or with the `zaco.start` service) |

---

### Select Controls

| Entity | Options | Description |
|--------|---------|-------------|
| Water Level | Off, Low, Medium, High | Water flow rate for mopping |

---

### Buttons

| Button | Description |
|--------|-------------|
| Spot Clean | Start spot cleaning at the robot's current location |
| Edge Clean | Start edge/wall-follow cleaning mode |

These are also available as services (`zaco.spot_clean`, `zaco.edge_clean`) for use in automations.

---

### Room Switches

One toggle switch is created for each room discovered from the saved map (e.g. "Bedroom", "Kitchen", "Living Room"). These switches select which rooms to include in the next cleaning run.

**Room cleaning workflow:**

1. Toggle **on** the rooms you want to clean.
2. Optionally adjust **Cleaning Passes** (1-3).
3. Press **Start** on the vacuum card (or call `vacuum.start`).
4. The robot starts cleaning only the selected rooms.
5. Room selections automatically reset when cleaning finishes.

If no rooms are selected, Start performs a full auto-clean of the entire map.

---

## Services

### `zaco.start`

Unified cleaning service. Supports three modes depending on which optional parameters are provided:

1. **Zone cleaning** — provide `zone` (takes priority over `rooms`)
2. **Room cleaning** — provide `rooms` (list of room names)
3. **Full auto-clean** — no `zone` or `rooms` provided

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entity_id` | string | Yes | The vacuum entity ID (e.g. `vacuum.friday`) |
| `rooms` | list of strings | No | Room names or numeric bitmask IDs to clean (e.g. `["Büro"]` or `["2"]`) |
| `zone` | list of numbers | No | Rectangle as `[x1, y1, x2, y2]` in robot coordinates. Takes priority over `rooms`. |
| `passes` | integer | No | Number of cleaning passes (1-3, default: 1) |

**Examples:**

Full auto-clean:

```yaml
service: zaco.start
data:
  entity_id: vacuum.friday
```

Room cleaning:

```yaml
service: zaco.start
data:
  entity_id: vacuum.friday
  rooms:
    - Bedroom
    - Kitchen
  passes: 2
```

Zone cleaning:

```yaml
service: zaco.start
data:
  entity_id: vacuum.friday
  zone: [100, -50, 300, 100]
  passes: 1
```

If a room name doesn't match any known room, the service raises an error listing the available room names.

### `zaco.spot_clean`

Start spot cleaning at the robot's current location.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entity_id` | string | Yes | The vacuum entity ID |

**Example:**

```yaml
service: zaco.spot_clean
data:
  entity_id: vacuum.friday
```

### `zaco.edge_clean`

Start edge/wall-follow cleaning. Optionally specify a room — the robot will first navigate to the room center, then start edge cleaning from there.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entity_id` | string | Yes | The vacuum entity ID |
| `room` | string | No | Room name to navigate to before starting edge clean. Must match a room from the saved map. |

**Examples:**

Immediate edge clean (from current position):

```yaml
service: zaco.edge_clean
data:
  entity_id: vacuum.friday
```

Edge clean in a specific room (navigate first, then edge clean):

```yaml
service: zaco.edge_clean
data:
  entity_id: vacuum.friday
  room: Schlafzimmer
```

When `room` is provided, the robot uses PointToGo to navigate to the room center, then automatically switches to edge cleaning mode once it arrives. The service call returns immediately — the mode switch happens in the background (typically 30-120 seconds depending on distance).

### `zaco.goto`

Send the robot to a specific point on the map. The robot will navigate to the target, do a spot clean there, and automatically return to the charging dock. This is the PointToGo feature from the ZACOHome app.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entity_id` | string | Yes | The vacuum entity ID |
| `x` | number | Yes | X coordinate in robot units |
| `y` | number | Yes | Y coordinate in robot units |

**Example:**

```yaml
service: zaco.goto
data:
  entity_id: vacuum.friday
  x: 150
  y: -30
```

The coordinates use the same coordinate system as the map. When used with the [xiaomi-vacuum-map-card](#map-card), the card translates pixel positions to robot coordinates automatically via the calibration points.

---

## Advanced: `send_command`

The vacuum entity supports `vacuum.send_command` for advanced use cases.

### Set arbitrary device properties

```yaml
service: vacuum.send_command
target:
  entity_id: vacuum.friday
data:
  command: set_properties
  params:
    FanPower: 80
```

### Clean specific rooms by ID

```yaml
service: vacuum.send_command
target:
  entity_id: vacuum.friday
data:
  command: clean_rooms
  params:
    room_ids: [1, 4, 32]
    passes: 2
```

Room IDs are bitmask values visible in the `available_rooms` attribute.

### Start edge cleaning

```yaml
service: vacuum.send_command
target:
  entity_id: vacuum.friday
data:
  command: edge_clean
```

---

## Polling Behavior

The integration uses two-tier polling to balance responsiveness with API efficiency:

| Condition | Interval | Properties polled |
|-----------|----------|-------------------|
| **Cleaning / Paused / Returning** | 3 seconds | Lightweight set: WorkMode, battery, robot position, cleaning stats, error codes |
| **Idle / Docked** | 30 seconds | Full set: all of the above plus SLAM map data, room info, consumable status |

During active cleaning, the robot's position, battery level, cleaning time, and cleaned area update every 3 seconds. The full SLAM map grid and room data are refreshed every 30 seconds (the floor plan itself doesn't change during a cleaning run, only the robot's position on it does).

---

## Map Card

The integration works with [xiaomi-vacuum-map-card](https://github.com/PiotrMachowski/lovelace-xiaomi-vacuum-map-card) (install via HACS) for an interactive map with zone cleaning, room cleaning, go-to-point, sensor tiles, and controls — all in a single card.

The camera entity exposes two attributes that the card uses automatically:

- **`calibration_points`** — Translates pixel positions on the map image to robot coordinates. No manual calibration needed.
- **`rooms`** — Per-room polygon outlines in robot coordinates, enabling automatic room configuration (see [Setting up Room Clean mode](#setting-up-room-clean-mode) below).

### Example card configuration

Replace `friday` with your device name.

```yaml
type: vertical-stack
cards:
  - type: custom:xiaomi-vacuum-map-card
    map_source:
      camera: camera.friday
    calibration_source:
      camera: true
    entity: vacuum.friday
    vacuum_platform: send_command
    tiles:
      - entity: vacuum.friday
        attribute: battery_level
        icon: mdi:battery
        label: Battery
        unit: "%"
      - entity: sensor.friday_clean_time
        icon: mdi:clock-outline
        label: Time
        unit: min
      - entity: sensor.friday_clean_area
        icon: mdi:texture-box
        label: Area
        unit: m²
      - entity: sensor.friday_current_room
        icon: mdi:door-open
        label: Room
      - entity: sensor.friday_filter_life
        icon: mdi:air-filter
        label: Filter
        unit: "%"
      - entity: sensor.friday_main_brush_life
        icon: mdi:brush
        label: Main Brush
        unit: "%"
      - entity: sensor.friday_side_brush_life
        icon: mdi:pinwheel-outline
        label: Side Brush
        unit: "%"
      - entity: number.friday_suction_power
        icon: mdi:fan
        label: Suction
        unit: "%"
        tap_action:
          action: more-info
      - entity: number.friday_side_brush_speed
        icon: mdi:rotate-right
        label: Brush Speed
        unit: "%"
        tap_action:
          action: more-info
      - entity: number.friday_cleaning_passes
        icon: mdi:repeat
        label: Passes
        tap_action:
          action: more-info
    icons:
      - type: menu
        menu_id: water_level
        entity: select.friday_water_level
        available_values_attribute: options
        icon: mdi:water
        icon_mapping:
          "Off": mdi:water-off
          "Low": mdi:water-minus
          "Medium": mdi:water
          "High": mdi:water-plus
        tap_action:
          action: call-service
          service: select.select_option
          service_data:
            entity_id: select.friday_water_level
            option: "[[value]]"
    map_modes:
      - name: Room Clean
        icon: mdi:floor-plan
        selection_type: ROOM
        max_selections: 99
        repeats_type: INTERNAL
        max_repeats: 3
        service_call_schema:
          service: zaco.start
          service_data:
            entity_id: "[[entity_id]]"
            rooms: "[[selection]]"
            passes: "[[repeats]]"
        predefined_selections: []
      - name: Zone Clean
        icon: mdi:select-drag
        selection_type: MANUAL_RECTANGLE
        max_selections: 1
        repeats_type: INTERNAL
        max_repeats: 3
        service_call_schema:
          service: zaco.start
          service_data:
            entity_id: "[[entity_id]]"
            zone: "[[selection]]"
            passes: "[[repeats]]"
      - name: Go To Point
        icon: mdi:map-marker
        selection_type: MANUAL_POINT
        max_selections: 1
        service_call_schema:
          service: zaco.goto
          service_data:
            entity_id: "[[entity_id]]"
            x: "[[point_x]]"
            y: "[[point_y]]"

  # Room selection switches (adjust entity IDs to match your rooms)
  - type: entities
    entities:
      - entity: switch.friday_room_1
      - entity: switch.friday_room_2
      - entity: switch.friday_room_4
      - entity: switch.friday_room_8
```

The **tiles** row below the map shows battery, cleaning stats, consumable life, and current settings. Tapping the Suction, Brush Speed, or Passes tiles opens the entity's detail dialog where you can adjust the value with a slider. The **water level** icon on the map overlay lets you cycle through Off / Low / Medium / High directly.

The room switch entity IDs use the room's bitmask ID as suffix (1, 2, 4, 8, 16, ...). Check your actual entity IDs in **Settings > Devices & Services > ZACO**.

### Setting up Room Clean mode

The Room Clean mode requires `predefined_selections` so the card knows where each room is on the map. The integration provides these automatically via the camera's `rooms` attribute:

1. Add the card configuration above (the Room Clean mode starts with `predefined_selections: []`).
2. Open the card in the visual editor.
3. Click **"Generate rooms config"** — the card reads the `rooms` attribute and auto-creates room selections with outlines, labels, and icons.
4. Optionally adjust the generated room labels and icons in the YAML.

After generating, the `predefined_selections: []` will be replaced with entries like:

```yaml
    predefined_selections:
      - id: 1
        outline:
          - [-50, -120]
          - [80, -120]
          - [80, 30]
          - [-50, 30]
          - ...
        label:
          text: Schlafzimmer
          x: 15
          y: -45
          offset_y: 35
        icon:
          name: mdi:bed
          x: 15
          y: -45
      - id: 8
        outline:
          - [100, -80]
          - [200, -80]
          - [200, 20]
          - [100, 20]
          - ...
        label:
          text: Küche
          x: 150
          y: -30
          offset_y: 35
        icon:
          name: mdi:silverware-fork-knife
          x: 150
          y: -30
```

The `id` field contains the room's numeric bitmask ID (1, 2, 4, 8, ...), which `zaco.start` accepts in the `rooms` parameter. The `outline` contains the actual room shape as polygon points in robot coordinates. Tapping rooms on the map sends the selected IDs directly to the service.

---

## Troubleshooting

### "Could not determine account region"

The integration contacts Aliyun's region discovery service to find the correct API endpoint for your account. This can fail if the Aliyun cloud is temporarily unreachable. Try again after a few minutes.

### "Invalid email or password"

Make sure you're using the same credentials as the ZACOHome app (not a third-party account). The password is encrypted with RSA before being sent — there is no plain-text transmission.

### "No devices found"

Your ZACO vacuum must be set up and linked in the ZACOHome app first. The integration discovers devices through the same cloud API the app uses.

### "Authentication expired, please reconfigure"

The integration automatically refreshes its authentication tokens. If the refresh token itself expires (after 30 days of the integration being offline), you will need to reconfigure by entering your credentials again.

### Vacuum shows wrong state

The `work_mode` attribute on the vacuum entity shows the raw firmware WorkMode value. If the state doesn't match what you expect, check this value — it can help identify unrecognized firmware states. Please report any unmapped WorkMode values so they can be added.

### Map not showing

The map requires a saved map on the device. Run at least one full cleaning with map saving enabled in the ZACOHome app, then the integration will pick up the saved SLAM data._
