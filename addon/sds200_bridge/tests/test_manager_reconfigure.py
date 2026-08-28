"""Tests for applying a settings change to already-running scanners.

The claim this module makes -- and the reason it exists at all rather than
the add-on just restarting itself on save -- is that a scanner whose
settings did not change is never stopped and restarted. That matters
because this hardware's RTSP server wedges on repeated session
teardown/reopen and only a physical power-cycle clears it (see
audio_bridge.py and docs/protocol-notes.md), so an unnecessary restart of
an unrelated scanner is a real risk, not a cosmetic one. It is also
invisible from the outside when it regresses: everything still works, the
audio just quietly gets torn down more often than it should.

The RTP port assignment is tested for the same reason. Ports used to be
handed out by list index, so deleting the first of three scanners shifted
the other two onto new ports -- which would force exactly the restart this
module avoids.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests

aiohttp is stubbed (manager -> audio_bridge -> mikrotik imports it), and the
real ScannerConnection/AudioBridge are swapped for fakes -- nothing here
touches a network.
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

# Stub aiohttp only if it genuinely isn't installed. An unconditional
# setdefault() here was enough to break *other* test modules in the same
# `unittest discover` run: the stub is an empty module, so a later module
# that does use aiohttp for real (tests/test_triggers.py) found no
# ClientError on it and failed -- but only when run as part of the suite,
# never on its own.
try:  # noqa: SIM105
    import aiohttp  # noqa: F401
except ImportError:
    sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

import manager as manager_module  # noqa: E402
from config_store import normalize  # noqa: E402
from manager import RTP_BASE_PORT, ScannerManager  # noqa: E402


class FakeConnection:
    instances: list["FakeConnection"] = []

    def __init__(self, scanner_id, name, host, control_port, rtsp_port, gsi_poll_interval=3.0):
        self.id = scanner_id
        self.name = name
        self.host = host
        self.control_port = control_port
        self.rtsp_port = rtsp_port
        self.gsi_poll_interval = gsi_poll_interval
        self.started = False
        self.stopped = False
        FakeConnection.instances.append(self)

    def add_status_listener(self, callback):
        pass

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class FakeBridge:
    instances: list["FakeBridge"] = []

    def __init__(self, scanner_id, host, rtsp_port, rtp_client_port, control, poe_reset,
                 auto_reboot, auto_reboot_on_control_failure):
        self.id = scanner_id
        self.rtp_client_port = rtp_client_port
        self.poe_reset = poe_reset
        self.auto_reboot = auto_reboot
        self.auto_reboot_on_control_failure = auto_reboot_on_control_failure
        self.started = False
        self.stopped = False
        FakeBridge.instances.append(self)

    def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class FakeStatusHub:
    def status_callback(self, scanner_id, status):
        pass


class FakeWeatherWatch:
    def __init__(self):
        self.configured: dict[str, tuple[float, str]] = {}
        self.attached: list[str] = []

    def attach(self, conn):
        self.attached.append(conn.id)

    def detach(self, scanner_id):
        self.configured.pop(scanner_id, None)

    def configure(self, scanner_id, *, return_after_s, fallback_key):
        self.configured[scanner_id] = (return_after_s, fallback_key)

    def status_callback(self, scanner_id, status):
        pass


def config(*scanners, log_level="info"):
    return normalize({"log_level": log_level, "scanners": list(scanners)})


class ManagerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeConnection.instances = []
        FakeBridge.instances = []
        for name, fake in (("ScannerConnection", FakeConnection), ("AudioBridge", FakeBridge)):
            original = getattr(manager_module, name)
            setattr(manager_module, name, fake)
            self.addCleanup(setattr, manager_module, name, original)
        self.manager = ScannerManager(FakeStatusHub())

    def bridge(self, scanner_id: str) -> FakeBridge:
        return self.manager.audio_bridges[scanner_id]


class TestApply(ManagerTestCase):
    async def test_starts_the_configured_scanners(self):
        changes = await self.manager.apply(
            config({"name": "Home", "host": "192.0.2.232"}, {"name": "Shop", "host": "192.0.2.233"})
        )
        self.assertEqual(changes["started"], ["home", "shop"])
        self.assertEqual(sorted(self.manager.scanners), ["home", "shop"])
        self.assertTrue(all(b.started for b in FakeBridge.instances))

    async def test_a_scanner_that_did_not_change_is_left_alone(self):
        home = {"name": "Home", "host": "192.0.2.232"}
        await self.manager.apply(config(home))
        running = self.bridge("home")

        changes = await self.manager.apply(config(home, {"name": "Shop", "host": "192.0.2.233"}))

        self.assertEqual(changes["started"], ["shop"])
        self.assertEqual(changes["unchanged"], ["home"])
        self.assertIs(self.bridge("home"), running)
        self.assertFalse(running.stopped, "an untouched scanner's audio session was torn down")

    async def test_resaving_an_identical_config_changes_nothing(self):
        home = {"name": "Home", "host": "192.0.2.232"}
        await self.manager.apply(config(home))
        changes = await self.manager.apply(config(home))
        self.assertEqual(changes, {"started": [], "restarted": [], "stopped": [], "unchanged": ["home"]})
        self.assertEqual(len(FakeBridge.instances), 1)

    async def test_changing_a_setting_restarts_only_that_scanner(self):
        home = {"name": "Home", "host": "192.0.2.232"}
        shop = {"name": "Shop", "host": "192.0.2.233"}
        await self.manager.apply(config(home, shop))
        old_home, old_shop = self.bridge("home"), self.bridge("shop")

        changes = await self.manager.apply(config({**home, "rtsp_port": 8554}, shop))

        self.assertEqual(changes["restarted"], ["home"])
        self.assertEqual(changes["unchanged"], ["shop"])
        self.assertTrue(old_home.stopped)
        self.assertFalse(old_shop.stopped)
        self.assertIs(self.bridge("shop"), old_shop)

    async def test_a_weather_setting_is_applied_without_restarting_anything(self):
        # Nothing about the connection or the audio session depends on it --
        # only the WeatherWatch does -- so this change must not cost an RTSP
        # teardown/reopen. See manager.WATCH_ONLY_FIELDS.
        watch = FakeWeatherWatch()
        self.manager.weather = watch
        home = {"name": "Home", "host": "192.0.2.232"}
        await self.manager.apply(config(home))
        running = self.bridge("home")

        changes = await self.manager.apply(config({**home, "wx_return_to_scan_s": 90}))

        self.assertEqual(changes["unchanged"], ["home"])
        self.assertEqual(changes["restarted"], [])
        self.assertFalse(running.stopped)
        self.assertEqual(watch.configured["home"], (90, ""))

    async def test_a_transcription_setting_is_applied_without_restarting_anything(self):
        # The most consequential entry in WATCH_ONLY_FIELDS. Transcription
        # reads the audio through AudioBridge's raw-payload listener list,
        # which attaches and detaches without the RTSP session noticing --
        # but this module's diff is exclusion-based, so leaving these fields
        # off that tuple silently makes them connection-affecting. Ticking a
        # checkbox would then tear down and reopen a session on the one piece
        # of this hardware known to wedge when sessions are cycled, and
        # recovering it takes a physical power-cycle.
        home = {"name": "Home", "host": "192.0.2.232"}
        await self.manager.apply(config(home))
        running = self.bridge("home")

        changes = await self.manager.apply(
            config({**home, "transcribe_enabled": True, "transcribe_modes": "analog"})
        )

        self.assertEqual(changes["unchanged"], ["home"])
        self.assertEqual(changes["restarted"], [])
        self.assertFalse(running.stopped)
        self.assertIs(self.bridge("home"), running)

    async def test_a_removed_scanner_is_stopped_and_dropped(self):
        home = {"name": "Home", "host": "192.0.2.232"}
        await self.manager.apply(config(home, {"name": "Shop", "host": "192.0.2.233"}))
        shop_conn = self.manager.scanners["shop"]
        shop_bridge = self.bridge("shop")

        changes = await self.manager.apply(config(home))

        self.assertEqual(changes["stopped"], ["shop"])
        self.assertNotIn("shop", self.manager.scanners)
        self.assertNotIn("shop", self.manager.audio_bridges)
        self.assertTrue(shop_conn.stopped)
        self.assertTrue(shop_bridge.stopped)

    async def test_the_dicts_handed_to_the_api_are_mutated_in_place(self):
        # api.create_app() is given these once at startup and never again,
        # so replacing them here would leave the REST routes serving a
        # frozen set of scanners.
        scanners, bridges = self.manager.scanners, self.manager.audio_bridges
        await self.manager.apply(config({"name": "Home", "host": "192.0.2.232"}))
        self.assertIs(self.manager.scanners, scanners)
        self.assertIs(self.manager.audio_bridges, bridges)
        self.assertIn("home", scanners)


class TestRtpPorts(ManagerTestCase):
    async def test_ports_are_assigned_from_the_reserved_range(self):
        await self.manager.apply(
            config({"name": "Home", "host": "a"}, {"name": "Shop", "host": "b"})
        )
        self.assertEqual(self.bridge("home").rtp_client_port, RTP_BASE_PORT)
        self.assertEqual(self.bridge("shop").rtp_client_port, RTP_BASE_PORT + 1)

    async def test_removing_a_scanner_does_not_move_the_others(self):
        await self.manager.apply(
            config(
                {"name": "Home", "host": "a"},
                {"name": "Shop", "host": "b"},
                {"name": "Barn", "host": "c"},
            )
        )
        shop_port = self.bridge("shop").rtp_client_port
        barn_port = self.bridge("barn").rtp_client_port

        await self.manager.apply(
            config({"name": "Shop", "host": "b"}, {"name": "Barn", "host": "c"})
        )

        self.assertEqual(self.bridge("shop").rtp_client_port, shop_port)
        self.assertEqual(self.bridge("barn").rtp_client_port, barn_port)

    async def test_a_freed_port_is_reused(self):
        await self.manager.apply(
            config({"name": "Home", "host": "a"}, {"name": "Shop", "host": "b"})
        )
        await self.manager.apply(config({"name": "Shop", "host": "b"}))
        await self.manager.apply(
            config({"name": "Shop", "host": "b"}, {"name": "Barn", "host": "c"})
        )
        self.assertEqual(self.bridge("barn").rtp_client_port, RTP_BASE_PORT)

    async def test_a_restarted_scanner_keeps_its_port(self):
        # It's stopped before anything is started, so its own port is free
        # again by the time it asks for one -- no reshuffle.
        await self.manager.apply(
            config({"name": "Home", "host": "a"}, {"name": "Shop", "host": "b"})
        )
        shop_port = self.bridge("shop").rtp_client_port
        await self.manager.apply(
            config({"name": "Home", "host": "a"}, {"name": "Shop", "host": "b2"})
        )
        self.assertEqual(self.bridge("shop").rtp_client_port, shop_port)


class TestUnusableEntries(ManagerTestCase):
    """These are blocked by config_store.validate at save time, so reaching
    the manager means a hand-edited or migrated config. One bad entry must
    not take the working scanners down with it.
    """

    async def test_an_entry_with_no_host_is_skipped(self):
        changes = await self.manager.apply(
            config({"name": "Home", "host": "a"}, {"name": "Broken", "host": ""})
        )
        self.assertEqual(changes["started"], ["home"])
        self.assertNotIn("broken", self.manager.scanners)

    async def test_a_duplicate_id_is_skipped_not_collided(self):
        changes = await self.manager.apply(
            config({"name": "Home", "host": "a"}, {"name": "home!", "host": "b"})
        )
        self.assertEqual(changes["started"], ["home"])
        self.assertEqual(self.manager.scanners["home"].host, "a")

    async def test_scanners_beyond_the_port_range_are_skipped(self):
        scanners = [{"name": f"s{i}", "host": "h"} for i in range(12)]
        changes = await self.manager.apply(config(*scanners))
        self.assertEqual(len(changes["started"]), manager_module.MAX_SCANNERS)


class TestConcurrentSaves(ManagerTestCase):
    async def test_two_saves_are_serialised(self):
        # Interleaved start/stop sequences could otherwise hand the same RTP
        # port to two bridges.
        first = self.manager.apply(config({"name": "Home", "host": "a"}))
        second = self.manager.apply(
            config({"name": "Home", "host": "a"}, {"name": "Shop", "host": "b"})
        )
        await asyncio.gather(first, second)
        ports = [b.rtp_client_port for b in self.manager.audio_bridges.values()]
        self.assertEqual(sorted(ports), [RTP_BASE_PORT, RTP_BASE_PORT + 1])


if __name__ == "__main__":
    unittest.main()
