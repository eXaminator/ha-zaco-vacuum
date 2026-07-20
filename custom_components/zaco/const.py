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
        Platform.IMAGE,
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
FAST_POLL_INTERVAL = 1  # seconds — fast polling during active cleaning
MAP_POLL_INTERVAL = 300  # seconds — SLAM maps change rarely

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
    "Low": 0,
    "Standard": 1,
    "Strong": 2,
}
WATER_LEVELS_REVERSE = {v: k for k, v in WATER_LEVELS.items()}

# ---------------------------------------------------------------------------
# Remote control directions (CleanDirection property)
# ---------------------------------------------------------------------------
REMOTE_DIRECTIONS = {
    "forward": 1,
    "back": 2,
    "left": 3,
    "right": 4,
    "stop": 5,
}

# ---------------------------------------------------------------------------
# ErrorCode mapping (from ErrorEnum.java + strings.xml adapter_error_*)
# Hardware component-level errors, codes 0-30.
# ---------------------------------------------------------------------------
ERROR_CODE_MAP: dict[int, str] = {
    0: "No error",
    1: "Bumper error",
    2: "OBS is abnormal",
    3: "Edge cleaning parts error",
    4: "Detectors error",
    5: "Robot isn't on a flat surface",
    6: "Nosewheel error",
    7: "Left brush error",
    8: "Right brush error",
    9: "Side brush error",
    10: "Left side wheel error",
    11: "Right side wheel error",
    12: "Main brush error",
    13: "Fan error",
    14: "Water pump error",
    15: "Air pump error",
    16: "Dustbin error",
    17: "Water tank error",
    18: "Filter error",
    19: "Battery error",
    20: "Gyroscope error",
    21: "Radar error",
    22: "Camera error",
    23: "Robot is trapped",
    24: "Optical flow component error",
    25: "Other errors",
    26: "Environment is too dark",
    27: "Clean water tank is abnormal",
    28: "Dirt water tank is abnormal",
    29: "Low battery, fast mapping failed",
    30: "Battery is too low, please charge",
}

ERROR_CODE_MAP_DE: dict[int, str] = {
    0: "Kein Fehler",
    1: "Stoßfänger-Fehler",
    2: "OBS-Sensor ist fehlerhaft",
    3: "Kantenreinigungsteil-Fehler",
    4: "Sensoren-Fehler",
    5: "Roboter steht nicht auf ebenem Boden",
    6: "Lenkrad-Fehler",
    7: "Linke Bürste Fehler",
    8: "Rechte Bürste Fehler",
    9: "Seitenbürste Fehler",
    10: "Linkes Rad Fehler",
    11: "Rechtes Rad Fehler",
    12: "Hauptbürste Fehler",
    13: "Lüfter-Fehler",
    14: "Wasserpumpe Fehler",
    15: "Luftpumpe Fehler",
    16: "Staubbehälter-Fehler",
    17: "Wassertank-Fehler",
    18: "Filter-Fehler",
    19: "Akku-Fehler",
    20: "Gyroskop-Fehler",
    21: "Radar-Fehler",
    22: "Kamera-Fehler",
    23: "Roboter steckt fest",
    24: "Optischer Flusssensor Fehler",
    25: "Sonstige Fehler",
    26: "Umgebung ist zu dunkel",
    27: "Frischwassertank ist fehlerhaft",
    28: "Schmutzwassertank ist fehlerhaft",
    29: "Akku zu schwach, schnelle Kartierung fehlgeschlagen",
    30: "Akku ist zu schwach, bitte laden",
}

# ---------------------------------------------------------------------------
# Fault code mapping (from ErrorDevice.java + strings.xml fault_title_*)
# Operational/runtime faults, codes 100-2114.
# ---------------------------------------------------------------------------
FAULT_CODE_MAP: dict[int, str] = {
    0: "No fault",
    100: "System error",
    500: "Laser sensor error",
    501: "Wheels are blocked",
    502: "Low battery",
    503: "Please make sure the dust box is installed",
    504: "A strong magnetic field was detected",
    505: "Failure to start",
    506: "The wall sensor is covered with dust",
    507: "Positioning failure",
    508: "Robot is tilted",
    509: "Cliff sensor error",
    510: "Collision sensor error",
    511: "Couldn't find dock",
    512: "Couldn't find dock",
    513: "Navigation failed",
    514: "Robot is blocked or stuck",
    515: "Charging error",
    516: "Abnormal battery temperature",
    517: "Upgrading",
    518: "Low battery",
    519: "Main brush is blocked",
    520: "Side brush entangled",
    521: "No water tank installed",
    522: "Mop is not installed",
    523: "Filter abnormity",
    524: "Power switch not open",
    525: "Water tank is empty",
    526: "Mop may need to be cleaned",
    527: "Robot is blocked or stuck",
    529: "Water tank and mop cloth not installed",
    530: "Water tank not installed",
    533: "Standby too long, turned off",
    534: "Low battery, turned off",
    550: "Abnormal battery temperature",
    551: "Battery temperature returned to normal",
    559: "Failed to create map",
    560: "Abnormal side brush",
    561: "Visual recognition sensor abnormal",
    562: "Infrared wall sensor is abnormal",
    563: "Dust box not installed completely",
    564: "2-in-1 water tank not installed",
    565: "Radar blocked",
    566: "Water tank not installed completely",
    567: "Overcurrent protection",
    568: "Left wheel abnormal",
    569: "Right wheel abnormal",
    570: "Main brush abnormal",
    572: "Robot in restricted area",
    573: "Fan error",
    574: "Laser radar tangled or stuck",
    581: "Clear water tank is empty",
    582: "Dirty water tank is full",
    583: "Clear water tank not installed",
    584: "Dirty water tank not installed",
    585: "Dirty water sieve not installed",
    586: "Cleaning station sink full",
    587: "Connection to cleaning station abnormal",
    588: "Robot may be on carpet",
    589: "Positioning failure",
    590: "Dust collection failure",
    591: "Dust bag full",
    592: "Hardware failure, unable to collect dust",
    593: "Hardware failure, unable to collect dust",
    594: "Waiting for dust collection",
    611: "Positioning failure",
    612: "Positioning failure, map changed",
    2000: "Dust box needs cleaning",
    2001: "Left brush blocked",
    2002: "Right brush blocked",
    2003: "Battery too low for scheduled clean",
    2007: "Unable to reach target area, cleaning incomplete",
    2012: "Unable to reach some areas, cleaning incomplete",
    2013: "Not started from cleaning station",
    2014: "Carpet detection abnormality",
    2015: "Self-cleaning, try again later",
    2017: "Map creation failed",
    2100: "Cleaning not completed, recharging",
    2101: "Charging",
    2102: "Cleaning done, going home",
    2103: "Charging",
    2104: "Returning to dock",
    2105: "Charging completed",
    2106: "Low battery, charging",
    2107: "Scheduled cleaning in progress",
    2108: "Repositioning in progress",
    2109: "Second cleaning in progress",
    2110: "Device self-test in progress",
    2114: "Cleaning 15h continuously, clean dust box",
}

FAULT_CODE_MAP_DE: dict[int, str] = {
    0: "Kein Fehler",
    100: "Systemfehler",
    500: "Lasersensor-Fehler",
    501: "Räder blockiert",
    502: "Akku schwach",
    503: "Bitte Staubbehälter einsetzen",
    504: "Starkes Magnetfeld erkannt",
    505: "Start fehlgeschlagen",
    506: "Wandsensor ist verstaubt",
    507: "Positionierung fehlgeschlagen",
    508: "Roboter ist gekippt",
    509: "Absturzsensor-Fehler",
    510: "Kollisionssensor-Fehler",
    511: "Ladestation nicht gefunden",
    512: "Ladestation nicht gefunden",
    513: "Navigation fehlgeschlagen",
    514: "Roboter ist blockiert oder steckt fest",
    515: "Ladefehler",
    516: "Ungewöhnliche Akkutemperatur",
    517: "Update wird durchgeführt",
    518: "Akku schwach",
    519: "Hauptbürste blockiert",
    520: "Seitenbürste verheddert",
    521: "Kein Wassertank eingesetzt",
    522: "Wischmopp ist nicht angebracht",
    523: "Filter-Fehler",
    524: "Netzschalter nicht eingeschaltet",
    525: "Wassertank ist leer",
    526: "Wischmopp muss gereinigt werden",
    527: "Roboter ist blockiert oder steckt fest",
    529: "Wassertank und Wischmopp nicht eingesetzt",
    530: "Wassertank nicht eingesetzt",
    533: "Zu lange im Standby, ausgeschaltet",
    534: "Akku schwach, ausgeschaltet",
    550: "Ungewöhnliche Akkutemperatur",
    551: "Akkutemperatur wieder normal",
    559: "Kartenerstellung fehlgeschlagen",
    560: "Seitenbürste fehlerhaft",
    561: "Visueller Erkennungssensor fehlerhaft",
    562: "Infrarot-Wandsensor fehlerhaft",
    563: "Staubbehälter nicht vollständig eingesetzt",
    564: "2-in-1-Wassertank nicht eingesetzt",
    565: "Radar blockiert",
    566: "Wassertank nicht vollständig eingesetzt",
    567: "Überstromschutz",
    568: "Linkes Rad fehlerhaft",
    569: "Rechtes Rad fehlerhaft",
    570: "Hauptbürste fehlerhaft",
    572: "Roboter im Sperrbereich",
    573: "Lüfter-Fehler",
    574: "Laserradar verheddert oder blockiert",
    581: "Frischwassertank ist leer",
    582: "Schmutzwassertank ist voll",
    583: "Frischwassertank nicht eingesetzt",
    584: "Schmutzwassertank nicht eingesetzt",
    585: "Schmutzwassersieb nicht eingesetzt",
    586: "Reinigungsstation Becken voll",
    587: "Verbindung zur Reinigungsstation fehlerhaft",
    588: "Roboter steht möglicherweise auf Teppich",
    589: "Positionierung fehlgeschlagen",
    590: "Staubsammlung fehlgeschlagen",
    591: "Staubbeutel voll",
    592: "Hardwarefehler, Staubsammlung nicht möglich",
    593: "Hardwarefehler, Staubsammlung nicht möglich",
    594: "Warte auf Staubsammlung",
    611: "Positionierung fehlgeschlagen",
    612: "Positionierung fehlgeschlagen, Karte geändert",
    2000: "Staubbehälter muss gereinigt werden",
    2001: "Linke Bürste blockiert",
    2002: "Rechte Bürste blockiert",
    2003: "Akku zu schwach für geplante Reinigung",
    2007: "Zielbereich nicht erreichbar, Reinigung unvollständig",
    2012: "Einige Bereiche nicht erreichbar, Reinigung unvollständig",
    2013: "Nicht von Reinigungsstation gestartet",
    2014: "Teppicherkennung fehlerhaft",
    2015: "Selbstreinigung läuft, bitte später versuchen",
    2017: "Kartenerstellung fehlgeschlagen",
    2100: "Reinigung nicht abgeschlossen, wird aufgeladen",
    2101: "Wird geladen",
    2102: "Reinigung fertig, fährt zur Station",
    2103: "Wird geladen",
    2104: "Fährt zur Ladestation",
    2105: "Ladevorgang abgeschlossen",
    2106: "Akku schwach, wird geladen",
    2107: "Geplante Reinigung läuft",
    2108: "Neupositionierung läuft",
    2109: "Zweite Reinigung läuft",
    2110: "Geräte-Selbsttest läuft",
    2114: "15h Dauerbetrieb, Staubbehälter reinigen",
}

# ---------------------------------------------------------------------------
# StopCleanReason mapping (from HistoryDetailNewX9Activity.gerRealErrorTip())
# Codes found in CleanHistory.StopCleanReason — the only REST-accessible
# error signal on this firmware (ErrorCode and Fault are always null).
# ---------------------------------------------------------------------------
STOP_CLEAN_REASON_MAP: dict[int, str] = {
    1: "Cleaning finished",
    2: "Low battery, auto recharge paused",
    3: "Remote control disabled",
    4: "Paused by button press",
    5: "Paused by app",
    6: "Robot got stuck and stopped",
    7: "Error occurred during cleaning",
    8: "Cleaning uncompleted",
    9: "Not on charging station",
    10: "Explore mode completed",
}

STOP_CLEAN_REASON_MAP_DE: dict[int, str] = {
    1: "Reinigung abgeschlossen",
    2: "Akku schwach, automatisches Aufladen",
    3: "Fernbedienung deaktiviert",
    4: "Durch Tastendruck pausiert",
    5: "Durch App pausiert",
    6: "Der Roboter befindet sich nicht auf einem Boden",
    7: "Fehler während der Reinigung",
    8: "Reinigung unvollständig",
    9: "Nicht auf der Ladestation",
    10: "Erkundungsmodus abgeschlossen",
}

# StopCleanReason codes that indicate an actionable error (needs user intervention).
# Excludes 2 (low battery, self-recovers) and 3 (remote disabled, informational).
STOP_CLEAN_REASON_ERROR: set[int] = {6, 7, 8}

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
    "BeepVolume",
    "CarpetControl",
    "ContinueCleanSwitch",
    "CleanHistory",
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
    "WiFiInfo",
]

ALL_PROPERTIES = CORE_PROPERTIES + MAP_PROPERTIES + ROOM_PROPERTIES + CONSUMABLE_PROPERTIES

# Normal polls — everything except heavy SLAM map data.
STATE_PROPERTIES = CORE_PROPERTIES + ROOM_PROPERTIES + CONSUMABLE_PROPERTIES

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
