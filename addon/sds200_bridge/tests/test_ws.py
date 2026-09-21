"""Tests for ws.py -- what reaches an open browser tab, and how often.

The status stream behind this runs four times a second, because that is the
rate a call's edges are cut from. A screen mirror is not read at that rate,
and every update is about a kilobyte to every open tab plus a redraw at the
other end -- so the fan-out is deliberately slower than the poll that feeds
it, and the two rates being different is the thing to keep true.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

try:  # noqa: SIM105 -- see test_audio_bridge_control_reboot.py
    import aiohttp  # noqa: F401
except ImportError:
    sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
    sys.modules["aiohttp"].web = types.SimpleNamespace(WebSocketResponse=object)

import ws  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds=1.0):
        self.now += seconds
        return self.now


class TestTheStatusFanOut(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.hub = ws.StatusHub(clock=self.clock)
        self.sent = []
        self.hub._broadcast = self.sent.append

    def test_the_first_reading_goes_straight_out(self):
        self.hub.status_callback("home", {"mute": "1"})
        self.assertEqual(len(self.sent), 1)

    def test_the_polls_in_between_do_not(self):
        for _ in range(4):
            self.hub.status_callback("home", {"mute": "1"})
            self.clock.tick(0.25)
        self.assertEqual(len(self.sent), 1)

    def test_the_next_one_after_the_interval_does(self):
        self.hub.status_callback("home", {"mute": "1"})
        self.clock.tick(ws.STATUS_FANOUT_INTERVAL_S)
        self.hub.status_callback("home", {"mute": "0"})
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[-1]["status"]["mute"], "0")

    def test_each_scanner_has_its_own_pace(self):
        # One busy scanner must not silence another's mirror.
        self.hub.status_callback("home", {"mute": "1"})
        self.hub.status_callback("shed", {"mute": "1"})
        self.assertEqual([m["scanner_id"] for m in self.sent], ["home", "shed"])

    def test_a_call_ending_is_not_rate_limited(self):
        # Receptions are events rather than a mirror: there is one per call,
        # they are what the history page is built from, and dropping one
        # loses a row until something forces a reload.
        for index in range(5):
            self.hub.reception_callback("end", {"id": index, "scanner_id": "home"})
        self.assertEqual(len(self.sent), 5)


if __name__ == "__main__":
    unittest.main()
