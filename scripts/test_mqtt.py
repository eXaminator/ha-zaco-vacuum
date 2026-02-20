#!/usr/bin/env python3
"""
test_mqtt.py - Test MQTT real-time push from Aliyun IoT Living Platform

Tests the MQTT connection flow used by the ZACO app for real-time
property push notifications (instead of polling).

Flow (from MobileAuthHttpRequest.java / MobileChannelImpl.java):
  1. Get MQTT credentials via /app/aepauth/handle (API gateway signed request)
  2. Connect to MQTT broker with the returned triple (productKey, deviceName, deviceSecret)
  3. Subscribe to wildcard # to receive all topics
  4. Bind account by publishing iotToken to /account/bind
  5. Listen for property changes on /app/down/thing/properties

Usage:
    # First run (needs valid iotToken — run test_aliyun.py first to login):
    python3 scripts/test_mqtt.py

    # With verbose output:
    python3 scripts/test_mqtt.py --verbose

    # Just test the aepauth credential fetch (no MQTT connect):
    python3 scripts/test_mqtt.py --aepauth-only

Prerequisites:
    pip3 install paho-mqtt
"""

import argparse
import hashlib
import hmac
import json
import random
import ssl
import string
import sys
import time
import uuid
from pathlib import Path

# Import shared functions from test_aliyun.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_aliyun import (
    APP_KEY,
    APP_SECRET,
    IOT_HOST_DEFAULT,
    build_iot_body,
    load_tokens,
    send_signed_request,
)

try:
    import paho.mqtt.client as mqtt
    HAS_PAHO = True
except ImportError:
    HAS_PAHO = False


# MQTT broker endpoint (from region lookup mqttEndpoint)
MQTT_HOST = "public.itls.eu-central-1.aliyuncs.com"
MQTT_PORT = 1883


def random_string(length):
    """Generate a random alphanumeric string (from RandomStringUtil.getRandomString)."""
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def aepauth_sign(app_key, client_id, device_sn, timestamp):
    """Compute the authInfo sign for /app/aepauth/handle.

    From MobileAuthHttpRequest.sign() with SecurityImpl fallback:
    sign_string = "appKey" + appkey_value + "clientId" + clientid_value
                + "deviceSn" + devicesn_value + "timestamp" + timestamp_value
    sign = HmacSHA1(appSecret, sign_string).lowercase()

    The key+value pairs are concatenated in a FIXED order:
    [appKey, clientId, deviceSn, timestamp] — this is NOT sorted,
    it's the explicit ArrayList order in the Java source.
    """
    sign_string = (
        f"appKey{app_key}"
        f"clientId{client_id}"
        f"deviceSn{device_sn}"
        f"timestamp{timestamp}"
    )
    mac = hmac.new(
        APP_SECRET.encode("utf-8"),
        sign_string.encode("utf-8"),
        hashlib.sha1,
    )
    return mac.hexdigest().lower()


def get_mqtt_credentials(host, verbose=False):
    """Call /app/aepauth/handle to get MQTT connection credentials.

    From MobileAuthHttpRequest.request():
    - Generates random deviceSn (32 chars) and clientId (8 chars)
    - Signs with appKey+clientId+deviceSn+timestamp
    - Sends via API gateway (standard signed request)
    - Returns (productKey, deviceName, deviceSecret) triple

    The authInfo dict goes as a param inside the standard IoT body.
    Note: appKey is used for signing but removed from the submitted authInfo.
    """
    device_sn = random_string(32)
    client_id = random_string(8)
    timestamp = str(int(time.time() * 1000))

    sign = aepauth_sign(APP_KEY, client_id, device_sn, timestamp)

    if verbose:
        sign_string = (
            f"appKey{APP_KEY}"
            f"clientId{client_id}"
            f"deviceSn{device_sn}"
            f"timestamp{timestamp}"
        )
        print(f"[AEPAUTH] sign_string: {sign_string}")
        print(f"[AEPAUTH] sign: {sign}")

    # authInfo contains the sign fields (but NOT appKey — removed after signing)
    auth_info = {
        "timestamp": timestamp,
        "clientId": client_id,
        "deviceSn": device_sn,
        "sign": sign,
    }

    # Build as standard IoT API request body
    request_id = str(uuid.uuid4())
    params = {"authInfo": auth_info}
    body = build_iot_body(request_id, "1.0.0", params)
    query_params = {"x-ca-request-id": request_id}

    print(f"Calling /app/aepauth/handle...")
    resp = send_signed_request(host, "/app/aepauth/handle", body, query_params, verbose)

    if resp is None:
        print("aepauth failed: no response")
        return None

    code = resp.get("code", -1)
    if code != 200:
        message = resp.get("message", "unknown error")
        print(f"aepauth failed: code={code}, message={message}")
        if verbose:
            print(f"  Full response: {json.dumps(resp, indent=2)}")
        return None

    data = resp.get("data", {})
    product_key = data.get("productKey")
    device_name = data.get("deviceName")
    device_secret = data.get("deviceSecret")

    if not all([product_key, device_name, device_secret]):
        print(f"aepauth: incomplete response: {data}")
        return None

    print(f"Got MQTT credentials:")
    print(f"  productKey: {product_key}")
    print(f"  deviceName: {device_name[:30]}...")
    print(f"  deviceSecret: {device_secret[:20]}...")

    return {
        "productKey": product_key,
        "deviceName": device_name,
        "deviceSecret": device_secret,
        "clientId": client_id,
        "deviceSn": device_sn,
    }


def compute_mqtt_password(params_map, device_secret):
    """Compute MQTT password: HMAC-SHA1(deviceSecret, sorted_key_value_pairs).

    From MqttNet.java method a(Map, String):
    - Sort map keys alphabetically
    - Concatenate key+value pairs (excluding 'sign')
    - HMAC-SHA1 with deviceSecret
    - Hex-encode uppercase

    The map typically contains: {productKey, deviceName, clientId}
    """
    sorted_keys = sorted(params_map.keys())
    content = ""
    for key in sorted_keys:
        if key.lower() != "sign":
            content += key + params_map[key]

    mac = hmac.new(
        device_secret.encode("utf-8"),
        content.encode("utf-8"),
        hashlib.sha1,
    )
    return mac.digest().hex().upper()


def connect_mqtt(creds, iot_token, verbose=False):
    """Connect to MQTT broker, subscribe, bind account, and listen.

    From MqttNet.g() + MobileChannelImpl.startConnect() + BaseSDKGlue.bindAccount().
    """
    if not HAS_PAHO:
        print("Error: paho-mqtt is required for MQTT connection.")
        print("Install it with: pip3 install paho-mqtt")
        sys.exit(1)

    product_key = creds["productKey"]
    device_name = creds["deviceName"]
    device_secret = creds["deviceSecret"]

    # Build MQTT credentials (from MqttNet.g())
    # clientId base = deviceName&productKey
    client_id_base = f"{device_name}&{product_key}"

    # MQTT password: HMAC-SHA1(deviceSecret, sorted params)
    # params map = {productKey, deviceName, clientId}
    params_map = {
        "productKey": product_key,
        "deviceName": device_name,
        "clientId": client_id_base,
    }
    mqtt_password = compute_mqtt_password(params_map, device_secret)

    # MQTT username = deviceName&productKey
    mqtt_username = f"{device_name}&{product_key}"

    # MQTT clientId = base|securemode=2,_v=...,signmethod=hmacsha1,...|
    sdk_version = "1.5.3"
    mqtt_client_id = (
        f"{client_id_base}"
        f"|securemode=2"
        f",_v={sdk_version}"
        f",lan=Python"
        f",signmethod=hmacsha1"
        f",ext=1|"
    )

    mqtt_host = f"ssl://{MQTT_HOST}:{MQTT_PORT}"

    print(f"\nMQTT Connection:")
    print(f"  Host: {mqtt_host}")
    print(f"  Username: {mqtt_username[:40]}...")
    print(f"  ClientId: {mqtt_client_id[:60]}...")
    if verbose:
        print(f"  Password: {mqtt_password[:20]}...")
        print(f"  Params map: {params_map}")

    # Create MQTT client
    client = mqtt.Client(
        client_id=mqtt_client_id,
        protocol=mqtt.MQTTv311,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )

    # Set credentials
    client.username_pw_set(mqtt_username, mqtt_password)

    # TLS (securemode=2)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)

    # Track state
    state = {"connected": False, "subscribed": False, "bound": False}

    # Topic prefixes (from MobileChannelImpl / MobileRequest)
    # All topics are prefixed with /sys/{productKey}/{deviceName}/
    topic_prefix = f"/sys/{product_key}/{device_name}"
    sub_topic = f"{topic_prefix}/app/down/#"
    bind_topic = f"{topic_prefix}/app/up/account/bind"

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            print(f"\n[MQTT] Connected successfully!")
            state["connected"] = True

            # Subscribe (from MobileChannelImpl.afterConnect())
            # App transforms '#' to '/sys/{pk}/{dn}/app/down/#'
            print(f"[MQTT] Subscribing to '{sub_topic}'...")
            client.subscribe(sub_topic, qos=0)
        else:
            print(f"\n[MQTT] Connection failed: rc={reason_code}")

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        print(f"[MQTT] Subscribed (mid={mid})")
        state["subscribed"] = True

        # Bind account with iotToken (from BaseSDKGlue.bindAccountInternal())
        # From MobileRequest (d.java): RPC call to /account/bind
        # Topic: /sys/{pk}/{dn}/app/up/account/bind
        # Payload: {"id": "1", "system": {...}, "request": {...}, "params": {"iotToken": ...}}
        bind_payload = json.dumps({
            "id": "1",
            "system": {
                "version": "1.0",
                "time": str(int(time.time() * 1000)),
            },
            "request": {
                "clientId": f"{device_name}&{product_key}",
            },
            "params": {
                "iotToken": iot_token,
            },
        })
        print(f"[MQTT] Binding account (publishing to {bind_topic})...")
        client.publish(bind_topic, bind_payload, qos=0)

    def on_message(client, userdata, msg):
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = msg.payload.hex()

        # Check for bind response
        if "/account/bind" in topic:
            print(f"\n[MQTT] Bind response: {json.dumps(payload, indent=2)[:200]}")
            if isinstance(payload, dict) and payload.get("code") == 200:
                state["bound"] = True
                print("[MQTT] Account bound successfully! Listening for property changes...")
            else:
                print(f"[MQTT] Bind failed: {payload}")
            return

        # Property change notification
        timestamp = time.strftime("%H:%M:%S")

        # Simplify topic for display
        display_topic = topic
        if "/app/down/" in topic:
            display_topic = topic.split("/app/down/")[-1]
        elif "/sys/" in topic:
            # Strip /sys/{pk}/{dn}/ prefix
            parts = topic.split("/")
            if len(parts) > 4:
                display_topic = "/".join(parts[4:])

        if isinstance(payload, dict):
            method = payload.get("method", "")
            params = payload.get("params", payload)

            if "property" in method or "property" in topic.lower() or "properties" in topic.lower():
                # Property update — show individual properties
                if isinstance(params, dict):
                    for key, val in params.items():
                        if isinstance(val, dict):
                            value = val.get("value", val)
                            ts = val.get("time", "")
                        else:
                            value = val
                            ts = ""
                        # Truncate large values (e.g. map data)
                        val_str = str(value)
                        if len(val_str) > 100:
                            val_str = val_str[:100] + "..."
                        print(f"  [{timestamp}] {key} = {val_str}")
                else:
                    print(f"  [{timestamp}] {display_topic}: {str(params)[:200]}")
            else:
                # Other topic
                payload_str = json.dumps(payload)
                if len(payload_str) > 300:
                    payload_str = payload_str[:300] + "..."
                print(f"  [{timestamp}] {display_topic}: {payload_str}")
        else:
            print(f"  [{timestamp}] {display_topic}: {str(payload)[:200]}")

    def on_disconnect(client, userdata, flags, reason_code, properties):
        print(f"\n[MQTT] Disconnected: rc={reason_code}")
        state["connected"] = False

    def on_log(client, userdata, level, buf):
        if verbose:
            print(f"  [MQTT-LOG] {buf}")

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    if verbose:
        client.on_log = on_log

    # Connect
    print(f"\nConnecting to {MQTT_HOST}:{MQTT_PORT}...")
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=65)
    except Exception as e:
        print(f"Connection failed: {e}")
        # Try alternative TLS port
        print(f"\nTrying port 443...")
        try:
            client.connect(MQTT_HOST, 443, keepalive=65)
        except Exception as e2:
            print(f"Port 443 also failed: {e2}")
            return

    print("Listening for messages (Ctrl+C to stop)...\n")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n\nDisconnecting...")
        client.disconnect()
        print("Done.")


def main():
    parser = argparse.ArgumentParser(
        description="Test MQTT real-time push from Aliyun IoT"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show detailed MQTT and signing output",
    )
    parser.add_argument(
        "--aepauth-only", action="store_true",
        help="Only test the aepauth credential fetch, don't connect MQTT",
    )
    parser.add_argument(
        "--host",
        help="Override IoT API host (default: from saved tokens or EU)",
    )
    args = parser.parse_args()

    # Load saved tokens (need iotToken for aepauth and account binding)
    saved = load_tokens()
    if not saved or not saved.get("iotToken"):
        print("No saved iotToken found. Run test_aliyun.py first to login.")
        sys.exit(1)

    iot_token = saved["iotToken"]
    host = args.host or saved.get("host") or IOT_HOST_DEFAULT

    if saved.get("_iot_expired"):
        print("Warning: iotToken may be expired. MQTT bind might fail.")
        print("Run test_aliyun.py to refresh tokens first.\n")

    # Phase 1: Get MQTT credentials
    creds = get_mqtt_credentials(host, verbose=args.verbose)
    if creds is None:
        print("\nFailed to get MQTT credentials.")
        sys.exit(1)

    if args.aepauth_only:
        print("\n--- aepauth test complete ---")
        sys.exit(0)

    # Phase 2: Connect to MQTT
    if not HAS_PAHO:
        print("\npaho-mqtt not installed. Install with: pip3 install paho-mqtt")
        print("Then re-run to test MQTT connection.")
        sys.exit(1)

    print()
    connect_mqtt(creds, iot_token, verbose=args.verbose)


if __name__ == "__main__":
    main()
