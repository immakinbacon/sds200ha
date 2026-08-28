"""Persistent add-on settings, owned by the add-on rather than by Supervisor.

Settings used to live in Supervisor's Configuration tab, i.e. in
`/data/options.json`, which Supervisor writes and this add-on only reads.
That works, but it means the whole configuration is a YAML blob validated
against `config.yaml`'s `schema:` -- no per-field help, no way to test a
value before saving, and (the concrete papercut that kept biting) a newly
added schema field needs the Add-on Store's "Reload" before Supervisor will
even show it, so an option can silently never reach the add-on at all. See
`as_bool` below for the failure that came out of exactly that.

So settings now live in `/data/config.json`, written by the add-on's own
ingress web UI (see `settings_api.py`). `/data` is the add-on's persistent
volume, so this survives restarts, updates and rebuilds the same way
options.json does -- the difference is only who owns the file.

`/data/options.json` is still read *once*, on first start after the
upgrade, to seed `config.json` (see `_migrate_from_options`). `config.yaml`
deliberately keeps its legacy `scanners:`/`log_level` schema for that one
release: drop the schema and Supervisor strips the now-unknown keys out of
options.json on its next validation pass, which would throw away the config
we are trying to migrate before we ever get to read it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

import audio_tap
import history
import key_codes
import reception
import stt
import triggers
from protocol import DEFAULT_CONTROL_PORT, GSI_POLL_INTERVAL

logger = logging.getLogger(__name__)

CONFIG_PATH = "/data/config.json"
LEGACY_OPTIONS_PATH = "/data/options.json"

DEFAULT_RTSP_PORT = 554
LOG_LEVELS = ("trace", "debug", "info", "warning", "error")
MAX_SCANNERS = 8  # matches the RTP port range reserved in config.yaml

# How long the receive history keeps calls, and the bounds the UI enforces.
#
# Retention is by time, because "keep six months" is what people actually
# mean -- how many calls that is depends entirely on how busy the scanner is.
# Ten years is the ceiling: past that the number stops being a retention
# policy and starts being "forever", which `0` does not mean here.
DEFAULT_HISTORY_DAYS = history.DEFAULT_RETENTION_DAYS
MIN_HISTORY_DAYS = 1
MAX_HISTORY_DAYS = 3650

# The disk backstop behind the window, not the primary control -- it only
# bites if the scanner produces this many calls *inside* the retention
# window. Roughly 300 bytes a row, so the ceiling is a few gigabytes.
DEFAULT_MAX_HISTORY_RECORDS = history.DEFAULT_MAX_RECORDS
MIN_HISTORY_RECORDS = 100
MAX_HISTORY_RECORDS = 10_000_000

# The GSI poll drives the receive history, so it is exposed as a setting
# (see history.py's module docstring for the tradeoff). Floors at 1s: below
# that this firmware's GSI responses start timing out more often than they
# succeed, which loses more calls than the faster polling catches.
MIN_GSI_INTERVAL = 1.0
MAX_GSI_INTERVAL = 30.0

MAX_TRIGGERS = 50
DEFAULT_FREQUENCY_TOLERANCE_KHZ = 5.0

# Longest "return to scanning after a weather alert" wait (an hour). Anything
# beyond that is indistinguishable from leaving it switched off.
MAX_WX_RETURN_S = 3600

# Ten minutes before a held scope is worth reporting. Long enough to sit out
# somebody holding a system from the front panel to listen to something, and
# far short of the eleven hours this went unnoticed for.
DEFAULT_STUCK_HOLD_S = 600
# A week. The silence check is measured in hours, not seconds, and a bound
# this loose exists only to keep a hand-edited config from storing something
# meaningless -- the UI offers sensible steps.
MAX_STUCK_S = 604800

# Returned by the API in place of a stored PoE password, and accepted back
# on save to mean "leave it alone". The UI never receives the real password,
# so a saved one can't leak through a browser session, a screenshot or the
# network tab -- while still letting every *other* field of that scanner be
# edited without retyping it.
PASSWORD_SENTINEL = "__stored__"

POE_FIELDS = ("poe_reset_host", "poe_reset_username", "poe_reset_password", "poe_reset_interface")


def slugify(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")


def as_bool(value, default: bool) -> bool:
    """Coerce a setting to a bool, treating string spellings correctly.

    NOT `bool(value)`: a non-empty string is truthy, so `bool("false")` is
    **True**. Settings arrive as JSON from an HTTP form and (for migrated
    ones) from `/data/options.json`, and a value that reaches us as the
    string "false" rather than a JSON boolean would silently invert the
    setting. That is exactly how `poe_reset_use_ssl: false` kept dialling
    https:// anyway, with the log and the UI both showing "false" -- back
    when Supervisor was the one writing the file and a field it did not yet
    know about got passed through as a raw string.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("false", "no", "off", "0", ""):
            return False
        if text in ("true", "yes", "on", "1"):
            return True
        return default
    return bool(value)


def _as_port(value, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def _clamp(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)


def _int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)


def normalize_scanner(raw: dict) -> dict:
    """One scanner, every field present and of the right type.

    Normalizing on the way *in* (rather than defaulting at each use site)
    means the running config, the file on disk and what the UI renders are
    all the same shape, so "what is this scanner actually configured as"
    has exactly one answer -- and the reconfigure diff in `manager.py` can
    be a plain dict comparison.
    """
    return {
        "name": _text(raw.get("name")),
        "host": _text(raw.get("host")),
        "control_port": _as_port(raw.get("control_port"), DEFAULT_CONTROL_PORT),
        "rtsp_port": _as_port(raw.get("rtsp_port"), DEFAULT_RTSP_PORT),
        # Per scanner, not global: one scanner might be a busy trunked system
        # worth polling hard for the history, another a quiet weather channel
        # where the extra traffic buys nothing.
        "gsi_poll_interval": _clamp(
            raw.get("gsi_poll_interval"), GSI_POLL_INTERVAL, MIN_GSI_INTERVAL, MAX_GSI_INTERVAL
        ),
        "poe_reset_host": _text(raw.get("poe_reset_host")),
        "poe_reset_username": _text(raw.get("poe_reset_username")),
        "poe_reset_password": _text(raw.get("poe_reset_password")),
        "poe_reset_interface": _text(raw.get("poe_reset_interface")),
        "poe_reset_verify_ssl": as_bool(raw.get("poe_reset_verify_ssl"), True),
        "poe_reset_use_ssl": as_bool(raw.get("poe_reset_use_ssl"), True),
        "auto_reboot_on_audio_failure": as_bool(raw.get("auto_reboot_on_audio_failure"), False),
        "auto_reboot_on_control_failure": as_bool(raw.get("auto_reboot_on_control_failure"), False),
        # 0 means "leave the scanner in the alert", which is the scanner's own
        # behaviour and so the safe default for an upgrade.
        "wx_return_to_scan_s": _int(raw.get("wx_return_to_scan_s"), 0, 0, MAX_WX_RETURN_S),
        "wx_return_to_scan_key": (
            key if (key := _text(raw.get("wx_return_to_scan_key"))) in key_codes.KEY_CODES else ""
        ),
        # On by default, unlike every other new behaviour here, because it
        # only ever reports: it presses no key and changes nothing on the
        # scanner, so the worst an unwanted one does is write a log line. The
        # thing it watches for went unnoticed for eleven hours twice and a
        # day once, which is what a default of 0 would buy.
        "stuck_hold_s": _int(raw.get("stuck_hold_s"), DEFAULT_STUCK_HOLD_S, 0, MAX_STUCK_S),
        # Off by default, because "quiet" is a property of the site rather
        # than of the scanner: a threshold that is obviously wrong for a
        # city trunked system is obviously right for one watching a single
        # rural channel, and there is no default that is not wrong for one
        # of them.
        "stuck_silence_s": _int(raw.get("stuck_silence_s"), 0, 0, MAX_STUCK_S),
        # Off by default, and per scanner rather than global: transcription
        # costs CPU on whatever runs the model, and a scanner parked on a
        # weather channel or one that is not set up yet has nothing worth
        # spending it on. Where the model lives is the global `transcribe`
        # section -- this is only which scanners are fed to it.
        "transcribe_enabled": as_bool(raw.get("transcribe_enabled"), False),
        # Empty means every mode. Exists because analog and digital are
        # different problems for a speech model -- P25 audio has already been
        # through a parametric vocoder -- so "worth it on the analog channels,
        # not on the trunk" is a real answer someone may need to express.
        "transcribe_modes": _text(raw.get("transcribe_modes")),
        # Log only calls this scanner actually played audio for.
        #
        # On by default. It changes what the receive history *means* -- from
        # every time the squelch-open test fired to every time the scanner
        # unmuted -- and the second is what someone reading a scanner log
        # actually wants. On a conventional channel the two are much the same
        # thing; on a trunked digital system the difference is other people's
        # talkgroups, which is most of the log and none of the interest.
        #
        # Never applied while the feed looks dead: see transcribe.on_call.
        # Hearing nothing is not evidence that nothing was said, and a
        # scanner whose audio path has wedged would otherwise quietly delete
        # its entire history.
        "require_audio": as_bool(raw.get("require_audio"), True),
    }


_TEXT_CRITERIA = ("transcript", "system", "department", "site", "channel", "tgid",
                  "unit_id", "sub_audio",
                  "system_type")


def normalize_trigger(raw: dict, index: int = 0) -> dict:
    """One trigger rule, every field present and of the right type.

    Same reasoning as `normalize_scanner`: normalizing on the way in means
    the file, the running rules and what the UI renders are one shape.

    Rule ids are generated here when missing rather than in the UI. The UI
    can reorder and delete rules freely, and an id assigned client-side would
    be lost by anything that edits the file directly -- while the cooldown
    bookkeeping and the "last fired" display in `TriggerEngine` are both
    keyed by it.
    """
    match = raw.get("match") or {}
    action = raw.get("action") or {}
    action_type = _text(action.get("type")).lower()
    event = _text(raw.get("event")).lower()

    frequency = match.get("frequency")
    try:
        frequency = float(frequency) if _text(frequency) else None
    except (TypeError, ValueError):
        frequency = None

    modes = [m for m in (match.get("modes") or []) if m in reception.MODES]

    return {
        "id": _text(raw.get("id")) or f"rule-{index + 1}-{os.urandom(3).hex()}",
        "name": _text(raw.get("name")),
        "enabled": as_bool(raw.get("enabled"), True),
        "scanner_id": _text(raw.get("scanner_id")),
        "event": event if event in triggers.EVENTS else "start",
        "cooldown_s": _int(raw.get("cooldown_s"), 30, 0, 86400),
        "match": {
            **{field: _text(match.get(field)) for field in _TEXT_CRITERIA},
            "modes": modes,
            "frequency": frequency,
            "frequency_tolerance_khz": _clamp(
                match.get("frequency_tolerance_khz"), DEFAULT_FREQUENCY_TOLERANCE_KHZ, 0, 1000
            ),
            "min_duration_s": _clamp(match.get("min_duration_s"), 0, 0, 3600),
        },
        "action": {
            "type": action_type if action_type in triggers.ACTION_TYPES else "ha_event",
            "url": _text(action.get("url")),
            "method": (_text(action.get("method")).upper() or "POST"),
            "domain": _text(action.get("domain")),
            "service": _text(action.get("service")),
            "entity_id": _text(action.get("entity_id")),
            "event_type": _text(action.get("event_type")) or "sds200_reception",
            "include_reception": as_bool(action.get("include_reception"), False),
            "data": _data_map(action.get("data")),
        },
    }


# How deep an extra-field value may nest. Deep enough for the shapes a real
# service call takes and no deeper, so a hand-edited config.json can't hand
# the JSON encoder something pathological.
MAX_DATA_DEPTH = 5

# A JSON value can only start with one of these. Checked before trying to
# parse, so the common case -- a template like "{channel} on {system}" --
# isn't run through json.loads on every save just to fail.
_JSON_STARTS = "-0123456789tfn[{\""


def _data_map(value) -> dict:
    """Extra key/value pairs sent with an action.

    Values keep the type they are meant to have. That matters because these
    are service-call fields: a script field declared as a number, or a
    boolean, is rejected by Home Assistant when it arrives as "3" or "true",
    and coercing everything to a string is what this used to do.

    Values arrive two ways. From the add-on's own config.json (and the YAML
    it was seeded from) they are already typed -- `priority: 3` is an int --
    and are kept as they are. From the UI's two-column form everything is
    text, so a string that parses as JSON takes the parsed type; that is how
    the form expresses a number, a boolean or a list at all.
    """
    if not isinstance(value, dict):
        return {}
    return {_text(k): _data_value(v) for k, v in value.items() if _text(k)}


def _data_value(value, depth: int = 0):
    """One extra-field value: JSON-parsed if it is text that looks like JSON,
    otherwise left alone.

    Anything that doesn't parse stays the string it was, which is what keeps
    templates working -- `{channel}` starts with a brace and is not JSON, and
    has to survive as text for `triggers.render` to substitute into. Wrapping
    a value in quotes ("911") is therefore also how you force a string that
    would otherwise be read as a number.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text or text[0] not in _JSON_STARTS:
            return value
        try:
            parsed = json.loads(text)
        except ValueError:
            return value
        return _sanitize_data(parsed, depth)
    return _sanitize_data(value, depth)


def _sanitize_data(value, depth: int = 0):
    """Make an already-typed value safe to store and to send as JSON.

    Deliberately does not re-parse strings: only the value the user typed is
    treated as maybe-JSON, so a string *inside* a parsed object stays the
    string JSON said it was.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        if depth >= MAX_DATA_DEPTH:
            return {}
        return {
            _text(k): _sanitize_data(v, depth + 1) for k, v in value.items() if _text(k)
        }
    if isinstance(value, (list, tuple)):
        if depth >= MAX_DATA_DEPTH:
            return []
        return [_sanitize_data(v, depth + 1) for v in value]
    return _text(value)


def normalize(raw: dict) -> dict:
    level = _text(raw.get("log_level")).lower()
    history_cfg = raw.get("history") or {}
    transcribe_cfg = raw.get("transcribe") or {}
    return {
        "log_level": level if level in LOG_LEVELS else "info",
        "scanners": [normalize_scanner(s) for s in raw.get("scanners") or []],
        # `limit` (a bare record count) was the old key here. It is not read:
        # under time-based retention its stored value -- 500 by default --
        # would cap a six-month window at five hundred calls, which is the
        # opposite of what anyone setting it meant. Dropping it resets
        # everyone to the new defaults, deliberately.
        "history": {
            "enabled": as_bool(history_cfg.get("enabled"), True),
            "retention_days": _int(
                history_cfg.get("retention_days"),
                DEFAULT_HISTORY_DAYS, MIN_HISTORY_DAYS, MAX_HISTORY_DAYS,
            ),
            "max_records": _int(
                history_cfg.get("max_records"),
                DEFAULT_MAX_HISTORY_RECORDS, MIN_HISTORY_RECORDS, MAX_HISTORY_RECORDS,
            ),
        },
        # Where speech-to-text happens. Global rather than per-scanner: the
        # model is a service on the network, and pointing two scanners at two
        # different ones is a configuration nobody has asked for. *Which*
        # scanners get transcribed is per-scanner and off by default.
        "transcribe": {
            "backend": (
                backend if (backend := _text(transcribe_cfg.get("backend")).lower())
                in stt.BACKENDS else stt.DEFAULT_BACKEND
            ),
            # Empty means "not configured", which is the default and a normal
            # state -- not an error to warn about.
            "url": _text(transcribe_cfg.get("url")),
            "model": _text(transcribe_cfg.get("model")) or "small.en",
            "language": _text(transcribe_cfg.get("language")) or "en",
            # Vocabulary bias. Blunt -- it nudges rather than constrains, and
            # an overloaded one can induce the model to hallucinate its own
            # terms -- so a handful of local agency and street names, not a
            # gazetteer.
            "prompt": _text(transcribe_cfg.get("prompt")),
            # The longest single clip sent to the model. A backstop for the
            # voice detector failing to close, not a limit on how long anyone
            # may speak -- a capped clip is still transcribed, so the cost is
            # a long message split across two rows rather than lost.
            # How long the channel has to stay quiet before a transmission
            # counts as over. Too short cuts people off mid-sentence at an
            # ordinary pause; too long merges separate transmissions into one
            # clip, which is the lesser harm of the two.
            # Whether the add-on resamples to 16kHz or declares 8kHz and
            # leaves it to the server. Ours is a linear interpolation, which
            # is worse than a real filter but certain; the server's is better
            # if it does it at all. Which is true of a given server is not
            # knowable from here.
            "upsample": as_bool(transcribe_cfg.get("upsample"), True),
            "silence_gap_s": _clamp(
                transcribe_cfg.get("silence_gap_s"),
                audio_tap.DEFAULT_CLOSE_FRAMES * audio_tap.FRAME_S,
                audio_tap.MIN_SILENCE_GAP_S,
                audio_tap.MAX_SILENCE_GAP_S,
            ),
            "max_segment_s": _clamp(
                transcribe_cfg.get("max_segment_s"),
                audio_tap.DEFAULT_MAX_SEGMENT_S,
                audio_tap.MIN_MAX_SEGMENT_S,
                audio_tap.MAX_MAX_SEGMENT_S,
            ),
        },
        "triggers": [normalize_trigger(t, i) for i, t in enumerate(raw.get("triggers") or [])],
    }


def validate(config: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for an already-normalized config.

    Errors block the save: they describe a config the add-on cannot run.
    Warnings do not, because the runtime already copes with them -- a
    partial poe_reset is ignored with a log line rather than crashing, and
    refusing to save it would strand someone mid-edit who filled in the
    host but hasn't reached the password field yet. They're shown in the UI
    so the "why is the reboot button doing nothing" case is visible at the
    point it's created, instead of only in the add-on log.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if len(config["scanners"]) > MAX_SCANNERS:
        errors.append(
            f"{len(config['scanners'])} scanners configured, but only {MAX_SCANNERS} RTP audio "
            f"ports are reserved by the add-on. Remove some, or widen the port range in "
            f"config.yaml and rebuild."
        )

    seen: dict[str, int] = {}
    for i, scanner in enumerate(config["scanners"], start=1):
        label = f"Scanner {i}"
        if not scanner["name"]:
            errors.append(f"{label}: name is required.")
        if not scanner["host"]:
            errors.append(f"{label} ({scanner['name'] or 'unnamed'}): host is required.")

        scanner_id = slugify(scanner["name"])
        if scanner["name"] and not scanner_id:
            # e.g. a name of "---": every character is stripped by slugify,
            # leaving an empty id that would collide with any other such
            # name and break every /scanners/{id} route for it.
            errors.append(
                f"{label}: name {scanner['name']!r} contains no letters or digits, so it has no "
                f"usable id."
            )
        elif scanner_id in seen:
            errors.append(
                f"{label} ({scanner['name']!r}) and scanner {seen[scanner_id]} produce the same "
                f"id {scanner_id!r}. Names must differ by more than punctuation or case."
            )
        elif scanner_id:
            seen[scanner_id] = i

        provided = [f for f in POE_FIELDS if scanner[f]]
        if provided and len(provided) != len(POE_FIELDS):
            missing = [f.removeprefix("poe_reset_") for f in POE_FIELDS if not scanner[f]]
            warnings.append(
                f"{label} ({scanner['name'] or 'unnamed'}): PoE reset is incomplete (missing "
                f"{', '.join(missing)}) and will be ignored. Reboot and auto-recovery will not "
                f"work for this scanner."
            )
        elif not provided and (
            scanner["auto_reboot_on_audio_failure"] or scanner["auto_reboot_on_control_failure"]
        ):
            warnings.append(
                f"{label} ({scanner['name'] or 'unnamed'}): auto-recovery is switched on but "
                f"there is no PoE reset configured for it to use, so nothing will happen."
            )

    gap = (config.get("transcribe") or {}).get("silence_gap_s")
    if gap and gap < audio_tap.ADVISED_MIN_SILENCE_GAP_S:
        # Not an error: a site with clipped, disciplined traffic may mean it.
        # But it is the setting most likely to have been turned down while
        # chasing a symptom it then causes, and nothing said so.
        warnings.append(
            f"A transmission is ended after {gap:g}s of quiet, which is inside an ordinary "
            f"pause between phrases. Expect one transmission to be logged as two clips and two "
            f"transcripts. 1.2s is the default; raise it unless this channel's traffic is "
            f"unusually clipped."
        )

    validate_triggers(config, errors, warnings)
    return errors, warnings


def validate_triggers(config: dict, errors: list[str], warnings: list[str]) -> None:
    """Same errors/warnings split as `validate`: an error is a rule the engine
    cannot deliver at all, a warning is one that will deliver but probably
    not do what the person meant.
    """
    rules = config.get("triggers") or []
    if len(rules) > MAX_TRIGGERS:
        errors.append(
            f"{len(rules)} actions configured, but only {MAX_TRIGGERS} are allowed. Every action is "
            f"evaluated on every reception, so the limit keeps a busy system's poll loop quick."
        )

    scanner_ids = {slugify(s["name"]) for s in config.get("scanners") or [] if s.get("name")}
    for i, rule in enumerate(rules, start=1):
        label = f"Action {i}" + (f" ({rule['name']})" if rule.get("name") else "")
        action = rule["action"]

        if action["type"] == "webhook" and not action["url"]:
            errors.append(f"{label}: a webhook action needs a URL.")
        elif action["type"] == "webhook" and not action["url"].lower().startswith(("http://", "https://")):
            errors.append(f"{label}: the webhook URL must start with http:// or https://.")
        if action["type"] == "ha_service" and not (action["domain"] and action["service"]):
            errors.append(
                f"{label}: a Home Assistant service action needs both a domain and a service "
                f"(e.g. notify / persistent_notification)."
            )
        if action["type"] == "ha_event" and not action["event_type"]:
            errors.append(f"{label}: a Home Assistant event action needs an event type.")

        if rule["scanner_id"] and rule["scanner_id"] not in scanner_ids:
            warnings.append(
                f"{label}: it is limited to scanner {rule['scanner_id']!r}, which no longer "
                f"exists, so it will never fire. Set it to \"any scanner\" or pick a current one."
            )

        # The one combination that looks reasonable and silently never fires:
        # duration is by definition 0 at the moment a call starts, and a
        # weather alert isn't a call at all.
        if rule["match"]["min_duration_s"] and rule["event"] not in ("end", "both"):
            warnings.append(
                f"{label}: a minimum duration only applies when a call *ends* -- nothing else has "
                f"a duration yet, so this action will never fire. Switch it to \"when a call "
                f"ends\", or clear the minimum duration."
            )

        if not rule["name"]:
            warnings.append(f"{label}: give it a name so it is identifiable in the log.")


def redact(config: dict) -> dict:
    """A copy safe to hand to the browser: stored passwords become the
    sentinel. See PASSWORD_SENTINEL.
    """
    out = normalize(config)
    for scanner in out["scanners"]:
        if scanner["poe_reset_password"]:
            scanner["poe_reset_password"] = PASSWORD_SENTINEL
    return out


def unredact(incoming: dict, stored: dict) -> dict:
    """Put back the real passwords the UI was never given.

    Matched by scanner id rather than list position: reordering or deleting
    a scanner in the UI shifts every later index, and matching by position
    would then hand one scanner another's router password.
    """
    by_id = {slugify(s["name"]): s for s in stored.get("scanners", []) if s.get("name")}
    out = normalize(incoming)
    for scanner in out["scanners"]:
        if scanner["poe_reset_password"] != PASSWORD_SENTINEL:
            continue
        previous = by_id.get(slugify(scanner["name"]))
        scanner["poe_reset_password"] = (previous or {}).get("poe_reset_password", "")
    return out


class ConfigStore:
    """Reads and writes the settings file, seeding it from the legacy
    Supervisor options on first run.
    """

    def __init__(self, path: str = CONFIG_PATH, legacy_options_path: str = LEGACY_OPTIONS_PATH):
        self.path = path
        self.legacy_options_path = legacy_options_path

    def load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    return normalize(json.load(f))
            except (OSError, ValueError):
                # Not fatal, and deliberately not "start with no scanners":
                # a truncated or hand-edited config.json would otherwise
                # look identical to a fresh install, and the add-on would
                # come up serving nothing with only a stack trace to say
                # why. Re-raise so the container fails loudly instead.
                logger.exception("could not read %s", self.path)
                raise
        migrated = self._migrate_from_options()
        self.save(migrated)
        return migrated

    def _migrate_from_options(self) -> dict:
        if not os.path.exists(self.legacy_options_path):
            return normalize({})  # fresh install, or local dev outside Supervisor
        try:
            with open(self.legacy_options_path) as f:
                options = json.load(f)
        except (OSError, ValueError):
            logger.exception(
                "could not read %s to migrate the old add-on options; starting empty",
                self.legacy_options_path,
            )
            return normalize({})
        config = normalize(options)
        if config["scanners"]:
            logger.warning(
                "migrated %d scanner(s) from the add-on's Configuration tab into %s -- settings "
                "are now edited in the add-on's own web UI (Open Web UI), and the Configuration "
                "tab is no longer read",
                len(config["scanners"]), self.path,
            )
        return config

    def save(self, config: dict) -> dict:
        """Write atomically: a crash or a full /data mid-write would
        otherwise leave a half-written config.json, which `load()` refuses
        to start from.
        """
        config = normalize(config)
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(config, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return config
