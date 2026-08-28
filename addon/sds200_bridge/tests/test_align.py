"""Tests for align.py -- finding how far apart the two clocks are running.

Everything that joins a piece of audio to a history row rests on an answer to
that question, and until this module the answer was a guess. What makes the
guess dangerous rather than merely wrong is that being off by a couple of
seconds does not fail loudly: it writes one transmission's words onto the
transmission beside it, which reads as a working feature with bad luck.

So the properties pinned here are mostly about *refusing to answer*:

* A channel that is almost never quiet has no pattern in it. Every offset
  agrees about equally well, and the module has to say so rather than picking
  the largest of a set of equal numbers.
* Two transmissions are not a pattern either. One thing lines up with one
  thing at whatever offset you like.
* An answer that keeps changing is not a measurement. Nothing is applied until
  several passes agree and agree closely.

And one about answering: the pattern is matched through missing events on both
sides, because that is the normal state of these two streams -- roughly a
quarter of logged calls have no audio behind them, and transmissions shorter
than a poll interval never get logged at all.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import align  # noqa: E402
from align import Correlator  # noqa: E402


class FakeClock:
    """Time under test control -- a real clock would make every window
    assertion here a flaky one."""

    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds=1.0):
        self.now += seconds
        return self.now


# A few minutes of traffic with the voids that make it a pattern: some short
# transmissions, some long, gaps from five seconds to a couple of minutes.
# Offsets are seconds before "now", so the whole thing sits inside the window.
PATTERN = [
    (-540.0, -537.0),
    (-530.0, -522.0),
    (-465.0, -461.0),
    (-400.0, -385.0),
    (-260.0, -256.5),
    (-230.0, -222.0),
    (-100.0, -94.0),
]


class CorrelatorTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.align = Correlator("home", clock=self.clock)

    def audio(self, spans=PATTERN):
        for start, end in spans:
            self.align.add_audio(self.clock.now + start, self.clock.now + end)

    def calls(self, spans=PATTERN, lag=0.0, poll=3.0, skip=()):
        """The same traffic as the log saw it.

        A poll lands wherever it lands, so each transmission is noticed
        somewhere inside the interval after it began and let go somewhere
        inside the interval before it ended. Walking the phase through thirds
        keeps that realistic without making the test depend on a random seed
        -- and a builder that always used the worst case would be measuring
        the builder rather than the module.
        """
        for index, (start, end) in enumerate(spans):
            if index in skip:
                continue
            late = (index % 3) * poll / 3.0
            early = ((index + 1) % 3) * poll / 3.0
            noticed = self.clock.now + start + lag + late
            gone = max(noticed + 0.1, self.clock.now + end + lag - early)
            self.align.add_call(noticed, gone)


class TestFindingTheOffset(CorrelatorTestCase):
    def test_a_pattern_of_transmissions_and_voids_finds_the_offset(self):
        self.audio()
        self.calls(lag=1.5)
        self.assertAlmostEqual(self.align.correlate(force=True), 1.5, delta=0.3)

    def test_it_works_the_other_way_round_too(self):
        # Audio arriving *later* than the log noticed the call, which is what
        # a buffering audio path looks like.
        self.audio()
        self.calls(lag=-2.0)
        self.assertAlmostEqual(self.align.correlate(force=True), -2.0, delta=0.3)

    def test_no_disagreement_reads_as_none_rather_than_as_nothing(self):
        self.audio()
        self.calls(lag=0.0)
        self.assertAlmostEqual(self.align.correlate(force=True), 0.0, delta=0.3)

    def test_calls_with_no_audio_behind_them_do_not_move_the_answer(self):
        # The normal state of these two streams: the squelch opens on data
        # bursts and noise blips, so a quarter of rows have nothing to hear.
        self.audio(spans=[s for i, s in enumerate(PATTERN) if i not in (1, 4)])
        self.calls(lag=1.5)
        self.assertAlmostEqual(self.align.correlate(force=True), 1.5, delta=0.3)

    def test_transmissions_with_no_row_do_not_move_it_either(self):
        # The mirror case: shorter than a poll interval, so never logged.
        self.audio()
        self.calls(lag=1.5, skip=(0, 4))
        self.assertAlmostEqual(self.align.correlate(force=True), 1.5, delta=0.3)


class TestRefusingToAnswer(CorrelatorTestCase):
    def test_a_channel_that_is_never_quiet_has_no_pattern_in_it(self):
        # Back-to-back traffic agrees about equally well at every offset. The
        # largest of a set of equal numbers is not an answer.
        busy = [(-500.0 + i * 6.0, -500.0 + i * 6.0 + 5.6) for i in range(60)]
        self.audio(spans=busy)
        self.calls(spans=busy, lag=1.5, poll=0.5)
        self.assertIsNone(self.align.correlate(force=True))
        self.assertLess(self.align.prominence, align.MIN_PROMINENCE)

    def test_two_transmissions_are_not_a_pattern(self):
        self.audio(spans=PATTERN[:2])
        self.calls(spans=PATTERN[:2], lag=1.5)
        self.assertIsNone(self.align.correlate(force=True))

    def test_audio_that_shares_nothing_with_the_log_is_not_an_answer(self):
        # What a wedged audio path looks like from here: recordings and
        # transmissions that never once coincide, at any offset in range.
        self.audio()
        self.calls(spans=[(-500.0, -497.0), (-440.0, -437.0), (-350.0, -345.0),
                          (-320.0, -317.0), (-180.0, -175.0), (-150.0, -147.0)])
        self.assertIsNone(self.align.correlate(force=True))
        self.assertLess(self.align.agreement, align.MIN_AGREEMENT)

    def test_an_offset_beyond_the_search_is_not_forced_into_it(self):
        self.audio()
        self.calls(lag=align.MAX_OFFSET_S * 3)
        self.assertIsNone(self.align.correlate(force=True))


class TestUsingTheAnswer(CorrelatorTestCase):
    def _agree(self, times=3, lag=1.5):
        for _ in range(times):
            self.audio()
            self.calls(lag=lag)
            self.align.correlate(force=True)

    def test_one_answer_is_not_enough_to_start_moving_things(self):
        self.audio()
        self.calls(lag=1.5)
        self.align.correlate(force=True)
        self.assertIsNone(self.align.offset)
        self.assertEqual(self.align.shift(500.0, 502.0), (500.0, 502.0))

    def test_several_passes_that_agree_are(self):
        self._agree()
        self.assertIsNotNone(self.align.offset)
        start, end = self.align.shift(500.0, 502.0)
        self.assertAlmostEqual(start - 500.0, self.align.offset, places=6)
        self.assertAlmostEqual(end - 502.0, self.align.offset, places=6)

    def test_an_answer_that_keeps_changing_is_reported_but_never_used(self):
        # Two clocks that are not tracking each other produce a median, which
        # is a number rather than a measurement.
        self.align._offsets.extend([-4.0, 0.5, 3.9])
        self.assertIsNone(self.align.offset)
        self.assertEqual(self.align.shift(500.0, 502.0), (500.0, 502.0))

    def test_the_shift_moves_a_window_into_the_logs_own_time(self):
        self.align._offsets.extend([2.0, 2.0, 2.0])
        self.assertEqual(self.align.shift(500.0, 502.0), (502.0, 504.0))


class TestKeepingTheTimelines(CorrelatorTestCase):
    def test_forgetting_the_audio_keeps_what_was_learned(self):
        # A restarted audio session invalidates the spans, not the offset:
        # that describes this installation's audio path, not one session of
        # it -- the same reasoning that keeps the noise floor across a reset.
        self.align._offsets.extend([1.5, 1.5, 1.5])
        self.audio()
        self.align.forget_audio("the audio session restarted")
        self.assertEqual(len(self.align._audio), 0)
        self.assertAlmostEqual(self.align.offset, 1.5)

    def test_events_older_than_the_window_fall_out(self):
        self.audio()
        self.clock.tick(align.WINDOW_S * 2)
        self.audio(spans=[(-5.0, -3.0)])
        self.assertEqual(len(self.align._audio), 1)

    def test_a_call_still_in_progress_is_not_a_transmission_yet(self):
        self.align.add_call(self.clock.now - 5.0, None)
        self.assertEqual(len(self.align._calls), 0)

    def test_a_call_seen_at_one_poll_still_says_the_channel_was_busy(self):
        # Its duration rounds to zero, but it is evidence of *when*, which is
        # what the pattern is made of.
        self.align.add_call(self.clock.now - 5.0, self.clock.now - 5.0)
        self.assertEqual(len(self.align._calls), 1)

    def test_correlating_is_debounced_rather_than_run_on_every_call(self):
        self.audio()
        self.calls(lag=1.5)  # each add_call asks for a pass
        self.assertEqual(self.align.attempts, 1)
        self.clock.tick(align.CORRELATE_INTERVAL_S + 1)
        self.align.add_call(self.clock.now - 2.0, self.clock.now - 1.0)
        self.assertEqual(self.align.attempts, 2)


if __name__ == "__main__":
    unittest.main()
