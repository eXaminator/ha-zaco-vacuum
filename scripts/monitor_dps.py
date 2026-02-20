#!/usr/bin/env python3
"""
monitor_dps.py - Passively monitor Tuya device DPS changes

This script connects to a Tuya device on the local network and listens
for Data Point (DPS) changes. It does NOT send any commands to the device.

Usage:
    # First run tinytuya wizard to generate devices.json:
    #   python -m tinytuya wizard
    #
    # Then run this script:
    #   python scripts/monitor_dps.py
    #
    # Or specify a device directly:
    #   python scripts/monitor_dps.py --device-id XXX --ip 192.168.1.XX --local-key YYY

IMPORTANT: This script is READ-ONLY. It only listens for status updates.
It never sends commands to the device.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import tinytuya
except ImportError:
    print("Error: tinytuya is not installed. Run: pip install tinytuya")
    sys.exit(1)


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEVICES_FILE = PROJECT_DIR / "devices.json"
LOG_DIR = PROJECT_DIR / "captures"
DPS_LOG_FILE = LOG_DIR / "dps_monitor_log.jsonl"


def load_device_from_json(device_id=None):
    """Load device info from tinytuya's devices.json."""
    if not DEVICES_FILE.exists():
        return None

    with open(DEVICES_FILE) as f:
        devices = json.load(f)

    if not devices:
        return None

    if device_id:
        for d in devices:
            if d.get("id") == device_id:
                return d
        return None

    # If no device_id specified, look for a vacuum / robot cleaner
    # or just return the first device
    for d in devices:
        cat = d.get("category", "").lower()
        name = d.get("name", "").lower()
        if "sd" in cat or "vacuum" in name or "robot" in name or "zaco" in name:
            return d

    return devices[0]


def format_dps_change(old_dps, new_dps):
    """Show which DPS values changed."""
    changes = []
    all_keys = set(list(old_dps.keys()) + list(new_dps.keys()))
    for key in sorted(all_keys, key=lambda x: int(x) if x.isdigit() else x):
        old_val = old_dps.get(key)
        new_val = new_dps.get(key)
        if old_val != new_val:
            changes.append(f"  DP {key}: {old_val!r} -> {new_val!r}")
    return "\n".join(changes)


def log_entry(entry):
    """Append a JSON log entry to the DPS log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(DPS_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Passively monitor Tuya device DPS changes (READ-ONLY)"
    )
    parser.add_argument("--device-id", help="Tuya device ID")
    parser.add_argument("--ip", help="Device IP address")
    parser.add_argument("--local-key", help="Device local key")
    parser.add_argument(
        "--version",
        default="3.3",
        help="Tuya protocol version (default: 3.3)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Poll interval in seconds (default: 5.0)",
    )
    args = parser.parse_args()

    device_id = args.device_id
    device_ip = args.ip
    local_key = args.local_key
    version = args.version

    # Try to load from devices.json if not all args provided
    if not all([device_id, device_ip, local_key]):
        device_info = load_device_from_json(device_id)
        if device_info:
            device_id = device_id or device_info.get("id")
            device_ip = device_ip or device_info.get("ip")
            local_key = local_key or device_info.get("key")
            version = device_info.get("ver", version)
            print(f"Loaded device from devices.json:")
            print(f"  Name: {device_info.get('name', 'unknown')}")
            print(f"  ID: {device_id}")
            print(f"  IP: {device_ip}")
            print(f"  Version: {version}")
        else:
            print("Error: No device info found.")
            print("")
            print("Either:")
            print("  1. Run 'python -m tinytuya wizard' to generate devices.json")
            print("  2. Provide --device-id, --ip, and --local-key arguments")
            sys.exit(1)

    if not all([device_id, device_ip, local_key]):
        print("Error: Missing device info. Need device-id, ip, and local-key.")
        sys.exit(1)

    # Connect to device (READ-ONLY)
    print(f"\nConnecting to device {device_id} at {device_ip}...")
    print(f"Protocol version: {version}")
    print(f"Log file: {DPS_LOG_FILE}")
    print(f"Poll interval: {args.interval}s")
    print("")
    print("=" * 60)
    print(" PASSIVE MONITORING MODE - NO COMMANDS WILL BE SENT")
    print("=" * 60)
    print("")
    print("Use the ZACOHome app to control the vacuum while this runs.")
    print("DPS changes will be logged here and to the log file.")
    print("Press Ctrl+C to stop.\n")

    d = tinytuya.Device(device_id, device_ip, local_key)
    d.set_version(float(version))

    # Initial status read
    last_dps = {}
    status = d.status()

    if not status or "dps" not in status:
        print(f"Warning: Could not get initial status: {status}")
        print("The device may be offline or the local key may be wrong.")
        print("Continuing to poll...\n")
    else:
        last_dps = status["dps"]
        timestamp = datetime.now().isoformat()
        print(f"[{timestamp}] Initial DPS state:")
        for key in sorted(last_dps.keys(), key=lambda x: int(x) if x.isdigit() else x):
            print(f"  DP {key}: {last_dps[key]!r}")
        print("")

        log_entry(
            {
                "timestamp": timestamp,
                "type": "initial",
                "dps": last_dps,
            }
        )

    # Poll loop
    try:
        while True:
            time.sleep(args.interval)
            status = d.status()

            if not status or "dps" not in status:
                continue

            current_dps = status["dps"]

            if current_dps != last_dps:
                timestamp = datetime.now().isoformat()
                changes = format_dps_change(last_dps, current_dps)
                print(f"[{timestamp}] DPS CHANGED:")
                print(changes)
                print("")

                log_entry(
                    {
                        "timestamp": timestamp,
                        "type": "change",
                        "old_dps": last_dps,
                        "new_dps": current_dps,
                    }
                )

                last_dps = current_dps.copy()

    except KeyboardInterrupt:
        print("\nMonitoring stopped.")
        print(f"Log saved to: {DPS_LOG_FILE}")


if __name__ == "__main__":
    main()
