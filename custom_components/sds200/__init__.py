from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import aiohttp
import voluptuous as vol
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_client import SDS200ApiError, SDS200Client
from .const import DOMAIN
from .coordinator import SDS200Coordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.MEDIA_PLAYER, Platform.NUMBER]

CARD_URL_PATH = f"/{DOMAIN}_static"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = SDS200Client(session, entry.data[CONF_HOST], entry.data[CONF_PORT])
    coordinator = SDS200Coordinator(hass, client)
    try:
        await coordinator.async_setup()
    except SDS200ApiError as exc:
        # Transient (add-on still starting, network hiccup, etc.) -- let HA
        # retry setup later instead of failing this entry permanently.
        raise ConfigEntryNotReady(f"could not reach the SDS200 Bridge add-on: {exc}") from exc

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Devices are created implicitly: every scanner unconditionally gets
    # entities in all three platforms below, and each entity's
    # _attr_device_info (entity.py) is what HA actually uses to populate the
    # device registry -- no need to pre-create them here too.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await _async_register_frontend_resource(hass)
    _async_register_audio_proxy(hass)
    _async_register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: SDS200Coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_shutdown()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def _async_register_frontend_resource(hass: HomeAssistant) -> None:
    """Serve custom_components/sds200/www/, and auto-add the card as a
    Lovelace resource.

    add_extra_js_url() injects a bare, unawaited import(), which races with
    Lovelace's own card-type resolution and can leave the card showing as
    broken on first load -- sds200-card.js self-heals that case once it
    actually finishes loading. See docs/protocol-notes.md's "Card doesn't
    render" section for the full debugging trail.
    """
    key = f"{DOMAIN}_frontend_registered"
    if hass.data.get(key):
        return
    hass.data[key] = True

    www_path = str(Path(__file__).parent / "www")

    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL_PATH, www_path, False)]
        )
    except ImportError:
        hass.http.register_static_path(CARD_URL_PATH, www_path, cache_headers=False)

    # Version query, not a bare path: confirmed the hard way (2026-07-25) --
    # a card fix that worked in a desktop browser did nothing in the Home
    # Assistant Android app until the app was force-closed and reopened,
    # because its WebView was still running the previous sds200-card.js. The
    # static path is registered with cache_headers=False, which only means
    # "no explicit far-future caching"; with no validator either way, clients
    # are free to heuristically cache it, and the app's WebView does. Bumping
    # manifest.json's version now changes this URL, so every client refetches.
    js_url = f"{CARD_URL_PATH}/sds200-card.js?v={await _integration_version(hass)}"
    try:
        from homeassistant.components import frontend

        frontend.add_extra_js_url(hass, js_url)
        registered = _extra_js_urls_snapshot(hass)
        _LOGGER.debug(
            "add_extra_js_url(%s) called; frontend extra module URLs now: %s",
            js_url, registered,
        )
    except Exception:
        _LOGGER.warning(
            "add_extra_js_url(%s) raised -- add %s as a Lovelace resource "
            "manually (Settings > Dashboards > Resources, type JavaScript "
            "Module) if the card doesn't show up in Add Card search.",
            js_url, js_url, exc_info=True,
        )


async def _integration_version(hass: HomeAssistant) -> str:
    """manifest.json's version, for cache-busting the card URL. Falls back
    to a timestamp rather than a constant: a URL that never changes is the
    exact failure this is here to prevent, so degrade to "refetch every
    restart", not to "never refetch".
    """
    try:
        from homeassistant.loader import async_get_integration

        integration = await async_get_integration(hass, DOMAIN)
        if integration.version:
            return str(integration.version)
    except Exception:  # noqa: BLE001 -- cache-busting must never block setup
        _LOGGER.debug("could not read the integration version for the card URL", exc_info=True)
    return str(int(time.time()))


def _extra_js_urls_snapshot(hass: HomeAssistant):
    """Best-effort read of whatever hass.data key frontend.add_extra_js_url
    actually wrote to, for diagnostic logging -- the exact key name has
    moved before, so try a couple of plausible ones rather than import a
    private constant that may not exist on every HA version. The value is a
    frontend.UrlManager, not a plain collection -- unwrap it (its .urls
    attribute, if present) rather than logging its bare repr, which told us
    nothing about whether our URL actually made it in.
    """
    for key in ("frontend_extra_module_url", "frontend_extra_js_url_es5"):
        manager = hass.data.get(key)
        if not manager:
            continue
        urls = getattr(manager, "urls", None)
        if urls is not None:
            return {key: list(urls)}
        try:
            return {key: list(manager)}
        except TypeError:
            return {key: vars(manager)}
    # Fall back to dumping any hass.data key that looks relevant.
    return {k: v for k, v in hass.data.items() if "extra" in k and "url" in k}


class SDS200AudioProxyView(HomeAssistantView):
    """Proxies a scanner's live MP3 stream through HA core.

    Real-install finding: the media player's stream_url used to point
    straight at the add-on's own stream.mp3 endpoint using the same
    Supervisor-internal host/port the integration uses for its REST/WS
    calls -- reachable from HA core, but the *browser* playing the card's
    audio can't resolve that hostname at all. Even a browser-reachable
    address (the HA host's LAN IP + the add-on's mapped external port)
    wouldn't fully fix it either: HA's frontend is served over HTTPS while
    the add-on is plain HTTP, so the browser's own mixed-content policy
    upgrades the request to HTTPS and it fails against a server that
    doesn't speak it (confirmed via a real browser console: "Mixed Content:
    Upgrading insecure display request ... to use 'https'").

    Routing through this view instead sidesteps both problems: the card's
    <audio> element points at a same-origin relative path
    (/api/sds200/<id>/audio/stream.mp3), which the browser resolves against
    whatever scheme/host it's already using to reach HA -- no separate
    "browser-reachable address" needs figuring out or configuring -- and HA
    core (which already reaches the add-on fine for every other call) does
    the actual upstream fetch and streams the response straight through.
    """

    url = "/api/sds200/{scanner_id}/audio/stream.mp3"
    name = "api:sds200:audio"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        self._hass = hass

    async def get(self, request: web.Request, scanner_id: str) -> web.StreamResponse:
        client = _find_client(self._hass, scanner_id)
        if client is None:
            return web.Response(status=404, text=f"unknown scanner_id: {scanner_id}")

        session = async_get_clientsession(self._hass)
        try:
            upstream = await session.get(
                client.audio_url(scanner_id), timeout=aiohttp.ClientTimeout(total=None)
            )
        except aiohttp.ClientError as exc:
            return web.Response(status=502, text=f"could not reach the add-on: {exc}")

        # Forward the add-on's own Content-Type rather than hardcoding it here
        # too -- it's audio/mp4 (fragmented MP4/AAC), not audio/mpeg, but this
        # way the two don't need to be kept in sync by hand.
        content_type = upstream.headers.get("Content-Type", "audio/mp4")
        response = web.StreamResponse(
            status=upstream.status,
            headers={
                "Content-Type": content_type,
                # For the card's sandboxed relay iframe (see sds200-card.js's
                # _openRelayStream): sandboxing without allow-same-origin is
                # what puts that frame outside the frontend service worker's
                # control -- the entire point, since the service worker kills
                # this stream at 30s -- and the price is that its origin is
                # opaque, making even this same-host request cross-origin.
                # Without this header the browser blocks it on CORS.
                #
                # "*" is not as broad as it looks: the URL carries a signed,
                # short-lived authSig (see media_player.py), so this only
                # widens access for someone who already has a valid signed
                # URL, and credentials are never sent cross-origin anyway.
                "Access-Control-Allow-Origin": "*",
            },
        )
        await response.prepare(request)

        # This hop used to be entirely silent, which left a real failure
        # (playback consistently stopping after ~30s) impossible to place:
        # the add-on's log showed its subscriber going away at 30.1s, but
        # not whether the add-on stopped sending or the browser hung up --
        # those are opposite bugs and the middle of the chain is the only
        # place that can tell them apart. Hence: which side ended it, how
        # long it lasted, and how much actually got through.
        started = time.monotonic()
        forwarded = 0
        ended_by = "the add-on (upstream stream ended)"
        try:
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
                forwarded += len(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            # Browser navigated away/stopped playback mid-stream -- not an
            # error, but very much worth naming when it happens 30s in.
            ended_by = "the client (disconnected)"
        except Exception as exc:  # noqa: BLE001 -- report, don't swallow
            ended_by = f"an error in the proxy: {exc!r}"
            raise
        finally:
            upstream.close()
            _LOGGER.info(
                "audio proxy for %s ended after %.1fs and %d bytes forwarded; ended by %s",
                scanner_id, time.monotonic() - started, forwarded, ended_by,
            )
        return response


def _find_client(hass: HomeAssistant, scanner_id: str) -> SDS200Client | None:
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if not isinstance(coordinator, SDS200Coordinator):
            continue
        if any(scanner["id"] == scanner_id for scanner in coordinator.scanners):
            return coordinator.client
    return None


def _async_register_audio_proxy(hass: HomeAssistant) -> None:
    key = f"{DOMAIN}_audio_proxy_registered"
    if hass.data.get(key):
        return
    hass.data[key] = True
    hass.http.register_view(SDS200AudioProxyView(hass))


_DEVICE_ID_SCHEMA = {vol.Required("device_id"): cv.string}

_KEY_SCHEMA = vol.Schema(
    {**_DEVICE_ID_SCHEMA, vol.Required("code"): cv.string, vol.Optional("mode", default="P"): cv.string}
)
_HOLD_AVOID_SCHEMA = vol.Schema(
    {
        **_DEVICE_ID_SCHEMA,
        vol.Optional("tkw", default=""): cv.string,
        vol.Optional("xxx1", default=""): cv.string,
        vol.Optional("xxx2", default=""): cv.string,
    }
)
# No default for "hold": absent means toggle, which is what the scanner's
# own command does and what a Hold button wants. True/False set a state.
_HOLD_SCHEMA = _HOLD_AVOID_SCHEMA.extend({vol.Optional("hold"): cv.boolean})
_AVOID_SCHEMA = _HOLD_AVOID_SCHEMA.extend({vol.Optional("status", default=1): vol.In([1, 2, 3])})
_LEVEL_SCHEMA = vol.Schema({**_DEVICE_ID_SCHEMA, vol.Required("level"): vol.Coerce(int)})
_REBOOT_SCHEMA = vol.Schema(_DEVICE_ID_SCHEMA)


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "key"):
        return  # already registered by a previously-set-up config entry

    def _resolve(device_id: str) -> tuple[SDS200Coordinator, str]:
        device_registry = dr.async_get(hass)
        device = device_registry.async_get(device_id)
        if device is None:
            raise ServiceValidationError(f"unknown device_id {device_id}")
        scanner_id = next((ident[1] for ident in device.identifiers if ident[0] == DOMAIN), None)
        if scanner_id is None:
            raise ServiceValidationError(f"device {device_id} is not an SDS200 scanner")
        for coordinator in hass.data.get(DOMAIN, {}).values():
            if scanner_id in coordinator.data:
                return coordinator, scanner_id
        raise ServiceValidationError(f"scanner {scanner_id} is not currently connected")

    def _ack(result: dict, what: str) -> None:
        """Raise if the scanner answered the command with NG rather than OK.

        The add-on reports a refusal as {"ok": false} on an otherwise
        successful HTTP call (its own web UI reads that flag and says so),
        and these handlers used to drop it on the floor -- so a KEY the
        scanner rejected, or an AVD it wouldn't act on, looked exactly like
        a success to everything on the Home Assistant side. A service that
        silently does nothing is worse than one that fails.
        """
        if isinstance(result, dict) and result.get("ok") is False:
            raise ServiceValidationError(f"the scanner refused {what}")

    async def handle_key(call: ServiceCall) -> None:
        coordinator, scanner_id = _resolve(call.data["device_id"])
        code = call.data["code"]
        _ack(
            await coordinator.client.send_key(scanner_id, code, call.data.get("mode", "P")),
            f"key {code}",
        )

    async def handle_hold(call: ServiceCall) -> None:
        coordinator, scanner_id = _resolve(call.data["device_id"])
        _ack(
            await coordinator.client.hold(
                scanner_id,
                call.data.get("tkw", ""),
                call.data.get("xxx1", ""),
                call.data.get("xxx2", ""),
                hold=call.data.get("hold"),
            ),
            "the hold command",
        )

    async def handle_avoid(call: ServiceCall) -> None:
        coordinator, scanner_id = _resolve(call.data["device_id"])
        _ack(
            await coordinator.client.avoid(
                scanner_id,
                call.data.get("tkw", ""),
                call.data.get("xxx1", ""),
                call.data.get("xxx2", ""),
                call.data.get("status", 1),
            ),
            "the avoid command",
        )

    async def handle_set_volume(call: ServiceCall) -> None:
        coordinator, scanner_id = _resolve(call.data["device_id"])
        await coordinator.client.set_volume(scanner_id, call.data["level"])

    async def handle_set_squelch(call: ServiceCall) -> None:
        coordinator, scanner_id = _resolve(call.data["device_id"])
        await coordinator.client.set_squelch(scanner_id, call.data["level"])

    async def handle_reboot(call: ServiceCall) -> None:
        coordinator, scanner_id = _resolve(call.data["device_id"])
        try:
            await coordinator.client.reboot(scanner_id)
        except SDS200ApiError as exc:
            raise ServiceValidationError(
                f"reboot failed for {scanner_id}: {exc} (is poe_reset_* configured "
                "for this scanner in the add-on?)"
            ) from exc

    hass.services.async_register(DOMAIN, "key", handle_key, schema=_KEY_SCHEMA)
    hass.services.async_register(DOMAIN, "hold", handle_hold, schema=_HOLD_SCHEMA)
    hass.services.async_register(DOMAIN, "avoid", handle_avoid, schema=_AVOID_SCHEMA)
    hass.services.async_register(DOMAIN, "set_volume", handle_set_volume, schema=_LEVEL_SCHEMA)
    hass.services.async_register(DOMAIN, "set_squelch", handle_set_squelch, schema=_LEVEL_SCHEMA)
    hass.services.async_register(DOMAIN, "reboot", handle_reboot, schema=_REBOOT_SCHEMA)
