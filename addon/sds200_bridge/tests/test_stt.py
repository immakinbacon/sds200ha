"""Tests for stt.py -- the two speech-to-text backends.

The Wyoming half is tested against a **real server on loopback** rather than
a mock, and that is the point of this file. Wyoming is a wire protocol: one
JSON line per event, optionally followed by that event's binary payload. A
mock would only assert that the client calls the methods the mock expects,
which proves nothing about whether a real `wyoming-faster-whisper` on the
other end can read what we send. The fake server here parses the bytes off
the socket exactly as the real one does, so a wrong header key, a missing
newline or a payload length that disagrees with the payload fails here
instead of failing silently against the Home Assistant Whisper add-on and
looking like "Whisper is bad at scanner audio".

Also pinned: that Wyoming reports its confidence fields as **None** rather
than as a passing value. The rejection filters need to distinguish "the model
was confident" from "nobody asked the model" -- defaulting the second to the
first would quietly disable a hallucination guard rather than skip it, which
is the kind of bug that only shows up as bad data months later.

The OpenAI half needs `aiohttp` and skips itself without it, like
test_api_audio_ws.py does.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import audio_tap  # noqa: E402
import stt  # noqa: E402
from stt import OpenAiTranscriber, SttError, WyomingTranscriber  # noqa: E402

try:
    import aiohttp  # noqa: F401
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:  # pragma: no cover -- see the module docstring
    web = None

# A second of "audio": mu-law that is not silence, so it survives any gate.
MULAW = b"\x00\x80" * (audio_tap.SAMPLE_RATE // 2)


class FakeWyomingServer:
    """Parses the Wyoming wire format the way the real server does.

    Records every event received so a test can assert on the *sequence*, not
    just the outcome: a transcript coming back is not evidence that the audio
    got there in a shape the far side could use.
    """

    def __init__(self, transcript="engine twelve responding", events_before=(),
                 payload_transcript=False, hang_up=False,
                 hang_up_after_chunks=False, transcript_data=None,
                 separate_data=False):
        self.transcript = transcript
        self.events_before = events_before
        self.payload_transcript = payload_transcript
        self.hang_up = hang_up
        self.hang_up_after_chunks = hang_up_after_chunks
        self.transcript_data = transcript_data
        self.separate_data = separate_data
        self.received: list[tuple[str, dict, int]] = []
        self.audio = bytearray()
        self._server = None
        self.port = None

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                header = json.loads(line)
                data_length = int(header.get("data_length") or 0)
                if data_length:
                    await reader.readexactly(data_length)
                length = int(header.get("payload_length") or 0)
                payload = await reader.readexactly(length) if length else b""
                self.received.append((header.get("type"), header.get("data") or {}, length))
                if header.get("type") == "audio-chunk":
                    self.audio.extend(payload)
                if header.get("type") != "audio-stop":
                    continue

                if self.hang_up:
                    return
                for kind, data in self.events_before:
                    self._send(writer, kind, data)
                if self.hang_up_after_chunks:
                    await writer.drain()
                    return
                if self.payload_transcript:
                    body = json.dumps({"text": self.transcript}).encode()
                    self._send(writer, "transcript", self.transcript_data, body)
                else:
                    data = dict(self.transcript_data or {})
                    data["text"] = self.transcript
                    self._send(writer, "transcript", data)
                await writer.drain()
                return
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()

    def _send(self, writer, kind, data=None, payload=b""):
        """One event, in the protocol's real shape.

        `data` goes out as a separate length-prefixed chunk when
        `separate_data` is set, which is what the spec allows and what a real
        server was found to do -- and what this fake never did, which is
        precisely why the client's failure to read `data_length` went
        unnoticed through several releases.
        """
        header = {"type": kind}
        extra = b""
        if data:
            if self.separate_data:
                extra = json.dumps(data).encode()
                header["data_length"] = len(extra)
            else:
                header["data"] = data
        if payload:
            header["payload_length"] = len(payload)
        writer.write(json.dumps(header).encode() + b"\n")
        if extra:
            writer.write(extra)
        if payload:
            writer.write(payload)

    def types(self):
        return [kind for kind, _data, _length in self.received]


class WyomingTestCase(unittest.IsolatedAsyncioTestCase):
    async def serve(self, **kwargs):
        self.server = await FakeWyomingServer(**kwargs).start()
        self.addAsyncCleanup(self.server.stop)
        return WyomingTranscriber("127.0.0.1", self.server.port, timeout=5.0)


class TestTheWireFormat(WyomingTestCase):
    """The protocol's own shape, which this client got wrong for a long time.

    An event is a JSON header line, then optionally `data_length` bytes of
    further UTF-8 JSON merged over the header's `data`, then
    `payload_length` bytes of binary. Skipping `data_length` means reading
    the data chunk as if it were the payload: no text found, and every
    subsequent event misaligned.

    The old fake server only ever sent data inline, so the tests agreed with
    the client's misunderstanding instead of with the protocol. These send it
    the other way.
    """

    async def test_a_transcript_sent_as_a_separate_data_chunk_is_read(self):
        server = await FakeWyomingServer(
            transcript="engine twelve responding", separate_data=True
        ).start()
        self.addAsyncCleanup(server.stop)
        client = WyomingTranscriber("127.0.0.1", server.port, timeout=5.0)
        result = await client.transcribe(MULAW)
        self.assertEqual(result["text"], "engine twelve responding")

    async def test_earlier_events_with_separate_data_do_not_desync_the_stream(self):
        # The second failure mode: an unread data chunk leaves the reader
        # pointed at the middle of it, so nothing afterwards parses either.
        server = await FakeWyomingServer(
            transcript="medic four en route",
            separate_data=True,
            events_before=[("info", {"asr": ["whisper"]}), ("transcript-start", {})],
        ).start()
        self.addAsyncCleanup(server.stop)
        client = WyomingTranscriber("127.0.0.1", server.port, timeout=5.0)
        result = await client.transcribe(MULAW)
        self.assertEqual(result["text"], "medic four en route")

    async def test_separate_data_merges_over_the_header_data(self):
        # The spec says merged on top of, not instead of.
        server = await FakeWyomingServer(
            transcript="copy that", separate_data=True,
            transcript_data={"language": "en"},
        ).start()
        self.addAsyncCleanup(server.stop)
        client = WyomingTranscriber("127.0.0.1", server.port, timeout=5.0)
        self.assertEqual((await client.transcribe(MULAW))["text"], "copy that")


class TestWyomingOnTheWire(WyomingTestCase):
    async def test_a_transcript_comes_back(self):
        client = await self.serve(transcript="engine twelve responding")
        result = await client.transcribe(MULAW)
        self.assertEqual(result["text"], "engine twelve responding")

    async def test_the_event_sequence_is_what_a_real_server_expects(self):
        client = await self.serve()
        await client.transcribe(MULAW)
        types = self.server.types()
        self.assertEqual(types[0], "transcribe")
        self.assertEqual(types[1], "audio-start")
        self.assertEqual(types[-1], "audio-stop")
        self.assertTrue(all(t == "audio-chunk" for t in types[2:-1]))

    async def test_the_audio_arrives_decoded_and_resampled(self):
        # The bytes on the wire must be 16kHz PCM16, not the 8kHz mu-law we
        # hold: two bytes per sample and twice as many samples, so four times
        # the length. Sending mu-law and declaring it PCM would transcribe as
        # noise; sending 8kHz to a server that assumes 16kHz transcribes as
        # nothing at all, which is worse because it looks like silence.
        client = await self.serve()
        await client.transcribe(MULAW)
        expected = audio_tap.upsample_16k(audio_tap.decode(MULAW))
        self.assertEqual(bytes(self.server.audio), expected)
        self.assertEqual(len(self.server.audio), 4 * len(MULAW))

    async def test_the_declared_rate_matches_the_audio_actually_sent(self):
        # A mismatch here is the classic Wyoming bug, and unlike a WAV -- which
        # carries its rate in its own header -- an audio-chunk's rate is only a
        # claim. Get it wrong and the far side plays the audio at the wrong
        # speed, transcribes nothing, and reports it as an empty string
        # indistinguishable from silence.
        client = await self.serve()
        await client.transcribe(MULAW)
        seen = 0
        for kind, data, _length in self.server.received:
            if kind in ("audio-start", "audio-chunk"):
                seen += 1
                self.assertEqual(data["rate"], 16000)
                self.assertEqual(data["width"], 2)
                self.assertEqual(data["channels"], 1)
        self.assertGreater(seen, 0)

    async def test_chunks_carry_running_timestamps(self):
        # Home Assistant's own pipeline sends these, and Assist transcribes
        # fine against the same server this client gets an empty string from
        # -- so every difference between the two is worth removing before
        # blaming the audio.
        client = await self.serve()
        await client.transcribe(MULAW)
        stamps = [data.get("timestamp") for kind, data, _n in self.server.received
                  if kind == "audio-chunk"]
        self.assertTrue(all(s is not None for s in stamps))
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(stamps[0], 0)

    async def test_the_clip_is_sent_in_more_than_one_chunk(self):
        # A second of audio at CHUNK_BYTES is many chunks, not one. If this
        # ever becomes a single write the chunking has silently reverted.
        client = await self.serve()
        await client.transcribe(MULAW)
        chunks = [k for k, _d, _n in self.server.received if k == "audio-chunk"]
        self.assertGreater(len(chunks), 5)

    async def test_chunks_are_whole_samples(self):
        # A chunk split mid-sample would put a byte of one sample and a byte
        # of the next on either side of a boundary, which a server
        # reassembling the stream cannot recover from.
        client = await self.serve()
        await client.transcribe(MULAW)
        for kind, _data, length in self.server.received:
            if kind == "audio-chunk":
                self.assertEqual(length % 2, 0)

    async def test_confidence_is_none_not_a_passing_value(self):
        client = await self.serve()
        result = await client.transcribe(MULAW)
        self.assertIsNone(result["no_speech_prob"])
        self.assertIsNone(result["avg_logprob"])

    async def test_events_before_the_transcript_are_skipped(self):
        # A server is free to announce itself or report progress first. That
        # must not be mistaken for the answer.
        client = await self.serve(
            events_before=[("info", {"asr": []}), ("audio-start", {})],
            transcript="medic four en route",
        )
        result = await client.transcribe(MULAW)
        self.assertEqual(result["text"], "medic four en route")

    async def test_a_streamed_transcript_is_assembled(self):
        # The bug that cost the most to find. Newer servers stream the text
        # as transcript-start / transcript-chunk / transcript-stop, and the
        # plain `transcript` event arrives afterwards as an empty envelope --
        # so reading only that returned nothing from a server that had
        # transcribed the audio perfectly well, which looks identical at this
        # end to a model that heard silence.
        client = await self.serve(
            events_before=[
                ("transcript-start", {}),
                ("transcript-chunk", {"text": "engine twelve "}),
                ("transcript-chunk", {"text": "responding"}),
                ("transcript-stop", {}),
            ],
            transcript="",
        )
        result = await client.transcribe(MULAW)
        self.assertEqual(result["text"], "engine twelve responding")

    async def test_the_payload_is_read_even_when_data_has_other_fields(self):
        # The bug a real install actually had. The payload used to be read
        # only when `data` was entirely empty, so a server sending a field or
        # two alongside the text in the payload lost the text silently -- and
        # the symptom was an empty transcript, indistinguishable from a model
        # hearing nothing.
        server = await FakeWyomingServer(
            transcript="", payload_transcript=True,
            transcript_data={"language": "en"},
        ).start()
        self.addAsyncCleanup(server.stop)
        client = WyomingTranscriber("127.0.0.1", server.port, timeout=5.0)
        server.transcript = "engine twelve responding"
        result = await client.transcribe(MULAW)
        self.assertEqual(result["text"], "engine twelve responding")

    async def test_text_under_another_key_is_still_found(self):
        client = await self.serve(
            events_before=[("transcript", {"transcript": "medic four en route"})],
            transcript="",
        )
        result = await client.transcribe(MULAW)
        self.assertEqual(result["text"], "medic four en route")

    async def test_an_empty_result_reports_the_data_keys_and_payload_size(self):
        # So the shape is visible next time instead of costing another guess.
        client = await self.serve(transcript="", transcript_data={"language": "en"})
        with self.assertLogs("stt", level="WARNING") as caught:
            await client.transcribe(MULAW)
        self.assertIn("language", caught.output[0])
        self.assertIn("payload=", caught.output[0])

    async def test_text_is_taken_from_an_unexpected_event_type(self):
        # Twice now a server has carried the text in a shape this client did
        # not read, and both times the symptom was an empty string that looks
        # exactly like a model hearing silence. Taking text wherever it
        # appears beats predicting where it will be.
        client = await self.serve(
            events_before=[("asr-result", {"text": "engine twelve responding"})],
            transcript="",
        )
        result = await client.transcribe(MULAW)
        self.assertEqual(result["text"], "engine twelve responding")

    async def test_an_empty_result_names_the_events_that_arrived(self):
        # So the next occurrence is evidence rather than another guess.
        client = await self.serve(
            events_before=[("transcript-start", {}), ("info", {})],
            transcript="",
        )
        with self.assertLogs("stt", level="WARNING") as caught:
            result = await client.transcribe(MULAW)
        self.assertEqual(result["text"], "")
        self.assertIn("transcript-start", caught.output[0])
        self.assertIn("info", caught.output[0])

    async def test_a_non_empty_final_transcript_still_wins(self):
        # The old shape has to keep working: one event carrying the lot.
        client = await self.serve(transcript="medic four en route")
        result = await client.transcribe(MULAW)
        self.assertEqual(result["text"], "medic four en route")

    async def test_streamed_text_survives_the_server_hanging_up(self):
        # Text that arrived is worth keeping even if the closing event never
        # does.
        server = await FakeWyomingServer(
            events_before=[("transcript-chunk", {"text": "copy that"})],
            hang_up_after_chunks=True,
        ).start()
        self.addAsyncCleanup(server.stop)
        client = WyomingTranscriber("127.0.0.1", server.port, timeout=5.0)
        result = await client.transcribe(MULAW)
        self.assertEqual(result["text"], "copy that")

    async def test_a_transcript_carried_as_a_payload_is_read(self):
        client = await self.serve(payload_transcript=True, transcript="copy that")
        result = await client.transcribe(MULAW)
        self.assertEqual(result["text"], "copy that")

    async def test_a_short_clip_still_sends_one_chunk(self):
        client = await self.serve()
        await client.transcribe(b"\x00" * 100)
        self.assertIn("audio-chunk", self.server.types())

    async def test_a_server_that_hangs_up_raises_stterror(self):
        client = await self.serve(hang_up=True)
        with self.assertRaises(SttError):
            await client.transcribe(MULAW)

    async def test_a_refused_connection_raises_stterror(self):
        # Port 1 on loopback: nothing is listening, and the failure has to
        # arrive as SttError like every other one, not as a bare OSError.
        client = WyomingTranscriber("127.0.0.1", 1, timeout=5.0)
        with self.assertRaises(SttError):
            await client.transcribe(MULAW)


@unittest.skipIf(web is None, "aiohttp is not installed")
class TestOpenAiBackend(unittest.IsolatedAsyncioTestCase):
    async def serve(self, handler):
        app = web.Application()
        app.router.add_post("/v1/audio/transcriptions", handler)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        return f"http://127.0.0.1:{self.client.port}"

    async def test_it_returns_the_worst_confidence_across_segments(self):
        # Worst-case, not average: a confident opening must not carry an
        # invented ending past the rejection filters.
        async def handler(request):
            await request.post()
            return web.json_response({
                "text": "engine twelve responding",
                "segments": [
                    {"no_speech_prob": 0.01, "avg_logprob": -0.2},
                    {"no_speech_prob": 0.90, "avg_logprob": -1.8},
                ],
            })

        url = await self.serve(handler)
        client = OpenAiTranscriber(url, model="small.en")
        self.addAsyncCleanup(client.close)
        result = await client.transcribe(MULAW)
        self.assertEqual(result["text"], "engine twelve responding")
        self.assertAlmostEqual(result["no_speech_prob"], 0.90)
        self.assertAlmostEqual(result["avg_logprob"], -1.8)

    async def test_it_sends_a_wav_and_asks_for_verbose_json(self):
        seen = {}

        async def handler(request):
            data = await request.post()
            seen["format"] = data.get("response_format")
            seen["condition"] = data.get("condition_on_previous_text")
            field = data.get("file")
            seen["riff"] = field.file.read(4)
            return web.json_response({"text": "ok", "segments": []})

        url = await self.serve(handler)
        client = OpenAiTranscriber(url)
        self.addAsyncCleanup(client.close)
        await client.transcribe(MULAW)
        self.assertEqual(seen["format"], "verbose_json")
        # Repetition loops bootstrap off the previous segment's text.
        self.assertEqual(seen["condition"], "false")
        self.assertEqual(seen["riff"], b"RIFF")

    async def test_an_error_status_raises_stterror_with_the_body(self):
        async def handler(request):
            return web.Response(status=503, text="model is loading")

        url = await self.serve(handler)
        client = OpenAiTranscriber(url)
        self.addAsyncCleanup(client.close)
        with self.assertRaises(SttError) as caught:
            await client.transcribe(MULAW)
        self.assertIn("503", str(caught.exception))
        self.assertIn("model is loading", str(caught.exception))


class TestAddressParsing(unittest.TestCase):
    def test_host_and_port(self):
        self.assertEqual(stt.split_host_port("198.51.100.5:10300", 10300), ("198.51.100.5", 10300))

    def test_a_bare_host_takes_the_default_port(self):
        self.assertEqual(stt.split_host_port("whisper", 10300), ("whisper", 10300))

    def test_a_pasted_scheme_is_tolerated(self):
        # Rejecting this would be pedantry: it plainly means the same thing.
        self.assertEqual(stt.split_host_port("tcp://whisper:10300/", 10300),
                         ("whisper", 10300))

    def test_a_nonnumeric_port_falls_back_rather_than_raising(self):
        self.assertEqual(stt.split_host_port("whisper:abc", 10300), ("whisper", 10300))


class TestBuild(unittest.TestCase):
    def test_no_url_means_no_transcriber(self):
        # Not an error. Transcription is opt-in and unconfigured is normal.
        self.assertIsNone(stt.build({"backend": "wyoming", "url": ""}))

    def test_wyoming_is_the_default_backend(self):
        client = stt.build({"url": "198.51.100.5:10300"})
        self.assertIsInstance(client, WyomingTranscriber)
        self.assertEqual((client.host, client.port), ("198.51.100.5", 10300))

    def test_an_openai_backend_carries_its_model(self):
        client = stt.build({"backend": "openai", "url": "http://nuc:8000/",
                            "model": "medium.en"})
        self.assertIsInstance(client, OpenAiTranscriber)
        self.assertEqual(client.model, "medium.en")
        # Trailing slash stripped, so the joined path has exactly one.
        self.assertEqual(client.base_url, "http://nuc:8000")


if __name__ == "__main__":
    unittest.main()
