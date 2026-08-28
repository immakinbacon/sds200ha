"""REST API: one route set shared by every configured scanner, namespaced by id."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

import avoid_audit
import key_codes
import reception
from avoids import AvoidLog
from mikrotik import MikrotikApiError
from xml_lists import element_to_dicts

logger = logging.getLogger(__name__)

SCANNERS_KEY = web.AppKey("scanners", dict)
AUDIO_BRIDGES_KEY = web.AppKey("audio_bridges", dict)
AVOIDS_KEY = web.AppKey("avoids", AvoidLog)


def create_app(scanners: dict, audio_bridges: dict, status_hub, avoids=None) -> web.Application:
    app = web.Application()
    app[SCANNERS_KEY] = scanners
    app[AUDIO_BRIDGES_KEY] = audio_bridges
    # Optional so every existing caller keeps working; an in-memory log is
    # the honest fallback, since the alternative is an endpoint that claims
    # to have recorded an index it dropped.
    app[AVOIDS_KEY] = avoids if avoids is not None else AvoidLog(path=None)

    app.router.add_get("/scanners", list_scanners)
    app.router.add_get("/scanners/{id}/status", get_status)
    app.router.add_post("/scanners/{id}/key", post_key)
    app.router.add_get("/scanners/{id}/volume", get_volume)
    app.router.add_post("/scanners/{id}/volume", post_volume)
    app.router.add_get("/scanners/{id}/squelch", get_squelch)
    app.router.add_post("/scanners/{id}/squelch", post_squelch)
    app.router.add_post("/scanners/{id}/hold", post_hold)
    app.router.add_post("/scanners/{id}/avoid", post_avoid)
    app.router.add_post("/scanners/{id}/avoid_current", post_avoid_current)
    app.router.add_post("/scanners/{id}/command", post_command)
    app.router.add_get("/scanners/{id}/avoids", get_avoids)
    # Before the {avoid_id} route: they differ by method as well, but a
    # literal path that could be read as an id belongs first regardless.
    app.router.add_get("/scanners/{id}/avoids/verify", get_avoids_verify)
    app.router.add_delete("/scanners/{id}/avoids/{avoid_id}", delete_avoid)
    app.router.add_get("/scanners/{id}/lists/{list_type}", get_list)
    app.router.add_get("/scanners/{id}/audio/stream.mp3", stream_audio)
    app.router.add_get("/scanners/{id}/audio/ws", stream_audio_ws)
    app.router.add_post("/scanners/{id}/reboot", post_reboot)
    app.router.add_get("/ws", status_hub.handle)
    return app


def _get_scanner(request: web.Request):
    scanners = request.app[SCANNERS_KEY]
    scanner = scanners.get(request.match_info["id"])
    if scanner is None:
        raise web.HTTPNotFound(text="unknown scanner id")
    return scanner


async def list_scanners(request: web.Request) -> web.Response:
    scanners = request.app[SCANNERS_KEY]
    return web.json_response(
        [{"id": s.id, "name": s.name, "host": s.host} for s in scanners.values()]
    )


async def get_status(request: web.Request) -> web.Response:
    scanner = _get_scanner(request)
    return web.json_response(scanner.last_status)


async def post_key(request: web.Request) -> web.Response:
    scanner = _get_scanner(request)
    payload = await request.json()
    name = str(payload.get("code", ""))
    try:
        code = key_codes.resolve(name)
    except KeyError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    mode = payload.get("mode", "P")

    # Logged, because for four episodes running the question after finding
    # this scanner held on a department has been "did something press a key
    # at it, or did a hand", and this route -- the one the web UI's Control
    # tab and the `sds200.key` service both use -- was the only key path in
    # the add-on that said nothing at all. weather.py has always logged its
    # presses, so the asymmetry made an empty log look like evidence when it
    # was just silence.
    #
    # The soft-key labels go in the line as well as the key name, and they
    # are the part worth having: soft1/soft2/soft3 mean nothing on their own
    # and mean System/Dept/Channel *hold* when the labels read like this.
    # "pressed soft1 against SYSTEM|DEPT|CHANNEL" is the whole diagnosis in
    # one line, where "pressed soft1" is another day of guessing.
    labels = "|".join(scanner.last_status.get("soft_keys") or ()) or "none"
    logger.info(
        "%s: key %s (%s, mode %s) requested by %s against soft keys [%s]",
        scanner.id, name, code, mode, request.remote or "unknown", labels,
    )
    resp = await _call(scanner.send_command, f"KEY,{code},{mode}")
    return web.json_response({"ok": resp.startswith("KEY,OK")})


async def get_volume(request: web.Request) -> web.Response:
    scanner = _get_scanner(request)
    resp = await _call(scanner.send_command, "VOL")
    return web.json_response({"level": _parse_single_value(resp)})


async def post_volume(request: web.Request) -> web.Response:
    scanner = _get_scanner(request)
    payload = await request.json()
    level = int(payload["level"])
    resp = await _call(scanner.send_command, f"VOL,{level}")
    return web.json_response({"ok": True, "level": _parse_single_value(resp)})


async def get_squelch(request: web.Request) -> web.Response:
    scanner = _get_scanner(request)
    resp = await _call(scanner.send_command, "SQL")
    return web.json_response({"level": _parse_single_value(resp)})


async def post_squelch(request: web.Request) -> web.Response:
    scanner = _get_scanner(request)
    payload = await request.json()
    level = int(payload["level"])
    resp = await _call(scanner.send_command, f"SQL,{level}")
    return web.json_response({"ok": True, "level": _parse_single_value(resp)})


async def post_hold(request: web.Request) -> web.Response:
    """Hold on the channel the scanner is on, or release the hold.

    `HLD,,,` -- "hold here", with no target -- answers `HLD,ERR` on this
    firmware. That is what this route sent from the day it was written, so
    `sds200.hold` and the card's Hold button have never once worked; the
    comment in sds200-card.js guessed as much and it turns out to be right.

    HLD names its target by list index, exactly as AVD does, and
    `HLD,CFREQ,<index>,` holds: GSI's `mode` goes to "Scan Hold" and the
    element's own `Hold` attribute to "On". Sending it again releases --
    it toggles rather than setting a state -- which is why this reads the
    current state first and only sends when there is something to change.
    Asking for a hold that is already in place is a no-op, not a release.

    `HLD,OK` is not evidence, in the way none of this hardware's
    acknowledgements are: it came back from a form that plainly didn't hold.
    The state in the answer is re-read from the scanner afterwards.

    An explicit `tkw` is still passed through verbatim, for the callers that
    want to name a system or department rather than the current channel.
    """
    scanner = _get_scanner(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    tkw = payload.get("tkw") or ""
    if tkw:
        command = f"HLD,{tkw},{payload.get('xxx1', '')},{payload.get('xxx2', '')}"
        resp = await _call(scanner.send_command, command)
        return web.json_response(
            {"ok": resp.startswith("HLD,OK"), "command": command, "response": resp}
        )

    try:
        gsi = await scanner.refresh_gsi()
    except Exception as exc:
        return web.json_response(
            {"ok": False, "detail": f"couldn't read what the scanner is on ({exc})"}, status=409
        )

    # No `hold` in the body means toggle, which is what a button labelled
    # "Hold" should do and what the command itself does. An explicit
    # true/false is a target state instead, so an automation asking twice
    # doesn't undo itself -- and asking for a hold already in place must not
    # release it, which is exactly what re-sending the toggle would do.
    want = payload.get("hold")
    holding = reception.is_holding(gsi)
    if want is not None and holding == bool(want):
        return web.json_response({"ok": True, "held": holding, "unchanged": True})

    target = reception.channel_target(gsi)
    if target is None:
        return web.json_response(
            {"ok": False, "detail": "the scanner isn't on a channel with a list index"},
            status=409,
        )

    command = f"HLD,{target['tkw']},{target['index']},"
    resp = await _call(scanner.send_command, command)
    try:
        held = reception.is_holding(await scanner.refresh_gsi())
    except Exception:
        # The command went out and was acknowledged; we just couldn't
        # confirm. Report the acknowledgement and say the state is unknown
        # rather than assert either way.
        return web.json_response(
            {"ok": resp.startswith("HLD,OK"), "held": None, "command": command, "response": resp}
        )

    ok = held != holding if want is None else held == bool(want)
    return web.json_response(
        {"ok": ok, "held": held, "command": command, "response": resp, "target": target}
    )


async def post_avoid(request: web.Request) -> web.Response:
    scanner = _get_scanner(request)
    payload = await request.json()
    tkw = payload.get("tkw", "")
    xxx1 = payload.get("xxx1", "")
    xxx2 = payload.get("xxx2", "")
    status = payload.get("status", 1)
    resp = await _call(scanner.send_command, f"AVD,{tkw},{xxx1},{xxx2},{status}")
    return web.json_response({"ok": resp.startswith("AVD,OK")})


AVOID_PRESS_GAP_S = 0.25
# Time for the scanner to settle before reading the list back.
AVOID_SETTLE_S = 1.0


async def post_avoid_current(request: web.Request) -> web.Response:
    """Permanently avoid the channel the scanner is on, and prove it stuck.

    This presses the unit's own AVOID key twice rather than sending `AVD`,
    which is the opposite of what it did first, and the reasoning is worth
    keeping because both halves were established the hard way (see
    docs/protocol-notes.md, "How avoids actually persist"):

    * `AVD,CFREQ,<index>,,1` genuinely does set a *permanent* avoid -- `GLT`
      reads back `Avoid`, not `T-Avoid`. But it writes only the scanner's
      working copy, so a power cycle discards it. That makes it, in
      practice, the same thing as a temporary avoid: precise, and gone by
      morning.
    * The only thing that writes to flash is a keypad press that sets a
      permanent avoid. It flushes the entire working copy when it does, so
      it also commits any pending un-avoids (see AvoidLog.commit_pending).

    The cost is aim: a key press acts on whatever the scanner is on at that
    instant, and it is scanning. Nothing can fix that -- there is no working
    hold command on this firmware (`HLD,,,` answers `HLD,ERR`) -- so instead
    of pretending, this checks. `GLT,CFREQ,<department>` reports `Off` /
    `T-Avoid` / `Avoid` per entry, so a second after the press we know
    whether the intended channel was hit, was only temporarily avoided
    (the two presses were too far apart), or was missed entirely.

    `KEY,OK` is not evidence of anything and is not treated as any. The
    verdict in the response comes from GLT or it doesn't exist.
    """
    scanner = _get_scanner(request)

    try:
        gsi = await scanner.refresh_gsi()
    except Exception as exc:
        return web.json_response(
            {"ok": False, "detail": f"couldn't read what the scanner is on ({exc})"}, status=409
        )

    target = reception.channel_target(gsi)
    if target is None:
        return web.json_response(
            {
                "ok": False,
                "detail": "the scanner isn't on a channel that can be avoided "
                "(nothing with a list index on screen)",
            },
            status=409,
        )
    snapshot = reception.extract({"gsi": gsi})
    if not snapshot["receiving"]:
        # The one condition that decides whether any of this works. While
        # free-scanning, the scanner steps through channels many times a
        # second: the channel GSI named is already gone by the time the
        # presses land, so they hit something else entirely -- confirmed
        # against real hardware, where a call made mid-scan reported the
        # intended channel untouched and left no trace of what it did hit.
        # Stopped on a transmission it is exact. So this is only offered
        # when the scanner is actually stopped on something, which is also
        # when a person would reach for it: you avoid what you are hearing.
        return web.json_response(
            {
                "ok": False,
                "detail": "the scanner isn't stopped on a channel — Perm Avoid presses the "
                "unit's own key, so it only lands where you mean while something is "
                "being received",
            },
            status=409,
        )

    if not target.get("department_index"):
        # Without the parent list there is no way to check the result, and
        # an unverifiable permanent avoid is exactly what this endpoint
        # exists to stop being.
        return web.json_response(
            {"ok": False, "detail": "no department index on screen, so the result can't be checked"},
            status=409,
        )

    # Twice, quickly. The escalation to permanent is timing-sensitive: at
    # ~0.25s apart the scanner goes straight to Avoid, while the same two
    # presses a second apart leave it at T-Avoid and stay there however many
    # follow. Nothing is read between them for that reason.
    code = key_codes.resolve("avoid")
    await _call(scanner.send_command, f"KEY,{code},P")
    await asyncio.sleep(AVOID_PRESS_GAP_S)
    await _call(scanner.send_command, f"KEY,{code},P")
    await asyncio.sleep(AVOID_SETTLE_S)

    try:
        result = await _avoid_state(scanner, target["department_index"], target["index"])
    except Exception as exc:
        return web.json_response(
            {
                "ok": False,
                "target": target,
                "detail": f"pressed Avoid, but couldn't read the list back to check ({exc})",
            },
            status=502,
        )

    ok = result == "Avoid"
    record = None
    committed = []
    if ok:
        record = request.app[AVOIDS_KEY].record(
            scanner.id, target, f"KEY,{code},P x2", result, context=snapshot,
        )
        # The same flush that saved this one saved every pending un-avoid.
        committed = request.app[AVOIDS_KEY].commit_pending()
        logger.info("%s: permanently avoided %s (index %s), %d pending clear(s) saved",
                    scanner.id, target["name"], target["index"], len(committed))
    else:
        logger.warning("%s: Avoid pressed for %s (index %s) but the list reads %r",
                       scanner.id, target["name"], target["index"], result)

    return web.json_response(
        {
            "ok": ok,
            "state": result,
            "target": target,
            "record": record,
            "committed": len(committed),
        }
    )


async def _avoid_state(scanner, department: str, index: str) -> str | None:
    """What GLT says about one channel: "Off", "T-Avoid", "Avoid" or None.

    The scanner's working copy, read back. This is the only honest success
    signal available -- `AVD,OK` comes back from commands that do nothing,
    and `KEY,OK` only means a key was accepted.
    """
    root = await scanner.send_xml_command(f"GLT,CFREQ,{department}")
    for entry in element_to_dicts(root):
        if entry.get("Index") == index:
            return entry.get("Avoid")
    return None


async def post_command(request: web.Request) -> web.Response:
    """Send one raw command and hand back the raw reply. A diagnostic.

    This project keeps running into the same wall: the Remote Command
    Specification was distilled into docs/protocol-notes.md rather than
    copied, and the tables that got left out are exactly the ones that
    matter -- the target keywords, the index pairs, the key modes. The
    scanner answers `OK` to plenty of commands it then doesn't act on the
    way the docs suggest (`AVD,,,,2` did nothing; `AVD,CFREQ,<index>,,1` is
    accepted and applied but doesn't survive a power cycle), so the only way
    to establish what this firmware actually does is to try forms against it
    and watch the hardware.

    Doing that through a code change per attempt is absurd, hence this. It
    is deliberately unvalidated -- the point is to send things the rest of
    the add-on doesn't know how to send -- and deliberately unglamorous:
    there is no UI for it, and nothing in the add-on calls it.

    Reachable only from behind Home Assistant's ingress authentication, the
    same as every other route here.
    """
    scanner = _get_scanner(request)
    payload = await request.json()
    command = str(payload.get("command", "")).strip()
    if not command:
        raise web.HTTPBadRequest(text="expected a command")
    resp = await _call(scanner.send_command, command)
    logger.info("%s: raw command %r -> %r", scanner.id, command, resp)
    return web.json_response({"command": command, "response": resp})


async def get_avoids(request: web.Request) -> web.Response:
    """Everything this add-on has permanently avoided on this scanner.

    What was *done*, not what is *true*: a record is what was sent, and an
    avoid cleared on the unit itself leaves a stale row here. Nor is it
    everything the scanner is avoiding -- front-panel avoids were never seen
    by this process. Both gaps are answerable, but only by asking the
    scanner: see get_avoids_verify.
    """
    scanner = _get_scanner(request)
    return web.json_response({"avoids": request.app[AVOIDS_KEY].for_scanner(scanner.id)})


async def get_avoids_verify(request: web.Request) -> web.Response:
    """Check the log against what the scanner says it is actually avoiding.

    `GLT` reports `Off` / `T-Avoid` / `Avoid` per entry, so both of the
    limits stated on `get_avoids` can be answered by reading the scanner
    rather than trusting the file. Two questions, two costs:

    * **Is what I wrote down still true?** One read per distinct department
      in the log -- a handful, fast enough to do on opening the section.
      That is the default.
    * **Is the scanner avoiding anything I didn't write down?** Only a walk
      of the whole tree can say, and that is thousands of reads and minutes
      of wall clock (see avoid_audit.sweep). `?sweep=true`, never implicit.

    What GLT reports is the scanner's *working copy*, which is the right
    question -- it is what the scanner is avoiding right now -- but a
    `T-Avoid`, and any `Avoid` that no keypad press has flushed, is gone at
    the next power cycle. The states are reported as they come back rather
    than collapsed into a boolean for exactly that reason.
    """
    scanner = _get_scanner(request)
    records = request.app[AVOIDS_KEY].for_scanner(scanner.id)
    checked = await _call(avoid_audit.check_records, scanner, records)

    body = {
        "checked": checked,
        # Records the scanner could not be asked about are counted apart
        # from records it contradicts. Folding them together reported a
        # radio that missed one read as every record disagreeing, which
        # is the one answer guaranteed to be wrong.
        "disagree": sum(1 for c in checked if not c["agrees"] and not c["unknown"]),
        "unknown": sum(1 for c in checked if c["unknown"]),
        "swept": False,
    }

    if request.query.get("sweep", "").lower() in ("1", "true", "yes"):
        result = await _call(avoid_audit.sweep, scanner)
        unrecorded = avoid_audit.unrecorded(result["avoided"], records)
        body.update(
            swept=True,
            counts=result["counts"],
            failures=result["failures"],
            avoided=result["avoided"],
            unrecorded=unrecorded,
            # Split out because they mean different things to whoever is
            # reading: a permanent one nothing here recorded is a channel
            # that stays avoided with no way back from this UI, while a
            # temporary one clears itself at the next restart.
            unrecorded_permanent=[a for a in unrecorded if a["avoid"] == avoid_audit.PERMANENT],
        )

    return web.json_response(body)


async def delete_avoid(request: web.Request) -> web.Response:
    """Un-avoid a recorded channel -- the same AVD, with status 3.

    The index is the entire point of the record: an avoided channel is the
    one the scanner never stops on again, so it never reappears in GSI and
    the number cannot be looked up a second time. Replaying what was sent is
    the only route back.

    `?forget=true` drops the record without sending anything, for an avoid
    that was cleared on the unit itself. Nothing here can detect that on its
    own -- same reason as above -- so it has to be something the operator can
    say.
    """
    scanner = _get_scanner(request)
    avoids = request.app[AVOIDS_KEY]
    record = avoids.get(scanner.id, request.match_info["avoid_id"])
    if record is None:
        raise web.HTTPNotFound(text="no such avoid on this scanner")

    if request.query.get("forget", "").lower() in ("1", "true", "yes"):
        avoids.remove(record)
        return web.json_response({"ok": True, "forgotten": True, "record": record})

    command = f"AVD,{record['tkw']},{record['index']},,3"
    resp = await _call(scanner.send_command, command)
    ok = resp.startswith("AVD,OK")
    if ok:
        # Marked, not removed. AVD writes the scanner's working copy only,
        # so the channel is scanning again *now* but will be avoided again
        # after the next power cycle -- and stays that way until some
        # permanent avoid flushes the working copy (post_avoid_current calls
        # commit_pending when one does). A record that vanished here would
        # be claiming a save that hasn't happened, and would take the only
        # copy of the index with it.
        avoids.mark_cleared(record)
        logger.info(
            "%s: stopped avoiding %s in the working copy; not saved until the next "
            "permanent avoid", scanner.id, record.get("name"),
        )
    return web.json_response(
        {"ok": ok, "command": command, "response": resp, "record": record, "saved": False}
    )


async def get_list(request: web.Request) -> web.Response:
    scanner = _get_scanner(request)
    list_type = request.match_info["list_type"]
    index = request.query.get("index")
    cmd = f"GLT,{list_type}" + (f",{index}" if index is not None else "")
    root = await _call(scanner.send_xml_command, cmd)
    return web.json_response(element_to_dicts(root))


def _get_bridge(request: web.Request):
    bridge = request.app[AUDIO_BRIDGES_KEY].get(request.match_info["id"])
    if bridge is None:
        raise web.HTTPNotFound(text="unknown scanner id")
    return bridge


async def stream_audio(request: web.Request) -> web.StreamResponse:
    bridge = _get_bridge(request)
    # audio/mp4, not audio/mpeg -- see audio_bridge.py's ffmpeg invocation
    # for why (fragmented MP4/AAC, for MediaSource Extensions compatibility).
    response = web.StreamResponse(headers={"Content-Type": "audio/mp4", "Cache-Control": "no-cache"})
    await response.prepare(request)
    queue = await bridge.subscribe()
    try:
        while True:
            chunk = await queue.get()
            if not chunk:
                # STREAM_CLOSED -- the bridge is dropping us (session ended,
                # or we fell too far behind to be sent a coherent stream).
                # End the response so the client sees a finished stream and
                # can reconnect, instead of an open socket going quiet.
                break
            await response.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        bridge.unsubscribe(queue)
    return response


async def stream_audio_ws(request: web.Request) -> web.WebSocketResponse:
    """The same fragmented-MP4 byte stream as stream.mp3, over a WebSocket.

    Exists for exactly one client: the add-on's own web UI, which is served
    through Home Assistant ingress and therefore runs on HA's origin, where
    HA's frontend service worker intercepts every same-origin request and
    kills a never-ending response body at ~30s (see docs/protocol-notes.md,
    "Root cause of the ~30s stop"). The Lovelace card dodges that by doing
    its fetch inside a sandboxed iframe, whose opaque origin has no service
    worker -- but that trick cannot work behind ingress: an opaque origin
    sends no cookies, and ingress authenticates by the `ingress_session`
    cookie, so the request is refused before the service worker even
    matters.

    A WebSocket has neither problem. Service workers never see the
    handshake (there is no `fetch` event for one), and it is an ordinary
    same-origin request, so the ingress cookie goes with it -- which is
    already proven in this deployment by the status socket the same page
    keeps open. Ingress proxies WebSockets natively (HA core's ingress view
    forwards them), so nothing about this needs a port of its own.

    Frames are the same 4 KiB ffmpeg chunks the HTTP route sends, starting
    with the cached init segment that `AudioBridge.subscribe()` replays --
    so a client can feed them straight into a MediaSource SourceBuffer,
    exactly as it would the HTTP body.

    The HTTP route stays: it is what the Home Assistant integration's audio
    proxy view fetches (custom_components/sds200/__init__.py), where none
    of the above applies.
    """
    bridge = _get_bridge(request)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    queue = await bridge.subscribe()
    # Two tasks rather than one loop: the send side blocks on the bridge's
    # queue and the receive side blocks on the socket, and either one ending
    # (stream closed by the bridge / client went away) has to end the other.
    reader = asyncio.create_task(_drain_ws(ws))
    sender = asyncio.create_task(_send_audio(ws, queue))
    try:
        await asyncio.wait({reader, sender}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        bridge.unsubscribe(queue)
        for task in (reader, sender):
            task.cancel()
        # Awaited before close(): closing while another task sits in
        # receive() is what aiohttp warns about, and cancelling the reader
        # first is what makes this the only receiver.
        await asyncio.gather(reader, sender, return_exceptions=True)
        await ws.close()
    return ws


async def _drain_ws(ws: web.WebSocketResponse) -> None:
    """Read and discard: this end sends, and the only thing the receive side
    is for is noticing that the client has gone away."""
    async for _msg in ws:
        pass


async def _send_audio(ws: web.WebSocketResponse, queue: "asyncio.Queue[bytes]") -> None:
    while True:
        chunk = await queue.get()
        if not chunk:
            # STREAM_CLOSED -- same meaning as in stream_audio: the bridge is
            # dropping us, so end the socket and let the client reconnect
            # into a fresh stream that starts with an init segment.
            return
        try:
            await ws.send_bytes(chunk)
        except (ConnectionResetError, RuntimeError):
            return


async def post_reboot(request: web.Request) -> web.Response:
    """Power-cycle the scanner via its configured poe_reset (optional, see
    audio_bridge.py). Manual/on-demand -- not gated by that scanner's
    auto_reboot setting.
    """
    bridge = _get_bridge(request)
    if not bridge.has_reboot_mechanism():
        raise web.HTTPBadRequest(text="no poe_reset configured for this scanner")
    try:
        ok = await bridge.trigger_reboot()
    except MikrotikApiError as exc:
        # 502, not a 200 with {"ok": false}: the caller here is HA's
        # sds200.reboot service, which only surfaces an error to the user
        # if the request actually fails. Returning 200 meant a
        # power-cycle that never happened looked like one that did, and
        # mikrotik.py's diagnostic message (which names the URL and the
        # likely www-ssl misconfiguration) was thrown away instead of
        # being shown.
        raise web.HTTPBadGateway(text=str(exc)) from exc
    return web.json_response({"ok": ok})


async def _call(fn, *args):
    try:
        return await fn(*args)
    except Exception as exc:
        raise web.HTTPBadGateway(text=str(exc))


def _parse_single_value(resp: str) -> int | None:
    parts = resp.split(",")
    if len(parts) >= 2 and parts[1].strip().lstrip("-").isdigit():
        return int(parts[1])
    return None
