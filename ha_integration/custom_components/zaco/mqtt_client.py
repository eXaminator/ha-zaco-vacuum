"""MQTT real-time push client for Aliyun IoT Living Platform.

Maintains a persistent MQTT connection to receive device property change
notifications in real time (instead of polling).

Connection flow (from MobileChannelImpl / MqttNet / BaseSDKGlue):
  1. Get MQTT credentials via REST /app/aepauth/handle
  2. Connect to broker with TLS (securemode=2) on port 1883
  3. Subscribe to /sys/{pk}/{dn}/app/down/#
  4. Bind account by publishing iotToken to /sys/{pk}/{dn}/app/up/account/bind
  5. Receive property pushes on /sys/{pk}/{dn}/app/down/thing/properties
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import ssl
import time
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

try:
    from .const import (
        MQTT_HOST_DEFAULT,
        MQTT_KEEPALIVE,
        MQTT_PORT,
        MQTT_RECONNECT_MAX,
        MQTT_RECONNECT_MIN,
    )
except ImportError:
    from const import (  # type: ignore[no-redef]
        MQTT_HOST_DEFAULT,
        MQTT_KEEPALIVE,
        MQTT_PORT,
        MQTT_RECONNECT_MAX,
        MQTT_RECONNECT_MIN,
    )

_LOGGER = logging.getLogger(__name__)


def _compute_mqtt_password(params_map: dict[str, str], device_secret: str) -> str:
    """HMAC-SHA1(deviceSecret, sorted key+value pairs) → hex uppercase.

    From MqttNet.java method a(Map, String).
    """
    content = ""
    for key in sorted(params_map.keys()):
        if key.lower() != "sign":
            content += key + params_map[key]
    mac = hmac.new(
        device_secret.encode("utf-8"),
        content.encode("utf-8"),
        hashlib.sha1,
    )
    return mac.digest().hex().upper()


class ZacoMqttClient:
    """Manages a persistent MQTT connection for real-time property push."""

    def __init__(
        self,
        on_properties: Callable[[dict[str, Any]], None],
        mqtt_host: str = MQTT_HOST_DEFAULT,
    ) -> None:
        self._on_properties = on_properties
        self._mqtt_host = mqtt_host
        self._client: mqtt.Client | None = None
        self._connected = False
        self._bound = False
        self._iot_token: str | None = None
        self._product_key: str | None = None
        self._device_name: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reconnect_delay = MQTT_RECONNECT_MIN
        self._reconnect_task: asyncio.Task | None = None
        self._stopping = False

    @property
    def connected(self) -> bool:
        """Return True if MQTT is connected and account is bound."""
        return self._connected and self._bound

    async def start(
        self,
        credentials: dict[str, str],
        iot_token: str,
    ) -> None:
        """Connect to the MQTT broker and bind account.

        credentials: dict with productKey, deviceName, deviceSecret
        iot_token: current iotToken for account binding
        """
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._iot_token = iot_token
        self._product_key = credentials["productKey"]
        self._device_name = credentials["deviceName"]
        device_secret = credentials["deviceSecret"]

        client_id_base = f"{self._device_name}&{self._product_key}"

        params_map = {
            "productKey": self._product_key,
            "deviceName": self._device_name,
            "clientId": client_id_base,
        }
        password = _compute_mqtt_password(params_map, device_secret)

        mqtt_client_id = (
            f"{client_id_base}"
            f"|securemode=2"
            f",_v=1.5.3"
            f",lan=Python"
            f",signmethod=hmacsha1"
            f",ext=1|"
        )

        self._client = mqtt.Client(
            client_id=mqtt_client_id,
            protocol=mqtt.MQTTv311,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self._client.username_pw_set(
            f"{self._device_name}&{self._product_key}", password
        )
        self._client.tls_set(cert_reqs=ssl.CERT_NONE)
        self._client.tls_insecure_set(True)

        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        _LOGGER.debug("Connecting to MQTT broker %s:%s", self._mqtt_host, MQTT_PORT)
        await self._loop.run_in_executor(
            None, self._client.connect, self._mqtt_host, MQTT_PORT, MQTT_KEEPALIVE
        )
        self._client.loop_start()

    async def stop(self) -> None:
        """Disconnect and clean up."""
        self._stopping = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
        self._connected = False
        self._bound = False
        _LOGGER.debug("MQTT client stopped")

    def update_iot_token(self, new_token: str) -> None:
        """Update iotToken and re-bind account (after token refresh)."""
        self._iot_token = new_token
        if self._connected and self._client:
            self._publish_bind()

    # -- paho callbacks (run in paho's network thread) ------------------------

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        if reason_code == 0:
            _LOGGER.info("MQTT connected to %s", self._mqtt_host)
            self._connected = True
            self._reconnect_delay = MQTT_RECONNECT_MIN

            sub_topic = (
                f"/sys/{self._product_key}/{self._device_name}/app/down/#"
            )
            client.subscribe(sub_topic, qos=0)
        else:
            _LOGGER.error("MQTT connection failed: rc=%s", reason_code)
            self._connected = False

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: Any,
        mid: int,
        reason_codes: Any,
        properties: Any,
    ) -> None:
        _LOGGER.debug("MQTT subscribed (mid=%s)", mid)
        self._publish_bind()

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        topic = msg.topic

        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            _LOGGER.debug("MQTT non-JSON on %s: %s bytes", topic, len(msg.payload))
            return

        # Bind response
        if "account/bind_reply" in topic:
            code = payload.get("code", -1)
            if code == 200:
                self._bound = True
                _LOGGER.info("MQTT account bound successfully")
            else:
                _LOGGER.error("MQTT bind failed: %s", payload)
                self._bound = False
            return

        # Property push on /app/down/thing/properties
        if "thing/properties" in topic:
            items = payload.get("items")
            if isinstance(items, dict) and self._loop:
                # Normalize items to {key: value} format matching REST API
                normalized: dict[str, Any] = {}
                for key, val in items.items():
                    if isinstance(val, dict) and "value" in val:
                        normalized[key] = val
                    else:
                        normalized[key] = {"value": val}

                self._loop.call_soon_threadsafe(
                    self._on_properties, normalized
                )
            return

        _LOGGER.debug("MQTT message on %s: %s bytes", topic, len(msg.payload))

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        self._connected = False
        self._bound = False

        if self._stopping:
            _LOGGER.debug("MQTT disconnected (stopping)")
            return

        _LOGGER.warning(
            "MQTT disconnected (rc=%s), will reconnect in %ss",
            reason_code,
            self._reconnect_delay,
        )
        if self._loop:
            self._reconnect_task = self._loop.call_soon_threadsafe(
                self._schedule_reconnect
            )

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnect attempt on the event loop."""
        if self._stopping:
            return
        self._reconnect_task = asyncio.ensure_future(self._reconnect())

    async def _reconnect(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        while not self._stopping and not self._connected:
            _LOGGER.debug("MQTT reconnecting in %ss", self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)

            if self._stopping:
                return

            self._reconnect_delay = min(
                self._reconnect_delay * 2, MQTT_RECONNECT_MAX
            )

            try:
                if self._client and self._loop:
                    await self._loop.run_in_executor(
                        None, self._client.reconnect
                    )
                    return  # on_connect will handle the rest
            except Exception:
                _LOGGER.debug(
                    "MQTT reconnect failed, retrying in %ss",
                    self._reconnect_delay,
                    exc_info=True,
                )

    def _publish_bind(self) -> None:
        """Publish account bind message."""
        if not self._client or not self._iot_token:
            return

        bind_topic = (
            f"/sys/{self._product_key}/{self._device_name}"
            f"/app/up/account/bind"
        )
        bind_payload = json.dumps({
            "id": "1",
            "system": {
                "version": "1.0",
                "time": str(int(time.time() * 1000)),
            },
            "request": {
                "clientId": f"{self._device_name}&{self._product_key}",
            },
            "params": {
                "iotToken": self._iot_token,
            },
        })

        _LOGGER.debug("Publishing account bind to %s", bind_topic)
        self._client.publish(bind_topic, bind_payload, qos=0)
