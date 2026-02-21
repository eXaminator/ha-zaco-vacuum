"""Constants for the ZACO Robot Vacuum integration."""

try:
    from homeassistant.const import Platform

    PLATFORMS = [
        Platform.VACUUM,
        Platform.CAMERA,
        Platform.SENSOR,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.BUTTON,
        Platform.SWITCH,
    ]
except ImportError:
    # Standalone usage (test scripts) — Platform enum not available
    PLATFORMS = []

DOMAIN = "zaco"

# ---------------------------------------------------------------------------
# Aliyun IoT Living Platform credentials (from SDKInitHelper.java, appTag=3)
# ---------------------------------------------------------------------------
APP_KEY = "28416395"
APP_SECRET = "a2a5fdb0aa8555d31d80016454f2b248"

# RSA public key for password encryption (from RSAKey.java)
RSA_PUBLIC_KEY_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAl4EFDk91/ArPHjyX7UBz"
    "ofPTAD3pcP8FMgOs83hvLEcbFJOVASrPAjbJTuXsSZJd9tYPwKbuqlGqndvdl2Kn2z"
    "LFpLOcFAYOyaIDFzDOCWQw/kMjcm1U08BvPE7dbtkGM23lCyTBlDMHWJvUz3JVTZm"
    "6ApGWEOGRhs1rECjcS9HXttnllQ2gTtBAW5Xjb8tzDgWR0jMaHzduCcSimHPtQO4Os"
    "h4Op3ianRocbb9o/4OR8HgKdbaKO3Sq2+pYV7FveXmfXqUr5lH7oHji+4j5TaU4WXR"
    "GKOjHSVXtN0UrfCXtsWE0aGCXXQN78NJUf5VrJMh14mqiSrR07wgu3UG7OwIDAQAB"
)

# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------
REGION_DISCOVERY_HOST = "cn-shanghai.api-iot.aliyuncs.com"
IOT_HOST_DEFAULT = "eu-central-1.api-iot.aliyuncs.com"
OA_HOST_DEFAULT = "living-account.eu-central-1.aliyuncs.com"

# ---------------------------------------------------------------------------
# Token timing
# ---------------------------------------------------------------------------
IOT_TOKEN_LIFETIME = 7200  # 2 hours
REFRESH_TOKEN_LIFETIME = 2592000  # 30 days
TOKEN_REFRESH_MARGIN = 300  # refresh 5 minutes early

# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------
DEFAULT_SCAN_INTERVAL = 30  # seconds
FAST_POLL_INTERVAL = 3  # seconds — used during active cleaning
MQTT_IDLE_POLL_INTERVAL = 120  # seconds — REST safety-net when MQTT is connected

# ---------------------------------------------------------------------------
# MQTT (Aliyun IoT Living Platform real-time push)
# ---------------------------------------------------------------------------
MQTT_HOST_DEFAULT = "public.itls.eu-central-1.aliyuncs.com"
MQTT_PORT = 1883
MQTT_KEEPALIVE = 65
MQTT_RECONNECT_MIN = 2  # seconds
MQTT_RECONNECT_MAX = 64  # seconds

# ---------------------------------------------------------------------------
# WorkMode state machine
# Values 6, 8, 9, 12 confirmed via live API testing (2026-02-19).
# Other values from WorkMode.java (3irobotix) — may need adjustment.
# ---------------------------------------------------------------------------
WORKMODE_IDLE = {-1, 0, 2, 9, 14, 16, 22, 23, 29, 35, 40, 49, 85}
WORKMODE_CLEANING = {1, 6, 7, 19, 20, 21, 25, 30, 36, 45, 81}
WORKMODE_PAUSED = {4, 12, 24, 27, 31, 37, 46, 82}
WORKMODE_RETURNING = {5, 8, 10, 13, 26, 32, 38, 47, 83}
WORKMODE_ERROR = {11, 28, 33, 39, 48, 84}

# ---------------------------------------------------------------------------
# Water / mop levels
# ---------------------------------------------------------------------------
WATER_LEVELS = {
    "Off": 0,
    "Low": 1,
    "Medium": 2,
    "High": 3,
}
WATER_LEVELS_REVERSE = {v: k for k, v in WATER_LEVELS.items()}

# ---------------------------------------------------------------------------
# Device properties to poll
# ---------------------------------------------------------------------------
CORE_PROPERTIES = [
    "WorkMode",
    "BatteryState",
    "FanPower",
    "WaterTankContrl",
    "PowerSwitch",
    "PauseSwitch",
    "ErrorCode",
    "Fault",
    "CleanSettings",
    "PointToGo",
    "BeepNoDisturb",
    # CleanTime and CleanArea are extracted from RealMapRoadData by the
    # coordinator (the top-level properties are stale/absent on this firmware).
]

MAP_PROPERTIES = [
    "RealMapRoadData",
    "ChargerPoint",
    "SaveMapDataX9_1",
    "SaveMapDataX9_2",
    "SaveMapDataX9_3",
    "SaveMapDataInfoX9_1",
    "SaveMapDataInfoX9_2",
    "SaveMapDataInfoX9_3",
]

ROOM_PROPERTIES = [
    "SaveMap",
    "MapRoomInfo1",
    "MapRoomInfo2",
    "MapRoomInfo3",
]

CONSUMABLE_PROPERTIES = [
    "PartsStatus",
]

ALL_PROPERTIES = CORE_PROPERTIES + MAP_PROPERTIES + ROOM_PROPERTIES + CONSUMABLE_PROPERTIES

# Lightweight subset for fast polling during active cleaning.
# Includes RealMapRoadData for robot position / CleanTime / CleanArea.
FAST_PROPERTIES = [
    "WorkMode",
    "BatteryState",
    "RealMapRoadData",
    "RealTimeRoadStart",
    "ErrorCode",
    "Fault",
    "PauseSwitch",
    "PowerSwitch",
    "PointToGo",
]

# ---------------------------------------------------------------------------
# Config entry keys
# ---------------------------------------------------------------------------
CONF_IOT_HOST = "iot_host"
CONF_OA_HOST = "oa_host"
CONF_IOT_ID = "iot_id"
CONF_IOT_TOKEN = "iot_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_IDENTITY_ID = "identity_id"
CONF_IOT_TOKEN_EXPIRY = "iot_token_expiry"
CONF_REFRESH_TOKEN_EXPIRY = "refresh_token_expiry"
