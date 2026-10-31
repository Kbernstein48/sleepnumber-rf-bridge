"""Readback-backed underbed light."""

from homeassistant.components.light import LightEntity, ColorMode
from .const import DOMAIN
from .entity import BedEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([UnderbedLight(data["coordinator"], data["client"])])


class UnderbedLight(BedEntity, LightEntity):
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF
    _attr_icon = "mdi:led-strip-variant"

    def __init__(self, coordinator, client):
        super().__init__(coordinator, client, "underbed_light", "Underbed light")

    @property
    def available(self):
        return super().available and self.bed.get("light", {}).get("available", False) and self.is_on is not None

    @property
    def is_on(self):
        return self.bed.get("light", {}).get("on")

    async def async_turn_on(self, **kwargs):
        await self.action("light", enabled=True)

    async def async_turn_off(self, **kwargs):
        await self.action("light", enabled=False)
