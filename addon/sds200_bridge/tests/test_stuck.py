"""Tests for stuck.py -- noticing a scanner that quietly stopped scanning.

The failure this module exists for looks *normal* in every field anything
else reads: `mode` is "Scan Mode", the display says "Scanning...", the
control link answers every poll, and the scanner really is scanning -- one
department out of a list of 359 systems. So the things worth pinning down
are the ones that decide whether it gets noticed at all:

* a scope hold (System/Department/Site) is reported, a channel hold is not.
  That line is the whole design: the channel hold is what the card's Hold
  button sets, and a watchdog that cried wolf on ordinary use would be
  switched off inside a week.
* it repeats. An edge-only event sends its one notification at 02:00 and the
  scanner is still stuck at 13:00 -- which is what actually happened, twice.
* it never sends a key. Every episode of this was caused by something
  pressing a key at a screen it had misread.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import reception  # noqa: E402
import stuck  # noqa: E402
from stuck import StuckWatch  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds=1.0):
        self.now += seconds
        return self.now


class FakeConnection:
    """Deliberately without send_command.

    stuck.py is not supposed to have any way to touch the scanner, and a fake
    that offered one would let a regression that started pressing keys pass
    the tests. If this module ever grows a send, these tests fail with an
    AttributeError, which is the right way to find out.
    """

    def __init__(self, scanner_id="home"):
        self.id = scanner_id


class RecordingEngine:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def on_call(self, event, record):
        self.calls.append((event, record))

    def events(self):
        return [event for event, _ in self.calls]


def status(*, held=(), channel_hold=False, mode="Scan Mode"):
    """A status dict shaped like the real one, for a scanner that is scanning.

    The default is the state this scanner was actually found in: `Scan Mode`,
    `conventional_scan`, display reading "Scanning...", and the hold flags the
    only thing that says otherwise.
    """
    gsi: dict = {
        "mode": mode,
        "v_screen": "conventional_scan",
        "System": {"Name": "Citizens Band (CB) - USA", "Index": "36312", "Hold": "Off"},
        "Department": {"Name": "Citizens Band (CB)", "Index": "36315", "Hold": "Off"},
        "ConvFrequency": {
            "Name": "Channel 22", "Index": "36361", "Freq": "  27.225000MHz", "Hold": "Off",
        },
    }
    for element in held:
        gsi[element] = {**gsi.get(element, {}), "Hold": "On"}
    if channel_hold:
        gsi["ConvFrequency"]["Hold"] = "On"
    return {"gsi": gsi, "soft_keys": ["SYSTEM", "DEPT", "CHANNEL"]}


class TestScopeHolds(unittest.TestCase):
    """The line between a hold worth reporting and one that isn't."""

    def test_the_held_scopes_are_named_in_nesting_order(self):
        gsi = status(held=("Department", "System"))["gsi"]
        self.assertEqual(reception.scope_holds(gsi), ("System", "Department"))

    def test_a_channel_hold_is_not_a_scope_hold(self):
        # The deliberate one: this is exactly what the card's Hold button and
        # the sds200.hold service set, via reception.channel_target.
        gsi = status(channel_hold=True)["gsi"]
        self.assertEqual(reception.scope_holds(gsi), ())
        # ...while still being a hold as far as "has it stopped" goes.
        self.assertTrue(reception.is_holding(gsi))

    def test_a_scanner_that_is_scanning_holds_nothing(self):
        self.assertEqual(reception.scope_holds(status()["gsi"]), ())
        self.assertEqual(reception.scope_holds({}), ())

    def test_the_snapshot_carries_them(self):
        snapshot = reception.extract(status(held=("System",)))
        self.assertEqual(snapshot["held_scopes"], ("System",))


class _WatchTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.engine = RecordingEngine()
        self.watch = StuckWatch(self.engine, clock=self.clock)
        self.conn = FakeConnection()
        self.watch.attach(self.conn)
        # The warning is the point of the module, but not of the test output.
        logging.getLogger("stuck").setLevel(logging.CRITICAL)

    def tearDown(self):
        logging.getLogger("stuck").setLevel(logging.NOTSET)

    def poll(self, seconds=1.0, **kwargs):
        """Advance the clock and hand the watch one status update."""
        self.clock.tick(seconds)
        self.watch.status_callback(self.conn.id, status(**kwargs))


class TestHeldScopes(_WatchTest):
    def setUp(self):
        super().setUp()
        self.watch.configure(self.conn.id, hold_s=600, silence_s=0)

    def test_a_hold_shorter_than_the_threshold_is_not_reported(self):
        self.poll(300, held=("System",))
        self.assertEqual(self.engine.events(), [])

    def test_a_hold_past_the_threshold_is_reported_once(self):
        self.poll(1, held=("System", "Department"))
        self.poll(600, held=("System", "Department"))
        self.assertEqual(self.engine.events(), ["stuck"])
        _, record = self.engine.calls[0]
        self.assertEqual(record["reason"], "held")
        self.assertEqual(record["held_scopes"], ["System", "Department"])
        self.assertGreaterEqual(record["stuck_for_s"], 600)
        self.assertIn("system and department", record["label"].lower())

        # Still held a minute later: the same episode, not a second one.
        self.poll(60, held=("System", "Department"))
        self.assertEqual(self.engine.events(), ["stuck"])

    def test_it_repeats_while_the_scanner_stays_stuck(self):
        # Two polls to trip it, here and below: the clock starts when the
        # hold is first *seen*, so the poll that discovers it is time zero.
        self.poll(1, held=("System",))
        self.poll(601, held=("System",))
        self.poll(stuck.REPEAT_S, held=("System",))
        self.assertEqual(self.engine.events(), ["stuck", "stuck"])
        # The repeat says how long it has been, which is what makes a second
        # notification worth more than the first.
        self.assertGreater(self.engine.calls[1][1]["stuck_for_s"],
                           self.engine.calls[0][1]["stuck_for_s"])

    def test_releasing_it_clears(self):
        self.poll(1, held=("System",))
        self.poll(601, held=("System",))
        self.poll(30)
        self.assertEqual(self.engine.events(), ["stuck", "stuck_clear"])
        _, record = self.engine.calls[1]
        # The scopes are gone from the scanner by now -- that is what ended
        # the episode -- so the all-clear has to have kept its own copy.
        self.assertEqual(record["held_scopes"], ["System"])
        self.assertEqual(record["reason"], "held")

    def test_a_second_episode_reports_again(self):
        self.poll(1, held=("System",))
        self.poll(601, held=("System",))
        self.poll(30)
        self.poll(1, held=("Department",))
        self.poll(601, held=("Department",))
        self.assertEqual(self.engine.events(), ["stuck", "stuck_clear", "stuck"])
        self.assertEqual(self.engine.calls[2][1]["held_scopes"], ["Department"])

    def test_a_growing_hold_does_not_restart_the_clock(self):
        # System held first, Department a press later: one accident getting
        # worse. Restarting the clock would push the report out by another
        # ten minutes every time somebody's stray press landed.
        self.poll(1, held=("System",))
        self.poll(400, held=("System",))
        self.poll(200, held=("System", "Department"))
        self.assertEqual(self.engine.events(), ["stuck"])
        self.assertEqual(self.engine.calls[0][1]["held_scopes"], ["System", "Department"])

    def test_a_channel_hold_is_never_reported(self):
        self.poll(10_000, channel_hold=True)
        self.poll(10_000, channel_hold=True)
        self.assertEqual(self.engine.events(), [])

    def test_zero_switches_it_off(self):
        self.watch.configure(self.conn.id, hold_s=0, silence_s=0)
        self.poll(10_000, held=("System", "Department"))
        self.assertEqual(self.engine.events(), [])


class TestSilence(_WatchTest):
    def setUp(self):
        super().setUp()
        self.watch.configure(self.conn.id, hold_s=600, silence_s=7200)

    def test_silence_is_measured_from_when_the_scanner_started(self):
        # Not from the epoch, and not from the first call: a scanner that has
        # just come up has not been silent for fifty-six years.
        self.poll(60)
        self.assertEqual(self.engine.events(), [])

    def test_hearing_nothing_for_long_enough_is_reported(self):
        self.poll(7200)
        self.assertEqual(self.engine.events(), ["stuck"])
        self.assertEqual(self.engine.calls[0][1]["reason"], "silent")

    def test_a_call_resets_the_clock(self):
        self.poll(7000)
        self.watch.on_call("end", {"scanner_id": self.conn.id})
        self.poll(1000)
        self.assertEqual(self.engine.events(), [])
        self.poll(6300)
        self.assertEqual(self.engine.events(), ["stuck"])

    def test_a_call_on_another_scanner_does_not_count(self):
        self.poll(7000)
        self.watch.on_call("end", {"scanner_id": "somewhere-else"})
        self.poll(300)
        self.assertEqual(self.engine.events(), ["stuck"])

    def test_a_hold_is_reported_as_a_hold_even_when_it_is_also_silent(self):
        # Both conditions true. Reporting the silence would send somebody
        # hunting an antenna fault when the answer is one HLD away.
        self.poll(1, held=("System",))
        self.poll(7200, held=("System",))
        self.assertEqual(self.engine.events(), ["stuck"])
        self.assertEqual(self.engine.calls[0][1]["reason"], "held")

    def test_off_by_default_is_the_configured_default(self):
        self.watch.configure(self.conn.id, hold_s=600, silence_s=0)
        self.poll(100_000)
        self.assertEqual(self.engine.events(), [])


class TestItNeverTouchesTheScanner(_WatchTest):
    def test_no_command_is_ever_sent(self):
        self.watch.configure(self.conn.id, hold_s=60, silence_s=60)
        for _ in range(20):
            self.poll(600, held=("System", "Department"))
        # FakeConnection has no send_command at all, so reaching here at all
        # is the assertion. Stated explicitly so the reason survives a
        # refactor of the fake.
        self.assertFalse(hasattr(self.conn, "send_command"))
        self.assertTrue(self.engine.events())


class TestDetach(_WatchTest):
    def test_a_removed_scanner_stops_being_watched(self):
        self.watch.configure(self.conn.id, hold_s=600, silence_s=0)
        self.watch.detach(self.conn.id)
        self.poll(10_000, held=("System",))
        self.assertEqual(self.engine.events(), [])

    def test_configure_on_an_unknown_scanner_is_ignored(self):
        self.watch.configure("never-attached", hold_s=1, silence_s=1)


class TestDescribe(unittest.TestCase):
    def test_a_held_scope_is_named_in_the_scanners_own_words(self):
        self.assertEqual(
            stuck.describe("held", ("System", "Department"), 40_500),
            "held on system and department for 11.2h",
        )

    def test_a_short_episode_reads_in_minutes(self):
        self.assertEqual(stuck.describe("silent", (), 1800), "nothing received for 30m")


if __name__ == "__main__":
    unittest.main()
