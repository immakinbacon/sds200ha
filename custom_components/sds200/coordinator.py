"""Push-driven coordinator: keeps one WebSocket connection to the add-on open
and demuxes status updates by scanner_id.

Two message types are consumed. `status` is the display/GSI mirror every
sensor is built on. `reception` reports a call starting, ending, or being
transcribed -- only the last of those is kept here, because a transcript is
the one part of a call that arrives long after the call itself and so cannot
be read off the status feed at all.

Any other type is ignored rather than treated as an error, which is what
lets the add-on add one without needing a matching integration release.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api_client import SDS200ApiError, SDS200Client
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY = 5


class SDS200Coordinator(DataUpdateCoordinator[dict]):
    """update_interval is intentionally None: data arrives via the add-on's WS push."""

    def __init__(self, hass: HomeAssistant, client: SDS200Client):
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self.client = client
        self.data: dict[str, dict] = {}
        # The most recent transcribed call per scanner. Separate from `data`
        # rather than folded into the status: a status is what the scanner is
        # doing now, and this is a record of something it finished doing,
        # potentially minutes ago.
        self.transcripts: dict[str, dict] = {}
        self.scanners: list[dict] = []
        self._ws_task: asyncio.Task | None = None

    async def async_setup(self) -> None:
        self.scanners = await self.client.list_scanners()
        for scanner in self.scanners:
            try:
                self.data[scanner["id"]] = await self.client.get_status(scanner["id"])
            except SDS200ApiError:
                self.data[scanner["id"]] = {}
        self._ws_task = self.hass.loop.create_task(self._ws_loop())

    async def async_shutdown(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

    async def _ws_loop(self) -> None:
        while True:
            try:
                async with self.client.session.ws_connect(self.client.ws_url, heartbeat=30) as ws:
                    _LOGGER.debug("connected to sds200_bridge websocket")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_message(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.debug("sds200_bridge websocket connection failed, retrying", exc_info=True)
            await asyncio.sleep(RECONNECT_DELAY)

    def _handle_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            return
        kind = message.get("type")
        if kind == "reception":
            self._handle_reception(message)
            return
        if kind != "status":
            return
        scanner_id = message.get("scanner_id")
        status = message.get("status")
        if scanner_id is None or status is None:
            return
        self.data[scanner_id] = status
        self.async_set_updated_data(dict(self.data))

    def _handle_reception(self, message: dict) -> None:
        """Keep the latest transcript, and only when there is one.

        A call whose transcript was rejected, never attempted, or is still in
        progress carries a status and no text. Storing those would replace a
        real transcript with a blank one every time the next call went by,
        which is worse than not having the sensor.
        """
        if message.get("event") != "transcript":
            return
        record = message.get("record") or {}
        scanner_id = message.get("scanner_id")
        if scanner_id is None or not (record.get("transcript") or "").strip():
            return
        self.transcripts[scanner_id] = record
        self.async_set_updated_data(dict(self.data))
