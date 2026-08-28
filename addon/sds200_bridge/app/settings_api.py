"""The add-on's own web UI and the API behind it.

Reached through Home Assistant ingress ("Open Web UI" on the add-on page),
so it is authenticated by HA itself and served under a per-session path
prefix. Everything the page requests is therefore addressed *relatively*
("static/app.js", "api/settings") -- an absolute "/api/settings" would
resolve against Home Assistant's own origin, not the ingress prefix, and
404.

The page has grown from a settings form into the add-on's control surface
(control the scanner, listen to it, browse the receive history, manage
actions), so this module now serves three API groups. They stay separate --
`/api/settings`, `/api/history`, `/api/triggers` -- so each tab saves and
reloads on its own: editing an action should not make a half-finished scanner
edit on another tab savable, and vice versa.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

import audio_tap
import config_store
import reception
import stt
import triggers as triggers_module
from config_store import ConfigStore
from manager import set_log_level

# One second of roughly 440Hz square wave, as mu-law codes picked straight
# out of the table (0xB0 and 0x30 are about +/-3900) rather than run through
# an encoder this module would otherwise have no use for.
#
# Tone rather than silence: a speech-to-text server may legitimately return
# nothing at all for silence, which would be indistinguishable from a
# connection that never worked -- and telling those two apart is the entire
# job of the test button.
_TONE_HALF_CYCLE = audio_tap.SAMPLE_RATE // 880
_TEST_CLIP = bytes(
    0xB0 if (n // _TONE_HALF_CYCLE) % 2 == 0 else 0x30
    for n in range(audio_tap.SAMPLE_RATE)
)

logger = logging.getLogger(__name__)

WWW_DIR = Path(__file__).resolve().parent / "www"

STORE_KEY = web.AppKey("config_store", ConfigStore)
MANAGER_KEY = web.AppKey("manager", object)
HISTORY_KEY = web.AppKey("history", object)
ENGINE_KEY = web.AppKey("trigger_engine", object)

# The most rows one /api/history request will return. A ceiling on the page,
# not on the log: the store holds months, and the page scrolls.
MAX_HISTORY_PAGE = 1000


def add_routes(app: web.Application, store: ConfigStore, manager, history, trigger_engine) -> None:
    app[STORE_KEY] = store
    app[MANAGER_KEY] = manager
    app[HISTORY_KEY] = history
    app[ENGINE_KEY] = trigger_engine
    app.router.add_get("/", index)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_post("/api/settings", post_settings)
    app.router.add_get("/api/history", get_history)
    app.router.add_get("/api/history/{call_id}/audio", get_call_audio)
    app.router.add_delete("/api/history", delete_history)
    app.router.add_get("/api/triggers", get_triggers)
    app.router.add_post("/api/triggers", post_triggers)
    app.router.add_post("/api/triggers/test", post_trigger_test)
    app.router.add_post("/api/transcribe/test", post_transcribe_test)
    app.router.add_static("/static", WWW_DIR)


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        WWW_DIR / "index.html",
        # The page is tiny and changes with every add-on rebuild; a cached
        # copy paired with a newly deployed settings.js is a confusing way
        # to spend an afternoon.
        headers={"Cache-Control": "no-store"},
    )


def _runtime(manager) -> list[dict]:
    """Live state per running scanner, so the settings page can show whether
    a host it was given actually answers -- the single most common thing to
    get wrong, and otherwise only visible in the add-on log.
    """
    return [
        {
            "id": scanner_id,
            "name": conn.name,
            "reachable": conn.is_reachable(),
            "audio": bool(manager.audio_bridges.get(scanner_id)),
            "can_reboot": bool(
                manager.audio_bridges.get(scanner_id)
                and manager.audio_bridges[scanner_id].has_reboot_mechanism()
            ),
        }
        for scanner_id, conn in manager.scanners.items()
    ]


async def get_settings(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    manager = request.app[MANAGER_KEY]
    config = store.load()
    errors, warnings = config_store.validate(config)
    return web.json_response(
        {
            "config": config_store.redact(config),
            "errors": errors,
            "warnings": warnings,
            "runtime": _runtime(manager),
            "max_scanners": config_store.MAX_SCANNERS,
            "log_levels": list(config_store.LOG_LEVELS),
            "password_sentinel": config_store.PASSWORD_SENTINEL,
            "gsi_interval_bounds": [config_store.MIN_GSI_INTERVAL, config_store.MAX_GSI_INTERVAL],
            "history_days_bounds": [config_store.MIN_HISTORY_DAYS, config_store.MAX_HISTORY_DAYS],
            "history_records_bounds": [
                config_store.MIN_HISTORY_RECORDS,
                config_store.MAX_HISTORY_RECORDS,
            ],
        }
    )


async def _save(request: web.Request, payload: dict) -> tuple[dict, list[str], list[str], dict | None]:
    """Merge a partial payload over what's stored, validate, save and apply.

    Partial on purpose. The page saves each tab independently, so the actions
    tab POSTs only `triggers` and the settings tab only `scanners`/
    `log_level`/`history`. Normalizing the payload on its own would default
    every key it *didn't* send back to empty -- i.e. saving a scanner would
    silently delete every action. Merging over the stored config first means a
    key that wasn't sent keeps its stored value.
    """
    store = request.app[STORE_KEY]
    manager = request.app[MANAGER_KEY]

    stored = store.load()
    config = config_store.unredact({**stored, **payload}, stored)
    errors, warnings = config_store.validate(config)
    if errors:
        # Nothing written or applied: a settings form that half-saves is
        # worse than one that refuses.
        return config, errors, warnings, None

    store.save(config)
    set_log_level(config["log_level"])
    changes = await manager.apply(config)
    return config, errors, warnings, changes


async def _json_body(request: web.Request) -> dict:
    try:
        payload = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="expected a JSON body")
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="expected a JSON object")
    return payload


async def post_settings(request: web.Request) -> web.Response:
    manager = request.app[MANAGER_KEY]
    payload = await _json_body(request)
    config, errors, warnings, changes = await _save(request, payload)
    if changes is None:
        return web.json_response(
            {"ok": False, "errors": errors, "warnings": warnings}, status=400
        )
    logger.info("settings saved from the web UI: %s", changes)
    return web.json_response(
        {
            "ok": True,
            "config": config_store.redact(config),
            "errors": [],
            "warnings": warnings,
            "changes": changes,
            "runtime": _runtime(manager),
        }
    )


async def get_call_audio(request: web.Request) -> web.StreamResponse:
    """The kept audio for one call, as a WAV the browser can play.

    Exists so a transcript can be judged against what was actually said,
    which is the only way to tell a good one from a plausible invention --
    and plausible inventions are this model's characteristic failure on
    audio this narrow. Only recent calls have a clip (see
    transcribe.ClipStore), so a 404 here is ordinary rather than an error.
    """
    history_store = request.app[HISTORY_KEY]
    manager = request.app[MANAGER_KEY]
    try:
        call_id = int(request.match_info["call_id"])
    except (TypeError, ValueError):
        raise web.HTTPNotFound() from None

    record = history_store.record(call_id)
    clip = record.get("clip") if record else None
    transcriber = getattr(manager, "transcriber", None)
    if not clip or transcriber is None:
        raise web.HTTPNotFound()
    path = Path(transcriber.clips.path(clip))
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Type": "audio/wav"})


async def post_transcribe_test(request: web.Request) -> web.Response:
    """Try the configured speech-to-text server and say what happened.

    Exists because every way this fails looks identical from the settings
    form -- a wrong port, an add-on whose Network settings never exposed one,
    a model still loading -- and the alternative to answering it here is
    switching transcription on, waiting for a call, and then reading the log
    to find out. It sends a second of tone rather than silence: a server is
    entitled to return nothing at all for silence, which would be
    indistinguishable from a broken connection.
    """
    payload = await _json_body(request)
    transcriber = stt.build({
        key: str(payload.get(key) or "").strip()
        for key in ("backend", "url", "model", "language")
    })
    if transcriber is None:
        return web.json_response({"ok": False, "error": "no address configured"})

    started = time.monotonic()
    try:
        result = await transcriber.transcribe(_TEST_CLIP)
    except stt.SttError as error:
        return web.json_response({"ok": False, "error": str(error)})
    except Exception as error:  # noqa: BLE001 -- a test button must not 500
        logger.exception("the speech-to-text test failed unexpectedly")
        return web.json_response({"ok": False, "error": f"{type(error).__name__}: {error}"})
    finally:
        await transcriber.close()

    return web.json_response({
        "ok": True,
        "elapsed": time.monotonic() - started,
        # Whatever it made of a tone -- usually nothing, occasionally an
        # invented word, which is itself worth seeing on the way in.
        "text": result["text"],
    })


def _time_param(raw: str | None, *, end_of_day: bool = False) -> float | None:
    """A `since`/`until` query value as epoch seconds.

    Accepts epoch seconds or an ISO date/datetime. ISO values are read as
    *local* time, deliberately: they come from a date picker sitting next to
    rows rendered in local time, and a filter that silently means UTC would
    cut the wrong day off a month-old search.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise web.HTTPBadRequest(text=f"not a date or timestamp: {text!r}")
    # A bare date as the *end* of a range means all of that day, not the
    # midnight that starts it -- otherwise "until the 6th" drops the 6th.
    if end_of_day and len(text) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed.timestamp()


async def get_history(request: web.Request) -> web.Response:
    history = request.app[HISTORY_KEY]
    try:
        limit = int(request.query.get("limit", 200))
    except ValueError:
        limit = 200
    filters = {
        "scanner_id": request.query.get("scanner") or None,
        "query": request.query.get("q", ""),
        "mode": request.query.get("mode", ""),
        "since": _time_param(request.query.get("since")),
        "until": _time_param(request.query.get("until"), end_of_day=True),
        # The history page asks for finished calls only; the control page's
        # recent list does not, because showing what is being received right
        # now is the whole point of it.
        "finished": request.query.get("finished") in ("1", "true", "yes"),
    }
    records = history.records(limit=max(1, min(limit, MAX_HISTORY_PAGE)), **filters)
    oldest, newest = history.span()
    return web.json_response(
        {
            "records": records,
            # Calls still in progress are in `records` too (the row is
            # written when the call starts and rewritten as it runs), so this
            # is only their ids -- enough for the UI to mark those rows live
            # without having to compare timestamps.
            "open_ids": [call.get("id") for call in history.open_calls()],
            "modes": list(reception.MODES),
            # How many match the filter rather than how many were returned:
            # with months of history a page is a small fraction of the answer,
            # and "200 of 148,392" is what makes that visible.
            "total": history.count(**filters),
            "oldest": oldest,
            "newest": newest,
        }
    )


async def delete_history(request: web.Request) -> web.Response:
    """Delete the log, and the audio that belongs to it.

    The clips go too, and not only because someone deleting their history
    means it: a clip is named after its call's row id, and SQLite issues ids
    from 1 again once the table is empty. Clips left behind therefore hold
    the ids the next calls are about to be given -- and ClipStore.prune reads
    recency out of the id, so it takes every newly written clip for the
    oldest file in the directory and deletes it moments after it is stored.
    The play button appears (the row keeps the name) and plays nothing.
    """
    history = request.app[HISTORY_KEY]
    manager = request.app[MANAGER_KEY]
    scanner_id = request.query.get("scanner") or None
    clips = history.clip_names(scanner_id)
    removed = history.clear(scanner_id)
    transcriber = getattr(manager, "transcriber", None)
    dropped = transcriber.discard_clips(clips) if transcriber is not None else 0
    logger.info(
        "receive history cleared from the web UI (%d record(s), %d clip(s))",
        removed, dropped,
    )
    return web.json_response({"ok": True, "removed": removed})


async def get_triggers(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    manager = request.app[MANAGER_KEY]
    engine = request.app[ENGINE_KEY]
    config = store.load()
    errors: list[str] = []
    warnings: list[str] = []
    config_store.validate_triggers(config, errors, warnings)
    return web.json_response(
        {
            "triggers": config["triggers"],
            "errors": errors,
            "warnings": warnings,
            "scanners": [{"id": s["id"], "name": s["name"]} for s in _runtime(manager)],
            "modes": list(reception.MODES),
            "action_types": list(triggers_module.ACTION_TYPES),
            "events": list(triggers_module.EVENTS),
            "max_triggers": config_store.MAX_TRIGGERS,
            # Keyed by rule id: {"at": epoch, "ok": bool, "detail": str}.
            # Shown per rule so "I saved it, did it work" has an answer
            # without opening the add-on log.
            "last_fired": engine.last_fired,
        }
    )


async def post_triggers(request: web.Request) -> web.Response:
    payload = await _json_body(request)
    config, errors, warnings, changes = await _save(request, {"triggers": payload.get("triggers", [])})
    if changes is None:
        return web.json_response({"ok": False, "errors": errors, "warnings": warnings}, status=400)
    logger.info("%d action rule(s) saved from the web UI", len(config["triggers"]))
    return web.json_response(
        {"ok": True, "triggers": config["triggers"], "errors": [], "warnings": warnings}
    )


async def post_trigger_test(request: web.Request) -> web.Response:
    """Fire one rule now, ignoring its match conditions and cooldown.

    Takes the rule from the request body rather than by id, so a rule can be
    tested before it is saved -- the point of a test button is to find out
    whether the URL/service is right, which is exactly when you don't yet
    want to commit it.
    """
    engine = request.app[ENGINE_KEY]
    payload = await _json_body(request)
    rule = config_store.normalize_trigger(payload.get("trigger") or {})
    errors: list[str] = []
    config_store.validate_triggers({"triggers": [rule], "scanners": []}, errors, [])
    if errors:
        return web.json_response({"ok": False, "detail": " ".join(errors)}, status=400)
    try:
        detail = await engine.test(rule, payload.get("scanner_id", ""))
    except triggers_module.TriggerError as exc:
        return web.json_response({"ok": False, "detail": str(exc)}, status=502)
    except Exception as exc:
        logger.exception("trigger test failed")
        return web.json_response({"ok": False, "detail": str(exc)}, status=502)
    return web.json_response({"ok": True, "detail": detail})
