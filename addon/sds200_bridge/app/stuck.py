"""Notices a scanner that has stopped covering its lists, and says so.

Three times in four days this scanner was found narrowed to one department --
twice for eleven hours, once for a day -- with `Mode="Scan Mode"`, a display
reading `Scanning...`, and nothing anywhere that thought it had a problem.
`weather.py` was the cause of at least the first of those and has been fixed
twice for it, but the fix is specific to the way weather.py gets it wrong,
and the third episode happened with that fix deployed. This module is the
cause-agnostic half: it does not care *why* the scanner stopped hearing
things, only that it has, and it is the piece that was missing every time --
the notes' own caution, "a held scanner has stopped scanning, and nothing in
the add-on notices", written before any of the three.

**It never presses a key.** That is not an omission, it is the point. Every
one of these episodes was produced by something pressing a key at a scanner
whose screen it had misread, and a watchdog that reacted by pressing more
keys would be the same bug with a longer fuse. It reports; a person (or an
action rule) decides. Releasing is one targeted `HLD,SYS,<index>,` away and
the API already has the route.

**Two conditions, because they fail differently.**

`hold_s` watches `held_scopes` -- a System, Department or Site hold, not a
channel hold. That distinction is `reception.SCOPE_HOLD_ELEMENTS` and the
reasoning is there: the channel hold is the deliberate one, so watching it
would fire on ordinary use. A scope hold has no deliberate path in this
project, so it means either somebody used the unit's front panel or
something pressed a soft key it shouldn't have. Catches the known failure
within minutes and names it exactly.

`silence_s` watches the receive history instead: no call recorded for this
long. It catches everything the hold check cannot -- a channel hold left on
for a day, squelch wound shut, an antenna knocked off, a control link that
answers polls while the receiver does nothing -- at the cost of not knowing
what is wrong, and of a threshold that depends on how busy the site is.
Default off for that reason; the hold check is the one that can be on by
default because it has no false-positive case that isn't also worth knowing.

**Both run off the status stream** (`ScannerConnection.add_status_listener`),
including the silence one, whose condition is the *absence* of an event and
so needs something ticking to be noticed at all. The status stream is
already ticking several times a second and already carries the hold flags, so
there is no
second clock to keep in step with a scanner being added or removed -- the
same reasoning weather.py is built on.

**It repeats.** A stuck scanner fires on the edge like any other event, and
then again every `REPEAT_S` for as long as it stays stuck. An edge-only
event is the wrong shape here: the failure it reports lasts for hours, and
the one notification it would send is exactly the one that gets missed at
02:00 -- which is how eleven hours happened twice. `stuck_for_s` in the
record is what lets the repeat say how long it has been, and a rule's own
`cooldown_s` can throttle it further.
"""

from __future__ import annotations

import logging
import time

import reception

logger = logging.getLogger(__name__)

# How often a scanner that is still stuck fires the event again. An hour is
# short enough that a missed notification comes back the same night and long
# enough not to be noise -- and a rule that wants it quieter has cooldown_s.
REPEAT_S = 3600.0


def describe(reason: str, scopes: tuple[str, ...], stuck_for_s: float) -> str:
    """One line saying what is wrong, for the log and for a notification body."""
    hours = stuck_for_s / 3600.0
    duration = f"{hours:.1f}h" if hours >= 1 else f"{stuck_for_s / 60:.0f}m"
    if reason == "held":
        # Named in the scanner's own words ("System", "Department") because
        # that is what the person releasing it will be looking at.
        return f"held on {' and '.join(scopes).lower() or 'a scope'} for {duration}"
    return f"nothing received for {duration}"


class _ScannerState:
    """One scanner's watchdog bookkeeping.

    `held_since` and `last_call` are the two clocks the conditions are
    measured against; `stuck_since` is None whenever the scanner is not
    currently reported stuck, which is what makes the edge detectable.
    """

    def __init__(self, conn, now: float):
        self.conn = conn
        self.hold_s: float = 0.0
        self.silence_s: float = 0.0
        self.held_since: float | None = None
        self.held_scopes: tuple[str, ...] = ()
        # Seeded at attach rather than left None: a scanner that has just
        # come up has not been silent since the epoch, and starting the
        # clock anywhere else would fire `silence_s` on every restart.
        self.last_call: float = now
        self.stuck_since: float | None = None
        self.stuck_reason: str = ""
        # What was held when this episode was reported, kept apart from
        # `held_scopes` because the release is exactly what ends the episode:
        # by the time the all-clear is written, `held_scopes` is empty and
        # the message would have nothing to name.
        self.stuck_scopes: tuple[str, ...] = ()
        self.last_fired: float | None = None


class StuckWatch:
    """Watches every running scanner for having quietly stopped scanning."""

    def __init__(self, trigger_engine=None, clock=time.monotonic):
        self.trigger_engine = trigger_engine
        self._clock = clock
        self._scanners: dict[str, _ScannerState] = {}

    def attach(self, conn) -> None:
        self._scanners[conn.id] = _ScannerState(conn, self._clock())

    def detach(self, scanner_id: str) -> None:
        self._scanners.pop(scanner_id, None)

    def configure(self, scanner_id: str, *, hold_s: float, silence_s: float) -> None:
        """Apply a scanner's watchdog settings in place.

        Separate from `attach` for the same reason weather.py's is: these are
        `manager.WATCH_ONLY_FIELDS`, so changing one must not restart a
        connection and take the audio session down with it.
        """
        state = self._scanners.get(scanner_id)
        if state is None:
            return
        state.hold_s = hold_s
        state.silence_s = silence_s

    def on_call(self, event: str, record: dict) -> None:
        """Plugged into `ReceiveHistory.add_listener`, alongside the trigger engine.

        Only the clock is wanted here, not the call: anything the scanner
        heard is proof it was still listening, whichever edge reported it.
        """
        state = self._scanners.get(record.get("scanner_id", ""))
        if state is not None:
            state.last_call = self._clock()

    def status_callback(self, scanner_id: str, status: dict) -> None:
        """Plugged into `ScannerConnection.add_status_listener`."""
        state = self._scanners.get(scanner_id)
        if state is None:
            return
        now = self._clock()
        snapshot = reception.extract(status)

        scopes = tuple(snapshot.get("held_scopes") or ())
        if scopes:
            if state.held_since is None:
                state.held_since = now
            # The set can grow -- System first, then Department a press later
            # -- and that is the same hold getting worse, not a new one. The
            # clock keeps running; only the names are updated.
            state.held_scopes = scopes
        else:
            state.held_since = None
            state.held_scopes = ()

        tripped = self._assess(state, now)
        if tripped is None:
            self._clear(state, scanner_id, snapshot, now)
            return
        self._report(state, scanner_id, snapshot, *tripped, now)

    def _assess(self, state: _ScannerState, now: float) -> tuple[str, float] | None:
        """Which condition is tripped and when it started, or None.

        Hold wins when both are, because it is the specific answer: if the
        scanner is held *and* has heard nothing, the hold is why, and
        reporting the silence instead would send somebody looking for a
        fault that isn't there.

        The second element is when the *condition* started, not when it
        crossed the threshold -- a notification saying "held for 11 hours"
        is worth reading and one saying "held for 0 minutes" is not, and the
        difference is which of those two times gets recorded.
        """
        if state.hold_s and state.held_since is not None:
            if now - state.held_since >= state.hold_s:
                return "held", state.held_since
        if state.silence_s and now - state.last_call >= state.silence_s:
            return "silent", state.last_call
        return None

    def _clear(
        self, state: _ScannerState, scanner_id: str, snapshot: dict, now: float
    ) -> None:
        if state.stuck_since is None:
            return
        stuck_for_s = now - state.stuck_since
        detail = describe(state.stuck_reason, state.stuck_scopes, stuck_for_s)
        logger.info("%s: scanning its lists again, after %s", scanner_id, detail)
        record = {
            **snapshot,
            "scanner_id": scanner_id,
            "label": f"Scanner scanning again, after {detail}",
            "unit_ids": [],
            "duration": 0.0,
            "reason": state.stuck_reason,
            "stuck_for_s": stuck_for_s,
            "held_scopes": list(state.stuck_scopes),
        }
        state.stuck_since = None
        state.stuck_reason = ""
        state.stuck_scopes = ()
        state.last_fired = None
        self._fire("stuck_clear", scanner_id, record)

    def _report(
        self, state: _ScannerState, scanner_id: str, snapshot: dict,
        reason: str, since: float, now: float,
    ) -> None:
        # A scanner that goes from held to silent (or back) is still the same
        # episode as far as "when did this start" goes, but the reason it is
        # reported under changes, so the message does too.
        if state.stuck_since is None:
            state.stuck_since = since
        state.stuck_reason = reason
        state.stuck_scopes = state.held_scopes if reason == "held" else ()
        if state.last_fired is not None and now - state.last_fired < REPEAT_S:
            return
        first = state.last_fired is None
        state.last_fired = now
        stuck_for_s = now - state.stuck_since
        detail = describe(reason, state.held_scopes, stuck_for_s)
        logger.warning(
            "%s: %s -- %s. Nothing here will press a key to fix it; release it from the "
            "Control tab or with HLD,<scope>,<index>,.",
            scanner_id, detail, "not scanning its lists" if first else "still not scanning its lists",
        )
        self._fire("stuck", scanner_id, {
            # Shaped like a history record for the same reason weather.py's
            # is: the rules, templates and cooldown bookkeeping all run
            # against both, and they expect `label`/`duration`/`unit_ids`.
            **snapshot,
            "scanner_id": scanner_id,
            "label": f"Scanner {detail}",
            "unit_ids": [],
            "duration": 0.0,
            "reason": reason,
            "stuck_for_s": stuck_for_s,
            "held_scopes": list(state.held_scopes),
        })

    def _fire(self, event: str, scanner_id: str, record: dict) -> None:
        if self.trigger_engine is None:
            return
        try:
            self.trigger_engine.on_call(event, record)
        except Exception:
            logger.exception("%s: could not evaluate actions for %s", scanner_id, event)
