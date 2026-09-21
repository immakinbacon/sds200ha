"""Squelch level as a settable number entity.

Optimistic then reconciled against the scanner's own reading, same as
media_player.py's volume -- see levels.py. Exists as its own entity (rather
than folded into media_player) so the card has a real state to read for +/-
stepping, and so squelch is controllable from anywhere in HA (automations,
other dashboards), not just this card.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SDS200Coordinator
from .entity import SDS200Entity
from .levels import ReportedLevel, gsi_level

MAX_SQUELCH = 19  # SDS200's SQL range is 0-19 (SDS100/150 use 0-15)
DEFAULT_SQUELCH = 4  # matches the scanner's own out-of-box default seen in testing


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SDS200Coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        SDS200SquelchNumber(coordinator, scanner["id"], scanner["name"])
        for scanner in coordinator.scanners
    ]
    async_add_entities(entities)


class SDS200SquelchNumber(SDS200Entity, NumberEntity):
    _attr_translation_key = "squelch"
    _attr_icon = "mdi:volume-vibrate"
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_SQUELCH
    _attr_native_step = 1

    def __init__(self, coordinator, scanner_id, scanner_name):
        super().__init__(coordinator, scanner_id, scanner_name, platform="number", suffix="squelch")
        self._level = ReportedLevel(DEFAULT_SQUELCH)

    @property
    def native_value(self) -> float:
        return self._level.resolve(gsi_level(self._status, "SQL"))

    async def async_set_native_value(self, value: float) -> None:
        level = max(0, min(MAX_SQUELCH, int(value)))
        self._level.set(level)
        await self.coordinator.client.set_squelch(self.scanner_id, level)
        self.async_write_ha_state()
