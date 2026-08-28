"""Raw call audio, tapped off the RTP stream and cut into utterances.

`audio_bridge.py` already receives every RTP packet the scanner sends and
pipes it straight into ffmpeg, which re-encodes it to AAC-in-fragmented-MP4
for the browser. That output is the wrong thing to transcribe from: getting
PCM back out of it means demuxing fMP4 and decoding AAC, so the audio would
have been through a second lossy codec for no reason. This module taps the
*input* side instead -- the raw G.711 mu-law bytes, exactly as the scanner
sent them -- and never touches the RTSP session, so a transcriber attaching
and detaching at runtime cannot disturb the one part of this hardware that
is known to wedge (see audio_bridge.py's module docstring).

Three things live here, and the split matters:

* `RtpClock` says when a payload's audio was *sampled*, from the sender's
  own counters rather than from when the datagram turned up. Both of the
  others take their times from it, which is the only reason their times
  still agree with each other after the network has lost something.
* `AudioRingBuffer` holds the last few minutes of audio keyed by wall clock.
  It answers "what did this scanner hear between t1 and t2".
* `VoiceSegmenter` decides *when* a transmission started and stopped. It
  holds no audio at all -- it reports boundaries, and the caller pulls the
  bytes out of the ring buffer.

**Why VAD rather than the receive history's call boundaries.** history.py
already reconstructs calls, so segmenting on those looks like free reuse. It
isn't, for two reasons that are properties of the poll loop rather than
things a future change can tune away. A call's *start* is only noticed on
the next GSI poll -- 3s later by default (`protocol.GSI_POLL_INTERVAL`) --
so slicing on it clips up to three seconds off the front of every
transmission, which on a dispatch channel is precisely the "Engine 12,
respond to" part. And history's own docstring notes that transmissions
shorter than the poll interval are missed entirely; those are exactly the
short unit acknowledgements worth catching. Squelched scanner audio is
near-silent, so an energy threshold finds both edges within a frame or two.

The segmenter is also the first and most effective guard against the failure
mode that makes this whole feature risky. Whisper does not degrade
gracefully on weak input: given silence or vocoder garble it falls back on
its language prior and emits fluent, confident, invented text. A fabricated
transcript of a public-safety call is worse than no transcript -- it fires
trigger rules and pollutes history search with events that never happened.
Never handing the model a clip without speech energy in it removes most of
that risk before any confidence threshold gets a chance to.

Segmentation is by exact silence rather than by an energy threshold, because
that is what this hardware measured out to (2026-08-19, via the RTP probe --
see DEFAULT_THRESHOLD for the numbers). A threshold is still available for a
site whose receiver has a real noise floor; it is just not what this one
needs, and picking a number where none is needed would only clip the quiet
edges of real transmissions.

The same capture is why segmentation does not use the receive history's call
boundaries even as a hint. In three of six minutes measured, *every* frame
carrying audio fell outside the window history called a call -- the poll lag
slides the window off the transmission and onto the silence after it.
"""

from __future__ import annotations

import io
import logging
import math
import time
import wave
from collections import deque

logger = logging.getLogger(__name__)

# The scanner's RTSP stream is G.711 mu-law, 8 kHz, mono -- 8000 bytes per
# second of audio, one byte per sample. Everything below assumes that; it is
# fixed by the hardware, not configurable.
SAMPLE_RATE = 8000

# 20ms of audio. Small enough that a squelch edge is located to within a
# frame or two, large enough that the RMS of one frame is a meaningful
# measure rather than sample noise.
FRAME_SAMPLES = 160
FRAME_S = FRAME_SAMPLES / SAMPLE_RATE

# How much audio the ring buffer keeps. Five minutes is ~2.4MB per scanner
# (8kB/s), which is nothing next to what it buys: a segment can be pulled
# out well after it ended, so transcription is free to run behind without
# the audio having aged out from under it.
DEFAULT_WINDOW_S = 300.0

# How far the media clock may drift from arrival time before it is thrown
# away and re-anchored. Generous, because being *late* is normal -- that is
# what a jitter buffer and a slow network look like, and re-anchoring on
# every hiccup would defeat the point of having a media clock at all. What
# this catches is the pathological case: a sender whose sample clock has
# jumped, a timestamp wrap misread, or our own process stalled long enough
# that the arithmetic is no longer describing reality.
MAX_MEDIA_SKEW_S = 5.0

# A hole in the audio smaller than this is bridged rather than treated as a
# break in the transmission. One or two lost packets is 20-40ms -- inaudible,
# and not worth cutting a transmission in half over -- while a quarter of a
# second is a splice a listener would hear and a transcript could turn into
# two half sentences.
MAX_BRIDGED_GAP_S = 0.25

# Why a segment ended. Recorded because "some transmissions are getting cut
# in two" cannot be answered from the outside: every one of these produces
# the same thing -- two clips where there should be one -- and they have
# entirely different fixes. Dead air means `silence_gap_s` is too short for
# the pauses on this channel (or the floor has risen, which is why the floor
# is reported alongside). A hole means packets went missing and the fix is
# nowhere near the segmenter. The cap means the detector never closed at all.
CLOSE_SILENCE = "silence"
CLOSE_GAP = "gap"
CLOSE_CAP = "cap"
CLOSE_RESET = "reset"

# A new segment opening this soon after the last one closed is a suspected
# split: real transmissions are not usually separated by less time than the
# silence that ended one of them. Reported per occurrence, because one line
# naming the gap and the reason is worth more than any amount of arguing
# about which setting to change.
SPLIT_SUSPECT_S = 3.0

# How many segments between rolling summaries of why they closed.
CLOSE_REPORT_EVERY = 25

# Both mu-law encodings of zero. A squelched SDS200 sends nothing else --
# see DEFAULT_THRESHOLD.
SILENCE_CODES = frozenset({0xFF, 0x7F})

# RMS (in PCM16 units, full scale 32124) above which a frame counts as
# speech -- or, at zero, "use the exact silence test instead".
#
# Zero because that is what the hardware turned out to do (measured over
# eight minutes on a real SDS200, 2026-08-19, via the RTP probe). Squelched
# output is not quiet, it is *exactly* the silence code: 17,209 of 18,000
# frames sat at RMS 0, and one whole minute ran 3000/3000 frames at zero
# using two distinct byte values. There is no noise floor to clear.
#
# So an exact silence test beats any threshold here on three counts. It has
# nothing to tune and cannot drift. It is cheaper -- a set difference over
# 160 bytes rather than 160 multiply-adds and a square root. And it is
# strictly more sensitive at the edges of a transmission: real audio in the
# same capture had a long low tail (86 frames between RMS 1 and 24 in one
# minute, 54 in another), every one of which a threshold of 500 would have
# discarded -- and those are the onsets the pre-roll exists to protect.
#
# Kept as a setting rather than hard-coded because "no noise floor" is a
# property of this scanner in this installation. A site running an analog
# receiver with the squelch cracked open would have a real floor to clear,
# and would want a threshold; that path is still here, just not the default.
DEFAULT_THRESHOLD = 0.0

# How far above the measured noise floor a frame has to be to count as
# speech, once there *is* a noise floor.
#
# The exact silence test above is right whenever a closed squelch produces
# exactly the silence code, and wrong the moment it does not -- and "does
# not" was observed on the same scanner within a day of the measurement that
# justified it. With any noise at all, no frame is ever silent, so a segment
# opens and never closes until DEFAULT_MAX_SEGMENT_S: 30-second blobs that
# are mostly hiss, which cost the most to transcribe and say the least.
#
# Rather than pick a threshold for this receiver on this day, the segmenter
# learns the floor from the audio itself and opens on anything well above
# it. When the floor really is zero this collapses back to the exact test,
# so the good case is not paid for by the bad one.
NOISE_MARGIN = 3.0

# The floor is the quietest frame seen recently, tracked as a minimum over a
# window rather than as a running average.
#
# An average cannot bootstrap itself here. The floor starts at zero, so every
# frame initially counts as loud, and an average that rises slowly enough not
# to be dragged up by speech also rises far too slowly to escape its own
# starting point -- it spends a minute treating a noisy idle channel as one
# continuous transmission, which is the exact failure this exists to fix.
#
# A minimum has neither problem. It is correct from the first block, and
# speech cannot raise it: loud frames simply are not the minimum. The window
# only has to be longer than a transmission, and on this hardware 99% are
# under six seconds.
FLOOR_BLOCK_FRAMES = 250        # 5s at 20ms a frame
FLOOR_BLOCKS = 6                # so the floor looks back over ~30s

# The floor is never believed above this, however quiet the window has been.
#
# A minimum is only as good as the window containing something quiet, and one
# case reliably does not: a tap attached in the middle of a transmission sees
# nothing but speech, takes that as the floor, and goes deaf for as long as
# the window lasts. Bounding it means the worst a bad start costs is reduced
# sensitivity for a few seconds rather than silence for thirty.
#
# Set well below speech and comfortably above any hiss, so it never binds on
# a receiver behaving normally.
MAX_FLOOR = 1500.0

# Hysteresis, in frames. Opening takes less evidence than closing because
# the costs are asymmetric: a spuriously-opened segment is dropped later by
# the minimum-duration and confidence filters, while a spuriously-closed one
# cuts a real transmission in half mid-word.
DEFAULT_OPEN_FRAMES = 3    # 60ms of speech to open
DEFAULT_CLOSE_FRAMES = 60  # 1.2s of silence to close

# Lengthening the close was not free to arrive at but is nearly free to
# have. Under the old exact-silence test any non-silent frame counted as
# speech, so a breath or an inter-word pause could never close a segment.
# Once the floor is learned, a quiet moment mid-sentence can fall below it,
# and 500ms is well inside the range of an ordinary pause -- so transmissions
# were being cut in half.
#
# The cost is only latency: a segment is emitted ending where the silence
# *began*, so the extra wait is trimmed off rather than transcribed. The
# other cost is merging two transmissions less than this apart into one clip,
# which is a far smaller harm than truncating one.
# Below this, the gap is inside an ordinary pause between phrases -- 0.3 to
# 0.5s is normal speech, not the end of a transmission -- so a setting under
# it cuts people off mid-sentence and turns one transmission into two clips,
# two transcripts and a worse job by the model on both halves. Not a bound:
# a site with clipped, disciplined traffic may genuinely want it, and
# MIN_SILENCE_GAP_S still allows it. It is the point at which the settings
# page should say what will happen. Measured from a real install that had
# been set to 0.3s while chasing exactly that symptom.
ADVISED_MIN_SILENCE_GAP_S = 0.6

MIN_SILENCE_GAP_S = 0.2
MAX_SILENCE_GAP_S = 5.0

# Audio prepended to a segment from before the threshold was crossed. The
# first phoneme of a transmission is usually well under the opening
# hysteresis -- without this, "Engine" reliably becomes "ngine".
DEFAULT_PREROLL_S = 0.5

# A segment is force-closed at this length even if the squelch never shuts,
# and configurable because the right value is a judgement about this
# particular traffic rather than a property of the hardware.
#
# It is a backstop, not a working limit: 99% of transmissions here are under
# six seconds, so a real one never reaches it. Lower costs less when the
# detector fails to close -- each capped segment is a clip of mostly nothing,
# and on a model slower than real time those are what put the queue behind
# and keep it there. Higher avoids cutting a genuinely long message in two.
#
# Thirty by default, which errs toward not truncating anyone. Nothing is
# discarded either way: a capped segment is emitted and transcribed like any
# other, so the cost of the cap is a message split across two rows rather
# than a message lost.
DEFAULT_MAX_SEGMENT_S = 30.0
MIN_MAX_SEGMENT_S = 5.0
MAX_MAX_SEGMENT_S = 120.0

# Below this a clip is nearly all hallucination risk and nearly no payload:
# too short to carry a useful phrase, plenty long enough for the decoder's
# language prior to invent one.
DEFAULT_MIN_SEGMENT_S = 1.0


def _now() -> float:
    return time.time()


def _build_mulaw_table() -> list[int]:
    """G.711 mu-law -> signed 16-bit PCM, all 256 values.

    The canonical bit-twiddling expansion (ITU-T G.711 / the Sun reference
    implementation), evaluated once at import into a lookup table -- decoding
    is on the hot path for every scanner with a tap attached, and a table
    lookup per byte is the difference between free and not.
    """
    table = []
    for byte in range(256):
        value = ~byte & 0xFF
        magnitude = ((value & 0x0F) << 3) + 0x84
        magnitude <<= (value & 0x70) >> 4
        table.append(0x84 - magnitude if value & 0x80 else magnitude - 0x84)
    return table


MULAW_TO_PCM16 = _build_mulaw_table()

# Squared amplitudes, so a frame's RMS is a table lookup and an add per byte
# rather than a decode and a multiply.
_MULAW_SQUARED = [value * value for value in MULAW_TO_PCM16]

# Little-endian PCM16 pairs, so decoding to WAV payload is a join over
# lookups rather than a struct.pack per sample.
_MULAW_PCM_BYTES = [
    int(value & 0xFFFF).to_bytes(2, "little") for value in MULAW_TO_PCM16
]


def decode(mulaw: bytes) -> bytes:
    """mu-law bytes -> little-endian signed 16-bit PCM."""
    return b"".join(map(_MULAW_PCM_BYTES.__getitem__, mulaw))


def frame_rms(mulaw: bytes) -> float:
    """RMS amplitude of `mulaw`, in PCM16 units, without decoding it first."""
    if not mulaw:
        return 0.0
    return math.sqrt(sum(map(_MULAW_SQUARED.__getitem__, mulaw)) / len(mulaw))


def is_silent(mulaw: bytes) -> bool:
    """True when every byte is one of the two mu-law encodings of zero.

    Exact rather than approximate, and that is the point: on this hardware a
    closed squelch produces these codes and nothing else, so "contains any
    other byte" separates silence from audio without a threshold to pick.
    Cheaper than frame_rms too -- one set difference, no arithmetic per
    sample -- which matters on a path that runs for every scanner with a tap
    attached.
    """
    return not (set(mulaw) - SILENCE_CODES)


# Peak level to normalise a clip to before sending it to a speech model, and
# the most any clip may be amplified to get there.
#
# Scanner audio arrives at whatever level the transmitting radio, the path
# and the receiver's own AGC left it at, and quiet speech is a documented way
# to get an empty transcript out of Whisper -- it is more sensitive to level
# than its own normalisation suggests. Standard ASR preprocessing, and cheap.
#
# Peak rather than RMS so nothing can clip, and bounded so a clip that is
# quiet because it contains nothing is not amplified into something that
# sounds like it does.
NORMALISE_PEAK = 0.9
NORMALISE_MAX_GAIN = 8.0


def normalise(pcm: bytes, peak: float = NORMALISE_PEAK,
              max_gain: float = NORMALISE_MAX_GAIN) -> bytes:
    """Bring a clip up to a consistent level, without clipping it."""
    if len(pcm) < 2:
        return pcm
    samples = memoryview(pcm).cast("h")
    loudest = max(max(samples), -min(samples))
    if loudest <= 0:
        return pcm
    gain = min((peak * 32767.0) / loudest, max_gain)
    if gain <= 1.01:
        return pcm
    out = bytearray(len(pcm))
    scaled = memoryview(out).cast("h")
    for index in range(len(samples)):
        value = int(samples[index] * gain)
        scaled[index] = max(-32768, min(32767, value))
    return bytes(out)


def upsample_16k(pcm: bytes) -> bytes:
    """8kHz PCM16 -> 16kHz, by putting the midpoint between each pair.

    Whisper works at 16kHz. A WAV says its own rate in its header, so a
    server reading one can resample properly and this is unnecessary -- but
    the Wyoming protocol carries the rate as a *declaration* alongside raw
    samples, and a server that ignores it and assumes 16kHz plays this audio
    at double speed, which transcribes as nothing at all.

    Doing it here rather than trusting the far side is the safer half of that
    trade: the cost is linear interpolation instead of a proper filter, and
    the benefit is that the audio is right whether or not the server would
    have resampled. There is nothing above 4kHz in this stream to protect --
    the source is telephone-bandwidth mu-law -- so the interpolation is
    inventing detail nobody is listening to either way.
    """
    if len(pcm) < 2:
        return pcm
    samples = memoryview(pcm).cast("h")
    out = bytearray(len(pcm) * 2)
    doubled = memoryview(out).cast("h")
    last = len(samples) - 1
    for index in range(len(samples)):
        value = samples[index]
        doubled[index * 2] = value
        # Midpoint with the next sample, or a repeat at the very end.
        doubled[index * 2 + 1] = (value + samples[index + 1]) // 2 if index < last else value
    return bytes(out)


def to_wav(mulaw: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """A complete 8 kHz mono 16-bit WAV file, in memory.

    Deliberately *not* resampled to the 16 kHz Whisper wants. The
    information above 4 kHz is not in this stream to recover -- the source is
    telephone-bandwidth mu-law -- so upsampling here would only substitute a
    hand-rolled interpolation for the properly-filtered one the STT server
    already does. Sending 8 kHz also halves the upload.
    """
    return pcm_to_wav(decode(mulaw), rate)


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """A complete mono 16-bit WAV around PCM that is already decoded."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buffer.getvalue()


class RtpClock:
    """When each RTP payload's audio was *sampled*, in wall-clock terms.

    The thing this replaces was counting bytes. A payload arrived, the frame
    it completed was stamped one frame after the last, and the timeline
    advanced by exactly as much audio as had been received -- so any audio
    that never arrived advanced it by nothing. A lost packet, a stalled
    socket, a scanner pausing its sender: each one slid the whole timeline
    permanently earlier, and it accumulated for as long as the RTSP session
    stayed up. Nothing downstream could see it happening. A history row and
    a segment stamped hours apart in session age were being compared on
    clocks that had quietly drifted apart by seconds, which is the shape of
    "the transcript is attached to the wrong transmission".

    The sender already tells us the answer: RTP's timestamp counts *samples*
    at the media rate, so the gap between two packets' timestamps is how much
    audio belongs between them whether or not it turned up. Anchoring that
    counter to one arrival time converts it to wall clock, and from then on
    the timeline is the sender's, not the network's.

    Two things it will not fix, both worth saying plainly. The anchor carries
    whatever latency the scanner and the network added before the first
    packet, so a constant transport lag stays constant and unmeasured here --
    `skew` is the drift *since* the anchor, not the lag itself. And the
    sender's 8kHz is only nominally 8kHz; a few parts per million against our
    own clock is real but far below anything that matters over one session.
    """

    def __init__(self, rate: int = SAMPLE_RATE, clock=_now,
                 max_skew_s: float = MAX_MEDIA_SKEW_S):
        self.rate = rate
        self._clock = clock
        self.max_skew_s = max_skew_s
        self.reset()

    def reset(self) -> None:
        """Forget the anchor. Called when the audio session restarts, after
        which the sender's counters are free to start wherever they like."""
        self._anchor_wall: float | None = None
        self._anchor_ticks = 0
        self._last_seq: int | None = None
        self._last_end_ticks: int | None = None
        self.anchored_at: float | None = None
        self.packets = 0
        self.lost = 0
        self.gaps = 0
        self.resyncs = 0
        self.skew = 0.0

    def stamp(self, payload: bytes, timestamp: int | None = None,
              seq: int | None = None, arrival: float | None = None) -> tuple[float, float]:
        """`(start, gap)` for one payload.

        `start` is when its first sample was heard, and `gap` how much audio
        is missing in front of it -- zero in the ordinary case, and the width
        of the hole after loss.

        With no `timestamp` (a caller that has no RTP header, and every test
        that predates one) this degrades to exactly what it replaced:
        arrival time, no gap detection.
        """
        arrival = self._clock() if arrival is None else arrival
        duration = len(payload) / self.rate
        self.packets += 1

        if seq is not None:
            if self._last_seq is not None:
                missed = (seq - self._last_seq - 1) & 0xFFFF
                # A large "gap" is a reordered or duplicated packet, not
                # thousands of lost ones. Counting those as loss would report
                # a catastrophe every time the network delivered out of order.
                if 0 < missed < 0x8000:
                    self.lost += missed
            self._last_seq = seq

        if timestamp is None:
            return arrival - duration, 0.0

        if self._anchor_wall is None:
            self._anchor(arrival - duration, timestamp)

        ticks = (timestamp - self._anchor_ticks) & 0xFFFFFFFF
        if ticks >= 0x80000000:  # the sender went backwards
            self._resync(arrival - duration, timestamp, "the timestamp went backwards")
            ticks = 0
        start = self._anchor_wall + ticks / self.rate

        self.skew = start - (arrival - duration)
        if abs(self.skew) > self.max_skew_s:
            self._resync(arrival - duration, timestamp,
                         f"it had drifted {self.skew:+.1f}s from arrival")
            start, ticks = arrival - duration, 0

        gap = 0.0
        if self._last_end_ticks is not None and ticks > self._last_end_ticks:
            gap = (ticks - self._last_end_ticks) / self.rate
            self.gaps += 1
        # One byte is one sample in mu-law, which is what makes a payload
        # length a tick count -- see SAMPLE_RATE.
        self._last_end_ticks = ticks + len(payload)
        return start, gap

    def _anchor(self, start: float, timestamp: int) -> None:
        self._anchor_wall = start
        self._anchor_ticks = timestamp
        self._last_end_ticks = None
        self.anchored_at = start
        self.skew = 0.0

    def _resync(self, start: float, timestamp: int, why: str) -> None:
        self.resyncs += 1
        logger.info("re-anchoring the audio clock: %s", why)
        self._anchor(start, timestamp)

    def age(self, now: float | None = None) -> float:
        """How long this session's clock has been running. The drift this
        class exists to prevent grew with exactly this number, so it belongs
        in any report about timing."""
        if self.anchored_at is None:
            return 0.0
        return (self._clock() if now is None else now) - self.anchored_at


class AudioRingBuffer:
    """The last `window_s` of one scanner's audio, keyed by wall clock.

    Timestamps come from the same clock history.py stamps calls with, so a
    history row's `started`/`ended` and a slice out of this buffer refer to
    the same moments and can be lined up without a correction term.

    `at` is how a caller with a media clock (RtpClock) keeps that promise
    when the network does not: without it, audio held up in transit is filed
    under the time it turned up rather than the time it was heard, and the
    buffer and the segmenter -- which is working from the media clock -- stop
    agreeing about which seconds a segment names.
    """

    def __init__(self, window_s: float = DEFAULT_WINDOW_S, clock=_now):
        self.window_s = window_s
        self._clock = clock
        self._chunks: deque[tuple[float, bytes]] = deque()
        self._bytes = 0

    def append(self, payload: bytes, at: float | None = None) -> None:
        """Add one RTP payload, stamped at the moment its last sample was
        heard -- its arrival time, unless a media clock says otherwise."""
        if not payload:
            return
        now = self._clock() if at is None else at
        self._chunks.append((now, payload))
        self._bytes += len(payload)
        cutoff = now - self.window_s
        while self._chunks and self._chunks[0][0] < cutoff:
            self._bytes -= len(self._chunks.popleft()[1])

    def clear(self) -> None:
        self._chunks.clear()
        self._bytes = 0

    @property
    def nbytes(self) -> int:
        return self._bytes

    @property
    def duration_s(self) -> float:
        return self._bytes / SAMPLE_RATE

    def slice(self, start: float, end: float) -> bytes:
        """Every buffered chunk overlapping [start, end], concatenated.

        Chunk-granular (20ms), not sample-accurate: an RTP payload is either
        in or out. That is well inside the precision anything downstream
        cares about, and it keeps this from having to model partial chunks.
        """
        if end <= start:
            return b""
        out = bytearray()
        for stamped_at, data in self._chunks:
            chunk_started = stamped_at - len(data) / SAMPLE_RATE
            if stamped_at <= start or chunk_started >= end:
                continue
            out.extend(data)
        return bytes(out)


class VoiceSegmenter:
    """Squelch-edge detection over the mu-law stream.

    Reports boundaries via `on_segment(start, end)`; holds no audio, so the
    caller decides where the bytes come from (in practice an AudioRingBuffer
    covering the same window). Keeping the two apart is what lets the
    pre-roll work: the segmenter can name a start time earlier than the
    moment it made the decision, because it is not the thing holding the
    audio.
    """

    def __init__(
        self,
        on_segment,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        open_frames: int = DEFAULT_OPEN_FRAMES,
        close_frames: int = DEFAULT_CLOSE_FRAMES,
        preroll_s: float = DEFAULT_PREROLL_S,
        max_segment_s: float = DEFAULT_MAX_SEGMENT_S,
        clock=_now,
    ):
        self.on_segment = on_segment
        self.threshold = threshold
        self.open_frames = open_frames
        self.close_frames = close_frames
        self.preroll_s = preroll_s
        self.max_segment_s = max_segment_s
        self._clock = clock

        # Why the last segment ended, readable by the listener it was handed
        # to -- see CLOSE_SILENCE. Kept as state rather than passed to the
        # callback so the callback contract stays (start, end), which every
        # caller and every test already uses.
        self.last_close: str | None = None
        self.closes: dict[str, int] = {}
        self._last_close_at: float | None = None
        self._gap_frames = 0

        # The quietest frame in each recent block, and the block being
        # filled. See FLOOR_BLOCK_FRAMES.
        self._blocks: deque[float] = deque(maxlen=FLOOR_BLOCKS)
        self._block_min: float | None = None
        self._block_frames = 0

        self._pending = bytearray()
        # Wall-clock time of the first byte still in `_pending`. Frame times
        # are derived from this by offset rather than by calling the clock
        # per frame, so a segment's reported length matches the number of
        # frames it actually contains instead of drifting with scheduling.
        self._pending_start: float | None = None

        self._open_run = 0
        self._close_run = 0
        self._segment_start: float | None = None
        # When the current run of silence began -- the segment's end, once
        # the run is long enough to count. Without this the trailing
        # close_frames of silence would be included in every segment.
        self._silence_start: float | None = None

    @property
    def in_segment(self) -> bool:
        return self._segment_start is not None

    def reset(self) -> None:
        """Drop all state. Called when the audio session restarts: the next
        payload after a reconnect is not contiguous with the last one before
        it, and carrying a half-open segment across the gap would produce one
        segment spanning the outage.
        """
        if self._segment_start is not None:
            logger.debug("segmenter reset while a segment was open -- discarding it")
            self.closes[CLOSE_RESET] = self.closes.get(CLOSE_RESET, 0) + 1
        self._pending.clear()
        self._pending_start = None
        # The floor is deliberately kept across a reset: it describes the
        # receiver, not the session, and relearning it from zero after every
        # reconnect would reopen the cap problem for as long as that took.
        self._open_run = 0
        self._close_run = 0
        self._segment_start = None
        self._silence_start = None

    def feed(self, payload: bytes, at: float | None = None) -> None:
        """Add one payload, optionally saying when its first sample was heard.

        `at` comes from RtpClock, and passing it is what stops this timeline
        from being a byte count wearing a clock's clothes. Without it the
        only thing advancing the segment times is how much audio arrived, so
        audio that never arrived silently shortens every timestamp that
        follows -- for the whole session, since nothing here ever looked at
        wall clock again after the first payload.

        A hole in the audio is also a break in the *transmission*, once it is
        wide enough to hear: a segment spanning one would claim continuous
        speech across seconds nobody has, and the clip pulled for it would
        be a splice. Past MAX_BRIDGED_GAP_S the open segment is closed at the
        last audio actually received, and the timeline re-anchored.
        """
        if not payload:
            return
        start = (self._clock() - len(payload) / SAMPLE_RATE) if at is None else at
        if self._pending_start is None:
            self._pending_start = start
        else:
            # Where this payload should have begun if nothing were missing.
            expected = self._pending_start + len(self._pending) / SAMPLE_RATE
            slip = start - expected
            if abs(slip) > MAX_BRIDGED_GAP_S:
                logger.debug("audio gap of %.2fs -- closing the timeline across it", slip)
                self._gap_frames += max(0, int(slip / FRAME_S))
                if self._segment_start is not None:
                    self._emit(expected, CLOSE_GAP)
                self._pending.clear()
                self._pending_start = start
            elif slip:
                # Too small to break a transmission over, but left alone it
                # is exactly the error that accumulates. Move the anchor
                # instead of letting the byte count define "now".
                self._pending_start += slip
        self._pending.extend(payload)

        while len(self._pending) >= FRAME_SAMPLES:
            frame = bytes(self._pending[:FRAME_SAMPLES])
            del self._pending[:FRAME_SAMPLES]
            frame_start = self._pending_start
            self._pending_start += FRAME_S
            self._consume_frame(frame, frame_start)

    @property
    def noise_floor(self) -> float:
        """What this receiver sounds like when nobody is talking."""
        seen = list(self._blocks)
        if self._block_min is not None:
            seen.append(self._block_min)
        return min(min(seen), MAX_FLOOR) if seen else 0.0

    def _has_voice(self, frame: bytes) -> bool:
        """Whether this frame counts as audio rather than dead air.

        Exact silence is still the fast path, and still the whole answer on a
        receiver that produces it: the floor stays at zero, so this reduces
        to "anything that is not the silence code". Once there is a floor, a
        frame has to clear NOISE_MARGIN times it -- which is what stops a
        segment running to the length cap on a receiver that never quite goes
        quiet.

        An explicitly configured `threshold` overrides both, for a site that
        would rather state its own number than have one inferred.
        """
        if self.threshold > 0:
            return frame_rms(frame) > self.threshold
        if is_silent(frame):
            self._learn_floor(0.0)
            return False
        rms = frame_rms(frame)
        # Judged against the floor as it stood *before* this frame, then the
        # frame is folded in. The other order lets a single loud frame set
        # the floor to its own level and then fail its own comparison.
        voice = rms > self.noise_floor * NOISE_MARGIN
        self._learn_floor(rms)
        return voice

    def _learn_floor(self, rms: float) -> None:
        self._block_min = rms if self._block_min is None else min(self._block_min, rms)
        self._block_frames += 1
        if self._block_frames >= FLOOR_BLOCK_FRAMES:
            self._blocks.append(self._block_min)
            self._block_min = None
            self._block_frames = 0

    def _consume_frame(self, frame: bytes, frame_start: float) -> None:
        loud = self._has_voice(frame)
        frame_end = frame_start + FRAME_S

        if self._segment_start is None:
            if not loud:
                self._open_run = 0
                return
            self._open_run += 1
            if self._open_run < self.open_frames:
                return
            # Name the start at the first loud frame of the run, less the
            # pre-roll -- not here, several frames later, which would clip
            # exactly the onset the pre-roll exists to preserve.
            run_started = frame_end - self._open_run * FRAME_S
            self._segment_start = run_started - self.preroll_s
            self._open_run = 0
            self._close_run = 0
            self._silence_start = None
            return

        if loud:
            self._close_run = 0
            self._silence_start = None
        else:
            if self._silence_start is None:
                self._silence_start = frame_start
            self._close_run += 1
            if self._close_run >= self.close_frames:
                self._emit(self._silence_start, CLOSE_SILENCE)
                return

        if frame_end - self._segment_start >= self.max_segment_s:
            # Worth a warning rather than a debug line: a transmission this
            # long is unusual, and a *run* of them means the detector is not
            # closing at all -- which produces expensive clips of mostly
            # nothing and is the failure this noise floor exists to prevent.
            logger.warning(
                "segment hit the %.0fs cap -- closing it. If this repeats, the "
                "squelch is not going quiet between transmissions (noise floor "
                "estimate %.0f).",
                self.max_segment_s, self.noise_floor,
            )
            self._emit(frame_end, CLOSE_CAP)

    def _report(self, start: float, end: float, reason: str,
                silence: float, gap_frames: int) -> None:
        """Say what just happened, in the terms the question is asked in.

        The question is always "why was that one transmission cut in two",
        and answering it used to mean guessing between three mechanisms that
        look identical from the history page. The noise floor is in here for
        the same reason: it is inferred rather than configured, and until now
        the only way to see it was to trip the length cap.
        """
        logger.debug(
            "segment %.1fs closed (%s): floor %.0f, %.2fs of quiet, %d frame(s) missing",
            end - start, reason, self.noise_floor, silence, gap_frames,
        )
        since = None if self._last_close_at is None else start - self._last_close_at
        self._last_close_at = end
        if since is not None and 0 <= since < SPLIT_SUSPECT_S:
            logger.info(
                "a segment opened %.2fs after the last one closed (%s) -- if those "
                "were one transmission, an end-of-transmission gap above %.1fs "
                "would have kept them together (noise floor %.0f)",
                since, reason, since + self.close_frames * FRAME_S, self.noise_floor,
            )
        total = sum(self.closes.values())
        if total and total % CLOSE_REPORT_EVERY == 0:
            logger.info(
                "%d segments so far, ending: %s. Noise floor %.0f, closing after "
                "%.1fs of quiet.",
                total,
                ", ".join(f"{count} on {why}" for why, count in sorted(self.closes.items())),
                self.noise_floor, self.close_frames * FRAME_S,
            )

    def _emit(self, end: float, reason: str) -> None:
        start, self._segment_start = self._segment_start, None
        # The quiet actually run, not `end - self._silence_start`: a silence
        # close ends the segment *at* the moment the quiet began, so that
        # subtraction is zero by construction and reported nothing at all.
        silence = self._close_run * FRAME_S
        gap_frames, self._gap_frames = self._gap_frames, 0
        self._close_run = 0
        self._open_run = 0
        self._silence_start = None
        if start is None or end <= start:
            return
        self.last_close = reason
        self.closes[reason] = self.closes.get(reason, 0) + 1
        self._report(start, end, reason, silence, gap_frames)
        try:
            self.on_segment(start, end)
        except Exception:
            # A failing consumer must not take the audio path down with it:
            # this runs on the RTP receive path, and an exception here would
            # propagate into the datagram handler.
            logger.exception("a voice-segment listener failed")
