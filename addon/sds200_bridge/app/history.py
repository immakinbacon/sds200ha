"""Receive history: what the scanner heard, and when.

The scanner has no "a call started" push -- it only answers polls -- so a
call is reconstructed from the poll stream. A call is a contiguous run of
polls in which the scanner was *playing* audio (`reception.audible`) on the
same identity (`reception.IDENTITY_FIELDS`); it ends when either of those
stops holding.

**Playing, not receiving**, and on a trunked system that is the whole
difference. The receiver has signal whenever the site is active, while the
scanner unmutes only for a talkgroup it is monitoring -- measured on real
hardware as eight separate stretches of RF in one minute against a single
unmute. A log built on the squelch is a record of the site rather than of
this scanner, and everything downstream inherits that: rows with nothing to
hear, audio that matches no row, transcripts landing on the neighbours. The
mute flag comes from the display poll at `STATUS_POLL_INTERVAL`, four times a
second. Where a scanner does not answer it -- older firmware, the first polls
after a restart -- the squelch test is the fallback and this reads as it did
before.

Three consequences worth stating plainly, because they are properties of the
hardware rather than things a future change can tune away:

* **A transmission shorter than the poll interval can be missed entirely.**
  This was severe while calls came from GSI at 3s: measured against a real
  timeline, where the median transmission is 0.6s, a 3s poll grid caught
  about 30% of them and missed the rest. At the display poll's quarter second
  it is a small effect, and timestamps are accurate to about that.
* **A call's end is confirmed, not observed.** A single lost poll looks
  identical to the scanner muting, so ending a call needs
  `IDLE_POLLS_TO_END` consecutive polls of silence. Otherwise one dropped
  reply mid-transmission would split one call into two history rows.
* **The identity arrives on a slower schedule than the audio does.** The
  names come from GSI at `GSI_POLL_INTERVAL`, and `last_status` is a merge,
  so a display poll re-delivers a GSI block describing whatever the scanner
  was doing up to three seconds ago. The screen settles it: it states the
  talkgroup and the frequency exactly, so a stale GSI block the screen agrees
  with is kept and one it contradicts is dropped -- see `status_callback` and
  `reception.from_display`.

Storage
-------

SQLite at `/data/history.db`, one row per call.

This used to be a `deque` of dicts snapshotted to `/data/history.json`, and
that was the right shape while the log held a few hundred calls: no schema,
no migrations, and a history file you could read with `cat`. It stopped
being the right shape once the log was meant to go back months. Every record
was resident in memory (~900 bytes each, measured, not the ~540 the JSON
suggests), and because a row is *mutated in place* while its call is open,
`save()` had to rewrite the entire file -- so a busy scanner rewrote the
whole history every `SAVE_DEBOUNCE_S`. At 90 days of a busy trunked system
that is a several-hundred-megabyte file written every ten seconds, which is
how you destroy an SD card.

SQLite fixes all three at once, and costs no new dependency (`sqlite3` is
stdlib, and Home Assistant's own recorder sets the precedent): writes are
per-call rather than per-corpus, memory is bounded by the page being looked
at rather than by the log, and filtering happens in the query.

The old `/data/history.json` is deliberately *not* imported -- a few hundred
rows of a superseded format were not worth the migration path. It is left
where it is rather than deleted, and can be removed by hand.

Retention is by **time first** -- `retention_days`, the thing someone
actually means by "keep six months" -- with `max_records` as a disk backstop
behind it. Pruning is a range delete over the `started` index, run at
startup and at most every `PRUNE_INTERVAL_S` thereafter.

The `search` column is a denormalized lowercase blob of everything a person
might type, including the call's local date and time in two formats, so that
"2026-08-06", "14:3" and "06 Aug" all match. It is a `LIKE '%x%'` scan by
design: arbitrary substring matching is what the search box has always done,
and FTS5 would quietly change that to token-prefix matching. Measured on
500k rows (~330 bytes each, a 163MB file), the worst case -- a search that
matches nothing, so `LIMIT` never short-circuits it -- is under 100ms, and
combining it with a date range brings it back under a millisecond because
the range *is* indexed. FTS5 is the trade to revisit only if that stops
being true.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time

import reception
from protocol import GSI_POLL_INTERVAL, STATUS_POLL_INTERVAL

logger = logging.getLogger(__name__)

HISTORY_PATH = "/data/history.db"

# Consecutive polls with the scanner muted before a call is considered over.
# Two, not one: one covers a single lost display poll (measured at about one
# in twenty), while still ending the call within half a second of the scanner
# going quiet -- which matters, because on this traffic the gap between two
# separate transmissions is routinely a second or less.
IDLE_POLLS_TO_END = 2

DAY_S = 86400.0

# How often an open call's row is rewritten while it runs. See status_callback:
# the poll loop is faster than the storage deserves.
OPEN_WRITE_INTERVAL_S = 1.0

# How far either side of a detected voice segment to look for the call row it
# belongs to. Generous on purpose: the row's timestamps come from the GSI
# poll loop, which notices a call up to GSI_POLL_INTERVAL late and holds it
# open IDLE_POLLS_TO_END afterwards, while the segment's come from the audio.
# On real hardware those disagree by seconds as a matter of course.
MATCH_SLACK_S = 10.0

# How far outside a segment a row may sit and still be treated as part of the
# same transmission -- which is a different and much stricter question than
# MATCH_SLACK_S's "could this audio belong to this call at all".
#
# Sized to the poll loop rather than to clock disagreement, because a row is
# a *shrunken* view of its own transmission: `started` is the first poll that
# saw the call and `ended` the last, so the row sits inside the audio, late
# at the front and early at the back, by up to one poll each.
#
# It was MATCH_SLACK_S, and that is the bug this replaces. A segment shares
# its transcript with every row it covers, so a ten-second reach meant that
# on a channel with traffic every few seconds one transmission's words were
# written onto the transmissions either side of it -- reported from the real
# install as transcripts appearing on calls they had nothing to do with.
COVER_SLACK_S = GSI_POLL_INTERVAL

# Two rows closer together than this were one transmission that the log cut
# in half, not two transmissions. A call ends either because the identity
# changed -- closed and reopened inside a single poll, so the rows are one
# poll apart -- or because the scanner muted, which takes IDLE_POLLS_TO_END
# polls of silence first. So the gap between two rows says which happened,
# and only the first kind may share a transcript.
#
# Measured against the *display* poll rather than GSI, which is what made
# this safe to tighten from four and a half seconds to under half of one. It
# had to be that loose while a call's edges came from a three-second poll;
# now they come from a quarter-second one, and four and a half seconds is
# wider than the gap between two genuinely separate transmissions on this
# traffic -- which is how one transmission's words end up on the next one's
# row. A lost poll still stretches "the very next poll" to two, which is why
# this is a poll and a half rather than exactly one.
CONTIGUOUS_GAP_S = 1.5 * STATUS_POLL_INTERVAL

# Keeping three months by default: long enough that "what was that call last
# month" is answerable, short enough that the default install never grows a
# database worth worrying about.
DEFAULT_RETENTION_DAYS = 90

# The backstop behind the retention window, not the primary control. It only
# bites if a scanner is busy enough to produce this many calls *inside* the
# window, at which point something has to give and dropping the oldest is
# the least surprising thing to drop.
DEFAULT_MAX_RECORDS = 500_000

# Deleting expired rows is bookkeeping, not something a call should wait on.
PRUNE_INTERVAL_S = 300.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id           INTEGER PRIMARY KEY,
    scanner_id   TEXT    NOT NULL,
    started      REAL    NOT NULL,
    ended        REAL,
    duration     REAL    NOT NULL DEFAULT 0,
    polls        INTEGER NOT NULL DEFAULT 0,
    label        TEXT,
    identity     TEXT,
    unit_ids     TEXT    NOT NULL DEFAULT '[]',
    rssi_peak    REAL,
    signal_peak  INTEGER,
    system       TEXT,
    system_type  TEXT,
    department   TEXT,
    site         TEXT,
    channel      TEXT,
    frequency    REAL,
    mod          TEXT,
    mode         TEXT,
    tgid         TEXT,
    sub_audio    TEXT,
    service_type TEXT,
    p25_status   TEXT,
    scan_mode    TEXT,
    interrupted  INTEGER NOT NULL DEFAULT 0,
    transcript   TEXT,
    transcript_status TEXT,
    clip         TEXT,
    search       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS calls_started ON calls(started DESC);
CREATE INDEX IF NOT EXISTS calls_scanner ON calls(scanner_id, started DESC);
CREATE INDEX IF NOT EXISTS calls_mode ON calls(mode, started DESC);
"""

# Every column of a record except `id` (the row's own) and `search` (derived
# on write). Order is the insert/update order.
_FIELDS = (
    "scanner_id", "started", "ended", "duration", "polls", "label", "identity",
    "unit_ids", "rssi_peak", "signal_peak", "system", "system_type", "department",
    "site", "channel", "frequency", "mod", "mode", "tgid", "sub_audio",
    "service_type", "p25_status", "scan_mode", "interrupted",
)

# Written only by set_transcript, and deliberately NOT in _FIELDS.
#
# _to_row builds its values with record.get(field) over _FIELDS, and the
# record a CallTracker carries has never heard of these columns -- so listing
# them there made every _update() write them back as NULL. _update runs on
# each poll of an open call and once more when it ends, which is several
# seconds *after* the voice segmenter has released the transmission and its
# transcript has already been stored. The transcript was being written and
# then wiped by the row's own next update, leaving a blank status that then
# got marked "no audio". Transcripts were being produced correctly the whole
# time and destroyed on the way out.
_TRANSCRIPT_FIELDS = ("transcript", "transcript_status", "clip")

# Columns added after the first release. `_SCHEMA` is CREATE TABLE IF NOT
# EXISTS, so it does nothing to a database that already exists -- without
# this, an upgraded install keeps its old table and every query naming a new
# column fails. Each is applied only if absent, so this is safe to re-run and
# safe to extend.
_ADDED_COLUMNS = (
    ("transcript", "TEXT"),
    ("transcript_status", "TEXT"),
    ("clip", "TEXT"),
)


def _now() -> float:
    return time.time()


class CallTracker:
    """Per-scanner state machine turning a stream of snapshots into calls.

    Kept separate from the store so it can be unit-tested by feeding it
    snapshots directly, with no event loop, file or clock involved.
    """

    def __init__(self, scanner_id: str, clock=_now):
        self.scanner_id = scanner_id
        self._clock = clock
        self._open: dict | None = None
        self._idle_polls = 0

    def update(self, snapshot: dict) -> tuple[dict | None, dict | None]:
        """Feed one poll's snapshot in.

        Returns `(started, ended)` -- either or both may be None. Both are
        non-None in the one poll where a call ends and a different one is
        already in progress, which is the normal case on a busy trunked
        system where the scanner moves straight from one talkgroup to the
        next.
        """
        now = self._clock()
        if not snapshot.get("receiving"):
            return None, self._maybe_close(now)

        # An open call whose identity changed is a *different* transmission,
        # even though the squelch never closed. Close it immediately (no idle
        # grace: the identity changing is positive evidence, not an absence
        # of evidence) and open the new one in the same poll.
        #
        # "Changed" is judged field by field, because a blank is the scanner
        # not having said yet rather than a different answer -- see
        # `reception.identity_conflict`, and `_extend` below, which has always
        # treated the first poll of a call as the unreliable one.
        if self._open is not None and reception.identity_conflict(self._open, snapshot):
            ended = self._close(now)
            return self._start(snapshot, now), ended

        if self._open is None:
            return self._start(snapshot, now), None

        self._idle_polls = 0
        self._extend(snapshot, now)
        return None, None

    def flush(self) -> dict | None:
        """Close any open call, e.g. because the scanner is being stopped."""
        return self._close(self._clock()) if self._open else None

    @property
    def open_call(self) -> dict | None:
        return self._open

    def _start(self, snapshot: dict, now: float) -> dict:
        record = {
            "scanner_id": self.scanner_id,
            "started": now,
            "ended": None,
            "duration": 0.0,
            "polls": 1,
            "label": reception.describe(snapshot),
            "identity": snapshot["identity"],
            "unit_ids": [snapshot["unit_id"]] if snapshot.get("unit_id") else [],
            "rssi_peak": snapshot.get("rssi"),
            "signal_peak": snapshot.get("signal"),
        }
        for field in (
            "system", "system_type", "department", "site", "channel", "frequency",
            "mod", "mode", "tgid", "sub_audio", "service_type", "p25_status", "scan_mode",
        ):
            record[field] = snapshot.get(field)
        self._open = record
        self._idle_polls = 0
        return record

    def _extend(self, snapshot: dict, now: float) -> None:
        record = self._open
        assert record is not None
        record["polls"] += 1
        record["duration"] = round(now - record["started"], 1)
        unit_id = snapshot.get("unit_id")
        if unit_id and unit_id not in record["unit_ids"]:
            record["unit_ids"].append(unit_id)
        for key, peak in (("rssi", "rssi_peak"), ("signal", "signal_peak")):
            value = snapshot.get(key)
            if value is not None and (record[peak] is None or value > record[peak]):
                record[peak] = value
        # Fields the scanner fills in late: a trunked call often reports the
        # talkgroup a poll or two after the squelch opens, and the very first
        # poll of a call can carry a stale channel name. Fill blanks in, but
        # never overwrite a value already recorded -- the first non-empty
        # reading is the one that belongs to this call.
        for field in ("tgid", "channel", "department", "system", "sub_audio", "frequency"):
            if not record.get(field) and snapshot.get(field):
                record[field] = snapshot[field]
                record["label"] = reception.describe(record)
                # And the identity with it, or the row keeps describing the
                # call by what was known in the first poll -- which is the
                # poll this whole paragraph exists because we cannot trust.
                record["identity"] = reception.identity(record)

    def _maybe_close(self, now: float) -> dict | None:
        if self._open is None:
            return None
        self._idle_polls += 1
        if self._idle_polls < IDLE_POLLS_TO_END:
            return None
        return self._close(now)

    def _close(self, now: float) -> dict | None:
        record = self._open
        if record is None:
            return None
        self._open = None
        self._idle_polls = 0
        # Not `now`: that includes the idle polls spent confirming the end,
        # during which the scanner was no longer receiving. The last poll
        # that *did* see the call is the honest end, and `duration` was
        # already set from it in _extend.
        record["ended"] = record["started"] + record["duration"]
        return record


class ReceiveHistory:
    """The store: one CallTracker per scanner, over a SQLite-backed log.

    `on_call` is called as `(event, record)` where event is "start" or "end",
    and is what the trigger engine and the web UI's live feed hang off.
    """

    def __init__(
        self,
        path: str = HISTORY_PATH,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        clock=_now,
    ):
        self.path = path
        self.max_records = max_records
        self.retention_days = retention_days
        self._clock = clock
        self._trackers: dict[str, CallTracker] = {}
        # The GSI stamp each scanner was last named from -- see status_callback.
        self._last_gsi_at: dict[str, float] = {}
        # When each scanner's open row was last rewritten. See status_callback.
        self._last_open_write: dict[str, float] = {}
        # Which scanners' calls were decided by the mute flag. See `played`.
        self._gated: dict[str, bool] = {}
        self._listeners: list = []
        self._enabled = True
        self._last_prune = 0.0
        self._closed = False
        self._db = self._connect(path)
        self._close_interrupted()
        self._clear_pending()
        self.prune(force=True)

    # -- setup ----------------------------------------------------------

    @staticmethod
    def _open(path: str) -> sqlite3.Connection:
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        # WAL so a long read (the history tab paging through months) never
        # blocks the poll loop's writes, and NORMAL so committing a call
        # doesn't fsync -- losing the last few seconds of a log on a hard
        # power cut is an acceptable trade for not writing to the disk
        # synchronously every time the squelch closes.
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript(_SCHEMA)
        _migrate(db)
        return db

    def _connect(self, path: str) -> sqlite3.Connection:
        """Open the log, or fail soft.

        Same philosophy as the JSON store it replaces: this is a log, not
        configuration. A history that cannot be opened is worth a loud
        warning and an empty start, never a refusal to run the add-on.
        """
        try:
            return self._open(path)
        except (sqlite3.Error, OSError):
            logger.warning(
                "could not open the receive history at %s; moving it aside and "
                "starting a new one", path, exc_info=True,
            )
        try:
            if os.path.exists(path):
                os.replace(path, path + ".corrupt")
            return self._open(path)
        except (sqlite3.Error, OSError):
            logger.error(
                "could not create a receive history at %s; keeping it in memory "
                "for this run only, so it will be lost on restart", path, exc_info=True,
            )
        return self._open(":memory:")

    def _clear_pending(self) -> None:
        """Drop any "transcribing" left over from a previous run.

        The transcription queue lives in memory, so a restart loses whatever
        was in it. Left alone those rows would read as work in progress for
        as long as the database survives, which is the one thing they are
        certainly not.
        """
        with self._db:
            self._db.execute(
                "UPDATE calls SET transcript_status = NULL "
                "WHERE transcript_status = 'pending'"
            )

    def _close_interrupted(self) -> None:
        """Close calls that were still open when the add-on stopped.

        The open row is kept current poll by poll, so a call cut short by a
        restart is in the log with what was known at the time -- but nothing
        is ever going to close it. Close it at its last recorded duration and
        say why, otherwise it renders as a transmission running for days.
        """
        with self._db:
            self._db.execute(
                "UPDATE calls SET ended = started + duration, interrupted = 1 "
                "WHERE ended IS NULL"
            )

    # -- recording ------------------------------------------------------

    def add_listener(self, callback) -> None:
        """callback(event: str, record: dict) -> None; "start" then "end"."""
        self._listeners.append(callback)

    def configure(self, *, enabled: bool, max_records: int, retention_days: int) -> None:
        """Apply settings changes in place (the store outlives a save)."""
        self._enabled = enabled
        self.max_records = max_records
        self.retention_days = retention_days
        self.prune(force=True)

    def status_callback(self, scanner_id: str, status: dict) -> None:
        """Plugged into `ScannerConnection.add_status_listener`.

        A call is a run of polls in which the scanner was *playing* audio --
        see `reception.audible`, and note that this is a different question
        from whether the squelch was open. Every poll of the fast display loop
        answers it, so calls are tracked at that rate rather than at GSI's,
        which is what makes their edges sharp enough to cut audio on.

        The identity comes from GSI, which is slower, and the two must not be
        confused. `last_status` is a merge, so a fast poll re-delivers the
        previous GSI block -- three seconds old, and belonging to whatever the
        scanner was doing *before* this transmission. Naming a call from that
        is how a new transmission inherits the last one's talkgroup. So a poll
        that carries no fresh GSI carries no identity either: the call opens
        unnamed and `_extend` fills the fields in from the first GSI that
        arrives during it, which is the same path a late-arriving talkgroup
        has always taken.
        """
        if not self._enabled:
            return
        snapshot = reception.extract(status)
        if not snapshot["muted_known"] and "gsi" not in status:
            # A display poll from a scanner that has not answered anything
            # else yet says nothing about reception either way.
            return
        stamp = status.get("gsi_at")
        if stamp is not None and stamp == self._last_gsi_at.get(scanner_id):
            # The same GSI block this scanner was named from last time, being
            # re-delivered by a display poll. No stamp at all means a caller
            # handed us a GSI reading directly, which is as fresh as it gets.
            snapshot = _named_by_the_screen(snapshot, reception.from_display(status))
        elif stamp is not None:
            self._last_gsi_at[scanner_id] = stamp
        snapshot["receiving"] = reception.audible(snapshot)
        self._gated[scanner_id] = bool(snapshot.get("muted_known"))
        tracker = self._tracker(scanner_id)
        started, ended = tracker.update(snapshot)
        # Emit the end first: a scanner moving straight from one call to the
        # next produces both in the same poll, and "call ended, call started"
        # is the order they happened in.
        if ended is not None:
            self._finish(ended)
        if started is not None:
            self._begin(started)
        elif tracker.open_call is not None:
            # A call in progress is rewritten as it runs rather than written
            # once at the end, so a call interrupted by a restart keeps the
            # duration and the late-arriving talkgroup that were known when
            # the power went.
            #
            # Not on every poll, though. The display loop now runs four times
            # a second, and rewriting an open row at that rate would turn one
            # transmission into a couple of dozen writes -- on hardware whose
            # storage is usually an SD card, and for a row that is going to be
            # written correctly at the end anyway. Once a second keeps what
            # the rewriting is for and costs a quarter of what it used to.
            now = self._clock()
            if now - self._last_open_write.get(scanner_id, 0.0) >= OPEN_WRITE_INTERVAL_S:
                self._last_open_write[scanner_id] = now
                self._update(tracker.open_call)

    def scanner_removed(self, scanner_id: str) -> None:
        tracker = self._trackers.pop(scanner_id, None)
        if tracker is None:
            return
        ended = tracker.flush()
        if ended is not None:
            self._finish(ended)

    def close(self) -> None:
        """Close open calls and the database, e.g. on shutdown.

        Idempotent: a shutdown path that runs twice (a signal handler and a
        `finally`, say) should not raise on the second pass.
        """
        if self._closed:
            return
        self._closed = True
        for scanner_id in list(self._trackers):
            self.scanner_removed(scanner_id)
        self._db.close()

    def _tracker(self, scanner_id: str) -> CallTracker:
        tracker = self._trackers.get(scanner_id)
        if tracker is None:
            tracker = CallTracker(scanner_id, clock=self._clock)
            self._trackers[scanner_id] = tracker
        return tracker

    def _begin(self, record: dict) -> None:
        record["id"] = self._insert(record)
        self._notify("start", record)

    def _finish(self, record: dict) -> None:
        self._update(record)
        self._notify("end", record)
        self.prune()

    def _notify(self, event: str, record: dict) -> None:
        for callback in self._listeners:
            try:
                callback(event, record)
            except Exception:
                logger.exception("a receive-history listener failed")

    def _insert(self, record: dict) -> int:
        columns = list(_FIELDS) + ["search"]
        sql = (
            f"INSERT INTO calls ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' * len(columns))})"
        )
        with self._db:
            cursor = self._db.execute(sql, _to_row(record) + (_searchable(record),))
        return cursor.lastrowid

    def _update(self, record: dict) -> None:
        if record.get("id") is None:
            return
        assignments = ", ".join(f"{field} = ?" for field in _FIELDS)
        # The stored transcript is appended in SQL rather than rebuilt from
        # the record, because the record here is the CallTracker's and has no
        # transcript in it -- recomputing `search` from it alone would drop an
        # already-stored transcript out of the search blob on the next poll,
        # leaving it in its column but unfindable. Done in the statement to
        # avoid a read before every write on a path that runs once per poll
        # per open call.
        assignments += ", search = ? || ' ' || LOWER(COALESCE(transcript, ''))"
        values = _to_row(record) + (_searchable(record), record["id"])
        with self._db:
            self._db.execute(f"UPDATE calls SET {assignments} WHERE id = ?", values)

    def set_transcript(self, call_id: int, text: str, status: str,
                       clip: str | None = None) -> dict | None:
        """Attach a transcript to a call that has already been written.

        Separate from `_update` because it arrives on a different schedule:
        the call row is finished when the squelch closes, and the transcript
        lands seconds to minutes later, once a queue has drained and a model
        somewhere else has had its turn. By then `_update`'s caller -- the
        CallTracker -- has long forgotten the record.

        `status` says what happened when there is no text, so a call nothing
        was said in is distinguishable from one that was never attempted, one
        the model declined, and one where the server was unreachable. Without
        that, every one of those looks like an empty column.

        Rewrites `search` too, which is what makes the transcript findable
        from the existing search box -- it is a denormalized blob, so a
        transcript that never lands in it may as well not exist.

        Returns the updated record so a caller can push it to listeners, or
        None if the row has gone (pruned or cleared while the clip was in
        the queue, which is normal rather than exceptional).
        """
        row = self._db.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        if row is None:
            return None
        record = _to_record(row)
        existing = (record.get("transcript") or "").strip()
        text = (text or "").strip()

        # One call can be covered by more than one piece of audio -- the
        # scanner dwells on a frequency and the transmission resumes, or the
        # detector closes on a pause -- and each arrives here separately.
        #
        # Whichever lands last must not simply win. Text beats no text, so a
        # rejection cannot erase a transcript that was already produced, and
        # two transcripts for one call accumulate rather than replace: they
        # are different things that were said during it, and keeping only the
        # last would silently discard half the call.
        if existing and text:
            text = f"{existing} {text}"
        elif existing and not text:
            # A later fragment had nothing in it. That is a fact about the
            # fragment, not about the call, and the call already has words.
            return record

        record["transcript"] = text
        record["transcript_status"] = status if text or not existing else record[
            "transcript_status"
        ]
        status = record["transcript_status"]
        if clip is not None:
            record["clip"] = clip
        with self._db:
            self._db.execute(
                "UPDATE calls SET transcript = ?, transcript_status = ?, clip = ?, "
                "search = ? WHERE id = ?",
                (text, status, record.get("clip"), _searchable(record), call_id),
            )
        self._notify("transcript", record)
        return record

    def drop(self, call_id: int) -> dict | None:
        """Delete a call, and tell listeners it has gone.

        For a scanner set to log only what it actually played. On a trunked
        system the squelch-open test reports a call whenever the site has RF,
        while the scanner unmutes only for a talkgroup it is monitoring -- so
        a row can be written for traffic that was never anyone's business to
        hear. Deleting is the honest end state for those, and it happens at
        the call's end, once whether any audio arrived is settled.
        """
        record = self.record(call_id)
        if record is None:
            return None
        with self._db:
            self._db.execute("DELETE FROM calls WHERE id = ?", (call_id,))
        self._notify("dropped", record)
        return record

    def record(self, call_id: int) -> dict | None:
        """One call by id, or None if it has been pruned."""
        row = self._db.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return _to_record(row) if row is not None else None

    def calls_at(self, scanner_id: str, start: float, end: float,
                 limit: int = 4) -> list[dict]:
        """The run of calls this segment of audio covers, oldest first.

        One continuous transmission can become several rows: a call is ended
        when the *identity* changes, so a talkgroup or department shifting
        mid-transmission splits it, and a dropped GSI read can too. The audio
        does not split -- the squelch never closed -- so a single segment
        legitimately covers all of them, and matching only the nearest left
        the others looking like calls nobody said anything in.

        But "covers" has to mean covers. Every row this returns is given the
        segment's transcript, so reaching too far does not merely lose the
        thread -- it writes one transmission's words onto its neighbours,
        which is what a ten-second reach did on a busy channel. So: the row
        the audio genuinely fits best, plus only those rows *contiguous* with
        it, which is the log's own evidence that the squelch never closed
        between them (see CONTIGUOUS_GAP_S).
        """
        rows = self._db.execute(
            "SELECT * FROM calls WHERE scanner_id = ? "
            "AND started <= ? AND (ended IS NULL OR ended >= ?) "
            "ORDER BY started LIMIT ?",
            (scanner_id, end + COVER_SLACK_S, start - COVER_SLACK_S, limit + 4),
        ).fetchall()
        if not rows:
            return []
        records = [_to_record(row) for row in rows]

        # Best overlap wins, and nearest start settles a tie -- which is what
        # decides it for the rows of one poll-quantized call, where every
        # overlap is the same.
        anchor = max(
            range(len(records)),
            key=lambda i: (_overlap(records[i], start, end),
                           -abs(records[i]["started"] - start)),
        )
        keep = [anchor]
        for step in (-1, 1):
            index = anchor + step
            while 0 <= index < len(records):
                earlier, later = sorted((index, index - step))
                if records[later]["started"] - _ended(records[earlier]) > CONTIGUOUS_GAP_S:
                    break
                keep.append(index)
                index += step
        return [records[i] for i in sorted(keep)][:limit]

    def call_at(self, scanner_id: str, start: float, end: float) -> dict | None:
        """The call overlapping [start, end], if there is one.

        How a segment of audio finds the row it belongs to. Deliberately
        loose about the boundaries, because the two are measured by
        completely different means: the row's times come from a 3s poll loop
        that notices a call late and lets go of it later still, while the
        segment's come from the audio itself. Measured against real hardware
        they disagree by seconds routinely, and in three of six minutes
        sampled the audio fell entirely outside the row's window -- so an
        overlap test would have matched nothing. Nearest start wins instead.
        """
        rows = self._db.execute(
            "SELECT * FROM calls WHERE scanner_id = ? AND started BETWEEN ? AND ? "
            "ORDER BY started",
            (scanner_id, start - MATCH_SLACK_S, end + MATCH_SLACK_S),
        ).fetchall()
        if not rows:
            return None
        return _to_record(min(rows, key=lambda r: abs(r["started"] - start)))

    # -- reading --------------------------------------------------------

    def records(
        self,
        *,
        scanner_id: str | None = None,
        query: str = "",
        mode: str = "",
        limit: int = 200,
        since: float | None = None,
        until: float | None = None,
        finished: bool = False,
    ) -> list[dict]:
        """Newest first, optionally filtered.

        `query` is a case-insensitive substring test across the text fields a
        person would search by, including the call's local date and time.
        `since`/`until` are epoch seconds, inclusive, matched against when
        the call *started*.

        `finished` drops the call that is happening *now*. A row is written
        when a call starts and rewritten as it runs, which is what lets a
        live view show one in progress -- but in a log it is a row whose
        duration keeps changing and whose transcript cannot exist yet, above
        rows that are finished and true. Off by default, so the live view
        keeps what it was built for.
        """
        where, params = _where(scanner_id, query, mode, since, until, finished)
        params.append(max(1, int(limit)))
        rows = self._db.execute(
            f"SELECT * FROM calls {where} ORDER BY started DESC, id DESC LIMIT ?", params
        ).fetchall()
        return [_to_record(row) for row in rows]

    def count(
        self,
        *,
        scanner_id: str | None = None,
        query: str = "",
        mode: str = "",
        since: float | None = None,
        until: float | None = None,
        finished: bool = False,
    ) -> int:
        """How many calls match, ignoring any page limit.

        Worth having now that the log is longer than one page by orders of
        magnitude: "200 of 148,392" is a different message from "200". Takes
        the same `finished` as `records`, or the total counts a row the page
        deliberately does not show.
        """
        where, params = _where(scanner_id, query, mode, since, until, finished)
        row = self._db.execute(f"SELECT COUNT(*) AS n FROM calls {where}", params).fetchone()
        return int(row["n"])

    def span(self) -> tuple[float | None, float | None]:
        """(oldest, newest) start times, or (None, None) when empty."""
        row = self._db.execute(
            "SELECT MIN(started) AS oldest, MAX(started) AS newest FROM calls"
        ).fetchone()
        return row["oldest"], row["newest"]

    def awaiting_transcripts(self, status: str, limit: int = 5_000) -> list[int]:
        """Ids of calls left waiting on a transcript that is not coming.

        For startup. The queue of audio does not survive a restart, so a row
        still marked as waiting when the add-on comes back is waiting on work
        that no longer exists anywhere -- and would go on saying so for as
        long as the row is kept. The caller owns the vocabulary of statuses,
        so it says which one it means rather than this having an opinion.
        """
        rows = self._db.execute(
            "SELECT id FROM calls WHERE transcript_status = ? LIMIT ?",
            (status, limit),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def played(self, scanner_id: str) -> bool:
        """Whether this scanner's calls are the ones it actually played.

        True once the mute flag is answering, which is what a call is built
        from then: the scanner unmuted, so there was audio, whatever our own
        tap did or did not manage to hear of it.
        """
        return self._gated.get(scanner_id, False)

    def open_calls(self) -> list[dict]:
        return [t.open_call for t in self._trackers.values() if t.open_call is not None]

    # -- retention ------------------------------------------------------

    def clip_names(self, scanner_id: str | None = None) -> list[str]:
        """The kept clips belonging to a scope, for a caller about to delete it.

        Read *before* `clear`, which is the only moment the names are still
        knowable: the store holds the names and the ClipStore holds the
        bytes, and neither knows the other exists. Deleting the rows without
        this leaves the audio behind, and since a clip is named after its row
        id -- which SQLite reissues from 1 once the table is empty -- what it
        leaves behind is a directory of files that shadow the calls recorded
        after them.
        """
        clauses = ["clip IS NOT NULL", "clip != ''"]
        params: list = []
        if scanner_id:
            clauses.append("scanner_id = ?")
            params.append(scanner_id)
        rows = self._db.execute(
            f"SELECT DISTINCT clip FROM calls WHERE {' AND '.join(clauses)}", params
        ).fetchall()
        return [row["clip"] for row in rows]

    def existing_ids(self, ids) -> set[int]:
        """Which of `ids` still have a row. For spotting orphaned clips."""
        ids = [int(i) for i in ids]
        if not ids:
            return set()
        placeholders = ", ".join("?" * len(ids))
        rows = self._db.execute(
            f"SELECT id FROM calls WHERE id IN ({placeholders})", ids
        ).fetchall()
        return {int(row["id"]) for row in rows}

    def clear(self, scanner_id: str | None = None) -> int:
        """Drop every record, or just one scanner's. Returns how many went.

        The audio is not this store's to delete -- see `clip_names`, which a
        caller doing this is expected to read first.
        """
        with self._db:
            if scanner_id:
                cursor = self._db.execute(
                    "DELETE FROM calls WHERE scanner_id = ?", (scanner_id,)
                )
            else:
                cursor = self._db.execute("DELETE FROM calls")
        return cursor.rowcount

    def prune(self, *, force: bool = False) -> int:
        """Drop what has fallen outside the retention window or the cap."""
        now = self._clock()
        if not force and now - self._last_prune < PRUNE_INTERVAL_S:
            return 0
        self._last_prune = now
        removed = 0
        with self._db:
            if self.retention_days:
                # `ended IS NOT NULL` so a call in progress is never deleted
                # out from under the tracker still writing to it.
                cursor = self._db.execute(
                    "DELETE FROM calls WHERE ended IS NOT NULL AND started < ?",
                    (now - self.retention_days * DAY_S,),
                )
                removed += cursor.rowcount
            if self.max_records and self.max_records > 0:
                # Find the cap-th newest row's timestamp and delete below it:
                # one indexed seek plus a range delete, rather than a
                # `NOT IN (SELECT ... LIMIT)` that walks the whole table.
                boundary = self._db.execute(
                    "SELECT started FROM calls ORDER BY started DESC, id DESC "
                    "LIMIT 1 OFFSET ?",
                    (self.max_records - 1,),
                ).fetchone()
                if boundary is not None:
                    cursor = self._db.execute(
                        "DELETE FROM calls WHERE started < ?", (boundary["started"],)
                    )
                    removed += cursor.rowcount
        if removed:
            logger.debug("pruned %d call(s) from the receive history", removed)
        return removed


def _named_by_the_screen(snapshot: dict, seen: dict) -> dict:
    """Reconcile a GSI block that may be stale with what the screen says now.

    GSI is polled every three seconds and the display four times a second, so
    a display poll re-delivers a GSI block describing whatever the scanner was
    doing up to three seconds ago -- possibly the *previous* transmission.
    Believing it names a new call after the old one; discarding it leaves the
    row with no system, department or channel at all, which is what 0.7.54
    did and what emptied the history page.

    The screen settles it. It states the talkgroup and the frequency exactly
    (`reception.from_display`), so:

    * screen and GSI agree, or the screen offers nothing to disagree with:
      the GSI block describes this call. Keep all of it, names included.
    * they differ: the GSI block belongs to something else. Drop the names --
      they will be filled in by the next GSI, which `_extend` has always done
      -- and take the screen's own fields, which are current by definition.

    The screen's names are deliberately not used either way: they are cut to
    the display width, so they are a different string from GSI's for the same
    call, and two spellings of one talkgroup read as the talkgroup changing.
    """
    contradicted = any(
        seen.get(field) is not None
        and snapshot.get(field) is not None
        and seen[field] != snapshot[field]
        for field in ("tgid", "frequency")
    )
    if not contradicted:
        return snapshot
    snapshot = {**snapshot, **dict.fromkeys(reception.IDENTITY_FIELDS)}
    snapshot.update(seen)
    snapshot["identity"] = reception.identity(snapshot)
    return snapshot


def _ended(record: dict) -> float:
    """When a call last had audio behind it.

    An open call has no `ended` yet, and reading that as "ends the moment it
    started" would make every long transmission look like a point in time --
    so its running duration stands in, which is what the poll loop has been
    keeping up to date all along.
    """
    if record.get("ended") is not None:
        return record["ended"]
    return record["started"] + (record.get("duration") or 0.0)


def _overlap(record: dict, start: float, end: float) -> float:
    """Seconds of `record` that fall inside [start, end]; negative if none.

    Negative is the useful part: it says *how far away* a row is, so the row
    that merely came near the audio can be ranked behind the one it actually
    happened during.
    """
    return min(_ended(record), end) - max(record["started"], start)


def _where(
    scanner_id: str | None,
    query: str,
    mode: str,
    since: float | None,
    until: float | None,
    finished: bool = False,
) -> tuple[str, list]:
    """The shared WHERE clause for records() and count()."""
    clauses = []
    params: list = []
    if finished:
        # `ended IS NULL` is exactly the call in progress -- a call cut off by
        # a restart has its end filled in on reload, so this excludes the one
        # happening now and nothing else.
        clauses.append("ended IS NOT NULL")
    if scanner_id:
        clauses.append("scanner_id = ?")
        params.append(scanner_id)
    if mode:
        clauses.append("mode = ?")
        params.append(mode)
    if since is not None:
        clauses.append("started >= ?")
        params.append(float(since))
    if until is not None:
        clauses.append("started <= ?")
        params.append(float(until))
    needle = query.strip().lower()
    if needle:
        clauses.append(r"search LIKE ? ESCAPE '\'")
        params.append(f"%{_escape_like(needle)}%")
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def _escape_like(text: str) -> str:
    """A search box is not a pattern language: `%` and `_` are literals."""
    for char in ("\\", "%", "_"):
        text = text.replace(char, "\\" + char)
    return text


def _to_row(record: dict) -> tuple:
    values = []
    for field in _FIELDS:
        value = record.get(field)
        if field == "unit_ids":
            value = json.dumps(list(value or []))
        elif field == "interrupted":
            value = 1 if value else 0
        values.append(value)
    return tuple(values)


def _to_record(row: sqlite3.Row) -> dict:
    record = dict(row)
    record.pop("search", None)
    try:
        record["unit_ids"] = json.loads(record["unit_ids"]) or []
    except (TypeError, ValueError):
        record["unit_ids"] = []
    record["interrupted"] = bool(record["interrupted"])
    return record


def _migrate(db: sqlite3.Connection) -> None:
    """Add any column this build knows about that the file on disk does not.

    `_SCHEMA` is `CREATE TABLE IF NOT EXISTS`, which is exactly nothing to a
    database that already exists -- so before this, adding a column would
    have worked perfectly on a fresh install and broken every query on an
    upgraded one, which is the worst way round for a bug to be distributed.

    ALTER TABLE ADD COLUMN is cheap in SQLite (a metadata change, not a table
    rewrite, for a nullable column with no default), so this costs nothing
    on every subsequent start once the columns are there.
    """
    have = {row["name"] for row in db.execute("PRAGMA table_info(calls)")}
    for name, kind in _ADDED_COLUMNS:
        if name in have:
            continue
        logger.info("adding the %r column to the receive history", name)
        db.execute(f"ALTER TABLE calls ADD COLUMN {name} {kind}")
    db.commit()


_SEARCH_FIELDS = (
    "label", "system", "department", "site", "channel", "tgid", "mode",
    "mod", "sub_audio", "system_type", "service_type",
    # What makes the existing search box search speech. Nothing else in the
    # UI or the query layer needs to know transcripts exist.
    "transcript",
)


def _searchable(record: dict) -> str:
    parts = [str(record.get(f) or "") for f in _SEARCH_FIELDS]
    parts.extend(str(u) for u in record.get("unit_ids") or [])
    if record.get("frequency") is not None:
        parts.append(f"{record['frequency']:.4f}")
    started = record.get("started")
    if started:
        # Two formats, both local time, so the search box answers "what did I
        # hear on the 6th" as readily as "what did I hear at 14:32". Local
        # rather than UTC because the row is rendered in local time too --
        # searching for what you can see on screen has to work.
        when = time.localtime(started)
        parts.append(time.strftime("%Y-%m-%d %H:%M:%S", when))
        parts.append(time.strftime("%a %d %b %Y", when))
    return " ".join(parts).lower()
