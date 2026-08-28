"""Turn a raw status dict into "what is the scanner hearing right now".

Everything downstream of this module -- the receive history, the trigger
rules, and the web UI's live panel -- works on the flat snapshot produced
here rather than poking at the nested GSI payload directly, so there is
exactly one place that knows which GSI element a field lives under.

That matters because *which* element holds a given field depends on the scan
mode (see the "Depend on mode elements" matrix in the Remote Command spec,
and docs/protocol-notes.md): conventional scanning puts frequency/modulation
under `ConvFrequency`, trunked puts them under `Site`/`SiteFrequency`, and
the channel-ish name is `ConvFrequency/@Name` conventional but `TGID/@Name`
trunked. `custom_components/sds200/sensor.py` already had to learn this the
hard way; this is the same lookup order, in the add-on, where the history
and trigger code can share it.

Digital mode classification is best-effort -- see `classify_mode`.
"""

from __future__ import annotations

import re

from protocol import UNMUTED

# Property/@Rssi uses -999 as "not currently receiving" -- confirmed on real
# hardware, where it showed up alongside a literal "RSSI: ---" in the STS
# display text of the same capture (see sensor.py's _extract_rssi).
RSSI_NOT_RECEIVING = -999.0

# Uniden writes "no value" as a word rather than an empty attribute, and the
# word differs per field: "TGID None", "UID None", "None", "Off". Treated as
# absent everywhere rather than matched literally, so a rule that matches
# `unit_id` contains "None" doesn't fire on every silent poll.
_EMPTY_VALUES = {"", "none", "off", "---", "tgid none", "uid none", "no data"}

# Normalized mode slugs. "analog" covers AM/FM/NFM/WFM/FMB; the rest are the
# digital voice formats an SDS-series scanner can decode. "unknown" means the
# scanner reported a system type this doesn't recognize -- rules can still
# match it by its raw `system_type` string, which is why that field is kept
# on every snapshot.
MODES = ("analog", "p25", "dmr", "nxdn", "provoice", "edacs", "ltr", "motorola", "unknown")

# Matched as substrings against System/@SystemType, longest-first, so
# "MotoTRBO Capacity Plus" resolves to dmr before the bare "moto" -> motorola
# entry can claim it. The strings come from Uniden's own system-type naming
# (Sentinel / the scanner's own menus); only "Conventional" has been seen in
# a capture from this project's hardware so far, so anything not listed here
# lands on "unknown" rather than being guessed at -- and is still matchable
# raw. Order within the tuple is significant.
_SYSTEM_TYPE_MODES: tuple[tuple[str, str], ...] = (
    ("mototrbo", "dmr"),
    ("dmr", "dmr"),
    ("nexedge", "nxdn"),
    ("nxdn", "nxdn"),
    ("provoice", "provoice"),
    ("edacs", "edacs"),
    ("ltr", "ltr"),
    ("p25", "p25"),
    ("project 25", "p25"),
    ("astro", "p25"),
    ("motorola", "motorola"),
    ("conventional", "analog"),
)

# WxMode/@Mode's two documented values. "Monitor Weather" is the scanner
# sitting on a weather channel; "Weather Alert" is a SAME/alert tone actually
# having been received, which is the state weather.py acts on.
WX_MONITOR_MODE = "Monitor Weather"
WX_ALERT_MODE = "Weather Alert"

# ScannerInfo/@V_Screen while the scanner is sitting on the alert screen --
# confirmed live against a real alert (docs/protocol-notes.md). This is the
# state that stops the scanner scanning, and it is the screen, not the alert:
# dismissing it needs a key press whether or not the alert is still current.
WX_ALERT_SCREEN = "wx_alert"

# ViewDescription child that means "a modal popup is covering the screen".
# Seen live on the weather alert, which opens as a popup over the WX Hold
# screen and only becomes the soft-key screen once it is dismissed.
POPUP_ELEMENT = "PopupScreen"

# Conventional channels don't carry a system type that says "this one is
# digital" -- the modulation attribute is the only hint, and Uniden's own
# values for it are the analog ones plus "AUTO" (decode whatever arrives).
_ANALOG_MODULATIONS = {"am", "fm", "nfm", "wfm", "fmb"}


def _text(value) -> str | None:
    """Strip a GSI attribute, mapping Uniden's various spellings of "nothing"
    to None so callers can just check for falsiness.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _EMPTY_VALUES:
        return None
    # A labelled field the scanner has nothing for yet: "TGID: ---", "UID: ---".
    # Worth catching separately from the bare "---" above, because this one is
    # not merely untidy -- it is a *value*, so an identity holding it compares
    # unequal to the same call's real talkgroup when that arrives a poll later,
    # and the call is split in two exactly as it was before 0.7.47.
    if not text.split(":", 1)[-1].strip().strip("-"):
        return None
    return text


def _first(gsi: dict, keys: tuple[str, ...], attribute: str) -> str | None:
    """First non-empty `attribute` across `keys`, in mode-priority order."""
    for key in keys:
        value = _text(gsi.get(key, {}).get(attribute))
        if value:
            return value
    return None


def _number(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_frequency(value) -> float | None:
    """" 462.612500MHz" -> 462.6125.

    The unit is baked into the string the scanner sends; a bare float is what
    every consumer here actually wants (see sensor.py, which hit the same
    thing).
    """
    text = _text(value)
    if not text:
        return None
    return _number(text.removesuffix("MHz").removesuffix("mhz"))


def classify_mode(gsi: dict) -> str:
    """Best-effort normalized mode slug for what's being received.

    Precedence, and why:

    1. `Property/@P25Status` -- if the scanner says it is decoding P25 right
       now, that beats anything inferred from configuration. This is the only
       field that reports an *observed* digital decode rather than how the
       system was programmed.
    2. `System/@SystemType` -- how the system being scanned is programmed.
       Substring-matched against `_SYSTEM_TYPE_MODES`.
    3. `ConvFrequency/@Mod` -- conventional only, and only enough to say
       "analog" when the modulation is one of the analog ones.

    Returns "unknown" rather than guessing when none of those settle it.
    Callers that need more precision should match `system_type`/`p25_status`
    directly; this slug exists to make the common "do something on any digital
    traffic" rule expressible without knowing Uniden's naming.
    """
    if _text(gsi.get("Property", {}).get("P25Status")):
        return "p25"

    system_type = (_text(gsi.get("System", {}).get("SystemType")) or "").lower()
    if system_type:
        for needle, mode in _SYSTEM_TYPE_MODES:
            if needle in system_type:
                # A conventional *system* can still hold digital channels, so
                # don't let "Conventional" short-circuit the modulation check
                # below -- fall through to it instead of returning "analog".
                if mode != "analog":
                    return mode
                break
        else:
            return "unknown"

    mod = (_first(gsi, ("ConvFrequency", "Site"), "Mod") or "").lower()
    if mod in _ANALOG_MODULATIONS:
        return "analog"
    return "unknown"


def _popup_keys(gsi: dict) -> list[str]:
    """The key codes a modal popup says will dismiss it, in the order given.

    The scanner states these itself, one `<Button KeyCode="..."/>` per way
    out, which matters because a popup hides the soft-key row that
    `weather.py` normally reads its way out of -- there is nothing in STS to
    reason from while one is up. Same principle as reading the soft-key
    labels rather than hardcoding a key: the screen is asked what it offers.
    """
    popup = (gsi.get("view_description") or {}).get(POPUP_ELEMENT) or {}
    return [code for button in popup.get("buttons", []) if (code := button.get("KeyCode"))]


# What the screen says, for the fields it states exactly.
#
# The identity has always come from GSI, which is polled every three seconds
# while the display is polled four times a second -- so for up to three
# seconds a new transmission carries the previous one's talkgroup, or none at
# all. The screen has the answer the whole time: captured from the real
# scanner mid-call, the talkgroup line reads `TGID:10852` against GSI's
# `TGID="TGID:10852"`, character for character.
#
# Only the fields the screen states *exactly* are taken this way. The names
# beside them are cut to the display width -- "Indiana University Campus Po"
# for "Indiana University Campus Police Dispatch" -- so a name read off the
# screen would be a different string from the one GSI gives for the same call,
# and two spellings of one talkgroup is worse than one arriving late.
#
# Matched by pattern rather than by line number on purpose: which line holds
# what depends on the screen, and a scanner sitting on a menu or a popup
# simply yields nothing here and falls back to GSI.
# Each pattern captures the *value*; the talkgroup keeps its "TGID:" prefix
# because that is how GSI spells it, and the two have to be the same string
# or the identity comparison sees a change where there was none.
_DISPLAY_FIELDS = (
    ("tgid", re.compile(r"\bTGID:\s*([^\s]+)"), "TGID:"),
    ("unit_id", re.compile(r"\bUID:\s*([^\s]+)"), ""),
    ("frequency", re.compile(r"\b(\d{2,4}\.\d{3,6})\s*MHz\b"), ""),
)


def from_display(status: dict) -> dict:
    """The identity fields the screen states outright, as fresh as the poll.

    Empty when the scanner is on a screen that does not show them, which is
    the same answer as "we do not know yet" and is handled the same way.
    """
    text = " ".join(
        line.get("text", "") if isinstance(line, dict) else str(line)
        for line in status.get("lines") or ()
    )
    found = {}
    for field, pattern, prefix in _DISPLAY_FIELDS:
        match = pattern.search(text)
        if not match:
            continue
        # The screen writes "nothing yet" as a row of dashes, in every field
        # that has one -- "TGID:---", "UID: ---".
        value = _text(match.group(1))
        if not value or not value.strip("-"):
            continue
        found[field] = parse_frequency(value) if field == "frequency" else prefix + value
    return found


def extract(status: dict) -> dict:
    """Flatten a merged status dict (STS keys plus a nested "gsi") into the
    snapshot the history, the trigger rules and the UI all consume.

    Always returns a dict, even with no GSI data yet -- `receiving` is just
    False and every field is None, which is what a scanner that has only
    answered STS so far genuinely looks like.
    """
    gsi = status.get("gsi") or {}
    prop = gsi.get("Property", {})

    rssi = _number(prop.get("Rssi"))
    if rssi is not None and rssi <= RSSI_NOT_RECEIVING:
        rssi = None
    signal = _number(prop.get("Sig"))

    frequency = None
    for key in ("ConvFrequency", "SiteFrequency"):
        frequency = parse_frequency(gsi.get(key, {}).get("Freq"))
        if frequency is not None:
            break

    snapshot = {
        "receiving": _is_receiving(rssi, signal),
        # Whether the scanner is *playing* this, which is a different question
        # from whether it can hear it -- see `audible`. From the fast display
        # poll rather than from GSI, so it is present on every poll and three
        # times as fresh as anything under `gsi`.
        "audible": status.get("mute") is not None and str(status["mute"]).strip() == UNMUTED,
        "muted_known": status.get("mute") is not None,
        "scan_mode": _text(gsi.get("mode")),
        "system": _first(gsi, ("System",), "Name"),
        "system_type": _first(gsi, ("System",), "SystemType"),
        "department": _first(gsi, ("Department",), "Name"),
        "site": _first(gsi, ("Site",), "Name"),
        # Conventional: the channel is the frequency entry's own name.
        # Trunked: the talkgroup name is what the scanner's own CHANNEL
        # soft-key shows, so it stands in (same call as sensor.py makes).
        "channel": _first(gsi, ("ConvFrequency", "TGID"), "Name"),
        "frequency": frequency,
        "mod": _first(gsi, ("ConvFrequency", "Site"), "Mod"),
        "mode": classify_mode(gsi),
        "tgid": _first(gsi, ("TGID", "ConvFrequency"), "TGID"),
        "unit_id": _first(gsi, ("TGID", "ConvFrequency"), "U_Id"),
        # SAD is what was actually decoded on this reception; SAS is what the
        # channel is *set* to look for. Prefer the observation.
        "sub_audio": (
            _first(gsi, ("ConvFrequency", "SiteFrequency"), "SAD")
            or _first(gsi, ("ConvFrequency", "SiteFrequency"), "SAS")
        ),
        "service_type": _first(gsi, ("ConvFrequency",), "SvcType"),
        "p25_status": _text(prop.get("P25Status")),
        "rssi": rssi,
        "signal": int(signal) if signal is not None else None,
        # WxMode is only present at all while the scanner is in weather
        # monitor or alert mode (the "Depend on mode elements" matrix again),
        # so its absence is a real "not in weather mode" rather than missing
        # data -- see weather.py, which watches these two fields.
        "wx_mode": _text(gsi.get("WxMode", {}).get("Mode")),
        "wx_same": _text(gsi.get("WxMode", {}).get("SAME")),
        # Which screen the scanner is actually showing, as it names it itself
        # (ScannerInfo/@V_Screen: "conventional_scan", "wx_alert", ...). The
        # alert *state* and the alert *screen* are not the same thing -- the
        # screen is what parks the scanner and outlives the alert clearing,
        # which is why weather.py watches this and not just wx_alert.
        "v_screen": _text(gsi.get("v_screen")),
        # A modal popup covering whatever screen the scanner was on. It takes
        # the soft-key row with it -- STS sends no label row at all while one
        # is up -- so the keys it lists are the only ones known to do
        # anything. Empty list when there is no popup, which is the usual case.
        "popup_keys": _popup_keys(gsi),
        # Which scopes the scanner is held on, which is the one thing about
        # this failure that GSI states plainly: `mode` still reads "Scan
        # Mode" and the display still reads "Scanning...", because it *is*
        # scanning -- just only the one department. See stuck.py.
        "held_scopes": scope_holds(gsi),
    }
    snapshot["wx_alert"] = snapshot["wx_mode"] == WX_ALERT_MODE
    # The screen is what parks the scanner, so `V_Screen` decides whenever GSI
    # sends one, and the alert only stands in when it doesn't. As an
    # unconditional `or` this read a scanner that was scanning perfectly well
    # as parked: `DualWatch WX="Priority"` dips into the weather channel and
    # comes back on its own, and an alert that is still current leaves
    # `WxMode` in GSI across the dip -- so `wx_alert` stayed true on
    # `V_Screen="conventional_scan"`. weather.py then aimed a weather-screen
    # key at the scan screen, where soft1/soft2/soft3 are System/Dept/Channel
    # hold, and held the scanner on a department for eleven hours. Seen; see
    # docs/protocol-notes.md.
    snapshot["wx_parked"] = (
        snapshot["v_screen"] == WX_ALERT_SCREEN
        if snapshot["v_screen"] is not None
        else snapshot["wx_alert"]
    )
    snapshot["identity"] = identity(snapshot)
    return snapshot


# AVD and HLD both name their target by list index, and which GSI element
# carries that index depends on the scan mode exactly the way every other
# field here does: conventional scanning is on a frequency entry, trunked is
# on a talkgroup. Same lookup order as `extract`'s "channel" above, so "the
# channel" means one thing in this module.
#
# The target keywords are the spec's "tkd and 1st,2nd opt" table, which this
# project only has second-hand (see docs/protocol-notes.md). `CFREQ` is
# confirmed against hardware (2026-08-06); `TGID` is still only the
# documented spelling, never yet sent at a trunked system here. That is why
# post_avoid_current reports the command it sent verbatim: a wrong keyword
# comes back as AVD,NG, and the reply is the only way to tell.
#
# The scope keywords are not in this table because nothing here resolves a
# scope automatically -- but they are real and confirmed: `HLD,SYS,<index>,`
# and `HLD,DEPT,<index>,` both answered HLD,OK and released the hold
# (2026-08-15), which is how a scanner accidentally held on a system gets
# let go without pressing a positional soft key at it. The indices come off
# GSI's own `System`/`Department` elements.
CHANNEL_TARGETS: tuple[tuple[str, str], ...] = (
    ("ConvFrequency", "CFREQ"),
    ("TGID", "TGID"),
)


def channel_target(gsi: dict) -> dict | None:
    """The list-index target for the channel the scanner is on, or None.

    Used by both of the commands that name a target that way: AVD (avoid)
    and HLD (hold).

    None is the honest answer for a scanner sitting in a menu, in a quick
    search, or on anything else that isn't an entry in a list -- there is no
    index to name, so there is nothing AVD can be pointed at.
    """
    for element, tkw in CHANNEL_TARGETS:
        attributes = gsi.get(element) or {}
        index = _text(attributes.get("Index"))
        if not index:
            continue
        return {
            "tkw": tkw,
            "index": index,
            "element": element,
            "name": _text(attributes.get("Name")),
            # The parent list, so the result can be checked afterwards:
            # GLT,CFREQ takes the department index and is the only way to
            # find out what the scanner actually did (AVD,OK and KEY,OK are
            # both worthless as success signals -- see docs/protocol-notes).
            "department_index": _text((gsi.get("Department") or {}).get("Index")),
            # "On"/"Off" as the scanner reports it; a target that is already
            # avoided is worth saying rather than silently re-avoiding.
            "avoided": str(attributes.get("Avoid", "")).strip().lower() == "on",
        }
    return None


def audible(snapshot: dict) -> bool:
    """Whether the scanner was playing audio at this poll.

    The distinction RSSI cannot make. On a conventional channel an open
    squelch and audible audio are the same event, so either test answers. On a
    trunked digital system the receiver has signal whenever the *site* is
    active, while the scanner unmutes only for a talkgroup it is monitoring --
    measured on real hardware as eight stretches of RF in one minute against a
    single unmute. Anything that means "we heard this" has to be built on the
    mute flag.

    Falls back to the squelch test when the flag is missing, which is what a
    scanner that has not answered the display poll yet looks like, and what
    every older firmware would look like if one turns up without it.
    """
    if snapshot.get("muted_known"):
        return bool(snapshot.get("audible"))
    return bool(snapshot.get("receiving"))


def _is_receiving(rssi: float | None, signal: float | None) -> bool:
    """Squelch-open test.

    RSSI is the primary signal: the scanner reports a real dBm value while a
    transmission is coming in and its -999 sentinel otherwise (already mapped
    to None above). `Sig` -- the bar-graph level -- is the fallback for the
    case where RSSI is missing from the payload entirely, which the mode
    matrix allows; it reads 0 when idle.
    """
    if rssi is not None:
        return True
    if signal is not None:
        return signal > 0
    return False


# Fields that together say "this is the same transmission I was already
# hearing". Deliberately excludes unit_id: on a trunked system the talkgroup
# stays put while individual radios key up in turn, and treating each new
# unit id as a new call would shred one conversation into a dozen history
# rows. Unit ids seen during a call are collected on the record instead.
IDENTITY_FIELDS = ("system", "department", "channel", "frequency", "tgid")


def identity(snapshot: dict) -> str:
    return "|".join(str(snapshot.get(field) or "") for field in IDENTITY_FIELDS)


def identity_conflict(known: dict, snapshot: dict) -> str | None:
    """The field that says this is a *different* transmission, or None.

    Not string equality against `identity`, and the difference matters more
    than it looks. A blank field is the scanner not having told us yet, which
    is routine rather than exceptional: a trunked call reports its talkgroup a
    poll or two after the squelch opens, and the first poll of a call can
    carry the previous channel's name. Comparing whole identity strings made
    the talkgroup *arriving* look exactly like the talkgroup *changing*, so
    one transmission became two history rows at the poll where the scanner
    finally said what it was listening to.

    Only a field where both sides have a value and the values differ is
    evidence of anything. That is the same rule the record itself already
    follows -- the first non-empty reading wins and blanks are filled in
    later (see `CallTracker._extend`) -- and this makes the two agree.
    """
    for field in IDENTITY_FIELDS:
        old, new = known.get(field), snapshot.get(field)
        if old and new and old != new:
            return field
    return None


def describe(snapshot: dict) -> str:
    """Short human label, used in log lines and as the history row's title."""
    parts = [snapshot.get("channel"), snapshot.get("department"), snapshot.get("system")]
    label = " / ".join(part for part in parts if part)
    if label:
        return label
    if snapshot.get("frequency") is not None:
        return f"{snapshot['frequency']:.4f} MHz"
    return "unknown"


# GSI names the parked state two ways: as a mode, and as a per-element flag.
# Either will do -- what matters is that the scanner has stopped, which is
# the only thing HLD,OK doesn't tell you.
HOLD_MODE = "scan hold"
_HOLD_ELEMENTS = ("ConvFrequency", "TGID", "Department", "System")


def is_holding(gsi: dict) -> bool:
    """Whether the scanner is parked on something rather than scanning."""
    if (gsi.get("mode") or "").strip().lower() == HOLD_MODE:
        return True
    return any(
        str((gsi.get(element) or {}).get("Hold", "")).strip().lower() == "on"
        for element in _HOLD_ELEMENTS
    )


# The holds that narrow *what the scanner covers* rather than parking it on
# one channel. Deliberately not the same list as `_HOLD_ELEMENTS`, because
# the difference is the whole basis on which stuck.py decides whether a hold
# is worth waking somebody for:
#
# - A channel hold (ConvFrequency/TGID) is the one with a deliberate path in
#   this project -- the card's Hold button and the `sds200.hold` service both
#   set exactly that, via `channel_target`. Somebody listening to one channel
#   on purpose is the ordinary case, and alerting on it would fire on normal
#   use until the alert got ignored.
# - A System/Department/Site hold has no deliberate path here at all. Nothing
#   in the card, the integration or the add-on API sets one; the only ways in
#   are the unit's own front panel and the soft keys, which is precisely how
#   this scanner ended up scanning 40 CB channels for a day at a time. It
#   also hides *behind* `Mode="Scan Mode"` and a display reading
#   "Scanning...", so nothing else about the status looks wrong.
#
# Site is included on the same reasoning even though it has not been seen
# held: it narrows a trunked system to one site the same way.
SCOPE_HOLD_ELEMENTS = ("System", "Department", "Site")


def scope_holds(gsi: dict) -> tuple[str, ...]:
    """Which list scopes are held, in the order the scanner nests them.

    A tuple rather than a boolean so the event a watchdog fires can say what
    is held -- "System and Department" and "Department" call for the same
    reaction but not the same message -- and so a change in the set is
    distinguishable from the hold being released and re-taken.
    """
    return tuple(
        element
        for element in SCOPE_HOLD_ELEMENTS
        if str((gsi.get(element) or {}).get("Hold", "")).strip().lower() == "on"
    )
