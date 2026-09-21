"""WebSocket status hub: fans out per-scanner updates to every client.

Two message types share the socket:

* `{"type": "status", ...}` -- the display/GSI mirror, consumed by the Home
  Assistant integration's coordinator (one WS connection, demuxed by
  "scanner_id") and by the add-on's own web UI.
* `{"type": "reception", ...}` -- a call starting or ending, from
  `history.ReceiveHistory`. Only the add-on's web UI reads these; the HA
  coordinator ignores any type it doesn't know, which is what lets a new
  type be added here without a matching integration release.

`reception_callback` forwards whatever event the history emits rather than
enumerating them, so "transcript" -- which arrives seconds to minutes after
the "end" of the call it belongs to, once a model elsewhere has had its turn
-- needs nothing here. That lateness is the whole reason it is an event: the
row is long since on screen by then, and without a push it would show an
empty transcript until something else forced a refresh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from aiohttp import web

logger = logging.getLogger(__name__)


# How often a browser is told what a scanner is doing. The poll behind it now
# runs four times a second, because that is what a call's edges are cut from
# -- but nobody reads a screen mirror four times a second, and every update is
# a kilobyte to every open tab and a redraw at the other end. So the display
# is fanned out at the rate it was designed for and the extra polls are the
# history's business.
#
# Rate-limited rather than sampled: the newest state always goes out, at most
# this often, so the mirror is never more than this far behind.
STATUS_FANOUT_INTERVAL_S = 1.0


class StatusHub:
    def __init__(self, clock=time.monotonic):
        self._clients: set[web.WebSocketResponse] = set()
        self._clock = clock
        self._last_sent: dict[str, float] = {}

    def status_callback(self, scanner_id: str, status: dict) -> None:
        now = self._clock()
        if now - self._last_sent.get(scanner_id, 0.0) < STATUS_FANOUT_INTERVAL_S:
            return
        self._last_sent[scanner_id] = now
        self._broadcast({"type": "status", "scanner_id": scanner_id, "status": status})

    def reception_callback(self, event: str, record: dict) -> None:
        """Plugged into `ReceiveHistory.add_listener` -- see the module
        docstring. `event` is "start", "end", or "transcript".
        """
        self._broadcast(
            {"type": "reception", "event": event, "scanner_id": record.get("scanner_id"),
             "record": record}
        )

    def _broadcast(self, payload: dict) -> None:
        if not self._clients:
            return
        message = json.dumps(payload)
        for ws in list(self._clients):
            if not ws.closed:
                asyncio.create_task(self._safe_send(ws, message))

    async def _safe_send(self, ws: web.WebSocketResponse, message: str) -> None:
        try:
            await ws.send_str(message)
        except Exception:
            logger.debug("failed to send to a websocket client", exc_info=True)

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        logger.debug("websocket client connected (%d total)", len(self._clients))
        try:
            async for _msg in ws:
                pass  # push-only endpoint; ignore anything a client sends
        finally:
            self._clients.discard(ws)
            logger.debug("websocket client disconnected (%d total)", len(self._clients))
        return ws
