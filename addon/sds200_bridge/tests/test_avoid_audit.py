"""Tests for reading the scanner's own avoid state back (avoid_audit).

The point of this module is that the log can be *wrong* -- a stale record, a
temporary avoid that looks permanent, a keypress that landed on a channel
nobody wrote down -- so the tests are mostly about those disagreements
rather than the happy path.

Two behaviours here were established against real hardware and are easy to
regress, so both are pinned:

* a trunked department's entries come from `GLT,TGID`, and `GLT,CFREQ` on
  one returns *nothing* rather than an error, so an audit that only asks for
  CFREQ silently skips every trunked system;
* an unrecognized list type comes back as the `FL` list instead of an error.

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

import avoid_audit  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# One conventional system with two departments, one trunked system with a
# site and a talkgroup. Small, but it has every shape the walk has to cope
# with -- including an avoid at system level and one at talkgroup level.
TREE = {
    "GLT,SYS": [
        ("SYS", {"Index": "1", "Name": "County", "Avoid": "Off", "Type": "Conventional"}),
        ("SYS", {"Index": "2", "Name": "Trunked", "Avoid": "T-Avoid", "Type": "P25 Trunk"}),
    ],
    "GLT,DEPT,1": [
        ("DEPT", {"Index": "10", "Name": "Fire", "Avoid": "Off"}),
        ("DEPT", {"Index": "20", "Name": "Police", "Avoid": "Off"}),
    ],
    "GLT,DEPT,2": [("DEPT", {"Index": "30", "Name": "Ops", "Avoid": "Off"})],
    "GLT,SITE,2": [("SITE", {"Index": "40", "Name": "North", "Avoid": "Off"})],
    "GLT,CFREQ,10": [
        ("CFREQ", {"Index": "101", "Name": "Dispatch", "Avoid": "Off", "Freq": " 154.0MHz"}),
        ("CFREQ", {"Index": "102", "Name": "Fireground", "Avoid": "Avoid", "Freq": " 156.0MHz"}),
    ],
    "GLT,CFREQ,20": [
        ("CFREQ", {"Index": "201", "Name": "Tactical", "Avoid": "T-Avoid", "Freq": " 155.0MHz"}),
    ],
    # The trunked department's entries are talkgroups; asking for CFREQ here
    # returns an empty list, exactly as the hardware does.
    "GLT,CFREQ,30": [],
    "GLT,TGID,30": [
        ("TGID", {"Index": "301", "Name": "Ops Primary", "Avoid": "Avoid", "TGID": "0-01-001"}),
    ],
}

FL_LIST = [("FL", {"Index": "4294967295", "Name": "Full Database", "Monitor": "On"})]


class FakeScanner:
    """Answers GLT out of TREE; anything it doesn't recognize gets the FL
    list, which is what the real scanner does and is the trap being tested."""

    def __init__(self, tree=None, fail_on=(), fail_first=(), error=None):
        self.id = "home"
        self.tree = TREE if tree is None else tree
        self.fail_on = set(fail_on)
        # Commands that fail once and then answer, which is what a dropped
        # datagram looks like from here.
        self.fail_first = dict.fromkeys(fail_first, 1)
        self.error = error or RuntimeError("timed out reassembling XML response")
        self.commands: list[str] = []

    async def send_xml_command(self, command: str):
        self.commands.append(command)
        if self.fail_first.get(command):
            self.fail_first[command] -= 1
            raise self.error
        if command in self.fail_on:
            raise self.error
        entries = self.tree.get(command)
        if entries is None:
            entries = FL_LIST
        root = ET.Element("GLT")
        for tag, attrs in entries:
            ET.SubElement(root, tag, attrs)
        return root


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.scanner = FakeScanner()

    def _sweep(self, scanner=None):
        return run(avoid_audit.sweep(scanner or self.scanner, pace=0))

    def test_finds_avoids_at_every_level(self):
        result = self._sweep()
        found = {(a["level"], a["index"], a["avoid"]) for a in result["avoided"]}

        self.assertEqual(
            found,
            {
                ("system", "2", "T-Avoid"),
                ("channel", "102", "Avoid"),
                ("channel", "201", "T-Avoid"),
                ("talkgroup", "301", "Avoid"),
            },
        )

    def test_trunked_departments_are_read_as_talkgroups(self):
        # The regression that matters: CFREQ on a trunked department is
        # empty rather than an error, so only asking for TGID finds these.
        self._sweep()
        self.assertIn("GLT,TGID,30", self.scanner.commands)
        self.assertNotIn("GLT,CFREQ,30", self.scanner.commands)

    def test_counts_what_it_actually_read(self):
        counts = self._sweep()["counts"]
        self.assertEqual(counts["systems"], 2)
        self.assertEqual(counts["departments"], 3)
        self.assertEqual(counts["channels"], 3)
        self.assertEqual(counts["talkgroups"], 1)
        self.assertEqual(counts["sites"], 1)

    def test_avoided_entries_carry_their_parents(self):
        channel = next(a for a in self._sweep()["avoided"] if a["index"] == "102")
        self.assertEqual(channel["system"], "County")
        self.assertEqual(channel["department"], "Fire")
        self.assertEqual(channel["department_index"], "10")
        self.assertEqual(channel["frequency"], "156.0MHz")

    def test_a_failed_read_is_reported_not_swallowed(self):
        scanner = FakeScanner(fail_on={"GLT,CFREQ,10"})
        result = self._sweep(scanner)

        # The rest of the walk still happens -- one dead department should
        # not cost the other 1900 -- but the failure is on the record, so a
        # partial answer can't be read as a clean one.
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(result["failures"][0]["index"], "10")
        self.assertTrue(any(a["index"] == "301" for a in result["avoided"]))

    def test_a_dropped_datagram_is_retried_rather_than_lost(self):
        # UDP, and send_xml_command does not retry the way send_command
        # does. One lost reply to GLT,SYS used to end the walk before it
        # started: nothing read, nothing avoided, and a UI that reported
        # the all-clear that came out of it.
        scanner = FakeScanner(fail_first={"GLT,SYS"})
        result = self._sweep(scanner)

        self.assertEqual(result["failures"], [])
        self.assertEqual(result["counts"]["systems"], 2)
        self.assertEqual(scanner.commands.count("GLT,SYS"), 2)

    def test_an_unrecognized_list_type_is_not_taken_as_data(self):
        # GLT with a bad type answers with the favourites list; treating
        # that as the answer would invent entries that aren't there.
        self.assertEqual(run(avoid_audit._list(FakeScanner(), "NOPE", "1")), [])


class CheckRecordsTest(unittest.TestCase):
    def setUp(self):
        self.scanner = FakeScanner()

    def _check(self, records):
        return run(avoid_audit.check_records(self.scanner, records, pace=0))

    def _record(self, **kw):
        base = {"id": 1, "tkw": "CFREQ", "index": "102", "department_index": "10",
                "name": "Fireground", "cleared_at": None}
        return {**base, **kw}

    def test_a_record_the_scanner_confirms(self):
        checked = self._check([self._record()])[0]
        self.assertEqual(checked["scanner_state"], "Avoid")
        self.assertTrue(checked["agrees"])

    def test_a_temporary_avoid_does_not_count_as_agreement(self):
        # The scanner says T-Avoid: the two presses were too far apart, and
        # this one disappears at the next power cycle. The log claims a
        # permanent avoid, so these disagree.
        checked = self._check([self._record(index="201", department_index="20")])[0]
        self.assertEqual(checked["scanner_state"], "T-Avoid")
        self.assertFalse(checked["agrees"])

    def test_a_stale_record_is_caught(self):
        checked = self._check([self._record(index="101")])[0]
        self.assertEqual(checked["scanner_state"], "Off")
        self.assertFalse(checked["agrees"])

    def test_an_index_that_is_not_in_the_list_at_all(self):
        checked = self._check([self._record(index="999")])[0]
        self.assertIsNone(checked["scanner_state"])
        self.assertFalse(checked["agrees"])
        # The scanner answered; the record points at nothing. That is a
        # real disagreement, not an unanswered question.
        self.assertFalse(checked["unknown"])
        self.assertIn("not in that department", checked["detail"])

    def test_a_read_that_fails_is_unknown_rather_than_disagreement(self):
        # The failure that prompted this: one department read timed out
        # and every record in it was reported as disagreeing with the
        # scanner, when the scanner had not said anything at all.
        self.scanner = FakeScanner(fail_on={"GLT,CFREQ,10"})
        checked = self._check([self._record(), self._record(id=2, index="101")])

        for row in checked:
            self.assertIsNone(row["scanner_state"])
            self.assertFalse(row["agrees"])
            self.assertTrue(row["unknown"])
            self.assertIn("could not read the list back", row["detail"])

    def test_a_failure_with_no_message_still_says_what_it_was(self):
        # asyncio.TimeoutError's str() is empty, and the detail went to
        # the UI as "could not read the list back ()".
        self.scanner = FakeScanner(fail_on={"GLT,CFREQ,10"}, error=asyncio.TimeoutError())
        detail = self._check([self._record()])[0]["detail"]

        self.assertNotIn("()", detail)
        self.assertIn("TimeoutError", detail)

    def test_a_record_that_can_be_read_is_settled_despite_a_neighbour_failing(self):
        # Departments are read one at a time; one dead read should not
        # take the records in a department that answered with it.
        self.scanner = FakeScanner(fail_on={"GLT,CFREQ,10"})
        checked = self._check([
            self._record(),
            self._record(id=2, index="201", department_index="20"),
        ])

        self.assertTrue(checked[0]["unknown"])
        self.assertFalse(checked[1]["unknown"])
        self.assertEqual(checked[1]["scanner_state"], "T-Avoid")

    def test_a_cleared_record_expects_off(self):
        # Un-avoided in the working copy and not yet flushed: the scanner
        # should say Off, and that is agreement, not a discrepancy.
        checked = self._check([self._record(index="101", cleared_at=123.0)])[0]
        self.assertEqual(checked["expected_state"], "Off")
        self.assertTrue(checked["agrees"])

    def test_a_cleared_record_the_scanner_still_avoids_disagrees(self):
        checked = self._check([self._record(cleared_at=123.0)])[0]
        self.assertEqual(checked["scanner_state"], "Avoid")
        self.assertFalse(checked["agrees"])

    def test_each_department_is_read_once(self):
        self._check([
            self._record(id=1, index="101"),
            self._record(id=2, index="102"),
            self._record(id=3, index="201", department_index="20"),
        ])
        self.assertEqual(self.scanner.commands.count("GLT,CFREQ,10"), 1)
        self.assertEqual(self.scanner.commands.count("GLT,CFREQ,20"), 1)

    def test_a_record_with_no_department_cannot_be_looked_up(self):
        checked = self._check([self._record(department_index=None)])[0]
        self.assertIsNone(checked["scanner_state"])
        self.assertFalse(checked["agrees"])
        self.assertTrue(checked["unknown"])
        self.assertIn("no department index", checked["detail"])


class UnrecordedTest(unittest.TestCase):
    def test_matches_on_index(self):
        avoided = [
            {"index": "102", "avoid": "Avoid"},
            {"index": "999", "avoid": "Avoid"},
        ]
        extra = avoid_audit.unrecorded(avoided, [{"index": "102"}])
        self.assertEqual([a["index"] for a in extra], ["999"])

    def test_a_cleared_record_still_accounts_for_its_entry(self):
        # It is expected back at the next restart and is already on the list
        # saying so; reporting it as unrecorded would be a second alarm for
        # something already shown.
        avoided = [{"index": "102", "avoid": "Avoid"}]
        self.assertEqual(avoid_audit.unrecorded(avoided, [{"index": "102", "cleared_at": 1.0}]), [])


try:
    import aiohttp  # noqa: F401
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:  # pragma: no cover -- see test_api_avoid_current
    aiohttp = None


@unittest.skipIf(aiohttp is None, "aiohttp is not installed")
class VerifyRouteTest(unittest.TestCase):
    """GET /scanners/{id}/avoids/verify.

    The route's job is to keep the two costs apart: the cheap check runs on
    request, and the walk of the whole database only ever runs when asked
    for explicitly.
    """

    def setUp(self):
        import avoids

        self.avoids = avoids.AvoidLog(path=None)
        self.scanner = FakeScanner()

    def _record(self, **kw):
        target = {"tkw": "CFREQ", "index": kw.pop("index", "102"), "element": "ConvFrequency",
                  "name": kw.pop("name", "Fireground"),
                  "department_index": kw.pop("department_index", "10")}
        return self.avoids.record("home", target, "KEY,L,P x2", "Avoid", context=kw or None)

    def _get(self, query=""):
        import api

        async def scenario():
            app = api.create_app({"home": self.scanner}, {}, FakeStatusHub(), avoids=self.avoids)
            async with TestClient(TestServer(app)) as client:
                response = await client.get(f"/scanners/home/avoids/verify{query}")
                return response.status, await response.json()

        return run(scenario())

    def test_checks_each_record_without_sweeping(self):
        self._record()
        status, body = self._get()

        self.assertEqual(status, 200)
        self.assertFalse(body["swept"])
        self.assertNotIn("unrecorded", body)
        self.assertEqual(body["disagree"], 0)
        self.assertEqual(body["checked"][0]["scanner_state"], "Avoid")
        # Only the one department the record names.
        self.assertEqual(self.scanner.commands, ["GLT,CFREQ,10"])

    def test_counts_the_records_that_disagree(self):
        self._record(index="101")  # the scanner says Off
        self._record()             # and Avoid for this one
        status, body = self._get()

        self.assertEqual(body["disagree"], 1)
        self.assertEqual(body["unknown"], 0)

    def test_records_the_scanner_never_answered_for_are_not_disagreements(self):
        self.scanner = FakeScanner(fail_on={"GLT,CFREQ,10"})
        self._record(index="101")
        self._record()
        status, body = self._get()

        self.assertEqual(body["disagree"], 0)
        self.assertEqual(body["unknown"], 2)

    def test_sweeping_finds_what_no_record_accounts_for(self):
        self._record()  # channel 102 only
        status, body = self._get("?sweep=true")

        self.assertTrue(body["swept"])
        # 201 is temporary and 301 is a talkgroup avoided for good; neither
        # is in the log, and only the permanent one is the alarming kind.
        self.assertEqual(
            sorted(a["index"] for a in body["unrecorded"]), ["2", "201", "301"]
        )
        self.assertEqual([a["index"] for a in body["unrecorded_permanent"]], ["301"])
        self.assertEqual(body["counts"]["departments"], 3)

    def test_the_verify_path_is_not_read_as_a_record_id(self):
        # It sits next to DELETE /avoids/{avoid_id}; a route table that
        # matched "verify" as an id would 404 here.
        self.assertEqual(self._get()[0], 200)


class FakeStatusHub:
    async def handle(self, request):  # pragma: no cover -- no socket in these tests
        raise AssertionError("the status socket is not part of these tests")


if __name__ == "__main__":
    unittest.main()
