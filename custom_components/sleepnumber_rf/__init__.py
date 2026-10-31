"""Reusable HTTP integration; the RF process lives outside Home Assistant."""

from datetime import timedelta
import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import Platform
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .client import BridgeClient, BridgeError

PLATFORMS = [Platform.NUMBER, Platform.COVER, Platform.LIGHT, Platform.BUTTON, Platform.SENSOR]
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


async def async_migrate_entry(hass, entry):
    """Use the slider in automatic dashboards, preserving the cover for automations."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        registry = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if (entity.domain == "cover" and entity.platform == DOMAIN
                    and entity.unique_id == entry.unique_id + "_head"
                    and entity.hidden_by is None):
                registry.async_update_entity(entity.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION)
        hass.config_entries.async_update_entry(entry, minor_version=2, version=1)
    return True


async def _reload(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        return True
    return False
