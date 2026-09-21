"""RTSP/RTP -> ffmpeg -> live HTTP audio stream, per scanner.

The scanner speaks RTSP/1.0 on TCP 554 (OPTIONS/DESCRIBE/SETUP/PLAY/
GET_PARAMETER/TEARDOWN) and sends G.711 mu-law audio over RTP/UDP once
PLAY'd. Rather than hand-decode mu-law, we strip the 12-byte RTP header off
each packet and pipe the raw payload straight into ffmpeg, which decodes and
re-encodes to MP3 that we fan out to any number of HTTP subscribers.

The RTSP session is opened once, when start() is called (at add-on
startup), and kept running for the add-on's entire lifetime regardless of
whether any HTTP client is currently listening -- NOT opened per-subscriber
and torn down when the last one disconnects, which is how this used to
work. Real-hardware finding (see docs/protocol-notes.md): the scanner's
embedded RTSP server tolerates a single long-lived session fine, but wedges
very easily (stop accepting new connections entirely) on repeated
connect/disconnect cycles -- and the old demand-driven design caused
exactly that on every listener disconnect/reconnect. If the session does
fail, _supervise_forever() reconnects with a backoff delay, not
immediately, for the same reason.

Real-hardware finding (see docs/protocol-notes.md): the embedded RTSP server
can wedge (stop accepting connections) after a malformed request or rapid
reconnects, and the only recovery seen so far is power-cycling the scanner --
there's no documented network reboot command. So this also supports an
*optional* power-cycle recovery via `poe_reset` (a MikrotikPoeReset):
power-cycles the switch port the scanner is PoE-powered from via RouterOS's
REST API.

Two independent conditions can trigger that power-cycle, each behind its
own opt-in toggle, because they fail in ways that don't overlap:

- `auto_reboot` -- a run of consecutive RTSP session failures, i.e. the
  audio server is wedged while the scanner is otherwise fine.
- `auto_reboot_on_control_failure` -- the UDP control interface has gone
  dark for CONTROL_REBOOT_AFTER, i.e. the whole scanner is unresponsive.
  Note this can't be folded into the counter above: no control interface
  means no RTSP session is ever attempted (see _supervise_forever), so
  this failure produces *zero* audio failures. See _await_control().

Both back off for a cooldown afterwards so we don't hammer a scanner
that's still rebooting. Manual/on-demand reboot (regardless of either
toggle) is exposed via trigger_reboot() -> the /scanners/{id}/reboot API
route.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import NamedTuple

from mikrotik import MikrotikApiError, MikrotikPoeReset
from protocol import ScannerConnection

logger = logging.getLogger(__name__)

RTP_HEADER_LEN = 12
KEEPALIVE_INTERVAL = 20.0

# Per-subscriber queue depth, in ffmpeg stdout chunks (4096 bytes each).
SUBSCRIBER_QUEUE_MAXSIZE = 64
# Queued instead of a chunk to tell a subscriber's HTTP handler to end its
# response (see api.py's stream_audio). Empty bytes never occur as real
# data: _pump_stdout treats a zero-length read as ffmpeg's EOF and never
# fans it out.
STREAM_CLOSED = b""

class RtpPacket(NamedTuple):
    """One RTP payload and the timing the sender put in front of it.

    The header used to be sliced off and thrown away, which cost the two
    fields that say *when* this audio was sampled and *whether any of it is
    missing*. Without them the only clock available to a listener is when the
    datagram happened to arrive, and audio_tap.RtpClock's docstring sets out
    what that costs: an arrival-driven timeline slides against wall clock
    every time the network loses a packet, and nothing downstream can tell
    that it has.

    `seq` counts packets (16-bit, wrapping) and finds loss; `timestamp`
    counts *samples* (32-bit, wrapping, 8kHz here) and gives the media clock.
    Both are None on AUDIO_RESET, which carries no audio to place in time.
    """

    payload: bytes
    seq: int | None = None
    timestamp: int | None = None


# Handed to raw-audio listeners (add_audio_listener) when the RTSP session
# restarts: the next payload is not contiguous with the last one, so anything
# holding a partial frame or a half-open segment has to drop it rather than
# splice across the gap. An empty payload is never a real one -- see
# _RtpProtocol.datagram_received.
AUDIO_RESET = RtpPacket(b"")

# Neither the initial TCP connect nor any individual RTSP request/response
# had a timeout before this -- on this hardware's documented wedged state
# (see docs/protocol-notes.md), the scanner can accept a TCP connection and
# then never answer a single request, which left _run_session() hanging
# forever with no exception, no log line, and therefore no retry/backoff/
# auto-reboot -- completely invisible. These bound that hang so a wedged
# scanner shows up as a logged, retried failure instead of silence.
RTSP_CONNECT_TIMEOUT = 5.0
RTSP_REQUEST_TIMEOUT = 5.0
# The periodic GET_PARAMETER keepalive gets a much more generous timeout than
# the initial handshake: it's sent every KEEPALIVE_INTERVAL (20s) purely to
# stay under the RTSP Session's 60s inactivity timeout, so there's ~40s of
# slack to spare, and a real-install regression showed the scanner can be
# slower to answer it while already mid-stream than it is to answer the
# initial OPTIONS/DESCRIBE/SETUP/PLAY handshake -- the tight 5s
# RTSP_REQUEST_TIMEOUT was tearing down (and, via RECONNECT_BACKOFF, briefly
# killing) perfectly healthy sessions right around the first keepalive.
RTSP_KEEPALIVE_TIMEOUT = 30.0

AUTO_REBOOT_FAILURE_THRESHOLD = 3
REBOOT_COOLDOWN = 90.0  # ample time for the PoE power-cycle + scanner boot before retrying
RECONNECT_BACKOFF = 30.0  # avoid rapid reconnect cycles, which wedge this hardware

# How long after this bridge starts an RTSP failure is *expected* rather than
# a reason to cut power to the radio.
#
# Restarting the add-on (every Rebuild, every settings change that restarts a
# scanner) abandons the previous container's RTSP session without a TEARDOWN.
# The scanner keeps holding it until its own inactivity timeout expires --
# 60s, the number RTSP_KEEPALIVE_TIMEOUT above is set to stay under -- and
# refuses new sessions the whole time, with a plain ConnectionRefusedError on
# port 554. That is not a wedged scanner; it is a scanner correctly declining
# to open a second session while the first is still live.
#
# With the threshold at 3 and a 30s backoff, the reboot decision lands at
# roughly 60-90s: right on top of that timeout, and the race went the wrong
# way in the field. Every lost race power-cycled the radio, and a power-cycle
# reloads the saved hold state from flash -- which is how one accidental
# System/Department hold came back on this scanner after every rebuild for
# four days running (docs/protocol-notes.md, 2026-08-15). The grace only
# delays the decision: failures keep counting, so a scanner that is still
# failing once it expires is power-cycled on the next pass.
STARTUP_REBOOT_GRACE = 150.0

# How long the *control* interface has to stay dark before a power-cycle is
# considered (auto_reboot_on_control_failure only). Real incident it exists
# for (2026-07-25, see docs/protocol-notes.md): the scanner answered ARP and
# ICMP while UDP 50536, RTSP and HTTP were all dead, and only a physical
# restart brought it back -- which nobody could see, because a scanner with
# no control interface never gets an RTSP session opened against it (see
# _supervise_forever's gating) and so never produces the audio failures the
# other auto-reboot path counts.
#
# Two minutes, not seconds: ScannerConnection already declares control
# unreachable after only ~3 failed one-second polls, which is deliberately
# twitchy (it's used to gate audio, where reacting early is free). Power-
# cycling production hardware is not free, so this wants the much stronger
# signal of a scanner that has been silent far longer than any hiccup,
# reboot, or brief network blip could account for -- a scanner restarted by
# hand answers control again ~18s after power-on.
CONTROL_REBOOT_AFTER = 120.0
# Consecutive power-cycles for one dark spell before giving up and just
# waiting. A scanner that's been power-cycled three times and still hasn't
# answered isn't wedged in a way that PoE can fix (it's unplugged, moved,
# renumbered, or dead), and a supervisor that keeps cutting power to a
# switch port every few minutes forever is worse than one that stops and
# says so. Reset once control answers again.
CONTROL_REBOOT_MAX_ATTEMPTS = 3


def _split_init_segment(buf: bytearray) -> tuple[bytes | None, bytes | None]:
    """Scan top-level MP4 boxes in `buf` for ffmpeg's ftyp+moov init segment,
    which is everything before the first `moof` box. Returns
    `(init_segment, rest)` once a `moof` box start is fully located in `buf`,
    or `(None, None)` if more bytes are needed before that can be determined.
    """
    offset = 0
    while offset + 8 <= len(buf):
        size = int.from_bytes(buf[offset : offset + 4], "big")
        box_type = bytes(buf[offset + 4 : offset + 8])
        if size < 8:
            # 0/1 (end-of-file / 64-bit largesize) isn't expected for the
            # small ftyp/moov boxes at the start of a fragmented stream --
            # wait for more data rather than mis-parse.
            return None, None
        if box_type == b"moof":
            return bytes(buf[:offset]), bytes(buf[offset:])
        if offset + size > len(buf):
            return None, None
        offset += size
    return None, None


class AudioBridge:
    def __init__(
        self,
        scanner_id: str,
        host: str,
        rtsp_port: int,
        rtp_client_port: int,
        control: ScannerConnection,
        poe_reset: MikrotikPoeReset | None = None,
        auto_reboot: bool = False,
        auto_reboot_on_control_failure: bool = False,
        clock=time.monotonic,
    ):
        self.id = scanner_id
        self.host = host
        self.rtsp_port = rtsp_port
        self.rtp_client_port = rtp_client_port
        self.control = control
        self.poe_reset = poe_reset
        self.auto_reboot = auto_reboot
        self.auto_reboot_on_control_failure = auto_reboot_on_control_failure
        self._clock = clock
        # Set when the supervisor loop starts rather than in __init__: what
        # STARTUP_REBOOT_GRACE is measuring is how long ago this bridge began
        # trying to open a session, and a bridge constructed and started at
        # different moments would otherwise spend its grace before it asked
        # the scanner for anything.
        self._supervising_since: float | None = None

        self._subscribers: set["asyncio.Queue[bytes]"] = set()
        # Raw mu-law RTP payloads, fanned out to anything wanting the audio
        # *before* ffmpeg re-encodes it -- transcription, via app/audio_tap.py.
        # Deliberately separate from _subscribers, who get ffmpeg's fMP4/AAC
        # output: recovering PCM from that means demuxing fMP4 and decoding
        # AAC, so the audio would have been through a second lossy codec for
        # nothing. Listeners attach and detach without the RTSP session
        # noticing, which is what lets transcription be toggled at runtime on
        # hardware where reopening a session is a real risk.
        self._audio_listeners: list = []
        self._supervisor_task: asyncio.Task | None = None
        self._ffmpeg: asyncio.subprocess.Process | None = None
        self._consecutive_failures = 0
        # Manual reboots (POST /scanners/{id}/reboot) and the auto-reboot
        # path (_supervise_forever(), on repeated RTSP failures) both call
        # trigger_reboot() independently -- without this lock, a manual
        # reboot and an auto-triggered one landing close together (a real
        # sequence seen on real hardware: RTSP failing is often *why*
        # someone clicks reboot) fire two overlapping power-cycle requests
        # at the same router port concurrently, which RouterOS's own
        # power-cycle timing isn't guaranteed to handle cleanly.
        self._reboot_lock = asyncio.Lock()
        # The fragmented-MP4 init segment (ftyp+moov) that ffmpeg emits once,
        # right at the start of its output, before any moof/mdat fragments.
        # MSE requires it to be the *first* thing appended to a SourceBuffer,
        # but ffmpeg only ever emits it once for the life of the process --
        # and the process runs continuously from add-on startup, independent
        # of subscribers. Without caching and replaying it here, only a
        # subscriber connected at that exact moment (in practice: none) would
        # ever get valid audio; everyone else's SourceBuffer.appendBuffer()
        # fails on the first (init-less) fragment and playback silently never
        # starts. See _pump_stdout() for capture, subscribe() for replay.
        self._init_segment: bytes | None = None

    def start(self) -> None:
        """Open the RTSP session once and keep it running for the add-on's
        whole lifetime -- see the module docstring for why this isn't tied
        to subscriber count. Call once at add-on startup, regardless of
        whether any HTTP client is listening yet.
        """
        if self._supervisor_task is None:
            self._supervisor_task = asyncio.create_task(
                self._supervise_forever(), name=f"{self.id}-audio"
            )

    async def stop(self) -> None:
        """Tear the audio session down for good.

        Not part of the normal lifetime -- the session is meant to stay up
        for as long as the add-on runs (see start()). This exists for the
        one case where that isn't true: a scanner being removed or
        reconfigured from the settings UI while the add-on keeps running
        (see manager.py). Cancelling the supervisor task runs
        _run_session's `finally`, which sends the RTSP TEARDOWN -- worth
        keeping rather than just dropping the socket, given this scanner's
        RTSP server is what wedges when sessions aren't closed cleanly.
        """
        task, self._supervisor_task = self._supervisor_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # expected -- we just cancelled it above
            except Exception:
                logger.exception("%s: audio supervisor raised while being stopped", self.id)
        if self._ffmpeg and self._ffmpeg.returncode is None:
            self._ffmpeg.kill()
        self._ffmpeg = None
        self._close_all_subscribers("the audio bridge was stopped")
        logger.info("%s: audio bridge stopped", self.id)

    async def subscribe(self) -> "asyncio.Queue[bytes]":
        queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE)
        # Put the cached init segment in before registering the queue as a
        # subscriber, so it's guaranteed to be the first item _pump_stdout's
        # live fan-out ever adds after it -- otherwise a live fragment could
        # race in ahead of it via the loop below.
        if self._init_segment is not None:
            queue.put_nowait(self._init_segment)
        self._subscribers.add(queue)
        logger.info("%s: audio subscriber added (%d now)", self.id, len(self._subscribers))
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[bytes]") -> None:
        self._subscribers.discard(queue)
        logger.info("%s: audio subscriber removed (%d left)", self.id, len(self._subscribers))

    def add_audio_listener(self, callback) -> None:
        """callback(packet: RtpPacket) -> None, for every RTP packet received.

        `packet.payload` is raw G.711 mu-law at 8kHz, straight off the wire,
        and the other two fields are the sender's own sequence number and
        sample clock. It is called with AUDIO_RESET (an empty payload) when
        the audio session restarts, to say that what comes next is not
        contiguous with what came before -- same sentinel idiom as
        STREAM_CLOSED above, and unambiguous for the same reason:
        _RtpProtocol drops any datagram with no payload in it, so a real
        packet is never empty.
        """
        self._audio_listeners.append(callback)
        logger.info("%s: audio listener added (%d now)", self.id, len(self._audio_listeners))

    def remove_audio_listener(self, callback) -> None:
        if callback in self._audio_listeners:
            self._audio_listeners.remove(callback)
        logger.info("%s: audio listener removed (%d left)", self.id, len(self._audio_listeners))

    def _fan_out_audio(self, packet: RtpPacket) -> None:
        for callback in list(self._audio_listeners):
            try:
                callback(packet)
            except Exception:
                # This runs on the RTP receive path. An exception escaping
                # here reaches the datagram handler and takes the audio the
                # *browser* is listening to down with it -- a transcription
                # bug must never cost anyone live audio.
                logger.exception("%s: an audio listener failed", self.id)

    def _fan_out(self, chunk: bytes) -> None:
        """Hand `chunk` to every subscriber, ending the stream of any that
        has fallen behind rather than dropping bytes out of the middle of it.

        This used to be `if not queue.full(): queue.put_nowait(chunk)` --
        a subscriber that couldn't keep up silently lost bytes, with nothing
        logged anywhere. That's a defensible thing to do to a raw realtime
        audio feed and completely wrong for this one: fragmented MP4 is a
        framed container, so a hole anywhere in it desynchronizes every
        following box header. The client doesn't hear a gap and carry on, it
        stops decoding for good -- and the add-on's log said nothing at all,
        which is exactly the kind of silent failure the rest of this file
        exists to avoid. Ending the response instead (STREAM_CLOSED, see
        api.py's stream_audio) fails visibly and lets the client reconnect
        into a fresh stream that starts with the init segment.
        """
        for queue in list(self._subscribers):
            if not queue.full():
                queue.put_nowait(chunk)
                continue
            logger.warning(
                "%s: an audio subscriber fell behind (queue full at %d chunks, "
                "~%d KiB unread); closing its stream rather than corrupting it",
                self.id, SUBSCRIBER_QUEUE_MAXSIZE, SUBSCRIBER_QUEUE_MAXSIZE * 4,
            )
            self._close_subscriber(queue)

    def _close_subscriber(self, queue: "asyncio.Queue[bytes]") -> None:
        self._subscribers.discard(queue)
        try:
            queue.get_nowait()  # make room for the sentinel in a full queue
        except asyncio.QueueEmpty:
            pass
        queue.put_nowait(STREAM_CLOSED)

    def _close_all_subscribers(self, reason: str) -> None:
        """End every subscriber's HTTP response when the audio session goes
        away. Leaving them connected across a session restart used to hand
        them the *next* ffmpeg process's fragments on the same response --
        a different moov than the init segment they started with, so the
        stream is unplayable from that point on anyway, and until then they
        just sit silently receiving nothing with no indication why.
        """
        if not self._subscribers:
            return
        logger.info(
            "%s: closing %d audio subscriber(s): %s", self.id, len(self._subscribers), reason
        )
        for queue in list(self._subscribers):
            self._close_subscriber(queue)

    def has_reboot_mechanism(self) -> bool:
        return bool(self.poe_reset)

    async def trigger_reboot(self) -> bool:
        """Power-cycle via poe_reset. Returns False if not configured, and
        raises MikrotikApiError if the power-cycle itself failed.

        Deliberately raises rather than returning False on failure: the
        manual path (POST /scanners/{id}/reboot) used to collapse every
        failure into `{"ok": false}` with a 200, so HA's own service call
        saw a *success* and reported nothing at all -- the reason a
        misconfigured poe_reset looked like a button that silently did
        nothing. The caller decides what to do with the error; the API
        handler turns it into a 502 carrying the message.
        """
        if not self.poe_reset:
            return False
        async with self._reboot_lock:
            logger.warning(
                "%s: power-cycling PoE port %r via MikroTik REST API at %s",
                self.id, self.poe_reset.interface, self.poe_reset.url,
            )
            await self.poe_reset.power_cycle()
            return True

    async def _await_control(self) -> None:
        """Block until the scanner's control interface is responding, power-
        cycling the scanner if it stays dark past CONTROL_REBOOT_AFTER (only
        when auto_reboot_on_control_failure is on and poe_reset is set up).

        This is the *only* auto-recovery path that covers a scanner whose
        control interface is down, and it deliberately doesn't share the
        audio-failure counter: a dark control interface produces no audio
        failures at all, because _supervise_forever never opens an RTSP
        session against a scanner it can't reach in the first place. That
        gap is what let a wedged scanner sit there indefinitely with the
        add-on running and nothing recovering it (2026-07-25, see
        docs/protocol-notes.md).

        Lives here, in the audio supervisor, rather than in
        ScannerConnection, because trigger_reboot()/poe_reset/_reboot_lock
        do -- and being on the same loop as the audio-failure reboot means
        the two can't fire concurrently at the same switch port, only in
        sequence.
        """
        if self.control.is_reachable():
            return
        logger.info(
            "%s: waiting for the control interface to be reachable "
            "before starting an audio session", self.id,
        )
        attempts = 0
        while True:
            try:
                await asyncio.wait_for(self.control.wait_until_reachable(), CONTROL_REBOOT_AFTER)
                if attempts:
                    logger.warning(
                        "%s: control interface came back after %d power-cycle(s)",
                        self.id, attempts,
                    )
                return
            except asyncio.TimeoutError:
                pass  # still dark -- fall through and decide whether to power-cycle

            if not (self.auto_reboot_on_control_failure and self.poe_reset):
                # Say why nothing is being done about it, once, rather than
                # going quiet for however long the scanner stays dark. Which
                # of the two is missing matters: one is a toggle in the
                # add-on's Configuration tab, the other is four more fields.
                reason = (
                    "auto_reboot_on_control_failure is off"
                    if self.poe_reset
                    else "no poe_reset is configured"
                )
                logger.warning(
                    "%s: control interface has been unreachable for %.0fs and will not be "
                    "power-cycled automatically (%s) -- waiting for it to come back",
                    self.id, CONTROL_REBOOT_AFTER, reason,
                )
                await self.control.wait_until_reachable()
                return

            if attempts >= CONTROL_REBOOT_MAX_ATTEMPTS:
                logger.error(
                    "%s: control interface still unreachable after %d power-cycle(s) -- "
                    "giving up on power-cycling it (this looks like something PoE can't "
                    "fix: check the scanner is plugged into %r and still has this IP)",
                    self.id, attempts, self.poe_reset.interface,
                )
                await self.control.wait_until_reachable()
                logger.warning("%s: control interface came back on its own", self.id)
                return

            attempts += 1
            logger.warning(
                "%s: control interface unreachable for %.0fs -- power-cycling the scanner "
                "(attempt %d of %d)",
                self.id, CONTROL_REBOOT_AFTER, attempts, CONTROL_REBOOT_MAX_ATTEMPTS,
            )
            try:
                await self.trigger_reboot()
            except MikrotikApiError:
                # Must not escape: this runs on the supervisor loop, and a
                # failed *recovery* attempt killing audio supervision for
                # this scanner outright would be a worse outcome than the
                # failure it's recovering from. Same reasoning as the
                # audio-failure path below.
                logger.exception("%s: MikroTik PoE power-cycle failed", self.id)
            logger.warning(
                "%s: waiting %.0fs for the scanner to boot before re-checking control",
                self.id, REBOOT_COOLDOWN,
            )
            await asyncio.sleep(REBOOT_COOLDOWN)

    def _past_startup_grace(self) -> bool:
        """Whether this bridge has been running long enough for an RTSP
        failure to mean something other than "the last container's session
        is still open".

        See STARTUP_REBOOT_GRACE. Logged when it holds a power-cycle back,
        because a scanner that is genuinely wedged at startup will now take
        a couple of minutes longer to recover and the log should say why
        rather than look like the auto-reboot quietly stopped working.
        """
        if self._supervising_since is None:
            return True
        waited = self._clock() - self._supervising_since
        if waited >= STARTUP_REBOOT_GRACE:
            return True
        logger.warning(
            "%s: %d consecutive audio failures, but only %.0fs since this bridge started -- "
            "not power-cycling yet, because the scanner holds the previous session for up to "
            "60s after an add-on restart and refuses new ones until it expires. Waiting until "
            "%.0fs before treating this as a wedged RTSP server.",
            self.id, self._consecutive_failures, waited, STARTUP_REBOOT_GRACE,
        )
        return False

    async def _supervise_forever(self) -> None:
        """Keep a single RTSP/audio session alive for as long as the add-on
        runs, reconnecting (with a backoff, not immediately) if it ever
        fails -- see the module docstring for why this doesn't tear down
        and reopen the session per-subscriber like it used to.

        Gated on the scanner's UDP control interface (`self.control`, a
        ScannerConnection): a new RTSP session is only opened once the
        control interface has proven reachable, and if control reachability
        is lost while a session is running, that session is torn down right
        away rather than left running (or left to fail on its own, possibly
        much later) against a scanner whose control interface has gone
        dark. Control and RTSP are two independent interfaces on the same
        fragile hardware (see docs/protocol-notes.md), so losing control
        responsiveness is a strong signal the scanner itself is down or
        rebooting, not just that this one interface hiccuped.
        """
        self._supervising_since = self._clock()
        while True:
            await self._await_control()

            session_task = asyncio.create_task(self._run_session())
            control_lost = asyncio.create_task(self.control.wait_until_unreachable())
            done, _pending = await asyncio.wait(
                {session_task, control_lost}, return_when=asyncio.FIRST_COMPLETED
            )

            if control_lost in done:
                logger.warning(
                    "%s: control interface stopped responding; stopping audio session "
                    "until it comes back", self.id,
                )
                session_task.cancel()
                try:
                    await session_task
                except asyncio.CancelledError:
                    pass  # expected -- we just cancelled it above
                except Exception:
                    logger.exception(
                        "%s: audio session raised while being stopped for control loss", self.id
                    )
                if self._ffmpeg and self._ffmpeg.returncode is None:
                    self._ffmpeg.kill()
                self._ffmpeg = None
                # Not counted as an audio-side failure -- the scanner (or its
                # control interface) is what went away, not the RTSP session.
                # The next loop iteration's wait_until_reachable() handles
                # waiting for it to come back before reconnecting.
                continue

            control_lost.cancel()
            try:
                await control_lost
            except asyncio.CancelledError:
                pass  # expected -- we just cancelled it above
            try:
                await session_task
                self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._consecutive_failures += 1
                logger.exception(
                    "%s: audio session failed (%d consecutive)", self.id, self._consecutive_failures
                )
            finally:
                if self._ffmpeg and self._ffmpeg.returncode is None:
                    self._ffmpeg.kill()
                self._ffmpeg = None

            if (
                self.auto_reboot
                and self._consecutive_failures >= AUTO_REBOOT_FAILURE_THRESHOLD
                and self._past_startup_grace()
            ):
                # Unlike the manual path, a failed auto-reboot must not
                # escape: this is the long-lived supervisor loop, and
                # letting MikrotikApiError out of it would kill audio
                # supervision for this scanner entirely over what is only a
                # failed recovery attempt.
                try:
                    rebooted = await self.trigger_reboot()
                except MikrotikApiError:
                    logger.exception("%s: MikroTik PoE power-cycle failed", self.id)
                    rebooted = False
                self._consecutive_failures = 0
                if rebooted:
                    logger.warning(
                        "%s: waiting %.0fs for the scanner to come back up before retrying audio",
                        self.id, REBOOT_COOLDOWN,
                    )
                    await asyncio.sleep(REBOOT_COOLDOWN)
                    continue

            logger.info(
                "%s: waiting %.0fs before reconnecting the audio session", self.id, RECONNECT_BACKOFF
            )
            await asyncio.sleep(RECONNECT_BACKOFF)

    async def _run_session(self) -> None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.rtsp_port), timeout=RTSP_CONNECT_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"{self.id}: RTSP connect to {self.host}:{self.rtsp_port} "
                f"timed out after {RTSP_CONNECT_TIMEOUT}s"
            ) from None
        cseq = 0

        async def rtsp(
            method: str,
            extra_headers: dict | None = None,
            session: str | None = None,
            timeout: float = RTSP_REQUEST_TIMEOUT,
        ) -> dict:
            nonlocal cseq
            cseq += 1
            # Confirmed against the RTSP spec's own packet trace (RTSP.pdf,
            # docs/protocol-notes.md's "Audio (RTSP/RTP)" section): OPTIONS
            # and DESCRIBE address the bare media path with NO trailing
            # slash, while SETUP/PLAY/GET_PARAMETER/TEARDOWN use one --
            # matching the Content-Base DESCRIBE's response returns
            # (".../au:scanner.au/"). Previously every non-SETUP method got
            # the trailing-slash form, which gave OPTIONS/DESCRIBE the wrong
            # URL -- a likely root cause of the earlier-seen 400 Bad Request
            # (see "Still open" above).
            if method == "SETUP":
                path = "/trackID=1"
            elif method in ("OPTIONS", "DESCRIBE"):
                path = ""
            else:
                path = "/"
            url = f"rtsp://{self.host}/au:scanner.au{path}"
            header_lines = [f"{method} {url} RTSP/1.0", f"CSeq: {cseq}", "User-Agent: sds200-bridge"]
            if session:
                header_lines.append(f"Session: {session}")
            for key, value in (extra_headers or {}).items():
                header_lines.append(f"{key}: {value}")
            request = "\r\n".join(header_lines) + "\r\n\r\n"
            writer.write(request.encode("ascii"))
            await writer.drain()
            try:
                return await asyncio.wait_for(_read_rtsp_response(reader), timeout=timeout)
            except asyncio.TimeoutError:
                raise ConnectionError(
                    f"{self.id}: no RTSP response to {method} within {timeout}s "
                    "-- scanner's RTSP server may be wedged (see docs/protocol-notes.md)"
                ) from None

        udp_transport = None
        keepalive_task: asyncio.Task | None = None
        pump_task: asyncio.Task | None = None
        session: str | None = None
        try:
            await rtsp("OPTIONS")
            await rtsp("DESCRIBE", {"Accept": "application/sdp"})
            setup_resp = await rtsp("SETUP", {"Transport": f"RTP/AVP;unicast;client_port={self.rtp_client_port}"})
            session = setup_resp["headers"].get("session", "").split(";")[0]
            await rtsp("PLAY", session=session)

            loop = asyncio.get_running_loop()
            udp_transport, _proto = await loop.create_datagram_endpoint(
                lambda: _RtpProtocol(self._on_rtp_packet),
                local_addr=("0.0.0.0", self.rtp_client_port),
            )

            # A new ffmpeg process means a new init segment; the previous
            # one (if any, from an earlier session) no longer matches this
            # process's moov and must not be replayed to new subscribers.
            self._init_segment = None
            self._ffmpeg = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-loglevel", "error",
                "-f", "mulaw", "-ar", "8000", "-ac", "1", "-i", "pipe:0",
                # Fragmented MP4/AAC, not plain MP3: real-install finding (see
                # docs/protocol-notes.md) -- Firefox's MediaSource Extensions
                # doesn't support "audio/mpeg" (MP3) at all, only "audio/mp4"
                # with an AAC codec string, and MSE-driven playback (fetch()
                # feeding a SourceBuffer) turned out to be necessary anyway --
                # HA's own frontend service worker mishandles a plain
                # <audio src> load of a long-lived stream. empty_moov (no
                # upfront duration/seek table -- there isn't one, this is
                # live) + default_base_moof + frag_duration (fragment
                # boundaries by time, since audio has no video-style
                # keyframes) make ffmpeg emit a continuously-appendable
                # fragment stream suitable for MSE.
                "-c:a", "aac", "-f", "mp4",
                "-movflags", "empty_moov+default_base_moof",
                "-frag_duration", "500000",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )

            pump_task = asyncio.create_task(self._pump_stdout())

            async def keepalive():
                while True:
                    await asyncio.sleep(KEEPALIVE_INTERVAL)
                    await rtsp("GET_PARAMETER", session=session, timeout=RTSP_KEEPALIVE_TIMEOUT)

            keepalive_task = asyncio.create_task(keepalive())
            logger.info("%s: audio session started", self.id)
            await asyncio.gather(pump_task, keepalive_task)
        finally:
            for task in (keepalive_task, pump_task):
                if task:
                    task.cancel()
            if udp_transport:
                udp_transport.close()
            if session:
                try:
                    await rtsp("TEARDOWN", session=session)
                except Exception:
                    pass
            writer.close()
            self._close_all_subscribers("the audio session ended")
            if self._audio_listeners:
                self._fan_out_audio(AUDIO_RESET)
            logger.info("%s: audio session ended", self.id)

    async def _pump_stdout(self) -> None:
        assert self._ffmpeg and self._ffmpeg.stdout
        init_buf = bytearray()
        while self._init_segment is None:
            chunk = await self._ffmpeg.stdout.read(4096)
            if not chunk:
                return
            init_buf.extend(chunk)
            init_segment, rest = _split_init_segment(init_buf)
            if init_segment is None:
                continue
            self._init_segment = init_segment
            self._fan_out(init_segment)
            if rest:
                self._fan_out(rest)

        while True:
            chunk = await self._ffmpeg.stdout.read(4096)
            if not chunk:
                break
            self._fan_out(chunk)

    def _on_rtp_packet(self, packet: RtpPacket) -> None:
        if self._ffmpeg and self._ffmpeg.stdin and not self._ffmpeg.stdin.is_closing():
            try:
                self._ffmpeg.stdin.write(packet.payload)
            except Exception:
                logger.debug("%s: failed writing RTP payload to ffmpeg", self.id, exc_info=True)
        # After ffmpeg, not before: the browser's live audio is the latency
        # that a person actually notices, and nothing here should get in
        # front of it.
        if self._audio_listeners:
            self._fan_out_audio(packet)


def parse_rtp(data: bytes) -> RtpPacket | None:
    """Split an RTP datagram into its payload and its timing, or None.

    No version check, deliberately: this socket is the client port *this*
    process negotiated in the RTSP SETUP for a unicast stream, so whatever
    arrives on it is the stream. The header bits are honoured as RFC 3550
    defines them, because getting the payload offset wrong does not fail
    loudly -- it splices header bytes into the audio as a click.
    """
    if len(data) <= RTP_HEADER_LEN:
        return None
    flags = data[0]
    end = len(data)
    if flags & 0x20:  # padding: the last byte counts the bytes to ignore
        pad = data[-1]
        if not 0 < pad <= end - RTP_HEADER_LEN:
            return None
        end -= pad
    start = RTP_HEADER_LEN + 4 * (flags & 0x0F)  # CSRC list
    if flags & 0x10:  # one-per-packet header extension
        if end < start + 4:
            return None
        start += 4 + 4 * int.from_bytes(data[start + 2:start + 4], "big")
    if end <= start:
        return None
    return RtpPacket(
        data[start:end],
        int.from_bytes(data[2:4], "big"),
        int.from_bytes(data[4:8], "big"),
    )


class _RtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self._on_packet = on_packet

    def datagram_received(self, data: bytes, addr) -> None:
        packet = parse_rtp(data)
        if packet is not None:
            self._on_packet(packet)


async def _read_rtsp_response(reader: asyncio.StreamReader) -> dict:
    status_line = (await reader.readline()).decode("ascii", errors="replace").strip()
    headers: dict[str, str] = {}
    while True:
        line = (await reader.readline()).decode("ascii", errors="replace")
        if line in ("\r\n", "\n", ""):
            break
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    body = b""
    if "content-length" in headers:
        body = await reader.readexactly(int(headers["content-length"]))
    if not status_line.startswith("RTSP/1.0 200"):
        raise ConnectionError(f"RTSP error: {status_line}")
    return {"status": status_line, "headers": headers, "body": body}
