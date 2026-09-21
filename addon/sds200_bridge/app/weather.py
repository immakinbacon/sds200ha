"""Weather alerts: fire actions on them, and get the scanner back to scanning.

The SDS200 stops scanning when a weather alert comes in and stays parked on
the alert screen until somebody presses a key -- which, unattended, means a
scanner that quietly stopped scanning hours ago. This module watches for that
state on the status stream both scanners already produce, hands it to the
trigger engine as its own event (so "weather alert -> notify me" is an action
rule like any other), and optionally presses the way back to scanning after a
configured wait.

**How it knows.** `reception.extract` lifts `WxMode/@Mode` out of GSI, which
per the spec's mode matrix is only present at all while the scanner is in
weather monitor/alert mode. `"Weather Alert"` is an alert actually received;
`"Monitor Weather"` is just sitting on a weather channel and is deliberately
*not* treated as an alert. Both the element and the `"Weather Alert"` value
are confirmed against a real alert on real hardware -- see
docs/protocol-notes.md.

**The alert and the alert screen are two different things.** The alert is a
state that comes and goes on its own; the screen it puts the scanner on stays
until a key is pressed. So the two halves below watch different fields. The
*events* fire on `wx_alert` (`WxMode/@Mode`), because that is the thing an
automation means by "a weather alert came in". The *return to scanning* runs
off `wx_parked` (`ScannerInfo/@V_Screen == "wx_alert"`), because that is the
thing that stopped the scanner scanning -- and it outlives the alert. Driving
the press off the alert instead would leave a scanner parked forever whenever
an alert cleared before the configured wait was up, which is precisely the
silent failure this module exists to prevent.

**Which key.** Not a hardcoded one. The scanner's own soft-key labels come
through in every STS poll (`protocol._parse_soft_keys`), and on the alert
screen one of them is the way out -- so the key pressed is whichever of
soft1/soft2/soft3 currently sits under a label like "TO SCAN". A firmware
that renames or moves it keeps working, and a screen that offers no such key
gets nothing pressed rather than a guess. The per-scanner fallback key exists
for the case where the label doesn't match at all; it defaults to unset,
because pressing an arbitrary key on an unknown screen can hold or avoid a
channel and leave the scanner in a stranger state than the alert did.

**It takes more than one press, because there is more than one screen.** An
alert opens as a *modal popup* over the WX Hold screen, and the popup takes
the soft-key row with it -- STS sends no label row at all while one is up, so
there is nothing for `find_scan_key` to read and the old code gave up here
and left the scanner parked. Dismissing the popup does not resume scanning
either; it reveals the soft-key screen underneath, still held on the weather
channel, where "to Scan" is what actually resumes. Both screens are
`V_Screen="wx_alert"`, and so is a third (`Mode="WX Scan"`, walking the WX
channels with soft3 relabelled "HOLD"). All three are parked as far as the
systems the user cares about are concerned, and getting out is a chain:
dismiss, then leave. `MAX_ATTEMPTS` is therefore counted *per screen* -- a
screen that changes under us is progress, not a failed press -- with
`MAX_PRESSES` as the backstop against two screens that bounce off each other
forever. All confirmed live against a real alert; see docs/protocol-notes.md.

**Every press is aimed at a screen and re-checked against it.** The key is
chosen from a status snapshot, but the scanner moves on its own -- WX
Priority dual-watch drops it back to scanning after a few seconds without
anybody pressing anything. A key chosen for the weather screen and delivered
a moment later can land on the scan screen, where soft1/soft2/soft3 are
System/Dept/Channel and the press silently *holds* one of them instead. That
is not hypothetical and it is not once: it happened during the live capture
this module was written from, and again in the field afterwards, where it
left the scanner scanning one CB department for eleven hours while the
display read "Scanning...". So `_press` re-reads the scanner immediately
before sending -- `refresh_display` *and* `refresh_gsi`, because the labels come
from one and the popup keys from the other, and re-reading only GSI is what
let the second one through -- and sends nothing if the scanner is no longer
on the screen the key was picked for.

**"Parked" follows the screen, not the alert.** `reception.wx_parked` reads
`V_Screen` and only falls back to the alert state when GSI sends no
`V_Screen` at all. It used to fall back unconditionally, which made a
scanner that was scanning perfectly well look parked for as long as an alert
was current anywhere -- WX Priority keeps `WxMode` in GSI across a dip
without ever leaving the scan screen -- and a module that presses keys at
parked scanners then had every reason to press one at this one.
"""

from __future__ import annotations

import asyncio
import logging
import time

import key_codes
import reception

logger = logging.getLogger(__name__)

# Matched case-insensitively as a substring of a soft-key label: the label
# observed on real hardware is "to Scan" (soft1, in the first column), but
# "Scan" alone or a differently-cased variant should count too.
SCAN_LABEL = "scan"

SOFT_KEYS = ("soft1", "soft2", "soft3")

# Last-resort key for a modal popup that declares buttons none of which
# clear it. A popup hides the soft-key labels, so there is nothing to read
# and this one position is by convention rather than from the screen. It is
# tried *after* the popup's own declared key codes (see `find_way_out`),
# because a soft-key position aimed at a popup and landing on the scan
# screen is how the scanner ends up held on a system.
POPUP_DISMISS_KEY = "soft1"

# How many times to press *on one screen* before giving up until the screen
# changes. The press is confirmed only as "the scanner accepted the key" --
# if the same screen is still up a wait later, the key was the wrong one or
# was swallowed, and retrying forever would key the panel every few seconds
# for as long as the alert runs. Reaching a different screen resets this,
# because leaving a weather screen is exactly what the press was for.
MAX_ATTEMPTS = 3

# Backstop across the whole park, counting presses on every screen. Two
# screens that lead to each other would otherwise reset MAX_ATTEMPTS off each
# other and key the panel indefinitely.
MAX_PRESSES = 6


def find_scan_key(status: dict) -> str | None:
    """The soft key whose on-screen label offers a way back to scanning."""
    labels = status.get("soft_keys") or []
    for name, label in zip(SOFT_KEYS, labels):
        if SCAN_LABEL in label.lower():
            return name
    return None


def screen_signature(status: dict, snapshot: dict) -> tuple:
    """What screen the scanner is showing, as far as pressing keys goes.

    Two weather screens can share a Mode and a V_Screen and still need
    different keys, so neither is what identifies a screen here -- what the
    screen *offers* is. The soft-key labels and the popup's key codes are
    exactly the things a press is chosen from, so a change in either is a
    change worth re-deciding on, and nothing else is.
    """
    return (tuple(status.get("soft_keys") or ()), tuple(snapshot.get("popup_keys") or ()))


def find_way_out(status: dict, snapshot: dict, attempt: int) -> str | None:
    """The key to press to get one step closer to scanning, or None.

    One step, not the whole way: an alert puts a popup over the weather
    screen, and dismissing it only uncovers the screen that still has to be
    left. `attempt` is how many times this screen has already been pressed
    at, which is what works down from the keys the popup itself named to the
    conventional dismiss position if none of them clear it.
    """
    key = find_scan_key(status)
    if key is not None:
        return key
    popup_keys = snapshot.get("popup_keys") or []
    if not popup_keys:
        return None
    # A popup is up and it hid the labels. Work through the keys the popup
    # itself named first, and only then the dismiss position. That order is
    # about which press does less damage if the scanner moved in between: the
    # popup's own key is a specific one ("E", seen live to clear a popup) that
    # does nothing much on the scan screen, whereas POPUP_DISMISS_KEY is a
    # soft-key *position*, and that position on the scan screen is SYSTEM --
    # the accidental hold this module keeps having to defend against. It is
    # also the key never confirmed to dismiss a popup at all, so trying it
    # first spent the riskier press on the less likely one.
    if attempt < len(popup_keys):
        return popup_keys[attempt]
    return POPUP_DISMISS_KEY


class _ScannerState:
    """One scanner's alert bookkeeping.

    Two clocks, because the alert and the screen it parks on are two different
    things (see the module docstring). `alert_since` is None whenever the
    scanner is not in an alert, and `parked_since` is None whenever it is not
    sitting on the alert screen -- each being None is what makes its own edge
    detectable.
    """

    def __init__(self, conn):
        self.conn = conn
        self.return_after_s: float = 0.0
        self.fallback_key: str = ""
        self.alert_since: float | None = None
        self.parked_since: float | None = None
        # Attempts on the current screen; presses across the whole park. The
        # two differ because getting out takes a press per screen, so a
        # screen change resets `attempts` and only `presses` keeps counting.
        # Both count keys actually sent. `last_try` is when one was last
        # *considered*, sent or not, and is what paces the retries -- a press
        # abandoned because the scanner could not be re-read must not spend
        # one of the three, or a flaky GSI (which this one is) would use them
        # all up without ever pressing anything and leave the scanner parked.
        self.attempts: int = 0
        self.presses: int = 0
        self.last_try: float | None = None
        self.screen: tuple | None = None
        self.pressing: bool = False


class WeatherWatch:
    """Watches every running scanner's status for a weather alert.

    Plugged in as a status listener (`ScannerConnection.add_status_listener`)
    rather than driven by its own poll loop: the status stream is already
    running several times a second, carries both the GSI-derived alert state and
the display-mirror
    soft-key labels, and this way there is no second clock to keep in step
    with a scanner being added or removed.
    """

    def __init__(self, trigger_engine=None, clock=time.monotonic):
        self.trigger_engine = trigger_engine
        self._clock = clock
        self._scanners: dict[str, _ScannerState] = {}

    def attach(self, conn) -> None:
        self._scanners[conn.id] = _ScannerState(conn)

    def detach(self, scanner_id: str) -> None:
        self._scanners.pop(scanner_id, None)

    def configure(self, scanner_id: str, *, return_after_s: float, fallback_key: str) -> None:
        """Apply a scanner's weather settings in place.

        Separate from `attach` so a settings save that only changes these can
        be applied without restarting the connection -- see manager.py's
        WATCH_ONLY_FIELDS.
        """
        state = self._scanners.get(scanner_id)
        if state is None:
            return
        state.return_after_s = return_after_s
        state.fallback_key = fallback_key

    def status_callback(self, scanner_id: str, status: dict) -> None:
        """Plugged into `ScannerConnection.add_status_listener`.

        Called for STS updates as well as GSI ones. That is deliberate: the
        alert state itself only changes on a GSI poll (every few seconds),
        but the return-to-scan countdown and the soft-key labels are both
        better served by the fast display tick.
        """
        state = self._scanners.get(scanner_id)
        if state is None:
            return
        snapshot = reception.extract(status)
        now = self._clock()

        # The alert itself: what the action rules fire on.
        if snapshot["wx_alert"]:
            if state.alert_since is None:
                state.alert_since = now
                logger.info(
                    "%s: weather alert (SAME %s)", scanner_id, snapshot["wx_same"] or "none"
                )
                self._fire("wx_alert", scanner_id, snapshot)
        elif state.alert_since is not None:
            logger.info(
                "%s: weather alert cleared after %.0fs", scanner_id, now - state.alert_since
            )
            state.alert_since = None
            self._fire("wx_clear", scanner_id, snapshot)

        # The screen it parked on: what the return-to-scan press is for. Kept
        # separate so an alert that clears on its own doesn't call off a press
        # the scanner still needs to start scanning again.
        if snapshot["wx_parked"]:
            screen = screen_signature(status, snapshot)
            if state.parked_since is None:
                state.parked_since = now
                state.attempts = 0
                state.presses = 0
                state.last_try = None
                state.screen = screen
            elif screen != state.screen:
                # Still parked, but on a different weather screen -- which is
                # what a press that worked looks like from here, and is also
                # what the scanner does on its own between WX Hold and WX
                # Scan. Either way the new screen gets its own attempts
                # rather than inheriting the last one's, and clearing
                # `last_try` lets the next step follow immediately: the wait
                # is there so somebody can read the alert, and by now that
                # grace has been spent.
                logger.info(
                    "%s: now on a different weather screen (soft keys %s, popup keys %s)",
                    scanner_id, list(screen[0]), list(screen[1]),
                )
                state.screen = screen
                state.attempts = 0
                state.last_try = None
            self._maybe_return_to_scan(state, status, snapshot, now)
        elif state.parked_since is not None:
            if state.presses:
                logger.info(
                    "%s: back to scanning after %d press(es), %.0fs on the weather screen",
                    scanner_id, state.presses, now - state.parked_since,
                )
            state.parked_since = None
            state.attempts = 0
            state.presses = 0
            state.last_try = None
            state.screen = None

    def _fire(self, event: str, scanner_id: str, snapshot: dict) -> None:
        if self.trigger_engine is None:
            return
        # Shaped like a history record rather than a bare snapshot: the same
        # rules, templates and cooldown bookkeeping run against both, and
        # `label`/`duration`/`unit_ids` are what those expect to find.
        record = {
            **snapshot,
            "scanner_id": scanner_id,
            "label": "Weather alert" if event == "wx_alert" else "Weather alert cleared",
            "unit_ids": [],
            "duration": 0.0,
        }
        try:
            self.trigger_engine.on_call(event, record)
        except Exception:
            logger.exception("%s: could not evaluate actions for %s", scanner_id, event)

    def _maybe_return_to_scan(
        self, state: _ScannerState, status: dict, snapshot: dict, now: float
    ) -> None:
        if not state.return_after_s or state.pressing:
            return
        if state.attempts >= MAX_ATTEMPTS or state.presses >= MAX_PRESSES:
            return
        assert state.parked_since is not None
        waited = now - state.parked_since
        # Each attempt gets its own full wait, so a press that didn't take is
        # retried at the same cadence rather than immediately. Counted from
        # the start of the park rather than the current screen, so working
        # through a two-screen chain doesn't restart the clock each step.
        due_at = (state.last_try if state.last_try is not None else state.parked_since)
        if now - due_at < state.return_after_s:
            return
        key = find_way_out(status, snapshot, state.attempts) or state.fallback_key
        if not key:
            state.attempts = MAX_ATTEMPTS  # nothing to press; stop looking until it changes
            logger.warning(
                "%s: parked on a weather screen offering neither a soft key labelled %r nor a "
                "popup button, and no fallback key configured -- leaving it alone. Set one in "
                "the scanner's settings.",
                state.conn.id, SCAN_LABEL,
            )
            return
        state.last_try = now
        state.pressing = True
        asyncio.create_task(self._press(state, key, state.screen))

    async def _press(self, state: _ScannerState, key: str, screen: tuple | None) -> None:
        try:
            code = key_codes.resolve(key)
        except KeyError:
            logger.error("%s: %r is not a key this scanner has", state.conn.id, key)
            state.attempts = MAX_ATTEMPTS
            state.pressing = False
            return
        try:
            if not await self._still_showing(state, screen):
                return
            # Counted here rather than where the key was chosen: these bound
            # how often the panel is keyed, so only a key that reaches the
            # wire spends one. A send that then fails still counts -- it was
            # pressed, we just never heard back.
            state.attempts += 1
            state.presses += 1
            response = await state.conn.send_command(f"KEY,{code},P")
        except Exception as exc:
            logger.warning(
                "%s: could not press %s to leave the weather alert: %s", state.conn.id, key, exc
            )
            return
        finally:
            state.pressing = False
        # Only ever "the key was accepted", never "the scanner is scanning
        # again" -- that is the alert screen going away, which status_callback
        # logs when it sees it. Keeping the two apart matters because a press
        # can be accepted and still not be the key that leaves the screen.
        if response.startswith("KEY,OK"):
            logger.info(
                "%s: pressed %s to leave the weather alert screen (attempt %d)",
                state.conn.id, key, state.attempts,
            )
        elif not response:
            # Seen on the popup: the key is taken and the screen changes, but
            # nothing comes back. Not a refusal, and not a confirmation
            # either -- the screen going away is the only confirmation there
            # has ever been here, and status_callback is what reports that.
            logger.info(
                "%s: pressed %s on the weather popup with no reply (attempt %d)",
                state.conn.id, key, state.attempts,
            )
        else:
            logger.warning(
                "%s: the scanner refused %s (%r) while leaving the weather alert screen",
                state.conn.id, key, response,
            )

    async def _still_showing(self, state: _ScannerState, screen: tuple | None) -> bool:
        """Re-read the scanner and confirm it is still on the screen the key was for.

        The key was picked from a snapshot up to a poll old, and this scanner
        moves on its own -- WX Priority dual-watch takes it back to scanning
        after a few seconds with nobody touching it. Landing a weather-screen
        key on the scan screen is not a harmless no-op there: soft1 is SYSTEM
        and the press holds the system, stopping the scanning it was supposed
        to restore.

        *Both* polls, because a screen is identified here by what it offers
        and the two halves of that come from different commands: the popup's
        key codes from GSI, the soft-key labels from STS. Refreshing GSI
        alone re-read the popup and left the labels a poll stale -- and the
        labels are the half that tells the scan screen from a weather one,
        so the check passed while the scanner sat on the screen where the
        key holds a system. It did exactly that. Confirming costs two round
        trips per press, and presses are rare.
        """
        try:
            await state.conn.refresh_display()
            await state.conn.refresh_gsi()
        except Exception as exc:
            logger.warning(
                "%s: could not confirm the weather screen before pressing, so did not press: %s",
                state.conn.id, exc,
            )
            return False
        snapshot = reception.extract(state.conn.last_status or {})
        if not snapshot["wx_parked"]:
            logger.info(
                "%s: left the weather screen on its own before the return-to-scan press",
                state.conn.id,
            )
            return False
        current = screen_signature(state.conn.last_status or {}, snapshot)
        if screen is not None and current != screen:
            logger.info(
                "%s: the weather screen changed before the press landed, so did not press "
                "(was %s, now %s)",
                state.conn.id, screen, current,
            )
            return False
        return True
