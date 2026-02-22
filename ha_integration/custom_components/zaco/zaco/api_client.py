"""Async client for the Aliyun IoT Living Platform (ZACO vacuum control).

Ported from scripts/test_aliyun.py to use aiohttp for Home Assistant compatibility.
All signing logic is synchronous (pure computation); HTTP calls are async.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import ssl
import time
import uuid
from email.utils import formatdate
from typing import Any

import aiohttp

try:
    from ..const import (
        APP_KEY,
        APP_SECRET,
        IOT_HOST_DEFAULT,
        OA_HOST_DEFAULT,
        REGION_DISCOVERY_HOST,
        RSA_PUBLIC_KEY_B64,
        TOKEN_REFRESH_MARGIN,
    )
except ImportError:
    from const import (
        APP_KEY,
        APP_SECRET,
        IOT_HOST_DEFAULT,
        OA_HOST_DEFAULT,
        REGION_DISCOVERY_HOST,
        RSA_PUBLIC_KEY_B64,
        TOKEN_REFRESH_MARGIN,
    )

_LOGGER = logging.getLogger(__name__)

# SSL context that skips verification (matching the original client behavior).
# We use a bare SSLContext instead of ssl.create_default_context() because:
# 1. create_default_context() calls load_default_certs() which does blocking
#    disk I/O — HA detects this as a blocking call and crashes.
# 2. We disable cert verification anyway, so loading system CAs is pointless.
_SSL_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AliyunApiError(Exception):
    """General Aliyun API error."""


class AliyunAuthError(AliyunApiError):
    """Authentication failed (wrong credentials, CAPTCHA, etc.)."""


class AliyunTokenExpiredError(AliyunApiError):
    """Both iotToken and refreshToken have expired."""


class AliyunConnectionError(AliyunApiError):
    """Network/connection error."""


# ---------------------------------------------------------------------------
# RSA Password Encryption
# ---------------------------------------------------------------------------

def _rsa_encrypt_password(password: str) -> str:
    """RSA-encrypt a password for OA login. Returns base64-encoded ciphertext.

    Uses the hardcoded public key from RSAKey.java (PKCS1v15 padding).
    """
    from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    pub_key = load_der_public_key(base64.b64decode(RSA_PUBLIC_KEY_B64))
    encrypted = pub_key.encrypt(password.encode("utf-8"), rsa_padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("ascii")


# ---------------------------------------------------------------------------
# Request signing helpers (synchronous, pure computation)
# ---------------------------------------------------------------------------

def _compute_content_md5(body_bytes: bytes) -> str:
    """Content-MD5: base64(md5(body)), truncated to 24 chars."""
    md5_digest = hashlib.md5(body_bytes).digest()
    b64 = base64.b64encode(md5_digest).decode("utf-8")
    return b64[:24]


def _build_canonicalized_headers(headers: dict[str, str]) -> tuple[str, str]:
    """Build CanonicalizedHeaders from x-ca-* headers.

    Returns (canon_headers_str, signature_headers_value).
    """
    ca_headers = {}
    for key, value in headers.items():
        if key.lower().startswith("x-ca-"):
            ca_headers[key.lower()] = value

    all_keys = sorted(list(ca_headers.keys()) + ["x-ca-signature-headers"])
    sig_headers_value = ",".join(all_keys)

    ca_headers["x-ca-signature-headers"] = sig_headers_value
    sorted_keys = sorted(ca_headers.keys())

    canon = ""
    for key in sorted_keys:
        canon += f"{key}:{ca_headers[key]}\n"

    return canon, sig_headers_value


def _build_canonicalized_resource(
    path: str, query_params: dict[str, str] | None
) -> str:
    """Build CanonicalizedResource from path and sorted query params."""
    resource = path
    if query_params:
        sorted_params = sorted(query_params.items())
        param_str = "&".join(
            f"{k}={v}" if v else k for k, v in sorted_params
        )
        resource += "?" + param_str
    return resource


def _build_string_to_sign(
    method: str,
    headers: dict[str, str],
    canon_headers: str,
    canon_resource: str,
) -> str:
    """Build the string-to-sign per Alibaba Cloud API Gateway spec."""
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


def _compute_signature(string_to_sign: str, secret: str) -> str:
    """HMAC-SHA1 signature, base64-encoded."""
    mac = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    )
    return base64.b64encode(mac.digest()).decode("utf-8")


def _sign_request(
    host: str,
    path: str,
    body_bytes: bytes,
    query_params: dict[str, str] | None = None,
    form_params: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[str, dict[str, str], bytes]:
    """Build a fully signed request. Returns (url, headers_dict, body_bytes)."""
    import urllib.parse

    if query_params is None:
        query_params = {}

    is_form = form_params is not None

    now = time.time()
    timestamp_ms = str(int(now * 1000))
    nonce = str(uuid.uuid4())
    date_str = formatdate(timeval=now, usegmt=True)

    if is_form:
        content_type = "application/x-www-form-urlencoded; charset=utf-8"
    else:
        content_type = "application/octet-stream; charset=utf-8"

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

    if extra_headers:
        for k, v in extra_headers.items():
            if v:
                headers[k.lower()] = v

    if not is_form and body_bytes:
        headers["content-md5"] = _compute_content_md5(body_bytes)

    canon_headers, sig_headers_value = _build_canonicalized_headers(headers)
    headers["x-ca-signature-headers"] = sig_headers_value

    resource_params = dict(query_params)
    if is_form:
        resource_params.update(form_params)

    canon_resource = _build_canonicalized_resource(path, resource_params)
    sts = _build_string_to_sign("POST", headers, canon_headers, canon_resource)
    signature = _compute_signature(sts, APP_SECRET)
    headers["x-ca-signature"] = signature
    headers["CA_VERSION"] = "1"

    url = f"https://{host}{path}"
    if query_params:
        param_str = "&".join(f"{k}={v}" for k, v in query_params.items())
        url += "?" + param_str

    if is_form:
        body_bytes = "&".join(
            f"{k}={urllib.parse.quote(v, safe='')}"
            for k, v in form_params.items()
        ).encode("utf-8")

    return url, headers, body_bytes


def _build_iot_body(
    path_id: str,
    api_version: str,
    params: dict[str, Any],
    iot_token: str | None = None,
) -> bytes:
    """Build the IoT API request body (JSON bytes)."""
    request_section: dict[str, Any] = {
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


def _build_risk_control_info() -> dict[str, str]:
    """Build riskControlInfo dict for OA requests."""
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


# ---------------------------------------------------------------------------
# AliyunApiClient
# ---------------------------------------------------------------------------

class AliyunApiClient:
    """Async client for the Aliyun IoT Living Platform."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self.iot_host: str = IOT_HOST_DEFAULT
        self.oa_host: str = OA_HOST_DEFAULT
        self.iot_token: str | None = None
        self.refresh_token: str | None = None
        self.identity_id: str | None = None
        self.region_id: str | None = None
        self.iot_token_expiry: float = 0
        self.refresh_token_expiry: float = 0

    @classmethod
    async def from_saved_tokens(
        cls,
        session: aiohttp.ClientSession,
        *,
        iot_host: str | None = None,
        iot_token: str | None = None,
        refresh_token: str | None = None,
        identity_id: str | None = None,
        iot_token_expiry: float = 0,
        refresh_token_expiry: float = 0,
    ) -> "AliyunApiClient":
        """Create a client with pre-existing auth state.

        Does NOT call ensure_token_valid() — callers (e.g. the HA
        coordinator) are responsible for token validation before use.
        This avoids blocking HTTP calls during HA startup.
        """
        client = cls(session)
        if iot_host:
            client.iot_host = iot_host
        client.iot_token = iot_token
        client.refresh_token = refresh_token
        client.identity_id = identity_id
        client.iot_token_expiry = iot_token_expiry
        client.refresh_token_expiry = refresh_token_expiry
        return client

    # -- Low-level HTTP -------------------------------------------------------

    async def _send_signed_request(
        self,
        host: str,
        path: str,
        body_bytes: bytes,
        query_params: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Sign and send an HTTPS POST request, return parsed JSON."""
        url, headers, body = _sign_request(host, path, body_bytes, query_params)
        try:
            async with self._session.post(
                url, headers=headers, data=body, ssl=_SSL_CONTEXT, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status >= 400:
                    error_body = await resp.text()
                    ca_error = resp.headers.get("X-Ca-Error-Message", "")
                    _LOGGER.error(
                        "HTTP %s from %s: %s %s",
                        resp.status, url, ca_error, error_body[:200],
                    )
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise AliyunConnectionError(f"Request to {host}{path} failed: {err}") from err

    async def _send_signed_form_request(
        self,
        host: str,
        path: str,
        form_params: dict[str, str],
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Sign and send a form-POST request, return parsed JSON."""
        url, headers, body = _sign_request(
            host, path, b"", form_params=form_params, extra_headers=extra_headers,
        )
        try:
            async with self._session.post(
                url, headers=headers, data=body, ssl=_SSL_CONTEXT, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status >= 400:
                    error_body = await resp.text()
                    _LOGGER.error("HTTP %s from %s: %s", resp.status, url, error_body[:200])
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise AliyunConnectionError(f"Request to {host}{path} failed: {err}") from err

    async def _send_iot_request(
        self,
        path: str,
        api_version: str,
        params: dict[str, Any],
        use_iot_token: bool = True,
    ) -> dict[str, Any] | None:
        """Send an IoT API request with standard body format."""
        request_id = str(uuid.uuid4())
        query_params = {"x-ca-request-id": request_id}
        iot_token = self.iot_token if use_iot_token else None
        body = _build_iot_body(request_id, api_version, params, iot_token)
        return await self._send_signed_request(self.iot_host, path, body, query_params)

    # -- Authentication -------------------------------------------------------

    async def lookup_region(self, email: str) -> dict[str, Any] | None:
        """Discover the correct API region for this account."""
        request_id = str(uuid.uuid4())
        params = {
            "type": "EMAIL",
            "countryCode": "",
            "email": email,
            "phoneLocationCode": "",
        }
        body = _build_iot_body(request_id, "1.0.2", params)
        query_params = {"x-ca-request-id": request_id}

        resp = await self._send_signed_request(
            REGION_DISCOVERY_HOST, "/living/account/region/get",
            body, query_params,
        )
        if resp is None or resp.get("code") != 200:
            _LOGGER.error("Region lookup failed: %s", resp)
            return None

        data = resp.get("data", {})
        iot_host = data.get("apiGatewayEndpoint")
        oa_host = data.get("oaApiGatewayEndpoint")

        if iot_host:
            self.iot_host = iot_host
        if oa_host:
            self.oa_host = oa_host
        self.region_id = data.get("regionId")

        _LOGGER.debug(
            "Region discovered: %s (IoT: %s, OA: %s)",
            self.region_id, self.iot_host, self.oa_host,
        )
        return data

    async def oa_login(self, email: str, password: str) -> str:
        """Login with email+password via OpenAccount. Returns OA sessionId."""
        loop = asyncio.get_running_loop()
        encrypted_password = await loop.run_in_executor(
            None, _rsa_encrypt_password, password
        )

        login_request = {
            "loginId": email,
            "password": encrypted_password,
            "riskControlInfo": _build_risk_control_info(),
        }

        form_params = {
            "loginRequest": json.dumps(login_request, separators=(",", ":")),
        }

        resp = await self._send_signed_form_request(
            self.oa_host, "/api/prd/login.json", form_params,
        )

        if resp is None:
            raise AliyunAuthError("OA login failed: no response")

        outer_data = resp.get("data", {})
        code = outer_data.get("code", -1)

        if code != 1:
            message = outer_data.get("message", "unknown error")
            if code == 4027:
                raise AliyunAuthError("Wrong username or password")
            if code in (26053, 26152):
                raise AliyunAuthError("CAPTCHA challenge required, try again later")
            raise AliyunAuthError(f"OA login failed: code={code}, message={message}")

        inner_data = outer_data.get("data", {})
        login_result = inner_data.get("loginSuccessResult", {})
        session_id = login_result.get("sid")

        if not session_id:
            raise AliyunAuthError("OA login: no sessionId in response")

        _LOGGER.debug("OA login successful, sid=%s...", session_id[:20])
        return session_id

    async def create_session(self, session_id: str) -> bool:
        """Exchange OA sessionId for iotToken."""
        params = {
            "request": {
                "authCode": session_id,
                "appKey": APP_KEY,
                "accountType": "OA_SESSION",
            }
        }
        resp = await self._send_iot_request(
            "/account/createSessionByAuthCode", "1.0.4", params,
            use_iot_token=False,
        )
        if resp is None or resp.get("code") != 200:
            _LOGGER.error("create_session failed: %s", resp)
            return False

        data = resp.get("data", {})
        self.iot_token = data.get("iotToken")
        self.refresh_token = data.get("refreshToken")
        self.identity_id = data.get("identityId")

        now = time.time()
        iot_expire = data.get("iotTokenExpire", 7200)
        refresh_expire = data.get("refreshTokenExpire", 2592000)
        self.iot_token_expiry = now + iot_expire
        self.refresh_token_expiry = now + refresh_expire

        if self.iot_token:
            _LOGGER.debug(
                "Got iotToken (expires in %ss), identityId=%s",
                iot_expire, self.identity_id,
            )
            return True

        _LOGGER.error("create_session: no iotToken in response")
        return False

    async def refresh_session(self) -> bool:
        """Refresh an expired iotToken using the refreshToken."""
        if not self.refresh_token or not self.identity_id:
            _LOGGER.error("Cannot refresh: no refreshToken or identityId")
            return False

        _LOGGER.debug("Refreshing iotToken...")
        params = {
            "request": {
                "refreshToken": self.refresh_token,
                "identityId": self.identity_id,
            }
        }
        resp = await self._send_iot_request(
            "/account/checkOrRefreshSession", "1.0.4", params,
            use_iot_token=False,
        )
        if resp is None or resp.get("code") != 200:
            _LOGGER.error("refresh_session failed: %s", resp)
            return False

        data = resp.get("data", {})
        new_token = data.get("iotToken")
        if not new_token:
            _LOGGER.error("refresh_session: no iotToken in response")
            return False

        self.iot_token = new_token
        new_refresh = data.get("refreshToken")
        if new_refresh:
            self.refresh_token = new_refresh
        new_identity = data.get("identityId")
        if new_identity:
            self.identity_id = new_identity

        now = time.time()
        self.iot_token_expiry = now + data.get("iotTokenExpire", 7200)
        self.refresh_token_expiry = now + data.get("refreshTokenExpire", 2592000)

        _LOGGER.debug("iotToken refreshed successfully")
        return True

    async def ensure_token_valid(self) -> None:
        """Check token expiry and refresh if needed.

        Raises AliyunTokenExpiredError if both tokens are expired.
        """
        now = time.time()

        if now < self.iot_token_expiry - TOKEN_REFRESH_MARGIN:
            return  # Token still good

        if now < self.refresh_token_expiry - TOKEN_REFRESH_MARGIN:
            success = await self.refresh_session()
            if success:
                return
            _LOGGER.warning("Token refresh failed, will retry next poll")
            return

        raise AliyunTokenExpiredError("All tokens expired, re-authentication required")

    # -- MQTT credentials -----------------------------------------------------

    async def get_mqtt_credentials(self) -> dict[str, str]:
        """Obtain MQTT connection credentials via /app/aepauth/handle.

        Returns dict with keys: productKey, deviceName, deviceSecret.
        Raises AliyunApiError on failure.
        """
        import random
        import string

        def _random_string(length: int) -> str:
            chars = string.ascii_letters + string.digits
            return "".join(random.choice(chars) for _ in range(length))

        device_sn = _random_string(32)
        client_id = _random_string(8)
        timestamp = str(int(time.time() * 1000))

        # Sign: HmacSHA1(appSecret, "appKey"+val+"clientId"+val+"deviceSn"+val+"timestamp"+val)
        sign_string = (
            f"appKey{APP_KEY}"
            f"clientId{client_id}"
            f"deviceSn{device_sn}"
            f"timestamp{timestamp}"
        )
        mac = hmac.new(
            APP_SECRET.encode("utf-8"),
            sign_string.encode("utf-8"),
            hashlib.sha1,
        )
        sign = mac.hexdigest().lower()

        auth_info = {
            "timestamp": timestamp,
            "clientId": client_id,
            "deviceSn": device_sn,
            "sign": sign,
        }

        request_id = str(uuid.uuid4())
        params = {"authInfo": auth_info}
        body = _build_iot_body(request_id, "1.0.0", params)
        query_params = {"x-ca-request-id": request_id}

        resp = await self._send_signed_request(
            self.iot_host, "/app/aepauth/handle", body, query_params
        )
        if resp is None or resp.get("code") != 200:
            msg = resp.get("message", "unknown") if resp else "no response"
            raise AliyunApiError(f"aepauth failed: {msg}")

        data = resp.get("data", {})
        product_key = data.get("productKey")
        device_name = data.get("deviceName")
        device_secret = data.get("deviceSecret")

        if not all([product_key, device_name, device_secret]):
            raise AliyunApiError(f"aepauth: incomplete response: {data}")

        _LOGGER.debug("Got MQTT credentials: pk=%s, dn=%s...", product_key, device_name[:20])
        return {
            "productKey": product_key,
            "deviceName": device_name,
            "deviceSecret": device_secret,
        }

    # -- Device API -----------------------------------------------------------

    async def list_devices(self) -> list[dict[str, Any]]:
        """List bound devices."""
        resp = await self._send_iot_request("/uc/listBindingByAccount", "1.0.2", {})
        if resp is None or resp.get("code") != 200:
            _LOGGER.error("list_devices failed: %s", resp)
            return []

        data = resp.get("data", [])
        if isinstance(data, dict):
            devices = data.get("data", data.get("list", []))
        elif isinstance(data, list):
            devices = data
        else:
            devices = []

        _LOGGER.debug("Found %d device(s)", len(devices))
        return devices

    async def get_property_timeline(
        self,
        iot_id: str,
        identifier: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        """Fetch historical property values within a time window (paginated).

        Uses /thing/property/timeline/get (same as GetHistoryRoadDelegateX9).
        Returns a chronologically-ordered list of items, each containing a
        ``data`` dict (the property value) and ``timestamp`` (ms).
        """
        all_items: list[dict[str, Any]] = []
        current_end = end_ms
        max_pages = 50  # safety limit: 10,000 items max
        for _ in range(max_pages):
            params: dict[str, Any] = {
                "iotId": iot_id,
                "identifier": identifier,
                "start": start_ms,
                "end": current_end,
                "pageSize": 200,
                "ordered": False,
            }
            resp = await self._send_iot_request(
                "/thing/property/timeline/get", "1.0.2", params
            )
            if resp is None or resp.get("code") != 200:
                _LOGGER.warning("get_property_timeline failed: %s", resp)
                break
            items = resp.get("data", {}).get("items", [])
            if not items:
                break
            all_items.extend(items)
            if len(items) < 200:
                break  # last page
            # Paginate backwards: last item's timestamp becomes new end
            next_end = items[-1].get("timestamp", start_ms)
            if next_end >= current_end or next_end <= start_ms:
                break  # cursor not advancing or past start
            current_end = next_end
        # API returns newest-first; reverse for chronological order
        all_items.reverse()
        return all_items

    async def get_properties(
        self, iot_id: str, items: list[str]
    ) -> dict[str, Any] | None:
        """Get device properties."""
        params = {
            "iotId": iot_id,
            "items": items,
        }
        resp = await self._send_iot_request("/thing/properties/get", "1.0.2", params)
        if resp is None or resp.get("code") != 200:
            _LOGGER.error("get_properties failed: %s", resp)
            return None
        return resp.get("data", {})

    async def set_properties(
        self, iot_id: str, items: dict[str, Any]
    ) -> bool:
        """Set device properties."""
        params = {
            "iotId": iot_id,
            "items": items,
        }
        resp = await self._send_iot_request("/thing/properties/set", "1.0.2", params)
        if resp is None or resp.get("code") != 200:
            _LOGGER.error("set_properties failed: %s", resp)
            return False
        return True

    # -- Room data helpers ----------------------------------------------------

    @staticmethod
    def parse_map_room_info(
        b64_string: str,
    ) -> tuple[int | str | None, list[tuple[int, str]]]:
        """Parse a base64-encoded MapRoomInfo string into room ID/name pairs.

        Delegates to room_utils.parse_map_room_info.
        """
        try:
            from .room_utils import parse_map_room_info
        except ImportError:
            from room_utils import parse_map_room_info
        return parse_map_room_info(b64_string)
