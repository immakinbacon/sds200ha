"""Scanner audio entity.

Volume follows the scanner rather than only HA: a level set here is applied
optimistically, then reconciled against the VOL the scanner reports in the
status feed, so turning the knob on the unit itself (or setting it from the
add-on's web UI) is picked up within a poll or two. See levels.py for why
the reading can't simply be taken as gospel the moment it lands.

`stream_url`/`media_content_id` point at this integration's own audio proxy
view (see __init__.py's SDS200AudioProxyView), not the add-on's endpoint
directly -- a real-install test found the add-on's own stream.mp3 URL
unreachable from the browser (Supervisor-internal hostname) and hit a
mixed-content HTTPS block besides.

The proxy path also needs to be *signed* (`async_sign_path`, `authSig=...`
query param), not just relative: confirmed live that the plain path 401s.
The card's <audio src=...> load is a normal browser resource fetch, not a
`fetch()`/XHR call this integration controls -- there's no way to attach
the Authorization Bearer header HA's frontend normally uses, and there's no
"current request" to derive a session from anyway since this is just an
entity property, read whenever HA serializes state, not during an active
HTTP request. `use_content_user=True` is the same mechanism HA's own
TTS/media-source URLs use for exactly this "browser-loadable media URL,
independent of the current user session" case.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SDS200Coordinator
from .entity import SDS200Entity
from .levels import ReportedLevel, gsi_level

MAX_VOLUME = 29  # SDS200's VOL range is 0-29 (SDS100/150 use 0-15)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SDS200Coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        SDS200MediaPlayer(coordinator, scanner["id"], scanner["name"])
        for scanner in coordinator.scanners
    ]
    async_add_entities(entities)


class SDS200MediaPlayer(SDS200Entity, MediaPlayerEntity):
    _attr_translation_key = "scanner_audio"
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.STOP
    )
    _attr_media_content_type = MediaType.MUSIC

    def __init__(self, coordinator: SDS200Coordinator, scanner_id: str, scanner_name: str):
        super().__init__(coordinator, scanner_id, scanner_name, platform="media_player", suffix="media_player")
        self._playing = True
        self._volume = ReportedLevel(round(MAX_VOLUME / 2))
        self._muted = False
        # What to go back to on unmute. Held separately because muting is
        # done by setting the scanner to 0 (the protocol has no mute
        # command), so once the scanner starts reporting that 0 back there's
        # nothing left in _volume to restore from.
        self._muted_restore = self._volume.value

    @property
    def state(self) -> MediaPlayerState:
        if not self.available:
            return MediaPlayerState.OFF
        return MediaPlayerState.PLAYING if self._playing else MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float:
        return self._raw_volume / MAX_VOLUME

    @property
    def _raw_volume(self) -> int:
        return self._volume.resolve(gsi_level(self._status, "VOL"))

    @property
    def is_volume_muted(self) -> bool:
        return self._muted

    @property
    def media_content_id(self) -> str:
        path = f"/api/sds200/{self.scanner_id}/audio/stream.mp3"
        return async_sign_path(self.hass, path, timedelta(minutes=10), use_content_user=True)

    @property
    def extra_state_attributes(self) -> dict:
        return {"stream_url": self.media_content_id}

    async def async_set_volume_level(self, volume: float) -> None:
        level = max(0, min(MAX_VOLUME, round(volume * MAX_VOLUME)))
        self._volume.set(level)
        if level:
            # Setting a level at all is an unmute: leaving _muted set would
            # otherwise leave the entity claiming to be muted while audibly
            # playing, and strand _muted_restore at a stale level.
            self._muted = False
        await self.coordinator.client.set_volume(self.scanner_id, level)
        self.async_write_ha_state()

    async def async_mute_volume(self, mute: bool) -> None:
        # No dedicated mute command in the protocol; approximate with volume 0 / restore.
        if mute:
            self._muted_restore = self._raw_volume or self._muted_restore
        level = 0 if mute else self._muted_restore
        self._muted = mute
        self._volume.set(level)
        await self.coordinator.client.set_volume(self.scanner_id, level)
        self.async_write_ha_state()

    async def async_media_play(self) -> None:
        self._playing = True
        self.async_write_ha_state()

    async def async_media_stop(self) -> None:
        self._playing = False
        self.async_write_ha_state()
