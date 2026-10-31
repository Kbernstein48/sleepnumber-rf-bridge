"""Reported head position and bounded movement."""

from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from .const import DOMAIN
from .entity import BedEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HeadCover(data["coordinator"], data["client"])])


class HeadCover(BedEntity, CoverEntity):
    # Keep the established entity ID available for automations and manual cards.
    # Automatic dashboards use the native percentage slider instead of arrows.
    _attr_entity_registry_visible_default = False
    _attr_icon = "mdi:bed"
    _attr_supported_features = (CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP | CoverEntityFeature.SET_POSITION)

    def __init__(self, coordinator, client):
        super().__init__(coordinator, client, "head", "Head position")

    @property
    def current_cover_position(self):
        return self.bed.get("positions", {}).get("right_head")

    @property
    def is_closed(self):
        position = self.current_cover_position
        return None if position is None else position == 0

    async def async_set_cover_position(self, **kwargs):
        await self.action("position", value=kwargs["position"])

    async def async_open_cover(self, **kwargs):
        await self.async_set_cover_position(position=100)

    async def async_close_cover(self, **kwargs):
        await self.async_set_cover_position(position=0)

    async def async_stop_cover(self, **kwargs):
        await self.action("stop")
