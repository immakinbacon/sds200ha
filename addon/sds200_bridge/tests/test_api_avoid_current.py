"""Tests for the permanent-avoid route (api.post_avoid_current).

Worth testing at the route rather than at reception.channel_target alone,
because the two things that make this endpoint different from every other
command route are both in the handler: it re-polls GSI before it fires (a
permanent avoid aimed at the channel the scanner has *just* moved off is not
undoable from here), and the command it builds is the first AVD this project
sends with a real target -- the keyword comes from a spec table read
second-hand, so the exact string on the wire is the thing a future reader
will want pinned down.

Runs against a real aiohttp server on a loopback port, like
test_api_audio_ws; the scanner is a fake, and nothing here talks to
hardware.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

try:
    import aiohttp  # noqa: F401
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:  # pragma: no cover -- see the module docstring
    aiohttp = None

import api  # noqa: E402
import avoids  # noqa: E402

CONVENTIONAL_GSI = {
    "mode": "Scan Mode",
    "System": {"Name": "Family Radio Service (FRS) - USA", "Index": "20214", "Avoid": "Off"},
    "Department": {"Name": "Family Radio Service (FRS)", "Index": "20217", "Avoid": "Off"},
    "ConvFrequency": {"Name": "Channel 3", "Index": "20251", "Avoid": "Off"},
    # Receiving: the route refuses mid-scan, because that is when the key
    # press lands somewhere other than where it was aimed.
    "Property": {"Rssi": "-73"},
}


def glt_xml(states: dict) -> ET.Element:
    """A GLT,CFREQ response shaped like the real one: one CFREQ element per
    channel, each carrying the Avoid state the scanner would report."""
    root = ET.Element("GLT")
    for index, avoid in states.items():
        ET.SubElement(root, "CFREQ", {"Index": index, "Name": f"ch {index}", "Avoid": avoid})
    return root


class FakeScanner:
    """The three things the routes touch: a GSI re-poll, a command, and the
    GLT read-back that is now the only accepted proof an avoid took.

    `avoid_state` is what GLT will report for the target channel after the
    presses -- the scanner's answer, which is the whole question. Setting
    `gsi` to an exception exercises the flaky-GSI path, as
    ScannerConnection.refresh_gsi does when the scanner doesn't answer.
    """

    def __init__(self, gsi=None, reply="AVD,OK", avoid_state="Avoid"):
        self.id = "home"
        self.gsi = CONVENTIONAL_GSI if gsi is None else gsi
        self.reply = reply
        self.avoid_state = avoid_state
        self.last_status: dict = {}
        self.commands: list[str] = []
        self.xml_commands: list[str] = []
        self.refreshes = 0

    async def refresh_gsi(self):
        self.refreshes += 1
        if isinstance(self.gsi, Exception):
            raise self.gsi
        self.last_status = {**self.last_status, "gsi": self.gsi}
        return self.gsi

    async def send_command(self, command: str) -> str:
        self.commands.append(command)
        if command.startswith("KEY,"):
            return "KEY,OK"
        return self.reply

    async def send_xml_command(self, command: str):
        self.xml_commands.append(command)
        if isinstance(self.avoid_state, Exception):
            raise self.avoid_state
        return glt_xml({"20251": self.avoid_state, "31007": self.avoid_state})


class FakeStatusHub:
    async def handle(self, request):  # pragma: no cover -- no socket in these tests
        raise AssertionError("the status socket is not part of these tests")


def run(coro):
    return asyncio.run(coro)


@unittest.skipIf(aiohttp is None, "aiohttp is not installed")
class AvoidCurrentTest(unittest.TestCase):
    """Permanently avoiding the current channel.

    The route presses the unit's AVOID key twice and then reads GLT to find
    out what the scanner actually did. Both halves matter: the press is the
    only thing that survives a power cycle (AVD writes RAM only), and the
    read-back is the only honest success signal (KEY,OK means a key was
    accepted, nothing more).
    """

    def setUp(self):
        self.avoids = avoids.AvoidLog(path=None)
        self._timing = (api.AVOID_PRESS_GAP_S, api.AVOID_SETTLE_S)
        # The gap is load-bearing on real hardware and irrelevant here.
        api.AVOID_PRESS_GAP_S = 0.0
        api.AVOID_SETTLE_S = 0.0

    def tearDown(self):
        (api.AVOID_PRESS_GAP_S, api.AVOID_SETTLE_S) = self._timing

    def _post(self, scanner, body=None, path="/scanners/home/avoid_current"):
        async def scenario():
            app = api.create_app({"home": scanner}, {}, FakeStatusHub(), avoids=self.avoids)
            async with TestClient(TestServer(app)) as client:
                response = await client.post(path, json=body if body is not None else {})
                try:
                    return response.status, await response.json()
                except Exception:
                    return response.status, {"detail": await response.text()}

        return run(scenario())

    def test_presses_avoid_twice(self):
        scanner = FakeScanner()
        status, body = self._post(scanner)

        self.assertEqual(status, 200)
        self.assertEqual(scanner.commands, ["KEY,L,P", "KEY,L,P"])
        self.assertTrue(body["ok"])
        self.assertEqual(body["state"], "Avoid")
        self.assertEqual(body["target"]["name"], "Channel 3")

    def test_checks_the_list_rather_than_trusting_the_key_reply(self):
        """KEY,OK comes back either way. The scanner's own list is the only
        thing that says whether the channel is actually avoided."""
        scanner = FakeScanner()
        self._post(scanner)
        self.assertEqual(scanner.xml_commands, ["GLT,CFREQ,20217"])

    def test_a_temporary_result_is_a_failure(self):
        """Two presses too far apart leave the channel at T-Avoid, which
        does not survive a power cycle and must not be reported as done."""
        scanner = FakeScanner(avoid_state="T-Avoid")
        _status, body = self._post(scanner)

        self.assertFalse(body["ok"])
        self.assertEqual(body["state"], "T-Avoid")
        self.assertEqual(self.avoids.for_scanner("home"), [])

    def test_a_missed_press_is_a_failure(self):
        # The scanner moved on between the GSI read and the press, so the
        # intended channel is untouched -- and something else may not be.
        scanner = FakeScanner(avoid_state="Off")
        _status, body = self._post(scanner)

        self.assertFalse(body["ok"])
        self.assertEqual(body["state"], "Off")
        self.assertEqual(self.avoids.for_scanner("home"), [])

    def test_records_a_verified_avoid(self):
        scanner = FakeScanner()
        _status, body = self._post(scanner)

        [record] = self.avoids.for_scanner("home")
        self.assertEqual(record["index"], "20251")
        self.assertEqual(record["department_index"], "20217")
        self.assertEqual(record["system"], "Family Radio Service (FRS) - USA")
        self.assertEqual(body["record"]["id"], record["id"])

    def test_a_successful_avoid_saves_pending_un_avoids(self):
        """The scanner flushes its whole working copy when a keypress sets a
        permanent avoid, so one success commits every un-avoid waiting on a
        save -- and those records stop being true at that moment."""
        old = self.avoids.record(
            "home", {"tkw": "CFREQ", "index": "999", "name": "Old"}, "x", "y"
        )
        self.avoids.mark_cleared(old)

        _status, body = self._post(FakeScanner())

        self.assertEqual(body["committed"], 1)
        self.assertEqual([r["index"] for r in self.avoids.for_scanner("home")], ["20251"])

    def test_nothing_is_pressed_while_the_scanner_is_mid_scan(self):
        """Confirmed against real hardware: called while free-scanning, the
        presses land on whatever the scanner had moved to, and the intended
        channel is untouched. The only safe moment is while it is stopped."""
        scanner = FakeScanner(gsi={**CONVENTIONAL_GSI, "Property": {"Rssi": "-999"}})
        status, body = self._post(scanner)

        self.assertEqual(status, 409)
        self.assertIn("isn't stopped on a channel", body["detail"])
        self.assertEqual(scanner.commands, [])
        self.assertEqual(self.avoids.for_scanner("home"), [], "a blocked attempt is not an avoid")

    def test_nothing_is_pressed_when_the_result_could_not_be_checked(self):
        # No department index means no list to read back, and an
        # unverifiable permanent avoid is what this route exists to avoid.
        gsi = {"ConvFrequency": {"Name": "Channel 3", "Index": "20251"},
               "Property": {"Rssi": "-73"}}
        scanner = FakeScanner(gsi=gsi)
        status, body = self._post(scanner)

        self.assertEqual(status, 409)
        self.assertIn("department index", body["detail"])
        self.assertEqual(scanner.commands, [])
        self.assertEqual(self.avoids.for_scanner("home"), [])

    def test_a_failed_read_back_is_not_reported_as_success(self):
        scanner = FakeScanner(avoid_state=TimeoutError("no response to 'GLT'"))
        status, body = self._post(scanner)

        self.assertEqual(status, 502)
        self.assertFalse(body["ok"])
        self.assertEqual(self.avoids.for_scanner("home"), [])

    def test_targets_the_talkgroup_on_a_trunked_system(self):
        scanner = FakeScanner(
            gsi={
                "TGID": {"Name": "Fire Dispatch", "Index": "31007"},
                "Department": {"Name": "Fire", "Index": "20217"},
                "Property": {"Rssi": "-73"},
            }
        )
        _status, body = self._post(scanner)

        self.assertEqual(body["target"]["element"], "TGID")
        self.assertTrue(body["ok"])

    def test_refuses_when_the_screen_has_nothing_indexed_on_it(self):
        scanner = FakeScanner(gsi={"mode": "Menu Mode"})
        status, body = self._post(scanner)

        self.assertEqual(status, 409)
        self.assertIn("isn't on a channel", body["detail"])
        self.assertEqual(scanner.commands, [])
        self.assertEqual(self.avoids.for_scanner("home"), [])

    def test_refuses_rather_than_guessing_when_the_gsi_poll_fails(self):
        scanner = FakeScanner(gsi=TimeoutError("no response to 'GSI'"))
        status, body = self._post(scanner)

        self.assertEqual(status, 409)
        self.assertIn("couldn't read", body["detail"])
        self.assertEqual(scanner.commands, [])
        self.assertEqual(self.avoids.for_scanner("home"), [])

    def test_rereads_gsi_before_pressing(self):
        scanner = FakeScanner()
        scanner.last_status = {"gsi": {"ConvFrequency": {"Name": "Stale", "Index": "111"}}}
        self._post(scanner)

        self.assertEqual(scanner.refreshes, 1)
        self.assertEqual(scanner.xml_commands, ["GLT,CFREQ,20217"])

    def test_unknown_scanner_id_is_a_404(self):
        status, _body = self._post(FakeScanner(), path="/scanners/nosuch/avoid_current")
        self.assertEqual(status, 404)


@unittest.skipIf(aiohttp is None, "aiohttp is not installed")
class HoldTest(unittest.TestCase):
    """Holding on the current channel.

    `HLD,,,` -- what this route sent from the day it was written -- answers
    `HLD,ERR` on this firmware, so the feature never worked. HLD names its
    target by index like AVD does, and the command toggles rather than sets,
    which is the part worth pinning down: asking to hold something already
    held must not release it.
    """

    def _post(self, scanner, body=None):
        async def scenario():
            app = api.create_app({"home": scanner}, {}, FakeStatusHub())
            async with TestClient(TestServer(app)) as client:
                response = await client.post("/scanners/home/hold", json=body or {})
                try:
                    return response.status, await response.json()
                except Exception:
                    return response.status, {"detail": await response.text()}

        return run(scenario())

    def test_holds_on_the_current_channel_by_index(self):
        scanner = FakeScanner()
        _status, body = self._post(scanner)

        self.assertEqual(scanner.commands, ["HLD,CFREQ,20251,"])
        self.assertNotIn("HLD,,,", scanner.commands)
        self.assertEqual(body["target"]["index"], "20251")

    def test_a_hold_already_in_place_is_left_alone(self):
        """The command toggles, so re-sending it on a held scanner would
        release -- the opposite of what an explicit hold:true asked for."""
        scanner = FakeScanner(gsi={**CONVENTIONAL_GSI, "mode": "Scan Hold"})
        _status, body = self._post(scanner, {"hold": True})

        self.assertEqual(scanner.commands, [])
        self.assertTrue(body["ok"])
        self.assertTrue(body["held"])
        self.assertTrue(body["unchanged"])

    def test_releasing_sends_the_same_toggle(self):
        scanner = FakeScanner(gsi={**CONVENTIONAL_GSI, "mode": "Scan Hold"})
        _status, _body = self._post(scanner, {"hold": False})

        self.assertEqual(scanner.commands, ["HLD,CFREQ,20251,"])

    def test_no_hold_field_means_toggle(self):
        """What a button labelled Hold should do, and what the command
        itself does: a held scanner asked again releases."""
        scanner = FakeScanner(gsi={**CONVENTIONAL_GSI, "mode": "Scan Hold"})
        self._post(scanner)
        self.assertEqual(scanner.commands, ["HLD,CFREQ,20251,"])

    def test_releasing_an_unheld_scanner_does_nothing(self):
        scanner = FakeScanner()
        _status, body = self._post(scanner, {"hold": False})

        self.assertEqual(scanner.commands, [])
        self.assertTrue(body["unchanged"])

    def test_an_explicit_target_is_passed_through(self):
        scanner = FakeScanner(reply="HLD,OK")
        _status, _body = self._post(scanner, {"tkw": "SYS", "xxx1": "42"})

        self.assertEqual(scanner.commands, ["HLD,SYS,42,"])

    def test_the_state_is_re_read_rather_than_taken_from_the_reply(self):
        # HLD,OK came back from a form that plainly didn't hold, so the
        # answer reports what the scanner says afterwards, not what it
        # acknowledged.
        scanner = FakeScanner(reply="HLD,OK")
        _status, body = self._post(scanner, {"hold": True})

        self.assertFalse(body["ok"], "the fake never enters Scan Hold, so nothing changed")
        self.assertFalse(body["held"])
        self.assertEqual(scanner.refreshes, 2)


@unittest.skipIf(aiohttp is None, "aiohttp is not installed")
class RawCommandTest(unittest.TestCase):
    """The diagnostic route. Its whole value is being a straight pipe, so
    what's worth pinning down is that it doesn't try to be clever: the
    command goes out verbatim and the reply comes back verbatim, including
    an NG."""

    def _post(self, scanner, body):
        async def scenario():
            app = api.create_app({"home": scanner}, {}, FakeStatusHub())
            async with TestClient(TestServer(app)) as client:
                response = await client.post("/scanners/home/command", json=body)
                try:
                    return response.status, await response.json()
                except Exception:
                    return response.status, {"detail": await response.text()}

        return run(scenario())

    def test_sends_the_command_verbatim(self):
        scanner = FakeScanner()
        status, body = self._post(scanner, {"command": "AVD,CFREQ,20217,20251,1"})

        self.assertEqual(status, 200)
        self.assertEqual(scanner.commands, ["AVD,CFREQ,20217,20251,1"])
        self.assertEqual(body["response"], "AVD,OK")

    def test_hands_back_a_refusal_rather_than_interpreting_it(self):
        scanner = FakeScanner(reply="AVD,NG")
        _status, body = self._post(scanner, {"command": "AVD,NOPE,1,,1"})

        # No "ok" key at all: this route reports, it doesn't judge.
        self.assertEqual(body["response"], "AVD,NG")
        self.assertNotIn("ok", body)

    def test_an_empty_command_is_rejected(self):
        scanner = FakeScanner()
        status, _body = self._post(scanner, {"command": "   "})

        self.assertEqual(status, 400)
        self.assertEqual(scanner.commands, [])


@unittest.skipIf(aiohttp is None, "aiohttp is not installed")
class UndoAvoidTest(unittest.TestCase):
    """Listing and reversing what was avoided.

    The reversal is the same command with status 3, replayed from the
    record -- there is no other route back, since the channel it names is
    the one the scanner will never show again.
    """

    def setUp(self):
        self.avoids = avoids.AvoidLog(path=None)
        self.record = self.avoids.record(
            "home",
            {"tkw": "CFREQ", "index": "20251", "element": "ConvFrequency", "name": "Channel 3"},
            "AVD,CFREQ,20251,,1",
            "AVD,OK",
            context={"system": "FRS - USA", "department": "FRS"},
        )

    def _request(self, scanner, method, path):
        async def scenario():
            app = api.create_app({"home": scanner}, {}, FakeStatusHub(), avoids=self.avoids)
            async with TestClient(TestServer(app)) as client:
                response = await client.request(method, path)
                try:
                    return response.status, await response.json()
                except Exception:
                    return response.status, {"detail": await response.text()}

        return run(scenario())

    def test_lists_what_was_avoided(self):
        status, body = self._request(FakeScanner(), "GET", "/scanners/home/avoids")

        self.assertEqual(status, 200)
        self.assertEqual([a["name"] for a in body["avoids"]], ["Channel 3"])

    def test_undo_sends_status_three_for_the_recorded_target(self):
        scanner = FakeScanner()
        status, body = self._request(
            scanner, "DELETE", f"/scanners/home/avoids/{self.record['id']}"
        )

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(scanner.commands, ["AVD,CFREQ,20251,,3"])

    def test_an_undone_avoid_is_marked_not_removed(self):
        """AVD writes the working copy only, so the channel is scanning again
        now but comes back avoided at the next power cycle -- until some
        permanent avoid flushes it. A record that vanished here would be
        claiming a save that hasn't happened."""
        self._request(FakeScanner(), "DELETE", f"/scanners/home/avoids/{self.record['id']}")

        [kept] = self.avoids.for_scanner("home")
        self.assertIsNotNone(kept["cleared_at"])

    def test_the_undo_does_not_claim_to_be_saved(self):
        _status, body = self._request(
            FakeScanner(), "DELETE", f"/scanners/home/avoids/{self.record['id']}"
        )
        self.assertFalse(body["saved"])

    def test_a_refused_undo_keeps_the_record(self):
        """Dropping it would throw away the only copy of the index, leaving
        the channel avoided on the hardware with nothing able to reach it."""
        scanner = FakeScanner(reply="AVD,NG")
        _status, body = self._request(
            scanner, "DELETE", f"/scanners/home/avoids/{self.record['id']}"
        )

        self.assertFalse(body["ok"])
        self.assertEqual(body["command"], "AVD,CFREQ,20251,,3")
        self.assertEqual(len(self.avoids.for_scanner("home")), 1)

    def test_forget_drops_the_record_without_touching_the_scanner(self):
        # For an avoid already cleared on the unit: nothing here can detect
        # that, so it has to be something the operator can say.
        scanner = FakeScanner()
        _status, body = self._request(
            scanner, "DELETE", f"/scanners/home/avoids/{self.record['id']}?forget=true"
        )

        self.assertTrue(body["ok"])
        self.assertTrue(body["forgotten"])
        self.assertEqual(scanner.commands, [])
        self.assertEqual(self.avoids.for_scanner("home"), [])

    def test_an_unknown_record_is_a_404(self):
        status, _body = self._request(FakeScanner(), "DELETE", "/scanners/home/avoids/999")
        self.assertEqual(status, 404)

    def test_another_scanners_record_is_not_reachable(self):
        status, _body = self._request(
            FakeScanner(), "DELETE", f"/scanners/nosuch/avoids/{self.record['id']}"
        )
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
