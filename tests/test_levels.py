"""The volume/squelch reconciliation in the integration's levels.py.

Imported as a bare module rather than through the package, the same way the
api_client test does it: levels.py deliberately has no homeassistant import,
so it can be exercised without HA installed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "custom_components" / "sds200"))

from levels import SETTLE_SECONDS, ReportedLevel, gsi_level  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class GsiLevelTests(unittest.TestCase):
    def test_reads_property_attributes(self):
        status = {"gsi": {"Property": {"VOL": "7", "SQL": "3"}}}
        self.assertEqual(gsi_level(status, "VOL"), 7)
        self.assertEqual(gsi_level(status, "SQL"), 3)

    def test_missing_is_none_not_zero(self):
        # Every one of these is a real shape off the wire: an STS-only push
        # has no "gsi" at all, and which GSI children are present depends on
        # the scan mode. None of them mean "the volume is 0".
        for status in ({}, {"gsi": {}}, {"gsi": {"Property": {}}}, {"gsi": {"Property": {"VOL": "x"}}}):
            self.assertIsNone(gsi_level(status, "VOL"), status)


class ReportedLevelTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.level = ReportedLevel(14, clock=self.clock)

    def test_default_until_something_is_reported(self):
        self.assertEqual(self.level.resolve(None), 14)

    def test_scanner_wins_when_nothing_was_set(self):
        # The whole point: a level changed on the unit's own knob.
        self.assertEqual(self.level.resolve(9), 9)
        self.assertEqual(self.level.value, 9)

    def test_last_reading_survives_a_gap_in_reporting(self):
        self.level.resolve(9)
        self.assertEqual(self.level.resolve(None), 9)

    def test_stale_reading_does_not_undo_a_set(self):
        self.level.resolve(9)
        self.level.set(12)
        # A GSI poll already in flight when the set landed still says 9.
        self.assertEqual(self.level.resolve(9), 12)
        self.assertEqual(self.level.resolve(12), 12)

    def test_agreement_hands_control_back_to_the_scanner(self):
        self.level.set(12)
        self.level.resolve(12)
        self.assertEqual(self.level.resolve(4), 4)

    def test_a_set_that_never_took_expires(self):
        self.level.set(12)
        self.assertEqual(self.level.resolve(9), 12)
        self.clock.now += SETTLE_SECONDS
        # Still 9 after the settle window: the command was dropped, or the
        # scanner clamped it. Believe the scanner.
        self.assertEqual(self.level.resolve(9), 9)


if __name__ == "__main__":
    unittest.main()
