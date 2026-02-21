# ZACO A10 API & MQTT Research Findings

**Date**: 2026-02-21  
**Device**: ZACO A10 (Friday), iotId `KReBFAPbEXU5Yk31mDep000000`  
**Firmware**: SoftwareVer `50462720`, HardwareVer `263685`  
**Platform**: Aliyun IoT Living Platform, region `eu-central-1`  
**Battery**: 100% throughout all tests  
**Test duration**: ~13 minutes (798s), 13 phases  
**Research script**: `scripts/test_research.py`  
**Raw event log**: `docs/research_log_20260221_160919.json` (283 events)

---

## Table of Contents

1. [Property Inventory](#1-property-inventory)
2. [Settings Read/Write Behavior](#2-settings-readwrite-behavior)
3. [MQTT Push Behavior](#3-mqtt-push-behavior)
4. [Cleaning Modes & WorkMode Transitions](#4-cleaning-modes--workmode-transitions)
5. [REST API Timing & Latency](#5-rest-api-timing--latency)
6. [UploadDataControl (Fast Updates)](#6-uploaddatacontrol-fast-updates)
7. [SoundLocate](#7-soundlocate)
8. [Timeline API](#8-timeline-api)
9. [Schedules](#9-schedules)
10. [Device Info & Misc Properties](#10-device-info--misc-properties)
11. [Summary & Implications for Integration](#11-summary--implications-for-integration)

---

## 1. Property Inventory

Tested 64 properties from APK analysis against the live device. Properties were read in batches of 20 via the async API client.

### 1.1 Existing Properties (30)

These properties returned non-null values when read:

| Property | Value (at baseline) | Type | Notes |
|----------|-------------------|------|-------|
| WorkMode | 9 | int | Docked/idle state |
| BatteryState | 100.0 | float | Percentage, reported as float |
| FanPower | 1 | int | Range 0-100, was set to 1 for testing |
| WaterTankContrl | 0 | int | Range 0-2 (NOT 0-3, see below) |
| PowerSwitch | 0 | int | 0=off/idle, 1=running |
| PauseSwitch | 1 | int | 1 when idle/docked, 0 when actively cleaning |
| CleanType | 1 | int | Current clean type |
| BeepVolume | 100 | int | Range 0-100 |
| CarpetControl | 1 | int | 0=off, 1=on |
| ContinueCleanSwitch | 1 | int | Breakpoint resume, 0=off, 1=on |
| WiFiInfo | `{"RSSI":97,"IP":-1062700941,...}` | object | Contains RSSI, IP, SoftWareVer, MAC |
| InitStatus | 1 | int | Device initialized |
| RobotInfo | `{"RobotType":0,"SoftwareVer":50462720,"HardwareVer":263685}` | object | Device identity |
| AppRemind | 0 | int | App reminder status |
| PartsStatus | `{"FilterLife":0,"SideBrushLife":0,"MainBrushLife":0}` | object | All at 0 = needs reset |
| Schedule1-7 | (see Schedules section) | object | 7 schedule slots |
| SaveMap | `{"SelectedMapId":1734269369,"SaveMapId":"Z17ZuWde0uFnKjKz"}` | object | Active map reference |
| ChargerPoint | `{"Piont":-851971,"DisplaySwitch":1}` | object | Charger location, packed int |
| ForbiddenAreaData | (base64, 240 bytes) | string | 3 forbidden zones configured |
| VirtualWallData | (base64, all zeros) | string | No virtual walls set |
| VirtualWallEN | 1 | int | Virtual walls feature enabled |
| UploadDataControl | `{"Status":0,"ValidityTime":210}` | object | Fast upload disabled |
| CleanHistory | (complex object) | object | Last cleaning session data |
| CleanHistoryStartTime | 1771689066 | int | Unix timestamp of last clean start |

### 1.2 Missing/Null Properties (34)

These returned null — either not supported by this firmware or only populated during specific states:

| Category | Properties |
|----------|-----------|
| **Error reporting** | ErrorCode, Fault |
| **Cleaning stats (runtime)** | CleanLoop, CleanTime, CleanArea, VacWateState, CurrentMode |
| **Audio settings** | BeepType, BeepNoDisturb |
| **Motor/power settings** | SideBrushPower, MainBrushPower, MaxMode, PowerMode |
| **Advanced settings** | FunctionSwitch, WheelSpeed, CleaningEfficiency |
| **Device info (standalone)** | HardwareVer, SoftwareVer (available inside RobotInfo instead) |
| **Statistics** | TotalCleanedInfo, StatisticalData, Maintenance |
| **Clean settings** | NormalCleanSettings, CleanSettings_1, CleanSettings_2, CleanSettingsManager |
| **State** | ContinueCleanStatus, LongConnection |
| **Commands** | PointToGo, SoundLocate, CleanDirection |
| **Other** | BatteryStateInfo, RealTimeObjectInfo, DrawSlamDone, OTAInfo |

**Key observations**:
- `HardwareVer` and `SoftwareVer` don't exist as standalone properties — they're embedded in `RobotInfo`
- `SideBrushPower`, `MainBrushPower`, `MaxMode`, `PowerMode` are in the APK UI but not on this firmware
- `ErrorCode` is always null, even after checking timeline history (zero entries)
- `CleanLoop`, `CleanTime`, `CleanArea` may only populate during active cleaning (not tested during cleaning)

---

## 2. Settings Read/Write Behavior

Each setting was tested with: read current value, write new value, poll REST for change, check for MQTT push, restore original.

### 2.1 Working Settings

| Setting | Test Values | Accepted | REST GET Latency | MQTT Push | Notes |
|---------|------------|----------|-----------------|-----------|-------|
| **FanPower** | 1, 25, 50, 75, 100 | All accepted | ~2.2s | None | 0-100 continuous range, first read after set sometimes returns old value |
| **WaterTankContrl** | 0, 1, 2, 3 | 0-2 accepted | ~2.2s | None | **Value 3 wraps to 0** — max is 2 |
| **CarpetControl** | 0, 1 | Both accepted | ~2.2s | None | Simple toggle |
| **ContinueCleanSwitch** | 0, 1 | Both accepted | ~2.2s | None | Breakpoint resume toggle |
| **BeepVolume** | 0, 25, 50, 100 | All accepted | ~2.2s | None | 0-100 continuous range |

### 2.2 Non-Working Settings

| Setting | Behavior | Notes |
|---------|----------|-------|
| **MaxMode** | API returns `ok`, but GET always returns `None` | Property defined in Aliyun model but not implemented in firmware |
| **PowerMode** | N/A — null on read | Not available on this device |
| **SideBrushPower** | N/A — null on read | Not available on this device |
| **MainBrushPower** | N/A — null on read | Not available on this device |
| **BeepType** | N/A — null on read | Not available on this device |
| **BeepNoDisturb** | N/A — null on read | Not available on this device |

### 2.3 Key Finding: WaterTankContrl Range

The APK presents 4 levels (off/low/med/high = 0/1/2/3), but this firmware only supports 3:
- **0**: Off
- **1**: Low
- **2**: Medium/High
- **3**: Wraps back to 0 (silently)

This suggests the water tank hardware has fewer flow settings than the app UI expects.

### 2.4 REST SET → GET Propagation

For every setting tested, the consistent pattern was:
- **SET API call**: ~80-130ms round trip (HTTP response)
- **GET reflects change**: ~2.2s after SET
- First GET poll at +1s usually returns **old value**
- Second GET poll at +2.2s returns **new value**
- This 2.2s delay is extremely consistent across all properties

---

## 3. MQTT Push Behavior

**Definitive finding: MQTT pushes NOTHING on this device/firmware.**

### 3.1 Test Methodology

- MQTT connection established via Aliyun AEP auth endpoint
- Connected to `public.itls.eu-central-1.aliyuncs.com:1883` with TLS
- Subscribed to `/sys/{productKey}/{deviceName}/app/down/#`
- Account binding completed successfully (bind_reply received)
- Interceptor monkey-patched `Zaco.merge_mqtt_push` to log all pushes with timestamps

### 3.2 Results by Scenario

| Scenario | Duration | MQTT Pushes | Notes |
|----------|----------|-------------|-------|
| Robot idle on charger | 30s baseline | **0** | |
| UploadDataControl enabled, idle | 60s | **0** | Fast upload mode does nothing for MQTT |
| FanPower changed (5 values) | ~15s | **0** | Setting changes not pushed |
| WaterTankContrl changed | ~12s | **0** | |
| BeepVolume changed | ~12s | **0** | |
| CarpetControl changed | ~4s | **0** | |
| ContinueCleanSwitch changed | ~4s | **0** | |
| Room cleaning (Kuche) | ~185s | **0** | Full cycle: start → clean → pause → resume → return → dock |
| Zone cleaning (Wohnzimmer) | ~200s | **0** | Full cycle: start → clean → return → dock |
| Edge cleaning (Esszimmer) | ~160s | **0** | Full cycle: goto → edge → return → dock |
| Remote control attempt | ~7s | **0** | |
| Explore mode | ~11s | **0** | |
| SoundLocate attempt | ~6s | **0** | |
| Passive idle observation | 60s | **0** | Final audit |
| FanPower trigger test | ~5s | **0** | During MQTT audit phase |

**Total MQTT push messages received across entire 13-minute session: 0**

### 3.3 Implications

- **MQTT on this device is non-functional for data push** — the device never sends property updates via MQTT
- MQTT is only useful for account binding (the bind_reply message was received)
- **All status monitoring MUST use REST polling** — there is no alternative
- The Aliyun MQTT infrastructure works (connection, auth, subscribe, bind all succeed) — the device firmware simply doesn't publish to it
- This may be firmware-specific — other 3irobotix/iLife devices on the same platform might push data

---

## 4. Cleaning Modes & WorkMode Transitions

### 4.1 Room Cleaning (Phase 5 — Kuche)

**Command**: `CleanPartitionData = {"PartitionData": 32, "CleanLoop": 1, "Enable": 1}`

**WorkMode transition sequence**:

```
t=0.0s  CMD: Set CleanPartitionData
t=1.2s  WM 9 → 16   (PS=0, Pause=1)  — "preparing" / transitional state
t=2.4s  WM 16 → 20  (PS=0, Pause=1)  — ROOM CLEANING active
t=20-60s WM=20 continuously (PS=1, Pause=0)  — cleaning in progress

t=60.8s  CMD: Set WorkMode=2 (pause)
t=63.0s  WM=20 still (2.2s after pause cmd)  — delay before pause takes effect
t=64.1s  WM 20 → 2   (PS=1, Pause=0)  — PAUSED, 3.3s after pause cmd

[10s observation in paused state]

t=75.8s  CMD: Set WorkMode=21 (resume)
[~34s of resumed cleaning, then manual return]

t=109.9s CMD: Set WorkMode=8 (return to dock)
t=112.2s WM 2 → 8    — RETURNING, 2.3s after return cmd
t=185.0s WM 8 → 9    — DOCKED, 75.1s transit time (Kuche → charger)
```

**Key observations**:
- **WM 16**: Transient "preparing" state, lasts ~1.2s before transitioning to WM 20
- **WM 20**: Room cleaning active state (not WM 6, which is the *command* to start planning clean)
- **WM 21**: Resume command (sent by app), robot may briefly show WM 2 then resume
- **PowerSwitch**: Stays 0 during initial transition, becomes 1 once actively cleaning
- **PauseSwitch**: Counter-intuitively, 1 when docked/idle, 0 when actively cleaning

### 4.2 Zone Cleaning (Phase 6 — Wohnzimmer)

**Command**: `CleanAreaData = {"AreaData": "<base64>", "CleanLoop": 1, "Enable": 1}`  
Zone: center (19,87), bounds (12,80)→(26,94)

**WorkMode transition sequence**:

```
t=0.0s  CMD: Set CleanAreaData
t=1.2s  WM → 9   (stale read, still idle)
t=2.4s  WM 9 → 19  — ZONE CLEANING active
[~45s zone cleaning]
t=53.4s CMD: Set WorkMode=2 (pause) + WorkMode=8 (return)
[~200s total including return journey]
WM 8 → 9  — DOCKED
```

**Key observations**:
- **WM 19**: Zone cleaning active state
- Transition to WM 19 takes ~2.4s (same as room clean startup delay)
- Zone clean returns to dock took longer (200s from zone in Wohnzimmer to dock)

### 4.3 Edge Cleaning (Phase 7 — Esszimmer)

**Sequence**: Navigate to room center via small zone, pause, switch to edge clean mode.

```
t=0.0s  CMD: Small zone at Esszimmer center (19,25)
t=1.2s  WM → 9 (stale)
t=3.5s  WM 9 → 19 (zone cleaning started)
t=3.5s  CMD: Pause (WM=2)
t=7.8s  WM → 2  (paused at location)
t=7.8s  CMD: Set WorkMode=4 (edge clean)
t=9.1s  WM 2 → 4  — EDGE CLEANING active, 2.5s after command
[~68s of edge cleaning]
t=77.0s CMD: Set WorkMode=8 (return)
t=84s   WM → 8 (returning)
t=161s  WM 8 → 9 (docked)
```

**Key observations**:
- **WM 4**: Edge/wall-following cleaning mode
- Must pause (WM 2) before switching to edge mode — cannot go directly from WM 19 to WM 4
- Edge clean runs along walls near the position where it started
- Classified as "PAUSED" by the state machine but actually actively cleaning along edges

### 4.4 Remote Control (Phase 8)

**Command**: `WorkMode = 10`

```
t=0.0s  CMD: Set WorkMode=10
t=0.09s REST SET response: ok
t=3.2s  WorkMode reads as: 9 (still idle)
```

**Result**: **Remote control mode is silently rejected.** The API accepts the command (returns `ok`) but the device ignores it and stays in WM 9. This is consistent with the pattern seen for PointToGo — some commands may require MQTT delivery rather than REST.

### 4.5 Explore Mode (Phase 9) — DESTRUCTIVE, DO NOT USE

**WARNING**: WorkMode 22 and 23 are the "create new map" commands per the APK's
`onEnsureNewMap()` flow in `SelectMapPresenter.java`. Sending WorkMode 22 **destroys
the saved room map** — even without sending `SelectedMapId=0` first. This was
discovered the hard way during the research session.

**Previous test results** (before this was understood):
- **WM 22**: ACCEPTED — robot entered explore/new-map mode, destroyed saved map
- **WM 23**: Stayed at WM 22 (no additional effect)

**APK code path**: `onEnsureNewMap()` → `setSelectMapId(0)` → `enterExploreMode()` → `WorkMode=22`

Phase 9 has been removed from the research script to prevent future map destruction.

### 4.6 WorkMode Summary Table

| WorkMode | Meaning | Source | Direction |
|----------|---------|--------|-----------|
| **2** | Pause/Standby | Command | Send |
| **4** | Edge/wall-follow clean | Command & Report | Both |
| **5** | Spot clean | Command | Send |
| **6** | Planning/auto clean | Command | Send |
| **8** | Return to dock | Command & Report | Both |
| **9** | Idle/docked | Report | Receive |
| **10** | Remote control | Command | **Rejected** |
| **16** | Preparing (transitional) | Report | Receive |
| **19** | Zone cleaning | Report | Receive |
| **20** | Room cleaning | Report | Receive |
| **21** | Resume cleaning | Command | Send |
| **22** | New map / explore (**DESTRUCTIVE** — destroys saved map) | Command & Report | Both |
| **23** | New map + clean (**DESTRUCTIVE**) | Command | **Stays at 22** |

---

## 5. REST API Timing & Latency

### 5.1 SET Command Latency

Measured across all settings tests (19 SET operations):

| Metric | Value |
|--------|-------|
| **Minimum** | 71ms |
| **Maximum** | 127ms |
| **Average** | ~92ms |
| **Median** | ~85ms |

This measures the HTTP round-trip time for the `set_properties` API call.

### 5.2 SET → GET Propagation Delay

The time from sending a SET command until a subsequent GET returns the new value:

| Metric | Value |
|--------|-------|
| **Consistent** | ~2.2s |
| **Pattern** | GET at +1s returns OLD value, GET at +2.2s returns NEW value |

This 2.2s delay represents:
1. Cloud processing the SET command
2. Cloud forwarding to device
3. Device updating its state
4. Next GET reading the updated value from the cloud

### 5.3 WorkMode Command → State Change

| Command | Delay to WorkMode Change |
|---------|-------------------------|
| Start room clean | 2.4s (via transitional WM 16) |
| Start zone clean | 2.4s (WM 9 → 19) |
| Pause (WM 2) | 3.3s (from WM 20) |
| Return to dock (WM 8) | 2.3s |
| Edge clean (WM 4) | 2.5s (from WM 2) |
| Explore (WM 22) | ~5.2s (longer, includes startup) |

### 5.4 REST Polling Rate

With 3-second poll intervals (as configured in the research script):
- **Baseline (idle)**: 10 polls in 30s (~3.0s between polls)
- **With UploadDataControl enabled**: 20 polls in 60s (~3.0s between polls)

Note: UploadDataControl does NOT change REST polling frequency — it's supposed to increase the device's data upload rate to the cloud, but with MQTT non-functional, this has no observable effect during idle.

---

## 6. UploadDataControl (Fast Updates)

### 6.1 Test Results

| Phase | Duration | MQTT Pushes | REST Polls | Notes |
|-------|----------|-------------|------------|-------|
| Baseline (off) | 30s | 0 | 10 | Normal state |
| Fast upload enabled | 60s | 0 | 20 | No observable difference |

### 6.2 Analysis

- **UploadDataControl** tells the device to report data at higher frequency
- On this device, with MQTT non-functional, enabling fast uploads has **no observable effect** while idle
- The property may have effect during active cleaning (more frequent RealMapRoadData updates to the cloud via device-to-cloud channel), which would be visible via the Timeline API but not via MQTT push
- The APK sends this when the map view is active and refreshes it every 60s

---

## 7. SoundLocate

### 7.1 Test Results

Two approaches tested:

1. **Via Zaco facade** (`zaco.locate()`):
   - Sends `{"SoundLocate": {"SoundDir": 0}}`
   - Response: **FAILED** (0.080s)

2. **Via direct async client** (raw `set_properties`):
   - Response: **False** (0.108s)

**Error code**: 5092 — "property not found"

### 7.2 Analysis

- `SoundLocate` appears in the property dump as `null` (missing from device model)
- The APK definitely uses this property (`SoundLocateBean.java`, `LocatePresenter.java`)
- The property may require MQTT delivery, similar to PointToGo
- Or it may only be supported on newer firmware versions
- No MQTT push was observed after either attempt

---

## 8. Timeline API

### 8.1 Overview

Endpoint: `/thing/property/timeline/get` (API version `1.0.2`)  
Queried: last 30 minutes from time of test (~1800s window)  
All queries completed within session that included room clean, zone clean, edge clean, and explore mode.

### 8.2 Results by Property

| Property | Entries | Time Span | Avg Interval | Notes |
|----------|---------|-----------|-------------|-------|
| **RealMapRoadData** | 16 | 970s | 64.6s | Min 0.3s, Max 354.3s — highly variable |
| **WorkMode** | 51 | 1016s | ~20s | High frequency during transitions |
| **BatteryState** | 40 | 1016s | ~25s | Consistently 100% during test |
| **FanPower** | 31 | 1410s | ~45s | Includes test value changes |
| **CleanHistory** | 37 | 792s | ~21s | Updated per cleaning session |
| **PowerSwitch** | 35 | 1011s | ~29s | |
| **PauseSwitch** | 35 | 1011s | ~29s | |
| **RealTimeRoadStart** | 17 | 1012s | ~60s | Marks cleaning session starts |
| **ErrorCode** | 0 | — | — | **No data at all** |

### 8.3 RealMapRoadData Timeline Detail

Sample entry:
```json
{
  "RoadData": "//r//f/4//0=",
  "CurrentPoint": -458755,
  "CleanArea": 0,
  "CleanDirection": 179,
  "RoadDataType": 0,
  "CleanTime": 0,
  "RoadResolution": 50
}
```

- Update intervals are **highly variable**: from 0.3s (rapid successive updates) to 354.3s (gap between sessions)
- Average of 64.6s hides bimodal distribution: frequent during cleaning, sparse when idle
- `CurrentPoint` is a packed int (two int16 values: x and -y)
- `RoadData` contains incremental path points (base64-encoded binary)

### 8.4 CleanHistory Timeline Detail

Sample entry:
```json
{
  "StopCleanReason": 5,
  "CleanTotalTime": 0,
  "PackNum": 7,
  "MapResolution": 50,
  "StartCleanReason": 1,
  "StartTime": 1771689911,
  "CleanTotalArea": 0,
  "PackId": 7,
  "CleanMapData": "BwAS/+gAFP/oABT/5gAS/+..."
}
```

- `StopCleanReason`: 5=user stopped, 7=returned to dock
- `StartCleanReason`: 1=user command, 8=schedule
- `CleanMapData`: base64-encoded map snapshot of the cleaning session
- `PackNum`/`PackId`: fragmentation counters for the map data

### 8.5 Properties NOT Supporting Timeline

- **ErrorCode**: Zero entries — either no errors occurred or this property doesn't support timeline
- Other null properties (SoundLocate, PointToGo, etc.) were not tested via timeline

---

## 9. Schedules

### 9.1 Active Schedules (3 of 7)

**Schedule 1**: Daily at 17:30 — Room clean Buro
```json
{
  "ScheduleHour": 17, "ScheduleMinutes": 30,
  "ScheduleWeek": 127,       // binary 1111111 = all days
  "ScheduleEnable": 1,
  "ScheduleType": 2,          // room cleaning
  "ScheduleMode": 6,          // planning mode
  "ScheduleLoop": 1,          // 1 pass
  "ScheduleRoom": 2,          // room bitmask: Buro (2)
  "ScheduleArea": "AAAAAAAAAAAAAAAAAAAAAA==",  // all zeros = no zone
  "ScheduleEnd": 300          // max runtime 300 min
}
```

**Schedule 2**: Weekdays (Tu/We/Th/Fr/Sa) at 12:00 — Room clean Esszimmer + Kuche + ???
```json
{
  "ScheduleHour": 12, "ScheduleMinutes": 0,
  "ScheduleWeek": 106,       // binary 1101010 = Tu,Th,Sa,? (or week encoding differs)
  "ScheduleRoom": 56,         // room bitmask: 8+16+32 = Badezimmer+Esszimmer+Kuche
  "ScheduleLoop": 1, "ScheduleMode": 6, "ScheduleType": 2
}
```

**Schedule 3**: Selected days at 12:00 — Multiple rooms
```json
{
  "ScheduleHour": 12, "ScheduleMinutes": 0,
  "ScheduleWeek": 21,         // binary 0010101 = Mon,Wed,Fri (or offset)
  "ScheduleRoom": 445,        // bitmask: 1+4+8+16+32+128+256 = all rooms except Buro
  "ScheduleLoop": 1, "ScheduleMode": 6, "ScheduleType": 2
}
```

**Schedules 4-7**: Empty (ScheduleEnable=0, all zeros)

### 9.2 Schedule Field Reference

| Field | Type | Description |
|-------|------|-------------|
| ScheduleHour | int | Hour (0-23) |
| ScheduleMinutes | int | Minutes (0-59) |
| ScheduleWeek | int | Bitmask for days of week (bit 0=Mon? or Sun?) |
| ScheduleEnable | int | 0=disabled, 1=enabled |
| ScheduleType | int | 0=none, 2=room clean |
| ScheduleMode | int | 6=planning/auto clean mode |
| ScheduleLoop | int | Number of passes |
| ScheduleRoom | int | Room bitmask (same as CleanPartitionData) |
| ScheduleArea | string | Base64 zone data (all zeros = no zone constraint) |
| ScheduleEnd | int | Max runtime in minutes |

### 9.3 Week Bitmask Encoding

| ScheduleWeek | Binary | Days (assuming bit0=Sun) |
|-------------|--------|--------------------------|
| 127 | 1111111 | All days |
| 106 | 1101010 | Mon, Wed, Fri, Sun |
| 21 | 0010101 | Sun, Tue, Thu |

Note: The exact day-to-bit mapping (bit0=Sunday vs bit0=Monday) needs further verification. The APK's `ScheduleHelper.java` would have the definitive mapping.

---

## 10. Device Info & Misc Properties

### 10.1 WiFiInfo
```json
{"RSSI": 97, "IP": -1062700941, "SoftWareVer": 50462720, "MAC": "NCADZku6"}
```
- RSSI: 97 (strong signal, values are 0-100 where higher = better)
- IP: packed as signed int32 (`-1062700941` = `192.168.0.115` approximately)
- MAC: base64-encoded `"NCADZku6"` = `34:20:03:66:4B:BA` (confirmed matches device)
- SoftWareVer: Same as in RobotInfo

### 10.2 RobotInfo
```json
{"RobotType": 0, "SoftwareVer": 50462720, "HardwareVer": 263685}
```
- RobotType: 0 (X9 series based on APK code paths)
- SoftwareVer: `50462720` = `0x0301F800` (major.minor.patch encoded)
- HardwareVer: `263685` = `0x000405F5`

### 10.3 PartsStatus
```json
{"FilterLife": 0, "SideBrushLife": 0, "MainBrushLife": 0}
```
All at 0 = all parts need replacement/reset (or counter has rolled over).

### 10.4 ChargerPoint
```json
{"Piont": -851971, "DisplaySwitch": 1}
```
- `Piont` (sic — typo in API): packed int encoding charger position
- `DisplaySwitch`: 1 = show charger on map

### 10.5 ForbiddenAreaData

Base64-encoded binary, 240 bytes (after decoding). Format:
- 20 bytes per zone × 10 slots = 200 bytes + header
- Each zone: 4 bytes type + 4 corners × 4 bytes (x,y pairs)
- Non-zero data in first 3 slots = 3 forbidden zones configured
- Remaining slots all zeros

### 10.6 VirtualWallData

Base64-encoded binary, all zeros = no virtual walls configured.
`VirtualWallEN = 1` means the feature is enabled even though no walls are set.

---

## 11. Summary & Implications for Integration

### 11.1 Critical Findings

1. **MQTT is non-functional for data push** — REST polling is the ONLY way to get state updates. Zero pushes across all 13 minutes of testing including active cleaning, idle, setting changes, and UploadDataControl enabled.

2. **REST SET → GET delay is consistently ~2.2s** — any integration must account for this propagation delay. After sending a command, the first poll should be delayed by at least 2s.

3. **WorkMode 16 is a transitional state** — appears briefly (1.2s) when starting room cleaning before transitioning to WM 20. Integration should handle this gracefully.

4. **WaterTankContrl max is 2, not 3** — value 3 wraps to 0. The integration should limit the selector to 3 options (Off/Low/Medium).

5. **Remote control (WM 10) is silently rejected** — cannot be implemented via REST API.

6. **Explore mode (WM 22) and explore+clean (WM 23) are DESTRUCTIVE** — they trigger "create new map", destroying the saved room map. Never send these.

7. **SoundLocate fails via REST** (error 5092) — requires MQTT delivery (which doesn't push on this device, so both directions are broken).

8. **Multiple APK settings don't exist on this firmware**: MaxMode, PowerMode, SideBrushPower, MainBrushPower, BeepType, BeepNoDisturb, and many others are phantom properties.

### 11.2 Recommended Polling Strategy

Given MQTT non-functionality:
- **Idle/docked**: Poll every 30s (nothing changes)
- **Active cleaning**: Poll every 3-5s (for position and state updates)
- **After sending command**: Wait 2.5s before first poll
- **UploadDataControl**: Enable during active cleaning for potentially more frequent cloud-side updates (visible via Timeline API for path reconstruction)

### 11.3 Properties Worth Monitoring

**Always poll** (change during operation):
- WorkMode, PowerSwitch, PauseSwitch, BatteryState

**Poll during cleaning**:
- RealMapRoadData (path + position), CleanHistory (session stats)

**Poll occasionally** (change infrequently):
- PartsStatus, WiFiInfo (RSSI), ForbiddenAreaData, VirtualWallData

**Read once on startup**:
- RobotInfo, SaveMap, ChargerPoint, Schedule1-7, InitStatus, MapRoomInfo1-3, SaveMapDataX9_N

### 11.4 Commands That Work

| Action | Command | Response Time |
|--------|---------|---------------|
| Start room clean | `CleanPartitionData` | 2.4s to WM 20 |
| Start zone clean | `CleanAreaData` | 2.4s to WM 19 |
| Start edge clean | `WorkMode=4` (from paused state) | 2.5s to WM 4 |
| Start explore | `WorkMode=22` | ~5s to WM 22 |
| Pause | `WorkMode=2` | 3.3s |
| Resume | `WorkMode=21` | ~2s |
| Return to dock | `WorkMode=8` | 2.3s |
| Spot clean | `WorkMode=5` (from paused state) | ~2s |
| Set fan power | `FanPower=N` (0-100) | 2.2s to reflect |
| Set water level | `WaterTankContrl=N` (0-2) | 2.2s to reflect |
| Set beep volume | `BeepVolume=N` (0-100) | 2.2s to reflect |

### 11.5 Commands That Don't Work

| Action | Reason |
|--------|--------|
| Remote control (WM 10) | Silently rejected |
| Explore / new map (WM 22) | **DESTRUCTIVE** — destroys saved room map |
| Explore + clean (WM 23) | **DESTRUCTIVE** — same as WM 22 |
| SoundLocate | Error 5092, property not found |
| PointToGo | Silently ignored (documented earlier) |
| Set MaxMode | API accepts but value stays null |
