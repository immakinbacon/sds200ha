"""Tests for audio_tap.py -- decoding the RTP stream and finding the edges
of a transmission in it.

Four things here are worth pinning down because getting them wrong is
silent rather than loud:

* **What counts as dead air.** Segmentation is by exact silence, not by an
  energy threshold, because that is what the hardware measured out to --
  a squelched SDS200 emits the two mu-law encodings of zero and nothing
  else. TestSilenceDetection and TestSegmentingOnSilence cover the default
  path; the threshold path is still supported for a receiver with a real
  noise floor, and TestVoiceSegmenter still exercises it. The test that
  matters most is the one showing a frame at RMS 8 -- real audio at the
  edge of a transmission -- opening a segment under the silence test and
  being discarded by any usable threshold.

* **The mu-law table.** A wrong expansion still produces audio-shaped bytes,
  so nothing crashes and nothing logs; it just transcribes badly, which is
  indistinguishable from the model being bad at scanner audio -- the exact
  question this feature exists to answer. The four boundary codes are
  checked against the values the G.711 reference expansion gives.

* **Where a segment starts.** The pre-roll exists because the opening
  hysteresis needs several loud frames before it is convinced, by which
  point the start of the word is already past. If the reported start is the
  moment the decision was made rather than the moment the run began, every
  transmission loses its first syllable -- "Engine 12" becomes "ngine 12" --
  and no test that only checks "a segment was emitted" would notice.

* **Where a segment ends.** The closing hysteresis deliberately waits half a
  second of silence, but that silence must not be *in* the segment: trailing
  silence is what Whisper hallucinates over.

* **What "now" means.** TestRtpClock is the newest of these and the one
  with the least visible failure mode. The timeline used to advance by
  bytes received, so audio that never arrived advanced it by nothing and
  every later timestamp slid earlier -- permanently, cumulatively, for the
  life of the RTSP session, with no log line anywhere. Working from the
  sender's own sample clock instead is what keeps a segment's times and a
  history row's times comparable an hour into a session.

TestAudioRingBuffer covers the storage side -- that eviction is by wall
clock rather than chunk count, and that a slice picks up every chunk
overlapping the window and no others.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import io
import sys
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import audio_tap  # noqa: E402
from audio_tap import AudioRingBuffer, RtpClock, VoiceSegmenter  # noqa: E402

# mu-law codes for the two extremes and for silence. 0xFF and 0x7F are
# positive and negative zero -- both decode to 0, which is what a squelched
# scanner sends.
SILENCE_BYTE = b"\xff"
LOUD_FRAME = b"\x00\x80" * (audio_tap.FRAME_SAMPLES // 2)
SILENT_FRAME = SILENCE_BYTE * audio_tap.FRAME_SAMPLES
# The negative encoding of zero. A closed squelch emits both.
NEG_SILENT_FRAME = b"\x7f" * audio_tap.FRAME_SAMPLES
# RMS 8 -- the kind of frame the real capture found 86 of in one minute at
# the quiet edges of a transmission, and which any usable threshold discards.
QUIET_FRAME = b"\xfe\x7e" * (audio_tap.FRAME_SAMPLES // 2)


class FakeClock:
    """Time under test control -- a real clock would make every boundary
    assertion here a flaky one."""

    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds
        return self.now


class TestRtpClock(unittest.TestCase):
    """Placing a payload in time from the header the sender put on it."""

    def setUp(self):
        self.clock = FakeClock()
        self.rtp = RtpClock(clock=self.clock)

    def send(self, samples=160, timestamp=None, seq=None, delay=None):
        """One packet's worth, arriving `delay` late if you like."""
        if delay:
            self.clock.tick(delay)
        payload = b"\xff" * samples
        return self.rtp.stamp(payload, timestamp, seq)

    def test_the_first_packet_anchors_on_its_own_arrival(self):
        start, gap = self.send(timestamp=4000)
        self.assertAlmostEqual(start, self.clock.now - 0.02)
        self.assertEqual(gap, 0.0)

    def test_later_packets_are_placed_by_the_senders_clock(self):
        self.send(timestamp=4000)
        anchored = self.clock.now - 0.02
        # Three packets on, and the wall clock is not consulted for where
        # they go -- the sample count is.
        start, _gap = self.send(timestamp=4000 + 3 * 160, delay=1.0)
        self.assertAlmostEqual(start, anchored + 3 * 0.02)

    def test_audio_that_never_arrived_still_takes_up_time(self):
        # The whole point. Two lost packets used to cost 40ms off every
        # timestamp that followed, for the rest of the session.
        self.send(timestamp=4000)
        anchored = self.clock.now - 0.02
        start, gap = self.send(timestamp=4000 + 3 * 160, delay=0.06)
        self.assertAlmostEqual(start, anchored + 0.06)
        self.assertAlmostEqual(gap, 0.04)
        self.assertEqual(self.rtp.gaps, 1)

    def test_a_run_of_packets_does_not_drift_against_the_wall_clock(self):
        # A hundred packets delivered slightly slow. Byte-counting would put
        # the last one 2s early; the sender's clock puts it where it belongs.
        for index in range(100):
            start, _gap = self.send(timestamp=4000 + index * 160, delay=0.02)
        self.assertAlmostEqual(start, self.clock.now - 0.02, places=6)

    def test_loss_is_counted_from_the_sequence_numbers(self):
        self.send(timestamp=4000, seq=10)
        self.send(timestamp=4000 + 320, seq=12, delay=0.04)
        self.assertEqual(self.rtp.lost, 1)

    def test_reordering_is_not_reported_as_catastrophic_loss(self):
        # A late packet reads as a sequence number 65k in the past. Counting
        # that as loss would report a disaster every time UDP was UDP.
        self.send(timestamp=4000, seq=10)
        self.send(timestamp=4000 + 160, seq=9)
        self.assertEqual(self.rtp.lost, 0)

    def test_a_clock_that_has_drifted_too_far_is_re_anchored(self):
        self.send(timestamp=4000)
        # The stream stalls for a minute and resumes without the sender's
        # timestamp having moved on -- media time now claims a minute ago.
        with self.assertLogs("audio_tap", level="INFO"):
            start, _gap = self.send(timestamp=4000 + 160, delay=60.0)
        self.assertEqual(self.rtp.resyncs, 1)
        self.assertAlmostEqual(start, self.clock.now - 0.02)

    def test_a_timestamp_going_backwards_re_anchors_rather_than_wrapping(self):
        self.send(timestamp=4000)
        with self.assertLogs("audio_tap", level="INFO"):
            start, _gap = self.send(timestamp=100, delay=0.02)
        self.assertEqual(self.rtp.resyncs, 1)
        self.assertAlmostEqual(start, self.clock.now - 0.02)

    def test_the_sample_counter_wraps_without_a_jump(self):
        top = 0xFFFFFFFF - 79  # 80 samples short of wrapping
        self.send(timestamp=top)
        anchored = self.clock.now - 0.02
        start, gap = self.send(timestamp=(top + 160) & 0xFFFFFFFF, delay=0.02)
        self.assertAlmostEqual(start, anchored + 0.02)
        self.assertEqual(gap, 0.0)
        self.assertEqual(self.rtp.resyncs, 0)

    def test_without_a_timestamp_it_behaves_as_arrival_stamping_did(self):
        start, gap = self.send(timestamp=None)
        self.assertAlmostEqual(start, self.clock.now - 0.02)
        self.assertEqual((gap, self.rtp.gaps), (0.0, 0))

    def test_a_reset_forgets_the_anchor_and_the_counters(self):
        self.send(timestamp=4000, seq=1)
        self.rtp.reset()
        self.assertEqual((self.rtp.packets, self.rtp.lost, self.rtp.gaps), (0, 0, 0))
        start, _gap = self.send(timestamp=999, delay=5.0)
        self.assertAlmostEqual(start, self.clock.now - 0.02)


class TestMulaw(unittest.TestCase):
    def test_the_table_matches_the_g711_reference_at_its_boundaries(self):
        # Both encodings of zero, and both full-scale extremes. If the
        # expansion is wrong these are the values that move first.
        self.assertEqual(audio_tap.MULAW_TO_PCM16[0xFF], 0)
        self.assertEqual(audio_tap.MULAW_TO_PCM16[0x7F], 0)
        self.assertEqual(audio_tap.MULAW_TO_PCM16[0x00], -32124)
        self.assertEqual(audio_tap.MULAW_TO_PCM16[0x80], 32124)

    def test_the_table_is_monotonic_within_each_sign(self):
        # A transposed nibble or a wrong shift shows up here even when the
        # boundary values above happen to survive it.
        positives = [audio_tap.MULAW_TO_PCM16[b] for b in range(0x80, 0x100)]
        self.assertEqual(positives, sorted(positives, reverse=True))
        negatives = [audio_tap.MULAW_TO_PCM16[b] for b in range(0x00, 0x80)]
        self.assertEqual(negatives, sorted(negatives))

    def test_decode_produces_little_endian_pairs(self):
        pcm = audio_tap.decode(b"\x00\xff\x80")
        self.assertEqual(len(pcm), 6)
        self.assertEqual(
            [int.from_bytes(pcm[i:i + 2], "little", signed=True) for i in range(0, 6, 2)],
            [-32124, 0, 32124],
        )

    def test_rms_of_silence_is_zero_and_of_full_scale_is_full_scale(self):
        self.assertEqual(audio_tap.frame_rms(SILENT_FRAME), 0.0)
        self.assertAlmostEqual(audio_tap.frame_rms(LOUD_FRAME), 32124.0, places=0)
        self.assertEqual(audio_tap.frame_rms(b""), 0.0)


class TestSilenceDetection(unittest.TestCase):
    """The default segmentation test, and the reason it is the default.

    Measured on real hardware (2026-08-19): a squelched SDS200 emits the
    silence codes and nothing else -- 17,209 of 18,000 frames at exactly RMS
    0, one whole minute at 3000/3000. There is no noise floor, so there is no
    threshold worth picking.
    """

    def test_both_encodings_of_zero_count_as_silence(self):
        # A closed squelch emits both 0xFF and 0x7F. Treating only one as
        # silence would make half of dead air look like audio.
        self.assertTrue(audio_tap.is_silent(SILENT_FRAME))
        self.assertTrue(audio_tap.is_silent(NEG_SILENT_FRAME))
        self.assertTrue(audio_tap.is_silent(b"\xff\x7f" * 80))

    def test_any_other_byte_is_not_silence(self):
        self.assertFalse(audio_tap.is_silent(LOUD_FRAME))
        self.assertFalse(audio_tap.is_silent(QUIET_FRAME))
        # One stray sample in an otherwise dead frame is enough. The opening
        # hysteresis, not this, is what stops a lone glitch opening a segment.
        self.assertFalse(audio_tap.is_silent(SILENT_FRAME[:-1] + b"\x00"))

    def test_an_empty_frame_is_silent(self):
        self.assertTrue(audio_tap.is_silent(b""))

    def test_the_default_is_the_silence_test_not_a_threshold(self):
        self.assertEqual(audio_tap.DEFAULT_THRESHOLD, 0.0)


class TestNormalising(unittest.TestCase):
    """Level, which Whisper is more sensitive to than its own normalisation
    suggests. Quiet speech is a documented way to get an empty transcript out
    of it, and scanner audio arrives at whatever level the transmitting
    radio, the path and the receiver's AGC happened to leave it at."""

    def pcm(self, *values):
        return b"".join(int(v & 0xFFFF).to_bytes(2, "little") for v in values)

    def peaks(self, pcm):
        values = [int.from_bytes(pcm[i:i + 2], "little", signed=True)
                  for i in range(0, len(pcm), 2)]
        return max(max(values), -min(values))

    def test_a_quiet_clip_is_brought_up(self):
        out = audio_tap.normalise(self.pcm(1000, -1000, 500))
        self.assertGreater(self.peaks(out), 5000)

    def test_nothing_is_clipped(self):
        out = audio_tap.normalise(self.pcm(30000, -30000))
        self.assertLessEqual(self.peaks(out), 32767)

    def test_an_already_loud_clip_is_left_alone(self):
        original = self.pcm(30000, -30000)
        self.assertEqual(audio_tap.normalise(original), original)

    def test_gain_is_bounded_so_near_silence_is_not_amplified_into_speech(self):
        # A clip that is quiet because it contains nothing must not be
        # turned into something that sounds like it contains something.
        out = audio_tap.normalise(self.pcm(2, -2, 1))
        self.assertLessEqual(self.peaks(out), 2 * audio_tap.NORMALISE_MAX_GAIN + 1)

    def test_digital_silence_is_untouched(self):
        original = self.pcm(0, 0, 0)
        self.assertEqual(audio_tap.normalise(original), original)

    def test_short_and_empty_input_do_not_raise(self):
        self.assertEqual(audio_tap.normalise(b""), b"")
        self.assertEqual(audio_tap.normalise(b"\x01"), b"\x01")


class TestUpsampling(unittest.TestCase):
    """8kHz -> 16kHz for the Wyoming backend.

    A WAV declares its own rate, so the OpenAI path can hand over 8kHz
    safely. Wyoming carries the rate as a claim beside raw samples, and a
    server that assumes 16kHz regardless plays 8kHz audio at double speed --
    which transcribes as an empty string, indistinguishable on the history
    row from nobody having spoken.
    """

    def test_it_doubles_the_sample_count(self):
        pcm = audio_tap.decode(LOUD_FRAME)
        self.assertEqual(len(audio_tap.upsample_16k(pcm)), 2 * len(pcm))

    def test_original_samples_are_preserved_in_place(self):
        # Interpolation must not disturb the samples that were actually
        # measured -- only fill between them.
        pcm = audio_tap.decode(b"\x00\x80\xff")
        out = audio_tap.upsample_16k(pcm)
        values = [int.from_bytes(out[i:i + 2], "little", signed=True)
                  for i in range(0, len(out), 2)]
        self.assertEqual(values[0::2], [-32124, 32124, 0])

    def test_inserted_samples_are_the_midpoints(self):
        pcm = audio_tap.decode(b"\x00\x80")
        out = audio_tap.upsample_16k(pcm)
        values = [int.from_bytes(out[i:i + 2], "little", signed=True)
                  for i in range(0, len(out), 2)]
        self.assertEqual(values[1], (-32124 + 32124) // 2)

    def test_short_and_empty_input_do_not_raise(self):
        self.assertEqual(audio_tap.upsample_16k(b""), b"")
        self.assertEqual(audio_tap.upsample_16k(b"\x01"), b"\x01")


class TestToWav(unittest.TestCase):
    def test_it_is_a_readable_8khz_mono_16bit_wav(self):
        data = audio_tap.to_wav(LOUD_FRAME)
        with wave.open(io.BytesIO(data), "rb") as parsed:
            self.assertEqual(parsed.getnchannels(), 1)
            self.assertEqual(parsed.getsampwidth(), 2)
            self.assertEqual(parsed.getframerate(), audio_tap.SAMPLE_RATE)
            # One mu-law byte in, one 16-bit frame out -- not resampled.
            self.assertEqual(parsed.getnframes(), len(LOUD_FRAME))


class TestAudioRingBuffer(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.buffer = AudioRingBuffer(window_s=1.0, clock=self.clock)

    def test_it_evicts_by_wall_clock_not_by_chunk_count(self):
        # Ten chunks well inside the window all survive; the same ten spread
        # past it do not. A count-based bound would keep the wrong ones.
        for _ in range(10):
            self.clock.tick(0.02)
            self.buffer.append(SILENT_FRAME)
        self.assertEqual(self.buffer.nbytes, 10 * audio_tap.FRAME_SAMPLES)

        self.clock.tick(5.0)
        self.buffer.append(SILENT_FRAME)
        self.assertEqual(self.buffer.nbytes, audio_tap.FRAME_SAMPLES)

    def test_audio_can_be_filed_under_when_it_was_heard(self):
        # Not when the datagram turned up. The segmenter works from the media
        # clock, so a buffer stamping by arrival would answer a segment's
        # window with different seconds than the ones it named.
        self.clock.tick(10.0)
        self.buffer.append(SILENT_FRAME, at=self.clock.now - 5.0)
        self.assertEqual(self.buffer.slice(self.clock.now - 5.1, self.clock.now - 4.9),
                         SILENT_FRAME)
        self.assertEqual(self.buffer.slice(self.clock.now - 0.1, self.clock.now), b"")

    def test_duration_tracks_the_bytes_held(self):
        for _ in range(50):
            self.clock.tick(0.02)
            self.buffer.append(SILENT_FRAME)
        self.assertAlmostEqual(self.buffer.duration_s, 1.0, places=3)

    def test_a_slice_takes_every_overlapping_chunk_and_no_others(self):
        # Three distinguishable chunks at 0.02s intervals from t=1000.02.
        marks = [b"\x01", b"\x02", b"\x03"]
        for mark in marks:
            self.clock.tick(0.02)
            self.buffer.append(mark * audio_tap.FRAME_SAMPLES)

        # A window covering only the middle chunk's span.
        middle = self.buffer.slice(1000.021, 1000.039)
        self.assertEqual(set(middle), {2})

        everything = self.buffer.slice(999.0, 1001.0)
        self.assertEqual(len(everything), 3 * audio_tap.FRAME_SAMPLES)

    def test_an_empty_or_inverted_window_yields_nothing(self):
        self.clock.tick(0.02)
        self.buffer.append(LOUD_FRAME)
        self.assertEqual(self.buffer.slice(1000.0, 1000.0), b"")
        self.assertEqual(self.buffer.slice(1000.5, 1000.1), b"")

    def test_clear_drops_everything(self):
        self.clock.tick(0.02)
        self.buffer.append(LOUD_FRAME)
        self.buffer.clear()
        self.assertEqual(self.buffer.nbytes, 0)
        self.assertEqual(self.buffer.slice(0.0, 1e12), b"")


class SegmenterTestCase(unittest.TestCase):
    """Feeds frames in real time on a fake clock, so the times a segment is
    reported with are the times the audio actually occupied."""

    def build(self, **kwargs):
        self.clock = FakeClock()
        self.segments = []
        options = dict(
            threshold=100.0,
            open_frames=3,
            close_frames=5,
            preroll_s=0.0,
            max_segment_s=30.0,
        )
        options.update(kwargs)
        self.segmenter = VoiceSegmenter(
            lambda start, end: self.segments.append((start, end)),
            clock=self.clock,
            **options,
        )
        return self.segmenter

    def feed(self, frame, count=1):
        """Advance the clock to each frame's arrival time, then feed it."""
        for _ in range(count):
            self.clock.tick(audio_tap.FRAME_S)
            self.segmenter.feed(frame)


class TestWhyASegmentClosed(SegmenterTestCase):
    """The reason recorded with every close.

    "Some transmissions are getting cut in two" cannot be answered from the
    outside: dead air, a hole in the delivered audio and the length cap all
    produce the same thing -- two clips where there should be one -- and they
    have entirely different fixes. Without the reason the only way to choose
    between them is to argue.
    """

    def test_dead_air_is_recorded_as_a_silence_close(self):
        self.build(open_frames=1, close_frames=3)
        self.feed(LOUD_FRAME, 4)
        self.feed(SILENT_FRAME, 4)
        self.assertEqual(self.segmenter.last_close, audio_tap.CLOSE_SILENCE)
        self.assertEqual(self.segmenter.closes, {audio_tap.CLOSE_SILENCE: 1})

    def test_a_hole_in_the_audio_is_recorded_as_a_gap_close(self):
        self.build(open_frames=1, close_frames=50)
        start = self.clock.now
        for index in range(4):
            self.segmenter.feed(LOUD_FRAME, at=start + index * audio_tap.FRAME_S)
        self.segmenter.feed(LOUD_FRAME, at=start + 4 * audio_tap.FRAME_S + 1.0)
        self.assertEqual(self.segmenter.last_close, audio_tap.CLOSE_GAP)

    def test_the_length_cap_is_recorded_as_a_cap_close(self):
        self.build(open_frames=1, close_frames=500, max_segment_s=0.5)
        self.feed(LOUD_FRAME, 40)
        self.assertEqual(self.segmenter.last_close, audio_tap.CLOSE_CAP)

    def test_the_reason_is_readable_by_the_listener_it_was_handed_to(self):
        # The whole point of keeping it as state: _on_segment reads it while
        # handling the segment it describes.
        seen = []
        self.build(open_frames=1, close_frames=3)
        self.segmenter.on_segment = lambda s, e: seen.append(self.segmenter.last_close)
        self.feed(LOUD_FRAME, 4)
        self.feed(SILENT_FRAME, 4)
        self.assertEqual(seen, [audio_tap.CLOSE_SILENCE])

    def test_a_suspected_split_says_what_would_have_kept_it_together(self):
        # Two segments in quick succession is the symptom being chased, and
        # the line names the setting rather than leaving it to be worked out.
        self.build(open_frames=1, close_frames=3)
        self.feed(LOUD_FRAME, 4)
        self.feed(SILENT_FRAME, 4)
        with self.assertLogs("audio_tap", level="INFO") as logs:
            self.feed(LOUD_FRAME, 4)
            self.feed(SILENT_FRAME, 4)
        self.assertIn("opened", logs.output[-1])
        self.assertIn("would have kept them together", logs.output[-1])

    def test_transmissions_far_apart_are_not_reported_as_a_split(self):
        self.build(open_frames=1, close_frames=3)
        self.feed(LOUD_FRAME, 4)
        self.feed(SILENT_FRAME, 4)
        self.feed(SILENT_FRAME, int(audio_tap.SPLIT_SUSPECT_S / audio_tap.FRAME_S) + 5)
        with self.assertNoLogs("audio_tap", level="INFO"):
            self.feed(LOUD_FRAME, 4)
            self.feed(SILENT_FRAME, 4)


class TestSegmentingOnTheMediaClock(SegmenterTestCase):
    """`feed(..., at=)`, which is where the drift was.

    Without it the timeline is a byte count: it advances only by audio that
    arrived, so a gap in delivery pulls every later timestamp earlier and the
    error never comes back. These pin both halves of the fix -- that the
    times follow the clock they are given, and that a hole wide enough to
    hear ends the transmission rather than being spliced across.
    """

    def feed_at(self, frame, count=1, at=None, step=None):
        step = audio_tap.FRAME_S if step is None else step
        for _ in range(count):
            self.clock.tick(step)
            at = self.clock.now - audio_tap.FRAME_S if at is None else at
            self.segmenter.feed(frame, at=at)
            at += audio_tap.FRAME_S

    def test_a_stall_does_not_pull_the_timeline_backwards(self):
        # Ten frames, then a two-second hole, then a transmission. The
        # reported start must be where the audio actually was.
        self.build(open_frames=1, close_frames=2)
        self.feed_at(SILENT_FRAME, 10)
        self.clock.tick(2.0)
        resumed = self.clock.now
        for index in range(6):
            self.segmenter.feed(LOUD_FRAME if index < 4 else SILENT_FRAME,
                                at=resumed + index * audio_tap.FRAME_S)
        self.assertEqual(len(self.segments), 1)
        self.assertAlmostEqual(self.segments[0][0], resumed, places=6)

    def test_a_gap_closes_the_transmission_it_interrupts(self):
        # A splice is not a transmission. Claiming continuous speech across
        # audio nobody has is how a clip stops matching its own transcript.
        self.build(open_frames=1, close_frames=5)
        start = self.clock.now
        for index in range(4):
            self.segmenter.feed(LOUD_FRAME, at=start + index * audio_tap.FRAME_S)
        self.segmenter.feed(LOUD_FRAME, at=start + 4 * audio_tap.FRAME_S + 1.0)
        self.assertEqual(len(self.segments), 1)
        self.assertAlmostEqual(self.segments[0][1], start + 4 * audio_tap.FRAME_S,
                               places=6)

    def test_a_hole_too_small_to_hear_does_not_cut_anything(self):
        # One lost packet is 20ms. Splitting a transmission over that would
        # cost more than it saves.
        self.build(open_frames=1, close_frames=5)
        start = self.clock.now
        for index in range(6):
            skew = audio_tap.MAX_BRIDGED_GAP_S / 2 if index >= 3 else 0.0
            self.segmenter.feed(LOUD_FRAME, at=start + index * audio_tap.FRAME_S + skew)
        self.assertEqual(self.segments, [])
        self.assertTrue(self.segmenter.in_segment)


class TestVoiceSegmenter(SegmenterTestCase):
    def test_it_needs_the_full_opening_run_before_a_segment_starts(self):
        self.build(open_frames=3)
        self.feed(LOUD_FRAME, 2)
        self.assertFalse(self.segmenter.in_segment)
        self.feed(LOUD_FRAME, 1)
        self.assertTrue(self.segmenter.in_segment)

    def test_a_brief_spike_does_not_open_a_segment(self):
        # Two loud frames then silence: an ignition pop or a squelch tail,
        # not speech. The run counter has to reset, or noise accumulates
        # across minutes and eventually opens a segment on nothing.
        self.build(open_frames=3)
        for _ in range(10):
            self.feed(LOUD_FRAME, 2)
            self.feed(SILENT_FRAME, 2)
        self.assertEqual(self.segments, [])

    def test_the_segment_starts_where_the_run_began_not_where_it_was_noticed(self):
        self.build(open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(SILENT_FRAME, 10)
        first_loud_at = self.clock.now
        self.feed(LOUD_FRAME, 10)
        self.feed(SILENT_FRAME, 5)

        self.assertEqual(len(self.segments), 1)
        start, _end = self.segments[0]
        # Not first_loud_at + 3 frames, which is when the decision was made.
        self.assertAlmostEqual(start, first_loud_at, places=6)

    def test_the_preroll_extends_the_start_backwards(self):
        self.build(open_frames=3, close_frames=5, preroll_s=0.3)
        self.feed(SILENT_FRAME, 30)
        first_loud_at = self.clock.now
        self.feed(LOUD_FRAME, 10)
        self.feed(SILENT_FRAME, 5)

        start, _end = self.segments[0]
        self.assertAlmostEqual(start, first_loud_at - 0.3, places=6)

    def test_the_segment_ends_where_the_silence_began(self):
        # The closing hysteresis waits close_frames of silence, but that
        # silence is what the model hallucinates over -- it must not be in
        # the emitted span.
        self.build(open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(LOUD_FRAME, 10)
        silence_began_at = self.clock.now
        self.feed(SILENT_FRAME, 5)

        _start, end = self.segments[0]
        self.assertAlmostEqual(end, silence_began_at, places=6)

    def test_a_short_gap_does_not_split_one_transmission(self):
        # A single quiet frame mid-word (a breath, a syllable boundary) is
        # well under close_frames and must not end the call.
        self.build(open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(LOUD_FRAME, 5)
        self.feed(SILENT_FRAME, 2)
        self.feed(LOUD_FRAME, 5)
        self.assertEqual(self.segments, [])
        self.feed(SILENT_FRAME, 5)
        self.assertEqual(len(self.segments), 1)

    def test_back_to_back_transmissions_are_two_segments(self):
        self.build(open_frames=3, close_frames=5, preroll_s=0.0)
        for _ in range(2):
            self.feed(LOUD_FRAME, 10)
            self.feed(SILENT_FRAME, 6)
        self.assertEqual(len(self.segments), 2)
        self.assertLess(self.segments[0][1], self.segments[1][0])

    def test_a_stuck_open_carrier_is_capped_rather_than_growing_forever(self):
        self.build(open_frames=3, close_frames=5, preroll_s=0.0, max_segment_s=0.2)
        self.feed(LOUD_FRAME, 50)
        self.assertGreaterEqual(len(self.segments), 4)
        for start, end in self.segments:
            self.assertLessEqual(end - start, 0.2 + audio_tap.FRAME_S)

    def test_reset_discards_a_half_open_segment(self):
        # The audio session dropping mid-transmission means the next payload
        # is minutes later. Splicing across that gap would emit one segment
        # spanning the whole outage.
        self.build(open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(LOUD_FRAME, 10)
        self.assertTrue(self.segmenter.in_segment)

        self.segmenter.reset()
        self.assertFalse(self.segmenter.in_segment)
        self.clock.tick(600.0)
        self.feed(SILENT_FRAME, 10)
        self.assertEqual(self.segments, [])

    def test_a_partial_frame_is_held_until_it_is_complete(self):
        # RTP payload sizes are not guaranteed to be a whole number of
        # frames; leftover bytes must carry into the next payload rather
        # than being dropped or padded.
        self.build(open_frames=1, close_frames=5, preroll_s=0.0)
        half = audio_tap.FRAME_SAMPLES // 2
        self.feed(LOUD_FRAME[:half])
        self.assertFalse(self.segmenter.in_segment)
        self.feed(LOUD_FRAME[:half])
        self.assertTrue(self.segmenter.in_segment)

    def test_a_failing_listener_does_not_reach_the_rtp_path(self):
        # on_segment runs inside the datagram handler. An exception escaping
        # it would take down the audio the browser is listening to.
        self.build(open_frames=3, close_frames=5, preroll_s=0.0)

        def explode(start, end):
            raise RuntimeError("boom")

        self.segmenter.on_segment = explode
        self.feed(LOUD_FRAME, 10)
        with self.assertLogs("audio_tap", level="ERROR"):
            self.feed(SILENT_FRAME, 5)
        self.assertFalse(self.segmenter.in_segment)


class TestSegmentingOnSilence(SegmenterTestCase):
    """The default path: threshold 0, so `is_silent` decides.

    The threshold path above is still supported and still tested, for a site
    whose receiver has a real noise floor. It is just not what this hardware
    needs.
    """

    def test_quiet_audio_a_threshold_would_discard_still_opens_a_segment(self):
        # RMS 8 is real audio at the edge of a transmission -- the capture
        # found 86 such frames in one minute -- and any fixed threshold worth
        # setting throws it away.
        #
        # Preceded by real silence, so the learned floor is zero. That is the
        # honest scene: the floor only suppresses a level once that level is
        # what the channel sounds like when idle, and then suppressing it is
        # correct. What must not happen is discarding a quiet onset on a
        # receiver that genuinely does go silent.
        self.build(threshold=0.0, open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(SILENT_FRAME, 300)
        self.assertEqual(self.segmenter.noise_floor, 0.0)
        self.feed(QUIET_FRAME, 3)
        self.assertTrue(self.segmenter.in_segment)

        self.build(threshold=500.0, open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(SILENT_FRAME, 300)
        self.feed(QUIET_FRAME, 10)
        self.assertFalse(self.segmenter.in_segment)

    def test_dead_air_does_not_open_a_segment_in_either_encoding(self):
        self.build(threshold=0.0, open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(SILENT_FRAME, 50)
        self.feed(NEG_SILENT_FRAME, 50)
        self.assertFalse(self.segmenter.in_segment)
        self.assertEqual(self.segments, [])

    def test_a_transmission_is_bounded_by_the_silence_either_side(self):
        self.build(threshold=0.0, open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(SILENT_FRAME, 20)
        started_at = self.clock.now
        self.feed(LOUD_FRAME, 25)
        ended_at = self.clock.now
        self.feed(SILENT_FRAME, 5)

        self.assertEqual(len(self.segments), 1)
        start, end = self.segments[0]
        self.assertAlmostEqual(start, started_at, places=6)
        self.assertAlmostEqual(end, ended_at, places=6)

    def test_a_pause_inside_a_transmission_does_not_split_it(self):
        # Digital silence mid-transmission is common -- a vocoder emits
        # exact zeroes between words. Only close_frames of it ends the call.
        self.build(threshold=0.0, open_frames=3, close_frames=25, preroll_s=0.0)
        self.feed(LOUD_FRAME, 10)
        self.feed(SILENT_FRAME, 10)
        self.feed(LOUD_FRAME, 10)
        self.assertEqual(self.segments, [])
        self.feed(SILENT_FRAME, 25)
        self.assertEqual(len(self.segments), 1)


class TestTheNoiseFloor(SegmenterTestCase):
    """A receiver that never goes quite quiet.

    The exact silence test is right whenever a closed squelch produces
    exactly the silence code, and wrong the moment it does not -- which was
    observed on the same scanner within a day of the measurement that
    justified it. With any noise at all no frame is ever silent, so a segment
    opens and never closes until the length cap: expensive clips of mostly
    hiss, which is both the worst thing to transcribe and the thing that puts
    a slow model permanently behind.
    """

    def test_a_noisy_idle_channel_does_not_open_a_segment(self):
        # The failure this exists for. Under the pure silence test every one
        # of these frames counts as audio.
        self.build(threshold=0.0, open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(QUIET_FRAME, 500)
        self.assertEqual(self.segments, [])
        self.assertFalse(self.segmenter.in_segment)

    def test_speech_over_a_noise_floor_still_opens_a_segment(self):
        self.build(threshold=0.0, open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(QUIET_FRAME, 200)
        self.feed(LOUD_FRAME, 20)
        self.assertTrue(self.segmenter.in_segment)

    def test_a_transmission_over_noise_closes_instead_of_running_to_the_cap(self):
        # The whole symptom: 99% of real transmissions are under six seconds,
        # so a segment reaching the cap means the detector never closed.
        self.build(threshold=0.0, open_frames=3, close_frames=5, preroll_s=0.0,
                   max_segment_s=15.0)
        self.feed(QUIET_FRAME, 200)
        self.feed(LOUD_FRAME, 50)
        self.feed(QUIET_FRAME, 20)
        self.assertEqual(len(self.segments), 1)
        start, end = self.segments[0]
        self.assertLess(end - start, 2.0)

    def test_exact_silence_still_behaves_as_before(self):
        # The good case must not be paid for by the bad one: with a floor of
        # zero this collapses back to the pure silence test.
        self.build(threshold=0.0, open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(SILENT_FRAME, 100)
        self.assertEqual(self.segmenter.noise_floor, 0.0)
        self.feed(QUIET_FRAME, 3)
        self.assertTrue(self.segmenter.in_segment)

    def test_the_floor_is_learned_from_the_first_block_not_slowly(self):
        # A running average cannot bootstrap out of its own zero start: every
        # frame counts as loud until it rises, and it has to rise slowly to
        # avoid being dragged up by speech. A minimum is right immediately.
        self.build(threshold=0.0, open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(QUIET_FRAME, 5)
        self.assertAlmostEqual(self.segmenter.noise_floor, 8.0, places=0)

    def test_speech_cannot_raise_the_floor(self):
        # Loud frames are simply never the minimum, which is the property an
        # average had to be tuned for and this gets for free.
        self.build(threshold=0.0, open_frames=3, close_frames=200, preroll_s=0.0)
        self.feed(QUIET_FRAME, 200)
        floor_before = self.segmenter.noise_floor
        self.feed(LOUD_FRAME, 100)
        self.assertAlmostEqual(self.segmenter.noise_floor, floor_before, places=6)

    def test_a_tap_that_starts_mid_transmission_is_not_left_deaf(self):
        # A minimum is only as good as its window containing something quiet.
        # Attach in the middle of someone talking and it does not -- so the
        # floor is bounded, and the worst that start costs is a few seconds of
        # reduced sensitivity rather than thirty of silence.
        self.build(threshold=0.0, open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(LOUD_FRAME, 300)
        self.assertLessEqual(self.segmenter.noise_floor, audio_tap.MAX_FLOOR)
        self.feed(SILENT_FRAME, 10)
        self.feed(LOUD_FRAME, 5)
        self.assertTrue(self.segmenter.in_segment)

    def test_an_explicit_threshold_overrides_the_learned_floor(self):
        # For a site that would rather state its number than have one
        # inferred.
        self.build(threshold=10_000.0, open_frames=3, close_frames=5, preroll_s=0.0)
        self.feed(QUIET_FRAME, 100)
        self.assertFalse(self.segmenter.in_segment)
        self.feed(LOUD_FRAME, 5)
        self.assertTrue(self.segmenter.in_segment)


class TestNotCuttingPeopleOff(SegmenterTestCase):
    """A pause mid-sentence must not end the transmission.

    Under the old exact-silence test this could not happen: any non-silent
    frame counted as speech, so a breath never closed a segment. Once the
    floor is learned a quiet moment can fall below it, and the closing
    hysteresis became the only thing standing between an ordinary pause and a
    transmission cut in half.
    """

    def test_a_pause_shorter_than_the_gap_does_not_end_the_transmission(self):
        self.build(threshold=0.0, open_frames=3, close_frames=60, preroll_s=0.0)
        self.feed(SILENT_FRAME, 100)
        self.feed(LOUD_FRAME, 20)
        self.feed(SILENT_FRAME, 40)   # 0.8s -- an ordinary pause
        self.feed(LOUD_FRAME, 20)
        self.assertEqual(self.segments, [])
        self.assertTrue(self.segmenter.in_segment)

    def test_a_pause_longer_than_the_gap_ends_it(self):
        self.build(threshold=0.0, open_frames=3, close_frames=60, preroll_s=0.0)
        self.feed(SILENT_FRAME, 100)
        self.feed(LOUD_FRAME, 20)
        self.feed(SILENT_FRAME, 70)
        self.assertEqual(len(self.segments), 1)

    def test_the_trailing_quiet_is_not_part_of_the_clip(self):
        # Why lengthening the gap costs only delay: the segment ends where
        # the silence began, so the extra wait is trimmed rather than sent.
        self.build(threshold=0.0, open_frames=3, close_frames=60, preroll_s=0.0)
        self.feed(SILENT_FRAME, 100)
        self.feed(LOUD_FRAME, 20)
        quiet_began = self.clock.now
        self.feed(SILENT_FRAME, 70)
        _start, end = self.segments[0]
        self.assertAlmostEqual(end, quiet_began, places=6)


class TestSegmenterWithRingBuffer(SegmenterTestCase):
    """The two halves are only useful together: the segmenter names a span,
    the buffer has to be able to produce exactly that span's audio."""

    def test_a_reported_segment_can_be_pulled_back_out_of_the_buffer(self):
        self.build(open_frames=3, close_frames=5, preroll_s=0.04)
        buffer = AudioRingBuffer(window_s=60.0, clock=self.clock)

        original_feed = self.segmenter.feed

        def feed_both(payload):
            buffer.append(payload)
            original_feed(payload)

        self.segmenter.feed = feed_both

        self.feed(SILENT_FRAME, 10)
        self.feed(LOUD_FRAME, 25)
        self.feed(SILENT_FRAME, 5)

        self.assertEqual(len(self.segments), 1)
        start, end = self.segments[0]
        audio = buffer.slice(start, end)

        # 25 loud frames plus two frames of pre-roll, give or take the chunk
        # granularity the slice works at.
        self.assertGreaterEqual(len(audio), 25 * audio_tap.FRAME_SAMPLES)
        self.assertLessEqual(len(audio), 29 * audio_tap.FRAME_SAMPLES)
        # It really is the loud audio, not the silence either side of it.
        self.assertGreater(audio_tap.frame_rms(audio), 1000.0)


if __name__ == "__main__":
    unittest.main()
