"""UDP ASCII control-protocol client for a single Uniden SDS200.

Protocol summary (see ../../../docs/protocol-notes.md for the full picture):
stateless request/response ASCII commands terminated with "\\r", sent as UDP
datagrams to port 50536. No handshake, no correlation id, so we serialize
one outstanding command at a time per scanner and treat the next datagram we
receive as that command's response.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

logger = logging.getLogger(__name__)

DEFAULT_CONTROL_PORT = 50536
KEEPALIVE_INTERVAL = 20.0

# Calibrated against a real SDS200 (firmware 1.23.15): the fast poll
# round-trips in ~20-70ms with an occasional drop. GSI is the slow, flaky one
# (sometimes 1-4s or no response at all), so structured data
# (system/frequency/etc) is polled on its own best-effort cycle instead of
# blocking the display mirror.
#
# The fast poll is **GST, not STS** (re-measured 2026-08-21 against the same
# firmware: 213 polls, p90 32ms, 12 unanswered). GST returns everything STS
# does and adds the one field that says whether the scanner is actually
# playing what it is receiving -- see MUTE below. It costs nothing extra: same
# command shape, same round trip, one poll rather than two.
# Four times a second, not once. This loop is now what defines a call --
# a transmission starts when the scanner unmutes and ends when it mutes again
# -- so the interval is the accuracy of every boundary downstream: the row's
# timestamps, and the seconds of audio cut for it.
#
# Sized against the traffic rather than against comfort. Measured on this
# install, the median transmission is 0.6s and the median gap between two of
# them is 1.0s, with gaps as short as 0.2s. A one-second poll cannot separate
# those; a quarter-second one can. The scanner is comfortable: 213 polls in a
# minute, p90 32ms, and no sign of strain.
STATUS_POLL_INTERVAL = 0.25

# What `mute` reads while the scanner is playing audio, as opposed to merely
# receiving it. Measured on real hardware: "1" while idle or while the site
# has RF the scanner is not monitoring, "0" for the seconds it unmutes.
#
# This is the distinction RSSI cannot make. On a trunked digital system the
# receiver has signal whenever the site is active, so `Rssi != -999` reports a
# call whether or not this scanner ever played a note of it -- measured on
# this install as eight stretches of RF in one minute against a single
# unmute. Anything that means "we heard this" has to be built on the mute
# flag; anything built on RSSI is a record of the site, not of the scanner.
UNMUTED = "0"
GSI_POLL_INTERVAL = 3.0

# Consecutive failed display polls (not a single one -- "an occasional drop"
# is normal per the calibration note above; GST was measured dropping 12 of
# 213, and three of those in a row is a different event from five percent
# scattered) before the control interface is considered down. Used by AudioBridge to gate RTSP connection attempts on
# this same scanner's control interface, not just its own RTSP server --
# see audio_bridge.py.
CONTROL_UNREACHABLE_THRESHOLD = 3


class CommandError(Exception):
    """Raised when a command gets no response after retries."""


class _SDS200Protocol(asyncio.DatagramProtocol):
    def __init__(self, queue: "asyncio.Queue[bytes]"):
        self._queue = queue
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self._queue.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        logger.warning("UDP error: %s", exc)


class ScannerConnection:
    """Owns the UDP socket, keepalive loop, and status-poll loop for one scanner."""

    def __init__(
        self,
        scanner_id: str,
        name: str,
        host: str,
        control_port: int = DEFAULT_CONTROL_PORT,
        rtsp_port: int = 554,
        gsi_poll_interval: float = GSI_POLL_INTERVAL,
    ):
        self.id = scanner_id
        self.name = name
        self.host = host
        self.control_port = control_port
        self.rtsp_port = rtsp_port
        # Configurable because it sets the resolution of the receive history
        # -- a transmission shorter than this can fall between two polls
        # entirely. See history.py's module docstring for the tradeoff.
        self.gsi_poll_interval = gsi_poll_interval

        self._transport: asyncio.DatagramTransport | None = None
        self._queue: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []

        self.last_status: dict = {}
        self._status_listeners: list = []

        # Reachability of the control interface itself, tracked from the
        # fast/reliable STS poll loop (not the flakier GSI one). Starts
        # unreachable -- nothing has responded yet -- so a fresh AudioBridge
        # waiting on wait_until_reachable() won't open RTSP before the
        # control connection has proven itself up at least once.
        self._reachable_event = asyncio.Event()
        self._unreachable_event = asyncio.Event()
        self._unreachable_event.set()
        self._consecutive_poll_failures = 0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _proto = await loop.create_datagram_endpoint(
            lambda: _SDS200Protocol(self._queue),
            remote_addr=(self.host, self.control_port),
        )
        self._tasks.append(asyncio.create_task(self._keepalive_loop(), name=f"{self.id}-kal"))
        self._tasks.append(asyncio.create_task(self._poll_loop(), name=f"{self.id}-poll"))
        self._tasks.append(
            asyncio.create_task(self._gsi_poll_loop(self.gsi_poll_interval), name=f"{self.id}-gsi")
        )
        logger.info("%s: connected to %s:%d", self.id, self.host, self.control_port)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self._transport:
            self._transport.close()
            self._transport = None

    def add_status_listener(self, callback) -> None:
        """callback(scanner_id: str, status: dict) -> None, called on every STS change."""
        self._status_listeners.append(callback)

    def is_reachable(self) -> bool:
        return self._reachable_event.is_set()

    async def wait_until_reachable(self) -> None:
        """Block until the control interface has responded recently enough
        to be considered up. AudioBridge waits on this before opening an
        RTSP session -- see audio_bridge.py.
        """
        await self._reachable_event.wait()

    async def wait_until_unreachable(self) -> None:
        """Block until the control interface stops responding. AudioBridge
        races this against its running session to tear audio down promptly
        if the scanner's control interface goes dark -- see audio_bridge.py.
        """
        await self._unreachable_event.wait()

    def _mark_poll_success(self) -> None:
        self._consecutive_poll_failures = 0
        if not self._reachable_event.is_set():
            logger.info("%s: control interface responding again", self.id)
        self._reachable_event.set()
        self._unreachable_event.clear()

    def _mark_poll_failure(self) -> None:
        self._consecutive_poll_failures += 1
        if self._consecutive_poll_failures < CONTROL_UNREACHABLE_THRESHOLD:
            return
        if self._reachable_event.is_set():
            logger.warning(
                "%s: control interface stopped responding (%d consecutive failed polls)",
                self.id, self._consecutive_poll_failures,
            )
            self._reachable_event.clear()
            self._unreachable_event.set()
        elif self._consecutive_poll_failures == CONTROL_UNREACHABLE_THRESHOLD:
            # Never reachable at all since add-on startup, not a drop from a
            # previously-working state -- the old version of this check only
            # logged on that transition, so this case (audio permanently
            # waiting, with zero explanation in the logs) was silent.
            logger.warning(
                "%s: control interface not responding (%d consecutive failed polls) -- "
                "audio will not start until it does", self.id, self._consecutive_poll_failures,
            )

    async def send_command(self, command: str, *, retries: int = 2, timeout: float = 1.0) -> str:
        """Send one ASCII command, return the raw response line (without trailing CR/LF)."""
        if self._transport is None:
            raise CommandError(f"{self.id}: not connected")
        payload = (command.strip() + "\r").encode("ascii")
        expected_prefix = command.strip().split(",")[0] + ","
        async with self._lock:
            _drain(self._queue)
            last_exc: Exception | None = None
            for attempt in range(retries + 1):
                self._transport.sendto(payload)
                loop = asyncio.get_event_loop()
                deadline = loop.time() + timeout
                try:
                    while True:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            raise asyncio.TimeoutError()
                        data = await asyncio.wait_for(self._queue.get(), remaining)
                        text = data.decode("ascii", errors="replace").rstrip("\r\n")
                        if text.startswith(expected_prefix) or text == command.strip():
                            return text
                        # Not our reply -- the scanner can interleave unsolicited
                        # pushes (e.g. a stray GSI push) or a slow/late reply to
                        # a prior request. Discard and keep waiting for a match.
                        logger.debug(
                            "%s: ignoring unmatched datagram %r while waiting for %r",
                            self.id, text[:60], command,
                        )
                except asyncio.TimeoutError as exc:
                    last_exc = exc
                    logger.debug("%s: timeout on %r (attempt %d)", self.id, command, attempt + 1)
            raise CommandError(f"{self.id}: no response to {command!r}") from last_exc

    async def send_xml_command(self, command: str, *, timeout: float = 2.0):
        """Send a command whose response is paginated XML (GLT/GSI/MSI); reassemble it.

        Returns a parsed xml.etree.ElementTree.Element.
        """
        from xml_lists import reassemble  # local import: keeps xml handling optional/isolated

        if self._transport is None:
            raise CommandError(f"{self.id}: not connected")
        payload = (command.strip() + "\r").encode("ascii")
        expected_prefix = command.strip().split(",")[0] + ","
        async with self._lock:
            _drain(self._queue)
            self._transport.sendto(payload)
            return await reassemble(self._queue, timeout=timeout, expected_prefix=expected_prefix)

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            try:
                if self._transport:
                    self._transport.sendto(b"KAL\r")
            except Exception:
                logger.exception("%s: keepalive failed", self.id)

    async def refresh_display(self) -> dict:
        """Poll the display mirror once, merge it and tell every listener.

        The fast counterpart of `refresh_gsi`, and here for the same reason:
        the display-mirror fields -- the soft-key labels above all -- are
        only as fresh as the loop below last left them, and weather.py has
        to decide *which key is under which label right now* before it
        presses one. Reading GSI alone left that half of the screen a poll
        stale, which is how a key chosen for a weather screen got sent to
        the scan screen. See docs/protocol-notes.md.

        Raises whatever send_command raises; the loop catches, a caller that
        must not press blind lets it through.
        """
        raw = await self.send_command("GST", retries=1, timeout=1.0)
        status = parse_gst(raw)
        if status is None:
            return {}
        self.last_status = {**self.last_status, **status}
        for callback in self._status_listeners:
            callback(self.id, self.last_status)
        return status

    async def _poll_loop(self, interval: float = STATUS_POLL_INTERVAL) -> None:
        """Fast, reliable display-mirror loop (GST -- see STATUS_POLL_INTERVAL)."""
        while True:
            try:
                await self.refresh_display()
                self._mark_poll_success()
            except CommandError:
                self._mark_poll_failure()
            except Exception:
                logger.exception("%s: status poll failed", self.id)
            await asyncio.sleep(interval)

    async def refresh_gsi(self) -> dict:
        """Poll GSI once, merge it into last_status and tell every listener.

        The loop below is the usual caller, but this is also here to be
        called directly by anything that has to act on where the scanner is
        *right now* rather than on where it was up to a poll interval ago --
        api.post_avoid_current, which turns the channel being received into
        an AVD target by its list index and would otherwise be aiming at
        whatever was on screen up to GSI_POLL_INTERVAL earlier.

        Raises whatever send_xml_command raises; callers that can carry on
        without it (the loop) catch, callers that can't (a permanent avoid)
        let it through.
        """
        from xml_lists import gsi_to_dict

        root = await self.send_xml_command("GSI", timeout=2.0)
        gsi = gsi_to_dict(root)
        # Stamped, because `last_status` is a *merge*: every fast display poll
        # re-delivers the same "gsi" block to every listener, so without a
        # stamp there is no way to tell a three-second-old identity from one
        # that has just arrived -- and a listener that cannot tell will
        # cheerfully name a new transmission after the previous one.
        self.last_status = {**self.last_status, "gsi": gsi, "gsi_at": time.time()}
        for callback in self._status_listeners:
            callback(self.id, self.last_status)
        return gsi

    async def _gsi_poll_loop(self, interval: float = GSI_POLL_INTERVAL) -> None:
        """Slower, best-effort structured-data loop (GSI): system/department/
        channel/frequency/talkgroup, merged into last_status under "gsi".
        """
        while True:
            try:
                await self.refresh_gsi()
            except (CommandError, TimeoutError, ValueError):
                pass  # GSI is noticeably flakier than STS on real hardware; skip and retry
            except Exception:
                logger.exception("%s: GSI poll failed", self.id)
            await asyncio.sleep(interval)


def _drain(queue: "asyncio.Queue[bytes]") -> None:
    """Discard any stale datagrams left over from a prior timed-out request."""
    while not queue.empty():
        queue.get_nowait()


def split_fields(s: str) -> list[str]:
    """Split a comma-separated response, undoing the protocol's tab-for-comma escaping."""
    return [f.replace("\t", ",") for f in s.split(",")]


_CONTROL_CHARS = "".join(chr(c) for c in range(0x20) if c not in (0x09,))  # keep tab (already unescaped)
# Responses are decoded as ASCII with errors="replace" (see send_command),
# since the SDS200's LCD uses custom glyph bytes above 0x7F (observed e.g.
# b"\xac\xad" trailing the clock line -- likely status icons; there's no
# documented mapping for them) that aren't valid ASCII. Those bytes decode
# to U+FFFD one-for-one, which without this would show up as literal "<63>"
# replacement-character glyphs in HA sensor states. We can't render the
# original icon anyway (HA entity state is plain text), so drop it same as
# a control character rather than show mangled placeholder chars.
_STRIP_CHARS = _CONTROL_CHARS + "�"


def _strip_control_chars(text: str) -> str:
    """Strip scroll/blink control codes (see docs/protocol-notes.md) and
    undecodable custom-glyph bytes so downstream consumers (HA sensors, the
    card) get clean text.
    """
    return text.translate(str.maketrans("", "", _STRIP_CHARS))


def parse_sts(raw: str) -> dict | None:
    """Parse an "STS,<DSP_FORM>,<L1_CHAR>,<L1_MODE>,...,<RSV>x9" response.

    Returns {"dsp_form": str, "lines": [...], "soft_keys": [str, str, str]}.
    """
    if not raw.startswith("STS,"):
        return None
    return _parse_display_lines(raw[len("STS,"):])


# GST's trailing fields, in order, after the display-line pairs -- see
# docs/protocol-notes.md ("STS / GST display mirroring").
GST_TRAILING_FIELDS = [
    "mute", "led1", "led2", "wf_mode", "freq", "mod",
    "mf_pos", "cf", "lower", "upper", "color_mode", "fft_size",
]


def parse_gst(raw: str) -> dict | None:
    """Parse a "GST,..." response: same display-line shape as STS, plus named
    status fields (mute/LEDs/mode/frequency/waterfall params) in place of STS's
    plain reserved fields.
    """
    if not raw.startswith("GST,"):
        return None
    parsed = _parse_display_lines(raw[len("GST,"):], trailing_fields=GST_TRAILING_FIELDS)
    return parsed


def _parse_display_lines(body: str, trailing_fields: list[str] | None = None) -> dict | None:
    fields = split_fields(body)
    if not fields or not fields[0]:
        return None
    dsp_form = fields[0]
    lines = []
    # The unstripped text kept alongside, because the soft-key labels are cut
    # out of it by *column* and stripping first moves the columns -- see
    # _parse_soft_keys.
    raw_rows: list[tuple[str, str]] = []
    idx = 1
    for _ in range(len(dsp_form)):
        if idx >= len(fields):
            break
        raw = fields[idx]
        mode = fields[idx + 1] if idx + 1 < len(fields) else ""
        lines.append({"text": _strip_control_chars(raw), "mode": mode})
        raw_rows.append((raw, mode))
        idx += 2
    result = {"dsp_form": dsp_form, "lines": lines}

    # GST's named fields are taken from the *end* of the response rather than
    # counted forward from the last display line, because what sits between
    # them is not a fixed width: DSP_FORM does not reliably count the soft-key
    # row, and the row's mask arrives as three fields of its own. Counted
    # forward, `mute` reads as the soft-key label on any screen where that
    # lands outside the count -- and `mute` is now what decides whether the
    # scanner is playing something, so it being off by three fields is not a
    # cosmetic problem.
    leftovers = fields[idx:]
    if trailing_fields:
        result.update(dict.fromkeys(trailing_fields))
        if len(fields) - idx >= len(trailing_fields):
            tail = fields[-len(trailing_fields):]
            leftovers = fields[idx:len(fields) - len(trailing_fields)]
            result.update(dict(zip(trailing_fields, tail)))

    # The soft-key labels come out of the *lines*, so they are there to be
    # found whatever follows them -- and they are needed whichever command
    # asked. Computing them for STS only is what took the labels off the
    # control tab, and out from under weather.py, the moment the fast poll
    # moved to GST.
    result["soft_keys"] = _parse_soft_keys(raw_rows, leftovers)
    return result


# The soft-key label row is marked by its own per-character "mode" field: one
# run of asterisks per key, e.g. " SYSTEM      DEPT     CHANNEL " under
# "********* ********** *********". That mask is what identifies the row --
# its *position* can't, because DSP_FORM does not reliably count it (one real
# capture has 17 digits and 18 lines, another 17 and 17), so depending on
# which side of the count it lands it arrives either as the last parsed line
# or as a leftover field.
_SOFT_KEY_SPANS = re.compile(r"\*+")
_SOFT_KEY_COUNT = 3


def _parse_soft_keys(raw_rows: list[tuple[str, str]], rest: list[str]) -> list[str]:
    """The three labels above the soft keys, or [] if none were sent.

    Worth having because the labels change with what the scanner is showing:
    on the weather-alert screen one of them is the way back to scanning, and
    reading which column it is in beats hardcoding a key that a firmware
    update or a different screen can move (see weather.py).

    Takes the row's *unstripped* text on purpose. A label is the slice of the
    row under its run of asterisks, so the column numbers only mean anything
    against a row still 30 characters wide. Stripping first deletes characters
    and shortens it, sliding every label right of a glyph one key to the left
    -- which is exactly what the weather-alert screen looks like, where the
    middle key's label is a run of custom glyph bytes (see the live capture in
    docs/protocol-notes.md). Each label is stripped after it is cut out, so a
    label that was nothing but glyphs comes back empty rather than as mojibake.
    """
    candidates = list(raw_rows)
    if rest:
        candidates.append((rest[0], rest[1] if len(rest) > 1 else ""))
    for text, mask in reversed(candidates):
        spans = [(m.start(), m.end()) for m in _SOFT_KEY_SPANS.finditer(mask)]
        if len(spans) == _SOFT_KEY_COUNT:
            return [_strip_control_chars(text[start:end]).strip() for start, end in spans]
    return []
