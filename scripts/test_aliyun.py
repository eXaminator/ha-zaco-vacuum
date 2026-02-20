#!/usr/bin/env python3
"""
test_aliyun.py - Aliyun IoT Living Platform client for ZACO A10 vacuum

Implements the real authentication and device query flow as observed
from MITM capture of the ZACO Home app (v1.7.7).

Flow:
  1. Region lookup → discover correct API endpoints for the account
  2. OA login → get session ID (sid)
  3. createSessionByAuthCode → exchange sid for iotToken
  4. listBindingByAccount → list devices
  5. thing/properties/get → read device status
  6. thing/properties/set → send commands

Usage:
    # Login with email/password (auto-discovers region):
    python3 scripts/test_aliyun.py --username YOUR_EMAIL --password YOUR_PASSWORD

    # With saved tokens (after first login):
    python3 scripts/test_aliyun.py

    # With pre-obtained iotToken (from MITM capture):
    python3 scripts/test_aliyun.py --iot-token TOKEN

    # Set a property on the device:
    python3 scripts/test_aliyun.py --set WorkMode=1

    # List available rooms:
    python3 scripts/test_aliyun.py --rooms

    # Clean specific rooms (by room ID):
    python3 scripts/test_aliyun.py --clean-rooms 1,2

    # Clean rooms with 2 passes:
    python3 scripts/test_aliyun.py --clean-rooms 1 --clean-passes 2

    # Verbose mode (shows signing details):
    python3 scripts/test_aliyun.py --verbose

Prerequisites:
    pip3 install cryptography   (only needed for --username/--password login)
"""

import argparse
import base64
import hashlib
import hmac
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from email.utils import formatdate
from pathlib import Path

# Optional: needed only for --username/--password login (RSA encryption)
try:
    from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False


PROJECT_DIR = Path(__file__).resolve().parent.parent
TOKEN_FILE = PROJECT_DIR / ".aliyun_tokens.json"

# Aliyun IoT app credentials (from SDKInitHelper.java, appTag=3 ZACO)
APP_KEY = "28416395"
APP_SECRET = "a2a5fdb0aa8555d31d80016454f2b248"

# Region discovery host (always cn-shanghai, determines user's actual region)
REGION_DISCOVERY_HOST = "cn-shanghai.api-iot.aliyuncs.com"

# Default hosts (used when region lookup is skipped or fails)
IOT_HOST_DEFAULT = "eu-central-1.api-iot.aliyuncs.com"
OA_HOST_DEFAULT = "living-account.eu-central-1.aliyuncs.com"

# Legacy hosts (kept for --host override compatibility)
IOT_HOST_INTL = "api-iot.ap-southeast-1.aliyuncs.com"  # international/singapore
IOT_HOST_CN = "api.link.aliyun.com"  # china
OA_HOST_INTL = "sgp-sdk.openaccount.aliyun.com"  # international
OA_HOST_CN = "sdk.openaccount.aliyun.com"  # china

# RSA public key for password encryption (from RSAKey.java — hardcoded, not fetched)
RSA_PUBLIC_KEY_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAl4EFDk91/ArPHjyX7UBz"
    "ofPTAD3pcP8FMgOs83hvLEcbFJOVASrPAjbJTuXsSZJd9tYPwKbuqlGqndvdl2Kn2z"
    "LFpLOcFAYOyaIDFzDOCWQw/kMjcm1U08BvPE7dbtkGM23lCyTBlDMHWJvUz3JVTZm"
    "6ApGWEOGRhs1rECjcS9HXttnllQ2gTtBAW5Xjb8tzDgWR0jMaHzduCcSimHPtQO4Os"
    "h4Op3ianRocbb9o/4OR8HgKdbaKO3Sq2+pYV7FveXmfXqUr5lH7oHji+4j5TaU4WXR"
    "GKOjHSVXtN0UrfCXtsWE0aGCXXQN78NJUf5VrJMh14mqiSrR07wgu3UG7OwIDAQAB"
)

# Default device (user's A10) — full iotId includes 000000 suffix
DEFAULT_IOT_ID = "KReBFAPbEXU5Yk31mDep000000"


def build_risk_control_info():
    """Build riskControlInfo dict for OA requests.

    From OpenAccountRiskControlContext.buildRiskContext() / getEnvironmentInfo().
    Values matched to MITM capture of ZACO Home v1.7.7.
    """
    return {
        "platformName": "android",
        "platformVersion": "34",
        "appVersion": "69",
        "sdkVersion": "3.4.2",
        "brand": "google",
        "model": "sdk_gphone64_arm64",
        "appID": "com.zaco.home.robot",
        "appVersionName": "1.7.7",
        "locale": "en_US",
        "netType": "wifi",
        "USE_OA_PWD_ENCRYPT": "true",
        "signType": "RSA",
        "USE_H5_NC": "true",
        "utdid": "ffffffffffffffffffffffff",
        "umidToken": "",
        "appAuthToken": "",
        "routerMac": "02:00:00:00:00:00",
        "yunOSId": "",
        "deviceId": str(uuid.uuid4()),
    }

# Device properties to query (from EnvConfigure.java)
VACUUM_PROPERTIES = [
    "WorkMode",
    "BatteryState",
    "FanPower",
    "WaterTankContrl",
    "CurrentMode",
    "ErrorCode",
    "CleanTime",
    "CleanArea",
    "SoftwareVer",
    "HardwareVer",
    "Fault",
    "Area",
    "Volume",
    "CarpetTurbo",
]


# ---------------------------------------------------------------------------
# RSA Password Encryption
# ---------------------------------------------------------------------------
# From Rsa.java: RSA/ECB/PKCS1Padding with hardcoded public key from RSAKey.java

def rsa_encrypt_password(password):
    """RSA-encrypt a password for OA login. Returns base64-encoded ciphertext."""
    if not HAS_CRYPTOGRAPHY:
        print("Error: 'cryptography' package is required for password login.")
        print("Install it with: pip3 install cryptography")
        sys.exit(1)
    pub_key = load_der_public_key(base64.b64decode(RSA_PUBLIC_KEY_B64))
    encrypted = pub_key.encrypt(password.encode("utf-8"), rsa_padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("ascii")


# ---------------------------------------------------------------------------
# Alibaba Cloud API Gateway Signing
# ---------------------------------------------------------------------------
# Implements the signing algorithm from:
#   ApiRequestMaker.java  — header assembly, content-md5
#   SignUtil.java          — string-to-sign, canonicalization
#   HMacSHA1SignerFactory  — HMAC computation

def compute_content_md5(body_bytes):
    """Compute Content-MD5: base64(md5(body)), truncated to 24 chars.

    From ApiRequestMaker.base64AndMD5(): computes MD5, base64-encodes it,
    then copies exactly 24 bytes of the result.
    """
    md5_digest = hashlib.md5(body_bytes).digest()
    b64 = base64.b64encode(md5_digest).decode("utf-8")
    return b64[:24]


def build_canonicalized_headers(headers):
    """Build CanonicalizedHeaders from all x-ca-* headers.

    From SignUtil.buildHeaders():
    1. Collect headers starting with "x-ca-"
    2. Build comma-separated list for x-ca-signature-headers
    3. Add x-ca-signature-headers itself to the collection
    4. Sort alphabetically (TreeMap)
    5. Format as "key:value\\n" lines

    Returns (canon_headers_str, signature_headers_value).
    """
    # Collect x-ca-* header names (lowercase)
    ca_headers = {}
    for key, value in headers.items():
        if key.lower().startswith("x-ca-"):
            ca_headers[key.lower()] = value

    # Build the signature-headers list, add itself, then sort
    all_keys = sorted(list(ca_headers.keys()) + ["x-ca-signature-headers"])
    sig_headers_value = ",".join(all_keys)

    # Now build the canonicalized string with the full set
    ca_headers["x-ca-signature-headers"] = sig_headers_value
    sorted_keys = sorted(ca_headers.keys())

    canon = ""
    for key in sorted_keys:
        canon += f"{key}:{ca_headers[key]}\n"

    return canon, sig_headers_value


def build_canonicalized_resource(path, query_params):
    """Build CanonicalizedResource from path and sorted query params.

    From SignUtil.buildResource():
    path + "?" + sorted(key=value) joined by "&"
    """
    resource = path
    if query_params:
        sorted_params = sorted(query_params.items())
        param_str = "&".join(
            f"{k}={v}" if v else k for k, v in sorted_params
        )
        resource += "?" + param_str
    return resource


def build_string_to_sign(method, headers, canon_headers, canon_resource):
    """Build the string-to-sign.

    From SignUtil.buildStringToSign():
    METHOD\\n
    Accept\\n
    Content-MD5\\n
    Content-Type\\n
    Date\\n
    CanonicalizedHeaders + CanonicalizedResource
    """
    accept = headers.get("accept", "")
    content_md5 = headers.get("content-md5", "")
    content_type = headers.get("content-type", "")
    date = headers.get("date", "")

    return (
        f"{method}\n"
        f"{accept}\n"
        f"{content_md5}\n"
        f"{content_type}\n"
        f"{date}\n"
        f"{canon_headers}"
        f"{canon_resource}"
    )


def compute_signature(string_to_sign, secret):
    """Compute HMAC-SHA1 signature, base64-encoded.

    From HMacSHA1SignerFactory / SecurityImpl.sign():
    HMAC-SHA1(secret, string_to_sign) -> base64
    """
    mac = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    )
    return base64.b64encode(mac.digest()).decode("utf-8")


def sign_request(host, path, body_bytes, query_params=None,
                 form_params=None, extra_headers=None, verbose=False):
    """Build a fully signed request and return (url, headers_dict, body_bytes).

    Implements the full flow from ApiRequestMaker.make().

    Two modes:
    - POST_BODY (default): body_bytes is sent as-is, content-md5 computed from body
    - POST_FORM (form_params set): form params are URL-encoded into body,
      merged into CanonicalizedResource for signing, no content-md5
    """
    if query_params is None:
        query_params = {}

    is_form = form_params is not None

    now = time.time()
    timestamp_ms = str(int(now * 1000))
    nonce = str(uuid.uuid4())
    # RFC 2822 date in GMT (matching Java's "EEE, dd MMM yyyy HH:mm:ss z")
    date_str = formatdate(timeval=now, usegmt=True)

    # Content-type depends on mode (from HttpMethod.java)
    if is_form:
        content_type = "application/x-www-form-urlencoded; charset=utf-8"
    else:
        content_type = "application/octet-stream; charset=utf-8"

    # Assemble headers (all lowercase keys, matching Java's addHeader behavior)
    headers = {
        "date": date_str,
        "x-ca-timestamp": timestamp_ms,
        "x-ca-nonce": nonce,
        "user-agent": "ALIYUN-ANDROID-DEMO",
        "host": host,
        "x-ca-key": APP_KEY,
        "content-type": content_type,
        "accept": "application/json; charset=utf-8",
        "x-ca-signature-method": "HmacSHA1",
    }

    # Extra headers (e.g. sid for OA)
    if extra_headers:
        for k, v in extra_headers.items():
            if v:
                headers[k.lower()] = v

    # Content-MD5: only for POST_BODY with non-empty body
    # For POST_FORM, apiRequest.getBody() is null so no content-md5
    if not is_form and body_bytes:
        headers["content-md5"] = compute_content_md5(body_bytes)

    # Build canonicalized components
    canon_headers, sig_headers_value = build_canonicalized_headers(headers)
    headers["x-ca-signature-headers"] = sig_headers_value

    # For form-POST, form params are merged into CanonicalizedResource
    # (from SignUtil.buildResource() which merges querys + formParams)
    resource_params = dict(query_params)
    if is_form:
        resource_params.update(form_params)

    canon_resource = build_canonicalized_resource(path, resource_params)

    # Build string-to-sign
    sts = build_string_to_sign("POST", headers, canon_headers, canon_resource)

    if verbose:
        print(f"\n[SIGNING] String-to-sign:")
        for i, line in enumerate(sts.split("\n")):
            print(f"  [{i}] {repr(line)}")

    # Compute signature
    signature = compute_signature(sts, APP_SECRET)
    headers["x-ca-signature"] = signature

    if verbose:
        print(f"[SIGNING] Signature: {signature}")

    # CA_VERSION is NOT x-ca-* so not included in signing, but sent as header
    headers["CA_VERSION"] = "1"

    # Build URL (query params only, NOT form params)
    url = f"https://{host}{path}"
    if query_params:
        param_str = "&".join(f"{k}={v}" for k, v in query_params.items())
        url += "?" + param_str

    # For form-POST, build the body from form params
    # (from HttpCommonUtil.buildParamString: key=URLEncode(value)&...)
    if is_form:
        body_bytes = "&".join(
            f"{k}={urllib.parse.quote(v, safe='')}"
            for k, v in form_params.items()
        ).encode("utf-8")

    return url, headers, body_bytes


def send_signed_request(host, path, body_bytes, query_params=None, verbose=False):
    """Sign and send an HTTPS POST request, return parsed JSON response."""
    url, headers, body = sign_request(host, path, body_bytes, query_params,
                                      verbose=verbose)

    if verbose:
        print(f"[REQUEST] POST {url}")
        print(f"[REQUEST] Body ({len(body)} bytes): {body[:200]}")

    req = urllib.request.Request(url, data=body, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            resp_body = resp.read()
            if verbose:
                print(f"[RESPONSE] Status: {resp.status}")
                print(f"[RESPONSE] Body: {resp_body[:500]}")
            return json.loads(resp_body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        error_headers = dict(e.headers) if e.headers else {}
        ca_error = error_headers.get("X-Ca-Error-Message", "")
        print(f"[HTTP ERROR] {e.code}: {e.reason}")
        if ca_error:
            print(f"[HTTP ERROR] X-Ca-Error-Message: {ca_error}")
        if error_body:
            print(f"[HTTP ERROR] Body: {error_body[:500]}")
        if verbose:
            print(f"[HTTP ERROR] Headers: {error_headers}")
        return None
    except Exception as e:
        print(f"[ERROR] {e}")
        return None


def send_signed_form_request(host, path, form_params, extra_headers=None,
                             verbose=False):
    """Sign and send a form-POST request (for OA login). Returns parsed JSON."""
    url, headers, body = sign_request(
        host, path, b"", form_params=form_params,
        extra_headers=extra_headers, verbose=verbose,
    )

    if verbose:
        print(f"[REQUEST] POST {url}")
        # Mask password in form body
        debug_body = body.decode("utf-8", errors="replace")
        if "password" in debug_body and len(debug_body) > 200:
            debug_body = debug_body[:200] + "...(truncated)"
        print(f"[REQUEST] Form body: {debug_body}")

    req = urllib.request.Request(url, data=body, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            resp_body = resp.read()
            if verbose:
                print(f"[RESPONSE] Status: {resp.status}")
                print(f"[RESPONSE] Body: {resp_body[:500]}")
            return json.loads(resp_body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        error_headers = dict(e.headers) if e.headers else {}
        ca_error = error_headers.get("X-Ca-Error-Message", "")
        print(f"[HTTP ERROR] {e.code}: {e.reason}")
        if ca_error:
            print(f"[HTTP ERROR] X-Ca-Error-Message: {ca_error}")
        if error_body:
            print(f"[HTTP ERROR] Body: {error_body[:500]}")
        if verbose:
            print(f"[HTTP ERROR] Headers: {error_headers}")
        return None
    except Exception as e:
        print(f"[ERROR] {e}")
        return None


# ---------------------------------------------------------------------------
# IoT Request Body Builder
# ---------------------------------------------------------------------------

def build_iot_body(path_id, api_version, params, iot_token=None):
    """Build the IoT API request body (JSON bytes).

    From IoTRequestPayload.java / IoTRequestWrapper.buildBody().
    Includes $ref fields matching the real app's request format.
    """
    request_section = {
        "apiVer": api_version,
        "language": "en-US",
    }
    if iot_token:
        request_section["iotToken"] = iot_token

    payload = {
        "a": path_id,
        "b": "1.0",
        "c": request_section,
        "d": params,
        "id": path_id,
        "params": {"$ref": "$.d"},
        "request": {"$ref": "$.c"},
        "version": "1.0",
    }

    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Token Persistence
# ---------------------------------------------------------------------------

def save_tokens(iot_token, refresh_token, identity_id,
                iot_token_expire=7200, refresh_token_expire=2592000,
                host=None):
    """Save Aliyun IoT tokens for reuse."""
    data = {
        "iotToken": iot_token,
        "refreshToken": refresh_token,
        "identityId": identity_id,
        "iotTokenExpire": iot_token_expire,
        "refreshTokenExpire": refresh_token_expire,
        "savedAt": int(time.time()),
    }
    if host:
        data["host"] = host
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    print(f"Tokens saved to {TOKEN_FILE}")


def load_tokens():
    """Load saved tokens. Returns dict or None."""
    if TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text())
            saved_at = data.get("savedAt", 0)
            iot_expire = data.get("iotTokenExpire", 7200)
            refresh_expire = data.get("refreshTokenExpire", 2592000)
            now = int(time.time())

            iot_expired = (now - saved_at) >= iot_expire
            refresh_expired = (now - saved_at) >= refresh_expire

            data["_iot_expired"] = iot_expired
            data["_refresh_expired"] = refresh_expired

            age_str = f"{(now - saved_at) // 60}m ago"
            if iot_expired:
                if refresh_expired:
                    print(f"Saved tokens fully expired ({age_str})")
                else:
                    print(f"iotToken expired ({age_str}), refreshToken still valid")
            else:
                print(f"Loaded saved tokens ({age_str}, still valid)")

            return data
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Warning: Could not load saved tokens: {e}")
    return None


# ---------------------------------------------------------------------------
# AliyunIoTClient
# ---------------------------------------------------------------------------

class AliyunIoTClient:
    def __init__(self, host=None, oa_host=None, verbose=False):
        self.host = host or IOT_HOST_DEFAULT
        self.oa_host = oa_host or OA_HOST_DEFAULT
        self.verbose = verbose
        self.iot_token = None
        self.refresh_token = None
        self.identity_id = None
        self.region_id = None

    def _send(self, path, api_version, params, use_iot_token=True):
        """Send an IoT API request."""
        request_id = str(uuid.uuid4())
        query_params = {"x-ca-request-id": request_id}

        iot_token = self.iot_token if use_iot_token else None
        body = build_iot_body(request_id, api_version, params, iot_token)

        return send_signed_request(
            self.host, path, body, query_params, self.verbose
        )

    def lookup_region(self, email):
        """Discover the correct API region for this account.

        The app always calls cn-shanghai first to find which regional
        endpoints to use. Returns region info dict on success, None on failure.
        """
        print(f"Looking up region for {email}...")
        request_id = str(uuid.uuid4())
        params = {
            "type": "EMAIL",
            "countryCode": "",
            "email": email,
            "phoneLocationCode": "",
        }
        body = build_iot_body(request_id, "1.0.2", params)
        query_params = {"x-ca-request-id": request_id}

        resp = send_signed_request(
            REGION_DISCOVERY_HOST, "/living/account/region/get",
            body, query_params, self.verbose,
        )
        if resp is None:
            print("Region lookup failed: no response")
            return None

        code = resp.get("code", -1)
        if code != 200:
            print(f"Region lookup failed: code={code}")
            if self.verbose:
                print(f"  Response: {json.dumps(resp, indent=2)[:500]}")
            return None

        data = resp.get("data", {})
        region_id = data.get("regionId")
        iot_host = data.get("apiGatewayEndpoint")
        oa_host = data.get("oaApiGatewayEndpoint")
        mqtt_endpoint = data.get("mqttEndpoint")
        region_name = data.get("regionEnglishName", region_id)

        print(f"  Region: {region_name} ({region_id})")
        print(f"  IoT API: {iot_host}")
        print(f"  OA API: {oa_host}")
        print(f"  MQTT: {mqtt_endpoint}")

        # Update client hosts
        if iot_host:
            self.host = iot_host
        if oa_host:
            self.oa_host = oa_host
        self.region_id = region_id

        return data

    def check_account_exist(self, email):
        """Check if an account exists in OpenAccount (pre-login step).

        From CheckOpenAccountExistTask.java, MobileFragment.java.
        The app calls this BEFORE password login. It may establish
        server-side session state required for login.

        Returns dict with 'exists' and 'has_password' bools, or None on error.
        """
        print(f"Checking account existence for {email}...")

        form_params = {
            "checkAccountExistRequest": json.dumps({
                "loginId": email,
                "riskControlInfo": build_risk_control_info(),
            }, separators=(",", ":")),
        }

        resp = send_signed_form_request(
            self.oa_host, "/api/prd/checkaccountexist.json", form_params,
            verbose=self.verbose,
        )

        if resp is None:
            print("check_account_exist failed: no response")
            return None

        outer_data = resp.get("data", {})
        code = outer_data.get("code", -1)

        if code != 1:
            message = outer_data.get("message", "unknown error")
            print(f"check_account_exist: code={code}, message={message}")
            if self.verbose:
                print(f"  Full response: {json.dumps(resp, indent=2)}")
            return None

        inner_data = outer_data.get("data", {})
        exists = inner_data.get("accountExist", False)
        has_password = inner_data.get("accountHasPassword", False)

        print(f"  Account exists: {exists}")
        print(f"  Has password: {has_password}")

        if self.verbose:
            print(f"  Full response: {json.dumps(inner_data, indent=2)}")

        return {"exists": exists, "has_password": has_password}

    def oa_login(self, email, password):
        """Login with email+password via OpenAccount.

        From ApiGatewayRpcServiceImpl.java, OpenAccountLoginTask.java.
        Returns the OA sessionId on success, or None on failure.
        """
        print(f"Logging in to OpenAccount as {email}...")

        # RSA-encrypt password (from Rsa.java, RSAKey.java)
        encrypted_password = rsa_encrypt_password(password)

        # Build loginRequest JSON (from buildQueryParam + PwdLoginFragment)
        # riskControlInfo is NESTED inside loginRequest (from RpcUtils.pureInvokeWithRiskControlInfo)
        login_request = {
            "loginId": email,
            "password": encrypted_password,
            "riskControlInfo": build_risk_control_info(),
        }

        # Form params (from ApiGatewayRpcServiceImpl.buildQueryParam)
        form_params = {
            "loginRequest": json.dumps(login_request, separators=(",", ":")),
        }

        # Path: /api/prd/{target}.json (from buildPath, target="login")
        resp = send_signed_form_request(
            self.oa_host, "/api/prd/login.json", form_params,
            verbose=self.verbose,
        )

        if resp is None:
            print("OA login failed: no response")
            return None

        # Parse response (from processMtopResponse + toLoginResult)
        # Response: {"data": {"code": 1, "data": {"loginSuccessResult": {"sid": ...}}}}
        outer_data = resp.get("data", {})
        code = outer_data.get("code", -1)

        if code != 1:
            message = outer_data.get("message", "unknown error")
            sub_code = outer_data.get("subCode", "")
            print(f"OA login failed: code={code}, message={message}")
            if sub_code:
                print(f"  subCode: {sub_code}")
            if code == 4027:
                print("  (Wrong username or password)")
            elif code in (26053, 26152):
                print("  (CAPTCHA challenge required — try again later)")
            # Always show full response for debugging login issues
            print(f"  Full response: {json.dumps(resp, indent=2)[:800]}")
            return None

        inner_data = outer_data.get("data", {})
        login_result = inner_data.get("loginSuccessResult", {})
        session_id = login_result.get("sid")

        if not session_id:
            print("OA login: no sessionId in response")
            print(f"  Response: {json.dumps(resp, indent=2)[:500]}")
            return None

        # Extract user info
        oa_user = login_result.get("openAccount", {})
        nick = oa_user.get("nick", oa_user.get("displayName", ""))
        oa_refresh = login_result.get("refreshToken", "")

        print(f"OA login successful!")
        print(f"  Session ID: {session_id[:40]}...")
        if nick:
            print(f"  Nickname: {nick}")

        # Save OA refresh token for future use
        if oa_refresh:
            oa_token_data = {
                "oaSessionId": session_id,
                "oaRefreshToken": oa_refresh,
                "savedAt": int(time.time()),
            }
            oa_file = PROJECT_DIR / ".aliyun_oa_tokens.json"
            oa_file.write_text(json.dumps(oa_token_data, indent=2))
            if self.verbose:
                print(f"  OA tokens saved to {oa_file}")

        return session_id

    def create_session(self, session_id):
        """Exchange OA sessionId for iotToken.

        From IoTCredentialUtils.getCreateIoTCredentialRequest()
        (default OA_SESSION path).
        """
        print("Exchanging OA session for iotToken...")
        params = {
            "request": {
                "authCode": session_id,
                "appKey": APP_KEY,
                "accountType": "OA_SESSION",
            }
        }
        resp = self._send(
            "/account/createSessionByAuthCode", "1.0.4", params,
            use_iot_token=False,
        )
        if resp is None:
            print("create_session failed: no response")
            return False

        code = resp.get("code", -1)
        if code != 200:
            print(f"create_session failed: code={code}")
            print(f"  Response: {json.dumps(resp, indent=2)[:500]}")
            return False

        data = resp.get("data", {})
        self.iot_token = data.get("iotToken")
        self.refresh_token = data.get("refreshToken")
        self.identity_id = data.get("identityId")
        iot_expire = data.get("iotTokenExpire", 7200)
        refresh_expire = data.get("refreshTokenExpire", 2592000)

        if self.iot_token:
            print(f"Got iotToken: {self.iot_token[:40]}...")
            print(f"  identityId: {self.identity_id}")
            print(f"  iotToken expires in {iot_expire}s")
            print(f"  refreshToken expires in {refresh_expire}s")
            save_tokens(
                self.iot_token, self.refresh_token, self.identity_id,
                iot_expire, refresh_expire, host=self.host,
            )
            return True
        else:
            print(f"create_session: no iotToken in response")
            print(f"  Response: {json.dumps(resp, indent=2)[:500]}")
            return False

    def refresh_session(self):
        """Refresh an expired iotToken using the refreshToken.

        From IoTCredentialUtils.getRefreshIoTCredentialRequest().
        """
        if not self.refresh_token or not self.identity_id:
            print("Cannot refresh: no refreshToken or identityId")
            return False

        print("Refreshing iotToken...")
        params = {
            "request": {
                "refreshToken": self.refresh_token,
                "identityId": self.identity_id,
            }
        }
        resp = self._send(
            "/account/checkOrRefreshSession", "1.0.4", params,
            use_iot_token=False,
        )
        if resp is None:
            print("refresh_session failed: no response")
            return False

        code = resp.get("code", -1)
        if code != 200:
            print(f"refresh_session failed: code={code}")
            print(f"  Response: {json.dumps(resp, indent=2)[:500]}")
            return False

        data = resp.get("data", {})
        new_token = data.get("iotToken")
        new_refresh = data.get("refreshToken")
        new_identity = data.get("identityId")
        iot_expire = data.get("iotTokenExpire", 7200)
        refresh_expire = data.get("refreshTokenExpire", 2592000)

        if new_token:
            self.iot_token = new_token
            if new_refresh:
                self.refresh_token = new_refresh
            if new_identity:
                self.identity_id = new_identity
            print(f"Refreshed iotToken: {self.iot_token[:40]}...")
            save_tokens(
                self.iot_token, self.refresh_token, self.identity_id,
                iot_expire, refresh_expire, host=self.host,
            )
            return True
        else:
            print(f"refresh_session: no iotToken in response")
            print(f"  Response: {json.dumps(resp, indent=2)[:500]}")
            return False

    def list_devices(self):
        """List bound devices (READ-ONLY).

        From EnvConfigure.PATH_LIST_BINDING.
        """
        print("Listing bound devices...")
        resp = self._send("/uc/listBindingByAccount", "1.0.2", {})
        if resp is None:
            print("list_devices failed: no response")
            return []

        code = resp.get("code", -1)
        if code != 200:
            print(f"list_devices: code={code}")
            print(f"  Response: {json.dumps(resp, indent=2)[:500]}")
            return []

        data = resp.get("data", [])
        if isinstance(data, list):
            devices = data
        elif isinstance(data, dict):
            devices = data.get("data", data.get("list", [data]))
        else:
            devices = []

        print(f"Found {len(devices)} device(s)")
        for dev in devices:
            iot_id = dev.get("iotId", dev.get("deviceId", "?"))
            name = dev.get("nickName", dev.get("deviceName", "?"))
            product_key = dev.get("productKey", "?")
            status = dev.get("status", "?")
            print(f"  - iotId: {iot_id}")
            print(f"    Name: {name}")
            print(f"    Product Key: {product_key}")
            print(f"    Status: {status}")

        return devices

    def get_properties(self, iot_id, property_keys=None):
        """Get device properties (READ-ONLY).

        From EnvConfigure.PATH_GET_PROPERTIES / IlifeAli.ping().
        """
        if property_keys is None:
            property_keys = VACUUM_PROPERTIES

        print(f"Getting properties for {iot_id}...")
        params = {
            "iotId": iot_id,
            "items": property_keys,
        }
        resp = self._send("/thing/properties/get", "1.0.2", params)
        if resp is None:
            print("get_properties failed: no response")
            return None

        code = resp.get("code", -1)
        if code != 200:
            print(f"get_properties: code={code}")
            print(f"  Response: {json.dumps(resp, indent=2)[:500]}")
            return None

        data = resp.get("data", {})
        print("Device properties:")
        if isinstance(data, dict):
            for key, val in sorted(data.items()):
                if isinstance(val, dict):
                    value = val.get("value", val)
                    ts = val.get("time", "")
                    print(f"  {key}: {value}" + (f"  (ts={ts})" if ts else ""))
                else:
                    print(f"  {key}: {val}")
        else:
            print(f"  {json.dumps(data, indent=2)[:500]}")

        return data

    def set_properties(self, iot_id, properties):
        """Set device properties.

        From EnvConfigure.PATH_SET_PROPERTIES / thing.service.property.set.
        properties: dict of {key: value} pairs to set.
        """
        print(f"Setting properties for {iot_id}: {properties}")
        params = {
            "iotId": iot_id,
            "items": properties,
        }
        resp = self._send("/thing/properties/set", "1.0.2", params)
        if resp is None:
            print("set_properties failed: no response")
            return None

        code = resp.get("code", -1)
        if code != 200:
            print(f"set_properties: code={code}")
            print(f"  Response: {json.dumps(resp, indent=2)[:500]}")
            return None

        print("Properties set successfully")
        if self.verbose:
            print(f"  Response: {json.dumps(resp, indent=2)[:500]}")
        return resp.get("data")

    def get_property_timeline(self, iot_id, identifier, start_ms, end_ms):
        """Fetch historical property values within a time window (paginated).

        Uses /thing/property/timeline/get (from EnvConfigure.PATH_GET_PROPERTY_TIMELINE).
        Returns list of items in chronological order.
        """
        print(f"Getting property timeline: {identifier}, start={start_ms}, end={end_ms}...")
        all_items = []
        current_end = end_ms
        page = 0
        while True:
            page += 1
            params = {
                "iotId": iot_id,
                "identifier": identifier,
                "start": start_ms,
                "end": current_end,
                "pageSize": 200,
                "ordered": False,
            }
            resp = self._send("/thing/property/timeline/get", "1.0.2", params)
            if resp is None or resp.get("code", -1) != 200:
                print(f"  Page {page}: failed — {resp}")
                break
            items = resp.get("data", {}).get("items", [])
            print(f"  Page {page}: {len(items)} items")
            if not items:
                break
            all_items.extend(items)
            if len(items) < 200:
                break
            current_end = items[-1].get("timestamp", start_ms)
            if current_end <= start_ms:
                break

        all_items.reverse()  # chronological order
        print(f"  Total: {len(all_items)} items")
        return all_items


# ---------------------------------------------------------------------------
# MapRoomInfo Parser
# ---------------------------------------------------------------------------

def parse_map_room_info(b64_string):
    """Parse a base64-encoded MapRoomInfo string into room ID/name pairs.

    Format (from SelectMapPresenter.addMapName()):
      base64(mapId,,roomId1,roomName1,roomId2,roomName2,...)

    Returns (map_id, [(room_id, room_name), ...]) or (None, []) on error.
    """
    try:
        decoded = base64.b64decode(b64_string).decode("utf-8")
    except Exception:
        return None, []

    fields = decoded.split(",")
    if len(fields) < 2:
        return None, []

    try:
        map_id = int(fields[0])
    except ValueError:
        map_id = fields[0]

    # fields[1] is empty (unused map name), room pairs start at [2]
    rooms = []
    i = 2
    while i + 1 < len(fields):
        try:
            room_id = int(fields[i])
            room_name = fields[i + 1]
            rooms.append((room_id, room_name))
        except ValueError:
            pass
        i += 2

    return map_id, rooms


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Aliyun IoT Living Platform client for ZACO A10"
    )

    # Login with email/password (like the app)
    parser.add_argument("--username", help="ZACOHome account email")
    parser.add_argument("--password", help="ZACOHome account password")

    # Token-based auth (advanced)
    parser.add_argument("--iot-token", help="Pre-obtained iotToken")
    parser.add_argument("--refresh-token", help="Refresh token")
    parser.add_argument("--identity-id", help="Identity ID")
    parser.add_argument("--session-id", help="OA sessionId (from MITM)")

    # Device
    parser.add_argument(
        "--iot-id", default=DEFAULT_IOT_ID,
        help=f"Device iotId (default: {DEFAULT_IOT_ID})",
    )
    parser.add_argument(
        "--host",
        help="Override IoT API host (default: auto-detect via region lookup)",
    )

    # Actions
    parser.add_argument(
        "--check-account", metavar="EMAIL",
        help="Check if account exists in Aliyun OpenAccount (diagnostic)",
    )
    parser.add_argument(
        "--list-devices", action="store_true",
        help="List bound devices",
    )
    parser.add_argument(
        "--get-properties", action="store_true",
        help="Get device properties",
    )
    parser.add_argument(
        "--set", dest="set_props", metavar="KEY=VALUE", nargs="+",
        help="Set device properties (e.g. --set WorkMode=1 FanPower=3). "
             "JSON values supported: --set 'CleanPartitionData={\"PartitionData\":1,\"CleanLoop\":1,\"Enable\":1}'",
    )
    parser.add_argument(
        "--rooms", action="store_true",
        help="List available rooms from saved map",
    )
    parser.add_argument(
        "--clean-rooms", dest="clean_rooms", metavar="ROOM_IDS",
        help="Clean specific rooms (comma-separated IDs, e.g. --clean-rooms 1,2,3)",
    )
    parser.add_argument(
        "--clean-passes", dest="clean_passes", type=int, default=1,
        help="Number of cleaning passes (1-3, default: 1)",
    )

    parser.add_argument(
        "--road-timeline", dest="road_timeline", type=int, metavar="MINUTES",
        help="Fetch RealMapRoadData timeline for the last N minutes "
             "(tests the /thing/property/timeline/get endpoint)",
    )

    parser.add_argument(
        "--verbose", action="store_true",
        help="Show signing details and full request/response",
    )

    args = parser.parse_args()

    client = AliyunIoTClient(
        host=args.host,
        verbose=args.verbose,
    )

    # --- Diagnostic: check account existence ---
    if args.check_account:
        client.lookup_region(args.check_account)
        result = client.check_account_exist(args.check_account)
        if result is None:
            sys.exit(1)
        sys.exit(0 if result["exists"] else 1)

    # --- Token acquisition ---

    # Priority 1: CLI-provided iotToken
    if args.iot_token:
        client.iot_token = args.iot_token
        client.refresh_token = args.refresh_token
        client.identity_id = args.identity_id
        print(f"Using provided iotToken: {args.iot_token[:40]}...")
        if args.refresh_token and args.identity_id:
            save_tokens(args.iot_token, args.refresh_token, args.identity_id)

    # Priority 2: Login with email+password
    elif args.username:
        password = args.password
        if not password:
            import getpass
            password = getpass.getpass("Password: ")

        # Step 1: Discover region (sets self.host and self.oa_host)
        if not args.host:
            region = client.lookup_region(args.username)
            if region:
                print()
            else:
                print("Warning: Region lookup failed, using default EU hosts")
                print()

        # Step 2: Login
        session_id = client.oa_login(args.username, password)
        if not session_id:
            print("OA login failed")
            sys.exit(1)
        print()

        # Step 3: Exchange session for iotToken
        if not client.create_session(session_id):
            print("Failed to exchange OA session for iotToken")
            sys.exit(1)
        print()

    # Priority 3: CLI-provided sessionId
    elif args.session_id:
        if not client.create_session(args.session_id):
            print("Failed to exchange session for iotToken")
            sys.exit(1)

    # Priority 4: Saved tokens
    else:
        saved = load_tokens()
        if saved:
            client.iot_token = saved.get("iotToken")
            client.refresh_token = saved.get("refreshToken")
            client.identity_id = saved.get("identityId")

            # Restore region host if saved
            saved_host = saved.get("host")
            if saved_host and not args.host:
                client.host = saved_host

            # Auto-refresh if iotToken expired
            if saved.get("_iot_expired") and not saved.get("_refresh_expired"):
                if not client.refresh_session():
                    print("Token refresh failed. Try --username/--password.")
                    sys.exit(1)
            elif saved.get("_refresh_expired"):
                print("All tokens expired. Use --username/--password to re-login.")
                sys.exit(1)
        else:
            print("No tokens available.")
            print()
            print("Login with your ZACOHome account:")
            print("  python3 scripts/test_aliyun.py --username YOUR_EMAIL --password YOUR_PASSWORD")
            print()
            print("Or provide a pre-obtained iotToken:")
            print("  python3 scripts/test_aliyun.py --iot-token YOUR_TOKEN")
            sys.exit(1)

    if not client.iot_token:
        print("No iotToken available.")
        sys.exit(1)

    # --- Actions ---

    has_action = (args.list_devices or args.get_properties or args.set_props
                  or args.rooms or args.clean_rooms or args.road_timeline)

    # Default: list devices + get properties
    if not has_action:
        args.list_devices = True
        args.get_properties = True

    if args.list_devices:
        client.list_devices()
        print()

    if args.get_properties:
        client.get_properties(args.iot_id)
        print()

    if args.rooms:
        # Fetch map room info — values are base64-encoded CSV strings
        map_props = ["SaveMap", "MapRoomInfo1", "MapRoomInfo2", "MapRoomInfo3"]
        data = client.get_properties(args.iot_id, map_props)
        if data:
            # Determine active map from SaveMap
            save_map_raw = data.get("SaveMap", {})
            save_map_val = save_map_raw.get("value", save_map_raw) if isinstance(save_map_raw, dict) else save_map_raw
            if isinstance(save_map_val, str):
                try:
                    save_map_val = json.loads(save_map_val)
                except (json.JSONDecodeError, ValueError):
                    pass
            selected_map = None
            if isinstance(save_map_val, dict):
                selected_map = save_map_val.get("SelectedMapId")

            # Parse each MapRoomInfo slot (base64-encoded CSV)
            found_rooms = False
            for i in range(1, 4):
                key = f"MapRoomInfo{i}"
                raw = data.get(key, {})
                val = raw.get("value", raw) if isinstance(raw, dict) else raw
                if not val or not isinstance(val, str):
                    continue

                map_id, rooms = parse_map_room_info(val)
                if rooms:
                    is_active = " (active)" if selected_map and selected_map == i else ""
                    print(f"\nMap {i}{is_active} (id={map_id}):")
                    found_rooms = True
                    max_id_width = max(len(str(r[0])) for r in rooms)
                    for room_id, room_name in rooms:
                        print(f"  Room {room_id:>{max_id_width}}: {room_name}")
                elif args.verbose:
                    print(f"\n{key}: could not parse (raw={val[:80]}...)")

            if not found_rooms:
                print("\nNo room data found in MapRoomInfo1-3.")
        print()

    if args.road_timeline:
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (args.road_timeline * 60 * 1000)
        end_ms = now_ms + 60_000
        items = client.get_property_timeline(
            args.iot_id, "RealMapRoadData", start_ms, end_ms,
        )
        if items:
            # Decode and count total path points
            total_points = 0
            for item in items:
                item_data = item.get("data")
                if isinstance(item_data, str):
                    try:
                        item_data = json.loads(item_data)
                    except (json.JSONDecodeError, ValueError):
                        continue
                if isinstance(item_data, dict):
                    road_b64 = item_data.get("RoadData", "")
                    if road_b64:
                        raw = base64.b64decode(road_b64)
                        total_points += len(raw) // 4
            print(f"\nTotal path points across {len(items)} chunks: {total_points}")
            # Show first and last item timestamps
            if items:
                first_ts = items[0].get("timestamp", 0)
                last_ts = items[-1].get("timestamp", 0)
                print(f"Time range: {first_ts} → {last_ts} "
                      f"({(last_ts - first_ts) / 1000:.0f}s span)")
            if args.verbose:
                for i, item in enumerate(items[:5]):
                    print(f"\n  Item {i}: ts={item.get('timestamp')}")
                    print(f"    data={json.dumps(item.get('data', ''))[:200]}")
                if len(items) > 5:
                    print(f"\n  ... ({len(items) - 5} more items)")
        print()

    if args.clean_rooms:
        passes = max(1, min(3, args.clean_passes))
        # Parse comma-separated room IDs
        try:
            room_ids = [int(r.strip()) for r in args.clean_rooms.split(",")]
        except ValueError:
            print(f"Error: --clean-rooms expects comma-separated integers, got: {args.clean_rooms}")
            sys.exit(1)

        # Build CleanPartitionData for each room
        # From SelectRoomActivity.java: PartitionData = room_id bitmask or single ID,
        # CleanLoop = number of passes, Enable = 1 to start
        partition_data = sum(room_ids)  # room IDs are summed as a bitmask
        clean_cmd = {
            "CleanPartitionData": {
                "PartitionData": partition_data,
                "CleanLoop": passes,
                "Enable": 1,
            }
        }
        print(f"Sending room clean command: rooms={room_ids}, passes={passes}")
        if args.verbose:
            print(f"  Payload: {json.dumps(clean_cmd, indent=2)}")
        client.set_properties(args.iot_id, clean_cmd)
        print()

    if args.set_props:
        props = {}
        for kv in args.set_props:
            if "=" not in kv:
                print(f"Error: --set values must be KEY=VALUE, got: {kv}")
                sys.exit(1)
            key, value = kv.split("=", 1)
            # Try JSON first (for objects/arrays), then int/float/bool
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (dict, list)):
                    value = parsed
                else:
                    value = parsed
            except (json.JSONDecodeError, ValueError):
                try:
                    value = int(value)
                except ValueError:
                    try:
                        value = float(value)
                    except ValueError:
                        if value.lower() in ("true", "false"):
                            value = value.lower() == "true"
            props[key] = value
        client.set_properties(args.iot_id, props)


if __name__ == "__main__":
    main()
