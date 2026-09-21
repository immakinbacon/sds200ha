from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SDS200Coordinator
from .entity import SDS200Entity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SDS200Coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        cls(coordinator, scanner["id"], scanner["name"])
        for scanner in coordinator.scanners
        for cls in (SDS200MutedSensor, SDS200WeatherAlertSensor)
    ]
    async_add_entities(entities)


class SDS200MutedSensor(SDS200Entity, BinarySensorEntity):
    _attr_translation_key = "muted"
    _attr_icon = "mdi:volume-mute"

    def __init__(self, coordinator, scanner_id, scanner_name):
        super().__init__(coordinator, scanner_id, scanner_name, platform="binary_sensor", suffix="muted")

    @property
    def is_on(self) -> bool | None:
        # Mute lives under the nested GSI payload's Property element (see
        # sensor.py's module docstring) -- there's no top-level "mute" key.
        mute = (self._status.get("gsi") or {}).get("Property", {}).get("Mute")
        if mute is None:
            return None
        return str(mute).lower() == "mute"


class SDS200WeatherAlertSensor(SDS200Entity, BinarySensorEntity):
    _attr_translation_key = "weather_alert"
    _attr_icon = "mdi:weather-alert"

    def __init__(self, coordinator, scanner_id, scanner_name):
        super().__init__(coordinator, scanner_id, scanner_name, platform="binary_sensor", suffix="weather_alert")

    @property
    def is_on(self) -> bool | None:
        # GSI only includes a WxMode element while the scanner is actually in
        # weather monitor/alert mode (see the "Depend on mode elements"
        # matrix in the Remote Command spec) -- confirmed live against a real
        # scanner in normal trunk-scan mode, where WxMode is absent entirely
        # (just DualWatch's separate WX="Priority"/"Off" dual-watch-inclusion
        # setting, not an alert state). So unlike SDS200MutedSensor, a
        # missing WxMode key is a real "no" (not currently monitoring/alerted
        # weather), not an unknown -- only return None if there's no GSI data
        # at all yet. Mode is "Monitor Weather" (listening, no alert) or
        # "Weather Alert" (an actual SAME/weather alert tone was received).
        gsi = self._status.get("gsi")
        if gsi is None:
            return None
        return gsi.get("WxMode", {}).get("Mode") == "Weather Alert"

    @property
    def extra_state_attributes(self) -> dict:
        # SAME is "Alert Only" or the actual SAME group name that triggered
        # the alert -- only meaningful while is_on, but harmless to expose
        # regardless (mirrors WxMode's own presence/absence).
        wx_mode = (self._status.get("gsi") or {}).get("WxMode", {})
        return {"same": wx_mode.get("SAME")}
