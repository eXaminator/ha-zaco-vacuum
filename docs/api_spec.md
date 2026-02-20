# ZACO A10 API Specification

## Platform

- **Hardware manufacturer:** iRobotics / 3irobotix (same company as iLife)
- **Primary cloud platform:** Aliyun IoT Living Platform (REST API + MQTT)
- **Secondary cloud:** 3irobotix WebSocket (`*.3irobotics.net`) for OTA updates and device discovery
- **App package:** `com.zaco.home.robot` (ZACOHome v1.7.7)
- **Product Key:** `a1wzCC1Mr2b`
- **Factory Type:** X901

## Architecture (Confirmed via MITM capture 2026-02-18)

```
                  HTTPS REST API                    Aliyun MQTT
ZACOHome App  <===================>  Aliyun IoT Cloud  <=========>  ZACO A10 Vacuum
              HmacSHA1 signed        (eu-central-1)                  (on local WiFi)
```

The app communicates with the vacuum via the Aliyun IoT Living Platform. Device
properties are read/written via REST API, real-time events via MQTT. The 3irobotix
WebSocket is used only for OTA updates and device discovery (not device control).

---

## Aliyun IoT Living Platform API (PRIMARY — WORKING)

### Credentials
- **App Key:** `28416395`
- **App Secret:** `a2a5fdb0aa8555d31d80016454f2b248`
- **Region discovery host:** `cn-shanghai.api-iot.aliyuncs.com`
- **EU IoT API host:** `eu-central-1.api-iot.aliyuncs.com`
- **EU OA login host:** `living-account.eu-central-1.aliyuncs.com`
- **EU MQTT broker:** `public.itls.eu-central-1.aliyuncs.com:1883`

### Request Signing (HmacSHA1)

Every request is signed with these headers:
```
x-ca-key: 28416395
x-ca-timestamp: <milliseconds>
x-ca-nonce: <UUID>
x-ca-signature-method: HmacSHA1
x-ca-signature-headers: <comma-separated list of x-ca-* headers>
x-ca-signature: <base64(HMAC-SHA1(app_secret, string_to_sign))>
CA_VERSION: 1
```

Content-MD5: `base64(md5(body))` truncated to 24 chars (for POST_BODY mode only).

### Request Body Format (IoT API)

```json
{
  "a": "<request-id-uuid>",
  "b": "1.0",
  "c": {"apiVer": "1.0.2", "language": "en-US", "iotToken": "<token>"},
  "d": {<actual params>},
  "id": "<same-as-a>",
  "params": {"$ref": "$.d"},
  "request": {"$ref": "$.c"},
  "version": "1.0"
}
```

### Authentication Flow

#### 1. Region Lookup
```
POST https://cn-shanghai.api-iot.aliyuncs.com/living/account/region/get
Body: {"type": "EMAIL", "email": "<user_email>", "countryCode": "", "phoneLocationCode": ""}
Response: {
  "code": 200,
  "data": {
    "regionId": "eu-central-1",
    "apiGatewayEndpoint": "eu-central-1.api-iot.aliyuncs.com",
    "oaApiGatewayEndpoint": "living-account.eu-central-1.aliyuncs.com",
    "mqttEndpoint": "public.itls.eu-central-1.aliyuncs.com:1883",
    "regionEnglishName": "Germany"
  }
}
```

#### 2. Login (form-encoded)
```
POST https://living-account.{region}.aliyuncs.com/api/prd/login.json
Content-Type: application/x-www-form-urlencoded
Body: loginRequest=<JSON: {"loginId": email, "password": RSA_encrypted, "riskControlInfo": {...}}>
Response: {
  "data": {"code": 1, "data": {"loginSuccessResult": {
    "sid": "<session_id>",
    "token": "<token>",
    "refreshToken": "OA-<hex>",
    "sidExpireIn": 86400,
    "reTokenExpireIn": 7776000
  }}}
}
```

Password is RSA-encrypted with the hardcoded public key from RSAKey.java (PKCS1v15).

#### 3. Create IoT Session
```
POST https://{region}.api-iot.aliyuncs.com/account/createSessionByAuthCode
Body params: {"request": {"authCode": "<sid>", "accountType": "OA_SESSION", "appKey": "28416395"}}
Response: {
  "code": 200,
  "data": {
    "iotToken": "<32-char hex>",
    "refreshToken": "<32-char hex>",
    "identityId": "<hex>",
    "iotTokenExpire": 72000,
    "refreshTokenExpire": 720000
  }
}
```

#### 4. Refresh Token
```
POST https://{region}.api-iot.aliyuncs.com/account/checkOrRefreshSession
Body params: {"request": {"refreshToken": "<token>", "identityId": "<id>"}}
```

### Device Management

#### List Devices
```
POST https://{region}.api-iot.aliyuncs.com/uc/listBindingByAccount
Body: apiVer 1.0.2, iotToken required, params: {}
Response: {
  "code": 200,
  "data": {"total": 1, "data": [{
    "iotId": "KReBFAPbEXU5Yk31mDep000000",
    "deviceName": "KReBFAPbEXU5Yk31mDep",
    "productKey": "a1wzCC1Mr2b",
    "productModel": "ZACO A10",
    "nickName": "Friday",
    "categoryKey": "RobotCleaner",
    "status": 1,
    "netType": "NET_WIFI"
  }]}
}
```

### Device Properties

#### Get Properties
```
POST https://{region}.api-iot.aliyuncs.com/thing/properties/get
Body params: {"iotId": "<iot_id>", "items": ["BatteryState", "WorkMode", ...]}
Response: {
  "code": 200,
  "data": {
    "BatteryState": {"value": 100.0, "time": 1771439168565},
    "PowerSwitch": {"value": 0, "time": 1771439171977},
    ...
  }
}
```

#### Set Properties
```
POST https://{region}.api-iot.aliyuncs.com/thing/properties/set
Body params: {"iotId": "<iot_id>", "items": {"WorkMode": 1, "FanPower": 3}}
```

### Room Cleaning (CONFIRMED WORKING)

#### MapRoomInfo Format

Room-to-ID mappings are stored in `MapRoomInfo1`, `MapRoomInfo2`, `MapRoomInfo3` properties (one per saved map slot). Values are **base64-encoded CSV strings** (NOT JSON).

```
# Raw property value (base64):
MTczNDI2OTM2OSwsMSxTY2hsYWZ6aW1tZXIsMixCw7xybyw0LFdvaG56aW1tZXIs...

# Decoded:
1734269369,,1,Schlafzimmer,2,Büro,4,Wohnzimmer,8,Badezimmer,16,Esszimmer,32,Küche,128,Flur,256,Dusche
```

CSV layout:
- `[0]` = map identifier (e.g. `1734269369`)
- `[1]` = empty (unused map name field)
- `[2..n]` = pairs of `roomId,roomName` repeating

Room IDs are **bitmask values** (powers of 2): 1, 2, 4, 8, 16, 32, 64, 128, 256...

APK source: `SelectMapPresenter.addMapName()` — `Base64.decode(str, 0)` then `split(",")`.

The `SaveMap` property contains `SelectedMapId` to identify the active map slot.

#### Triggering Room Clean

Set the `CleanPartitionData` property:
```json
{"CleanPartitionData": {"PartitionData": 33, "CleanLoop": 1, "Enable": 1}}
```

| Field | Type | Description |
|-------|------|-------------|
| `PartitionData` | int | Sum of room IDs to clean (bitmask OR) |
| `CleanLoop` | int | Number of cleaning passes (1-3) |
| `Enable` | int | 1 to start cleaning |

To clean multiple rooms, sum their IDs. Example: Schlafzimmer (1) + Küche (32) = `PartitionData: 33`.

APK source: `RoomHelper.getSelectRoomId()` sums selected room IDs via `+=`. `SelectRoomActivity` sends the result as `CleanPartitionData`.

### Known Device Properties

| Property | Type | Description |
|----------|------|-------------|
| BatteryState | float | Battery level 0-100 |
| PowerSwitch | int | 0=off/docked, 1=on |
| WorkMode | int | See WorkMode state machine below |
| CleanType | int | Type of cleaning operation |
| FanPower | int | Suction power level |
| WaterTankContrl | int | Water tank/mop level |
| CarpetControl | int | Carpet boost mode |
| PauseSwitch | int | Pause state |
| InitStatus | bool | Initialization status |
| CleanSettings | JSON | Per-room cleaning config |
| CleanAreaData | JSON | Area cleaning data |
| CleanPartitionData | JSON | Room partition data |
| ForbiddenAreaData | base64 | No-go zones |
| RealMapRoadData | JSON | Real-time map + path data |
| RealTimeMapStart | int | Map start timestamp |
| CleanHistory | JSON | Cleaning session history |
| CleanHistoryStartTime | int | History start timestamp |
| PartsStatus | JSON | Filter/brush life (FilterLife, SideBrushLife, MainBrushLife) |
| ChargerPoint | JSON | Charger station location |
| AppRemind | int | App reminder flags |
| Schedule1-7 | JSON | Weekly cleaning schedules |
| BeepVolume | int | Voice volume |
| BeepType | int | Language code |
| BeepNoDisturb | JSON | Do not disturb settings |

### MQTT Topics (for real-time communication)

| Direction | Method | Description |
|-----------|--------|-------------|
| App → Device | `thing.service.property.set` | Set device properties |
| App → Device | `thing.service.property.get` | Get device properties |
| Device → App | `thing.event.property.post` | Property change events |

Gateway credentials are obtained via:
```
POST https://{region}.api-iot.aliyuncs.com/app/aepauth/handle
Body params: {"authInfo": {"clientId": "<id>", "sign": "<hmac>", "deviceSn": "<sn>", "timestamp": "<ms>"}}
Response: {"code": 200, "data": {"deviceSecret": "<hex>", "productKey": "<key>", "deviceName": "<name>"}}
```

### Script Usage

```bash
# Full login + device listing + properties:
python3 scripts/test_aliyun.py --username EMAIL --password PASSWORD

# With saved tokens (auto-refresh):
python3 scripts/test_aliyun.py

# Set a device property:
python3 scripts/test_aliyun.py --set WorkMode=1

# Set a JSON property value:
python3 scripts/test_aliyun.py --set 'CleanPartitionData={"PartitionData":32,"CleanLoop":1,"Enable":1}'

# List available rooms (decoded from MapRoomInfo):
python3 scripts/test_aliyun.py --rooms

# Clean specific rooms by bitmask ID:
python3 scripts/test_aliyun.py --clean-rooms 32        # single room (Küche)
python3 scripts/test_aliyun.py --clean-rooms 1,4,32    # multiple rooms
python3 scripts/test_aliyun.py --clean-rooms 8 --clean-passes 2  # with 2 passes

# Pre-obtained iotToken:
python3 scripts/test_aliyun.py --iot-token TOKEN
```

---

## 3irobotix WebSocket Protocol (SECONDARY — OTA/Discovery Only)

> Note: This protocol is used by the app for OTA firmware updates and device
> discovery only. Device control and status monitoring use the Aliyun IoT
> Living Platform API documented above.

## Server Infrastructure

### WebSocket Servers (command & control)
| Region | Host | Port |
|--------|------|------|
| China | `fas.3irobotics.net` | 4020 (cmd), 4030 (map) |
| Europe | `eu.fas.3irobotics.net` | same |
| USA | `us.fas.3irobotics.net` | same |
| Australia | `au.fas.3irobotics.net` | same |

The actual WSS URL is resolved dynamically via:
```
POST https://ota.3irobotix.net:8001/service-publish/open/upgrade/try_upgrade
```
Response contains `targetUrls` with `wss://` and `https://` endpoints.

EU-specific OTA: `https://eu-ota.3irobotix.net:8001`

### HTTP API Server
- Same host as WebSocket, different protocol
- Used for OTA updates, user management, avatar uploads

## WebSocket Protocol

### Connection Flow

1. **Resolve server URL:** POST to `service-publish/open/upgrade/try_upgrade` to get WSS endpoint
2. **Connect WebSocket:** `wss://<resolved_host>`
3. **Login with token:** Send `login_token` message with stored token + userId
4. **Heartbeat:** Send to `heart-beat` service every 15 seconds
5. **Send commands:** POST to `sweeper-transmit/transmit/to_bind` service
6. **Receive status:** Listen for `TRANSMIT_PUSH` push messages
7. **Receive maps:** Binary WebSocket frames parsed by `MapParseCommon`

### Message Format

All messages are JSON:
```json
{
  "traceId": "<timestamp_ms>",
  "method": "POST|GET|PUT|DELETE",
  "service": "<service_path>",
  "content": "<JSON_string_payload>"
}
```

### Response Format
```json
{
  "service": "<echo_service>",
  "method": "<echo_method>",
  "traceId": "<echo_trace_id>",
  "code": 0,
  "content": "<JSON_string_payload>",
  "pushTag": "sweeper-transmit/to_bind|kick_out",
  "pushContent": "<push_data>"
}
```

Response codes: `0` = success, `3`/`9`/`20` = re-login needed, `-1` = device offline/timeout

## Authentication

### Login Flow

1. **Register:** POST `sweeper-app-user/auth/register` with username/password
2. **Login by password:** POST `sweeper-app-user/auth/login` with `{username, password}`
3. **Login by auth code:** POST `sweeper-app-user/auth/login_authcode` with `{authcode, username, factoryId}`
4. **Login by token (reconnect):** POST `sweeper-app-user/auth/login_token` with `{factoryId, token, userId, versionCode, versionName}`
5. **Get token:** GET `sweeper-app-user/auth/get_token`

Login response contains: `AUTH` token, `CONNECTION_TYPE`, `ROBOT_TYPE`, `FACTORY_ID`, `USERNAME`

Token is stored in SharedPreferences under `user_info` -> `token`.

### Other Auth Endpoints
- `sweeper-app-user/auth/logout`
- `sweeper-app-user/auth/change_password`
- `sweeper-app-user/auth/reset_password`
- `sweeper-app-user/auth/obtain_authcode` (email/SMS code)
- `sweeper-app-user/app/user` (DELETE account)

## Device Management

### Bind/Unbind
- `sweeper-robot-center/app/bind` - Bind device (POST with `{mac, nickname, sn}`)
- `sweeper-robot-center/app/unbind?robotId=<id>` - Unbind device
- `sweeper-robot-center/app/bind_confirm?bindKey=<key>` - Confirm binding
- `sweeper-robot-center/app/bind_override` - Override existing binding
- `sweeper-robot-center/app/get_user_bind` - Get bound device list
- `sweeper-robot-center/app/get_robot_info?mac=<mac>&sn=<sn>` - Get device ID
- `sweeper-robot-center/app/is_robot_online?robotId=<id>` - Check online status
- `sweeper-robot-center/app/set_default?robotId=<id>` - Set as default device
- `sweeper-robot-center/app/modify_nickname?nickname=<name>&robotId=<id>` - Rename

### Device Sharing
- `sweeper-robot-center/app/share_robot?robotId=<id>&beInvited=<user>` - Share
- `sweeper-robot-center/app/get_shared_robot` - Get shared devices
- `sweeper-robot-center/app/shared_robot_reply?inviterId=<id>&robotId=<id>&dealType=<type>` - Reply to share invite

## Device Control Commands

All device commands are sent as POST to `sweeper-transmit/transmit/to_bind` service. The `content` payload contains a JSON object with `targets` (device ID array) and `data` (command details).

### Command Payload Structure
```json
{
  "clientType": "ROBOT",
  "targets": [<device_id>],
  "data": {
    "control": "<method_name>",
    "type": <ctrl_type>,
    "value": <ctrl_value>
  }
}
```

### Start / Stop / Pause

**Method:** `device_ctrl`

| Action | type | value | Notes |
|--------|------|-------|-------|
| Start cleaning | 1 | 1 | `CTRL_VALUE_START` |
| Pause | 1 | 2 | `CTRL_VALUE_PAUSE` |
| Fake pause (temporary) | 1 | 3 | `CTRL_VALUE_FAKE_PAUSE` |
| Stop | 1 | 0 | `CTRL_VALUE_STOP` |
| Go to idle mode | 1 | 4 | `CTRL_VALUE_TO_IDLE_MODE` |

### Cleaning Mode

**Method:** `set_mode`

| Mode | type | value | Notes |
|------|------|-------|-------|
| Auto | 0 | varies | `MODE_TYPE_AUTO` |
| Edge/Wall follow | 1 | - | `MODE_TYPE_EDGE` |
| Scrubbing/Mopping | 2 | - | `MODE_TYPE_SCRUBBING` |
| Return to dock | 3 | 0/1 | `MODE_TYPE_CHARGE` |
| Spot | 4 | - | `MODE_TYPE_SPOT` |
| Spiral | 5 | - | `MODE_TYPE_SPIRAL` |
| Area clean | 6 | area_id | `MODE_TYPE_AREA` |
| Explore | 7 | - | `MODE_TYPE_EXPLORE` |
| Random | 8 | - | `MODE_TYPE_RANDOM` |
| Gyro | 9 | - | `MODE_TYPE_GYRO` |
| Twice (double pass) | 10 | - | `MODE_TYPE_TWICE` |
| Point navigation | 11 | x,y | `MODE_TYPE_POINT` |

### Suction / Fan Speed

**Method:** `set_preference` with `type=1` (`CTRL_TYPE_POWER`)

| Level | value | Name |
|-------|-------|------|
| Quiet | 0 | Quiet/Eco |
| Normal | 1 | Normal |
| Medium | 2 | Range/Medium |
| Strong | 3 | Strong/Max |

### Water / Mop Flow

**Method:** `set_preference` with `type=2` (`CTRL_TYPE_WATER`)

| Level | value | Name |
|-------|-------|------|
| Off | 0 | Closed |
| Low | 1 | Low |
| Medium | 2 | Medium |
| High | 3 | High |

### Other Preferences

**Method:** `set_preference`

| type | Property | Values |
|------|----------|--------|
| 1 | Suction/Power | 0-3 |
| 2 | Water flow | 0-3 |
| 3 | Twice/Double clean | 0/1 |
| 4 | Broken resume | ? |
| 5 | Carpet turbo | 0/1 |
| 6 | Memory | ? |
| 7 | Sweep + Mop | ? |
| 8 | Clean action | ? |
| 9 | Carpet avoid | ? |
| 10 | Pile | ? |
| 11 | Low power | ? |

### Manual Direction Control

**Method:** `set_direct`

| Direction | value |
|-----------|-------|
| Forward | 1 |
| Left | 2 |
| Right | 3 |
| Backward | 4 |
| Stop | 5 |
| Exit manual mode | 10 |

### Get Device Status

**Method:** `get_status` with `type=-1, value=-1`

### Get Device Info

**Method:** `get_device_info`
Returns: battery, model, firmware version, etc.

### Voice Control

**Method:** `set_voice` with `type=0/1` (enable/disable), `value=<volume_level>`
**Method:** `get_voice`

### Quiet Hours / Do Not Disturb

**Method:** `set_quiet_time` with `is_open=0/1`, `begin_time`, `end_time`
**Method:** `get_quiet_time`

### Consumables

**Method:** `get_consumables` - Get wear levels
**Method:** `set_consumables` with `type=<consumable_type>` - Reset consumable

### Map Operations

**Method:** `get_map` - Get current map (params: mapType, mapId, planId, targets)
**Method:** `getMapAll` - Get all saved maps
**Method:** `get_map_list_info` - Get map list
**Method:** `setSaveMap` - Save current map to memory
**Method:** `setMapName` - Rename a saved map
**Method:** `get_buffer_map` - Get buffer/temp map

### Navigation

**Method:** `set_navigation` - Go to point (params: mapHeadId, planId, x, y)
**Method:** `set_mult_navigation` - Go to multiple points

### Room/Zone Cleaning

**Method:** `setRoomClean` - Clean specific room (params: mapHeadId, planId, roomData)
**Method:** `set_custom_clean` - Custom room clean with per-room settings
**Method:** `start_custom_clean` - Start custom room clean
**Method:** `delete_custom_clean` - Delete custom clean config

### Virtual Walls / No-Go Zones

**Method:** `set_virwall` - Set virtual walls
**Method:** `set_area` - Set area/zone restrictions

### Scheduling

**Method:** `set_order` - Set cleaning schedule
**Method:** `get_order` - Get schedules
**Method:** `delete_order` - Delete schedule
**Method:** `set_plan_order` / `get_plan_order` / `delete_plan_order` - Plan-based schedules

### Room Management

**Method:** `mergeRoom` / `mergeRooms` - Merge rooms on map
**Method:** `splitRoom` / `splitRooms` - Split rooms on map

## Device Status

### Push Status Data (real-time updates)

Received as `TRANSMIT_PUSH` messages. Parsed into `PushStatusData`:

```json
{
  "control": "<method_echo>",
  "devid": 12345,
  "result": 0,
  "mode": 1,
  "battary": 85,
  "pref": 2,
  "water": 1,
  "fault": 0,
  "voice": 1,
  "direct": 0,
  "time": 45,
  "area": 23.5
}
```

| Field | Type | Description |
|-------|------|-------------|
| `control` | string | Command that triggered this update |
| `devid` | int | Device ID |
| `result` | int | Command result (0=OK) |
| `mode` | int | Current WorkMode (see below) |
| `battary` | int | Battery level 0-100 |
| `pref` | int | Current suction level 0-3 |
| `water` | int | Current water level 0-3 |
| `fault` | int | Fault code (0=no error) |
| `voice` | int | Voice enabled |
| `direct` | int | Current direction |
| `time` | int | Cleaning time (minutes) |
| `area` | float | Cleaned area (m2) |

### Work Mode State Machine

From `WorkMode.java` - the `mode` field maps to these states:

**Idle States:**
| Value | Name | HA State |
|-------|------|----------|
| -1 | DEFAULT | idle |
| 0 | IDLE | idle |
| 14 | NAVIGATION_IDLE | idle |
| 22 | POINT_IDLE | idle |
| 23 | SCREW_IDLE | idle |
| 29 | CORNERS_IDLE | idle |
| 35 | AREA_IDLE | idle |
| 40 | MOPPING_IDLE | idle |
| 49 | EXPLORE_IDLE | idle |
| 85 | CARPET_CLEAN_IDLE | idle |

**Cleaning States:**
| Value | Name | HA State |
|-------|------|----------|
| 1 | AUTO | cleaning |
| 6 | FIX_POINT | cleaning |
| 7 | NAVIGATION | cleaning |
| 20 | SCREW_CLEAN | cleaning |
| 25 | CORNERS_CLEAN | cleaning |
| 30 | AREA_CLEAN | cleaning |
| 36 | MOPPING_CLEAN | cleaning |
| 45 | EXPLORE | cleaning |
| 81 | CARPET_CLEAN | cleaning |

**Paused States:**
| Value | Name | HA State |
|-------|------|----------|
| 4 | AUTO_PAUSE | paused |
| 9 | NAVIGATION_PAUSE | paused |
| 24 | FIX_POINT_PAUSE | paused |
| 27 | CORNERS_PAUSE | paused |
| 31 | AREA_PAUSE | paused |
| 37 | MOPPING_PAUSE | paused |
| 46 | EXPLORE_PAUSE | paused |
| 82 | CARPET_CLEAN_PAUSE | paused |

**Returning to Dock:**
| Value | Name | HA State |
|-------|------|----------|
| 5 | GO_HOME | returning |
| 10 | GLOBAL_GO_HOME | returning |
| 12 | NAVIGATION_GO_HOME | returning |
| 13 | FIX_POINT_GO_HOME | returning |
| 21 | SCREW_GO_HOME | returning |
| 26 | CORNERS_GO_HOME | returning |
| 32 | AREA_GO_HOME | returning |
| 38 | MOPPING_GO_HOME | returning |
| 47 | EXPLORE_GO_HOME | returning |
| 83 | CARPET_CLEAN_GO_CHARGE | returning |

**Error States:**
| Value | Name | HA State |
|-------|------|----------|
| 11 | GLOBAL_BROKEN | error |
| 28 | CORNERS_BROKEN | error |
| 33 | AREA_BROKEN | error |
| 39 | MOPPING_BROKEN | error |
| 48 | EXPLORE_BROKEN | error |
| 84 | CARPET_CLEAN_BROKEN | error |

**Manual Control:**
| Value | Name | HA State |
|-------|------|----------|
| 2 | MANUAL | cleaning |

## Map Data Protocol

Map data arrives as **binary WebSocket frames**. Parsed by `MapParseCommon`:

### Binary Packet Header
```
Offset  Size  Field
0       1     Pack Type (0=Location, 1=Map, 2=Plan Edit, 3=Plan All)
1       1     Pack Number (sequence, 0=last fragment)
2       2     Pack ID (short)
4       4     Device ID (int)
8       4     Content Length (int)
12+     var   Payload Data
```

### Fragmentation
- Large maps are split into multiple packets
- Reassembled by matching Pack ID
- Last packet has Pack Number = 0
- XOR checksum validation on all bytes

## ZACO A10 Capabilities (from `zaco_robot.json`)

```json
{
  "productKey": "a1wzCC1Mr2b",
  "factoryType": "X901",
  "robotType": "A10",
  "isHaveMap": true,
  "isHaveMapData": true,
  "isSupportPause": true,
  "waterLevelType": 1,
  "suctionType": false,
  "newSuctionUi": true,
  "settingCarpet": true,
  "settingBrush": true,
  "settingVoice": true,
  "newSettingVoiceUi": true,
  "newScheduleVersion": true,
  "newScheduleUi": true,
  "settingUpdate": true,
  "settingRecord": true,
  "mapType": 3,
  "waterTank": 1,
  "historyMapType": 3,
  "showCarpetInVir": true
}
```

## Key Source Files

| File | Purpose |
|------|---------|
| `com/irobotix/robotsdk/conn/ServiceProtocol.java` | All service paths, method names, constants |
| `com/irobotix/robotsdk/conn/MasterRequest.java` | High-level command API (start, stop, mode, etc.) |
| `com/irobotix/robotsdk/conn/network/RobotNetWork.java` | WebSocket connection management |
| `com/irobotix/cleanrobot/utils/WorkMode.java` | Work mode state machine |
| `com/irobotix/robotsdk/conn/rsp/PushStatusData.java` | Real-time status data model |
| `com/irobotix/robotsdk/conn/req/DeviceCtrl.java` | Command request builder |
| `com/irobotix/robotsdk/conn/req/DeviceManualCtrl.java` | Manual control request builder |
| `com/irobotix/robotsdk/conn/network/MapParseCommon.java` | Binary map data parser |
| `com/irobotix/robotsdk/utils/Constants.java` | Server URLs, ports |
| `com/irobotix/cleanrobot/utils/UrlInfo.java` | Hostname constants, regional servers |
| `com/aliyun/iot/aep/sdk/helper/SDKInitHelper.java` | Aliyun SDK keys (provisioning only) |

## Error Codes (WebSocket)

| Code | Meaning |
|------|---------|
| 0 | Success |
| -1 | Device offline / timeout |
| 3 | Re-login needed |
| 9 | Re-login needed |
| 20 | Re-login needed |

## Kick-Out Codes

| Code | Meaning |
|------|---------|
| 1 | No heartbeat received |
| 2 | Not logged in |
| 3 | Server has no session |
| 4 | Connection replaced (lazy disconnect) |
| 5 | Connection replaced (immediate disconnect) |
