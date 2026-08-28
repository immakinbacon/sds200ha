"""Tests for transcribe.py -- deciding what to believe, and what to write.

Most of this file is about **refusing** to record something. That is the
right emphasis: Whisper does not degrade gracefully on weak input, and this
input is weak by construction -- 8kHz telephone bandwidth, and on a digital
system already through a vocoder that resynthesizes speech rather than
carrying it. Given a clip it cannot make out, the model does not return
nothing; it returns fluent, confident, invented text.

That makes a fabricated transcript a false *record* rather than a poor one.
It fires trigger rules and answers searches with events that never happened,
and unlike a garbled transcript there is nothing about it that looks wrong.
So every rejection path here is tested, and each one records why -- a call
nobody spoke in has to stay distinguishable from one the model declined, one
where the server was unreachable, and one that was never attempted, because
all four render as an empty column otherwise.

The queue's drop-oldest behaviour is tested for the same reason it exists: a
model that cannot keep up should cost recent audio, not memory, and should
say so. An unbounded queue turns "the model is too slow" into "the add-on
died overnight".

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import wave
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

try:  # noqa: SIM105 -- see test_audio_bridge_control_reboot.py
    import aiohttp  # noqa: F401
except ImportError:
    sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

import align  # noqa: E402
import audio_bridge  # noqa: E402
import audio_tap  # noqa: E402
import stt  # noqa: E402
import transcribe  # noqa: E402
from transcribe import ClipStore, Transcriber  # noqa: E402

FRAME = audio_tap.FRAME_SAMPLES
LOUD = b"\x00\x80" * (FRAME // 2)
# Two seconds, comfortably over MIN_DURATION_S.
CLIP = LOUD * 100


def tap(buffer, segmenter=None):
    """A tap entry for tests that drive one directly. Only the buffer, the
    segmenter and the correlator are read on those paths; the rest exist for
    detach."""
    return transcribe._Tap(buffer, segmenter, None, None,
                           audio_tap.RtpClock(), align.Correlator("home"))


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


class FakeHistory:
    """`call_at`, `record` and `set_transcript`, which is all the worker uses.

    Keeps what was written rather than only logging it, because the worker
    reads a row back before overwriting it -- a fake that always answers
    "nothing stored" would let the bug this guards against pass.
    """

    def __init__(self, call=None):
        self.call = call if call is not None else {"id": 7, "mode": "analog"}
        self.written: list[tuple] = []
        self.stored: dict[int, dict] = {}
        self.calls = None
        self.missing: set = set()
        self.dropped: list = []

    def call_at(self, scanner_id, start, end):
        return self.call

    def calls_at(self, scanner_id, start, end, limit=4):
        # One transmission can be logged as several calls, so the real store
        # answers with a list. `calls` overrides `call` when a test needs the
        # split case.
        #
        # A row carrying timestamps is matched against the window, because
        # which rows a span covers is now what decides whether a call is
        # marked "transcribing". A row without them matches anything, which
        # is what most of these tests want -- they are about what happens to
        # the row that was found, not about finding it.
        found = list(self.calls) if getattr(self, "calls", None) is not None else (
            [self.call] if self.call else [])
        matched = []
        for call in found:
            started = call.get("started")
            if started is None or (started <= end and call.get("ended", started) >= start):
                # With whatever has since been written to the row. The real
                # store answers from the table, so a row that was given a
                # clip comes back carrying it -- and a fake that forgot that
                # hid a bug where a later segment adopted an earlier one's
                # clip and appended its audio to it.
                stored = self.stored.get(call.get("id")) or {}
                if stored.get("clip") and not call.get("clip"):
                    call = {**call, "clip": stored["clip"]}
                matched.append(call)
        return matched[:limit]

    def record(self, call_id):
        if call_id in self.stored:
            return self.stored[call_id]
        if self.call and self.call.get("id") == call_id:
            return dict(self.call)
        # A row that exists and has no transcript yet, which is what the
        # worker is nearly always looking at. `missing` makes a genuinely
        # absent row available for the tests that need one.
        if call_id in self.missing:
            return None
        return {"id": call_id, "transcript_status": None}

    def awaiting_transcripts(self, status, limit=5_000):
        return list(getattr(self, "pending", []))[:limit]

    def drop(self, call_id):
        self.dropped.append(call_id)
        return {"id": call_id}

    def existing_ids(self, ids):
        """Which of those rows are still there: everything written, plus the
        one the worker is currently being told about."""
        live = set(self.stored)
        if self.call:
            live.add(self.call.get("id"))
        return {int(i) for i in ids if int(i) in live}

    def set_transcript(self, call_id, text, status, clip=None):
        self.written.append((call_id, text, status, clip))
        row = {"id": call_id, "transcript": text, "transcript_status": status,
               "clip": clip}
        self.stored[call_id] = row
        return row


class FakeBridge:
    """Just the audio-listener half of AudioBridge, so `attach` can be tested
    without an RTSP session anywhere near it."""

    def __init__(self):
        self.listeners = []

    def add_audio_listener(self, callback):
        self.listeners.append(callback)

    def remove_audio_listener(self, callback):
        if callback in self.listeners:
            self.listeners.remove(callback)

    def send(self, payload, timestamp=None, seq=None):
        for callback in list(self.listeners):
            callback(audio_bridge.RtpPacket(payload, seq, timestamp))

    def reset(self):
        for callback in list(self.listeners):
            callback(audio_bridge.AUDIO_RESET)


class FakeClient:
    """A speech-to-text server under test control."""

    def __init__(self, result=None, error=None):
        self.result = result or {"text": "engine twelve responding",
                                 "no_speech_prob": None, "avg_logprob": None}
        self.error = error
        self.calls = 0

    def describe(self):
        return "fake://stt"

    def sent_audio(self, mulaw):
        """What the model receives, which is what the clip stores. The real
        clients all have this; a fake without it silently skipped the clip
        paths that depend on it."""
        return audio_tap.decode(mulaw), audio_tap.SAMPLE_RATE

    async def transcribe(self, mulaw):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    async def close(self):
        pass


class TestJudging(unittest.TestCase):
    """What comes back from the model, and whether to believe it."""

    def judge(self, **result):
        payload = {"text": "", "no_speech_prob": None, "avg_logprob": None}
        payload.update(result)
        return Transcriber._judge(payload)

    def test_a_plain_transcript_is_kept(self):
        self.assertEqual(
            self.judge(text="engine twelve responding"),
            ("engine twelve responding", transcribe.OK),
        )

    def test_empty_text_is_recorded_as_nothing_said(self):
        # Not silently skipped: "the model heard nothing" and "we never asked"
        # are different facts about a call.
        self.assertEqual(self.judge(text="  "), ("", transcribe.NO_SPEECH))

    def test_whisper_subtitle_boilerplate_is_rejected(self):
        # Absorbed from its training data and emitted over noise. It is
        # fluent, plausible English and completely unrelated to the audio.
        for text in ("Thank you for watching!", "Subscribe to my channel",
                     "[BLANK_AUDIO]", "www.example.com"):
            with self.subTest(text=text):
                self.assertEqual(self.judge(text=text)[1], transcribe.ARTIFACT)

    def test_a_repetition_loop_is_rejected(self):
        # The classic failure on weak input: one phrase repeated until the
        # window fills. Long, fluent, and entirely invented.
        looped = "the fire the fire the fire the fire the fire the fire"
        self.assertEqual(self.judge(text=looped)[1], transcribe.REPEATED)

    def test_ordinary_repetition_in_real_speech_is_not_rejected(self):
        # Radio traffic repeats itself for a living -- unit numbers, street
        # names, "copy". The check must not fire on that.
        real = "engine twelve engine twelve respond to a structure fire on elm street"
        self.assertEqual(self.judge(text=real)[1], transcribe.OK)

    def test_low_confidence_is_rejected_when_the_backend_reports_it(self):
        self.assertEqual(
            self.judge(text="something", no_speech_prob=0.95)[1], transcribe.DOUBTFUL
        )
        self.assertEqual(
            self.judge(text="something", avg_logprob=-2.5)[1], transcribe.DOUBTFUL
        )

    def test_unreported_confidence_is_not_read_as_good_confidence(self):
        # Wyoming carries no confidence at all. None means "not measured",
        # and treating that as "measured and fine" would be correct by
        # accident here but wrong in general -- it is the difference between
        # skipping a guard and silently disabling it.
        text, status = self.judge(text="engine twelve", no_speech_prob=None,
                                  avg_logprob=None)
        self.assertEqual((text, status), ("engine twelve", transcribe.OK))


class TestModeFiltering(unittest.TestCase):
    def test_blank_means_every_mode(self):
        self.assertTrue(transcribe.wanted_mode("p25", ""))
        self.assertTrue(transcribe.wanted_mode("analog", ""))

    def test_a_list_restricts_to_those_modes(self):
        # The setting exists because analog and digital are different
        # problems for a speech model -- "worth it on the analog channels,
        # not on the trunk" is a real answer.
        self.assertTrue(transcribe.wanted_mode("analog", "analog"))
        self.assertFalse(transcribe.wanted_mode("p25", "analog"))
        self.assertTrue(transcribe.wanted_mode("p25", "analog, p25"))

    def test_matching_ignores_case_and_spacing(self):
        self.assertTrue(transcribe.wanted_mode("P25", " p25 , analog "))


class TranscriberTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = FakeClock()
        self.history = FakeHistory()
        self.clips = ClipStore(os.path.join(self._tmp.name, "clips"), keep=5)

    def build(self, client=None, **settings):
        worker = Transcriber(history=self.history, clips=self.clips, clock=self.clock)
        worker.find_call_retry_s = 0  # no real waiting in tests
        worker.set_client(client or FakeClient())
        options = {"enabled": True, "modes": ""}
        options.update(settings)
        worker.configure("home", **options)
        return worker


class TestTheWorker(TranscriberTestCase):
    async def test_a_transcript_reaches_the_history_row(self):
        worker = self.build()
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.assertEqual(self.history.written[-1][:3], (7, "engine twelve responding", "ok"))

    async def test_a_rejected_transcript_still_records_why(self):
        worker = self.build(FakeClient({"text": "Thank you for watching!",
                                        "no_speech_prob": None, "avg_logprob": None}))
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        call_id, text, status, _clip = self.history.written[-1]
        self.assertEqual((text, status), ("", transcribe.ARTIFACT))

    async def test_a_clip_too_short_is_never_sent_to_the_model(self):
        # Nearly no payload and nearly all hallucination risk. Cheaper to
        # refuse than to ask and then disbelieve.
        client = FakeClient()
        worker = self.build(client)
        await worker._handle("home", 1000.0, 1000.4, LOUD * 20)
        self.assertEqual(client.calls, 0)
        self.assertEqual(self.history.written[-1][2], transcribe.TOO_SHORT)

    async def test_a_server_failure_is_recorded_not_swallowed(self):
        worker = self.build(FakeClient(error=stt.SttError("connection refused")))
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.assertEqual(self.history.written[-1][2], transcribe.ERROR)

    async def test_audio_matching_no_call_is_dropped(self):
        # Transmissions shorter than the GSI poll interval never get a row.
        # There is nowhere to write, so the audio goes rather than being
        # stored orphaned.
        self.history.call = None
        worker = self.build()
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.assertEqual(self.history.written, [])

    async def test_a_mode_the_scanner_excluded_is_not_transcribed(self):
        client = FakeClient()
        self.history.call = {"id": 7, "mode": "p25"}
        worker = self.build(client, modes="analog")
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.assertEqual(client.calls, 0)
        self.assertEqual(self.history.written, [])

    async def test_the_clip_is_kept_even_when_the_transcript_was_rejected(self):
        # Precisely the case where someone wants to hear what the model was
        # given and judge whether the rejection was right.
        worker = self.build(FakeClient({"text": "", "no_speech_prob": None,
                                        "avg_logprob": None}))
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        _call_id, _text, status, clip = self.history.written[-1]
        self.assertEqual(status, transcribe.NO_SPEECH)
        self.assertEqual(clip, "7.wav")
        self.assertTrue(os.path.exists(self.clips.path(clip)))


class TestRequireAudio(TranscriberTestCase):
    """Logging only what the scanner actually played.

    Squelch-open detection asks whether there is RF in front of the receiver.
    On a trunked digital system the site has RF whenever it is active, while
    the scanner unmutes only for a monitored talkgroup -- so the log fills
    with other people's traffic. The audio is the better arbiter of whether a
    call was one this scanner was listening to.
    """

    def build(self, client=None, **settings):
        settings.setdefault("require_audio", True)
        return super().build(client, **settings)

    def end(self, worker, call_id=7):
        worker._heard.add("home")
        worker._attached_at["home"] = self.clock.now - 600
        worker._last_voice_at["home"] = self.clock.now - 5
        worker.on_call("end", {"id": call_id, "scanner_id": "home",
                               "started": self.clock.now - 2,
                               "ended": self.clock.now,
                               "transcript_status": None})

    async def test_a_call_with_no_audio_is_dropped(self):
        worker = self.build()
        self.end(worker)
        self.assertEqual(self.history.dropped, [7])
        self.assertEqual(self.history.written, [])

    async def test_it_never_drops_while_the_feed_looks_dead(self):
        # The dangerous case, and the reason this can be on by default at
        # all: hearing nothing is not evidence that nothing was said, and a
        # wedged audio path would otherwise delete the whole history one call
        # at a time, silently, exactly when it is most worth keeping.
        worker = self.build()
        worker._heard.clear()
        worker._attached_at["home"] = self.clock.now - 600
        worker._last_voice_at.pop("home", None)
        worker.on_call("end", {"id": 7, "scanner_id": "home",
                               "started": self.clock.now - 2,
                               "ended": self.clock.now,
                               "transcript_status": None})
        self.assertEqual(self.history.dropped, [])
        self.assertEqual(self.history.written[-1][2], transcribe.NO_FEED)

    async def test_a_call_with_audio_in_flight_is_not_dropped(self):
        worker = self.build()
        worker._inflight["home"] = [(self.clock.now - 2, self.clock.now)]
        self.end(worker)
        self.assertEqual(self.history.dropped, [])

    async def test_it_is_off_when_not_configured(self):
        worker = self.build(require_audio=False)
        self.end(worker)
        self.assertEqual(self.history.dropped, [])
        self.assertEqual(self.history.written[-1][2], transcribe.NO_AUDIO)

    async def test_it_works_without_transcription_being_enabled(self):
        # The tap runs for every scanner, so a call can be judged against the
        # audio whether or not anyone asked for a transcript.
        worker = self.build(enabled=False, require_audio=True)
        self.end(worker)
        self.assertEqual(self.history.dropped, [7])


class TestPendingTranscripts(TranscriberTestCase):
    """Saying that a transcript is coming.

    A call ends seconds before its transcript arrives -- a model's runtime
    plus whatever queue is ahead of it -- and for that whole window the row
    was blank, which is exactly what a call nobody tried to transcribe looks
    like. The two need telling apart.
    """

    def end(self, worker, call_id, started=None):
        worker.on_call("end", {"id": call_id, "scanner_id": "home",
                               "started": started if started is not None
                               else self.clock.now - 2,
                               "ended": self.clock.now,
                               "transcript_status": None})

    async def test_the_clip_is_playable_before_the_transcript_arrives(self):
        # Listening to a call should not wait on a transcript of it, and on a
        # slow model that wait is minutes.
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        first = self.history.written[0]
        self.assertEqual(first[2], transcribe.PENDING)
        self.assertEqual(first[3], "7.wav")
        self.assertTrue(os.path.exists(self.clips.path("7.wav")))

    async def test_the_clip_is_not_written_twice(self):
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        # Pending pass then result pass, one file, same name on both.
        self.assertEqual(sorted(os.listdir(self.clips.directory)), ["7.wav"])
        self.assertEqual([w[3] for w in self.history.written], ["7.wav", "7.wav"])
        self.assertEqual(self.history.written[-1][1], "engine twelve responding")

    async def test_a_call_with_audio_still_in_flight_says_so(self):
        worker = self.build()
        worker._inflight["home"] = [(self.clock.now - 2, self.clock.now)]
        self.end(worker, 7)
        self.assertEqual(self.history.written[-1][2], transcribe.PENDING)

    async def test_a_call_with_nothing_in_flight_is_still_no_audio(self):
        worker = self.build()
        self.end(worker, 7)
        self.assertEqual(self.history.written[-1][2], transcribe.NO_AUDIO)

    async def test_in_flight_audio_for_a_different_moment_does_not_count(self):
        worker = self.build()
        self.history.call = {"id": 7, "mode": "analog",
                             "started": self.clock.now - 2, "ended": self.clock.now}
        worker._inflight["home"] = [(self.clock.now - 900, self.clock.now - 880)]
        self.end(worker, 7)
        self.assertEqual(self.history.written[-1][2], transcribe.NO_AUDIO)

    async def test_pending_does_not_count_toward_doubting_the_feed(self):
        # Work in progress is not evidence of a dead feed -- quite the
        # opposite, it means audio arrived.
        worker = self.build()
        worker._inflight["home"] = [(self.clock.now - 2, self.clock.now)]
        for call_id in range(transcribe.SILENT_CALLS_BEFORE_DOUBTING_FEED + 2):
            # Each of them is a call the audio in flight covers.
            self.history.calls = [{"id": call_id, "mode": "analog"}]
            self.end(worker, call_id)
        self.assertTrue(all(s == transcribe.PENDING
                            for _i, _t, s, _c in self.history.written))

    async def test_a_clip_pushed_out_of_the_queue_settles_the_rows_waiting(self):
        # The reported symptom: rows claiming "transcribing" long after the
        # audio they were promised was thrown away. PENDING is a promise, and
        # the work that would have kept it is the work that just stopped.
        worker = self.build()
        settled = []
        worker._settle = lambda sid, s, e, status=transcribe.NO_AUDIO: settled.append(status)
        buffer = audio_tap.AudioRingBuffer(window_s=1e6, clock=self.clock)
        buffer.append(CLIP)
        worker._taps["home"] = tap(buffer)
        for _ in range(transcribe.QUEUE_MAXSIZE + 2):
            worker._on_segment("home", self.clock.now - 2, self.clock.now)

        self.assertEqual(settled, [transcribe.DROPPED] * 2)

    async def test_a_clip_too_stale_to_transcribe_says_so_on_the_row(self):
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}]
        self.history.stored[7] = {"id": 7, "transcript": "",
                                  "transcript_status": transcribe.PENDING}
        worker._queue.put_nowait(("home", 1000.0, 1002.0, CLIP))
        self.clock.now = 1002.0 + transcribe.MAX_CLIP_AGE_S + 1
        worker.start()
        await asyncio.sleep(0)
        await worker.stop()

        self.assertEqual(self.history.written[-1][2], transcribe.DROPPED)

    async def test_a_row_that_got_its_words_is_left_alone(self):
        # Settling must never overwrite the thing it was waiting for.
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        worker._settle("home", 1000.0, 1002.0, transcribe.DROPPED)
        self.assertEqual(self.history.written[-1][1], "engine twelve responding")

    async def test_a_row_still_covered_by_other_audio_keeps_waiting(self):
        # One call can be covered by two segments. The first giving up says
        # nothing about the second.
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}]
        self.history.stored[7] = {"id": 7, "transcript": "",
                                  "transcript_status": transcribe.PENDING}
        worker._inflight["home"] = [(1010.0, 1012.0)]
        worker._settle("home", 1000.0, 1002.0, transcribe.DROPPED)
        self.assertEqual(self.history.written, [])

    async def test_audio_that_belonged_to_another_call_settles_the_rest(self):
        # The row was marked "transcribing" because audio was in flight for
        # roughly its moment; the audio turned out to cover a different call.
        # Nothing else will ever come back to this row.
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}]
        self.history.stored[7] = {"id": 7, "transcript": "",
                                  "transcript_status": transcribe.PENDING}
        worker._settle("home", 1000.0, 1002.0)
        self.assertEqual(self.history.written[-1][:3], (7, "", transcribe.NO_AUDIO))

    async def test_a_restart_ends_the_transcribing_state_it_left_behind(self):
        # The queue is in memory. A row still waiting when the add-on stops
        # is waiting on something that no longer exists anywhere, and would
        # say "transcribing" for as long as the row is kept -- rows months
        # old were found in that state on the real install.
        worker = self.build()
        self.history.pending = [4, 5]
        self.assertEqual(worker.settle_pending(), 2)
        self.assertEqual([w[:3] for w in self.history.written],
                         [(4, "", transcribe.DROPPED), (5, "", transcribe.DROPPED)])

    async def test_a_restart_with_nothing_waiting_writes_nothing(self):
        worker = self.build()
        self.assertEqual(worker.settle_pending(), 0)
        self.assertEqual(self.history.written, [])

    async def test_a_dropped_clip_stops_being_counted_as_in_flight(self):
        # Every exit from the pipeline has to clear the span, or a row reads
        # "transcribing" for as long as the add-on runs.
        worker = self.build()
        buffer = audio_tap.AudioRingBuffer(window_s=1e6, clock=self.clock)
        buffer.append(CLIP)
        worker._taps["home"] = tap(buffer)
        for _ in range(transcribe.QUEUE_MAXSIZE + 5):
            worker._on_segment("home", self.clock.now - 2, self.clock.now)
        self.assertLessEqual(len(worker._inflight["home"]),
                             transcribe.QUEUE_MAXSIZE)

    async def test_clearing_the_pipeline_clears_what_was_in_flight(self):
        worker = self.build()
        worker._inflight["home"] = [(self.clock.now - 2, self.clock.now)]
        worker.clear("test")
        self.assertEqual(worker._inflight, {})


class TestDoubtingTheFeed(TranscriberTestCase):
    """Telling "nothing was said" apart from "we cannot hear this scanner".

    A dead audio path is indistinguishable from a quiet channel per call: the
    control interface keeps reporting the squelch opening and RTP keeps
    flowing, every byte of it the silence code. That state has been seen on
    this hardware. Reporting it as "squelch opened but nothing was said"
    claims something about the transmission that we are in no position to
    claim, and sends the reader looking at the wrong thing.
    """

    def end(self, worker, call_id):
        worker.on_call("end", {"id": call_id, "scanner_id": "home",
                               "transcript_status": None})

    def hearing(self, worker, ago=1e6):
        """A scanner we have heard audio from, tapped a while ago.

        `ago` is how long since the last segment -- large by default, so the
        consecutive-silence rules are the thing under test.
        """
        worker._heard.add("home")
        worker._attached_at["home"] = self.clock.now - 600
        worker._last_voice_at["home"] = self.clock.now - ago

    async def test_recent_audio_settles_it_however_many_calls_look_silent(self):
        # A single segment routinely covers several calls, so the ones after
        # it end with nothing of their own and look silent. A real install
        # warned about a dead feed while audio was demonstrably arriving.
        worker = self.build()
        self.hearing(worker, ago=5)
        for call_id in range(transcribe.SILENT_CALLS_BEFORE_DOUBTING_FEED + 4):
            self.end(worker, call_id)
        self.assertTrue(all(s == transcribe.NO_AUDIO
                            for _i, _t, s, _c in self.history.written))

    async def test_a_call_on_a_scanner_never_heard_from_doubts_the_feed_at_once(self):
        # The wedge signature, and it is not subtle: calls are being logged,
        # so the radio is receiving, and nothing has ever arrived, so we are
        # not. Waiting for a run of them only delays saying so.
        worker = self.build()
        worker._attached_at["home"] = self.clock.now - 600
        self.end(worker, 1)
        self.assertEqual(self.history.written[-1][2], transcribe.NO_FEED)

    async def test_a_scanner_only_just_tapped_is_given_a_moment(self):
        # Right after startup, having heard nothing yet means nothing.
        worker = self.build()
        worker._attached_at["home"] = self.clock.now
        self.end(worker, 1)
        self.assertEqual(self.history.written[-1][2], transcribe.NO_AUDIO)

    async def test_a_few_silent_calls_are_still_reported_as_no_audio(self):
        # A squelch blip with nothing behind it is ordinary on a scanner we
        # can otherwise hear.
        worker = self.build()
        self.hearing(worker)
        for call_id in range(2):
            self.end(worker, call_id)
        self.assertTrue(all(s == transcribe.NO_AUDIO
                            for _i, _t, s, _c in self.history.written))

    async def test_a_run_of_them_doubts_the_feed_instead(self):
        worker = self.build()
        self.hearing(worker)
        for call_id in range(transcribe.SILENT_CALLS_BEFORE_DOUBTING_FEED + 2):
            self.end(worker, call_id)
        self.assertEqual(self.history.written[-1][2], transcribe.NO_FEED)

    async def test_any_real_audio_clears_the_doubt(self):
        # So a feed that comes back says so on the very next transmission,
        # and a genuinely quiet channel never trips it in the first place.
        worker = self.build()
        self.hearing(worker)
        buffer = audio_tap.AudioRingBuffer(clock=self.clock)
        buffer.append(CLIP)
        worker._taps["home"] = tap(buffer)
        for call_id in range(transcribe.SILENT_CALLS_BEFORE_DOUBTING_FEED + 1):
            self.end(worker, call_id)
        self.assertEqual(self.history.written[-1][2], transcribe.NO_FEED)

        worker._on_segment("home", self.clock.now - 2, self.clock.now)
        self.end(worker, 99)
        self.assertEqual(self.history.written[-1][2], transcribe.NO_AUDIO)


class TestWaitingForTheCallRow(TranscriberTestCase):
    """The audio is always ready before the row it belongs to exists.

    A segment is released about half a second after the speech stops. The row
    only appears when the next GSI poll notices the squelch -- up to
    GSI_POLL_INTERVAL later -- so for a short transmission the transcript is
    ready before there is anywhere to put it. Looking once and giving up lost
    exactly the calls this path was added to catch, and they then got marked
    "no audio" on a scanner that plainly had audio.
    """

    async def test_a_row_that_appears_late_is_still_matched(self):
        appears_after = 3
        attempts = {"n": 0}
        call = {"id": 7, "mode": "analog"}

        def call_at(scanner_id, start, end):
            attempts["n"] += 1
            return call if attempts["n"] > appears_after else None

        worker = self.build()
        self.history.calls_at = lambda *a, **k: (
            [call] if (attempts.__setitem__("n", attempts["n"] + 1)
                       or attempts["n"] > appears_after) else []
        )
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.assertEqual(self.history.written[-1][1], "engine twelve responding")

    async def test_a_segment_with_no_row_at_all_is_given_up_on(self):
        # Bounded: a transmission the poll loop missed entirely has nowhere to
        # be recorded, and retrying forever would stall the queue behind it.
        worker = self.build()
        self.history.call_at = lambda *a: None
        self.history.calls_at = lambda *a, **k: []
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.assertEqual(self.history.written, [])

    async def test_a_row_that_is_already_there_is_not_waited_for_again(self):
        # The single cheap look up front does double duty: it lets the clip
        # be stored for playback before the model runs, and when it finds the
        # row there is nothing left to wait for afterwards.
        order = []
        client = FakeClient()
        original = client.transcribe

        async def note(mulaw):
            order.append("transcribe")
            return await original(mulaw)

        client.transcribe = note
        worker = self.build(client)
        self.history.calls_at = lambda *a, **k: (order.append("lookup"),
                                                 [{"id": 7, "mode": "analog"}])[1]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.assertEqual(order, ["lookup", "transcribe"])

    async def test_an_excluded_mode_is_never_sent_to_the_model(self):
        # The early look exists for this, and only a positive mismatch stops
        # anything -- absence proves nothing, since the row may not exist yet.
        client = FakeClient()
        self.history.call = {"id": 7, "mode": "p25"}
        worker = self.build(client, modes="analog")
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.assertEqual(client.calls, 0)


class TestCallsThatProducedNoAudio(TranscriberTestCase):
    """A history row with no audio behind it.

    `Rssi != -999` means the squelch opened, which a data burst or a noise
    blip does as readily as speech -- measured at ~23% of "calls" on one real
    scanner. Those rows would otherwise render exactly like calls recorded
    before transcription was switched on, which is the one thing a blank
    genuinely means.
    """

    def end(self, worker, **fields):
        record = {"id": 7, "scanner_id": "home", "transcript_status": None}
        record.update(fields)
        worker.on_call("end", record)

    async def test_a_finished_call_with_no_audio_is_marked(self):
        worker = self.build()
        self.end(worker)
        self.assertEqual(self.history.written[0][:3], (7, "", transcribe.NO_AUDIO))

    async def test_a_call_that_was_already_transcribed_is_left_alone(self):
        # The segmenter lets go of a transmission about half a second after
        # it ends; the poll loop takes two more polls to agree. So the
        # transcript routinely lands first, and must not be overwritten.
        #
        # The state that decides this has to be read back from the store: the
        # record the history hands to its listeners is the CallTracker's,
        # built from poll snapshots, and has never carried a transcript
        # field. Trusting it marked every call "no audio" no matter what had
        # already been transcribed -- which is what actually happened.
        worker = self.build()
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.assertEqual(self.history.written[-1][1], "engine twelve responding")

        self.end(worker)  # a stale record, exactly as the listener gets it
        self.assertEqual(self.history.written[-1][1], "engine twelve responding")

    async def test_a_scanner_not_being_transcribed_is_left_blank(self):
        # A blank row is the honest rendering for a scanner nobody asked to
        # transcribe -- saying "no audio" would imply we looked.
        worker = self.build(enabled=False)
        self.end(worker)
        self.assertEqual(self.history.written, [])

    async def test_only_the_end_of_a_call_is_marked(self):
        worker = self.build()
        worker.on_call("start", {"id": 7, "scanner_id": "home",
                                 "transcript_status": None})
        self.assertEqual(self.history.written, [])

    async def test_a_late_transcript_corrects_the_marking(self):
        # The right way round: the transient state says nothing was heard,
        # the final state is the truth.
        worker = self.build()
        self.end(worker)
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.assertEqual(self.history.written[-1][1], "engine twelve responding")


class TestTheQueue(TranscriberTestCase):
    def enqueue(self, worker, count):
        # The same window every time: this is about what the queue does when
        # it is full, not about distinct audio, and a sliding window would
        # walk off the end of the ring buffer and return empty slices.
        for _ in range(count):
            worker._on_segment("home", 1000.0, 1002.0)

    async def test_nothing_is_queued_without_a_configured_server(self):
        worker = Transcriber(history=self.history, clips=self.clips, clock=self.clock)
        worker.configure("home", enabled=True)
        worker.set_client(None)
        worker._taps["home"] = tap(audio_tap.AudioRingBuffer(clock=self.clock))
        self.enqueue(worker, 3)
        self.assertTrue(worker._queue.empty())

    async def test_nothing_is_queued_for_a_scanner_that_is_switched_off(self):
        worker = self.build(enabled=False)
        buffer = audio_tap.AudioRingBuffer(clock=self.clock)
        buffer.append(CLIP)
        worker._taps["home"] = tap(buffer)
        self.enqueue(worker, 3)
        self.assertTrue(worker._queue.empty())

    async def test_an_overflowing_queue_drops_the_oldest_and_says_so(self):
        # Bounded on purpose. If the model cannot keep up, what is worth
        # having is the most recent audio -- a backlog draining an hour
        # behind real time looks like a working feature and is not one.
        worker = self.build()
        buffer = audio_tap.AudioRingBuffer(window_s=10_000.0, clock=self.clock)
        for _ in range(200):
            self.clock.tick(audio_tap.FRAME_S)
            buffer.append(LOUD)
        worker._taps["home"] = tap(buffer)

        self.enqueue(worker, transcribe.QUEUE_MAXSIZE + 10)
        self.assertEqual(worker._queue.qsize(), transcribe.QUEUE_MAXSIZE)
        self.assertGreater(worker.dropped, 0)


class TestStaleClips(TranscriberTestCase):
    """Work that is no longer worth doing.

    A model slower than the traffic turns the queue into a backlog, and every
    second spent on a four-minute-old clip is a second the next one waits.
    Dropping stale work is how a pipeline that has fallen behind catches up
    rather than falling further behind.
    """

    async def drain_one(self, worker):
        worker.start()
        self.addAsyncCleanup(worker.stop)
        for _ in range(50):
            if worker._queue.empty():
                return
            await asyncio.sleep(0)

    async def test_a_clip_older_than_the_limit_is_never_transcribed(self):
        client = FakeClient()
        worker = self.build(client)
        end = self.clock.now
        self.clock.tick(transcribe.MAX_CLIP_AGE_S + 10)
        worker._queue.put_nowait(("home", end - 2, end, CLIP))
        await self.drain_one(worker)
        self.assertEqual(client.calls, 0)
        self.assertEqual(worker.stale, 1)

    async def test_a_fresh_clip_is_transcribed(self):
        client = FakeClient()
        worker = self.build(client)
        end = self.clock.now
        worker._queue.put_nowait(("home", end - 2, end, CLIP))
        await self.drain_one(worker)
        self.assertEqual(client.calls, 1)
        self.assertEqual(worker.stale, 0)


class TestOneTransmissionLoggedAsSeveralCalls(TranscriberTestCase):
    """The audio is more accurate than the call log, and has to win.

    A call ends when the *identity* changes, so a talkgroup shifting
    mid-transmission splits one continuous piece of speech into two rows.
    The squelch never closed, so the audio is a single segment covering
    both. Matching only the nearest row left the other looking like a call
    nobody spoke in -- reported from a real install as exactly that.
    """

    async def test_the_transcript_reaches_every_row_the_segment_covered(self):
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}, {"id": 8, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        written = {call_id: text for call_id, text, _s, _c in self.history.written}
        self.assertEqual(written[7], "engine twelve responding")
        self.assertEqual(written[8], "engine twelve responding")

    async def test_the_clip_is_stored_once_and_shared(self):
        # One piece of audio however many rows the log split it into.
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}, {"id": 8, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        clips = {clip for _i, _t, _s, clip in self.history.written}
        self.assertEqual(clips, {"7.wav"})
        self.assertEqual(sorted(os.listdir(self.clips.directory)), ["7.wav"])

    async def test_a_row_whose_mode_is_excluded_is_skipped(self):
        worker = self.build(modes="analog")
        self.history.calls = [{"id": 7, "mode": "analog"}, {"id": 8, "mode": "p25"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.assertEqual({c for c, _t, _s, _cl in self.history.written}, {7})


class TestClearingThePipeline(TranscriberTestCase):
    """Queued audio was cut under the settings in force when it was detected.

    Once those change it is the wrong work -- at best reproducing behaviour
    that was just switched off, at worst holding the pipeline minutes behind
    with clips from a detector that was misbehaving, while every new call is
    missed.
    """

    def fill(self, worker, count=4):
        for _ in range(count):
            worker._queue.put_nowait(("home", 1000.0, 1002.0, CLIP))

    async def test_changing_the_server_drops_the_backlog(self):
        worker = self.build()
        self.fill(worker)
        worker.set_client(FakeClient())
        # describe() is the same, so this is not treated as a change.
        self.assertEqual(worker._queue.qsize(), 4)

        other = FakeClient()
        other.describe = lambda: "fake://elsewhere"
        worker.set_client(other)
        self.assertTrue(worker._queue.empty())

    async def test_changing_the_longest_clip_drops_the_backlog(self):
        worker = self.build()
        self.fill(worker)
        worker.set_limits(max_segment_s=worker.max_segment_s)
        self.assertEqual(worker._queue.qsize(), 4)

        worker.set_limits(max_segment_s=10.0)
        self.assertTrue(worker._queue.empty())

    async def test_clearing_also_drops_a_half_open_segment(self):
        # A segment accumulating right now was started under the old rules
        # and would otherwise be emitted under them.
        worker = self.build()
        buffer = audio_tap.AudioRingBuffer(clock=self.clock)
        segmenter = audio_tap.VoiceSegmenter(lambda s, e: None, clock=self.clock)
        worker._taps["home"] = tap(buffer, segmenter)
        quiet = b"\xff" * FRAME
        for frame, count in ((quiet, 50), (LOUD, 20)):
            for _ in range(count):
                self.clock.tick(audio_tap.FRAME_S)
                buffer.append(frame)
                segmenter.feed(frame)
        self.assertTrue(segmenter.in_segment)

        worker.clear("test")
        self.assertFalse(segmenter.in_segment)
        self.assertEqual(buffer.nbytes, 0)

    async def test_clearing_an_empty_pipeline_is_harmless(self):
        worker = self.build()
        self.assertEqual(worker.clear("test"), 0)


class TestClipsMatchTheirTranscripts(TranscriberTestCase):
    """A clip has to contain everything its transcript describes.

    Two segments covering one call both contribute text, so both have to
    contribute audio. Reported from the real install as clips that were
    shorter than what the model had plainly been given -- and a transcript
    you cannot check against what you can hear is the one thing this feature
    cannot afford, since a confident invention reads exactly like a good
    transcription.
    """

    def duration(self, name):
        with wave.open(self.clips.path(name), "rb") as clip:
            return clip.getnframes() / clip.getframerate()

    async def test_a_second_segment_extends_the_clip(self):
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        first = self.duration("7.wav")

        self.history.calls = [{"id": 7, "mode": "analog", "clip": "7.wav"}]
        await worker._handle("home", 1010.0, 1012.0, CLIP)
        self.assertAlmostEqual(self.duration("7.wav"), first * 2, places=1)

    async def test_the_same_segment_is_not_added_twice(self):
        # The pending pass and the result pass carry the same audio.
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.assertAlmostEqual(self.duration("7.wav"),
                               len(CLIP) / audio_tap.SAMPLE_RATE, places=1)

    async def test_the_clip_and_the_transcript_grow_together(self):
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.history.calls = [{"id": 7, "mode": "analog", "clip": "7.wav"}]
        await worker._handle("home", 1010.0, 1012.0, CLIP)
        # Two transcripts written, and a clip holding both pieces of audio.
        texts = [t for _i, t, _s, _c in self.history.written if t]
        self.assertEqual(len(texts), 2)
        self.assertGreater(self.duration("7.wav"), 2.0)


class TestWhoseAudioIsInAClip(TranscriberTestCase):
    """A clip must hold the transmission its row is about, and no other.

    The clip exists so a transcript can be checked by ear, which is the only
    way to tell a good one from a plausible invention. A clip carrying
    somebody else's words does not merely fail at that -- it makes a
    hallucination look confirmed, because the listener hears speech and
    stops. Reported from the real install as the audio not matching the
    transcript.

    Runs of rows overlap at their edges: a row can be the last of one
    segment's run and the first of the next, since a call is ended by an
    identity change while the audio carries on. So "this row already has a
    clip" is not evidence that the clip is ours.
    """

    def duration(self, name):
        with wave.open(self.clips.path(name), "rb") as clip:
            return clip.getnframes() / clip.getframerate()

    async def test_a_later_segment_does_not_grow_the_earlier_ones_clip(self):
        worker = self.build()
        # One segment covering rows 7, 8 and 9 -- the clip is filed under 7.
        self.history.calls = [{"id": 7, "mode": "analog"}, {"id": 8, "mode": "analog"},
                              {"id": 9, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        first = self.duration("7.wav")

        # The next segment's run starts at 9, which already points at 7.wav.
        self.history.calls = [{"id": 9, "mode": "analog"}, {"id": 10, "mode": "analog"}]
        await worker._handle("home", 1010.0, 1012.0, CLIP)

        self.assertAlmostEqual(self.duration("7.wav"), first, places=3)
        self.assertIn("9.wav", os.listdir(self.clips.directory))

    async def test_the_rows_of_the_earlier_run_keep_pointing_at_their_own_audio(self):
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}, {"id": 8, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        self.history.calls = [{"id": 8, "mode": "analog"}, {"id": 9, "mode": "analog"}]
        await worker._handle("home", 1010.0, 1012.0, CLIP)

        self.assertEqual(self.history.stored[7]["clip"], "7.wav")
        self.assertEqual(self.history.stored[9]["clip"], "8.wav")

    async def test_one_call_covered_by_two_segments_still_gets_both(self):
        # The other half, and the reason the extending exists at all: when the
        # same row anchors both segments, its transcript gains both pieces of
        # audio and its clip has to as well, or the text describes more than
        # the clip contains.
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        one = self.duration("7.wav")
        self.history.calls = [{"id": 7, "mode": "analog", "clip": "7.wav"}]
        await worker._handle("home", 1010.0, 1012.0, CLIP)
        self.assertGreater(self.duration("7.wav"), one + 1.0)


class TestAClipAndItsTranscriptAgreeing(TranscriberTestCase):
    """The clip has to be an account of the same thing the text is.

    Two ways they came apart on the real install, and they are mirror images.
    A later fragment overwrote the file while the row kept the earlier text,
    leaving a long call with a transcript and half a second of silence to
    play. And a fragment whose words were discarded still had its audio added
    to the clip, leaving a transcript that plainly misses things the clip
    says. Both make the feature accuse itself of being wrong.
    """

    def duration(self, name):
        with wave.open(self.clips.path(name), "rb") as clip:
            return clip.getnframes() / clip.getframerate()

    async def test_a_second_segment_never_writes_over_the_first_ones_clip(self):
        # No unusual state needed: any restart empties the memory of what was
        # already stored, and the file name is just the row's id.
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1004.0, CLIP + CLIP)
        long_one = self.duration("7.wav")

        worker._clipped.clear()  # as a restart leaves it
        await worker._handle("home", 1010.0, 1011.5, CLIP)

        self.assertAlmostEqual(self.duration("7.wav"), long_one, places=3)
        self.assertIn("7-2.wav", os.listdir(self.clips.directory))

    async def test_audio_joins_a_clip_only_when_its_words_join_the_text(self):
        # A fragment the model returned nothing for is discarded by
        # set_transcript when the row already has words. Its audio must go
        # the same way, or the clip describes more than the transcript does.
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        first = self.duration("7.wav")

        worker.set_client(FakeClient({"text": "", "no_speech_prob": None,
                                      "avg_logprob": None}))
        self.history.calls = [{"id": 7, "mode": "analog", "clip": "7.wav"}]
        await worker._handle("home", 1010.0, 1012.0, CLIP)

        self.assertAlmostEqual(self.duration("7.wav"), first, places=3)

    async def test_a_second_segment_with_words_still_joins_both(self):
        # The case the appending exists for, unchanged: the transcript gains
        # the words and the clip gains the seconds they were said in.
        worker = self.build()
        self.history.calls = [{"id": 7, "mode": "analog"}]
        await worker._handle("home", 1000.0, 1002.0, CLIP)
        first = self.duration("7.wav")
        self.history.calls = [{"id": 7, "mode": "analog", "clip": "7.wav"}]
        await worker._handle("home", 1010.0, 1012.0, CLIP)
        self.assertGreater(self.duration("7.wav"), first + 1.0)


class TestClipStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ClipStore(os.path.join(self._tmp.name, "clips"), keep=3)

    def test_a_clip_is_a_playable_wav_named_for_its_call(self):
        name = self.store.save(42, CLIP)
        self.assertEqual(name, "42.wav")
        with open(self.store.path(name), "rb") as handle:
            self.assertEqual(handle.read(4), b"RIFF")

    def test_a_second_clip_for_one_call_gets_its_own_name(self):
        self.assertEqual(self.store.save(42, CLIP), "42.wav")
        self.assertEqual(self.store.save(42, CLIP), "42-2.wav")
        self.assertEqual(self.store.save(42, CLIP), "42-3.wav")

    def test_a_second_clip_is_pruned_by_its_call_not_as_if_it_had_none(self):
        # "42-2" parses as no id at all unless the suffix is stripped, which
        # sorts it below every real one and deletes it first.
        for call_id in (10, 11, 12):
            self.store.save(call_id, CLIP)
        self.store.save(12, CLIP)
        kept = sorted(os.listdir(self.store.directory))
        self.assertEqual(kept, ["11.wav", "12-2.wav", "12.wav"])

    def test_old_clips_are_pruned_by_call_id(self):
        # By id rather than mtime: monotonic, needs no stat per file, and
        # cannot be confused by a filesystem with coarse timestamps.
        for call_id in range(1, 8):
            self.store.save(call_id, CLIP)
        kept = sorted(int(n[:-4]) for n in os.listdir(self.store.directory))
        self.assertEqual(kept, [5, 6, 7])

    def test_an_unwritable_directory_loses_the_clip_not_the_transcript(self):
        store = ClipStore("/proc/nonexistent/clips", keep=3)
        self.assertIsNone(store.save(1, CLIP))

    def test_discarding_takes_the_file_and_forgives_a_missing_one(self):
        self.store.save(1, CLIP)
        self.assertEqual(self.store.discard(["1.wav", "9.wav"]), 1)
        self.assertEqual(os.listdir(self.store.directory), [])


class TestTheTapsClock(TranscriberTestCase):
    """What the tap does with the timing the sender puts on each packet.

    Both halves of the tap have to place audio the same way. They did not:
    the ring buffer filed a payload under when it arrived while the
    segmenter counted bytes, so once the network lost anything the two
    disagreed -- and a segment's times were being compared against history
    rows written on the wall clock neither of them was following.
    """

    def build_tap(self):
        worker = self.build()
        bridge = FakeBridge()
        worker.attach("home", bridge)
        return worker, bridge

    def test_a_stall_does_not_move_the_audio_it_delays(self):
        worker, bridge = self.build_tap()
        segments = []
        worker._on_segment = lambda sid, start, end: segments.append((start, end))

        # Silence, then a stall long enough to matter, then a transmission
        # whose packets carry the sender's own timestamps throughout.
        stamp = 8000
        for _ in range(10):
            self.clock.tick(audio_tap.FRAME_S)
            bridge.send(b"\xff" * FRAME, timestamp=stamp)
            stamp += FRAME
        self.clock.tick(3.0)
        stamp += 3 * audio_tap.SAMPLE_RATE
        resumed = self.clock.now
        for index in range(110):
            self.clock.tick(audio_tap.FRAME_S)
            bridge.send(LOUD if index < 40 else b"\xff" * FRAME, timestamp=stamp)
            stamp += FRAME

        self.assertEqual(len(segments), 1)
        # Where the audio really was, less the pre-roll -- not three seconds
        # earlier, where a byte-counted timeline would have put it.
        expected = resumed - worker._taps["home"].segmenter.preroll_s
        self.assertAlmostEqual(segments[0][0], expected, delta=audio_tap.FRAME_S * 2)

    def test_the_buffer_holds_the_audio_the_segment_names(self):
        worker, bridge = self.build_tap()
        found = []
        worker._on_segment = lambda sid, start, end: found.append(
            worker._taps["home"].buffer.slice(start, end))

        stamp = 8000
        for index in range(110):
            self.clock.tick(audio_tap.FRAME_S)
            # One late burst, as a network under load produces.
            self.clock.tick(0.5 if index == 12 else 0.0)
            bridge.send(LOUD if index < 40 else b"\xff" * FRAME, timestamp=stamp)
            stamp += FRAME
        self.assertEqual(len(found), 1)
        self.assertGreater(len(found[0]), 20 * FRAME)

    def test_a_session_restart_forgets_the_clock_with_everything_else(self):
        worker, bridge = self.build_tap()
        bridge.send(b"\xff" * FRAME, timestamp=8000, seq=1)
        bridge.reset()
        self.assertEqual(worker._taps["home"].clock.packets, 0)


class TestFeedingTheCorrelator(TranscriberTestCase):
    """Both timelines have to reach `align`, and every lookup has to use what
    it learned.

    The failure mode if one lookup is left in raw time while the others move
    into the log's is not a wrong answer but an inconsistent one: 0.7.46 was a
    row marked "transcribing" by a test that disagreed with the test that
    would have written to it.
    """

    def build_tap(self):
        worker = self.build()
        bridge = FakeBridge()
        worker.attach("home", bridge)
        return worker, worker._taps["home"].correlator

    async def test_a_recording_joins_the_audio_timeline(self):
        worker, correlator = self.build_tap()
        buffer = worker._taps["home"].buffer
        buffer.append(CLIP, at=self.clock.now)
        worker._on_segment("home", self.clock.now - 2, self.clock.now)
        self.assertEqual(len(correlator._audio), 1)

    async def test_a_finished_call_joins_the_other_one(self):
        worker, correlator = self.build_tap()
        worker.on_call("end", {"id": 7, "scanner_id": "home",
                               "started": self.clock.now - 5,
                               "ended": self.clock.now})
        self.assertEqual(len(correlator._calls), 1)

    async def test_a_call_that_is_still_running_does_not(self):
        worker, correlator = self.build_tap()
        worker.on_call("start", {"id": 7, "scanner_id": "home",
                                 "started": self.clock.now})
        self.assertEqual(len(correlator._calls), 0)

    async def test_every_lookup_asks_in_the_same_shifted_time(self):
        # The guard against 0.7.46 coming back by a different route.
        worker, correlator = self.build_tap()
        correlator._offsets.extend([2.0, 2.0, 2.0])
        asked = []
        self.history.calls_at = lambda sid, start, end, limit=4: (
            asked.append((round(start, 3), round(end, 3))) or [])

        worker._settle("home", 1000.0, 1002.0)
        worker._covered_by_inflight("home", 7)
        await worker._find_calls("home", 1000.0, 1002.0)
        self.assertEqual(set(asked), {(1002.0, 1004.0)})

    async def test_nothing_moves_until_the_offset_is_an_answer(self):
        worker, _correlator = self.build_tap()
        asked = []
        self.history.calls_at = lambda sid, start, end, limit=4: (
            asked.append((start, end)) or [])
        worker._settle("home", 1000.0, 1002.0)
        self.assertEqual(asked, [(1000.0, 1002.0)])

    async def test_a_restarted_audio_session_forgets_only_the_recordings(self):
        worker, correlator = self.build_tap()
        correlator._offsets.extend([1.5, 1.5, 1.5])
        correlator.add_audio(self.clock.now - 4, self.clock.now - 2)
        worker._taps["home"].bridge.reset()
        self.assertEqual(len(correlator._audio), 0)
        self.assertAlmostEqual(correlator.offset, 1.5)


class TestRequiringAudioOnceTheMuteFlagAnswers(TranscriberTestCase):
    """"Log only what the scanner played", after the log already knows.

    The setting deletes a call that produced no audio, and it was right to:
    the squelch opening said nothing about whether the scanner unmuted for
    it. Once calls are built from the mute flag that question has already
    been asked, by the scanner, before the row was written -- so hearing
    nothing of a call afterwards is a statement about our own tap, and
    deleting the row over it loses a log entry for a transmission that
    demonstrably happened.
    """

    def end(self, worker, call_id=7):
        worker.on_call("end", {"id": call_id, "scanner_id": "home",
                               "started": self.clock.now - 2, "ended": self.clock.now,
                               "transcript_status": None})

    async def test_a_silent_call_is_still_dropped_when_the_squelch_decided_it(self):
        worker = self.build(require_audio=True)
        self.history.played = lambda scanner_id: False
        worker._heard.add("home")
        worker._last_voice_at["home"] = self.clock.now
        self.end(worker)
        self.assertEqual(self.history.dropped, [7])

    async def test_it_is_kept_when_the_mute_flag_decided_it(self):
        worker = self.build(require_audio=True)
        self.history.played = lambda scanner_id: True
        worker._heard.add("home")
        worker._last_voice_at["home"] = self.clock.now
        self.end(worker)
        self.assertEqual(self.history.dropped, [])
        self.assertEqual(self.history.written[-1][2], transcribe.NO_AUDIO)

    async def test_a_store_that_cannot_say_behaves_as_it_always_did(self):
        worker = self.build(require_audio=True)
        worker._heard.add("home")
        worker._last_voice_at["home"] = self.clock.now
        self.end(worker)
        self.assertEqual(self.history.dropped, [7])


class TestAClearedHistory(TranscriberTestCase):
    """Clips outliving the calls they belong to.

    A clip is named after its row id and `prune` reads recency out of that
    id, both of which hold only while the history is intact. Clearing it
    hands the ids back out from 1, so audio left behind occupies the numbers
    the next calls are about to be given: every clip written afterwards is
    the lowest-numbered file in a full directory, and prunes itself moments
    after it is stored. The row still names it, so the play button appears
    and plays nothing -- which is exactly how this was reported.
    """

    def _clips_from_a_previous_history(self, *call_ids):
        for call_id in call_ids:
            self.clips.save(call_id, CLIP)

    async def test_a_clip_stored_after_a_clear_is_not_pruned_by_the_old_ones(self):
        self._clips_from_a_previous_history(101, 102, 103, 104, 105)
        worker = self.build()
        worker.discard_clips(["101.wav", "102.wav", "103.wav", "104.wav", "105.wav"])

        await worker._handle("home", 1000.0, 1002.0, CLIP)

        self.assertEqual(self.history.written[-1][3], "7.wav")
        self.assertTrue(os.path.exists(self.clips.path("7.wav")))

    async def test_a_reused_id_starts_a_new_clip_rather_than_extending(self):
        # The other half: `_clipped` is keyed by call id too, so an entry
        # left over from before the clear makes the next call with that id
        # append its audio to a deleted call's clip.
        self._clips_from_a_previous_history(7)
        worker = self.build()
        worker._clipped[7] = ((900.0, 902.0), "7.wav")
        worker.discard_clips(["7.wav"])

        await worker._handle("home", 1000.0, 1002.0, CLIP)

        with wave.open(self.clips.path("7.wav"), "rb") as clip:
            seconds = clip.getnframes() / clip.getframerate()
        self.assertLess(seconds, 3.0)

    def test_orphaned_clips_are_reconciled_at_startup(self):
        # For an install already in this state: cleared by a version that
        # left the audio behind, or a history.db deleted by hand.
        self._clips_from_a_previous_history(3, 4)
        self.history.stored[4] = {"id": 4}
        worker = self.build()

        self.assertEqual(worker.reconcile_clips(), 1)
        self.assertEqual(sorted(os.listdir(self.clips.directory)), ["4.wav"])

    def test_reconciling_leaves_files_that_are_not_ours(self):
        os.makedirs(self.clips.directory, exist_ok=True)
        with open(os.path.join(self.clips.directory, "notes.txt"), "w") as handle:
            handle.write("hello")
        worker = self.build()

        self.assertEqual(worker.reconcile_clips(), 0)
        self.assertEqual(os.listdir(self.clips.directory), ["notes.txt"])


if __name__ == "__main__":
    unittest.main()
