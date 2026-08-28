"""Volume and squelch: what we asked for, versus what the scanner reports.

The protocol has no push notification for a VOL/SQL change, which is why
these two started out purely optimistic -- HA remembered whatever it last
set and nothing else. But the value is not actually unobservable: GSI's
`Property` element carries VOL and SQL alongside Rssi and the rest, and the
add-on already polls GSI every few seconds and pushes it in the status feed
(the add-on's own web UI syncs its sliders from exactly this). So a level
turned on the scanner's own knob, or set from the add-on UI, or left over
from before HA restarted, is knowable -- just at GSI's cadence rather than
instantly.

`ReportedLevel` reconciles the two sources. Taking the reading as gospel
the moment it arrives is wrong: a GSI poll already in flight when a set
lands still carries the pre-set value, so the slider would visibly snap back
to the old level for a poll or two before settling. So a value we just set
outranks the reading until either the reading agrees with it or the settle
window passes -- after which the scanner wins, because if it still disagrees
by then it is because the set didn't take (dropped UDP command, level
clamped by the scanner) and the scanner is the one telling the truth.
"""

from __future__ import annotations

import time

# Comfortably longer than protocol.GSI_POLL_INTERVAL (3s) plus a retry, so
# an ordinary set settles by agreement rather than by timeout. It bounds how
# long HA can keep showing a level the scanner never accepted, so it wants
# to stay short enough that a dropped command self-corrects while the user
# is still looking at the card.
SETTLE_SECONDS = 8.0


def gsi_level(status: dict, key: str) -> int | None:
    """Read `Property/@VOL` or `@SQL` out of a status push, or None.

    Absent is a real and frequent case, not an error: which GSI children are
    present depends on the scan mode/screen (see sensor.py), and the status
    feed carries STS-only updates that have no `gsi` key at all.
    """
    value = ((status or {}).get("gsi") or {}).get("Property", {}).get(key)
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


class ReportedLevel:
    """One optimistic level, reconciled against the scanner's own reading."""

    def __init__(self, default: int, clock=time.monotonic):
        self._clock = clock
        self._last = default
        self._pending: int | None = None
        self._pending_since = 0.0

    @property
    def value(self) -> int:
        """The last resolved level -- no reading involved, so it's also what
        a caller should build the *next* value from (stepping, mute restore).
        """
        return self._pending if self._pending is not None else self._last

    def set(self, level: int) -> None:
        """Record a level we have just sent to the scanner."""
        self._pending = level
        self._pending_since = self._clock()

    def resolve(self, reported: int | None) -> int:
        """Fold in this poll's reading and return the level to report."""
        if self._pending is not None:
            settled = reported == self._pending
            expired = self._clock() - self._pending_since >= SETTLE_SECONDS
            if not (settled or expired):
                return self._pending
            self._pending = None
        if reported is not None:
            self._last = reported
        return self._last
