"""Turning what the scanner heard into text on the history row.

Shaped like `WeatherWatch`/`StuckWatch` -- `attach`/`detach`/`configure` --
so `manager.py` can apply a settings change to it in place rather than
restarting a scanner. That matters more here than the code cost suggests:
the audio this reads arrives through `AudioBridge`'s raw-payload listener
list, which attaches and detaches without the RTSP session noticing, and a
scanner restart would tear that session down and reopen it on hardware where
doing so is the one thing known to wedge it.

The path is: RTP payloads -> a per-scanner ring buffer and voice segmenter
(`audio_tap`) -> a bounded queue -> a worker that pulls the segment's audio
out of the buffer, sends it to a speech-to-text server (`stt`), decides
whether to believe the answer, and writes it to the call's history row.

**Why the audio decides the boundaries and the history row only supplies the
label.** `history.py` already reconstructs calls, so cutting audio on its
timestamps looks like free reuse. Measured on real hardware (2026-08-19) it
is worse than useless: a call is noticed up to a GSI poll late and held open
for two more after it ends, and in three of six minutes sampled *every*
frame carrying audio fell outside the window history called a call. Cutting
there would have fed the model silence and missed the transmissions.

**Why so much of this is about refusing to believe the model.** Whisper does
not degrade gracefully. Given weak audio it falls back on its language prior
and returns fluent, confident, invented text -- and this audio is weak by
construction: 8kHz telephone bandwidth, and on a digital system already
through a vocoder that resynthesizes speech rather than carrying it. A
fabricated transcript here is not a bad transcript, it is a false record: it
fires trigger rules and answers searches with events that never happened. So
the defaults are biased hard toward writing nothing, and every rejection is
recorded as a `transcript_status` rather than left as an empty column, so
"nothing was said" stays distinguishable from "we never tried".

The strongest guard is the one that costs nothing: the segmenter never emits
a clip without audio in it, and on this hardware a closed squelch is
*exactly* the mu-law silence code, so that gate is unusually solid here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import wave
from collections import deque
from typing import NamedTuple

import align
import audio_tap
import stt

logger = logging.getLogger(__name__)

CLIP_DIR = "/data/clips"

# Kept so a transcript can be judged against what was actually said -- the
# only way to tell a good one from a plausible invention is to listen. Recent
# calls only: at 8kHz mono 16-bit a clip is ~16kB/s, so a few hundred is a
# handful of megabytes and a few hundred thousand would be a problem.
DEFAULT_KEEP_CLIPS = 300

# How many separate clips one history row may accumulate. More than a couple
# means several segments are landing on the same row, which is a matching
# problem wearing an audio problem's clothes.
MAX_CLIPS_PER_CALL = 4

# Queue depth. Small on purpose, and smaller than it looks: this is a number
# of *clips*, and a model running slower than real time turns it into a
# duration. Measured on a real install, medium-int8 on CPU took 33 seconds
# for a 4.9 second clip -- at which point a 32-deep queue is a seventeen
# minute backlog, and every transcript in it lands long after anyone has
# stopped caring which call it belonged to.
QUEUE_MAXSIZE = 8

# A clip older than this is dropped unheard when it reaches the front of the
# queue. The point of transcription here is a searchable log of what was just
# said; audio from four minutes ago arriving now is not worth the CPU it
# would take, and spending that CPU is precisely what keeps the queue behind.
# Dropping stale work is how a pipeline that has fallen behind catches up
# instead of falling further.
MAX_CLIP_AGE_S = 240.0

# Below this a clip is nearly all hallucination risk and nearly no payload:
# too short to carry a useful phrase, plenty long enough for the decoder's
# language prior to invent one.
MIN_DURATION_S = 1.0

# How long to keep looking for the history row a segment belongs to.
#
# The two are produced by different clocks and the audio is always first: the
# segmenter releases a transmission about half a second after the speech
# stops, while the row only appears when the next GSI poll notices the
# squelch -- up to GSI_POLL_INTERVAL later -- and is only *finished* after
# IDLE_POLLS_TO_END more. So for a short transmission the audio is ready to
# transcribe before anything exists to attach it to, and giving up at that
# point loses exactly the calls the VAD path was added to catch.
# Counted in attempts rather than measured against a deadline: this class
# takes an injected clock for the audio timestamps, and a wall-clock deadline
# paired with a real asyncio.sleep would never expire under a frozen one.
FIND_CALL_ATTEMPTS = 6
FIND_CALL_RETRY_S = 2.0

# Whisper emits these over silence and noise -- subtitle boilerplate absorbed
# from its training data. Matched case-insensitively as substrings, because
# they arrive with varying punctuation and capitalisation.
KNOWN_ARTIFACTS = (
    "thank you for watching", "thanks for watching", "subscribe",
    "[blank_audio]", "[music]", "[silence]", "you're watching",
    "see you next time", "www.", ".com", "♪",
)

# Rejection thresholds, applied only when the backend reports them -- Wyoming
# does not (see stt.py). None means "not measured", which must not be read as
# "measured and fine".
MAX_NO_SPEECH_PROB = 0.6
MIN_AVG_LOGPROB = -1.0

# Statuses written to history.transcript_status. Anything other than OK means
# the column is empty for a reason, and the reason is worth keeping.
OK = "ok"
NO_SPEECH = "no-speech"
TOO_SHORT = "too-short"
DOUBTFUL = "doubtful"
ARTIFACT = "artifact"
REPEATED = "repeated"
ERROR = "error"
NO_CALL = "no-call"
# A row was written and no audio ever arrived to transcribe.
#
# What this means depends on which question built the row, and that changed in
# 0.7.54. While a call was a run of polls with the squelch open it was common
# and not a fault -- a data burst or a noise blip opens the squelch as readily
# as speech does, measured at ~23% of "calls" on one real scanner, and on a
# trunked site far more. Now that a call is an unmute, the scanner played this
# one by definition, so hearing nothing of it points at our own audio path
# rather than at the transmission.
#
# Recorded rather than left blank either way, because a blank row is
# indistinguishable from one recorded before transcription was switched on.
NO_AUDIO = "no-audio"

# Not "nothing was said" but "we cannot hear this scanner at all".
#
# A dead audio path looks exactly like a quiet channel from the history's
# point of view: the control interface keeps reporting the squelch opening,
# RTP keeps flowing, and every byte of it is the silence code. That state has
# been seen on this hardware -- a scanner streaming pure silence at a normal
# volume setting while its control side reported traffic -- and reporting it
# per call as "squelch opened but nothing was said" asserts something about
# the transmission that we are in no position to claim.
#
# Distinguished by persistence rather than by any one call. Any real audio
# clears it, so a genuinely quiet channel never trips it and a feed that
# comes back says so on the next transmission.
NO_FEED = "no-feed"

# Audio for this call is queued or being transcribed right now.
#
# Written when a call ends with work still outstanding for the moment it
# covers, and overwritten by whatever that work concludes. Worth saying: the
# gap between a call ending and its transcript arriving is a model's runtime
# plus a queue -- seconds at best, minutes when the queue is deep -- and for
# all of it the row was blank, which is what a call nobody ever tried to
# transcribe also looks like.
PENDING = "pending"

# Audio arrived for this call and was thrown away untranscribed, because the
# model was further behind than the pipeline is willing to get.
#
# PENDING is a promise -- "a transcript is coming" -- and every promise needs
# an ending. There were two paths that dropped a clip without keeping it: a
# full queue evicting the oldest, and a clip reaching the front too stale to
# be worth the CPU. Both left the row saying "transcribing" for as long as
# the add-on ran, which is worse than either the truth or a blank: it is a
# progress indicator for work that stopped.
#
# Distinct from NO_AUDIO on purpose. Nothing here says the transmission was
# silent -- we heard it and gave up on it, which is a statement about this
# add-on rather than about the radio, and points at the fix (a smaller model,
# or fewer scanners transcribed).
DROPPED = "dropped"

# Consecutive calls with no audio behind them before the feed itself is
# doubted rather than the transmissions. Low enough to be noticed, high
# enough that a run of squelch blips on a busy channel does not trip it.
SILENT_CALLS_BEFORE_DOUBTING_FEED = 3


# How long a scanner has to have been tapped before "we have never heard a
# sound from this one" is worth saying rather than "we have only just
# started listening".
#
# Once that has passed, a single silent call is enough. A scanner whose feed
# is dead and one whose channel is quiet are not actually hard to tell apart:
# a quiet channel produces audio *sometimes*. Calls being logged while
# nothing whatsoever has arrived is the wedge, and waiting for a run of them
# only delays saying so.
SILENT_GRACE_S = 60.0

# How recently audio must have been detected for the feed to be considered
# alive regardless of how many calls have gone by without any.
#
# Calls and segments do not correspond one to one: a single segment routinely
# covers several calls, so the calls after the one it was attributed to end
# with nothing of their own and look silent. On a real install that produced
# a dead-feed warning while audio was demonstrably arriving. Consecutive
# silent calls are only evidence when nothing has been heard for a while.
FEED_ALIVE_S = 120.0


def looks_repeated(text: str) -> bool:
    """Whether `text` is Whisper stuck in a loop.

    Its classic failure on weak input is to emit one phrase over and over
    until the window fills. Cheap to spot -- a real transmission does not
    repeat one word for a paragraph -- and worth spotting, because the result
    is long, fluent and completely false.
    """
    words = text.split()
    if len(words) < 3:
        return False
    # Three was six, which let "BANG! BANG! BANG! BANG! BANG!" through -- a
    # real return from a real install, and a textbook Whisper hallucination
    # on percussive noise. Five identical words is a loop by any reading, and
    # ordinary speech does not repeat one word for a third of a sentence:
    # "engine twelve engine twelve respond to a structure fire" has nine
    # distinct words in eleven and is nowhere near this.
    return len(set(words)) <= max(1, len(words) // 3)


def is_artifact(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in KNOWN_ARTIFACTS)


def wanted_mode(record_mode: str, modes: str) -> bool:
    """Whether a call in `record_mode` is one this scanner wants transcribed.

    Empty means every mode. The filter exists because analog and digital are
    genuinely different problems for a speech model -- digital audio has
    already been through a vocoder -- so "worth it on the analog channels,
    not on the trunk" is a real answer someone may need to express.
    """
    wanted = [m.strip().lower() for m in (modes or "").split(",") if m.strip()]
    return not wanted or (record_mode or "").lower() in wanted


class ClipStore:
    """Recent call audio on disk, oldest pruned first.

    Not in SQLite: audio is big, opaque to every query the history makes, and
    would drag the retention logic into rewriting the database rather than
    unlinking a file.
    """

    def __init__(self, directory: str = CLIP_DIR, keep: int = DEFAULT_KEEP_CLIPS):
        self.directory = directory
        self.keep = keep

    def append(self, call_id: int, name: str, pcm: bytes, rate: int) -> str | None:
        """Add more audio to a clip that already exists.

        A call covered by two segments gets both transcripts, so it has to
        get both pieces of audio too -- otherwise the text describes more
        than the clip contains, and anyone checking the transcript against
        what they can hear finds words that are not there.
        """
        path = self.path(name)
        try:
            with wave.open(path, "rb") as existing:
                if existing.getframerate() != rate:
                    return name  # different rate; keep what is already there
                have = existing.readframes(existing.getnframes())
            with open(path, "wb") as out:
                out.write(audio_tap.pcm_to_wav(have + pcm, rate))
        except (OSError, wave.Error):
            logger.warning("could not extend the clip for call %s", call_id, exc_info=True)
        return name

    def save(self, call_id: int, mulaw: bytes, sent=None) -> str | None:
        """Store the clip for a call.

        `sent` is (pcm, rate) as the model actually received it, when that is
        known. Kept in preference to the raw mu-law because the clip exists
        to let a transcript be judged, and judging it against audio the model
        never heard answers the wrong question -- the resampling in between is
        exactly the kind of thing that could be at fault.

        Never over the top of a clip that is already there. The name is the
        call's id, so a second segment landing on the same row aims at the
        same file -- and this used to truncate it. The row's transcript is
        decided *afterwards*, and a fragment carrying no words is discarded
        at that point, so the row kept its text and its duration while the
        file underneath became half a second of nothing. Reported from the
        real install as a long call with a transcript whose clip is short and
        silent, and it needs no unusual state to happen: any restart empties
        the memory of what was already stored.
        """
        data = audio_tap.pcm_to_wav(*sent) if sent else audio_tap.to_wav(mulaw)
        try:
            os.makedirs(self.directory, exist_ok=True)
            name = self._free_name(call_id)
            with open(os.path.join(self.directory, name), "wb") as out:
                out.write(data)
        except OSError:
            # A clip is a convenience for judging a transcript, not the
            # transcript itself -- losing one must not lose the text too.
            logger.warning("could not write the clip for call %s", call_id, exc_info=True)
            return None
        self.prune()
        return name

    def _free_name(self, call_id: int) -> str:
        """`7.wav`, or `7-2.wav` if that is taken, and so on.

        Bounded: past a handful of clips for one row something is wrong with
        the matching rather than with the audio, and the last name is reused
        rather than growing the directory forever.
        """
        for suffix in range(1, MAX_CLIPS_PER_CALL + 1):
            name = f"{call_id}.wav" if suffix == 1 else f"{call_id}-{suffix}.wav"
            if not os.path.exists(self.path(name)):
                return name
        return f"{call_id}-{MAX_CLIPS_PER_CALL}.wav"

    def path(self, name: str) -> str:
        return os.path.join(self.directory, os.path.basename(name))

    def names(self) -> list[str]:
        """Every clip on disk. Missing directory reads as empty, not as an
        error: nothing has been kept yet is the first-run state."""
        try:
            return [n for n in os.listdir(self.directory) if n.endswith(".wav")]
        except OSError:
            return []

    def discard(self, names) -> int:
        """Delete named clips, e.g. because their calls have been deleted.

        Audio that outlives its call is worse than wasted disk. Clips are
        named after the row id, and SQLite hands the id of a deleted row
        straight back to the next call, so an orphan is not merely
        unreachable -- it is a file the *next* call's clip will be confused
        with, both by `prune` (which reads recency out of the id) and by
        anyone listening to it.
        """
        dropped = 0
        for name in names:
            try:
                os.unlink(self.path(name))
                dropped += 1
            except OSError:
                # Already gone is the outcome asked for.
                pass
        return dropped

    def prune(self) -> int:
        names = self.names()
        if len(names) <= self.keep:
            return 0
        # By call id, which is monotonic *for as long as the history it
        # names is intact*, rather than by mtime: it needs no stat per file
        # and cannot be confused by a filesystem with coarse timestamps.
        # Clearing the history restarts the ids, so the clips are deleted
        # with it and any that survive that are reconciled at startup --
        # otherwise every clip written afterwards is the lowest-numbered
        # file in a full directory and prunes itself the moment it is
        # written, which is a play button that plays nothing.
        def key(name: str) -> int:
            # The id, ignoring any `-2` a second clip for the same row got.
            # Without that these sort as zero, which reads as "the oldest
            # file here" and deletes them first.
            try:
                return int(name[:-4].split("-")[0])
            except ValueError:
                return 0

        dropped = 0
        for name in sorted(names, key=key)[:len(names) - self.keep]:
            try:
                os.unlink(os.path.join(self.directory, name))
                dropped += 1
            except OSError:
                pass
        return dropped


class _Tap(NamedTuple):
    """One scanner's audio path.

    A tuple with names on it, because it grew a fifth member and positional
    unpacking at four separate call sites is how a refactor silently swaps
    two of them.
    """

    buffer: "audio_tap.AudioRingBuffer"
    segmenter: "audio_tap.VoiceSegmenter"
    callback: object
    bridge: object
    clock: "audio_tap.RtpClock"
    correlator: "align.Correlator"


class Transcriber:
    """One per add-on. Holds a tap per scanner and one worker for all of them.

    A single worker rather than one per scanner on purpose: the bottleneck is
    a model on another machine, and giving four scanners four concurrent
    requests to it would make every transcript slower without making any of
    them arrive sooner.
    """

    def __init__(self, history=None, clips: ClipStore | None = None,
                 clock=time.time):
        self.history = history
        self.clips = clips or ClipStore()
        self._clock = clock
        self._client = None
        self._settings: dict[str, dict] = {}
        self._taps: dict[str, tuple] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._worker: asyncio.Task | None = None
        self.dropped = 0
        self.stale = 0
        self._last_clip: str | None = None
        # Per call: which segment's audio its clip already holds, and what
        # that clip is called. Kept here rather than read back off the record
        # because the record a pass is working from was fetched before the
        # previous pass wrote the clip, so it is reliably out of date.
        self._clipped: dict[int, tuple[tuple[float, float] | None, str | None]] = {}
        # Consecutive calls per scanner that produced no audio at all.
        self._silent_calls: dict[str, int] = {}
        # Spans of audio queued or in the worker, per scanner, so a call
        # ending can tell "we are still working on this" from "there was
        # nothing to work on".
        self._inflight: dict[str, list[tuple[float, float]]] = {}
        # When each tap was attached, and whether it has ever heard anything.
        self._attached_at: dict[str, float] = {}
        self._heard: set[str] = set()
        self._last_voice_at: dict[str, float] = {}
        # Instance attributes so a test can collapse the waiting.
        self.find_call_attempts = FIND_CALL_ATTEMPTS
        self.find_call_retry_s = FIND_CALL_RETRY_S
        self.max_segment_s = audio_tap.DEFAULT_MAX_SEGMENT_S
        self.close_frames = audio_tap.DEFAULT_CLOSE_FRAMES

    # -- configuration --------------------------------------------------

    def clear(self, reason: str) -> int:
        """Throw away everything in flight. Returns how many clips went.

        Queued audio was cut under the settings in force when it was
        detected, so after those settings change it is the wrong work: at
        best it spends CPU reproducing the behaviour that was just turned
        off, and at worst -- a queue of 30-second clips from a detector that
        was failing to close -- it holds the pipeline minutes behind while
        every new call is missed.

        Half-open segments and the ring buffers go too. A segment currently
        accumulating was started under the old rules and would otherwise be
        emitted under them.
        """
        dropped = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        for tap in self._taps.values():
            tap.segmenter.reset()
            tap.buffer.clear()
        self._inflight.clear()
        if dropped:
            logger.info("transcription queue cleared (%d clip(s)): %s", dropped, reason)
        return dropped

    def set_limits(self, *, max_segment_s: float,
                   silence_gap_s: float | None = None) -> None:
        """Apply settings that shape future segments.

        Applied to running segmenters as well as to ones built later, so a
        saved change takes effect without restarting a scanner -- which is
        the whole reason these settings are in WATCH_ONLY_FIELDS.
        """
        close_frames = self.close_frames
        if silence_gap_s:
            close_frames = max(1, round(silence_gap_s / audio_tap.FRAME_S))
        if max_segment_s != self.max_segment_s or close_frames != self.close_frames:
            self.clear("the segmentation settings changed")
        self.max_segment_s = max_segment_s
        self.close_frames = close_frames
        for tap in self._taps.values():
            tap.segmenter.max_segment_s = max_segment_s
            tap.segmenter.close_frames = close_frames

    def set_client(self, client) -> None:
        """Point at a speech-to-text server, or None to switch off.

        Anything already queued is dropped when the server changes: it was
        going to be sent somewhere else, and holding a backlog across the
        change is the surest way for a settings edit to look like it did
        nothing for the next several minutes.
        """
        before = self._client.describe() if self._client is not None else None
        after = client.describe() if client is not None else None
        if before != after:
            self.clear("the speech-to-text server changed")
        self._client = client
        if client is not None:
            logger.info("transcription will use %s", after)

    def configure(self, scanner_id: str, *, enabled: bool, modes: str = "",
                  require_audio: bool = False) -> None:
        self._settings[scanner_id] = {
            "enabled": bool(enabled),
            "modes": modes or "",
            "require_audio": bool(require_audio),
        }

    def enabled_for(self, scanner_id: str) -> bool:
        return bool(self._settings.get(scanner_id, {}).get("enabled"))

    def requires_audio(self, scanner_id: str) -> bool:
        return bool(self._settings.get(scanner_id, {}).get("require_audio"))

    # -- kept audio -----------------------------------------------------

    def discard_clips(self, names) -> int:
        """Delete kept audio, and forget it was ever there.

        Both halves matter. `_clipped` is keyed by call id, and a cleared
        history hands those ids straight back out, so an entry left behind
        makes the *next* call with that id take the extend path -- appending
        its audio to a deleted call's clip, or to nothing at all.
        """
        names = list(names)
        dropped = self.clips.discard(names)
        stale = set(names)
        for call_id, (_segment, clip) in list(self._clipped.items()):
            if clip in stale:
                del self._clipped[call_id]
        return dropped

    def reconcile_clips(self) -> int:
        """Drop clips whose call has gone. Returns how many went.

        Run at startup, because a clip can outlive its row without anyone
        asking it to: a history cleared by an older version, a
        `/data/history.db` deleted by hand, a crash between the two
        deletions. Left alone those orphans are permanent -- they hold the
        highest ids in the directory, so `prune` reads them as the newest
        audio there is and deletes every real clip written after them
        instead. Cheap enough to be unconditional: one listdir and one
        indexed lookup over at most `keep` ids.
        """
        if self.history is None:
            return 0
        names = self.clips.names()
        ids = {}
        for name in names:
            try:
                ids[int(name[:-4])] = name
            except ValueError:
                continue  # not ours to judge
        if not ids:
            return 0
        live = self.history.existing_ids(ids)
        orphans = [name for call_id, name in ids.items() if call_id not in live]
        if not orphans:
            return 0
        dropped = self.discard_clips(orphans)
        logger.info("dropped %d clip(s) whose call is no longer in the history", dropped)
        return dropped

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._work_forever(), name="transcribe")

    async def stop(self) -> None:
        task, self._worker = self._worker, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # expected -- we just cancelled it
        if self._client is not None:
            await self._client.close()

    def attach(self, scanner_id: str, bridge,
               poll_interval: float = align.GSI_POLL_INTERVAL) -> None:
        """Start following a scanner's audio.

        Attached for every scanner regardless of whether transcription is on
        for it: the tap is a list append and a few kB of ring buffer, while
        making attachment conditional would mean re-deriving it on every
        settings save, which is exactly the kind of bookkeeping that ends up
        tearing down an audio session by accident.
        """
        if scanner_id in self._taps:
            return
        buffer = audio_tap.AudioRingBuffer(clock=self._clock)
        segmenter = audio_tap.VoiceSegmenter(
            lambda start, end, sid=scanner_id: self._on_segment(sid, start, end),
            clock=self._clock,
            max_segment_s=self.max_segment_s,
            close_frames=self.close_frames,
        )
        rtp = audio_tap.RtpClock(clock=self._clock)
        # The correlator's tolerances follow the poll loop this scanner is
        # actually running, which is a per-scanner setting.
        correlator = align.Correlator(scanner_id, poll_interval=poll_interval,
                                      clock=self._clock)

        def tap(packet, sid=scanner_id) -> None:
            if not packet.payload:  # audio_bridge.AUDIO_RESET
                segmenter.reset()
                buffer.clear()
                rtp.reset()
                correlator.forget_audio("the audio session restarted")
                return
            # Both halves of the tap are placed by the same clock. They used
            # to disagree -- the buffer filed audio under when it arrived
            # while the segmenter counted bytes -- so a segment could name a
            # span the buffer would answer with different seconds.
            anchored = rtp.resyncs
            start, _gap = rtp.stamp(packet.payload, packet.timestamp, packet.seq)
            if rtp.resyncs != anchored:
                # The media clock just moved. Spans placed by the old anchor
                # and spans placed by the new one are on different clocks, and
                # correlating across the join blurs two offsets into one.
                correlator.forget_audio("the audio clock re-anchored")
            buffer.append(packet.payload,
                          at=start + len(packet.payload) / audio_tap.SAMPLE_RATE)
            segmenter.feed(packet.payload, at=start)

        self._taps[scanner_id] = _Tap(buffer, segmenter, tap, bridge, rtp, correlator)
        self._attached_at[scanner_id] = self._clock()
        bridge.add_audio_listener(tap)

    def detach(self, scanner_id: str) -> None:
        entry = self._taps.pop(scanner_id, None)
        self._settings.pop(scanner_id, None)
        self._silent_calls.pop(scanner_id, None)
        self._inflight.pop(scanner_id, None)
        self._attached_at.pop(scanner_id, None)
        self._heard.discard(scanner_id)
        self._last_voice_at.pop(scanner_id, None)
        if entry is None:
            return
        entry.bridge.remove_audio_listener(entry.callback)

    def on_call(self, event: str, record: dict) -> None:
        """Plugged into `ReceiveHistory.add_listener`.

        Marks a finished call that never produced any audio, so "the squelch
        opened and nobody spoke" stops looking exactly like "this call was
        recorded before transcription existed". Only ever fills in a status
        that is still empty -- a segment for this call may already have been
        transcribed, since the voice segmenter lets go of a transmission
        about half a second after it ends while the poll loop takes two more
        polls to agree it is over.

        A clip still in the queue when this runs is marked here and corrected
        when it lands, which is the right way round: the transient state says
        nothing was heard, and the final state is the truth.
        """
        if event == "start":
            return
        if event != "end" or self.history is None:
            return
        scanner = record.get("scanner_id", "")
        tap = self._taps.get(scanner)
        if tap is not None:
            # One transmission on the log's timeline, whether or not this
            # scanner is being transcribed -- the clocks are worth measuring
            # either way, and the tap runs for every scanner regardless.
            tap.correlator.add_call(record.get("started"), record.get("ended"))
        # The tap runs for every scanner, so a call can be judged against the
        # audio whether or not this one is being transcribed.
        if not (self.enabled_for(scanner) or self.requires_audio(scanner)):
            return
        # Read the stored row rather than trusting the one handed over: this
        # record is the CallTracker's, built from poll snapshots, and has
        # never carried a transcript field. Believing it would mark every
        # call "no audio" regardless of what had already been transcribed.
        stored = self.history.record(record["id"])
        if stored is None or stored.get("transcript_status"):
            return
        scanner_id = record.get("scanner_id", "")
        if self._covered_by_inflight(scanner_id, record["id"]):
            # Still working on audio covering this call. Not silent, and not
            # a silent call for the purposes of doubting the feed either.
            #
            # Asked with the delivery rule rather than with MATCH_SLACK_S's
            # ten seconds: a row marked "transcribing" by a test looser than
            # the one that writes the transcript is a row nothing will ever
            # come back to.
            self.history.set_transcript(record["id"], "", PENDING)
            return

        silent = self._silent_calls.get(scanner_id, 0) + 1
        self._silent_calls[scanner_id] = silent

        # Never heard a thing from this scanner, and long enough since the tap
        # was attached that the silence is not just a slow start. Calls are
        # being logged, so the radio is receiving; nothing is arriving, so we
        # are not. One call is enough to say that.
        listening_for = self._clock() - self._attached_at.get(scanner_id, self._clock())
        never_heard = scanner_id not in self._heard and listening_for >= SILENT_GRACE_S

        # Audio recently -- from a segment that may well have been attributed
        # to an earlier call -- settles it. Whatever these calls look like on
        # their own, the feed is plainly working.
        since_voice = self._clock() - self._last_voice_at.get(scanner_id, 0.0)
        heard_recently = scanner_id in self._heard and since_voice < FEED_ALIVE_S

        doubt_feed = not heard_recently and (
            never_heard or silent >= SILENT_CALLS_BEFORE_DOUBTING_FEED
        )
        status = NO_FEED if doubt_feed else NO_AUDIO

        gated = getattr(self.history, "played", lambda _id: False)(scanner_id)
        if self.requires_audio(scanner_id) and not doubt_feed and not gated:
            # Log only what the scanner actually played. Never while the feed
            # is in doubt: hearing nothing is not evidence that nothing was
            # said, and a wedged audio path would otherwise delete the entire
            # history one call at a time, silently, exactly when the log is
            # most worth keeping.
            #
            # And never at all once the mute flag is answering, because then
            # the question this setting exists to ask has already been asked,
            # by the scanner, before the row was written: a call *is* an
            # unmute. Hearing nothing of it after that says something about
            # our own tap, and deleting somebody's log over that is the same
            # mistake as the wedged-feed case, arriving by a shorter route.
            self.history.drop(record["id"])
            return
        if doubt_feed and silent in (1, SILENT_CALLS_BEFORE_DOUBTING_FEED):
            logger.warning(
                "%s: a call ended with no audio behind it and %s -- the audio feed "
                "looks dead rather than the channel quiet. Check that this scanner's "
                "live audio plays.",
                scanner_id,
                "nothing at all has been heard from this scanner" if never_heard
                else f"{silent} calls in a row have been silent",
            )
        logger.debug(
            "%s: call %s ended with nothing transcribed yet -- marking %s",
            scanner_id, record["id"], status,
        )
        self.history.set_transcript(record["id"], "", status)

    # -- the path from a segment to a row -------------------------------

    def _on_segment(self, scanner_id: str, start: float, end: float) -> None:
        """Runs on the RTP receive path, so it does no work beyond a slice
        and an enqueue -- anything slower here delays the live audio a person
        is listening to."""
        entry = self._taps.get(scanner_id)
        if entry is None:
            return
        mulaw = entry.buffer.slice(start, end)
        if not mulaw:
            logger.debug(
                "%s: a segment at %.1f-%.1f had no audio in the ring buffer",
                scanner_id, start, end,
            )
            return
        # Any real audio means the feed is alive, whatever the last several
        # calls looked like.
        # Recorded before the transcription checks below, so a scanner that
        # is only using require_audio still knows its feed is alive -- and so
        # the dead-feed test is about the audio rather than about whether
        # anyone asked for a transcript.
        self._silent_calls[scanner_id] = 0
        self._heard.add(scanner_id)
        self._last_voice_at[scanner_id] = self._clock()
        # One recording on the audio timeline. Both its edges, not just its
        # onset: what `align` matches is the shape of the traffic and the
        # voids between, and half an event has half a shape.
        #
        # The pre-roll is added back first. A segment is reported starting
        # `preroll_s` before its first loud frame, deliberately, so the
        # opening syllable survives -- but that is half a second of quiet on
        # the front of every recording and nothing on the back, which would
        # tilt every comparison the same way and put a bias straight into the
        # answer.
        preroll = entry.segmenter.preroll_s if entry.segmenter else 0.0
        entry.correlator.add_audio(start + preroll, end)
        if self._client is None or not self.enabled_for(scanner_id):
            return
        logger.debug(
            "%s: voice segment, %.1fs, ended on %s (queue %d/%d)",
            scanner_id, len(mulaw) / audio_tap.SAMPLE_RATE,
            entry.segmenter.last_close if entry.segmenter else "?",
            self._queue.qsize(), QUEUE_MAXSIZE,
        )
        self._inflight.setdefault(scanner_id, []).append((start, end))
        try:
            self._queue.put_nowait((scanner_id, start, end, mulaw))
        except asyncio.QueueFull:
            # Drop the oldest, not the newest. If the model cannot keep up,
            # what is worth having is the most recent audio -- a backlog
            # draining an hour behind real time is worse than a gap, because
            # it looks like a working feature.
            try:
                dropped = self._queue.get_nowait()
                self._forget(dropped[0], dropped[1], dropped[2])
                self._settle(dropped[0], dropped[1], dropped[2], DROPPED)
                self._queue.put_nowait((scanner_id, start, end, mulaw))
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                self._forget(scanner_id, start, end)
                self._settle(scanner_id, start, end, DROPPED)
            self.dropped += 1
            if self.dropped % 10 == 1:
                logger.warning(
                    "transcription is falling behind -- %d clip(s) dropped so far. "
                    "A smaller model, or fewer scanners transcribed, would keep up.",
                    self.dropped,
                )

    def _forget(self, scanner_id: str, start: float, end: float) -> None:
        """Stop counting a span as in flight, however it left the pipeline.

        Every exit has to come through here -- transcribed, dropped for age,
        pushed out of a full queue -- or a row sits reading "transcribing"
        for as long as the add-on runs.
        """
        spans = self._inflight.get(scanner_id)
        if spans and (start, end) in spans:
            spans.remove((start, end))

    def _settle(self, scanner_id: str, start: float, end: float,
                status: str = NO_AUDIO) -> None:
        """End the "transcribing" state on rows this audio will never reach.

        Called where the pipeline gives up on a piece of audio. PENDING is a
        promise that a transcript is coming, and the rows carrying it have no
        other way to learn that it is not: the work that would have written
        one is the work that just stopped.

        Only rows still waiting are touched, and only ones with nothing in
        them. A call that already has words had its audio from somewhere
        else, and a status that has moved on has moved on for a reason.
        """
        if self.history is None:
            return
        for call in self._calls_at(scanner_id, start, end):
            stored = self.history.record(call["id"])
            if stored is None or stored.get("transcript_status") != PENDING:
                continue
            if (stored.get("transcript") or "").strip():
                continue
            if self._covered_by_inflight(scanner_id, call["id"]):
                # Another segment of the same transmission is still being
                # worked on. The promise stands.
                continue
            self.history.set_transcript(call["id"], "", status)

    def settle_pending(self) -> int:
        """End every "transcribing" left over from a previous run.

        Run at startup. The queue lives in memory, so a row that was waiting
        on a clip when the add-on stopped is waiting on something that no
        longer exists -- and nothing else will ever come back to it. Rows in
        that state have been seen months old on a real install, which is a
        progress indicator with nothing behind it.
        """
        if self.history is None:
            return 0
        stranded = self.history.awaiting_transcripts(PENDING)
        for call_id in stranded:
            self.history.set_transcript(call_id, "", DROPPED)
        if stranded:
            logger.info(
                "%d call(s) were still waiting on a transcript from a previous "
                "run -- their audio is long gone, so they now say so.",
                len(stranded),
            )
        return len(stranded)

    def _calls_at(self, scanner_id: str, start: float, end: float) -> list[dict]:
        """The rows a piece of audio covers, asked in the log's own time.

        Every lookup goes through here. The two clocks run apart by an amount
        `align.Correlator` measures, and asking three of the four questions in
        shifted time and the fourth in raw time is how 0.7.46 marked rows
        "transcribing" that nothing would ever write to.
        """
        if self.history is None:
            return []
        tap = self._taps.get(scanner_id)
        if tap is not None:
            start, end = tap.correlator.shift(start, end)
        return self.history.calls_at(scanner_id, start, end)

    def _covered_by_inflight(self, scanner_id: str, call_id: int) -> bool:
        """Whether audio still in the pipeline is going to land on this call.

        Asked with the same rule that decides where a transcript is written
        (`calls_at`), rather than with the looser "could this audio possibly
        belong to this call" test. When the two disagreed, a row was marked
        "transcribing" by one and never written by the other.
        """
        if self.history is None:
            return False
        for start, end in list(self._inflight.get(scanner_id, ())):
            if any(call["id"] == call_id
                   for call in self._calls_at(scanner_id, start, end)):
                return True
        return False

    async def _work_forever(self) -> None:
        while True:
            scanner_id, start, end, mulaw = await self._queue.get()
            age = self._clock() - end
            if age > MAX_CLIP_AGE_S:
                self._forget(scanner_id, start, end)
                self._settle(scanner_id, start, end, DROPPED)
                self.stale += 1
                logger.warning(
                    "%s: dropping a %.1fs clip that waited %.0fs to be transcribed "
                    "(%d so far). The model is slower than the traffic -- a smaller "
                    "one, or fewer scanners transcribed, would keep up.",
                    scanner_id, len(mulaw) / audio_tap.SAMPLE_RATE, age, self.stale,
                )
                continue
            try:
                await self._handle(scanner_id, start, end, mulaw)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("a transcription failed unexpectedly")
            finally:
                self._forget(scanner_id, start, end)
                # Whatever _handle concluded has been written by now, so a
                # row still saying "transcribing" is one this audio turned
                # out not to belong to.
                self._settle(scanner_id, start, end)

    async def _find_calls(self, scanner_id: str, start: float,
                          end: float) -> list[dict]:
        """Every row this segment belongs to, waiting for them to appear.

        Plural because one transmission can be logged as several calls -- the
        log ends a call when the identity changes, while the audio simply
        carries on -- so a segment covering two rows belongs to both, and
        attaching it to only the nearest leaves the other looking like
        nobody spoke.
        """
        if self.history is None:
            return []
        for attempt in range(self.find_call_attempts):
            calls = self._calls_at(scanner_id, start, end)
            if calls:
                return calls
            if attempt + 1 < self.find_call_attempts:
                await asyncio.sleep(self.find_call_retry_s)
        return []

    async def _find_call(self, scanner_id: str, start: float, end: float) -> dict | None:
        """The history row for a segment, waiting for it to appear if need be.

        Waiting is the whole point. The audio is always ready first -- see
        FIND_CALL_TIMEOUT_S -- so looking once and giving up loses every
        transmission short enough to finish inside a poll interval, which is
        the class of call this path exists to catch in the first place.
        """
        if self.history is None:
            return None
        for attempt in range(self.find_call_attempts):
            call = self.history.call_at(scanner_id, start, end)
            if call is not None:
                return call
            if attempt + 1 < self.find_call_attempts:
                await asyncio.sleep(self.find_call_retry_s)
        return None

    async def _handle(self, scanner_id: str, start: float, end: float,
                      mulaw: bytes) -> None:
        duration = len(mulaw) / audio_tap.SAMPLE_RATE
        logger.debug("%s: transcribing a %.1fs segment", scanner_id, duration)

        # A single cheap look before the expensive part. Absence proves
        # nothing here -- the row may simply not exist yet -- so it never
        # stops the work; but when the row *is* already there it lets the
        # clip be stored and offered for playback straight away, instead of
        # after the model has finished. Listening to a call should not wait
        # on a transcript of it.
        wanted = self._modes(scanner_id)
        found = self._calls_at(scanner_id, start, end)
        early = [c for c in found if wanted_mode(c.get("mode") or "", wanted)]
        if found and not early:
            # Every row this covers is a mode nobody asked to transcribe.
            # A positive mismatch, so it is safe to stop before the model --
            # unlike an empty `found`, which only means the rows are late.
            return
        if early and not early[0].get("clip"):
            self._record_all(early, "", PENDING, mulaw, segment=(start, end))

        if duration < MIN_DURATION_S:
            for call in await self._find_calls(scanner_id, start, end):
                self._record(call["id"], "", TOO_SHORT, mulaw)
            return

        try:
            result = await self._client.transcribe(mulaw)
        except stt.SttError as error:
            logger.warning("%s: transcription failed: %s", scanner_id, error)
            result = None

        # Looked up *after* transcribing, not before. The model takes seconds,
        # which is time the poll loop needs anyway -- so doing the slow work
        # first means the row has usually appeared by the time it is wanted,
        # and the wait costs nothing that was not already being spent.
        calls = early or await self._find_calls(scanner_id, start, end)
        if not calls:
            logger.debug(
                "%s: a %.1fs segment matched no call after %d attempts -- discarding it",
                scanner_id, duration, self.find_call_attempts,
            )
            return
        calls = [c for c in calls if wanted_mode(c.get("mode") or "", wanted)]
        if not calls:
            return

        if result is None:
            self._record_all(calls, "", ERROR, mulaw, segment=(start, end))
            return
        text, status = self._judge(result)
        logger.debug(
            "%s: %d call(s) %s -> %s %r", scanner_id, len(calls),
            [c["id"] for c in calls], status, text[:80],
        )
        self._record_all(calls, text, status, mulaw, segment=(start, end))

    def _modes(self, scanner_id: str) -> str:
        return self._settings.get(scanner_id, {}).get("modes", "")

    @staticmethod
    def _judge(result: dict) -> tuple[str, str]:
        """What to believe of what came back.

        Ordered cheapest and most certain first. The confidence checks run
        last because they are the ones that may not be available at all --
        `None` means the backend never reported them, which is not the same
        as reporting that everything was fine.
        """
        text = (result.get("text") or "").strip()
        if not text:
            return "", NO_SPEECH
        if is_artifact(text):
            return "", ARTIFACT
        if looks_repeated(text):
            return "", REPEATED
        no_speech = result.get("no_speech_prob")
        if no_speech is not None and no_speech > MAX_NO_SPEECH_PROB:
            return "", DOUBTFUL
        logprob = result.get("avg_logprob")
        if logprob is not None and logprob < MIN_AVG_LOGPROB:
            return "", DOUBTFUL
        return text, OK

    def _record_all(self, calls: list[dict], text: str, status: str,
                    mulaw: bytes, segment: tuple[float, float] | None = None) -> None:
        """Write one transcript to every row the segment covered.

        The clip is stored once, under the first row, and the rest point at
        the same file -- it is one piece of audio however many rows the log
        split it into, and storing it per row would keep several copies of
        the same seconds.
        """
        first = calls[0]["id"]
        prior_segment, prior_clip = self._clipped.get(first, (None, None))
        # Only a clip *this* run stored is ours to add to. A row can already
        # point at a clip filed under a different first row -- runs overlap
        # at their edges, so a row can be the last of one segment's run and
        # the first of the next -- and taking that as ours appended the new
        # audio to the earlier segment's file. Which every row of the earlier
        # run also points at. So one transmission's audio appeared inside the
        # clip of the transmission before it, and pressing play on a row gave
        # you somebody else's words ahead of your own: reported as the audio
        # not matching the transcript, and the worst possible failure for a
        # feature whose whole job is to let a transcript be checked.
        clip = prior_clip

        # Written once per *segment*. The pending pass and the result pass
        # carry the same audio, so the second must not store it again; a
        # genuinely new segment for the same call is added to what is already
        # there, since its transcript is being added too -- a clip that held
        # only the first of two segments would describe less than the text
        # does, and anyone checking one against the other finds words they
        # cannot hear.
        already = segment is not None and prior_segment == segment
        write = not already
        for index, call in enumerate(calls):
            self._record(call["id"], text, status, mulaw,
                         clip=clip, save_clip=write and index == 0 and clip is None,
                         extend=write and index == 0 and clip is not None)
            if index == 0:
                clip = self._last_clip or clip
        self._clipped[first] = (segment, clip)

    def _record(self, call_id: int | None, text: str, status: str,
                mulaw: bytes, clip: str | None = None,
                save_clip: bool = True, extend: bool = False) -> None:
        if call_id is None or self.history is None:
            return
        # A clip *of its own* is kept even when nothing was written, because a
        # rejected transcript is exactly the case where someone wants to hear
        # what the model was given and decide whether the rejection was right
        # -- so it is stored as the model heard it, resampling included.
        #
        # Adding to a clip that already exists is the opposite case and gets
        # the opposite answer: audio joins a clip only when its words join
        # the transcript. `set_transcript` discards a fragment that carries
        # no text when the row already has some, so appending it anyway grew
        # the audio without growing the account of it -- reported from the
        # real install as a transcript missing things the clip plainly says.
        # Anyone checking one against the other then finds the feature
        # accusing itself of having missed something.
        extend = extend and bool(text)
        if save_clip or extend:
            sent = None
            if self._client is not None and hasattr(self._client, "sent_audio"):
                try:
                    sent = self._client.sent_audio(mulaw)
                except Exception:
                    logger.debug("could not render the sent audio", exc_info=True)
            if extend and clip and sent:
                clip = self.clips.append(call_id, clip, *sent)
            elif save_clip:
                clip = self.clips.save(call_id, mulaw, sent=sent)
        self._last_clip = clip
        self.history.set_transcript(call_id, text, status, clip=clip)
