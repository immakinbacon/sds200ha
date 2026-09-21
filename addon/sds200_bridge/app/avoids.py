"""What this add-on has permanently avoided, and enough to put it back.

A permanent avoid edits the favourites list on the scanner itself: it lives
nowhere in this add-on's state, and the way to reverse it is an `AVD` naming
the same list index with status 3. It is made by pressing the unit's AVOID
key twice rather than by sending `AVD,<tkw>,<index>,,1` -- that command sets
a genuine permanent avoid, but in the working copy only, so a power cycle
discards it. See api.post_avoid_current, and docs/protocol-notes.md under
"How avoids actually persist".

The list index is the whole reason this module exists. An avoided channel is
precisely the one the scanner never stops on again, so it never comes back
round in the GSI poll stream -- the number is unrecoverable a second after
it was used unless something wrote it down. So every avoid the add-on makes
is recorded here, along with the names that were on screen at the time, and
un-avoiding is replaying that record with the status changed.

Two limits, both worth stating plainly rather than discovering:

* **Only avoids this add-on made are here.** Anything avoided from the
  scanner's own front panel was never seen by this process, so it cannot be
  listed or undone from this file. The list is "what I did", not "what the
  scanner is avoiding".
* **A record is what was sent, not what is still true.** Clearing an avoid
  on the unit leaves an entry here that no longer corresponds to anything.
  That is what forgetting an entry is for.

Neither limit is a limit on what can be *known*, though an earlier version
of this note claimed both were. `GLT` reports `Off` / `T-Avoid` / `Avoid`
for every entry in the tree, so the scanner can be asked what it is really
avoiding and the answer compared against this file -- including finding
avoids nothing here recorded, which is how a mis-aimed keypress gets caught.
That is `avoid_audit.py`, and it is the reason Forget stayed an operator
assertion rather than becoming an inference: the sweep that would justify
one costs minutes.

Persisted to `/data/avoids.json` and written through on every change, unlike
history.py's debounced save: these arrive a handful of times a day rather
than a handful of times a minute, and losing one to an unclean shutdown
means losing the only copy of a number that cannot be looked up again.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time

logger = logging.getLogger(__name__)

AVOIDS_PATH = "/data/avoids.json"

# A ceiling rather than a working limit: permanently avoiding this many
# channels from a dashboard is not a thing anyone does on purpose, and an
# unbounded file that only ever grows is worse than losing the oldest entry
# of five hundred. Oldest go first.
MAX_RECORDS = 500

# Copied onto each record from reception.extract, so a row reads like
# something recognizable months later rather than like an index.
CONTEXT_FIELDS = ("system", "department", "site", "frequency", "mode")


def _now() -> float:
    return time.time()


class AvoidLog:
    """Every permanent avoid this add-on has sent, newest last.

    `path=None` keeps it in memory only -- what the tests use, and what a
    caller that hasn't got a writable /data can fall back to without
    special-casing every call site.
    """

    def __init__(self, path: str | None = AVOIDS_PATH, max_records: int = MAX_RECORDS):
        self.path = path
        self.max_records = max_records
        self._records: list[dict] = []
        self._next_id = 1
        self.load()  # same as ReceiveHistory: a store reads itself in

    def mark_cleared(self, record: dict) -> None:
        """Note that an avoid has been un-done in the scanner's working copy
        but not yet written to flash.

        Un-avoiding is `AVD,...,3`, which lands in RAM only -- so until
        something saves, the channel is scanning again *and* will be avoided
        again after the next power cycle. The record has to stay visible
        saying exactly that, rather than disappear as though it were done.
        """
        record["cleared_at"] = _now()
        self.save()

    def commit_pending(self) -> list[dict]:
        """Drop the cleared records: a save has just happened.

        The scanner flushes its whole working copy when a keypad press sets
        a permanent avoid, so one successful avoid writes out every pending
        un-avoid along with it. That is the only moment those records stop
        being true.
        """
        committed = [r for r in self._records if r.get("cleared_at")]
        if committed:
            self._records = [r for r in self._records if not r.get("cleared_at")]
            self.save()
        return committed

    def record(self, scanner_id: str, target: dict, command: str, response: str,
               context: dict | None = None) -> dict:
        """Write down an avoid that the scanner acknowledged.

        `target` is a reception.channel_target() result -- the tkw and index
        that were actually sent, not re-derived here, so what gets replayed
        on an undo is exactly what was sent in the first place.
        """
        context = context or {}
        record = {
            "id": self._next_id,
            "scanner_id": scanner_id,
            "at": _now(),
            "tkw": target.get("tkw"),
            "index": target.get("index"),
            "element": target.get("element"),
            "name": target.get("name"),
            "department_index": target.get("department_index"),
            "command": command,
            "response": response,
            # Set once un-avoided in RAM but not yet saved; see mark_cleared.
            "cleared_at": None,
            **{field: context.get(field) for field in CONTEXT_FIELDS},
        }
        self._next_id += 1
        self._records.append(record)
        del self._records[: max(0, len(self._records) - self.max_records)]
        self.save()
        return record

    def for_scanner(self, scanner_id: str) -> list[dict]:
        """Newest first: the one you just made is the one you are most
        likely to have wanted back."""
        return [r for r in reversed(self._records) if r.get("scanner_id") == scanner_id]

    def get(self, scanner_id: str, record_id) -> dict | None:
        try:
            wanted = int(record_id)
        except (TypeError, ValueError):
            return None
        for record in self._records:
            if record.get("id") == wanted and record.get("scanner_id") == scanner_id:
                return record
        return None

    def remove(self, record: dict) -> None:
        if record in self._records:
            self._records.remove(record)
            self.save()

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            # Same call as history.py makes, for a much sharper reason: this
            # file is the only record of indices that cannot be re-read from
            # the scanner. Refusing to start would not bring them back, but
            # the warning needs to be loud enough to notice.
            logger.warning(
                "could not read %s; the record of what has been avoided is empty until "
                "something is avoided again", self.path, exc_info=True,
            )
            return
        self._records = [r for r in data.get("records", []) if isinstance(r, dict)]
        del self._records[: max(0, len(self._records) - self.max_records)]
        self._next_id = max((r.get("id") or 0 for r in self._records), default=0) + 1

    def save(self) -> None:
        if not self.path:
            return
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".avoids-", suffix=".json")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({"records": self._records}, f)
                os.replace(tmp, self.path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        except OSError:
            logger.warning("could not write the avoid log to %s", self.path, exc_info=True)
