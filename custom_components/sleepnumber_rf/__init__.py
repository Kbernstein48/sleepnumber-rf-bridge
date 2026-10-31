"""Reusable HTTP integration; the RF process lives outside Home Assistant."""

from datetime import timedelta
import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import Platform
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .client import BridgeClient, BridgeError

PLATFORMS = [Platform.COVER, Platform.LIGHT, Platform.BUTTON, Platform.SENSOR]
CONFIG_SCHEMA = vol.Schema({vol.Optional(DOMAIN): vol.Schema({vol.Required("host"): cv.string})}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass, config):
    if DOMAIN in config:
        hass.async_create_task(hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_IMPORT}, data=dict(config[DOMAIN])))
    return True


async def async_setup_entry(hass, entry):
    client = BridgeClient(async_get_clientsession(hass), entry.data["host"])

    async def update():
        try:
            return await client.status()
        except BridgeError as exc:
            raise UpdateFailed(str(exc)) from exc

    coordinator = DataUpdateCoordinator(hass, logging.getLogger(__name__),
        name="Sleep Number RF", update_method=update, update_interval=timedelta(seconds=2), config_entry=entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coordinator, "client": client}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        return True
    return False
