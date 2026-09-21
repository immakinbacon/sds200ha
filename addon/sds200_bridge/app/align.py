"""Lining the audio up with the log, by the shape of the quiet between.

Two clocks describe the same transmissions and neither is trustworthy on its
own. A history row's `started` is the first GSI poll that *noticed* the call,
up to `GSI_POLL_INTERVAL` after it began, and its `ended` is the last poll that
still saw it -- so a row is a shrunken view of its own transmission, late at
the front and early at the back. A segment's times come from the audio, which
is as late as the scanner's buffering and the network made it. Everything that
joins the two -- `history.calls_at`, `COVER_SLACK_S`, the old edge probe in
`transcribe.py` -- rested on a guess about the difference between them.

**The gaps carry the signal, not the durations.** A three-second transmission
is measured by a three-second poll loop, which quantizes it to nothing: a call
seen at one poll is recorded with a duration of zero, and one seen at two polls
records three seconds whether it ran for three or for five. But a two-minute
silence is two minutes on both sides, to the second. So a stretch of traffic --
a few seconds of speech, a long void, ten seconds of speech, a shorter void --
has a shape, that shape is close to unique over a few minutes, and it is the
same shape in both streams however coarsely each transmission was measured.

Which is why this correlates the *pattern* rather than matching transmissions
one at a time. Pairing events needs to be right about which event is which,
and here that is a coin toss: roughly a quarter of logged calls have no audio
behind them at all (the squelch opens on a data burst), while transmissions
shorter than a poll interval never get a row. Both streams are missing pieces
the other has. A pattern match does not care -- a missing event lowers the
agreement a little and moves the answer not at all.

How
---

Each timeline becomes a bitmap: one bit per `QUANTUM_S`, set while a
transmission or a recording was in progress. The bitmaps are Python integers,
so testing a candidate offset is one `&` and one `bit_count()` over a few
hundred bytes, and the whole search is a few hundred of those. No dependency,
which matters on an image whose `requirements.txt` is one line.

The answer is the offset with the most agreement. Its *prominence* over the
others is the confidence, and that test is doing real work: a channel busy
enough to be almost always-on agrees about equally well at every offset, and
that has to read as "no answer" rather than as an answer.

What the quantization does to the answer, since it would be easy to assume it
biases it: the log's on-intervals sit *inside* the true transmissions, missing
up to a poll at the front and a poll at the back. That shrinks the peak. It
does not move it, because the two errors pull in opposite directions and are
the same size on average -- unlike a start-to-start estimate, which is late by
half a poll interval and needs correcting for it.

What it is not
--------------

It is not a call matcher. It answers one question -- how far apart the two
clocks are running -- and the answer is handed to the code that already knows
how to find the row a piece of audio belongs to. Widening or narrowing that
search stays `history.calls_at`'s business.
"""

from __future__ import annotations

import logging
import time
from collections import deque

from history import CONTIGUOUS_GAP_S
from protocol import GSI_POLL_INTERVAL

logger = logging.getLogger(__name__)

# Bitmap resolution. A tenth of a second is far finer than the poll loop can
# see and far coarser than the audio needs, which is the right way round: the
# answer cannot be better than the coarser of the two inputs, and paying for
# resolution neither side has would only cost memory.
QUANTUM_S = 0.1

# How much history to correlate over. Long enough to hold several
# transmissions and the voids between them on a quiet channel -- the voids are
# the signal, so a window that fits only the busy part throws it away. Ten
# minutes is 6000 bits, under a kilobyte per timeline.
WINDOW_S = 600.0

# How far apart the two clocks are allowed to be. Larger than any offset that
# should exist: the audio path is buffering plus a network, the log is at most
# a couple of polls, and if the truth is outside this the answer needs finding
# rather than applying.
MAX_OFFSET_S = 10.0

# Bounds on the events kept, so a busy channel cannot grow the timelines
# without limit even inside the window.
MAX_EVENTS = 400

# Below this many events on either side there is no pattern to match -- one
# transmission agrees with one transmission at whatever offset lines them up.
MIN_EVENTS = 5

# The peak has to beat the middle of the field by this much to count as an
# answer rather than as noise. Measured against the median of every offset
# tried, not against the next best: agreement falls away over roughly the
# length of a transmission, so the offsets on either side of the answer score
# almost as well as the answer does, and comparing against those would call
# every clean result ambiguous. Against the median, a structured few minutes
# of traffic scores about 2.3 and an almost-always-on channel about 1.06 --
# which is the case this exists to refuse.
MIN_PROMINENCE = 1.5

# And it has to explain this much of the smaller timeline. A peak can be
# prominent and still tiny if the two streams have almost nothing in common,
# which is what a wedged audio path looks like from here.
MIN_AGREEMENT = 0.35

# Offsets scoring this close to the peak are treated as part of it. See the
# plateau note in `correlate`.
PLATEAU = 0.99

# A peak this close to the end of the search is not an answer, it is the
# search running out of room -- the true offset is somewhere further out, and
# clipping it to the boundary would report the edge of the range as a
# measurement. Worth saying in the log, because it means MAX_OFFSET_S is
# wrong for this installation rather than that nothing was found.
EDGE_GUARD_S = 1.0

# Recordings closer together than this are one transmission that the detector
# cut in half -- which is the very thing being investigated, so the timeline
# must not inherit it. An asymmetric split moves the pair's combined centre,
# and people pause after the callsign rather than at random, so left alone it
# is a systematic pull rather than noise. Rows get the same treatment under
# the log's own contiguity rule, so both timelines end up describing
# transmissions rather than records of them.
MERGE_GAP_S = 2.0

# Estimates kept, and how many have to agree before the offset is used for
# anything. Short on purpose: the offset describes the current audio session
# and poll interval, and a long memory would take an hour to notice either
# changing.
OFFSET_SAMPLES = 12
MIN_SAMPLES_TO_APPLY = 3

# The spread those estimates may have and still be treated as a measurement.
# Wider than this and the two clocks are not tracking each other at all, in
# which case the median is a number rather than an answer.
MAX_SPREAD_S = 1.0

# Correlating is bookkeeping, not something a call should wait on -- the same
# reasoning as history.PRUNE_INTERVAL_S, and the same debounce, so it stays
# testable on an injected clock with no event loop anywhere near it.
CORRELATE_INTERVAL_S = 30.0


def _now() -> float:
    return time.time()


class Correlator:
    """One scanner's two timelines, and the offset between them.

    Kept as a plain state machine with an injected clock, like
    `history.CallTracker`, so it can be tested by feeding it spans directly.
    The clock is wall clock rather than monotonic because both inputs are:
    a history row's `started` and an audio segment's start have to mean the
    same thing here as they do everywhere else.
    """

    def __init__(self, scanner_id: str, *, poll_interval: float = GSI_POLL_INTERVAL,
                 clock=_now, window_s: float = WINDOW_S):
        self.scanner_id = scanner_id
        self.poll_interval = poll_interval
        self.window_s = window_s
        self._clock = clock
        self._audio: deque[tuple[float, float]] = deque(maxlen=MAX_EVENTS)
        self._calls: deque[tuple[float, float]] = deque(maxlen=MAX_EVENTS)
        self._offsets: deque[float] = deque(maxlen=OFFSET_SAMPLES)
        self._last_correlated = 0.0
        self.agreement = 0.0
        self.prominence = 0.0
        self.attempts = 0
        self.answers = 0

    # -- the two timelines ----------------------------------------------

    def add_audio(self, start: float, end: float) -> None:
        """One recording: when the audio for a transmission began and ended."""
        self._append(self._audio, start, end)

    def add_call(self, started: float, ended: float | None) -> None:
        """One transmission, as the poll loop saw it.

        A call still open has no end yet and is not offered here; a call whose
        end equals its start is a real thing -- a transmission seen at exactly
        one poll -- and is kept, because it still says the channel was busy at
        that moment, which is what the pattern is made of.
        """
        if ended is None:
            return
        self._append(self._calls, started, max(ended, started + QUANTUM_S))
        self.correlate()

    def forget_audio(self, why: str) -> None:
        """Drop the audio timeline, e.g. because the RTSP session restarted.

        The spans before a restart were placed by a clock that has since been
        re-anchored, so correlating across the join would blur the peak with
        two different offsets at once. The learned offset itself is kept: it
        describes this installation's audio path, not one session of it --
        the same reasoning that keeps the noise floor across a segmenter reset.
        """
        if self._audio:
            logger.debug("%s: forgetting %d audio span(s) -- %s",
                         self.scanner_id, len(self._audio), why)
        self._audio.clear()

    def _append(self, spans: deque, start: float, end: float) -> None:
        if end <= start:
            return
        spans.append((start, end))
        cutoff = self._clock() - self.window_s
        while spans and spans[0][1] < cutoff:
            spans.popleft()

    # -- the answer -----------------------------------------------------

    @property
    def offset(self) -> float | None:
        """How far behind the audio the log is, in seconds, or None.

        Positive is the ordinary case: the audio was heard first and the poll
        loop caught up. Add it to an audio timestamp to get the log's idea of
        the same moment.
        """
        if len(self._offsets) < MIN_SAMPLES_TO_APPLY:
            return None
        ordered = sorted(self._offsets)
        if ordered[-1] - ordered[0] > MAX_SPREAD_S:
            return None
        return ordered[len(ordered) // 2]

    def shift(self, start: float, end: float) -> tuple[float, float]:
        """An audio window, moved into the log's time. Unchanged until the
        offset is an answer rather than a guess."""
        offset = self.offset
        if offset is None:
            return start, end
        return start + offset, end + offset

    def correlate(self, *, force: bool = False) -> float | None:
        """Line the two timelines up. Returns this pass's estimate, or None.

        Debounced rather than scheduled, for the reason `history.prune` is:
        it runs off an event that already happens, so there is no second clock
        to keep in step with a scanner coming or going.
        """
        now = self._clock()
        if not force and now - self._last_correlated < CORRELATE_INTERVAL_S:
            return None
        if len(self._audio) < MIN_EVENTS or len(self._calls) < MIN_EVENTS:
            # Not an attempt, and deliberately not counted as one: burning the
            # interval on a pass that could not run would mean the first real
            # chance to answer waits another interval for no reason.
            return None

        self._last_correlated = now
        self.attempts += 1
        epoch = now - self.window_s
        audio = _bitmap(_merge(self._audio, MERGE_GAP_S), epoch)
        calls = _bitmap(_merge(self._calls, CONTIGUOUS_GAP_S), epoch)
        audio_bits, call_bits = audio.bit_count(), calls.bit_count()
        if not audio_bits or not call_bits:
            return None

        reach = int(MAX_OFFSET_S / QUANTUM_S)
        scores = [_agreement(calls, audio, lag) for lag in range(-reach, reach + 1)]
        peak = max(scores)
        middle = sorted(scores)[len(scores) // 2]

        self.agreement = peak / min(audio_bits, call_bits)
        self.prominence = peak / middle if middle else float("inf")
        if self.agreement < MIN_AGREEMENT or self.prominence < MIN_PROMINENCE:
            logger.debug(
                "%s: no clock offset found -- %.0f%% agreement, %.2fx prominence "
                "(%d recording(s), %d transmission(s) in the window)",
                self.scanner_id, self.agreement * 100, self.prominence,
                len(self._audio), len(self._calls),
            )
            return None

        # The middle of the plateau, not the first offset to reach it. A
        # row's on-interval sits *inside* its transmission, so once it is
        # contained there is a range of offsets that all contain it equally
        # -- and taking the first of those puts the answer at the edge of the
        # range rather than in the middle of it, which is a bias the width of
        # the poll interval.
        top = [i for i, score in enumerate(scores) if score >= peak * PLATEAU]
        estimate = ((top[0] + top[-1]) / 2 - reach) * QUANTUM_S
        if abs(estimate) > MAX_OFFSET_S - EDGE_GUARD_S:
            logger.info(
                "%s: the clocks look more than %.0fs apart, which is further "
                "than this looks -- not using it",
                self.scanner_id, MAX_OFFSET_S - EDGE_GUARD_S,
            )
            return None
        self._offsets.append(estimate)
        self.answers += 1
        logger.info(
            "%s: the log runs %+.1fs behind the audio (%.0f%% agreement, %.1fx "
            "prominence, from %d recording(s) and %d transmission(s); using "
            "%+.1fs)",
            self.scanner_id, estimate, self.agreement * 100, self.prominence,
            len(self._audio), len(self._calls),
            self.offset if self.offset is not None else 0.0,
        )
        return estimate

    def report(self) -> dict:
        """Everything worth putting in a log line about this scanner."""
        return {
            "offset": self.offset,
            "estimates": len(self._offsets),
            "agreement": self.agreement,
            "prominence": self.prominence,
            "recordings": len(self._audio),
            "transmissions": len(self._calls),
            "attempts": self.attempts,
            "answers": self.answers,
        }


def _merge(spans, gap: float) -> list[tuple[float, float]]:
    """Spans closer than `gap`, joined. See MERGE_GAP_S."""
    joined: list[list[float]] = []
    for start, end in sorted(spans):
        if joined and start - joined[-1][1] <= gap:
            joined[-1][1] = max(joined[-1][1], end)
        else:
            joined.append([start, end])
    return [(start, end) for start, end in joined]


def _bitmap(spans, epoch: float) -> int:
    """The timeline as one integer: a set bit per QUANTUM_S that was busy."""
    bits = 0
    for start, end in spans:
        low = int((start - epoch) / QUANTUM_S)
        high = int((end - epoch) / QUANTUM_S) + 1
        if high <= 0:
            continue
        low = max(low, 0)
        if high > low:
            bits |= ((1 << (high - low)) - 1) << low
    return bits


def _agreement(calls: int, audio: int, lag: int) -> int:
    """How much of the two timelines coincides with the audio moved `lag`
    quanta later. Shifting up is later, because bit n is the nth quantum."""
    moved = audio << lag if lag >= 0 else audio >> -lag
    return (calls & moved).bit_count()
