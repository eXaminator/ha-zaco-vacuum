"""Config flow for ZACO integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_client import AliyunApiClient, AliyunAuthError, AliyunConnectionError
from .const import (
    CONF_IDENTITY_ID,
    CONF_IOT_HOST,
    CONF_IOT_ID,
    CONF_IOT_TOKEN,
    CONF_IOT_TOKEN_EXPIRY,
    CONF_OA_HOST,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_EXPIRY,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class ZacoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ZACO."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._client: AliyunApiClient | None = None
        self._user_input: dict[str, Any] = {}
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial email/password step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = AliyunApiClient(session)

            try:
                # 1. Discover region
                region = await client.lookup_region(user_input[CONF_EMAIL])
                if not region:
                    errors["base"] = "region_lookup_failed"
                else:
                    # 2. Login
                    sid = await client.oa_login(
                        user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                    )

                    # 3. Exchange for iotToken
                    if not await client.create_session(sid):
                        errors["base"] = "session_failed"
                    else:
                        # 4. List devices
                        devices = await client.list_devices()
                        if not devices:
                            errors["base"] = "no_devices"
                        elif len(devices) == 1:
                            return self._create_entry(
                                user_input, client, devices[0]
                            )
                        else:
                            # Multiple devices — show selection
                            self._client = client
                            self._user_input = user_input
                            self._devices = devices
                            return await self.async_step_device()

            except AliyunAuthError:
                errors["base"] = "invalid_auth"
            except AliyunConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle device selection (when account has multiple devices)."""
        if user_input is not None:
            selected = next(
                d for d in self._devices if d["iotId"] == user_input["device"]
            )
            return self._create_entry(self._user_input, self._client, selected)

        device_options = {
            d["iotId"]: f"{d.get('nickName', 'Unknown')} ({d.get('productModel', '')})"
            for d in self._devices
        }

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {vol.Required("device"): vol.In(device_options)}
            ),
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Handle re-authentication when tokens expire."""
        return await self.async_step_user()

    def _create_entry(
        self,
        user_input: dict[str, Any],
        client: AliyunApiClient,
        device: dict[str, Any],
    ) -> FlowResult:
        """Create a config entry for the selected device."""
        iot_id = device["iotId"]
        nick = device.get("nickName", "ZACO Vacuum")

        # Prevent duplicate entries for the same device
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=nick,
            data={
                CONF_EMAIL: user_input[CONF_EMAIL],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_IOT_HOST: client.iot_host,
                CONF_OA_HOST: client.oa_host,
                CONF_IOT_ID: iot_id,
                CONF_IOT_TOKEN: client.iot_token,
                CONF_REFRESH_TOKEN: client.refresh_token,
                CONF_IDENTITY_ID: client.identity_id,
                CONF_IOT_TOKEN_EXPIRY: client.iot_token_expiry,
                CONF_REFRESH_TOKEN_EXPIRY: client.refresh_token_expiry,
            },
        )
