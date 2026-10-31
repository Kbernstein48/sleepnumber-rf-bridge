"""Configure, import and reconfigure a local bridge."""

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import BridgeClient, BridgeError, normalize_host
from .const import DOMAIN


class SleepNumberRFFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def validate(self, data):
        data = {"host": normalize_host(data["host"])}
        status = await BridgeClient(async_get_clientsession(self.hass), **data).status()
        return data, status

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                data, status = await self.validate(user_input)
                await self.async_set_unique_id(status["device_id"])
                self._abort_if_unique_id_configured(updates=data)
                return self.async_create_entry(title=status.get("name", "Sleep Number base"), data=data)
            except (BridgeError, ValueError):
                errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="user", errors=errors,
            data_schema=vol.Schema({vol.Required("host", default="http://bridge.local:8768"): str}))

    async def async_step_import(self, user_input):
        try:
            data, status = await self.validate(user_input)
        except (BridgeError, ValueError):
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(status["device_id"])
        self._abort_if_unique_id_configured(updates=data)
        return self.async_create_entry(title=status.get("name", "Sleep Number base"), data=data)

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            try:
                data, status = await self.validate(user_input)
                if status["device_id"] != entry.unique_id:
                    errors["base"] = "wrong_device"
                else:
                    return self.async_update_reload_and_abort(entry, data_updates=data)
            except (BridgeError, ValueError):
                errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="reconfigure", errors=errors,
            data_schema=vol.Schema({vol.Required("host", default=entry.data["host"]): str}))
