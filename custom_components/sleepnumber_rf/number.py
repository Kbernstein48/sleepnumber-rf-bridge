"""Native percentage slider for the adjustable head."""

import math

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .entity import BedEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HeadPositionNumber(data["coordinator"], data["client"])])


class HeadPositionNumber(BedEntity, NumberEntity):
    _attr_icon = "mdi:bed"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, client):
        super().__init__(coordinator, client, "head_percent", "Head position")

    @property
    def native_value(self):
        return self.bed.get("positions", {}).get("right_head")

    async def async_set_native_value(self, value):
        # HA number services supply floats, whereas the RF API takes integers.
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 100 or value != int(value)):
            raise HomeAssistantError("Choose a whole percentage from 0 to 100")
        await self.action("position", value=int(value))
