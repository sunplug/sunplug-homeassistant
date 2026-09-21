"""Config flow for Sunplug."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CannotConnect, InvalidCode, async_claim
from .const import CONF_INGEST_URL, CONF_TOKEN, DOMAIN
from .energy_map import async_resolve_roles

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema({vol.Required("pairing_code"): str})


def _normalize_code(raw: str) -> str:
    return raw.strip().upper()


class SunplugConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Sunplug config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial pairing step."""
        roles = await async_resolve_roles(self.hass)
        if not roles.has_required:
            return self.async_abort(
                reason="no_power_sensors",
                description_placeholders={"energy_url": "/config/energy"},
            )

        errors: dict[str, str] = {}
        if user_input is not None:
            code = _normalize_code(user_input["pairing_code"])
            if len(code) != 6:
                errors["pairing_code"] = "invalid_code"
            else:
                session = async_get_clientsession(self.hass)
                try:
                    result = await async_claim(session, code)
                except InvalidCode:
                    errors["base"] = "invalid_code"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                else:
                    unique_id = await instance_id.async_get(self.hass)
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="Sunplug",
                        data={
                            CONF_TOKEN: result.token,
                            CONF_INGEST_URL: result.ingest_url,
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth triggered by a 401 from the backend."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new pairing code and update the existing entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            code = _normalize_code(user_input["pairing_code"])
            if len(code) != 6:
                errors["pairing_code"] = "invalid_code"
            else:
                session = async_get_clientsession(self.hass)
                try:
                    result = await async_claim(session, code)
                except InvalidCode:
                    errors["base"] = "invalid_code"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                else:
                    reauth_entry = self._get_reauth_entry()
                    return self.async_update_reload_and_abort(
                        reauth_entry,
                        data={
                            **reauth_entry.data,
                            CONF_TOKEN: result.token,
                            CONF_INGEST_URL: result.ingest_url,
                        },
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
