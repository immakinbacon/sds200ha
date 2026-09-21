"""Speech-to-text, behind one interface with two backends.

Both run somewhere else. This add-on's container is Alpine (musl) and
CTranslate2 -- which faster-whisper needs -- publishes glibc wheels only, so
a model cannot be loaded in this process at all. That constraint turns out
not to cost anything: the natural place for a model is a box with the spare
CPU for it, not the one holding a scanner's RTSP socket.

**Wyoming** talks to Home Assistant's own Whisper add-on
(`wyoming-faster-whisper`), which installs from the add-on store and needs no
setup beyond picking a model. This is the default, and for most installs the
right answer.

**OpenAI-compatible** talks to any server exposing
`/v1/audio/transcriptions` -- a faster-whisper server run by hand, something
GPU-backed, or a hosted API. More to set up; two things in return.

The difference that matters is not speed or quality -- the Whisper add-on
*is* faster-whisper, same engine, same models. It is metadata. Whisper
computes a `no_speech_prob` and an `avg_logprob` per segment, and those are
the cheapest defence against its worst habit: handed weak audio it falls back
on its language prior and returns fluent, confident, invented text. For a
scanner log that is a correctness bug rather than an annoyance, since a
hallucinated "structure fire" fires a trigger rule and a hallucinated search
hit is an event that never happened.

Wyoming carries none of that. It was designed for voice assistants, where
there is always a person who just spoke and the only question is what they
said, so the protocol returns text and nothing else. Behind that backend the
confidence filters are simply unavailable, and what remains is the VAD gate
in `audio_tap.py` (which never hands the model a clip without audio in it --
on this hardware a closed squelch is *exactly* the silence code, so that
gate is unusually solid here), a minimum duration, and a list of known
Whisper artifacts.

That is a reasonable trade for zero setup, and it is the caller's to make,
which is why both are here.
"""

from __future__ import annotations

import asyncio
import json
import logging

import audio_tap

logger = logging.getLogger(__name__)

BACKENDS = ("wyoming", "openai")
DEFAULT_BACKEND = "wyoming"

# The Home Assistant Whisper add-on's default. Its Network settings can remap
# it, which is why the port is part of the configured address rather than
# assumed here.
DEFAULT_WYOMING_PORT = 10300

# Generous: a cold model load on the far side can take tens of seconds, and
# the work is queued in the background rather than in front of anything a
# person is waiting on.
DEFAULT_TIMEOUT_S = 120.0

# Bytes of PCM16 per Wyoming audio-chunk. 1024 samples, which is what Home
# Assistant's own voice pipeline sends over the same protocol to the same
# server.
#
# Matching it is not a claim that chunk size matters -- a server buffering
# until audio-stop should not care. It is that Assist transcribes fine
# against this add-on while this client gets an empty string back, so every
# difference between the two is worth removing before concluding the audio
# is at fault. This was half a second a chunk, which is not wrong, only
# unlike the case that works.
CHUNK_BYTES = 1024 * 2


class SttError(Exception):
    """Any failure to get a transcript. Deliberately one type: every caller
    does the same thing with it (log it, mark the call, move on), and the
    distinction between a refused connection and a malformed reply is for the
    log line, not for control flow."""


def _result(text: str, no_speech_prob=None, avg_logprob=None) -> dict:
    """The normalized shape both backends return.

    The two probabilities are None behind Wyoming rather than defaulted to a
    passing value -- a filter has to be able to tell "the model was confident"
    from "nobody asked the model", and silently treating the second as the
    first would quietly disable the filter rather than skip it.
    """
    return {
        "text": (text or "").strip(),
        "no_speech_prob": no_speech_prob,
        "avg_logprob": avg_logprob,
    }


class WyomingTranscriber:
    """Home Assistant's Whisper add-on, over the Wyoming protocol.

    Wyoming is JSONL over TCP: one JSON line per event, followed by that
    event's binary payload when it has one. A transcription is
    `transcribe` -> `audio-start` -> `audio-chunk`* -> `audio-stop`, and the
    server answers with `transcript`.
    """

    backend = "wyoming"

    def __init__(self, host: str, port: int = DEFAULT_WYOMING_PORT,
                 language: str = "en", timeout: float = DEFAULT_TIMEOUT_S,
                 upsample: bool = True):
        self.host = host
        self.port = port
        self.language = language
        self.timeout = timeout
        # Whether to convert to 16kHz here or declare 8kHz and leave it to
        # the server. Ours is linear interpolation, which leaves a mirrored
        # copy of the audio above 4kHz -- an artifact present in no natural
        # speech. A server that resamples properly will do better; one that
        # ignores the declared rate will play the audio at double speed and
        # transcribe nothing. Which of those is true is not knowable from
        # here, so it is a setting.
        self.upsample = upsample

    def describe(self) -> str:
        rate = "16k" if self.upsample else "8k"
        return f"wyoming://{self.host}:{self.port} ({rate})"

    def sent_audio(self, mulaw: bytes) -> tuple[bytes, int]:
        """Exactly the PCM this would put on the wire, and its rate.

        So the kept clip can be what the model was given rather than what the
        radio produced. Those differ by the resampling, and a transcript can
        only be judged against what was actually transcribed.
        """
        # Normalised before resampling, so the interpolation works on the
        # levels that will actually be sent rather than being scaled after.
        pcm = audio_tap.normalise(audio_tap.decode(mulaw))
        if self.upsample:
            return audio_tap.upsample_16k(pcm), 16000
        return pcm, audio_tap.SAMPLE_RATE

    async def close(self) -> None:
        """Nothing to release -- a connection is opened per transcript. Here
        so callers need not care which backend they hold."""

    async def transcribe(self, mulaw: bytes) -> dict:
        try:
            return await asyncio.wait_for(self._transcribe(mulaw), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise SttError(
                f"{self.describe()} did not answer within {self.timeout:.0f}s"
            ) from None
        except (OSError, ConnectionError) as error:
            raise SttError(f"{self.describe()}: {error}") from error

    async def _transcribe(self, mulaw: bytes) -> dict:
        # Sent at 16kHz rather than declared at 8kHz and left to the server.
        # A WAV carries its rate in its own header, so the OpenAI backend can
        # safely hand over 8kHz -- but a Wyoming audio-chunk carries the rate
        # as a *declaration* next to raw samples, and a server that assumes
        # 16kHz regardless plays this at double speed, which transcribes as
        # an empty string and looks exactly like "nobody said anything".
        # Resampling here costs a linear interpolation instead of a proper
        # filter and buys not having to be right about the far side.
        pcm, rate = self.sent_audio(mulaw)
        audio_format = {"rate": rate, "width": 2, "channels": 1}
        duration = len(pcm) / (rate * 2)
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            self._send(writer, "transcribe", {"language": self.language})
            self._send(writer, "audio-start", {**audio_format, "timestamp": 0})
            chunks = 0
            for offset in range(0, len(pcm), CHUNK_BYTES):
                # Timestamps in milliseconds from the start of the utterance,
                # as Home Assistant's pipeline sends them. Optional in the
                # protocol; included for the same reason as the chunk size.
                stamp = int(offset / (rate * 2) * 1000)
                self._send(writer, "audio-chunk", {**audio_format, "timestamp": stamp},
                           pcm[offset:offset + CHUNK_BYTES])
                chunks += 1
            self._send(writer, "audio-stop", {"timestamp": int(duration * 1000)})
            await writer.drain()
            result = await self._await_transcript(reader)
            # Both halves on one line, because "the model returned nothing"
            # and "the model was given 2.4s across 19 chunks and returned
            # nothing" are different claims, and only the second one rules
            # anything out.
            logger.debug(
                "wyoming: sent %.1fs as %d chunks (%d bytes at %dHz) -> %r",
                duration, chunks, len(pcm), rate, result["text"],
            )
            return result
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass  # the far side hanging up first is not a failure here

    @staticmethod
    def _send(writer, kind: str, data: dict | None = None,
              payload: bytes = b"") -> None:
        header: dict = {"type": kind}
        if data:
            header["data"] = data
        if payload:
            header["payload_length"] = len(payload)
        writer.write(json.dumps(header).encode() + b"\n")
        if payload:
            writer.write(payload)

    @staticmethod
    async def _await_transcript(reader) -> dict:  # noqa: C901
        """Read events until the transcript is complete.

        Two shapes of answer, because the protocol grew one. Originally a
        single `transcript` event carried the whole text. Newer servers stream
        it as `transcript-start`, one or more `transcript-chunk`, then
        `transcript-stop` -- and in that flow the `transcript` event is an
        empty envelope arriving *after* the text it belongs to.

        Reading only the first `transcript` therefore returned an empty string
        from a server that had transcribed the audio perfectly well, which is
        indistinguishable at this end from a model that heard nothing. It cost
        a long time to find, because every part of it looked healthy: the
        round trip completed, the server logged the text it produced, and this
        end logged an empty result.

        So text is collected from any event that carries some, and the
        accumulated text wins whenever the final envelope is empty.
        """
        chunks: list[str] = []
        seen: list[str] = []
        while True:
            line = await reader.readline()
            if not line:
                if chunks:
                    # Text arrived and the server hung up without a closing
                    # event. Keeping it beats discarding it over a formality.
                    return _result("".join(chunks))
                raise SttError(
                    "the Wyoming server closed the connection with no transcript "
                    f"(events: {' -> '.join(seen) or 'none'})"
                )
            try:
                header = json.loads(line)
            except json.JSONDecodeError:
                raise SttError(f"unparseable Wyoming event: {line[:200]!r}") from None
            # A Wyoming event is a JSON header line, then OPTIONALLY
            # `data_length` bytes of further UTF-8 JSON merged on top of the
            # header's own `data`, then `payload_length` bytes of binary.
            #
            # This client did not read `data_length` at all until now, so on
            # a server that sends the transcript as a separate data chunk it
            # read that chunk as if it were the payload: no text found, and
            # the stream misaligned for everything after. The tests could not
            # catch it because the fake server only ever sent data inline --
            # testing against the same misunderstanding the client had.
            kind = header.get("type") or ""
            data = header.get("data") or {}
            data_length = int(header.get("data_length") or 0)
            if data_length:
                extra = await reader.readexactly(data_length)
                try:
                    merged = json.loads(extra)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    merged = None
                if isinstance(merged, dict):
                    data = {**data, **merged}
            length = int(header.get("payload_length") or 0)
            payload = await reader.readexactly(length) if length else b""
            seen.append(
                f"{kind}(data={sorted(data)}, data_length={data_length}, "
                f"payload={len(payload)}B)"
                if not text_in(data) else kind
            )

            # Any event carrying text counts, wherever it is carried. The
            # payload is checked even when `data` has content: the previous
            # version only fell back to it when `data` was entirely empty, so
            # a server sending a field or two alongside the text in the
            # payload lost the text silently -- which is exactly what a real
            # install turned out to be doing.
            text = text_in(data) or text_in_payload(payload)
            if text:
                chunks.append(text)

            if kind in ("transcript", "transcript-stop"):
                answer = "".join(chunks)
                if not answer:
                    # Empty is the failure that has cost the most today, and
                    # it is indistinguishable from a model hearing silence.
                    # Naming the events actually received turns the next
                    # occurrence into evidence instead of another guess.
                    logger.warning(
                        "wyoming returned no text; events were %s. If the server's own "
                        "log shows a transcript for this request, it is arriving in a "
                        "shape this client does not read.",
                        " -> ".join(seen),
                    )
                # No confidence fields: see the module docstring.
                return _result(answer)


class OpenAiTranscriber:
    """Any server exposing `/v1/audio/transcriptions`.

    Asks for `verbose_json` because that is the whole reason to prefer this
    backend: it returns per-segment `avg_logprob` and `no_speech_prob`, which
    the rejection filters need and Wyoming does not carry.
    """

    backend = "openai"

    def __init__(self, base_url: str, model: str = "small.en", language: str = "en",
                 prompt: str = "", timeout: float = DEFAULT_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.language = language
        self.prompt = prompt
        self.timeout = timeout
        self._session = None

    def describe(self) -> str:
        return f"{self.base_url} ({self.model})"

    def sent_audio(self, mulaw: bytes) -> tuple[bytes, int]:
        """A WAV declares its own rate, so this backend sends 8kHz as-is."""
        return audio_tap.normalise(audio_tap.decode(mulaw)), audio_tap.SAMPLE_RATE

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def transcribe(self, mulaw: bytes) -> dict:
        import aiohttp  # local: only this backend needs it at call time

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        form = aiohttp.FormData()
        form.add_field("model", self.model)
        form.add_field("response_format", "verbose_json")
        form.add_field("language", self.language)
        # Off deliberately. The default feeds the previous segment's text back
        # in as context, which is how Whisper's repetition loops bootstrap --
        # and call clips are independent of one another, so there is no
        # context worth keeping even when it behaves.
        form.add_field("condition_on_previous_text", "false")
        if self.prompt:
            form.add_field("prompt", self.prompt)
        pcm, rate = self.sent_audio(mulaw)
        form.add_field(
            "file", audio_tap.pcm_to_wav(pcm, rate),
            filename="call.wav", content_type="audio/wav",
        )
        try:
            async with self._session.post(
                self.base_url + "/v1/audio/transcriptions", data=form
            ) as response:
                if response.status != 200:
                    body = (await response.text())[:200]
                    raise SttError(f"{self.describe()} returned {response.status}: {body}")
                body = await response.json()
        except asyncio.TimeoutError:
            raise SttError(
                f"{self.describe()} did not answer within {self.timeout:.0f}s"
            ) from None
        except aiohttp.ClientError as error:
            raise SttError(f"{self.describe()}: {error}") from error

        segments = body.get("segments") or []
        return _result(
            body.get("text", ""),
            # Worst-case across segments for both, because a filter should
            # reject on the least confident part of a clip rather than let a
            # confident opening carry an invented ending.
            no_speech_prob=max(
                (s.get("no_speech_prob", 0.0) for s in segments), default=None
            ),
            avg_logprob=min(
                (s.get("avg_logprob", 0.0) for s in segments), default=None
            ),
        )


# Keys a Wyoming server might carry the transcript under. "text" is the
# documented one; the rest cost nothing to check and this client has now been
# wrong about the shape of this exchange three times.
TEXT_KEYS = ("text", "transcript", "result")


def text_in(data) -> str:
    """The transcript inside an event's data, under whichever key it used."""
    if not isinstance(data, dict):
        return ""
    for key in TEXT_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def text_in_payload(payload: bytes) -> str:
    """The transcript inside an event's binary payload.

    Tried as JSON first and then as plain UTF-8, because a payload is only
    ever bytes on the wire and the protocol does not oblige a server to say
    which it meant.
    """
    if not payload:
        return ""
    try:
        return text_in(json.loads(payload))
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    try:
        return payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


def split_host_port(address: str, default_port: int) -> tuple[str, int]:
    """"host:port" -> (host, port), with the port optional.

    Tolerant of a scheme someone pasted in out of habit -- "tcp://host:10300"
    and "host:10300" mean the same thing to a Wyoming server, and rejecting
    the first would be pedantry rather than validation.
    """
    address = address.strip()
    if "://" in address:
        address = address.split("://", 1)[1]
    address = address.rstrip("/")
    host, _, port = address.rpartition(":")
    if not host:
        return address, default_port
    try:
        return host, int(port)
    except ValueError:
        # A port that isn't a number: keep the host and fall back on the
        # default. Returning the whole string as the host instead would send
        # "whisper:abc" to the resolver and surface as a DNS failure, which
        # points at the network rather than at the typo that caused it.
        return host, default_port


def build(config: dict):
    """A transcriber from the `transcribe` config section, or None when it is
    not configured. None is a normal state, not an error: transcription is
    opt-in and off by default."""
    url = (config.get("url") or "").strip()
    if not url:
        return None
    backend = config.get("backend") or DEFAULT_BACKEND
    language = config.get("language") or "en"
    if backend == "openai":
        return OpenAiTranscriber(
            url,
            model=config.get("model") or "small.en",
            language=language,
            prompt=config.get("prompt") or "",
        )
    host, port = split_host_port(url, DEFAULT_WYOMING_PORT)
    return WyomingTranscriber(
        host, port, language=language,
        upsample=config.get("upsample", True),
    )
