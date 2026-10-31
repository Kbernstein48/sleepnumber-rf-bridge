"""RF diagnostic sensor."""

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory
from .const import DOMAIN
from .entity import BedEntity


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SignalSensor(data["coordinator"], data["client"])])


class SignalSensor(BedEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "dBm"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, client):
        super().__init__(coordinator, client, "signal", "RF signal")

    @property
    def native_value(self):
        return self.bed.get("radio", {}).get("rssi")
