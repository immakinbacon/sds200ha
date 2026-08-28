"""Tests for the WebSocket audio route (api.stream_audio_ws).

This route exists because the add-on's own web UI cannot use the HTTP one:
behind Home Assistant ingress every same-origin request goes through HA's
frontend service worker, which kills a never-ending response body at ~30s
(see docs/protocol-notes.md). So the thing worth testing is precisely that
it behaves like the HTTP stream does -- the init segment first, then
fragments, and the stream *ending* when the bridge drops the subscriber --
because a difference there doesn't look like a bug, it looks like the audio
problem this route was added to fix.

Runs against a real aiohttp server on a loopback port (like `test_triggers`,
this one needs the real `aiohttp`; nothing here touches the network beyond
localhost, and no scanner is involved -- the AudioBridge is a fake).

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

try:
    import aiohttp  # noqa: F401
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:  # pragma: no cover -- see the module docstring
    aiohttp = None

import api  # noqa: E402


class FakeBridge:
    """Just the subscribe/unsubscribe half of AudioBridge, which is all this
    route touches. Queues are handed out per subscriber and fed by the test,
    exactly as _fan_out would."""

    def __init__(self):
        self.queues: list[asyncio.Queue] = []
        self.unsubscribed: list[asyncio.Queue] = []

    async def subscribe(self):
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        # The real bridge replays the cached MP4 init segment to every new
        # subscriber before any live fragment -- MSE is unplayable without
        # it arriving first (see AudioBridge.subscribe).
        queue.put_nowait(b"INIT")
        self.queues.append(queue)
        return queue

    def unsubscribe(self, queue):
        self.unsubscribed.append(queue)
        if queue in self.queues:
            self.queues.remove(queue)


class FakeStatusHub:
    async def handle(self, request):  # pragma: no cover -- never called here
        raise AssertionError("the status socket is not part of these tests")


def run(coro):
    return asyncio.run(coro)


@unittest.skipIf(aiohttp is None, "aiohttp is not installed")
class AudioWebSocketTest(unittest.TestCase):
    def _app(self, bridges):
        return api.create_app({}, bridges, FakeStatusHub())

    def test_sends_the_init_segment_then_fragments(self):
        bridge = FakeBridge()

        async def scenario():
            async with TestClient(TestServer(self._app({"home": bridge}))) as client:
                async with client.ws_connect("/scanners/home/audio/ws") as ws:
                    first = await asyncio.wait_for(ws.receive(), 5)
                    self.assertEqual(first.data, b"INIT")
                    bridge.queues[0].put_nowait(b"moof-1")
                    bridge.queues[0].put_nowait(b"moof-2")
                    self.assertEqual((await asyncio.wait_for(ws.receive(), 5)).data, b"moof-1")
                    self.assertEqual((await asyncio.wait_for(ws.receive(), 5)).data, b"moof-2")

        run(scenario())

    def test_binary_frames_not_text(self):
        """The client feeds these straight into a SourceBuffer, so they have
        to arrive as binary -- a text frame would be decoded as UTF-8 and
        mangle the fragment."""
        bridge = FakeBridge()

        async def scenario():
            async with TestClient(TestServer(self._app({"home": bridge}))) as client:
                async with client.ws_connect("/scanners/home/audio/ws") as ws:
                    message = await asyncio.wait_for(ws.receive(), 5)
                    self.assertEqual(message.type, aiohttp.WSMsgType.BINARY)

        run(scenario())

    def test_closes_when_the_bridge_drops_the_subscriber(self):
        """STREAM_CLOSED (b"") means the bridge is dropping us -- the socket
        has to close so the page can reconnect into a fresh stream that
        starts with an init segment, rather than sit there silently."""
        bridge = FakeBridge()

        async def scenario():
            async with TestClient(TestServer(self._app({"home": bridge}))) as client:
                async with client.ws_connect("/scanners/home/audio/ws") as ws:
                    await asyncio.wait_for(ws.receive(), 5)  # init segment
                    bridge.queues[0].put_nowait(b"")
                    message = await asyncio.wait_for(ws.receive(), 5)
                    self.assertIn(
                        message.type,
                        (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED),
                    )

        run(scenario())

    def test_unsubscribes_when_the_client_goes_away(self):
        """A subscriber left registered after its socket is gone would keep
        being fanned out to, and eventually fill and log as 'fell behind'."""
        bridge = FakeBridge()

        async def scenario():
            async with TestClient(TestServer(self._app({"home": bridge}))) as client:
                async with client.ws_connect("/scanners/home/audio/ws") as ws:
                    await asyncio.wait_for(ws.receive(), 5)  # init segment
                    await ws.close()
                for _ in range(50):
                    if bridge.unsubscribed:
                        break
                    await asyncio.sleep(0.02)
            self.assertEqual(len(bridge.unsubscribed), 1)
            self.assertEqual(bridge.queues, [])

        run(scenario())

    def test_unknown_scanner_is_rejected(self):
        async def scenario():
            async with TestClient(TestServer(self._app({}))) as client:
                response = await client.get("/scanners/nope/audio/ws")
                self.assertEqual(response.status, 404)

        run(scenario())


if __name__ == "__main__":
    unittest.main()
