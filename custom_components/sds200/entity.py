from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import SDS200Coordinator


class SDS200Entity(CoordinatorEntity[SDS200Coordinator]):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SDS200Coordinator,
        scanner_id: str,
        scanner_name: str,
        *,
        platform: str,
        suffix: str,
    ):
        """platform/suffix determine both unique_id and entity_id, e.g.
        platform="sensor", suffix="display" -> entity_id
        "sensor.sds200_<scanner_id>_display". Explicit rather than left to
        HA's default (name-based) entity_id generation: with
        _attr_has_entity_name=True, that default is derived from the
        device+entity *display* name, not unique_id, which would silently
        stop matching the fixed convention the Lovelace card assumes.
        """
        super().__init__(coordinator)
        self.scanner_id = scanner_id
        self._attr_unique_id = f"{DOMAIN}_{scanner_id}_{suffix}"
        self.entity_id = f"{platform}.{DOMAIN}_{scanner_id}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, scanner_id)},
            name=scanner_name,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def _status(self) -> dict:
        return self.coordinator.data.get(self.scanner_id, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._status)
