"""Sensors driven by the add-on's status feed (protocol.py: STS for display
lines at 1/sec, GSI merged in under status["gsi"] at ~1/3sec best-effort --
see docs/protocol-notes.md for why it's split that way).

Frequency/mode/sub-audio (CTCSS/DCS or a P25 NAC) are all pulled out of the
nested GSI data, and all live under different GSI child elements depending
on scan mode -- conventional uses ConvFrequency, trunked uses
Site/SiteFrequency (see docs/protocol-notes.md's "Depend on mode elements"
notes) -- so both are checked for each. System/department/talkgroup name
and battery are already present in the GSI payload's other keys but not yet
surfaced as their own entities; battery additionally needs the add-on to
poll GCS, which it doesn't yet.
"""

from __future__ import annotations

import re

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SDS200Coordinator
from .entity import SDS200Entity


def _extract_frequency(gsi: dict) -> float | None:
    # The scanner's raw Freq value has the unit baked into the string
    # itself, e.g. " 462.612500MHz" -- HA requires native_value to be
    # numeric when native_unit_of_measurement is set (SDS200FrequencySensor
    # sets it to "MHz"), otherwise the sensor goes unavailable. Confirmed on
    # a real install: this exact mismatch was why frequency showed
    # unavailable while other sensors worked fine.
    for key in ("ConvFrequency", "SiteFrequency"):
        freq = gsi.get(key, {}).get("Freq")
        if freq:
            numeric = freq.strip().removesuffix("MHz").strip()
            try:
                return float(numeric)
            except ValueError:
                return None
    return None


def _extract_mod(gsi: dict) -> str | None:
    for key in ("ConvFrequency", "Site"):
        mod = gsi.get(key, {}).get("Mod")
        if mod:
            return mod
    return None


def _extract_sub_audio(gsi: dict) -> str | None:
    # SAS = configured sub-audio *setting* (e.g. a CTCSS/DCS search target,
    # or "NAC 261h" on a P25 trunked site); SAD = actual *decoded* value on
    # the current reception, when there is one. Prefer SAD (what's actually
    # happening right now) and fall back to SAS (what it's set to search
    # for), skipping "None" either way. Same ConvFrequency/SiteFrequency
    # mode-dependent lookup as frequency/mod above.
    for key in ("ConvFrequency", "SiteFrequency"):
        elem = gsi.get(key, {})
        sad = elem.get("SAD")
        if sad and sad != "None":
            return sad
        sas = elem.get("SAS")
        if sas and sas != "None":
            return sas
    return None


def _extract_rssi(gsi: dict) -> float | None:
    rssi = gsi.get("Property", {}).get("Rssi")
    if rssi is None:
        return None
    try:
        value = float(rssi)
    except ValueError:
        return None
    # -999 is the scanner's own sentinel for "not currently receiving"
    # (confirmed: paired with "RSSI: ---" in the STS display text in the
    # same capture) -- report as unknown rather than a nonsense -999 dBm.
    return None if value <= -999 else value


def _extract_system(gsi: dict) -> str | None:
    return gsi.get("System", {}).get("Name")


def _extract_department(gsi: dict) -> str | None:
    return gsi.get("Department", {}).get("Name")


def _extract_channel(gsi: dict) -> str | None:
    # Conventional: the channel is the ConvFrequency entry's own name.
    # Trunked: there's no per-"channel" concept the same way -- the closest
    # analog is the talkgroup (TGID.Name), which is what the real front
    # panel's "CHANNEL" soft-key label shows in that mode too.
    for key in ("ConvFrequency", "TGID"):
        name = gsi.get(key, {}).get("Name")
        if name:
            return name
    return None


_NOISE_RE = re.compile(r"NOISE:\s*(\d+)")


def _extract_noise(status: dict) -> float | None:
    # No structured GSI field for this -- it only ever showed up as literal
    # text ("NOISE:38500") embedded in one of the STS display lines (mixed
    # in with the WACN line), so it needs regex extraction from the raw
    # display text instead of the gsi dict the other extractors use. Unknown
    # units/scale -- the spec doesn't document this field at all, it's only
    # ever been observed on the scanner's own screen.
    lines = status.get("lines") or []
    text = " ".join(line.get("text", "") for line in lines)
    match = _NOISE_RE.search(text)
    return float(match.group(1)) if match else None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SDS200Coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for scanner in coordinator.scanners:
        entities.append(SDS200DisplaySensor(coordinator, scanner["id"], scanner["name"]))
        entities.append(SDS200FrequencySensor(coordinator, scanner["id"], scanner["name"]))
        entities.append(SDS200ModeSensor(coordinator, scanner["id"], scanner["name"]))
        entities.append(SDS200SubAudioSensor(coordinator, scanner["id"], scanner["name"]))
        entities.append(SDS200RssiSensor(coordinator, scanner["id"], scanner["name"]))
        entities.append(SDS200NoiseSensor(coordinator, scanner["id"], scanner["name"]))
        entities.append(SDS200SystemSensor(coordinator, scanner["id"], scanner["name"]))
        entities.append(SDS200DepartmentSensor(coordinator, scanner["id"], scanner["name"]))
        entities.append(SDS200ChannelSensor(coordinator, scanner["id"], scanner["name"]))
        entities.append(SDS200TranscriptSensor(coordinator, scanner["id"], scanner["name"]))
    async_add_entities(entities)


class SDS200TranscriptSensor(SDS200Entity, SensorEntity):
    """What the scanner was last heard to say.

    Fed by the add-on's `reception` push rather than the status feed, because
    a transcript arrives long after the call it belongs to -- the model runs
    behind a queue, and the call it describes ended seconds or minutes
    earlier. There is no moment at which it is part of "what the scanner is
    doing now".

    Expect the gist rather than a record. The audio is 8kHz telephone
    bandwidth and on a digital system has already been through a vocoder that
    rebuilds speech rather than carrying it. Good enough to automate on with
    a distinctive phrase; not good enough to quote.
    """

    _attr_translation_key = "transcript"
    _attr_icon = "mdi:text-to-speech"

    def __init__(self, coordinator, scanner_id, scanner_name):
        super().__init__(coordinator, scanner_id, scanner_name,
                         platform="sensor", suffix="transcript")

    @property
    def _record(self) -> dict:
        return self.coordinator.transcripts.get(self.scanner_id) or {}

    @property
    def native_value(self) -> str | None:
        # HA states are capped at 255 characters and a long transmission can
        # exceed that, so the state is truncated and the whole thing lives in
        # an attribute. Truncating silently would make an automation matching
        # on the state quietly miss anything said late in a long call.
        text = (self._record.get("transcript") or "").strip()
        return text[:255] or None

    @property
    def extra_state_attributes(self) -> dict:
        record = self._record
        return {
            "full_transcript": record.get("transcript"),
            "truncated": len((record.get("transcript") or "")) > 255,
            "call_id": record.get("id"),
            "started": record.get("started"),
            "duration": record.get("duration"),
            "label": record.get("label"),
            "system": record.get("system"),
            "department": record.get("department"),
            "channel": record.get("channel"),
            "mode": record.get("mode"),
        }


class SDS200DisplaySensor(SDS200Entity, SensorEntity):
    _attr_translation_key = "display"
    _attr_icon = "mdi:radio-handheld"

    def __init__(self, coordinator, scanner_id, scanner_name):
        super().__init__(coordinator, scanner_id, scanner_name, platform="sensor", suffix="display")

    @property
    def native_value(self) -> str | None:
        lines = self._status.get("lines") or []
        for line in lines:
            text = (line.get("text") or "").strip()
            if text:
                return text[:255]
        return None

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "lines": [line.get("text", "") for line in self._status.get("lines", [])],
            "line_modes": [line.get("mode", "") for line in self._status.get("lines", [])],
            "dsp_form": self._status.get("dsp_form"),
        }


class SDS200FrequencySensor(SDS200Entity, SensorEntity):
    _attr_translation_key = "frequency"
    _attr_icon = "mdi:sine-wave"
    _attr_native_unit_of_measurement = "MHz"

    def __init__(self, coordinator, scanner_id, scanner_name):
        super().__init__(coordinator, scanner_id, scanner_name, platform="sensor", suffix="frequency")

    @property
    def native_value(self) -> float | None:
        return _extract_frequency(self._status.get("gsi") or {})


class SDS200ModeSensor(SDS200Entity, SensorEntity):
    _attr_translation_key = "mode"
    _attr_icon = "mdi:radio"

    def __init__(self, coordinator, scanner_id, scanner_name):
        super().__init__(coordinator, scanner_id, scanner_name, platform="sensor", suffix="mode")

    @property
    def native_value(self) -> str | None:
        return _extract_mod(self._status.get("gsi") or {})


class SDS200SubAudioSensor(SDS200Entity, SensorEntity):
    _attr_translation_key = "ctcss_dcs"
    _attr_icon = "mdi:tune-variant"

    def __init__(self, coordinator, scanner_id, scanner_name):
        super().__init__(coordinator, scanner_id, scanner_name, platform="sensor", suffix="ctcss_dcs")

    @property
    def native_value(self) -> str | None:
        return _extract_sub_audio(self._status.get("gsi") or {})


class SDS200RssiSensor(SDS200Entity, SensorEntity):
    _attr_translation_key = "rssi"
    _attr_icon = "mdi:signal"
    _attr_native_unit_of_measurement = "dBm"

    def __init__(self, coordinator, scanner_id, scanner_name):
        super().__init__(coordinator, scanner_id, scanner_name, platform="sensor", suffix="rssi")

    @property
    def native_value(self) -> float | None:
        return _extract_rssi(self._status.get("gsi") or {})


class SDS200NoiseSensor(SDS200Entity, SensorEntity):
    _attr_translation_key = "noise"
    _attr_icon = "mdi:waveform"

    def __init__(self, coordinator, scanner_id, scanner_name):
        super().__init__(coordinator, scanner_id, scanner_name, platform="sensor", suffix="noise")

    @property
    def native_value(self) -> float | None:
        return _extract_noise(self._status)


class SDS200SystemSensor(SDS200Entity, SensorEntity):
    _attr_translation_key = "system"
    _attr_icon = "mdi:radio-tower"

    def __init__(self, coordinator, scanner_id, scanner_name):
        super().__init__(coordinator, scanner_id, scanner_name, platform="sensor", suffix="system")

    @property
    def native_value(self) -> str | None:
        return _extract_system(self._status.get("gsi") or {})


class SDS200DepartmentSensor(SDS200Entity, SensorEntity):
    _attr_translation_key = "department"
    _attr_icon = "mdi:sitemap"

    def __init__(self, coordinator, scanner_id, scanner_name):
        super().__init__(coordinator, scanner_id, scanner_name, platform="sensor", suffix="department")

    @property
    def native_value(self) -> str | None:
        return _extract_department(self._status.get("gsi") or {})


class SDS200ChannelSensor(SDS200Entity, SensorEntity):
    _attr_translation_key = "channel"
    _attr_icon = "mdi:format-list-bulleted"

    def __init__(self, coordinator, scanner_id, scanner_name):
        super().__init__(coordinator, scanner_id, scanner_name, platform="sensor", suffix="channel")

    @property
    def native_value(self) -> str | None:
        return _extract_channel(self._status.get("gsi") or {})
