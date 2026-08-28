"""What the scanner is actually avoiding, read back out of it.

`avoids.py` records what this add-on *did*. This module asks the scanner
what is *true*, which turns out to be answerable after all.

`GLT` reads the scanner's working copy and reports `Off`, `T-Avoid` or
`Avoid` per entry, at every level of the tree -- systems, sites,
departments, channels and talkgroups alike. So the avoid state is fully
enumerable: walk the tree and read the attribute. Earlier notes in this
project said no command enumerates what the scanner is avoiding; that was
wrong, and it mattered, because it is the only way to find an avoid nothing
here recorded.

Two things to keep in mind about what a read means:

* **`GLT` is the working copy, not flash.** It answers "what is the scanner
  avoiding right now", which is the question worth asking -- but a `T-Avoid`
  entry, and any `Avoid` that no keypad press has flushed, will be gone
  after the next power cycle. See docs/protocol-notes.md, "How avoids
  actually persist".
* **An unrecognized list type silently returns the `FL` list** rather than
  erroring, so a typo comes back looking like data. Every read here checks
  the tag it got back against the one it asked for.

The walk is not cheap: ~360 systems, ~1900 departments and one read per
department is a few thousand UDP round trips, about three and a half
minutes against real hardware. It is paced deliberately (`PACE_S`) so it
stays in the background of a scanner that is also being listened to, and
nothing calls it unless asked.
"""

from __future__ import annotations

import asyncio
import logging

from xml_lists import element_to_dicts

logger = logging.getLogger(__name__)

# Between reads. The scanner keeps answering without it, but this runs
# alongside the add-on's own GSI polling and a live audio session, and a
# few thousand back-to-back queries is not a neighbourly thing to do to a
# radio someone is listening to.
PACE_S = 0.03

# Anything that isn't this is an avoid of some kind.
OFF = "Off"
PERMANENT = "Avoid"
TEMPORARY = "T-Avoid"

# The child list under a department: talkgroups for a trunked system,
# channels for a conventional one. Getting this wrong is silent -- GLT,CFREQ
# on a trunked department returns zero entries rather than an error, so an
# audit that only ever asks for CFREQ skips every trunked system and looks
# like it covered everything.
CONVENTIONAL = "Conventional"

# Attempts per read. `send_xml_command` does not retry the way
# `send_command` does, and this is UDP: one dropped datagram is a read
# that fails rather than one that comes back wrong, and a single failure
# is enough to make the whole answer wrong. A failed department read
# leaves every record in it unconfirmable, and a failed `GLT,SYS` leaves
# the sweep with nothing to walk and no avoids to report.
ATTEMPTS = 2


def _describe(exc: Exception) -> str:
    """What to show for a read that failed.

    A read that times out raises `asyncio.TimeoutError`, whose `str()` is
    empty -- printed straight into the UI that came out as an error in
    empty parentheses, which told the reader nothing at all.
    """
    return str(exc).strip() or type(exc).__name__


async def _list(
    scanner, list_type: str, index: str | None = None, *, attempts: int = ATTEMPTS
) -> list[dict]:
    """One GLT read, retried, with the wrong-list-type trap checked.

    An unrecognized `list_type` comes back as the `FL` (favourites list)
    list with no error, so the tag on the entries is the only thing that
    says whether the question was understood.
    """
    command = f"GLT,{list_type}" + (f",{index}" if index is not None else "")
    for attempt in range(attempts):
        try:
            entries = element_to_dicts(await scanner.send_xml_command(command))
            break
        except Exception as exc:
            if attempt == attempts - 1:
                raise
            logger.debug("%s failed (%s), retrying", command, _describe(exc))
            await asyncio.sleep(PACE_S)
    if entries and list_type != "FL" and all(e.get("_tag") == "FL" for e in entries):
        logger.warning("%s answered with the FL list -- unrecognized list type?", command)
        return []
    return entries


def _entry(level: str, item: dict, **where) -> dict:
    return {
        "level": level,
        "index": item.get("Index"),
        "name": item.get("Name"),
        "avoid": item.get("Avoid"),
        "frequency": (item.get("Freq") or "").strip() or None,
        "tgid": item.get("TGID"),
        **where,
    }


async def sweep(scanner, *, pace: float = PACE_S) -> dict:
    """Walk the whole tree; return every entry the scanner is avoiding.

    Returns `{"avoided": [...], "counts": {...}, "failures": [...]}`, where
    each avoided entry carries the level it was found at and enough of its
    parents to name it. `counts` records how much was actually looked at,
    so a partial walk can't be mistaken for a clean one.
    """
    avoided: list[dict] = []
    failures: list[dict] = []
    counts = {"systems": 0, "sites": 0, "departments": 0, "channels": 0, "talkgroups": 0}

    async def read(list_type, index, context):
        try:
            return await _list(scanner, list_type, index)
        except Exception as exc:
            failures.append({"list": list_type, "index": index, "error": _describe(exc), **context})
            return []
        finally:
            await asyncio.sleep(pace)

    systems = await read("SYS", None, {})
    for system in systems:
        counts["systems"] += 1
        sidx = system.get("Index")
        where = {"system": system.get("Name"), "system_index": sidx}
        if system.get("Avoid") not in (None, OFF):
            avoided.append(_entry("system", system, **where))

        trunked = system.get("Type") != CONVENTIONAL
        if trunked:
            for site in await read("SITE", sidx, where):
                counts["sites"] += 1
                if site.get("Avoid") not in (None, OFF):
                    avoided.append(_entry("site", site, **where))

        for dept in await read("DEPT", sidx, where):
            counts["departments"] += 1
            didx = dept.get("Index")
            below = {**where, "department": dept.get("Name"), "department_index": didx}
            if dept.get("Avoid") not in (None, OFF):
                avoided.append(_entry("department", dept, **where))

            child = "TGID" if trunked else "CFREQ"
            for item in await read(child, didx, below):
                counts["talkgroups" if trunked else "channels"] += 1
                if item.get("Avoid") not in (None, OFF):
                    avoided.append(_entry("talkgroup" if trunked else "channel", item, **below))

    logger.info(
        "%s: avoid sweep read %d systems / %d departments / %d channels / %d talkgroups, "
        "found %d avoided, %d read failure(s)",
        getattr(scanner, "id", "?"), counts["systems"], counts["departments"],
        counts["channels"], counts["talkgroups"], len(avoided), len(failures),
    )
    return {"avoided": avoided, "counts": counts, "failures": failures}


def expected_state(record: dict) -> str:
    """What the scanner should say about a record, if the log is right.

    A cleared record is one that `AVD,...,3` un-avoided in the working copy
    and nothing has flushed yet, so the scanner should report it `Off` --
    it is still on the list only because it comes back at the next restart.
    """
    return OFF if record.get("cleared_at") else PERMANENT


async def check_records(scanner, records: list[dict], *, pace: float = PACE_S) -> list[dict]:
    """Read back just the channels the log claims, department by department.

    Cheap where `sweep` is not: one read per *distinct* department, which
    for a realistic log is a handful. This answers "is what I wrote down
    still true", and says nothing about avoids nobody wrote down -- that is
    what the sweep is for.
    """
    listings: dict[tuple[str, str], dict] = {}
    checked = []

    for record in records:
        dept = record.get("department_index")
        state = None
        detail = None
        # "The scanner never answered" and "the scanner answered, and it
        # is not what the log claims" are different results, and folding
        # the first into the second turned a moment's silence from the
        # radio into every record disagreeing at once.
        unknown = False

        if not dept:
            unknown = True
            detail = "no department index was recorded, so it can't be looked up"
        else:
            # Keyed by list type as well as department: the same index under
            # CFREQ and under TGID is not the same list.
            key = (record.get("tkw") or "CFREQ", dept)
            if key not in listings:
                try:
                    entries = await _list(scanner, key[0], dept)
                    listings[key] = {e.get("Index"): e for e in entries}
                except Exception as exc:
                    listings[key] = {"__error__": exc}
                await asyncio.sleep(pace)

            listing = listings[key]
            if "__error__" in listing:
                unknown = True
                detail = f"could not read the list back ({_describe(listing['__error__'])})"
            else:
                entry = listing.get(record.get("index"))
                if entry is None:
                    detail = "the index is not in that department's list"
                else:
                    state = entry.get("Avoid")
                    # The name is the useful half of this: it says the index
                    # is aimed where the record claims, not just that
                    # something there is avoided.
                    detail = entry.get("Name")

        expected = expected_state(record)
        checked.append({
            **record,
            "scanner_state": state,
            "expected_state": expected,
            "agrees": state == expected,
            "unknown": unknown,
            "detail": detail,
        })

    return checked


def unrecorded(avoided: list[dict], records: list[dict]) -> list[dict]:
    """Avoids the scanner is holding that no record accounts for.

    Matched on list index, which is what an avoid is actually addressed by.
    Cleared records still count as accounted for: the entry is expected to
    come back at the next restart, and it is already on the list saying so.
    """
    known = {r.get("index") for r in records if r.get("index")}
    return [a for a in avoided if a.get("index") not in known]
