"""Coordinator-backed bed entity."""

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .client import BridgeError


class BedEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, client, key, name):
        super().__init__(coordinator)
        self.client = client
        self._attr_name = name
        data = coordinator.data
        self._attr_unique_id = data["device_id"] + "_" + key
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, data["device_id"])},
            name=data.get("name", "Sleep Number base"), manufacturer="Sleep Number / community RF bridge",
            model=data["bed"].get("model", "360 FlexFit"), sw_version=data["version"], configuration_url=client.host)

    @property
    def bed(self):
        return self.coordinator.data["bed"]

    @property
    def available(self):
        return self.coordinator.last_update_success and self.bed.get("connected", False)

    async def action(self, action, **parameters):
        try:
            await self.client.command(action, **parameters)
        except BridgeError as exc:
            raise HomeAssistantError(str(exc)) from exc
        finally:
            await self.coordinator.async_request_refresh()
