#!/usr/bin/env python3
"""
test_research.py - Comprehensive API & MQTT research session.

Systematically tests every known ZACO A10 feature while observing both REST
API responses and MQTT push messages with precise timestamps.

Phases:
    0  Setup & baseline
    1  Comprehensive property dump
    2  Settings read/write tests
    3  UploadDataControl timing
    4  SoundLocate test
    5  Room cleaning (Küche)
    6  Zone cleaning (Wohnzimmer)
    7  Edge clean (Esszimmer)
    8  Remote control test
    9  Explore mode test
   10  Timeline API deep dive
   11  Schedule reading
   12  MQTT push audit

Usage:
    python3 scripts/test_research.py                 # all phases
    python3 scripts/test_research.py --phase 1       # single phase
    python3 scripts/test_research.py --no-movement   # docked-only (0-4,10-12)
    python3 scripts/test_research.py -v              # verbose
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Import load_tokens BEFORE adding HA path (avoids select.py shadow on asyncio)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_aliyun import load_tokens, DEFAULT_IOT_ID, AliyunIoTClient, IOT_HOST_DEFAULT

# Import Zaco from the HA integration
HA_PATH = str(
    Path(__file__).resolve().parent.parent
    / "ha_integration"
    / "custom_components"
    / "zaco"
)
sys.path.insert(0, HA_PATH)
from zaco import Zaco
from const import WORKMODE_IDLE, WORKMODE_CLEANING, WORKMODE_PAUSED, WORKMODE_RETURNING

_LOGGER = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = PROJECT_DIR / "docs"


# ---------------------------------------------------------------------------
# Experiment Logger
# ---------------------------------------------------------------------------

class ExperimentLogger:
    """Records events with ms timestamps for later analysis."""

    def __init__(self):
        self.events: list[dict[str, Any]] = []
        self._start = time.monotonic()
        self._start_wall = time.time()

    def log(self, category: str, event: str, data: Any = None) -> None:
        entry = {
            "t": round(time.monotonic() - self._start, 3),
            "wall": round(time.time(), 3),
            "category": category,
            "event": event,
        }
        if data is not None:
            entry["data"] = data
        self.events.append(entry)
        # Print concisely
        ts = f"[{entry['t']:8.2f}s]"
        detail = ""
        if data is not None:
            s = str(data)
            if len(s) > 120:
                s = s[:120] + "..."
            detail = f" | {s}"
        print(f"{ts} [{category}] {event}{detail}")

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.events, indent=2, default=str))
        print(f"\nLog saved to {path}")


# ---------------------------------------------------------------------------
# MQTT Interceptor
# ---------------------------------------------------------------------------

class MqttInterceptor:
    """Wraps Zaco.merge_mqtt_push to log MQTT messages."""

    def __init__(self, zaco: Zaco, logger: ExperimentLogger):
        self._zaco = zaco
        self._logger = logger
        self._original_merge = zaco.merge_mqtt_push
        self.push_log: list[dict[str, Any]] = []
        zaco.merge_mqtt_push = self._intercepted_merge

    def _intercepted_merge(self, items: dict[str, Any]) -> None:
        ts = time.time()
        prop_names = list(items.keys())
        self.push_log.append({"time": ts, "properties": prop_names, "items": items})
        self._logger.log("MQTT_PUSH", f"Properties: {prop_names}")
        self._original_merge(items)

    def clear(self) -> None:
        self.push_log.clear()

    def restore(self) -> None:
        self._zaco.merge_mqtt_push = self._original_merge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_int(data: dict | None, key: str) -> int | None:
    if data is None:
        return None
    raw = data.get(key, {})
    val = raw.get("value", raw) if isinstance(raw, dict) else raw
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def prop_value(data: dict | None, key: str) -> Any:
    """Extract the value from a property dict, handling nested {value:...} format."""
    if data is None:
        return None
    raw = data.get(key)
    if raw is None:
        return None
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    return raw


def state_label(wm: int | None) -> str:
    if wm is None:
        return "UNKNOWN"
    if wm in WORKMODE_IDLE:
        return "IDLE"
    if wm in WORKMODE_CLEANING:
        return "CLEANING"
    if wm in WORKMODE_PAUSED:
        return "PAUSED"
    if wm in WORKMODE_RETURNING:
        return "RETURNING"
    return f"OTHER({wm})"


async def wait_for_idle(
    zaco: Zaco, logger: ExperimentLogger, timeout: int = 300, poll: float = 5.0,
) -> bool:
    """Wait until robot is idle/docked. Returns True if idle."""
    t0 = time.monotonic()
    await asyncio.sleep(5)  # initial grace
    while time.monotonic() - t0 < timeout:
        await zaco.refresh(fast=False)
        wm = parse_int(zaco.data, "WorkMode")
        bat = parse_int(zaco.data, "BatteryState")
        logger.log("POLL", f"WM={wm} ({state_label(wm)}) bat={bat}%")
        if wm in WORKMODE_IDLE:
            return True
        await asyncio.sleep(poll)
    return False


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------

async def phase_0_setup(zaco: Zaco, logger: ExperimentLogger, sync_client: AliyunIoTClient) -> None:
    """Phase 0: Setup & baseline."""
    logger.log("PHASE", "=== Phase 0: Setup & Baseline ===")

    await zaco.refresh(fast=False)
    wm = parse_int(zaco.data, "WorkMode")
    bat = parse_int(zaco.data, "BatteryState")
    fan = parse_int(zaco.data, "FanPower")
    ps = parse_int(zaco.data, "PowerSwitch")
    paus = parse_int(zaco.data, "PauseSwitch")

    logger.log("STATE", "Initial state", {
        "WorkMode": wm, "BatteryState": bat, "FanPower": fan,
        "PowerSwitch": ps, "PauseSwitch": paus,
    })

    if wm not in WORKMODE_IDLE:
        logger.log("ERROR", f"Robot not idle (WM={wm}). Aborting.")
        raise RuntimeError("Robot must be docked/idle to start research session")

    if bat is not None and bat < 20:
        logger.log("ERROR", f"Battery too low ({bat}%). Aborting.")
        raise RuntimeError("Battery too low")

    # Set fan power to minimum
    logger.log("CMD", "Setting FanPower=1")
    await zaco.set_fan_power(1)
    await asyncio.sleep(2)
    await zaco.refresh(fast=False)
    fan = parse_int(zaco.data, "FanPower")
    logger.log("STATE", f"FanPower after set: {fan}")

    # Start MQTT
    logger.log("CMD", "Starting MQTT")
    mqtt_ok = await zaco.start_mqtt()
    logger.log("STATE", f"MQTT connected: {mqtt_ok}")
    if mqtt_ok:
        await asyncio.sleep(5)  # let bind complete

    # Show rooms
    rooms = zaco.rooms
    logger.log("INFO", f"Rooms: {rooms}")


async def phase_1_property_dump(zaco: Zaco, logger: ExperimentLogger, mqtt: MqttInterceptor, sync_client: AliyunIoTClient) -> None:
    """Phase 1: Comprehensive property dump — read ALL known properties."""
    logger.log("PHASE", "=== Phase 1: Comprehensive Property Dump ===")

    # All properties from APK analysis
    all_props = [
        # Core state
        "WorkMode", "BatteryState", "FanPower", "WaterTankContrl", "PowerSwitch",
        "PauseSwitch", "ErrorCode", "Fault", "CleanType", "CleanLoop", "CleanTime",
        "CleanArea", "VacWateState", "CurrentMode",
        # Settings
        "BeepVolume", "BeepType", "BeepNoDisturb", "CarpetControl",
        "ContinueCleanSwitch", "SideBrushPower", "MainBrushPower", "MaxMode",
        "PowerMode", "FunctionSwitch", "WheelSpeed", "CleaningEfficiency",
        # Device info
        "WiFiInfo", "HardwareVer", "SoftwareVer", "InitStatus", "RobotInfo",
        "AppRemind",
        # Statistics
        "TotalCleanedInfo", "StatisticalData", "Maintenance", "PartsStatus",
        # Clean settings
        "NormalCleanSettings", "CleanSettings_1", "CleanSettings_2",
        "CleanSettingsManager",
        # Schedules
        "Schedule1", "Schedule2", "Schedule3", "Schedule4", "Schedule5",
        "Schedule6", "Schedule7",
        # Map
        "SaveMap", "ChargerPoint", "ForbiddenAreaData", "VirtualWallData",
        "VirtualWallEN",
        # Other
        "UploadDataControl", "ContinueCleanStatus", "LongConnection", "PointToGo",
        "SoundLocate", "CleanDirection",
        # Extended — may not exist
        "BatteryStateInfo", "CleanHistory", "CleanHistoryStartTime",
        "RealTimeObjectInfo", "DrawSlamDone", "OTAInfo",
    ]

    # Use async client to avoid blocking the event loop
    client = zaco.client
    iot_id = zaco.iot_id
    chunk_size = 20
    results: dict[str, Any] = {}
    existing: list[str] = []
    missing: list[str] = []

    for i in range(0, len(all_props), chunk_size):
        chunk = all_props[i:i + chunk_size]
        logger.log("REST_GET", f"Reading properties batch {i // chunk_size + 1}: {chunk}")
        data = await client.get_properties(iot_id, chunk)
        if data:
            for key in chunk:
                val = data.get(key)
                if val is not None:
                    results[key] = val
                    existing.append(key)
                else:
                    missing.append(key)

    logger.log("RESULT", f"Existing properties ({len(existing)})", existing)
    logger.log("RESULT", f"Missing/null properties ({len(missing)})", missing)

    # Log values of existing properties
    for key in sorted(results.keys()):
        raw = results[key]
        val = raw.get("value", raw) if isinstance(raw, dict) else raw
        ts = raw.get("time", "") if isinstance(raw, dict) else ""
        val_str = json.dumps(val, default=str) if isinstance(val, (dict, list)) else str(val)
        if len(val_str) > 200:
            val_str = val_str[:200] + "..."
        logger.log("PROP", f"{key} = {val_str}", {"time": ts} if ts else None)


async def phase_2_settings(zaco: Zaco, logger: ExperimentLogger, mqtt: MqttInterceptor, sync_client: AliyunIoTClient) -> None:
    """Phase 2: Settings read/write tests."""
    logger.log("PHASE", "=== Phase 2: Settings Read/Write Tests ===")

    async def test_setting(name: str, test_values: list, restore_to: Any = None):
        """Test a single setting: read, write test values, observe, restore."""
        logger.log("TEST", f"--- Testing {name} ---")
        await zaco.refresh(fast=False)
        original = prop_value(zaco.data, name)
        logger.log("READ", f"{name} current value: {original}")

        for val in test_values:
            if val == original:
                continue  # skip if already the current value
            mqtt.clear()
            t0 = time.monotonic()

            logger.log("CMD", f"Setting {name}={val}")
            ok = await zaco.set_properties({name: val})
            set_time = time.monotonic() - t0
            logger.log("REST_SET", f"Response: {'ok' if ok else 'FAILED'} ({set_time:.3f}s)")

            if not ok:
                logger.log("ERROR", f"Failed to set {name}={val}")
                continue

            # Wait for value to propagate
            await asyncio.sleep(2)

            # Check REST
            await zaco.refresh(fast=False)
            new_val = prop_value(zaco.data, name)
            rest_latency = time.monotonic() - t0
            logger.log("REST_GET", f"{name} after set: {new_val} (latency {rest_latency:.1f}s)")

            # Check MQTT pushes
            if mqtt.push_log:
                for push in mqtt.push_log:
                    logger.log("MQTT_TIMING", f"Push received {push['time'] - (t0 + time.time() - time.monotonic()):.3f}s after command", push["properties"])
            else:
                logger.log("MQTT_TIMING", f"No MQTT push for {name} within 2s")

            await asyncio.sleep(1)

        # Restore original
        restore_val = restore_to if restore_to is not None else original
        if restore_val is not None and restore_val != prop_value(zaco.data, name):
            logger.log("RESTORE", f"Restoring {name} to {restore_val}")
            await zaco.set_properties({name: restore_val})
            await asyncio.sleep(1)

    # Test each setting
    await test_setting("FanPower", [1, 25, 50, 75, 100], restore_to=1)
    await test_setting("WaterTankContrl", [0, 1, 2, 3], restore_to=0)
    await test_setting("MaxMode", [0, 1], restore_to=0)
    await test_setting("CarpetControl", [0, 1])
    await test_setting("ContinueCleanSwitch", [0, 1])
    await test_setting("BeepVolume", [0, 25, 50, 100])

    # PowerMode — may not exist on all devices
    await zaco.refresh(fast=False)
    if prop_value(zaco.data, "PowerMode") is not None:
        await test_setting("PowerMode", [1, 2, 3, 4])
    else:
        logger.log("SKIP", "PowerMode not available on this device")

    # SideBrushPower / MainBrushPower
    if prop_value(zaco.data, "SideBrushPower") is not None:
        await test_setting("SideBrushPower", [25, 50, 75])
    else:
        logger.log("SKIP", "SideBrushPower not available")

    if prop_value(zaco.data, "MainBrushPower") is not None:
        await test_setting("MainBrushPower", [25, 50, 75])
    else:
        logger.log("SKIP", "MainBrushPower not available")

    # BeepType — be careful, language change is audible
    if prop_value(zaco.data, "BeepType") is not None:
        current_beep = prop_value(zaco.data, "BeepType")
        logger.log("READ", f"BeepType current: {current_beep}")
        # Don't change language, just log it
        logger.log("SKIP", "Not changing BeepType (language) to avoid confusion")
    else:
        logger.log("SKIP", "BeepType not available")

    # BeepNoDisturb — read and toggle
    await zaco.refresh(fast=False)
    dnd = prop_value(zaco.data, "BeepNoDisturb")
    if dnd is not None:
        logger.log("READ", f"BeepNoDisturb current: {dnd}")
        if isinstance(dnd, dict):
            current_switch = dnd.get("Switch", 0)
            # Toggle DND
            new_dnd = dict(dnd)
            new_dnd["Switch"] = 1 - current_switch
            await test_setting("BeepNoDisturb", [new_dnd], restore_to=dnd)
        elif isinstance(dnd, str):
            try:
                parsed = json.loads(dnd)
                logger.log("READ", f"BeepNoDisturb parsed: {parsed}")
            except json.JSONDecodeError:
                logger.log("READ", f"BeepNoDisturb is string: {dnd}")
    else:
        logger.log("SKIP", "BeepNoDisturb not available")


async def phase_3_upload_control(zaco: Zaco, logger: ExperimentLogger, mqtt: MqttInterceptor, sync_client: AliyunIoTClient) -> None:
    """Phase 3: UploadDataControl timing — measure push frequencies."""
    logger.log("PHASE", "=== Phase 3: UploadDataControl Timing ===")

    # Measure baseline (fast upload disabled)
    logger.log("CMD", "Ensuring UploadDataControl disabled")
    await zaco.set_properties({"UploadDataControl": {"Status": 0, "ValidityTime": 210}})
    await asyncio.sleep(2)

    logger.log("TEST", "Baseline measurement (30s, fast upload OFF)")
    mqtt.clear()
    t0 = time.monotonic()
    rest_updates = []
    while time.monotonic() - t0 < 30:
        await asyncio.sleep(3)
        await zaco.refresh(fast=False)
        wm = parse_int(zaco.data, "WorkMode")
        rest_updates.append({"t": round(time.monotonic() - t0, 1), "wm": wm})

    baseline_mqtt = len(mqtt.push_log)
    baseline_rest = len(rest_updates)
    logger.log("RESULT", f"Baseline: {baseline_mqtt} MQTT pushes, {baseline_rest} REST polls in 30s")
    for push in mqtt.push_log:
        logger.log("DETAIL", f"  Push: {push['properties']}")

    # Enable fast upload
    logger.log("CMD", "Enabling UploadDataControl (Status=1, ValidityTime=210)")
    mqtt.clear()
    await zaco.set_properties({"UploadDataControl": {"Status": 1, "ValidityTime": 210}})
    await asyncio.sleep(2)

    logger.log("TEST", "Fast upload measurement (60s)")
    t0 = time.monotonic()
    rest_updates = []
    while time.monotonic() - t0 < 60:
        await asyncio.sleep(3)
        await zaco.refresh(fast=False)
        wm = parse_int(zaco.data, "WorkMode")
        rest_updates.append({"t": round(time.monotonic() - t0, 1), "wm": wm})

    fast_mqtt = len(mqtt.push_log)
    fast_rest = len(rest_updates)
    logger.log("RESULT", f"Fast upload: {fast_mqtt} MQTT pushes, {fast_rest} REST polls in 60s")
    for push in mqtt.push_log:
        logger.log("DETAIL", f"  Push at t={push['time']:.3f}: {push['properties']}")

    # Disable again
    logger.log("CMD", "Disabling UploadDataControl")
    await zaco.set_properties({"UploadDataControl": {"Status": 0, "ValidityTime": 210}})


async def phase_4_locate(zaco: Zaco, logger: ExperimentLogger, mqtt: MqttInterceptor, sync_client: AliyunIoTClient) -> None:
    """Phase 4: SoundLocate test."""
    logger.log("PHASE", "=== Phase 4: SoundLocate Test ===")

    mqtt.clear()
    logger.log("CMD", "Sending SoundLocate via facade (locate())")
    t0 = time.monotonic()
    ok = await zaco.locate()
    elapsed = time.monotonic() - t0
    logger.log("REST_SET", f"Response: {'ok' if ok else 'FAILED'} ({elapsed:.3f}s)")

    await asyncio.sleep(3)

    # Also try direct REST (via async client to avoid blocking)
    mqtt.clear()
    logger.log("CMD", "Sending SoundLocate via direct async client")
    t0 = time.monotonic()
    resp = await zaco.client.set_properties(zaco.iot_id, {"SoundLocate": {"SoundDir": 0}})
    elapsed = time.monotonic() - t0
    logger.log("REST_SET", f"Direct REST response: {resp} ({elapsed:.3f}s)")

    await asyncio.sleep(3)
    if mqtt.push_log:
        logger.log("MQTT_TIMING", f"MQTT pushes after locate: {len(mqtt.push_log)}")
    else:
        logger.log("MQTT_TIMING", "No MQTT push after SoundLocate")


async def phase_5_room_clean(zaco: Zaco, logger: ExperimentLogger, mqtt: MqttInterceptor, sync_client: AliyunIoTClient) -> None:
    """Phase 5: Room cleaning (Küche) with detailed state monitoring."""
    logger.log("PHASE", "=== Phase 5: Room Cleaning — Küche ===")

    room = "Küche"
    room_id = zaco.get_room_id(room)
    if room_id is None:
        logger.log("ERROR", f"Room '{room}' not found. Available: {list(zaco.rooms.keys())}")
        return

    # Ensure fan power is minimal
    await zaco.set_fan_power(1)
    await asyncio.sleep(1)

    # Start room clean
    mqtt.clear()
    logger.log("CMD", f"Starting room clean: {room} (id={room_id}), 1 pass, FanPower=1")
    t0 = time.monotonic()
    ok = await zaco.clean_rooms([room], passes=1)
    logger.log("REST_SET", f"clean_rooms response: {'ok' if ok else 'FAILED'}")

    if not ok:
        logger.log("ERROR", "Failed to start room clean")
        return

    # Monitor state transitions for ~60 seconds
    logger.log("TEST", "Monitoring cleaning state transitions (60s)")
    transitions: list[dict] = []
    last_wm = None
    monitor_end = time.monotonic() + 60

    while time.monotonic() < monitor_end:
        await asyncio.sleep(1)
        await zaco.refresh(fast=True)
        wm = parse_int(zaco.data, "WorkMode")
        bat = parse_int(zaco.data, "BatteryState")
        ps = parse_int(zaco.data, "PowerSwitch")
        paus = parse_int(zaco.data, "PauseSwitch")
        elapsed = round(time.monotonic() - t0, 1)

        if wm != last_wm:
            logger.log("TRANSITION", f"WorkMode {last_wm} -> {wm} ({state_label(wm)}) at {elapsed}s", {
                "PowerSwitch": ps, "PauseSwitch": paus, "Battery": bat,
            })
            transitions.append({
                "t": elapsed, "from": last_wm, "to": wm,
                "PowerSwitch": ps, "PauseSwitch": paus, "Battery": bat,
            })
            last_wm = wm
        elif int(elapsed) % 10 == 0:
            logger.log("POLL", f"WM={wm} ({state_label(wm)}) bat={bat}% PS={ps} Pause={paus} @ {elapsed}s")

    # Pause
    mqtt.clear()
    logger.log("CMD", "Sending pause (WorkMode=2)")
    t_pause = time.monotonic()
    await zaco.stop()  # WorkMode 2 = stop/standby
    await asyncio.sleep(1)

    # Monitor pause transition
    for _ in range(15):
        await asyncio.sleep(1)
        await zaco.refresh(fast=True)
        wm = parse_int(zaco.data, "WorkMode")
        ps = parse_int(zaco.data, "PowerSwitch")
        paus = parse_int(zaco.data, "PauseSwitch")
        elapsed = round(time.monotonic() - t_pause, 1)
        logger.log("POLL", f"After pause cmd: WM={wm} ({state_label(wm)}) PS={ps} Pause={paus} @ {elapsed}s")
        if wm != last_wm:
            logger.log("TRANSITION", f"WorkMode {last_wm} -> {wm} ({state_label(wm)})", {
                "PowerSwitch": ps, "PauseSwitch": paus,
            })
            transitions.append({"t": elapsed, "from": last_wm, "to": wm, "context": "after_pause"})
            last_wm = wm
        if wm in WORKMODE_PAUSED or wm in WORKMODE_IDLE:
            break

    # Also try PauseSwitch=1 if robot is still moving
    wm = parse_int(zaco.data, "WorkMode")
    if wm in WORKMODE_CLEANING:
        logger.log("CMD", "Robot still cleaning, trying PauseSwitch=1")
        await zaco.set_properties({"PauseSwitch": 1})
        await asyncio.sleep(5)
        await zaco.refresh(fast=True)
        wm = parse_int(zaco.data, "WorkMode")
        logger.log("STATE", f"After PauseSwitch=1: WM={wm} ({state_label(wm)})")

    # Wait 10 seconds in paused state
    logger.log("TEST", "Observing paused state for 10s")
    for _ in range(10):
        await asyncio.sleep(1)
        await zaco.refresh(fast=True)
        wm = parse_int(zaco.data, "WorkMode")
        if wm != last_wm:
            logger.log("TRANSITION", f"WorkMode {last_wm} -> {wm}")
            last_wm = wm

    # Resume
    mqtt.clear()
    logger.log("CMD", "Sending resume (WorkMode=21)")
    t_resume = time.monotonic()
    await zaco.set_properties({"WorkMode": 21})

    # Monitor resume for 30s
    for _ in range(30):
        await asyncio.sleep(1)
        await zaco.refresh(fast=True)
        wm = parse_int(zaco.data, "WorkMode")
        elapsed = round(time.monotonic() - t_resume, 1)
        if wm != last_wm:
            logger.log("TRANSITION", f"WorkMode {last_wm} -> {wm} ({state_label(wm)}) @ {elapsed}s")
            transitions.append({"t": elapsed, "from": last_wm, "to": wm, "context": "after_resume"})
            last_wm = wm

    # Return to dock
    mqtt.clear()
    logger.log("CMD", "Sending return to dock (WorkMode=8)")
    t_return = time.monotonic()
    await zaco.return_to_base()

    # Monitor return
    logger.log("TEST", "Monitoring return to dock (max 300s)")
    for _ in range(300):
        await asyncio.sleep(2)
        await zaco.refresh(fast=True)
        wm = parse_int(zaco.data, "WorkMode")
        bat = parse_int(zaco.data, "BatteryState")
        elapsed = round(time.monotonic() - t_return, 1)

        if wm != last_wm:
            logger.log("TRANSITION", f"WorkMode {last_wm} -> {wm} ({state_label(wm)}) @ {elapsed}s")
            transitions.append({"t": elapsed, "from": last_wm, "to": wm, "context": "returning"})
            last_wm = wm

        if wm in WORKMODE_IDLE:
            logger.log("STATE", f"Robot docked after {elapsed}s, bat={bat}%")
            break
    else:
        logger.log("ERROR", "Timeout waiting for dock")

    logger.log("RESULT", f"Total transitions recorded: {len(transitions)}", transitions)
    logger.log("RESULT", f"Total MQTT pushes during phase: {len(mqtt.push_log)}")


async def phase_6_zone_clean(zaco: Zaco, logger: ExperimentLogger, mqtt: MqttInterceptor, sync_client: AliyunIoTClient) -> None:
    """Phase 6: Zone cleaning in Wohnzimmer."""
    logger.log("PHASE", "=== Phase 6: Zone Cleaning — Wohnzimmer ===")

    center = zaco.get_room_center("Wohnzimmer")
    if center is None:
        logger.log("ERROR", "Room 'Wohnzimmer' not found")
        return

    x, y = center
    # Small zone: 15x15 units around center
    half = 7
    x1, y1 = x - half, y - half
    x2, y2 = x + half, y + half

    await zaco.set_fan_power(1)
    await asyncio.sleep(1)

    mqtt.clear()
    logger.log("CMD", f"Starting zone clean at ({x},{y}) zone=({x1},{y1})->({x2},{y2}), 1 pass")
    t0 = time.monotonic()
    ok = await zaco.clean_zone(x1, y1, x2, y2, passes=1)
    logger.log("REST_SET", f"clean_zone response: {'ok' if ok else 'FAILED'}")

    if not ok:
        logger.log("ERROR", "Failed to start zone clean")
        return

    # Monitor for 45 seconds
    transitions: list[dict] = []
    last_wm = None

    for _ in range(45):
        await asyncio.sleep(1)
        await zaco.refresh(fast=True)
        wm = parse_int(zaco.data, "WorkMode")
        bat = parse_int(zaco.data, "BatteryState")
        elapsed = round(time.monotonic() - t0, 1)

        if wm != last_wm:
            logger.log("TRANSITION", f"WM {last_wm} -> {wm} ({state_label(wm)}) @ {elapsed}s")
            transitions.append({"t": elapsed, "from": last_wm, "to": wm})
            last_wm = wm

    # Pause and return
    wm = parse_int(zaco.data, "WorkMode")
    if wm not in WORKMODE_IDLE:
        logger.log("CMD", "Sending stop then return to dock")
        await zaco.stop()
        await asyncio.sleep(3)
        await zaco.return_to_base()

        # Wait for dock
        docked = await wait_for_idle(zaco, logger, timeout=300)
        if not docked:
            logger.log("ERROR", "Timeout waiting for dock after zone clean")
    else:
        logger.log("STATE", f"Robot already idle (WM={wm})")

    logger.log("RESULT", f"Zone clean transitions: {transitions}")
    logger.log("RESULT", f"MQTT pushes during zone clean: {len(mqtt.push_log)}")


async def phase_7_edge_clean(zaco: Zaco, logger: ExperimentLogger, mqtt: MqttInterceptor, sync_client: AliyunIoTClient) -> None:
    """Phase 7: Edge clean in Esszimmer."""
    logger.log("PHASE", "=== Phase 7: Edge Clean — Esszimmer ===")

    center = zaco.get_room_center("Esszimmer")
    if center is None:
        logger.log("ERROR", "Room 'Esszimmer' not found")
        return

    x, y = center

    await zaco.set_fan_power(1)
    await asyncio.sleep(1)

    # First navigate to room (small goto zone)
    mqtt.clear()
    logger.log("CMD", f"Navigating to Esszimmer center ({x},{y})")
    t0 = time.monotonic()

    # Use small zone for goto
    from zaco.zone_utils import encode_clean_area, rect_to_corners
    half = 1
    corners = rect_to_corners(x - half, y - half, x + half, y + half)
    area_data = encode_clean_area(*corners)
    await zaco.set_properties({
        "CleanAreaData": {"AreaData": area_data, "CleanLoop": 1, "Enable": 1}
    })

    # Wait for zone start (WM 19)
    last_wm = None
    for _ in range(30):
        await asyncio.sleep(1)
        await zaco.refresh(fast=True)
        wm = parse_int(zaco.data, "WorkMode")
        if wm != last_wm:
            logger.log("TRANSITION", f"WM {last_wm} -> {wm} ({state_label(wm)})")
            last_wm = wm
        if wm == 19:
            break

    # Pause and switch to edge clean
    logger.log("CMD", "Pausing (WM=2) then starting edge clean (WM=4)")
    await zaco.set_properties({"WorkMode": 2})
    await asyncio.sleep(3)
    mqtt.clear()
    t_edge = time.monotonic()
    await zaco.set_properties({"WorkMode": 4})

    # Monitor edge clean for 60 seconds
    transitions: list[dict] = []
    last_wm = None
    for _ in range(60):
        await asyncio.sleep(1)
        await zaco.refresh(fast=True)
        wm = parse_int(zaco.data, "WorkMode")
        elapsed = round(time.monotonic() - t_edge, 1)

        if wm != last_wm:
            logger.log("TRANSITION", f"WM {last_wm} -> {wm} ({state_label(wm)}) @ {elapsed}s")
            transitions.append({"t": elapsed, "from": last_wm, "to": wm})
            last_wm = wm

    # Return to dock
    wm = parse_int(zaco.data, "WorkMode")
    if wm not in WORKMODE_IDLE:
        logger.log("CMD", "Sending return to dock")
        await zaco.stop()
        await asyncio.sleep(2)
        await zaco.return_to_base()
        docked = await wait_for_idle(zaco, logger, timeout=300)
        if not docked:
            logger.log("ERROR", "Timeout waiting for dock after edge clean")

    logger.log("RESULT", f"Edge clean transitions: {transitions}")
    logger.log("RESULT", f"MQTT pushes during edge clean: {len(mqtt.push_log)}")


async def phase_8_remote_control(zaco: Zaco, logger: ExperimentLogger, mqtt: MqttInterceptor, sync_client: AliyunIoTClient) -> None:
    """Phase 8: Remote control test — try WorkMode 10 + CleanDirection."""
    logger.log("PHASE", "=== Phase 8: Remote Control Test ===")

    # Make sure robot is idle
    await zaco.refresh(fast=False)
    wm = parse_int(zaco.data, "WorkMode")
    if wm not in WORKMODE_IDLE:
        logger.log("ERROR", f"Robot not idle (WM={wm}), skipping remote control test")
        return

    # Try WorkMode 10
    mqtt.clear()
    logger.log("CMD", "Setting WorkMode=10 (remote control)")
    t0 = time.monotonic()
    ok = await zaco.set_properties({"WorkMode": 10})
    logger.log("REST_SET", f"Response: {'ok' if ok else 'FAILED'}")

    await asyncio.sleep(3)
    await zaco.refresh(fast=True)
    wm = parse_int(zaco.data, "WorkMode")
    logger.log("STATE", f"WorkMode after setting 10: {wm} ({state_label(wm)})")

    if wm == 10:
        # Try directions briefly
        for direction, name in [(5, "pause"), (1, "forward"), (5, "pause")]:
            logger.log("CMD", f"CleanDirection={direction} ({name})")
            await zaco.set_properties({"CleanDirection": direction})
            await asyncio.sleep(2)
            await zaco.refresh(fast=True)
            wm2 = parse_int(zaco.data, "WorkMode")
            logger.log("STATE", f"WM={wm2} after direction {direction}")

        # Stop
        logger.log("CMD", "Stopping remote control (WorkMode=2)")
        await zaco.set_properties({"WorkMode": 2})
    else:
        logger.log("RESULT", f"WorkMode 10 not accepted (robot reports WM={wm})")

    await asyncio.sleep(3)
    await zaco.refresh(fast=False)
    wm = parse_int(zaco.data, "WorkMode")
    logger.log("STATE", f"Final state after remote control test: WM={wm}")

    if mqtt.push_log:
        logger.log("RESULT", f"MQTT pushes: {len(mqtt.push_log)}")
    else:
        logger.log("RESULT", "No MQTT pushes during remote control test")


async def phase_9_explore(zaco: Zaco, logger: ExperimentLogger, mqtt: MqttInterceptor, sync_client: AliyunIoTClient) -> None:
    """Phase 9: Explore mode test — SKIPPED (destructive).

    WorkMode 22/23 = "create new map" per APK's onEnsureNewMap() flow.
    Sending WM 22 destroys the saved room map. Do NOT send these.
    """
    logger.log("PHASE", "=== Phase 9: Explore Mode Test ===")
    logger.log("SKIP", "WorkMode 22/23 = new map creation (destroys saved map). Skipped.")


async def phase_10_timeline(zaco: Zaco, logger: ExperimentLogger, _mqtt: MqttInterceptor, sync_client: AliyunIoTClient) -> None:
    """Phase 10: Timeline API deep dive."""
    logger.log("PHASE", "=== Phase 10: Timeline API Deep Dive ===")

    now_ms = int(time.time() * 1000)
    # Look at the last 30 minutes
    start_ms = now_ms - 30 * 60 * 1000
    end_ms = now_ms + 60_000

    # Test multiple properties
    test_properties = [
        "RealMapRoadData", "WorkMode", "BatteryState", "FanPower",
        "ErrorCode", "CleanHistory", "PowerSwitch", "PauseSwitch",
        "RealTimeRoadStart",
    ]

    client = zaco.client
    iot_id = zaco.iot_id

    for prop_name in test_properties:
        logger.log("CMD", f"Timeline query: {prop_name} (last 30 min)")
        t0 = time.monotonic()
        items = await client.get_property_timeline(iot_id, prop_name, start_ms, end_ms)
        elapsed = time.monotonic() - t0
        logger.log("REST_GET", f"{prop_name}: {len(items)} items ({elapsed:.2f}s)")

        if items:
            # Show first and last timestamps
            first_ts = items[0].get("timestamp", 0)
            last_ts = items[-1].get("timestamp", 0)
            span_s = (last_ts - first_ts) / 1000 if last_ts > first_ts else 0

            # Show sample data
            sample = items[-1].get("data")
            if isinstance(sample, str):
                try:
                    sample = json.loads(sample)
                except json.JSONDecodeError:
                    pass
            sample_str = json.dumps(sample, default=str)
            if len(sample_str) > 200:
                sample_str = sample_str[:200] + "..."

            logger.log("RESULT", f"{prop_name}: {len(items)} entries, span={span_s:.0f}s, sample={sample_str}")

            # For RealMapRoadData, calculate avg interval
            if prop_name == "RealMapRoadData" and len(items) >= 2:
                timestamps = [it.get("timestamp", 0) for it in items]
                intervals = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
                avg_interval = sum(intervals) / len(intervals) / 1000
                min_interval = min(intervals) / 1000
                max_interval = max(intervals) / 1000
                logger.log("RESULT", f"RoadData intervals: avg={avg_interval:.1f}s min={min_interval:.1f}s max={max_interval:.1f}s")
        else:
            logger.log("RESULT", f"{prop_name}: NO timeline data available")


async def phase_11_schedules(zaco: Zaco, logger: ExperimentLogger, mqtt: MqttInterceptor, sync_client: AliyunIoTClient) -> None:
    """Phase 11: Schedule reading."""
    logger.log("PHASE", "=== Phase 11: Schedule Reading ===")

    schedule_props = [f"Schedule{i}" for i in range(1, 8)]
    data = await zaco.client.get_properties(zaco.iot_id, schedule_props)

    if data:
        for key in schedule_props:
            raw = data.get(key)
            if raw is None:
                logger.log("PROP", f"{key}: not present")
                continue
            val = raw.get("value", raw) if isinstance(raw, dict) else raw
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except json.JSONDecodeError:
                    pass
            logger.log("PROP", f"{key} = {json.dumps(val, default=str)}")
    else:
        logger.log("ERROR", "Failed to read schedules")


async def phase_12_mqtt_audit(zaco: Zaco, logger: ExperimentLogger, mqtt: MqttInterceptor, sync_client: AliyunIoTClient) -> None:
    """Phase 12: MQTT push audit — observe what pushes when idle."""
    logger.log("PHASE", "=== Phase 12: MQTT Push Audit ===")

    if not zaco.mqtt_connected:
        logger.log("ERROR", "MQTT not connected, skipping audit")
        return

    # Passive observation: 60s idle
    logger.log("TEST", "Passive MQTT observation (60s, robot idle)")
    mqtt.clear()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 60:
        await asyncio.sleep(5)

    idle_pushes = len(mqtt.push_log)
    idle_properties = set()
    for push in mqtt.push_log:
        idle_properties.update(push["properties"])

    logger.log("RESULT", f"Idle MQTT pushes in 60s: {idle_pushes}")
    if idle_properties:
        logger.log("RESULT", f"Properties pushed while idle: {sorted(idle_properties)}")
    else:
        logger.log("RESULT", "No MQTT pushes while robot idle")

    # Active trigger: change FanPower
    mqtt.clear()
    logger.log("CMD", "Setting FanPower=50 (trigger test)")
    t0 = time.monotonic()
    await zaco.set_properties({"FanPower": 50})
    await asyncio.sleep(5)

    fp_pushes = [p for p in mqtt.push_log if "FanPower" in p["properties"]]
    logger.log("RESULT", f"FanPower change: {len(fp_pushes)} MQTT pushes containing FanPower")
    if fp_pushes:
        latency = fp_pushes[0]["time"] - (t0 + time.time() - time.monotonic())
        logger.log("RESULT", f"FanPower MQTT push latency: ~{latency:.2f}s")

    # Restore fan power
    await zaco.set_properties({"FanPower": 1})
    await asyncio.sleep(2)

    # Summary
    all_pushed_props = set()
    for push in mqtt.push_log:
        all_pushed_props.update(push["properties"])
    logger.log("RESULT", f"All properties seen in MQTT during audit: {sorted(all_pushed_props)}")


# ---------------------------------------------------------------------------
# Findings document generator
# ---------------------------------------------------------------------------

def generate_findings(logger: ExperimentLogger) -> str:
    """Generate markdown findings from the experiment log."""
    lines = [
        "# ZACO A10 API & MQTT Research Findings",
        f"",
        f"**Date**: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Total events logged**: {len(logger.events)}",
        "",
    ]

    # Group events by phase
    current_phase = None
    for event in logger.events:
        if event["category"] == "PHASE":
            if current_phase is not None:
                lines.append("")
            current_phase = event["event"]
            lines.append(f"## {event['event'].replace('=== ', '').replace(' ===', '')}")
            lines.append("")
        elif event["category"] == "RESULT":
            data_str = ""
            if event.get("data"):
                data_str = f"\n  ```json\n  {json.dumps(event['data'], indent=2, default=str)}\n  ```"
            lines.append(f"- **{event['event']}**{data_str}")
        elif event["category"] == "TRANSITION":
            data_str = ""
            if event.get("data"):
                extras = ", ".join(f"{k}={v}" for k, v in event["data"].items() if v is not None)
                data_str = f" ({extras})" if extras else ""
            lines.append(f"  - `{event['event']}`{data_str}")
        elif event["category"] == "PROP":
            lines.append(f"- `{event['event']}`")
        elif event["category"] == "ERROR":
            lines.append(f"- **ERROR**: {event['event']}")
        elif event["category"] == "SKIP":
            lines.append(f"- *Skipped*: {event['event']}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

DOCKED_PHASES = {0, 1, 2, 3, 4, 10, 11, 12}
MOVEMENT_PHASES = {5, 6, 7, 8, 9}
ALL_PHASES = sorted(DOCKED_PHASES | MOVEMENT_PHASES)

PHASE_FNS = {
    0: phase_0_setup,
    1: phase_1_property_dump,
    2: phase_2_settings,
    3: phase_3_upload_control,
    4: phase_4_locate,
    5: phase_5_room_clean,
    6: phase_6_zone_clean,
    7: phase_7_edge_clean,
    8: phase_8_remote_control,
    9: phase_9_explore,
    10: phase_10_timeline,
    11: phase_11_schedules,
    12: phase_12_mqtt_audit,
}


async def create_zaco(iot_id: str) -> Zaco:
    """Create a Zaco instance from saved tokens."""
    saved = load_tokens()
    if not saved:
        print("No saved tokens. Run test_aliyun.py --username/--password first.")
        sys.exit(1)
    if saved.get("_refresh_expired"):
        print("All tokens expired. Re-login with test_aliyun.py.")
        sys.exit(1)

    saved_at = saved.get("savedAt", 0)
    return await Zaco.from_tokens(
        iot_host=saved.get("host", ""),
        iot_token=saved.get("iotToken", ""),
        refresh_token=saved.get("refreshToken", ""),
        identity_id=saved.get("identityId", ""),
        iot_id=iot_id,
        iot_token_expiry=saved_at + saved.get("iotTokenExpire", 7200),
        refresh_token_expiry=saved_at + saved.get("refreshTokenExpire", 2592000),
        verbose=False,
    )


def create_sync_client() -> AliyunIoTClient:
    """Create a sync AliyunIoTClient from saved tokens."""
    saved = load_tokens()
    if not saved:
        print("No saved tokens.")
        sys.exit(1)

    client = AliyunIoTClient(host=saved.get("host", IOT_HOST_DEFAULT))
    client.iot_token = saved.get("iotToken")
    client.refresh_token = saved.get("refreshToken")
    client.identity_id = saved.get("identityId")

    # Auto-refresh if needed
    if saved.get("_iot_expired") and not saved.get("_refresh_expired"):
        if not client.refresh_session():
            print("Token refresh failed.")
            sys.exit(1)

    return client


async def run(phases: list[int], iot_id: str) -> None:
    logger = ExperimentLogger()
    logger.log("INFO", "Research session starting", {"phases": phases, "iot_id": iot_id})

    zaco = await create_zaco(iot_id)
    sync_client = create_sync_client()
    mqtt = MqttInterceptor(zaco, logger)

    # Setup signal handler for safe shutdown
    def _signal_handler(sig, frame):
        print("\n\nInterrupted! Sending return to dock...")
        asyncio.get_event_loop().create_task(_emergency_dock(zaco, logger))

    signal.signal(signal.SIGINT, _signal_handler)

    try:
        # Phase 0 (setup) is always first if included
        if 0 in phases:
            await PHASE_FNS[0](zaco, logger, sync_client)
        elif phases:
            # Minimal setup if phase 0 not included
            await zaco.refresh(fast=False)
            await zaco.start_mqtt()
            await asyncio.sleep(3)

        for phase_num in phases:
            if phase_num == 0:
                continue  # already done

            fn = PHASE_FNS.get(phase_num)
            if fn is None:
                logger.log("ERROR", f"Unknown phase: {phase_num}")
                continue

            # Phases 2-4, 12 need mqtt interceptor
            if phase_num in (2, 3, 4, 5, 6, 7, 8, 9, 12):
                await fn(zaco, logger, mqtt, sync_client)
            else:
                await fn(zaco, logger, mqtt, sync_client)

    except Exception as e:
        logger.log("ERROR", f"Exception: {e}")
        raise
    finally:
        mqtt.restore()

        # Save outputs
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = DOCS_DIR / f"research_log_{ts}.json"
        logger.save(log_path)

        findings = generate_findings(logger)
        findings_path = DOCS_DIR / "research_findings.md"
        findings_path.write_text(findings)
        print(f"Findings saved to {findings_path}")

        await zaco.close()


async def run_mqtt_only(iot_id: str) -> None:
    """Quick MQTT push test: connect, trigger FanPower change, observe."""
    logger = ExperimentLogger()
    logger.log("INFO", "MQTT-only test starting", {"iot_id": iot_id})

    zaco = await create_zaco(iot_id)
    mqtt = MqttInterceptor(zaco, logger)

    try:
        await zaco.refresh(fast=False)
        wm = parse_int(zaco.data, "WorkMode")
        fan = parse_int(zaco.data, "FanPower")
        logger.log("STATE", f"Initial: WM={wm}, FanPower={fan}")

        # Start MQTT
        logger.log("CMD", "Starting MQTT connection")
        mqtt_ok = await zaco.start_mqtt()
        logger.log("STATE", f"MQTT connected: {mqtt_ok}")
        if not mqtt_ok:
            logger.log("ERROR", "MQTT connection failed")
            return

        # Wait for bind
        await asyncio.sleep(5)
        logger.log("STATE", f"MQTT bound: {zaco.mqtt_connected}")

        # Passive observation (15s)
        logger.log("TEST", "Passive observation (15s idle)")
        mqtt.clear()
        await asyncio.sleep(15)
        logger.log("RESULT", f"Idle pushes in 15s: {len(mqtt.push_log)}")
        for p in mqtt.push_log:
            logger.log("DETAIL", f"  Push: {p['properties']}")

        # Trigger: set FanPower to something different
        test_fan = 50 if fan != 50 else 25
        mqtt.clear()
        logger.log("CMD", f"Setting FanPower={test_fan} (trigger test)")
        t0 = time.monotonic()
        await zaco.set_properties({"FanPower": test_fan})

        # Watch for 15 seconds
        for i in range(15):
            await asyncio.sleep(1)
            if mqtt.push_log:
                latency = mqtt.push_log[0]["time"] - (t0 + time.time() - time.monotonic())
                logger.log("RESULT", f"MQTT push received! Latency: {latency:.2f}s")
                logger.log("RESULT", f"Push properties: {mqtt.push_log[0]['properties']}")
                break
        else:
            logger.log("RESULT", "No MQTT push after FanPower change (15s)")

        total = len(mqtt.push_log)
        logger.log("RESULT", f"Total pushes after FanPower trigger: {total}")

        # Restore FanPower
        if fan is not None:
            await zaco.set_properties({"FanPower": fan})

        # Summary
        print("\n" + "=" * 60)
        if total > 0:
            print("MQTT PUSHES RECEIVED! The clientId fix worked.")
        else:
            print("ZERO MQTT pushes. Firmware does not push data.")
        print("=" * 60)

    finally:
        mqtt.restore()
        await zaco.close()


async def _emergency_dock(zaco: Zaco, logger: ExperimentLogger) -> None:
    """Emergency: stop and return to dock."""
    try:
        logger.log("EMERGENCY", "Sending stop + return to dock")
        await zaco.set_properties({"WorkMode": 2})
        await asyncio.sleep(2)
        await zaco.return_to_base()
    except Exception as e:
        logger.log("ERROR", f"Emergency dock failed: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ZACO A10 API/MQTT research session")
    parser.add_argument("--phase", type=int, action="append", help="Run specific phase(s)")
    parser.add_argument("--no-movement", action="store_true", help="Only docked phases (0-4,10-12)")
    parser.add_argument("--mqtt-only", action="store_true", help="Quick MQTT push test only")
    parser.add_argument("--iot-id", default=DEFAULT_IOT_ID, help="Device iotId")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    if args.mqtt_only:
        print("MQTT-only test mode")
        print(f"Device: {args.iot_id}")
        print()
        asyncio.run(run_mqtt_only(args.iot_id))
        return

    if args.phase:
        phases = sorted(set(args.phase))
        # Always include phase 0 at the start if not explicitly excluded
        if 0 not in phases:
            phases = [0] + phases
    elif args.no_movement:
        phases = sorted(DOCKED_PHASES)
    else:
        phases = ALL_PHASES

    print(f"Research session: phases {phases}")
    print(f"Device: {args.iot_id}")
    print()

    asyncio.run(run(phases, args.iot_id))


if __name__ == "__main__":
    main()
