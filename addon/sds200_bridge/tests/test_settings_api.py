"""Tests for the /api/history routes: its date bounds, and clearing it.

The receive history goes back months, so "show me the afternoon of the 6th"
is a normal question and the parsing behind it is worth pinning down. Two
decisions here are easy to get wrong in a way nobody notices until a search
quietly returns the wrong day:

* an ISO value is *local* time, not UTC -- it comes from a picker sitting
  next to rows rendered in local time;
* a bare date used as the end of a range means all of that day, not the
  midnight that starts it.

Clearing is here for a third: deleting the rows has to delete the audio with
them. A clip is named after its call's row id, and SQLite issues ids from 1
again once the table is empty -- so audio left behind holds the numbers the
next calls will be given, and ClipStore.prune, which reads recency out of
the id, deletes each new clip as the oldest file in the directory moments
after it is written.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

try:
    from aiohttp import web
except ImportError:  # pragma: no cover - exercised only without aiohttp
    web = None

if web is not None:
    import manager
    import settings_api
    from settings_api import _time_param


@unittest.skipIf(web is None, "aiohttp is not installed")
class TimeParamTest(unittest.TestCase):
    def test_blank_means_unbounded(self):
        self.assertIsNone(_time_param(None))
        self.assertIsNone(_time_param(""))
        self.assertIsNone(_time_param("   "))

    def test_epoch_seconds_pass_through(self):
        self.assertEqual(_time_param("1754481600"), 1754481600.0)
        self.assertEqual(_time_param("1754481600.5"), 1754481600.5)

    def test_an_iso_datetime_is_read_as_local_time(self):
        # What the browser's datetime-local input sends.
        self.assertEqual(
            _time_param("2026-08-06T14:32"),
            datetime(2026, 8, 6, 14, 32).timestamp(),
        )

    def test_a_bare_date_starts_the_day(self):
        self.assertEqual(_time_param("2026-08-06"), datetime(2026, 8, 6).timestamp())

    def test_a_bare_date_as_an_upper_bound_covers_the_whole_day(self):
        # The failure this guards: "until the 6th" excluding everything heard
        # on the 6th, because midnight is where the day starts.
        bound = _time_param("2026-08-06", end_of_day=True)
        self.assertGreater(bound, datetime(2026, 8, 6, 23, 59).timestamp())
        self.assertLess(bound, datetime(2026, 8, 7).timestamp())

    def test_an_explicit_time_is_not_stretched_to_the_end_of_the_day(self):
        self.assertEqual(
            _time_param("2026-08-06T09:15", end_of_day=True),
            datetime(2026, 8, 6, 9, 15).timestamp(),
        )

    def test_nonsense_is_rejected_rather_than_ignored(self):
        # Silently dropping an unparseable bound would answer a different
        # question than the one asked, with no sign that it had.
        with self.assertRaises(web.HTTPBadRequest):
            _time_param("last tuesday")


@unittest.skipIf(web is None, "aiohttp is not installed")
class LogLevelTest(unittest.TestCase):
    """The access log is the noise floor of this add-on's log.

    Two requests every five seconds per open settings page, at INFO, on the
    same stream as everything worth reading. Debug logging turned on to
    investigate the audio path came back as a wall of `GET /api/settings`
    and nothing else -- so the level applies to the app, not to aiohttp's
    request log, which stays at WARNING.
    """

    def setUp(self):
        self.access = logging.getLogger("aiohttp.access")
        root = logging.getLogger()
        self.addCleanup(root.setLevel, root.level)
        self.addCleanup(self.access.setLevel, self.access.level)

    def test_debug_logging_does_not_turn_the_access_log_back_on(self):
        manager.set_log_level("debug")
        self.assertEqual(logging.getLogger().level, logging.DEBUG)
        self.assertEqual(self.access.level, logging.WARNING)

    def test_an_unknown_level_still_falls_back_to_info(self):
        manager.set_log_level("trace")
        self.assertEqual(logging.getLogger().level, logging.INFO)


class FakeHistory:
    def __init__(self, clips):
        self.clips = clips
        self.cleared = None

    def clip_names(self, scanner_id=None):
        return [name for name, owner in self.clips.items()
                if scanner_id is None or owner == scanner_id]

    def clear(self, scanner_id=None):
        self.cleared = scanner_id
        removed = self.clip_names(scanner_id)
        for name in removed:
            del self.clips[name]
        return len(removed)


class FakeTranscriber:
    def __init__(self):
        self.discarded = []

    def discard_clips(self, names):
        self.discarded.extend(names)
        return len(self.discarded)


class FakeRequest:
    def __init__(self, app, query):
        self.app = app
        self.query = query


@unittest.skipIf(web is None, "aiohttp is not installed")
class ClearHistoryTest(unittest.TestCase):
    def _delete(self, query, transcriber):
        history = FakeHistory({"1.wav": "home", "2.wav": "shed"})
        app = {
            settings_api.HISTORY_KEY: history,
            settings_api.MANAGER_KEY: types.SimpleNamespace(transcriber=transcriber),
        }
        response = asyncio.run(settings_api.delete_history(FakeRequest(app, query)))
        return history, json.loads(response.body)

    def test_clearing_the_history_takes_the_clips_with_it(self):
        transcriber = FakeTranscriber()
        history, body = self._delete({}, transcriber)
        self.assertEqual(body["removed"], 2)
        self.assertIsNone(history.cleared)
        self.assertEqual(sorted(transcriber.discarded), ["1.wav", "2.wav"])

    def test_clearing_one_scanner_leaves_the_other_scanners_audio(self):
        transcriber = FakeTranscriber()
        history, body = self._delete({"scanner": "home"}, transcriber)
        self.assertEqual(history.cleared, "home")
        self.assertEqual(transcriber.discarded, ["1.wav"])

    def test_clearing_works_with_no_transcriber_at_all(self):
        # Transcription is off by default, and the manager has no transcriber
        # in that case. Deleting the log must not depend on one.
        history, body = self._delete({}, None)
        self.assertEqual(body["removed"], 2)


if __name__ == "__main__":
    unittest.main()
