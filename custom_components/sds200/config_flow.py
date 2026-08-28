from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_HOST, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)


class SDS200ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _async_check(self, host: str, port: int) -> bool:
        """True if the add-on's REST API answers at host:port."""
        session = async_get_clientsession(self.hass)
        url = f"http://{host}:{port}/scanners"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return resp.status == 200
        except Exception:
            _LOGGER.debug("failed to reach sds200_bridge at %s", url, exc_info=True)
            return False

    def _schema(self, host: str, port: int) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_HOST, default=host): str,
                vol.Required(CONF_PORT, default=port): int,
            }
        )

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            if not await self._async_check(host, port):
                errors["base"] = "cannot_connect"

            if not errors:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="SDS200 Bridge", data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=self._schema(DEFAULT_HOST, DEFAULT_PORT), errors=errors
        )

    async def async_step_reconfigure(self, user_input: dict | None = None) -> FlowResult:
        """Change the add-on's host/port on an existing entry.

        Without this there was no way to change them in the UI at all --
        the only route was deleting and re-adding the integration, which
        throws away every entity id and all its history. That turned an
        ordinary event (the add-on's published port moving because
        something else on the host already had 8000) into a config entry
        permanently pointed at the wrong port, with the resulting
        connection errors looking like an add-on or network fault rather
        than a stale setting.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            if not await self._async_check(host, port):
                errors["base"] = "cannot_connect"

            if not errors:
                # The unique id is derived from host:port, so it has to move
                # with them or the entry would keep the old one and a later
                # re-add of the *same* address would be wrongly rejected as
                # a duplicate.
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_mismatch(reason="already_configured")
                return self.async_update_reload_and_abort(entry, data_updates=user_input)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._schema(entry.data[CONF_HOST], entry.data[CONF_PORT]),
            errors=errors,
        )
