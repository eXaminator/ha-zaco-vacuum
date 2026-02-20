#!/usr/bin/env python3
"""
test_connection.py - Test connection to the iRobotics/ZACO cloud API

This script connects to the iRobotics WebSocket cloud server and attempts
to authenticate and retrieve device status. It does NOT send any control
commands to the vacuum.

Usage:
    # Interactive mode (prompts for credentials):
    python3 scripts/test_connection.py

    # With credentials:
    python3 scripts/test_connection.py --username YOUR_EMAIL --password YOUR_PASSWORD

    # With saved token (after first login):
    python3 scripts/test_connection.py --token YOUR_TOKEN --user-id YOUR_ID

    # Monitor mode (listen for status updates):
    python3 scripts/test_connection.py --token YOUR_TOKEN --user-id YOUR_ID --monitor

    # Scan all regions for a device:
    python3 scripts/test_connection.py --username YOUR_EMAIL --password YOUR_PASSWORD \\
        --mac 34:20:03:66:4B:BA --sn ZA900201223020981 --scan-regions

    # Full provisioning (lock + bind) after device is found:
    python3 scripts/test_connection.py --username YOUR_EMAIL --password YOUR_PASSWORD \\
        --mac 34:20:03:66:4B:BA --sn ZA900201223020981 --provision

IMPORTANT: This script is READ-ONLY by default. It only retrieves status information.
Use --bind or --provision to modify server state (bind device to account).

Prerequisites:
    pip3 install websocket-client
"""

import argparse
import json
import os
import ssl
import sys
import threading
import time
from pathlib import Path

import hashlib

try:
    import websocket
except ImportError:
    print("Error: websocket-client is not installed. Run: pip3 install websocket-client")
    sys.exit(1)


PROJECT_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = PROJECT_DIR / ".credentials.json"

# Allowed password characters (from android:digits="@string/login_password_digits")
# The app's EditText silently drops any characters NOT in this set.
APP_PASSWORD_DIGITS = set(
    "1234567890abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    ",./;'[]-=`~!@#$%^*()_+{}:?|"
)


def filter_password(password, warn=True):
    """Strip characters not allowed by the app's password input field.

    Android's EditText digits filter silently drops unsupported characters,
    so the server never sees them. We must do the same.
    """
    filtered = "".join(c for c in password if c in APP_PASSWORD_DIGITS)
    if warn and filtered != password:
        removed = [c for c in password if c not in APP_PASSWORD_DIGITS]
        print(f"Warning: Stripped {len(removed)} unsupported character(s) from password: {removed}")
        print(f"  (The app's input field does not allow these characters)")
    return filtered


# App identity constants (from decompiled BaseReq.java + Robot.java)
FACTORY_ID = 1059
PROJECT_TYPE = "android-ILIFE_robot"
VERSION_NAME = "1.3.22"
VERSION_CODE = 10322
PACKAGE_TYPE = "android"
ROBOT_TYPE = "sweeper"

# Secret key from BaseActivity.java / ActivityHome.java
# Used for loginByCode: "#KEY#" + MD5("WOq9vaJyDSHksKrW#" + username)
AUTH_SECRET_KEY = "WOq9vaJyDSHksKrW"


def make_authcode(username):
    """Generate authcode: '#KEY#' + MD5('WOq9vaJyDSHksKrW#' + username)"""
    raw = f"{AUTH_SECRET_KEY}#{username}"
    md5_hash = hashlib.md5(raw.encode()).hexdigest()
    return f"#KEY#{md5_hash}"

# Server configuration (from decompiled UrlInfo.java + Robot.java)
# OTA servers return the actual WSS/HTTPS URLs in targetUrls
REGIONS = {
    "eu": {
        "ota_host": "https://eu-ota.3irobotix.net:8001",
        "ws_fallback": "wss://web-eu.3irobotix.net:8001",
    },
    "cn": {
        "ota_host": "https://ota.3irobotix.net:8001",
        "ws_fallback": "wss://web.3irobotix.net:8001",
    },
    "us": {
        "ota_host": "https://us-ota.3irobotix.net:8001",
        "ws_fallback": "wss://web-us.3irobotix.net:8001",
    },
}

UPGRADE_PATH = "/service-publish/open/upgrade/try_upgrade"
HEARTBEAT_INTERVAL = 15  # seconds


def make_trace_id():
    return str(int(time.time() * 1000))


def make_request(service, content, method="POST"):
    return json.dumps({
        "traceId": make_trace_id(),
        "method": method,
        "service": service,
        "content": content if isinstance(content, str) else json.dumps(content, separators=(",", ":")),
    }, separators=(",", ":"))


def resolve_server_urls(region="eu"):
    """Resolve WebSocket and HTTP URLs from the OTA server.

    Returns dict with 'ws' and 'http' keys.
    """
    import urllib.request

    region_info = REGIONS.get(region, REGIONS["eu"])
    url = region_info["ota_host"] + UPGRADE_PATH

    # Full OTA payload matching the real app (BaseReq + BaseUrlReq)
    payload = json.dumps({
        "factoryId": FACTORY_ID,
        "projectType": PROJECT_TYPE,
        "versionName": VERSION_NAME,
        "versionCode": VERSION_CODE,
        "packageVersions": [{"packageType": PACKAGE_TYPE, "version": VERSION_CODE}],
        "robotType": ROBOT_TYPE,
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    # Allow self-signed certs for the OTA server
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    ws_url = None
    http_url = None

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            data = json.loads(resp.read())
            print(f"OTA response: {json.dumps(data, indent=2)}")
            if "result" in data and "targetUrls" in data["result"]:
                for target_url in data["result"]["targetUrls"]:
                    if target_url.lower().startswith("wss://"):
                        ws_url = target_url
                    elif target_url.lower().startswith("https://"):
                        http_url = target_url
    except Exception as e:
        print(f"Warning: Could not resolve URLs from OTA server: {e}")

    # Fallback to known defaults if OTA didn't return URLs
    if not ws_url:
        ws_url = region_info["ws_fallback"]
        print(f"Using fallback WebSocket URL: {ws_url}")

    return {"ws": ws_url, "http": http_url}


def save_credentials(token, user_id):
    """Save login credentials for reuse."""
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump({"token": token, "userId": user_id}, f)
    print(f"Credentials saved to {CREDENTIALS_FILE}")


def load_credentials():
    """Load saved credentials."""
    if CREDENTIALS_FILE.exists():
        with open(CREDENTIALS_FILE) as f:
            return json.load(f)
    return None


class ZACOClient:
    def __init__(self, ws_url, verbose=False):
        self.ws_url = ws_url
        self.verbose = verbose
        self.ws = None
        self.connected = False
        self.logged_in = False
        self.token = None
        self.user_id = None
        self.devices = []
        self.responses = {}
        self._heartbeat_thread = None
        self._stop_heartbeat = threading.Event()

    def connect(self):
        print(f"Connecting to {self.ws_url}...")

        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        # Run in background thread
        wst = threading.Thread(
            target=self.ws.run_forever,
            kwargs={"sslopt": {"cert_reqs": ssl.CERT_NONE}},
            daemon=True,
        )
        wst.start()

        # Wait for connection
        for _ in range(50):
            if self.connected:
                return True
            time.sleep(0.1)

        print("Connection timeout")
        return False

    def _on_open(self, ws):
        print("WebSocket connected!")
        self.connected = True
        self._start_heartbeat()

    def _on_message(self, ws, message):
        if isinstance(message, bytes):
            print(f"[BINARY] Received {len(message)} bytes (map data)")
            return

        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            print(f"[RAW] {message[:200]}")
            return

        service = data.get("service", "")
        code = data.get("code", None)
        push_tag = data.get("pushTag", "")
        trace_id = data.get("traceId", "")

        if service == "heart-beat":
            if self.verbose:
                print("[HEARTBEAT] OK")
            return

        if push_tag == "sweeper-transmit/to_bind":
            # Device status push
            try:
                content = json.loads(data.get("content", "{}"))
                push_content = data.get("pushContent", "")
                if push_content:
                    status = json.loads(push_content)
                    print(f"\n[STATUS UPDATE]")
                    print(f"  Mode: {status.get('mode', '?')}")
                    print(f"  Battery: {status.get('battary', '?')}%")
                    print(f"  Suction: {status.get('pref', '?')}")
                    print(f"  Water: {status.get('water', '?')}")
                    print(f"  Fault: {status.get('fault', 0)}")
                    print(f"  Clean time: {status.get('time', 0)} min")
                    print(f"  Clean area: {status.get('area', 0)} m2")
            except (json.JSONDecodeError, KeyError):
                print(f"[PUSH] {data}")
            return

        if push_tag == "kick_out":
            reason = data.get("pushContent", "unknown")
            print(f"[KICK OUT] Reason code: {reason}")
            return

        # Store response for request matching
        if trace_id:
            self.responses[trace_id] = data

        if self.verbose or code != 0:
            print(f"[RESPONSE] service={service} code={code}")
            if self.verbose:
                print(f"  {json.dumps(data, indent=2)[:500]}")

    def _on_error(self, ws, error):
        print(f"[ERROR] {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        print(f"WebSocket closed (code={close_status_code}, msg={close_msg})")
        self.connected = False
        self._stop_heartbeat.set()

    def _start_heartbeat(self):
        self._stop_heartbeat.clear()

        def heartbeat_loop():
            while not self._stop_heartbeat.is_set():
                try:
                    msg = make_request("heart-beat", "0")
                    self.ws.send(msg)
                except Exception:
                    break
                self._stop_heartbeat.wait(HEARTBEAT_INTERVAL)

        self._heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def send_and_wait(self, service, content, method="POST", timeout=10):
        trace_id = make_trace_id()
        msg = json.dumps({
            "traceId": trace_id,
            "method": method,
            "service": service,
            "content": content if isinstance(content, str) else json.dumps(content, separators=(",", ":")),
        }, separators=(",", ":"))

        if self.verbose:
            # Mask password in debug output
            debug_msg = msg
            if '"password"' in debug_msg:
                import re
                debug_msg = re.sub(
                    r'"password"\s*:\s*"[^"]*"',
                    '"password":"***"',
                    debug_msg,
                )
            print(f"[SEND] {debug_msg}")

        self.ws.send(msg)

        deadline = time.time() + timeout
        while time.time() < deadline:
            if trace_id in self.responses:
                return self.responses.pop(trace_id)
            time.sleep(0.1)

        return None

    def login_password(self, username, password, lang="en"):
        """Login with username/password."""
        print(f"Logging in as {username}...")
        # Match exact Gson serialization of LoginReq (extends BaseReq)
        # Use compact separators to match Gson output (no spaces)
        content = json.dumps({
            "factoryId": FACTORY_ID,
            "projectType": PROJECT_TYPE,
            "versionName": VERSION_NAME,
            "versionCode": VERSION_CODE,
            "packageVersions": [{"packageType": PACKAGE_TYPE, "version": VERSION_CODE}],
            "lang": lang,
            "password": password,
            "userId": 0,
            "username": username,
        }, separators=(",", ":"))
        resp = self.send_and_wait("sweeper-app-user/auth/login", content)
        return self._handle_login_response(resp)

    def login_token(self, token, user_id):
        """Login with saved token."""
        print(f"Logging in with token (userId={user_id})...")
        content = json.dumps({
            "factoryId": str(FACTORY_ID),
            "token": token,
            "userId": user_id,
            "versionCode": VERSION_CODE,
            "versionName": VERSION_NAME,
        }, separators=(",", ":"))
        resp = self.send_and_wait("sweeper-app-user/auth/login_token", content)
        return self._handle_login_response(resp)

    def login_authcode(self, username, password, lang="en"):
        """Login with authcode (#KEY# + MD5 mechanism from BaseActivity.java).

        The app uses this for session recovery (accountExpiredJava) and in
        various activities. Sends to sweeper-app-user/auth/login_authcode.
        """
        authcode = make_authcode(username)
        print(f"Logging in as {username} via login_authcode...")
        print(f"  authcode: {authcode}")
        # LoginReq 3-arg constructor: LoginReq(authcode, username, password)
        # Gson serializes: BaseReq fields first, then LoginReq fields in declaration order
        content = json.dumps({
            "factoryId": FACTORY_ID,
            "projectType": PROJECT_TYPE,
            "versionName": VERSION_NAME,
            "versionCode": VERSION_CODE,
            "packageVersions": [{"packageType": PACKAGE_TYPE, "version": VERSION_CODE}],
            "authcode": authcode,
            "lang": lang,
            "password": password,
            "userId": 0,
            "username": username,
        }, separators=(",", ":"))
        resp = self.send_and_wait("sweeper-app-user/auth/login_authcode", content)
        return self._handle_login_response(resp)

    def _handle_login_response(self, resp):
        if resp is None:
            print("Login timeout - no response received")
            return False

        code = resp.get("code", -1)
        if code != 0:
            print(f"Login failed with code {code}")
            print(f"Response: {json.dumps(resp, indent=2)}")
            return False

        try:
            # Response can come as either:
            # WebSocket: {"code":0, "result": {"data": {"AUTH": ...}, "id": "..."}}
            # or with content string: {"code":0, "content": "{\"data\":{\"AUTH\":...}}"}
            result = resp.get("result")
            if result is None:
                content_str = resp.get("content", "{}")
                result = json.loads(content_str) if isinstance(content_str, str) else content_str

            data = result.get("data", result)
            self.token = data.get("AUTH", data.get("token"))
            self.user_id = result.get("id", data.get("userId", data.get("uid")))
            if isinstance(self.user_id, str):
                self.user_id = int(self.user_id)
            print(f"Login successful!")
            print(f"  Token: {self.token[:40]}..." if self.token else "  Token: None")
            print(f"  User ID: {self.user_id}")
            self.logged_in = True
            save_credentials(self.token, self.user_id)
            return True
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"Could not parse login response: {e}")
            print(f"Response: {json.dumps(resp, indent=2)}")
            return False

    def get_device_list(self):
        """Get list of bound devices (READ-ONLY)."""
        print("Fetching device list...")
        resp = self.send_and_wait(
            "sweeper-robot-center/app/get_user_bind", "", method="GET"
        )
        if resp and resp.get("code") == 0:
            try:
                content = json.loads(resp.get("content", "[]"))
                if isinstance(content, list):
                    self.devices = content
                else:
                    self.devices = content.get("result", content.get("data", []))
                print(f"Found {len(self.devices)} device(s)")
                for dev in self.devices:
                    print(f"  - ID: {dev.get('robotId', dev.get('id', '?'))}")
                    print(f"    Name: {dev.get('nickname', '?')}")
                    print(f"    SN: {dev.get('sn', '?')}")
                return self.devices
            except json.JSONDecodeError:
                pass
        print(f"Response: {json.dumps(resp, indent=2) if resp else 'None'}")
        return []

    def get_robot_info(self, mac, sn=""):
        """Look up robot by MAC and/or SN (READ-ONLY).

        Calls sweeper-robot-center/app/get_robot_info which returns the
        numeric robotId needed for device control.

        From MasterRequest.getDevideId() (line 356).
        """
        service = f"sweeper-robot-center/app/get_robot_info?mac={mac}"
        if sn:
            service += f"&sn={sn}"
        print(f"Looking up robot info (mac={mac}, sn={sn or '(empty)'})...")
        resp = self.send_and_wait(service, "", method="GET")
        if resp is None:
            print("get_robot_info timeout - no response")
            return None
        code = resp.get("code", -1)
        if code != 0:
            print(f"get_robot_info failed with code {code}")
            print(f"Response: {json.dumps(resp, indent=2)}")
            return None
        try:
            result = resp.get("result")
            if result is None:
                content_str = resp.get("content", "{}")
                result = json.loads(content_str) if isinstance(content_str, str) else content_str
            robot_id = result.get("robotId", result.get("id"))
            sn_resp = result.get("sn", "")
            nickname = result.get("nickname", "")
            version = result.get("version", "")
            ctrl_version = result.get("ctrlVersion", "")
            print(f"Robot found!")
            print(f"  Robot ID: {robot_id}")
            print(f"  SN: {sn_resp}")
            print(f"  Nickname: {nickname}")
            print(f"  Version: {version}")
            print(f"  Ctrl Version: {ctrl_version}")
            return result
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"Could not parse get_robot_info response: {e}")
            print(f"Response: {json.dumps(resp, indent=2)}")
            return None

    def is_robot_online(self, robot_id):
        """Check if a robot is online (READ-ONLY).

        From MasterRequest.getIsRobotOnline() (line 374).
        """
        service = f"sweeper-robot-center/app/is_robot_online?robotId={robot_id}"
        print(f"Checking if robot {robot_id} is online...")
        resp = self.send_and_wait(service, "", method="GET")
        if resp is None:
            print("is_robot_online timeout")
            return None
        code = resp.get("code", -1)
        print(f"Online check: code={code}")
        if self.verbose:
            print(f"  {json.dumps(resp, indent=2)[:500]}")
        return resp

    def bind_override(self, sn, mac, nickname=""):
        """Bind/re-bind a device in the 3irobotix cloud.

        From MasterRequest.setBindOverride() (line 686).
        Sends POST to sweeper-robot-center/app/bind_override with
        request_id header = userId.

        WARNING: This modifies server state. Only call with --bind flag.
        """
        if not self.user_id:
            print("bind_override requires a valid user_id (login first)")
            return None
        print(f"Binding device (sn={sn}, mac={mac}, nickname={nickname or '(empty)'})...")
        content = json.dumps({
            "mac": mac,
            "nickname": nickname,
            "sn": sn,
        }, separators=(",", ":"))
        # The app sends request_id as a WebSocket header via map parameter.
        # We can't add headers to an open WebSocket, so try HTTPS first,
        # then fall back to including request_id in the JSON envelope.
        resp = self._bind_override_https(sn, mac, nickname)
        if resp is not None:
            return resp
        # Fallback: try via WebSocket (request_id might be picked up from auth)
        print("  HTTPS bind failed, trying WebSocket...")
        resp = self.send_and_wait(
            "sweeper-robot-center/app/bind_override", content, method="POST"
        )
        if resp is None:
            print("bind_override timeout - no response")
            return None
        code = resp.get("code", -1)
        print(f"bind_override response: code={code}")
        print(f"  {json.dumps(resp, indent=2)}")
        return resp

    def _bind_override_https(self, sn, mac, nickname=""):
        """Try bind_override via HTTPS REST API (allows setting request_id header)."""
        import urllib.request

        if not self.token:
            return None
        # Use the HTTPS URL from OTA (stored during connection setup)
        # Try known EU endpoints
        for base_url in [
            "https://web-cecotec.3irobotix.net:8002",
            "https://web-eu.3irobotix.net:8002",
        ]:
            url = f"{base_url}/sweeper-robot-center/app/bind_override"
            payload = json.dumps({
                "mac": mac,
                "nickname": nickname,
                "sn": sn,
            }).encode()

            req = urllib.request.Request(url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", self.token)
            req.add_header("request_id", str(self.user_id))

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            try:
                with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                    data = json.loads(resp.read())
                    code = data.get("code", -1)
                    print(f"HTTPS bind_override ({base_url}): code={code}")
                    print(f"  {json.dumps(data, indent=2)}")
                    if code == 0:
                        return data
            except Exception as e:
                print(f"  HTTPS bind_override error ({base_url}): {e}")
        return None

    def get_device_list_http(self, http_base_url=None):
        """Get device list via HTTP REST (mirrors Robot.getAllDevices()).

        This is how the ZACO app actually fetches devices — via HTTPS with
        the Authorization header, NOT via WebSocket.
        Returns list of device dicts, or empty list on failure.
        """
        import urllib.request

        if not self.token:
            print("get_device_list_http requires a valid token (login first)")
            return []

        # Try multiple known endpoints
        base_urls = []
        if http_base_url:
            base_urls.append(http_base_url)
        base_urls.extend([
            "https://web-eu.3irobotix.net:8001",
            "https://web-cecotec.3irobotix.net:8002",
        ])

        for base_url in base_urls:
            url = f"{base_url}/sweeper-robot-center/app/get_user_bind"
            print(f"HTTP device list: {url}")

            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "*/*")
            req.add_header("Content-Type", "application/json")
            req.add_header("authorization", self.token)

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            try:
                with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                    data = json.loads(resp.read())
                    code = data.get("code", -1)
                    print(f"  HTTP response code: {code}")

                    if code == 0:
                        result = data.get("result", [])
                        if isinstance(result, list):
                            devices = result
                        elif isinstance(result, dict):
                            devices = result.get("data", result.get("list", [result]))
                        else:
                            devices = []

                        print(f"  Found {len(devices)} device(s)")
                        for dev in devices:
                            print(f"    - Robot ID: {dev.get('robotId', dev.get('id', '?'))}")
                            print(f"      SN: {dev.get('sn', '?')}")
                            print(f"      Nickname: {dev.get('nickname', '?')}")
                            print(f"      Type: {dev.get('deviceType', '?')}")
                            print(f"      Status: {dev.get('status', '?')}")
                        return devices
                    else:
                        print(f"  Response: {json.dumps(data, indent=2)[:500]}")
            except Exception as e:
                print(f"  Error: {e}")

        return []

    def get_robot_info_http(self, mac, sn="", http_base_urls=None):
        """Look up robot by MAC/SN via HTTP REST (fallback for WebSocket).

        Tries multiple base URLs. Returns parsed result dict or None.
        """
        import urllib.request

        if not self.token:
            print("get_robot_info_http requires a valid token (login first)")
            return None

        base_urls = list(http_base_urls or [])
        base_urls.extend([
            "https://web-eu.3irobotix.net:8001",
            "https://web-cecotec.3irobotix.net:8002",
        ])

        query = f"mac={mac}"
        if sn:
            query += f"&sn={sn}"

        for base_url in base_urls:
            url = f"{base_url}/sweeper-robot-center/app/get_robot_info?{query}"
            print(f"  HTTP get_robot_info: {url}")

            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "*/*")
            req.add_header("Content-Type", "application/json")
            req.add_header("authorization", self.token)

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            try:
                with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                    data = json.loads(resp.read())
                    code = data.get("code", -1)
                    print(f"    HTTP response code: {code}")

                    if code == 0:
                        result = data.get("result", {})
                        robot_id = result.get("robotId", result.get("id"))
                        print(f"    Robot found! ID={robot_id}, SN={result.get('sn', '?')}")
                        return result
                    else:
                        print(f"    {json.dumps(data, indent=2)[:300]}")
            except Exception as e:
                print(f"    Error: {e}")

        return None

    def lock_device(self, robot_id, sn, mac):
        """Lock device to get control rights (required before bind_override).

        From MasterRequest.lockDeviceByServer().
        Endpoint: sweeper-transmit/transmit/lock

        WARNING: This modifies server state.
        """
        if not self.user_id:
            print("lock_device requires a valid user_id (login first)")
            return None

        print(f"Locking device (robotId={robot_id}, sn={sn}, mac={mac})...")
        content = json.dumps({
            "uid": self.user_id,
            "did": robot_id,
            "k": sn,
            "v": mac,
        }, separators=(",", ":"))
        resp = self.send_and_wait("sweeper-transmit/transmit/lock", content)
        if resp is None:
            print("lock_device timeout - no response")
            return None
        code = resp.get("code", -1)
        print(f"lock_device response: code={code}")
        if self.verbose or code != 0:
            print(f"  {json.dumps(resp, indent=2)}")
        # Check inner result field too (app checks result==0)
        try:
            result_str = resp.get("content", resp.get("result", ""))
            if isinstance(result_str, str) and result_str:
                inner = json.loads(result_str)
                inner_result = inner.get("result", -1)
                print(f"  Inner result: {inner_result}")
        except (json.JSONDecodeError, ValueError):
            pass
        return resp

    def get_device_status(self, device_id):
        """Get current device status (READ-ONLY)."""
        print(f"Getting status for device {device_id}...")
        content = json.dumps({
            "clientType": "ROBOT",
            "targets": [device_id],
            "data": {
                "control": "get_status",
                "type": -1,
                "value": -1,
            },
        }, separators=(",", ":"))
        resp = self.send_and_wait("sweeper-transmit/transmit/to_bind", content)
        if resp:
            print(f"Status response: {json.dumps(resp, indent=2)[:500]}")
        return resp

    def close(self):
        self._stop_heartbeat.set()
        if self.ws:
            self.ws.close()


def scan_all_regions(username, password, mac, sn, verbose=False, no_strip=False):
    """Try get_robot_info across all known regions (EU, CN/Global, US).

    Connects to each region's WebSocket, logs in, and queries get_robot_info.
    Also tries HTTP REST as fallback for each region.

    Returns (region_name, urls_dict, robot_info_dict) on success, or (None, None, None).
    """
    if no_strip:
        filtered = password
    else:
        filtered = filter_password(password, warn=False)

    for region_name in ["eu", "cn", "us"]:
        print(f"\n{'='*50}")
        print(f"Scanning region: {region_name}")
        print(f"{'='*50}")

        try:
            urls = resolve_server_urls(region_name)
        except Exception as e:
            print(f"  Failed to resolve URLs for {region_name}: {e}")
            continue

        ws_url = urls["ws"]
        client = ZACOClient(ws_url, verbose=verbose)

        if not client.connect():
            print(f"  Failed to connect to {region_name}")
            client.close()
            continue

        # Login
        lang = {"eu": "de", "cn": "zh", "us": "en"}.get(region_name, "en")
        success = client.login_authcode(username, filtered, lang=lang)
        if not success:
            client.close()
            continue

        # Try WebSocket get_robot_info
        info = client.get_robot_info(mac, sn=sn)
        if info:
            robot_id = info.get("robotId", info.get("id"))
            print(f"\n  >>> FOUND device in region '{region_name}'! Robot ID: {robot_id}")
            client.close()
            return region_name, urls, info

        # Try HTTP REST get_robot_info as fallback
        print(f"  WebSocket get_robot_info failed, trying HTTP REST...")
        http_urls = []
        if urls.get("http"):
            http_urls.append(urls["http"])
        info = client.get_robot_info_http(mac, sn=sn, http_base_urls=http_urls)
        if info:
            robot_id = info.get("robotId", info.get("id"))
            print(f"\n  >>> FOUND device via HTTP in region '{region_name}'! Robot ID: {robot_id}")
            client.close()
            return region_name, urls, info

        # Also try get_user_bind to see if any devices exist in this region
        devices = client.get_device_list()
        if devices:
            print(f"  Found {len(devices)} device(s) in get_user_bind for {region_name}:")
            for dev in devices:
                print(f"    SN={dev.get('sn', '?')}, MAC={dev.get('mac', '?')}, ID={dev.get('robotId', '?')}")

        client.close()

    print(f"\n{'='*50}")
    print("Device not found in any region.")
    print(f"{'='*50}")
    return None, None, None


def main():
    parser = argparse.ArgumentParser(
        description="Test connection to iRobotics/ZACO cloud API (READ-ONLY)"
    )
    parser.add_argument("--username", help="ZACOHome account email/username")
    parser.add_argument("--password", help="ZACOHome account password")
    parser.add_argument("--token", help="Saved auth token (from previous login)")
    parser.add_argument("--user-id", type=int, help="User ID (from previous login)")
    parser.add_argument(
        "--region", default="eu", choices=["eu", "cn", "us"],
        help="Server region (default: eu)",
    )
    parser.add_argument(
        "--monitor", action="store_true",
        help="Stay connected and monitor status updates",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show all WebSocket messages",
    )
    parser.add_argument(
        "--no-strip", action="store_true",
        help="Don't strip unsupported characters from password (send as-is)",
    )
    parser.add_argument(
        "--lang",
        help="Language code for login (default: based on region: eu=de, cn=zh, us=en)",
    )
    parser.add_argument(
        "--mac",
        help="Device MAC address for get_robot_info lookup (e.g. 34:20:03:66:4B:BA)",
    )
    parser.add_argument(
        "--sn",
        help="Device serial number for get_robot_info lookup",
    )
    parser.add_argument(
        "--robot-id", type=int,
        help="Known numeric robot ID (skip get_robot_info lookup)",
    )
    parser.add_argument(
        "--bind", action="store_true",
        help="Try bind_override if get_robot_info fails (modifies server state!)",
    )
    parser.add_argument(
        "--http-devices", action="store_true",
        help="Also try HTTP REST device list (mirrors ZACO app behavior)",
    )
    parser.add_argument(
        "--scan-regions", action="store_true",
        help="Scan all regions (EU, CN, US) for the device via get_robot_info",
    )
    parser.add_argument(
        "--provision", action="store_true",
        help="Full provisioning: get_robot_info -> lock -> bind_override -> verify",
    )
    args = parser.parse_args()

    # Default lang based on region
    if not args.lang:
        args.lang = {"eu": "de", "cn": "zh", "us": "en"}.get(args.region, "en")

    # Handle --scan-regions mode (scans all regions, then exits)
    if args.scan_regions:
        if not args.username or not args.mac:
            print("--scan-regions requires --username and --mac (and ideally --sn)")
            sys.exit(1)
        password = args.password
        if not password:
            import getpass
            password = getpass.getpass("Password: ")
        region, urls, info = scan_all_regions(
            args.username, password, args.mac, args.sn or "",
            verbose=args.verbose, no_strip=args.no_strip,
        )
        if region and info:
            robot_id = info.get("robotId", info.get("id"))
            print(f"\nDevice found in region '{region}', robot ID: {robot_id}")
            print(f"To connect directly: --region {region} --robot-id {robot_id}")
        sys.exit(0 if region else 1)

    # Resolve server URLs via OTA
    print(f"Resolving server URLs for region '{args.region}'...")
    urls = resolve_server_urls(args.region)
    ws_url = urls["ws"]
    print(f"WebSocket: {ws_url}")
    if urls["http"]:
        print(f"HTTP API:  {urls['http']}")

    # Create client
    client = ZACOClient(ws_url, verbose=args.verbose)

    if not client.connect():
        print("Failed to connect. Check your network and try a different region.")
        sys.exit(1)

    # Login
    if args.token and args.user_id:
        success = client.login_token(args.token, args.user_id)
    elif args.username:
        password = args.password
        if not password:
            import getpass
            password = getpass.getpass("Password: ")
        if args.no_strip:
            filtered = password
        else:
            filtered = filter_password(password)

        # Try login_authcode first (uses #KEY# + MD5 mechanism)
        success = client.login_authcode(args.username, filtered, lang=args.lang)

        if not success:
            # Reconnect and try regular loginByPassword
            print("\nauthcode login failed, trying regular password login...")
            client.close()
            client = ZACOClient(ws_url, verbose=args.verbose)
            if not client.connect():
                print("Failed to reconnect.")
                sys.exit(1)
            success = client.login_password(args.username, filtered, lang=args.lang)

        if not success and filtered != password:
            # Retry with raw password
            print("\nRetrying with raw (unfiltered) password via authcode...")
            client.close()
            client = ZACOClient(ws_url, verbose=args.verbose)
            if not client.connect():
                print("Failed to reconnect.")
                sys.exit(1)
            success = client.login_authcode(args.username, password, lang=args.lang)
    else:
        # Try saved credentials
        saved = load_credentials()
        if saved:
            print("Using saved credentials...")
            success = client.login_token(saved["token"], saved["userId"])
        else:
            print("No credentials provided. Use --username/--password or --token/--user-id")
            print("Or run: python3 scripts/test_connection.py --username YOUR_EMAIL --password YOUR_PASSWORD")
            client.close()
            sys.exit(1)

    if not success:
        client.close()
        sys.exit(1)

    # Device discovery
    device_id = args.robot_id  # Use --robot-id if provided directly

    if not device_id and args.mac:
        # Try get_robot_info with MAC (and optionally SN) via WebSocket
        info = client.get_robot_info(args.mac, sn=args.sn or "")
        if info:
            device_id = info.get("robotId", info.get("id"))
            if isinstance(device_id, str):
                device_id = int(device_id)

        # Try HTTP REST get_robot_info as fallback
        if not device_id:
            print("\nWebSocket get_robot_info failed, trying HTTP REST...")
            http_urls = []
            if urls.get("http"):
                http_urls.append(urls["http"])
            info = client.get_robot_info_http(args.mac, sn=args.sn or "", http_base_urls=http_urls)
            if info:
                device_id = info.get("robotId", info.get("id"))
                if isinstance(device_id, str):
                    device_id = int(device_id)

        # If get_robot_info succeeded and --provision is set, do lock + bind
        if device_id and (args.provision or args.bind):
            print(f"\n--- Provisioning (lock + bind) for robot {device_id} ---")
            lock_resp = client.lock_device(device_id, args.sn or "", args.mac)
            lock_ok = lock_resp and lock_resp.get("code") == 0
            if lock_ok:
                print("Lock acquired! Proceeding to bind_override...")
                bind_resp = client.bind_override(args.sn or "", args.mac, nickname="")
                if bind_resp and bind_resp.get("code") == 0:
                    print("Bind succeeded! Verifying with get_user_bind...")
                    time.sleep(1)
                    client.get_device_list()
            else:
                print("Lock failed. Cannot proceed to bind_override.")
                print("  (Device may be offline or already locked by another user)")

        # If get_robot_info failed and --bind/--provision is set, try lock+bind anyway
        if not device_id and (args.bind or args.provision) and args.sn:
            print("\nget_robot_info failed. Trying bind_override directly (--bind flag set)...")
            print("  Note: lock step skipped (no robotId). This may fail with code 11.")
            bind_resp = client.bind_override(args.sn, args.mac, nickname="")
            if bind_resp and bind_resp.get("code") == 0:
                print("\nBind succeeded! Retrying get_robot_info...")
                time.sleep(1)
                info = client.get_robot_info(args.mac, sn=args.sn)
                if info:
                    device_id = info.get("robotId", info.get("id"))
                    if isinstance(device_id, str):
                        device_id = int(device_id)
                if not device_id:
                    result = bind_resp.get("result", {})
                    if isinstance(result, dict):
                        device_id = result.get("robotId", result.get("id",
                            result.get("deviceId")))
                        if isinstance(device_id, str):
                            device_id = int(device_id)
        elif not device_id and args.mac and args.sn and not args.bind and not args.provision:
            print("\nTip: The device may not be registered in the 3irobotix cloud.")
            print("     Try --scan-regions to check all regions.")
            print("     Try --provision to attempt lock + bind sequence.")

    if not device_id and args.http_devices:
        # Try HTTP REST device list (mirrors Robot.getAllDevices() in the ZACO app)
        print("\n--- HTTP REST Device List ---")
        http_url = urls.get("http")
        devices = client.get_device_list_http(http_base_url=http_url)
        if devices:
            device_id = devices[0].get("robotId", devices[0].get("id"))
            if isinstance(device_id, str):
                device_id = int(device_id)

    if not device_id:
        # Fall back to get_user_bind via WebSocket (may return empty for Aliyun-managed devices)
        devices = client.get_device_list()
        if devices:
            device_id = devices[0].get("robotId", devices[0].get("id"))
        else:
            print("No devices found via get_user_bind.")
            if not args.mac:
                print("\nTo look up your device, you need its MAC address and serial number (SN).")
                print("The SN is printed on a label on the bottom of the vacuum.")
                print("Usage: --mac AA:BB:CC:DD:EE:FF --sn YOUR_SERIAL_NUMBER")
            elif not args.sn:
                print("\nget_robot_info requires both MAC and SN (serial number).")
                print("The SN is printed on a label on the bottom of the vacuum.")
                print("Usage: --mac 34:20:03:66:4B:BA --sn YOUR_SERIAL_NUMBER")

    # Get device status
    if device_id:
        print(f"\n--- Device {device_id} ---")
        client.is_robot_online(device_id)
        client.get_device_status(device_id)
    else:
        print("No device ID available. Cannot query device status.")

    if args.monitor:
        print("\n=== MONITORING MODE ===")
        print("Listening for status updates. Press Ctrl+C to stop.\n")
        try:
            while client.connected:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping monitor...")
    else:
        time.sleep(2)  # Wait for any late responses

    client.close()
    print("Done.")


if __name__ == "__main__":
    main()
