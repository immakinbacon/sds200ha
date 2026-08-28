"""Fire something when the scanner hears something.

A rule is a match plus an action. Matching runs against the flat snapshot
`reception.extract` produces (so it sees the same fields the history rows
show, digital-mode slug included); the action is a webhook POST, a Home
Assistant service call, or a Home Assistant event.

**Why three action types rather than just a webhook.** A webhook needs
something on the other end to have been set up first; an HA *event* needs
nothing but an automation trigger, and is the cheapest way to get "when the
fire dispatch talkgroup keys up, do X" working. And a *service call* covers
the case where the whole point is one specific action (send a notification,
flip a light) and putting an automation in between is just ceremony. All
three cost about a dozen lines each here.

Both HA action types go through the Supervisor proxy at `http://supervisor/
core/api/...` with `SUPERVISOR_TOKEN`, which needs `homeassistant_api: true`
in config.yaml -- without it the token is issued but the proxy returns 401,
so `_ha_request` names that explicitly rather than surfacing a bare 401.

Matching is deliberately forgiving: every criterion is optional, empty ones
are ignored, and text ones are case-insensitive substring tests. A rule with
nothing filled in matches everything, which is a legitimate ("log every
call to my webhook") configuration rather than an error.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict

import aiohttp

logger = logging.getLogger(__name__)

SUPERVISOR_CORE_URL = "http://supervisor/core/api"

ACTION_TYPES = ("webhook", "ha_service", "ha_event")

# The edges a rule can fire on. "start"/"end" (and "both", which is the pair
# of them) come from the receive history; the weather ones come from
# weather.py and the stuck ones from stuck.py, both on the same `on_call`
# path. They are listed together because they are one dropdown in the UI, but
# they never overlap: "both" is deliberately *not* "everything", or every
# existing call rule would start firing on weather alerts the moment this
# shipped.
CALL_EVENTS = ("start", "end")

# A transcript arriving for a call that has already ended.
#
# Separate from "end" rather than folded into it, because the two happen at
# different times and cannot be merged without spoiling one of them: a call
# ends when the squelch has been shut for two polls, while its transcript
# lands after a model somewhere else has had its turn -- seconds later, or
# minutes behind a queue. Delaying "end" until the text existed would hold up
# every rule that has nothing to do with speech, and firing on "end" with the
# text not yet written would match nothing.
TRANSCRIPT_EVENTS = ("transcript",)
WX_EVENTS = ("wx_alert", "wx_clear")
# Unlike every other event here, "stuck" can fire repeatedly through one
# episode (stuck.REPEAT_S) rather than once on an edge -- the condition it
# reports lasts hours, and a single notification at 02:00 is the one that
# gets missed. A rule that wants it quieter has cooldown_s.
STUCK_EVENTS = ("stuck", "stuck_clear")
EVENTS = ("start", "end", "both", *TRANSCRIPT_EVENTS, *WX_EVENTS, *STUCK_EVENTS)

REQUEST_TIMEOUT_S = 10.0

# Cooldown is keyed by (rule, what was heard) rather than by rule alone: a
# rule watching a whole department should not have an action for engine 12
# swallow the one for engine 3 thirty seconds later. Bounded so a rule
# matching everything on a busy system can't grow this without limit.
MAX_COOLDOWN_KEYS = 2000


class TriggerError(Exception):
    """An action could not be delivered. Carries a message meant for the UI."""


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def _matches_text(criterion: str, value) -> bool:
    if not criterion:
        return True
    return criterion.lower() in _text(value).lower()


def _matches_frequency(rule_match: dict, snapshot: dict) -> bool:
    """Frequency match with a tolerance, in kHz.

    An exact float compare would essentially never fire: the scanner reports
    " 462.612500MHz" and a person types "462.6125", but they also type
    "462.612" or "462.61250". The default tolerance (see
    config_store.normalize_trigger) is 5 kHz, comfortably inside the spacing
    of any band this scanner covers while forgiving how the number was typed.
    """
    wanted = rule_match.get("frequency")
    if wanted in (None, ""):
        return True
    actual = snapshot.get("frequency")
    if actual is None:
        return False
    tolerance_mhz = (rule_match.get("frequency_tolerance_khz") or 0) / 1000.0
    return abs(float(actual) - float(wanted)) <= tolerance_mhz


def fires_on(rule_event: str, event: str) -> bool:
    """Does a rule set to `rule_event` fire on `event`? See EVENTS."""
    if rule_event == "both":
        return event in CALL_EVENTS
    return rule_event == event


def matches(rule: dict, snapshot: dict, event: str) -> bool:
    """Does `rule` fire for this snapshot/record on this event?"""
    if not rule.get("enabled", True):
        return False
    if not fires_on(rule.get("event", "start"), event):
        return False
    scanner_id = rule.get("scanner_id") or ""
    if scanner_id and snapshot.get("scanner_id") != scanner_id:
        return False

    criteria = rule.get("match") or {}

    modes = criteria.get("modes") or []
    if modes and snapshot.get("mode") not in modes:
        return False

    for field in ("system", "department", "channel", "tgid", "sub_audio", "system_type", "site"):
        if not _matches_text(criteria.get(field) or "", snapshot.get(field)):
            return False

    # Only a transcript that was actually written counts. A rule looking for
    # a word must not fire on a call whose transcript was rejected, or was
    # never attempted -- those carry a status and no text, and matching an
    # empty string against them would fire on everything.
    if criteria.get("transcript"):
        if not _matches_text(criteria["transcript"], snapshot.get("transcript")):
            return False

    unit = criteria.get("unit_id") or ""
    if unit:
        # On a history record this is the list of every unit heard during the
        # call; on a live snapshot it is the single current one. Accept both.
        units = snapshot.get("unit_ids") or ([snapshot["unit_id"]] if snapshot.get("unit_id") else [])
        if not any(_matches_text(unit, u) for u in units):
            return False

    if not _matches_frequency(criteria, snapshot):
        return False

    # Only meaningful on "end", where the duration is known. On "start" it is
    # always 0, so a rule combining min_duration with start would never fire
    # -- config_store.validate warns about exactly that.
    minimum = criteria.get("min_duration_s") or 0
    if minimum and event == "end" and (snapshot.get("duration") or 0) < minimum:
        return False

    return True


def render(template: str, snapshot: dict) -> str:
    """Substitute `{channel}`, `{frequency}`, ... into a user-supplied string.

    A missing or misspelled placeholder renders as an empty string rather
    than raising: a typo in a notification message should produce a slightly
    wrong notification, not a silently dead rule.
    """
    if "{" not in template:
        return template
    values = defaultdict(str, {k: "" if v is None else v for k, v in snapshot.items()})
    values["unit_ids"] = ", ".join(str(u) for u in snapshot.get("unit_ids") or [])
    try:
        return template.format_map(values)
    except (ValueError, IndexError):
        # Unbalanced braces or positional fields -- neither is recoverable,
        # and the raw text is more useful to the user than an exception.
        logger.warning("could not render trigger template %r; sending it as-is", template)
        return template


def render_data(data: dict, snapshot: dict) -> dict:
    """Render every string in an action's extra fields, however deep.

    Recursive because those values keep their own types now (see
    `config_store._data_map`), so a template can sit inside a list or a
    nested object -- and a number or a boolean has to come out the other side
    still a number or a boolean.
    """
    return {key: _render_value(value, snapshot) for key, value in (data or {}).items()}


def _render_value(value, snapshot):
    if isinstance(value, str):
        return render(value, snapshot)
    if isinstance(value, dict):
        return {key: _render_value(item, snapshot) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_value(item, snapshot) for item in value]
    return value


def payload_for(snapshot: dict, event: str) -> dict:
    """What every action sends: the reception, plus which edge fired it."""
    payload = {key: value for key, value in snapshot.items() if not key.startswith("_")}
    payload["event"] = event
    return payload


class TriggerEngine:
    """Holds the rules and delivers their actions.

    Owns one aiohttp session for the add-on's lifetime -- a session per
    delivery would mean a fresh TCP/TLS handshake for every action on a busy
    system.
    """

    def __init__(self, rules: list[dict] | None = None):
        self.rules: list[dict] = list(rules or [])
        self._session: aiohttp.ClientSession | None = None
        self._cooldowns: dict[tuple[str, str], float] = {}
        # Most recent snapshot per scanner, so "Test" in the UI can fire a
        # rule against something real instead of an invented example.
        self._last: dict[str, dict] = {}
        self.last_fired: dict[str, dict] = {}

    def set_rules(self, rules: list[dict]) -> None:
        self.rules = list(rules or [])
        live = {rule["id"] for rule in self.rules}
        self._cooldowns = {k: v for k, v in self._cooldowns.items() if k[0] in live}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
            )
        return self._session

    def on_call(self, event: str, record: dict) -> None:
        """Plugged into `ReceiveHistory.add_listener`, and called the same way
        by `weather.WeatherWatch` for the weather-alert edges.

        Synchronous (the history calls it inline from a poll callback), so
        delivery is spawned as a task -- a slow webhook must not stall the
        poll loop that produced the event.
        """
        self._last[record.get("scanner_id", "")] = record
        for rule in self.rules:
            if not matches(rule, record, event):
                continue
            if not self._claim_cooldown(rule, record):
                continue
            asyncio.create_task(self._deliver_and_log(rule, record, event))

    def _claim_cooldown(self, rule: dict, record: dict) -> bool:
        seconds = rule.get("cooldown_s") or 0
        if seconds <= 0:
            return True
        key = (rule["id"], record.get("identity", ""))
        now = time.monotonic()
        until = self._cooldowns.get(key)
        if until is not None and now < until:
            return False
        if len(self._cooldowns) >= MAX_COOLDOWN_KEYS:
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > now}
        self._cooldowns[key] = now + seconds
        return True

    async def _deliver_and_log(self, rule: dict, record: dict, event: str) -> None:
        try:
            detail = await self.deliver(rule, record, event)
            self.last_fired[rule["id"]] = {"at": time.time(), "ok": True, "detail": detail}
            logger.info("trigger %r fired on %r: %s", rule.get("name"), record.get("label"), detail)
        except Exception as exc:
            self.last_fired[rule["id"]] = {"at": time.time(), "ok": False, "detail": str(exc)}
            logger.warning("trigger %r failed on %r: %s", rule.get("name"), record.get("label"), exc)

    async def test(self, rule: dict, scanner_id: str = "") -> str:
        """Fire a rule now, ignoring its match and cooldown, against the last
        real reception if there is one.
        """
        snapshot = self._last.get(scanner_id) or next(iter(self._last.values()), None)
        if snapshot is None:
            snapshot = {
                "scanner_id": scanner_id, "label": "Test reception", "channel": "Test channel",
                "system": "Test system", "department": "Test department", "frequency": 462.6125,
                "mode": "analog", "tgid": None, "unit_ids": [], "duration": 0.0,
            }
        return await self.deliver(rule, snapshot, "test")

    async def deliver(self, rule: dict, snapshot: dict, event: str) -> str:
        action = rule.get("action") or {}
        kind = action.get("type")
        payload = payload_for(snapshot, event)
        if kind == "webhook":
            return await self._webhook(action, payload)
        if kind == "ha_service":
            return await self._ha_service(action, payload)
        if kind == "ha_event":
            return await self._ha_event(action, payload)
        raise TriggerError(f"unknown action type {kind!r}")

    async def _webhook(self, action: dict, payload: dict) -> str:
        url = render(action.get("url") or "", payload)
        if not url:
            raise TriggerError("no webhook URL configured")
        body = {**payload, **render_data(action.get("data") or {}, payload)}
        session = self._get_session()
        try:
            async with session.request(
                action.get("method") or "POST", url, json=body
            ) as response:
                text = (await response.text())[:200]
                if response.status >= 400:
                    raise TriggerError(f"{url} returned HTTP {response.status}: {text}")
                return f"POSTed to {url} (HTTP {response.status})"
        except aiohttp.ClientError as exc:
            raise TriggerError(f"could not reach {url}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise TriggerError(f"{url} did not respond within {REQUEST_TIMEOUT_S:.0f}s") from exc

    async def _ha_service(self, action: dict, payload: dict) -> str:
        domain = _text(action.get("domain"))
        service = _text(action.get("service"))
        if not domain or not service:
            raise TriggerError("a Home Assistant service needs both a domain and a service name")
        data = render_data(action.get("data") or {}, payload)
        if action.get("entity_id"):
            data["entity_id"] = render(action["entity_id"], payload)
        if action.get("include_reception"):
            # Under its own key rather than merged in: service schemas
            # reject unknown top-level fields, and this way a notify message
            # can still reach the whole reception via a template.
            data["reception"] = payload
        await self._ha_request("POST", f"services/{domain}/{service}", data)
        return f"called {domain}.{service}"

    async def _ha_event(self, action: dict, payload: dict) -> str:
        event_type = _text(action.get("event_type"))
        if not event_type:
            raise TriggerError("a Home Assistant event needs an event type")
        body = {**payload, **render_data(action.get("data") or {}, payload)}
        await self._ha_request("POST", f"events/{event_type}", body)
        return f"fired event {event_type}"

    async def _ha_request(self, method: str, path: str, body: dict) -> None:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise TriggerError(
                "no SUPERVISOR_TOKEN in the environment -- Home Assistant actions only work when "
                "the add-on runs under Supervisor."
            )
        url = f"{SUPERVISOR_CORE_URL}/{path}"
        session = self._get_session()
        try:
            async with session.request(
                method, url, json=body, headers={"Authorization": f"Bearer {token}"}
            ) as response:
                text = (await response.text())[:200]
                if response.status == 401:
                    raise TriggerError(
                        "Home Assistant rejected the add-on's token (401). This add-on needs "
                        "\"homeassistant_api: true\" in its config.yaml -- if you just updated, "
                        "restart the add-on."
                    )
                if response.status >= 400:
                    raise TriggerError(f"Home Assistant returned HTTP {response.status}: {text}")
        except aiohttp.ClientError as exc:
            raise TriggerError(f"could not reach Home Assistant: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise TriggerError(
                f"Home Assistant did not respond within {REQUEST_TIMEOUT_S:.0f}s"
            ) from exc
