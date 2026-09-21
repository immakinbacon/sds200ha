"""Cross-component test: the HA integration's api_client.py (SDS200Client)
against the add-on's actual api.py (real aiohttp app, in-process via
aiohttp's TestServer -- no real network/Docker needed).

This is the kind of test that would have caught the sensor.py bug found
during review: the add-on's real status shape (STS display lines + a
best-effort nested "gsi" dict) drifted out from under sensor.py's
"status['freq']"/"status['mod']" lookups after protocol.py was reworked to
stop polling GST. Exercising the real client against the real server (even
with a stubbed ScannerConnection/AudioBridge standing in for actual UDP/RTSP
I/O) keeps the two sides honest about what shape actually flows over the
wire.

Requires aiohttp (see README.md's Development section / how this session
obtained it without root: `apt-get download` + extract, no pip/sudo).
Doesn't require `homeassistant` -- api_client.py has no such dependency.
"""

from __future__ import annotations

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestServer

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "addon" / "sds200_bridge" / "app"))
sys.path.insert(0, str(REPO_ROOT / "custom_components" / "sds200"))

import api  # noqa: E402  (add-on's real REST app)
from mikrotik import MikrotikApiError  # noqa: E402
from ws import StatusHub  # noqa: E402

import api_client  # noqa: E402  (HA integration's real client)


class FakeScannerConnection:
    """Stands in for protocol.ScannerConnection -- no real UDP socket."""

    def __init__(self, scanner_id: str, name: str, host: str):
        self.id = scanner_id
        self.name = name
        self.host = host
        self.last_status = {
            "dsp_form": "111",
            "lines": [{"text": "Test Channel", "mode": "   "}],
            "gsi": {
                "mode": "Scan Mode",
                "ConvFrequency": {"Freq": " 462.612500MHz", "Mod": "NFM"},
                "Property": {"VOL": "4", "SQL": "4", "Mute": "Unmute"},
            },
        }
        self.commands: list[str] = []

    async def send_command(self, command: str, **_kwargs) -> str:
        self.commands.append(command)
        word = command.split(",")[0]
        parts = command.split(",")
        if word == "KEY":
            return "KEY,OK"
        if word in ("VOL", "SQL"):
            return f"{word},{parts[1]}" if len(parts) > 1 else f"{word},4"
        if word in ("HLD", "AVD"):
            return f"{word},OK"
        raise AssertionError(f"unexpected command in test: {command!r}")

    async def send_xml_command(self, command: str, **_kwargs) -> ET.Element:
        return ET.fromstring('<GLT><FL Index="0" Name="Test List" Monitor="On" /></GLT>')


class FakeAudioBridge:
    def __init__(self, poe_reset=None):
        self.poe_reset = poe_reset
        self.reboot_calls = 0
        # When set, trigger_reboot() raises it -- mirroring the real
        # AudioBridge, which propagates MikrotikApiError rather than
        # collapsing a failed power-cycle into a falsy return.
        self.reboot_error: Exception | None = None

    def has_reboot_mechanism(self) -> bool:
        return bool(self.poe_reset)

    async def trigger_reboot(self) -> bool:
        self.reboot_calls += 1
        if self.reboot_error is not None:
            raise self.reboot_error
        return True


class TestApiClientAgainstRealServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scanner = FakeScannerConnection("home", "Home", "192.0.2.232")
        self.bridge = FakeAudioBridge()
        app = api.create_app({"home": self.scanner}, {"home": self.bridge}, StatusHub())
        self.server = TestServer(app)
        await self.server.start_server()
        self.session = aiohttp.ClientSession()
        self.client = api_client.SDS200Client(self.session, self.server.host, self.server.port)

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()

    async def test_list_scanners(self):
        scanners = await self.client.list_scanners()
        self.assertEqual(scanners, [{"id": "home", "name": "Home", "host": "192.0.2.232"}])

    async def test_get_status_shape_matches_what_sensor_py_expects(self):
        # Regression: sensor.py used to read status["freq"]/status["mod"]
        # directly; the real shape nests structured data under status["gsi"].
        status = await self.client.get_status("home")
        self.assertIn("lines", status)
        self.assertIn("gsi", status)
        self.assertEqual(status["gsi"]["ConvFrequency"]["Freq"].strip(), "462.612500MHz")
        self.assertNotIn("freq", status)  # would indicate the old (wrong) shape

    async def test_send_key(self):
        # api.py resolves friendly names to raw key codes before sending
        # (key_codes.resolve): confirm "menu" actually went out as "M".
        result = await self.client.send_key("home", "menu")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.scanner.commands, ["KEY,M,P"])

    async def test_set_volume(self):
        result = await self.client.set_volume("home", 7)
        self.assertEqual(result, {"ok": True, "level": 7})

    async def test_hold_and_avoid(self):
        self.assertEqual(await self.client.hold("home", "SYS", "1"), {"ok": True})
        self.assertEqual(await self.client.avoid("home", "SYS", "1", status=2), {"ok": True})

    async def test_reboot_succeeds_when_configured(self):
        self.bridge.poe_reset = object()
        result = await self.client.reboot("home")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.bridge.reboot_calls, 1)

    async def test_reboot_fails_cleanly_when_not_configured(self):
        with self.assertRaises(api_client.SDS200ApiError):
            await self.client.reboot("home")

    async def test_failed_power_cycle_surfaces_the_routers_error_message(self):
        # Regression: post_reboot used to return 200 {"ok": false} when the
        # power-cycle failed, so __init__.py's handle_reboot never raised and
        # HA reported *success* for a reboot that never happened -- throwing
        # away mikrotik.py's diagnostic message in the process. The failure
        # must reach the user, with the message intact.
        self.bridge.poe_reset = object()
        self.bridge.reboot_error = MikrotikApiError(
            "could not reach MikroTik REST API at https://192.0.2.252/rest/...: "
            "set poe_reset_use_ssl: false"
        )
        with self.assertRaises(api_client.SDS200ApiError) as ctx:
            await self.client.reboot("home")
        self.assertIn("poe_reset_use_ssl", str(ctx.exception))
        self.assertEqual(self.bridge.reboot_calls, 1)

    async def test_unknown_scanner_raises_api_error(self):
        with self.assertRaises(api_client.SDS200ApiError):
            await self.client.get_status("does-not-exist")

    async def test_glt_list(self):
        entries = await self.client._get("/scanners/home/lists/FL")
        self.assertEqual(entries, [{"Index": "0", "Name": "Test List", "Monitor": "On", "_tag": "FL"}])


if __name__ == "__main__":
    unittest.main()
