"""Tests for history.py -- turning a poll stream into calls.

The scanner pushes nothing; a call only exists because CallTracker inferred
it from a sequence of polls. The two rules that inference rests on are both
easy to get subtly wrong and impossible to notice from the outside:

* a call ends only after IDLE_POLLS_TO_END consecutive idle polls, because
  one dropped GSI read is indistinguishable from the squelch closing;
* a call ends immediately when the *identity* changes, because that is
  positive evidence of a different transmission rather than an absence of
  evidence.

Get the first wrong and every call splits in two on the first dropped read;
get the second wrong and back-to-back talkgroups merge into one row.

TestReceiveHistory covers the SQLite store around that: what retention drops
and what it must not (a call still in progress), that an interrupted call is
closed on the next start with the duration it had, and that filtering --
text, date range, and the two together -- means the same thing in SQL that
it did when the whole log was a deque in memory.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import history  # noqa: E402
import protocol  # noqa: E402
import reception  # noqa: E402
from history import CallTracker, ReceiveHistory  # noqa: E402


class FakeClock:
    """Time under test control -- a real clock would make every duration
    assertion here a flaky one."""

    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds=3.0):
        self.now += seconds
        return self.now


def snapshot(receiving=True, **fields):
    """A snapshot as reception.extract would produce it, without needing a
    whole GSI document per assertion."""
    base = {
        "receiving": receiving, "system": "Countywide", "department": "Fire",
        "channel": "Dispatch", "frequency": 851.0125, "tgid": "1001", "unit_id": None,
        "mode": "p25", "mod": "NFM", "sub_audio": None, "rssi": -70.0, "signal": 4,
        "site": None, "system_type": "P25 Standard", "service_type": None,
        "p25_status": "P25", "scan_mode": "Scan Mode",
    }
    base.update(fields)
    base["identity"] = reception.identity(base)
    return base


class TestCallTracker(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.tracker = CallTracker("home", clock=self.clock)

    def test_a_call_starts_on_the_first_receiving_poll(self):
        started, ended = self.tracker.update(snapshot())
        self.assertIsNotNone(started)
        self.assertIsNone(ended)
        self.assertEqual(started["channel"], "Dispatch")
        self.assertEqual(started["scanner_id"], "home")

    def test_a_continuing_call_does_not_start_a_second_one(self):
        self.tracker.update(snapshot())
        self.clock.tick()
        started, ended = self.tracker.update(snapshot())
        self.assertIsNone(started)
        self.assertIsNone(ended)
        self.assertEqual(self.tracker.open_call["polls"], 2)

    def test_one_idle_poll_does_not_end_the_call(self):
        # This is the dropped-GSI-read case. Ending here would split one
        # transmission into two history rows.
        self.tracker.update(snapshot())
        self.clock.tick()
        started, ended = self.tracker.update(snapshot(receiving=False))
        self.assertIsNone(ended)
        self.assertIsNotNone(self.tracker.open_call)

    def test_a_talkgroup_arriving_late_does_not_start_a_second_call(self):
        # The reported bug: one transmission appearing as several rows. A
        # trunked call reports its talkgroup a poll or two after the squelch
        # opens, and comparing whole identity strings made that arrival look
        # exactly like a change of talkgroup.
        self.tracker.update(snapshot(tgid=None))
        self.clock.tick()
        started, ended = self.tracker.update(snapshot(tgid="1001"))
        self.assertIsNone(started)
        self.assertIsNone(ended)
        self.assertEqual(self.tracker.open_call["polls"], 2)

    def test_the_call_takes_the_name_of_what_arrived_late(self):
        # The row has to end up describing what it actually heard, not what
        # was known in the poll we have just agreed not to trust.
        self.tracker.update(snapshot(tgid=None, channel=None))
        self.clock.tick()
        self.tracker.update(snapshot(tgid="1001", channel="Dispatch"))
        call = self.tracker.open_call
        self.assertEqual(call["polls"], 2)  # still the one call
        self.assertEqual(call["tgid"], "1001")
        self.assertIn("1001", call["identity"])
        self.assertIn("Dispatch", call["label"])

    def test_a_different_talkgroup_still_starts_a_new_call(self):
        # The other half. A value that genuinely disagrees is still positive
        # evidence of a different transmission, with no idle grace.
        self.tracker.update(snapshot(tgid="1001"))
        self.clock.tick()
        started, ended = self.tracker.update(snapshot(tgid="1002"))
        self.assertIsNotNone(started)
        self.assertIsNotNone(ended)
        self.assertEqual(started["tgid"], "1002")

    def test_a_field_going_blank_mid_call_is_not_a_new_call(self):
        # Losing a field is the scanner saying less, not saying something
        # different -- and it happens on the same polls that fill others in.
        self.tracker.update(snapshot(tgid="1001"))
        self.clock.tick()
        started, ended = self.tracker.update(snapshot(tgid=None))
        self.assertIsNone(started)
        self.assertIsNone(ended)
        self.assertEqual(self.tracker.open_call["tgid"], "1001")

    def test_a_conflict_in_any_identity_field_is_enough(self):
        self.tracker.update(snapshot())
        self.clock.tick()
        started, _ended = self.tracker.update(snapshot(department="Police"))
        self.assertIsNotNone(started)

    def test_two_idle_polls_end_the_call(self):
        self.tracker.update(snapshot())
        self.clock.tick()
        self.tracker.update(snapshot())
        self.clock.tick()
        self.tracker.update(snapshot(receiving=False))
        self.clock.tick()
        _started, ended = self.tracker.update(snapshot(receiving=False))
        self.assertIsNotNone(ended)
        self.assertIsNone(self.tracker.open_call)

    def test_a_call_resuming_after_one_idle_poll_stays_one_call(self):
        self.tracker.update(snapshot())
        self.clock.tick()
        self.tracker.update(snapshot(receiving=False))
        self.clock.tick()
        started, ended = self.tracker.update(snapshot())
        self.assertIsNone(started)
        self.assertIsNone(ended)

    def test_the_duration_excludes_the_polls_spent_confirming_the_end(self):
        # The call was last *seen* at t+3; the two idle polls that confirmed
        # its end are not part of it.
        self.tracker.update(snapshot())
        self.clock.tick(3.0)
        self.tracker.update(snapshot())
        self.clock.tick(3.0)
        self.tracker.update(snapshot(receiving=False))
        self.clock.tick(3.0)
        _started, ended = self.tracker.update(snapshot(receiving=False))
        self.assertEqual(ended["duration"], 3.0)
        self.assertEqual(ended["ended"], ended["started"] + 3.0)

    def test_a_changed_talkgroup_ends_one_call_and_starts_the_next(self):
        self.tracker.update(snapshot())
        self.clock.tick()
        started, ended = self.tracker.update(snapshot(tgid="1002", channel="Tac 2"))
        self.assertIsNotNone(ended)
        self.assertIsNotNone(started)
        self.assertEqual(ended["tgid"], "1001")
        self.assertEqual(started["tgid"], "1002")

    def test_unit_ids_accumulate_across_a_call(self):
        self.tracker.update(snapshot(unit_id="4021"))
        self.clock.tick()
        self.tracker.update(snapshot(unit_id="4099"))
        self.clock.tick()
        self.tracker.update(snapshot(unit_id="4021"))
        self.assertEqual(self.tracker.open_call["unit_ids"], ["4021", "4099"])

    def test_the_peak_rssi_is_kept_not_the_last(self):
        self.tracker.update(snapshot(rssi=-90.0))
        self.clock.tick()
        self.tracker.update(snapshot(rssi=-62.0))
        self.clock.tick()
        self.tracker.update(snapshot(rssi=-88.0))
        self.assertEqual(self.tracker.open_call["rssi_peak"], -62.0)

    def test_a_field_the_scanner_reports_late_fills_in(self):
        # A trunked call routinely reports the talkgroup a poll after the
        # squelch opens. Filling blanks in is what stops the row reading
        # "unknown" for a call the scanner did identify.
        self.tracker.update(snapshot(tgid=None, channel=None))
        self.clock.tick()
        self.tracker.update(snapshot(tgid="1001", channel="Dispatch"))
        self.assertEqual(self.tracker.open_call["channel"], "Dispatch")
        self.assertEqual(self.tracker.open_call["label"], "Dispatch / Fire / Countywide")

    def test_a_field_already_recorded_is_not_overwritten(self):
        self.tracker.update(snapshot(channel="Dispatch"))
        self.clock.tick()
        self.tracker.update(snapshot(channel="Dispatch"))
        # Same identity, so still one call -- and the first reading stands.
        self.assertEqual(self.tracker.open_call["channel"], "Dispatch")

    def test_flush_closes_an_open_call(self):
        self.tracker.update(snapshot())
        self.clock.tick()
        self.assertIsNotNone(self.tracker.flush())
        self.assertIsNone(self.tracker.open_call)


class TestReceiveHistory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "history.db"
        self.clock = FakeClock()
        self.store = self._store()

    def _store(self, **kwargs):
        options = {"max_records": 5, "retention_days": 90, "clock": self.clock}
        options.update(kwargs)
        store = ReceiveHistory(path=str(self.path), **options)
        self.addCleanup(store.close)
        return store

    def _status(self, **fields):
        """A status dict as the scanner's poll loop emits it. The "gsi" key
        is what marks it as a detail poll rather than a display-only STS
        one."""
        gsi = {
            "System": {"Name": "Countywide", "SystemType": "P25 Standard"},
            "Department": {"Name": "Fire"},
            "TGID": {"Name": "Dispatch", "TGID": "1001"},
            "SiteFrequency": {"Freq": " 851.012500MHz"},
            "Property": {"Rssi": "-70" if fields.pop("receiving", True) else "-999"},
        }
        for key, value in fields.items():
            gsi[key] = value
        return {"gsi": gsi}

    def _record_one_call(self, **fields):
        """One complete call: the receiving poll, then the idle polls that
        confirm its end."""
        self.store.status_callback("home", self._status(**fields))
        self.clock.tick()
        for _ in range(history.IDLE_POLLS_TO_END):
            self.store.status_callback("home", self._status(receiving=False, **fields))
            self.clock.tick()

    def test_sts_only_updates_are_ignored(self):
        # STS ticks three times as fast as GSI and carries no reception data
        # at all. Counting those as idle polls would close every call almost
        # as soon as it opened.
        self.store.status_callback("home", self._status())
        self.store.status_callback("home", {"lines": [{"text": "Scanning", "mode": " "}]})
        self.store.status_callback("home", {"lines": [{"text": "Scanning", "mode": " "}]})
        self.assertEqual(len(self.store.open_calls()), 1)

    def test_records_are_newest_first(self):
        for tgid in ("1001", "1002", "1003"):
            self.store.status_callback("home", self._status(TGID={"Name": tgid, "TGID": tgid}))
            self.clock.tick()
        self.assertEqual([r["tgid"] for r in self.store.records()], ["1003", "1002", "1001"])

    def test_the_record_cap_drops_the_oldest(self):
        for i in range(8):
            self.store.status_callback("home", self._status(TGID={"Name": str(i), "TGID": str(i)}))
            self.clock.tick()
        # Forced, because the cap is a disk backstop enforced on a timer
        # (PRUNE_INTERVAL_S) rather than on every insert -- overshooting it
        # for five minutes is cheaper than a delete per call.
        self.store.prune(force=True)
        self.assertEqual(len(self.store.records()), 5)
        self.assertEqual(self.store.records()[-1]["tgid"], "3")

    def test_calls_older_than_the_retention_window_are_dropped(self):
        self._record_one_call()
        old = self.store.records()[0]["id"]
        self.clock.tick(2 * history.DAY_S)
        self._record_one_call()
        store = self._store(retention_days=1)  # reopens the same file
        store.prune(force=True)
        self.assertNotIn(old, [r["id"] for r in store.records()])
        self.assertEqual(len(store.records()), 1)

    def test_an_open_call_is_never_pruned_out_from_under_its_tracker(self):
        # It has no `ended` yet, so a naive "started < cutoff" delete would
        # take the row the tracker is still writing to.
        self.store.status_callback("home", self._status())
        self.clock.tick(2 * history.DAY_S)
        store = self.store
        store.retention_days = 1
        store.prune(force=True)
        self.assertEqual(len(store.records()), 1)

    def test_searching_matches_across_the_text_fields(self):
        self.store.status_callback("home", self._status())
        self.assertEqual(len(self.store.records(query="dispatch")), 1)
        self.assertEqual(len(self.store.records(query="countywide")), 1)
        self.assertEqual(len(self.store.records(query="nothing here")), 0)

    def test_searching_matches_the_date_and_the_time(self):
        self.store.status_callback("home", self._status())
        started = self.store.records()[0]["started"]
        when = time.localtime(started)
        self.assertEqual(len(self.store.records(query=time.strftime("%Y-%m-%d", when))), 1)
        self.assertEqual(len(self.store.records(query=time.strftime("%H:%M", when))), 1)
        self.assertEqual(len(self.store.records(query=time.strftime("%d %b", when))), 1)
        self.assertEqual(len(self.store.records(query="1999-01-01")), 0)

    def test_a_wildcard_in_the_search_box_is_a_literal(self):
        # "%" reaching LIKE unescaped would match everything, which is the
        # opposite of what someone typing it into a search box expects.
        self.store.status_callback("home", self._status())
        self.assertEqual(len(self.store.records(query="%")), 0)
        self.assertEqual(len(self.store.records(query="dis_atch")), 0)

    def test_filtering_by_a_date_range(self):
        self._record_one_call()
        first = self.store.records()[0]["started"]
        self.clock.tick(3 * history.DAY_S)
        self._record_one_call()
        second = self.store.records()[0]["started"]

        self.assertEqual(len(self.store.records(since=first - 1)), 2)
        self.assertEqual(len(self.store.records(since=second - 1)), 1)
        self.assertEqual(len(self.store.records(until=first + 1)), 1)
        self.assertEqual(len(self.store.records(since=first - 1, until=first + 1)), 1)
        self.assertEqual(len(self.store.records(since=second + 1)), 0)

    def test_the_count_ignores_the_page_limit(self):
        for i in range(6):
            self.store.status_callback("home", self._status(TGID={"Name": str(i), "TGID": str(i)}))
            self.clock.tick()
        self.assertEqual(len(self.store.records(limit=2)), 2)
        self.assertEqual(self.store.count(), 6)
        self.assertEqual(self.store.count(query="nothing here"), 0)

    def test_the_span_reports_how_far_back_the_log_goes(self):
        self.assertEqual(self.store.span(), (None, None))
        self._record_one_call()
        first = self.store.records()[0]["started"]
        self.clock.tick(3 * history.DAY_S)
        self._record_one_call()
        oldest, newest = self.store.span()
        self.assertEqual(oldest, first)
        self.assertGreater(newest, oldest)

    def test_filtering_by_scanner_and_mode(self):
        self.store.status_callback("home", self._status())
        self.store.status_callback("shed", self._status())
        self.assertEqual(len(self.store.records(scanner_id="home")), 1)
        self.assertEqual(len(self.store.records(mode="p25")), 2)
        self.assertEqual(len(self.store.records(mode="dmr")), 0)

    def test_disabling_it_stops_recording(self):
        self.store.configure(enabled=False, max_records=5, retention_days=90)
        self.store.status_callback("home", self._status())
        self.assertEqual(self.store.records(), [])

    def test_listeners_see_the_start_and_the_end(self):
        events = []
        self.store.add_listener(lambda event, record: events.append((event, record["tgid"])))
        self.store.status_callback("home", self._status())
        self.clock.tick()
        self.store.status_callback("home", self._status(receiving=False))
        self.clock.tick()
        self.store.status_callback("home", self._status(receiving=False))
        self.assertEqual(events, [("start", "1001"), ("end", "1001")])

    def test_clearing_one_scanner_leaves_the_others(self):
        self.store.status_callback("home", self._status())
        self.store.status_callback("shed", self._status())
        self.assertEqual(self.store.clear("home"), 1)
        self.assertEqual([r["scanner_id"] for r in self.store.records()], ["shed"])

    def test_the_clips_of_a_scope_are_nameable_before_it_is_cleared(self):
        # Whoever deletes the rows has to delete the audio too, and this is
        # the last moment anyone can tell which files those were.
        self.store.status_callback("home", self._status())
        self.store.status_callback("shed", self._status())
        home, shed = (r["id"] for r in reversed(self.store.records()))
        self.store.set_transcript(home, "engine twelve", "ok", clip=f"{home}.wav")
        self.store.set_transcript(shed, "", "no-speech", clip=f"{shed}.wav")

        self.assertEqual(self.store.clip_names("home"), [f"{home}.wav"])
        self.assertEqual(sorted(self.store.clip_names()),
                         sorted([f"{home}.wav", f"{shed}.wav"]))

    def test_a_call_with_no_clip_contributes_no_name(self):
        self.store.status_callback("home", self._status())
        self.assertEqual(self.store.clip_names(), [])

    def test_existing_ids_answers_which_rows_survived(self):
        # What tells an orphaned clip from a current one.
        self.store.status_callback("home", self._status())
        call_id = self.store.records()[0]["id"]
        self.assertEqual(self.store.existing_ids([call_id, call_id + 500]), {call_id})
        self.assertEqual(self.store.existing_ids([]), set())

    def test_a_call_open_at_shutdown_is_closed_on_reload(self):
        # The open row is rewritten every poll, so a stored call can be one
        # nothing will ever close. Left as-is it would render as a
        # transmission that has been running for days.
        self.store.status_callback("home", self._status())
        self.clock.tick(4.0)
        self.store.status_callback("home", self._status())
        started = self.store.records()[0]["started"]

        # A restart with the call still open: a second store opens the same
        # file and finds a row nothing will ever close. Deliberately without
        # closing the first -- a power cut doesn't get a shutdown path.
        record = self._store().records()[0]
        self.assertEqual(record["ended"], started + 4.0)
        self.assertTrue(record["interrupted"])

    def test_the_partial_duration_of_an_interrupted_call_survives(self):
        # The point of rewriting the open row each poll rather than writing
        # once at the end: a call cut off by a restart is still in the log
        # with what was known about it at the time.
        self.store.status_callback("home", self._status())
        self.clock.tick(9.0)
        self.store.status_callback("home", self._status())

        record = self._store().records()[0]
        self.assertEqual(record["duration"], 9.0)
        self.assertEqual(record["channel"], "Dispatch")

    def test_a_corrupt_file_does_not_stop_the_addon(self):
        # Unlike config.json, this is a log rather than configuration --
        # refusing to start over it would be worse than losing it.
        #
        # Its own path, not setUp's: an open connection leaves a -wal beside
        # the database, and SQLite will happily serve a valid -wal over a
        # garbage main file, which would hide the very failure under test.
        path = Path(self._tmp.name) / "corrupt.db"
        path.write_text("this is not a database")
        store = ReceiveHistory(path=str(path), clock=self.clock)
        self.addCleanup(store.close)
        self.assertEqual(store.records(), [])
        # And the unreadable one is kept rather than silently destroyed.
        self.assertTrue(Path(str(path) + ".corrupt").exists())

    def test_ids_continue_after_a_reload(self):
        self.store.status_callback("home", self._status())
        self.clock.tick()
        self.store.close()
        reloaded = self._store()
        reloaded.status_callback("home", self._status(TGID={"Name": "x", "TGID": "2"}))
        self.assertEqual([r["id"] for r in reloaded.records()], [2, 1])


class TestIdlePollConstant(unittest.TestCase):
    def test_the_end_needs_more_than_one_idle_poll(self):
        # Stated as its own test because dropping this to 1 would look like a
        # harmless simplification and would silently split calls in two on
        # this firmware, which drops GSI reads routinely.
        self.assertGreater(history.IDLE_POLLS_TO_END, 1)


# The schema as it shipped before transcripts existed, so the migration is
# tested against the real thing rather than against a mock of it.
_PRE_TRANSCRIPT_SCHEMA = """
CREATE TABLE calls (
    id INTEGER PRIMARY KEY, scanner_id TEXT NOT NULL, started REAL NOT NULL,
    ended REAL, duration REAL NOT NULL DEFAULT 0, polls INTEGER NOT NULL DEFAULT 0,
    label TEXT, identity TEXT, unit_ids TEXT NOT NULL DEFAULT '[]', rssi_peak REAL,
    signal_peak INTEGER, system TEXT, system_type TEXT, department TEXT, site TEXT,
    channel TEXT, frequency REAL, mod TEXT, mode TEXT, tgid TEXT, sub_audio TEXT,
    service_type TEXT, p25_status TEXT, scan_mode TEXT,
    interrupted INTEGER NOT NULL DEFAULT 0, search TEXT NOT NULL DEFAULT ''
);
"""


class TestTheTranscriptMigration(unittest.TestCase):
    """`_SCHEMA` is CREATE TABLE IF NOT EXISTS, which does nothing at all to a
    database that already exists. Without an explicit migration, adding a
    column works perfectly on a fresh install and breaks every query on an
    upgraded one -- the worst possible way round for a bug to be distributed,
    since it cannot be reproduced by anyone who installed after the change."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "history.db"
        self.clock = FakeClock()

    def _write_old_database(self):
        db = sqlite3.connect(self.path)
        db.executescript(_PRE_TRANSCRIPT_SCHEMA)
        db.execute(
            "INSERT INTO calls (scanner_id, started, ended, duration, label, mode, search)"
            " VALUES (?,?,?,?,?,?,?)",
            ("home", self.clock.now - 60, self.clock.now - 58, 2.0,
             "Engine 12", "analog", "engine 12 analog"),
        )
        db.commit()
        db.close()

    def _store(self):
        store = ReceiveHistory(path=str(self.path), clock=self.clock, retention_days=90)
        self.addCleanup(store.close)
        return store

    def test_an_existing_database_gains_the_columns(self):
        self._write_old_database()
        store = self._store()
        columns = {row["name"] for row in store._db.execute("PRAGMA table_info(calls)")}
        self.assertLessEqual({"transcript", "transcript_status", "clip"}, columns)

    def test_existing_rows_survive_and_stay_searchable(self):
        # The migration must not cost anyone their history, and the search
        # blob of a row written before the change still has to match.
        self._write_old_database()
        store = self._store()
        self.assertEqual([r["label"] for r in store.records()], ["Engine 12"])
        self.assertEqual([r["label"] for r in store.records(query="engine")], ["Engine 12"])
        self.assertIsNone(store.records()[0]["transcript"])

    def test_it_is_idempotent_across_restarts(self):
        # Runs on every open, so a second start must not fail on columns that
        # are already there.
        self._write_old_database()
        self._store().close()
        store = self._store()
        self.assertEqual(len(store.records()), 1)

    def test_a_migrated_row_can_be_given_a_transcript(self):
        self._write_old_database()
        store = self._store()
        call_id = store.records()[0]["id"]
        store.set_transcript(call_id, "structure fire on elm street", "ok")
        self.assertEqual(store.records()[0]["transcript"], "structure fire on elm street")


class TestTranscripts(TestReceiveHistory):
    def _one_call(self):
        self._record_one_call()
        return self.store.records()[0]

    def test_a_transcript_is_findable_from_the_existing_search_box(self):
        # The whole payoff. `search` is a denormalized blob, so a transcript
        # that never lands in it may as well not have been produced.
        call = self._one_call()
        self.store.set_transcript(call["id"], "structure fire on elm street", "ok")
        self.assertEqual(len(self.store.records(query="elm street")), 1)
        self.assertEqual(len(self.store.records(query="structure")), 1)

    def test_setting_a_transcript_keeps_the_row_findable_by_its_old_terms(self):
        # `search` is rewritten wholesale, so it has to be rebuilt from the
        # record rather than appended to -- otherwise transcribing a call
        # silently drops it out of every non-speech search.
        call = self._one_call()
        before = self.store.records(query="dispatch")
        self.assertEqual(len(before), 1)
        self.store.set_transcript(call["id"], "engine twelve responding", "ok")
        self.assertEqual(len(self.store.records(query="dispatch")), 1)

    def test_status_distinguishes_nothing_said_from_never_attempted(self):
        # Four outcomes render as an empty column otherwise: not attempted,
        # attempted and silent, declined as too doubtful, and the server
        # being unreachable. Only the first is "transcription is off".
        call = self._one_call()
        self.assertIsNone(self.store.records()[0]["transcript_status"])
        self.store.set_transcript(call["id"], "", "no-speech")
        self.assertEqual(self.store.records()[0]["transcript_status"], "no-speech")

    def test_it_notifies_listeners_so_a_late_transcript_can_be_pushed(self):
        # The row is already on screen by the time this lands -- the call
        # ended seconds or minutes ago -- so without an event the UI shows a
        # blank transcript until something else forces a refresh.
        seen = []
        self.store.add_listener(lambda event, record: seen.append((event, record)))
        call = self._one_call()
        self.store.set_transcript(call["id"], "medic four en route", "ok")
        self.assertEqual([e for e, _r in seen][-1], "transcript")
        self.assertEqual(seen[-1][1]["transcript"], "medic four en route")

    def test_a_transcript_survives_the_row_being_updated(self):
        # The bug this exists for: _to_row builds its values from _FIELDS
        # using record.get(), and the record a CallTracker carries has never
        # heard of the transcript columns. Listing them in _FIELDS made every
        # _update() write them back as NULL -- and _update runs on each poll
        # of an open call and once more when it ends, seconds *after* the
        # segmenter has released the transmission and its transcript has been
        # stored. Transcripts were being produced correctly and then wiped by
        # the row's own next update.
        call = self._one_call()
        self.store.set_transcript(call["id"], "structure fire on elm street", "ok",
                                  clip="1.wav")
        record = dict(call)
        record["duration"] = 9.0
        self.store._update(record)

        after = self.store.record(call["id"])
        self.assertEqual(after["transcript"], "structure fire on elm street")
        self.assertEqual(after["transcript_status"], "ok")
        self.assertEqual(after["clip"], "1.wav")

    def test_a_transcript_stays_searchable_after_the_row_is_updated(self):
        # The same bug once removed. `search` is rebuilt on every update from
        # a record with no transcript in it, so without preserving the stored
        # one the text stays in its column and silently stops being findable
        # -- which is worse than losing it, because nothing looks wrong.
        call = self._one_call()
        self.store.set_transcript(call["id"], "structure fire on elm street", "ok")
        record = dict(call)
        record["duration"] = 9.0
        self.store._update(record)

        self.assertEqual(len(self.store.records(query="elm street")), 1)
        self.assertEqual(len(self.store.records(query="dispatch")), 1)

    def test_a_rejection_does_not_erase_a_transcript_already_there(self):
        # One call can be covered by more than one piece of audio -- the
        # scanner dwells on a frequency and the transmission resumes, or the
        # detector closes on a pause -- and each arrives separately. Seen on
        # a real install as a row reading "too short to transcribe" and then
        # changing to "no speech", having had words in it moments earlier.
        call = self._one_call()
        self.store.set_transcript(call["id"], "engine twelve responding", "ok")
        for status in ("too-short", "no-speech", "doubtful", "no-audio"):
            with self.subTest(status=status):
                self.store.set_transcript(call["id"], "", status)
                after = self.store.record(call["id"])
                self.assertEqual(after["transcript"], "engine twelve responding")
                self.assertEqual(after["transcript_status"], "ok")

    def test_two_transcripts_for_one_call_accumulate(self):
        # They are different things that were said during it. Keeping only
        # the last would silently discard half the call.
        call = self._one_call()
        self.store.set_transcript(call["id"], "engine twelve responding", "ok")
        self.store.set_transcript(call["id"], "on scene", "ok")
        self.assertEqual(self.store.record(call["id"])["transcript"],
                         "engine twelve responding on scene")

    def test_both_halves_stay_searchable(self):
        call = self._one_call()
        self.store.set_transcript(call["id"], "structure fire", "ok")
        self.store.set_transcript(call["id"], "on elm street", "ok")
        self.assertEqual(len(self.store.records(query="structure")), 1)
        self.assertEqual(len(self.store.records(query="elm street")), 1)

    def test_a_status_still_moves_on_while_there_is_no_text(self):
        # Nothing to protect yet, so the newer fact wins -- pending gives way
        # to whatever the model concluded.
        call = self._one_call()
        self.store.set_transcript(call["id"], "", "pending")
        self.store.set_transcript(call["id"], "", "no-speech")
        self.assertEqual(self.store.record(call["id"])["transcript_status"], "no-speech")

    def test_a_call_in_progress_can_be_left_out_of_the_log(self):
        # The history page shows what happened; a call still running is a row
        # whose duration keeps changing and whose transcript cannot exist
        # yet. The live view is where that belongs, so it asks for finished
        # calls only -- and the count has to agree with the page.
        self._record_one_call()
        self.store.status_callback("home", self._status())  # still receiving

        self.assertEqual(len(self.store.records()), 2)
        self.assertEqual(len(self.store.records(finished=True)), 1)
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.count(finished=True), 1)

    def test_a_call_cut_off_by_a_restart_still_counts_as_finished(self):
        # Its end is filled in on reload, so "unfinished" means the one
        # happening now and nothing else.
        self.store.status_callback("home", self._status())
        self.clock.tick()
        reloaded = self._store()
        self.assertEqual(len(reloaded.records(finished=True)), 1)

    def test_calls_still_waiting_on_a_transcript_can_be_found(self):
        # For startup: the queue of audio is in memory, so a row still marked
        # as waiting when the add-on comes back is waiting on nothing.
        waiting = self._one_call()
        self.store.set_transcript(waiting["id"], "", "pending")
        self._record_one_call()
        done = self.store.records()[0]
        self.store.set_transcript(done["id"], "engine twelve", "ok")

        self.assertEqual(self.store.awaiting_transcripts("pending"), [waiting["id"]])
        self.assertEqual(self.store.awaiting_transcripts("dropped"), [])

    def test_a_transcript_for_a_pruned_call_is_dropped_not_raised(self):
        # Normal, not exceptional: retention can delete a row while its audio
        # is still sitting in the transcription queue.
        self.assertIsNone(self.store.set_transcript(9999, "anything", "ok"))

    def test_call_at_finds_the_row_a_segment_belongs_to(self):
        call = self._one_call()
        found = self.store.call_at("home", call["started"] + 1, call["started"] + 2)
        self.assertEqual(found["id"], call["id"])

    def test_call_at_tolerates_the_poll_loop_and_the_audio_disagreeing(self):
        # Measured on real hardware, the row's window and the audio's can
        # miss each other entirely -- an overlap test would match nothing.
        call = self._one_call()
        offset = call["started"] - 5.0
        self.assertIsNotNone(self.store.call_at("home", offset, offset + 1))

    def test_call_at_returns_nothing_when_there_is_no_call_nearby(self):
        self._one_call()
        self.assertIsNone(self.store.call_at("home", 1e9, 1e9 + 1))

    def test_call_at_does_not_match_another_scanner(self):
        call = self._one_call()
        self.assertIsNone(self.store.call_at("shop", call["started"], call["started"] + 1))


class TestWhatCountsAsHearingSomething(TestReceiveHistory):
    """A call is what the scanner *played*, not what it could hear.

    On a conventional channel those are the same event. On a trunked digital
    system they are not: the receiver has signal whenever the site is active,
    while the scanner unmutes only for a talkgroup it is monitoring. Measured
    on the real scanner, one minute held eight stretches of RF and a single
    unmute -- so a log built on the squelch is a record of the site rather
    than of this scanner, and everything downstream inherits that.

    The identity is the other half. It comes from GSI, which is slow, while
    the mute flag comes from the fast display poll -- and `last_status` is a
    merge, so every fast poll re-delivers the previous GSI block. Naming a
    call from that gives a new transmission the last one's talkgroup.
    """

    def _display(self, muted=True, at=None, **fields):
        """A fast display poll: the mute flag, and the GSI block merged in
        behind it exactly as ScannerConnection re-delivers it."""
        status = self._status(**fields)
        status["mute"] = "1" if muted else "0"
        if at is not None:
            status["gsi_at"] = at
        return status

    def test_rf_the_scanner_never_played_is_not_a_call(self):
        for _ in range(4):
            self.store.status_callback("home", self._display(muted=True))
            self.clock.tick(0.25)
        self.assertEqual(self.store.records(), [])

    def test_the_scanner_unmuting_is(self):
        self.store.status_callback("home", self._display(muted=False))
        self.clock.tick(0.25)
        self.assertEqual(len(self.store.records()), 1)

    def test_a_call_ends_when_the_scanner_mutes_again(self):
        self.store.status_callback("home", self._display(muted=False))
        self.clock.tick(0.25)
        for _ in range(history.IDLE_POLLS_TO_END):
            self.store.status_callback("home", self._display(muted=True))
            self.clock.tick(0.25)
        self.assertIsNotNone(self.store.records()[0]["ended"])
        self.assertEqual(self.store.open_calls(), [])

    def test_a_stale_gsi_the_screen_contradicts_does_not_name_the_call(self):
        # The same GSI block, re-delivered by a display poll: its talkgroup
        # belongs to whatever the scanner was doing before this transmission.
        # The screen says otherwise, and the screen is current.
        first = self._display(muted=False, at=1000.0,
                              TGID={"Name": "Dispatch", "TGID": "TGID:1001"})
        first["lines"] = [{"text": "TGID:1001"}]
        self.store.status_callback("home", first)
        self.clock.tick(0.25)
        for _ in range(history.IDLE_POLLS_TO_END + 1):
            self.store.status_callback("home", self._display(muted=True, at=1000.0))
            self.clock.tick(0.25)

        second = self._display(muted=False, at=1000.0,
                               TGID={"Name": "Dispatch", "TGID": "TGID:1001"})
        second["lines"] = [{"text": "TGID:2002"}]
        self.store.status_callback("home", second)
        opened = self.store.open_calls()[0]
        self.assertEqual(opened["tgid"], "TGID:2002")
        self.assertIsNone(opened["channel"])   # named by the next GSI, not this one

    def test_a_stale_gsi_the_screen_agrees_with_keeps_its_names(self):
        # The other half, and the one that matters for the history page: when
        # the screen and GSI describe the same call, throwing GSI away leaves
        # the row with no system, department or channel at all.
        poll = self._display(muted=False, at=1000.0,
                             TGID={"Name": "Dispatch", "TGID": "TGID:1001"})
        poll["lines"] = [{"text": "TGID:1001"}]
        self.store.status_callback("home", poll)
        self.clock.tick(0.25)
        self.store.status_callback("home", {**poll})   # same stamp: a fast poll

        opened = self.store.open_calls()[0]
        self.assertEqual(opened["channel"], "Dispatch")
        self.assertEqual(opened["department"], "Fire")
        self.assertEqual(opened["system"], "Countywide")

    def test_a_screen_with_nothing_on_it_leaves_gsi_alone(self):
        # A menu, a popup, the weather page. Nothing to contradict, so the
        # last thing the scanner told us stands -- which is what every version
        # before the fast poll did, and better than an empty row.
        poll = self._display(muted=False, at=1000.0)
        poll["lines"] = [{"text": "Menu"}, {"text": "Settings"}]
        self.store.status_callback("home", poll)
        self.clock.tick(0.25)
        self.store.status_callback("home", {**poll})

        self.assertEqual(self.store.open_calls()[0]["channel"], "Dispatch")

    def test_the_screen_names_the_call_the_moment_it_starts(self):
        # GSI is three seconds behind; the screen is not. A transmission
        # should not have to wait to be identified, and it should never be
        # identified as the one before it.
        stale = self._display(muted=False, at=1000.0,
                              TGID={"Name": "Dispatch", "TGID": "TGID:1001"})
        stale["lines"] = [{"text": "TGID:10852"},
                          {"text": "Sys ID: ---      857.937500MHz"}]
        self.store.status_callback("home", stale)   # first sight: fresh GSI
        self.clock.tick(1.0)
        for _ in range(history.IDLE_POLLS_TO_END + 1):
            self.store.status_callback("home", self._display(muted=True, at=1000.0))
            self.clock.tick(0.25)

        second = self._display(muted=False, at=1000.0)
        second["lines"] = [{"text": "TGID:20304"},
                           {"text": "Sys ID: ---      851.012500MHz"}]
        self.store.status_callback("home", second)
        opened = self.store.open_calls()[0]
        self.assertEqual(opened["tgid"], "TGID:20304")
        self.assertAlmostEqual(opened["frequency"], 851.0125)

    def test_a_fresh_gsi_names_the_call_it_arrives_during(self):
        self.store.status_callback("home", self._display(muted=False, at=1000.0))
        self.clock.tick(0.25)
        self.store.status_callback(
            "home", self._display(muted=False, at=1001.0,
                                  TGID={"Name": "Tactical", "TGID": "1002"}))
        self.assertEqual(self.store.open_calls()[0]["tgid"], "1002")

    def test_the_store_says_when_a_call_is_one_the_scanner_played(self):
        # What lets `require_audio` stop deleting rows it has no business
        # deleting: the question it asks was already answered before the row
        # was written.
        self.assertFalse(self.store.played("home"))
        self.store.status_callback("home", self._display(muted=False))
        self.assertTrue(self.store.played("home"))

    def test_a_scanner_answering_only_the_squelch_is_not_claimed_as_played(self):
        self.store.status_callback("home", self._status())
        self.assertFalse(self.store.played("home"))

    def test_a_scanner_that_has_not_answered_the_mute_flag_uses_the_squelch(self):
        # Older firmware, or the first polls after a restart. The squelch test
        # is the fallback, which is exactly what every earlier version did.
        self.store.status_callback("home", self._status())
        self.clock.tick()
        self.assertEqual(len(self.store.records()), 1)


class TestWhichCallsASegmentCovers(TestReceiveHistory):
    """`calls_at`, which decides who gets a transcript.

    Every row it returns is written with the segment's words, so this is not
    a search that can afford to be generous. Being too narrow loses the
    second half of a transmission the log split in two; being too wide files
    one transmission's words under its neighbours, which is a false record
    and was reported from the real install.

    The two are told apart by the gap between the rows, which is the log's
    own evidence about the squelch: an identity change is noticed on the next
    poll, while a call that actually ended took idle polls -- silence -- to
    confirm it.
    """

    def _transmission(self, seconds=6.0, **fields):
        """One call held open across several polls, then confirmed ended."""
        started = self.clock.now
        elapsed = 0.0
        while elapsed < seconds:
            self.store.status_callback("home", self._status(**fields))
            self.clock.tick()
            elapsed += 3.0
        for _ in range(history.IDLE_POLLS_TO_END):
            self.store.status_callback("home", self._status(receiving=False, **fields))
            self.clock.tick()
        return started

    def test_a_neighbouring_transmission_does_not_share_the_transcript(self):
        # The reported bug. Two separate calls, seconds apart on a busy
        # channel, and the audio of the second reaching back to the first.
        self._transmission(TGID={"Name": "Dispatch", "TGID": "1001"})
        second = self._transmission(TGID={"Name": "Tactical", "TGID": "1002"})

        covered = self.store.calls_at("home", second, second + 6.0)
        self.assertEqual([c["tgid"] for c in covered], ["1002"])

    def test_a_transmission_the_log_split_in_two_is_returned_whole(self):
        # The talkgroup changing mid-transmission ends the row without the
        # scanner ever muting: closed and reopened inside a single poll, so
        # the two rows are one poll apart. The audio did not split, so
        # neither should the transcript.
        started = self.clock.now
        for tgid in ("1001", "1001", "1002", "1002"):
            self.store.status_callback(
                "home", self._status(TGID={"Name": "Dispatch", "TGID": tgid}))
            self.clock.tick(protocol.STATUS_POLL_INTERVAL)
        for _ in range(history.IDLE_POLLS_TO_END):
            self.store.status_callback("home", self._status(receiving=False))
            self.clock.tick(protocol.STATUS_POLL_INTERVAL)

        covered = self.store.calls_at("home", started, started + 12.0)
        self.assertEqual([c["tgid"] for c in covered], ["1001", "1002"])

    def test_only_the_run_the_audio_belongs_to_comes_back(self):
        # Three transmissions in half a minute: the middle one split by an
        # identity change, the others separated by real silence.
        self._transmission(TGID={"Name": "A", "TGID": "1001"})
        middle = self.clock.now
        for tgid in ("2001", "2002"):
            self.store.status_callback(
                "home", self._status(TGID={"Name": "B", "TGID": tgid}))
            self.clock.tick(protocol.STATUS_POLL_INTERVAL)
        for _ in range(history.IDLE_POLLS_TO_END):
            self.store.status_callback("home", self._status(receiving=False))
            self.clock.tick(protocol.STATUS_POLL_INTERVAL)
        middle_ended = self.clock.now
        self._transmission(TGID={"Name": "C", "TGID": "3001"})

        covered = self.store.calls_at("home", middle, middle_ended)
        self.assertEqual([c["tgid"] for c in covered], ["2001", "2002"])

    def test_a_call_still_in_progress_is_matched_where_it_has_got_to(self):
        # An open row has no `ended` at all. Reading that as "it ended the
        # moment it began" would rank a long transmission behind a short one
        # that merely happened nearby.
        started = self.clock.now
        for _ in range(6):
            self.store.status_callback("home", self._status())
            self.clock.tick()

        covered = self.store.calls_at("home", started + 12.0, started + 15.0)
        self.assertEqual([c["started"] for c in covered], [started])

    def test_audio_with_no_call_anywhere_near_it_matches_nothing(self):
        self._transmission()
        self.assertEqual(self.store.calls_at("home", 1e9, 1e9 + 3), [])

    def test_the_reach_is_a_poll_rather_than_the_old_ten_seconds(self):
        # Pinning the number, because the contiguity rule alone does not
        # cover this case: audio that overlaps *no* row at all used to be
        # filed under whichever call came within ten seconds of it. Nine
        # seconds later is a different transmission by any reading, and
        # writing nothing is the safer half of the choice -- an unattached
        # transcript is lost, an attached one is false.
        started = self._transmission(seconds=3.0)
        self.assertEqual(
            [c["started"] for c in self.store.calls_at("home", started, started + 3)],
            [started])
        self.assertEqual(self.store.calls_at("home", started + 5.0, started + 8.0), [])



if __name__ == "__main__":
    unittest.main()
