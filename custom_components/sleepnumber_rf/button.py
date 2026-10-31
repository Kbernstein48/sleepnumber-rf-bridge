"""Positional presets and Stop."""

from homeassistant.components.button import ButtonEntity
from .const import DOMAIN
from .entity import BedEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator, client = data["coordinator"], data["client"]
    names = list(dict.fromkeys([*coordinator.data.get("presets", {}), "favorite", "snore"]))
    async_add_entities([BedButton(coordinator, client, name) for name in names] + [BedButton(coordinator, client, "stop")])


class BedButton(BedEntity, ButtonEntity):
    _attr_icon = "mdi:bed"

    def __init__(self, coordinator, client, preset):
        super().__init__(coordinator, client, preset, preset.replace("_", " ").title())
        self.preset = preset
        if preset == "stop":
            self._attr_icon = "mdi:stop"

    @property
    def available(self):
        return super().available and (self.preset == "stop" or self.preset in self.coordinator.data.get("presets", {}))

    async def async_press(self):
        if self.preset == "stop":
            await self.action("stop")
        else:
            await self.action("preset", preset=self.preset)
