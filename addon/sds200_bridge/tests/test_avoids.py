"""Tests for avoids.py -- the record of what has been permanently avoided.

This store is the only copy of something the hardware cannot be asked for
twice: an avoided channel never comes back round in the poll stream, so the
list index it was avoided by is gone the moment this file loses it. That
makes the persistence path worth testing directly rather than by inspection,
and it is why these tests write to a real temporary file rather than stub
the I/O out.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import avoids  # noqa: E402

TARGET = {"tkw": "CFREQ", "index": "20251", "element": "ConvFrequency", "name": "Channel 3"}
CONTEXT = {"system": "FRS - USA", "department": "FRS", "frequency": 462.6125, "mode": "analog"}


class AvoidLogTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        os.unlink(self.path)  # a path that doesn't exist yet, like first run
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.log = avoids.AvoidLog(path=self.path)

    def _record(self, log=None, scanner="home", target=None):
        return (log or self.log).record(
            scanner, target or TARGET, "AVD,CFREQ,20251,,1", "AVD,OK", context=CONTEXT
        )

    def test_records_what_was_sent_and_what_was_on_screen(self):
        record = self._record()

        # The target as sent: an undo replays these two fields, so they are
        # the ones that must survive verbatim.
        self.assertEqual(record["tkw"], "CFREQ")
        self.assertEqual(record["index"], "20251")
        self.assertEqual(record["name"], "Channel 3")
        self.assertEqual(record["command"], "AVD,CFREQ,20251,,1")
        # Context, so a row reads like a channel rather than a number.
        self.assertEqual(record["system"], "FRS - USA")
        self.assertEqual(record["department"], "FRS")
        self.assertIsNotNone(record["at"])

    def test_survives_a_restart(self):
        """The whole point: /data outlives the process, and the index cannot
        be re-read from the scanner if it doesn't."""
        self._record()
        reloaded = avoids.AvoidLog(path=self.path)

        self.assertEqual(len(reloaded.for_scanner("home")), 1)
        self.assertEqual(reloaded.for_scanner("home")[0]["index"], "20251")

    def test_writes_through_immediately(self):
        # Not debounced like history.py: an unclean shutdown between the
        # avoid and the save would lose the only copy of the index.
        self._record()
        with open(self.path) as f:
            self.assertEqual(len(json.load(f)["records"]), 1)

    def test_ids_keep_climbing_across_a_restart(self):
        first = self._record()
        reloaded = avoids.AvoidLog(path=self.path)
        second = self._record(log=reloaded)

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(reloaded.for_scanner("home")), 2)

    def test_scanners_do_not_see_each_others_avoids(self):
        self._record(scanner="home")
        self._record(scanner="shop")

        self.assertEqual(len(self.log.for_scanner("home")), 1)
        self.assertEqual(len(self.log.for_scanner("shop")), 1)
        self.assertIsNone(self.log.get("shop", self.log.for_scanner("home")[0]["id"]))

    def test_newest_first(self):
        self._record(target={**TARGET, "name": "Older"})
        self._record(target={**TARGET, "name": "Newer"})

        self.assertEqual([r["name"] for r in self.log.for_scanner("home")], ["Newer", "Older"])

    def test_get_accepts_the_string_id_a_url_carries(self):
        record = self._record()
        self.assertEqual(self.log.get("home", str(record["id"])), record)
        self.assertIsNone(self.log.get("home", "not-a-number"))
        self.assertIsNone(self.log.get("home", 999))

    def test_removing_persists(self):
        record = self._record()
        self.log.remove(record)

        self.assertEqual(self.log.for_scanner("home"), [])
        self.assertEqual(avoids.AvoidLog(path=self.path).for_scanner("home"), [])

    def test_the_cap_drops_the_oldest(self):
        log = avoids.AvoidLog(path=self.path, max_records=3)
        for n in range(5):
            self._record(log=log, target={**TARGET, "name": f"Channel {n}"})

        names = [r["name"] for r in log.for_scanner("home")]
        self.assertEqual(names, ["Channel 4", "Channel 3", "Channel 2"])

    def test_a_corrupt_file_does_not_stop_the_add_on(self):
        Path(self.path).write_text("{not json")
        with self.assertLogs("avoids", level="WARNING"):
            log = avoids.AvoidLog(path=self.path)
        self.assertEqual(log.for_scanner("home"), [])

    def test_no_path_keeps_it_in_memory(self):
        log = avoids.AvoidLog(path=None)
        self._record(log=log)
        self.assertEqual(len(log.for_scanner("home")), 1)


if __name__ == "__main__":
    unittest.main()
