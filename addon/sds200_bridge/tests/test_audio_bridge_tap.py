"""Tests for AudioBridge's raw-audio tap (add_audio_listener).

The tap is how transcription gets at the audio, and it sits on the RTP
receive path -- the same callback that feeds the live stream every listener
in the house is hearing. So the property worth pinning is not really "the
bytes arrive"; it is that **nothing a tap does can cost anyone their audio**.
A transcriber raising on a malformed packet must not propagate into the
datagram handler, and a tap being attached or removed must not involve the
RTSP session in any way, because reopening a session on this hardware is
what wedges it (see audio_bridge.py's module docstring).

Also pinned here: that AUDIO_RESET (an empty payload) is unambiguous. It
means "the stream you were reading is not contiguous with what comes next",
and that reading only holds because _RtpProtocol drops any datagram too
short to carry a payload -- so a real payload is never empty. If that guard
ever goes, a runt packet starts silently resetting every tap.

And the header parsing, which is new and easy to get quietly wrong. The
payload does not always start at byte 12: a CSRC list or a header extension
pushes it further in, and padding shortens it at the other end. Getting any
of that wrong does not raise -- it splices header bytes into the audio as a
click and shifts every sample after it.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests

No external dependencies, per README -- `aiohttp` is stubbed below, since
audio_bridge imports mikrotik which imports it, but nothing here touches it.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

# Stub aiohttp only if it genuinely isn't installed -- see the comment in
# test_audio_bridge_control_reboot.py for what an unconditional setdefault
# broke in the discover run.
try:  # noqa: SIM105
    import aiohttp  # noqa: F401
except ImportError:
    sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

import audio_bridge  # noqa: E402
from audio_bridge import (  # noqa: E402
    AUDIO_RESET,
    RTP_HEADER_LEN,
    AudioBridge,
    RtpPacket,
    parse_rtp,
)


def rtp(payload: bytes, seq: int = 0, timestamp: int = 0, flags: int = 0x80,
        marker: int = 0) -> bytes:
    """One RTP datagram, built the way the scanner builds them."""
    return (bytes([flags, marker]) + seq.to_bytes(2, "big")
            + timestamp.to_bytes(4, "big") + b"\x00" * 4 + payload)


def bridge() -> AudioBridge:
    """A bridge that has never opened a session. Nothing under test here
    needs one: the tap is fed from _on_rtp_packet, which is reachable
    without any RTSP at all -- which is itself the point."""
    return AudioBridge(
        scanner_id="home",
        host="192.0.2.10",
        rtsp_port=554,
        rtp_client_port=5004,
        control=None,
    )


class TestTheTap(unittest.TestCase):
    def setUp(self):
        self.bridge = bridge()
        self.received = []

    def test_a_listener_gets_the_raw_payload(self):
        self.bridge.add_audio_listener(self.received.append)
        self.bridge._on_rtp_packet(RtpPacket(b"\xff\xff\x00", 7, 1600))
        self.assertEqual([p.payload for p in self.received], [b"\xff\xff\x00"])

    def test_a_listener_gets_the_senders_timing_with_it(self):
        # Without these the only clock a tap has is when the datagram turned
        # up, which is the drift audio_tap.RtpClock exists to end.
        self.bridge.add_audio_listener(self.received.append)
        self.bridge._on_rtp_packet(RtpPacket(b"\x01", 7, 1600))
        self.assertEqual((self.received[0].seq, self.received[0].timestamp), (7, 1600))

    def test_payloads_reach_every_listener(self):
        other = []
        self.bridge.add_audio_listener(self.received.append)
        self.bridge.add_audio_listener(other.append)
        self.bridge._on_rtp_packet(RtpPacket(b"\x01"))
        self.assertEqual([p.payload for p in self.received], [b"\x01"])
        self.assertEqual([p.payload for p in other], [b"\x01"])

    def test_a_removed_listener_stops_receiving(self):
        self.bridge.add_audio_listener(self.received.append)
        self.bridge._on_rtp_packet(RtpPacket(b"\x01"))
        self.bridge.remove_audio_listener(self.received.append)
        self.bridge._on_rtp_packet(RtpPacket(b"\x02"))
        self.assertEqual([p.payload for p in self.received], [b"\x01"])

    def test_removing_a_listener_that_was_never_added_is_not_an_error(self):
        # Detach runs on paths that can't always know whether attach ran --
        # a scanner removed from settings before its tap was ever wired up.
        self.bridge.remove_audio_listener(self.received.append)

    def test_a_failing_listener_does_not_reach_the_rtp_path(self):
        # The whole point. An exception escaping here propagates into
        # _RtpProtocol.datagram_received and takes down the audio the
        # browser is playing -- a transcription bug must never do that.
        def explode(packet):
            raise RuntimeError("boom")

        self.bridge.add_audio_listener(explode)
        self.bridge.add_audio_listener(self.received.append)
        with self.assertLogs("audio_bridge", level="ERROR"):
            self.bridge._on_rtp_packet(RtpPacket(b"\x01"))
        # And the listener behind the failing one still got its bytes.
        self.assertEqual([p.payload for p in self.received], [b"\x01"])

    def test_a_listener_can_be_removed_from_inside_a_callback(self):
        # A tap that detaches itself (a scanner going away mid-packet)
        # mutates the list the fan-out is walking.
        def detach(packet):
            self.bridge.remove_audio_listener(detach)
            self.received.append(packet)

        self.bridge.add_audio_listener(detach)
        self.bridge._on_rtp_packet(RtpPacket(b"\x01"))
        self.bridge._on_rtp_packet(RtpPacket(b"\x02"))
        self.assertEqual([p.payload for p in self.received], [b"\x01"])

    def test_the_tap_works_with_no_ffmpeg_running(self):
        # Listeners are fed whether or not a session is up, so a tap
        # attached before the first RTSP connect isn't silently dead.
        self.assertIsNone(self.bridge._ffmpeg)
        self.bridge.add_audio_listener(self.received.append)
        self.bridge._on_rtp_packet(RtpPacket(b"\x01"))
        self.assertEqual([p.payload for p in self.received], [b"\x01"])


class TestTheResetSentinel(unittest.TestCase):
    def test_a_real_payload_is_never_empty(self):
        # What makes AUDIO_RESET readable as a sentinel. _RtpProtocol only
        # forwards datagrams *longer* than the header, so a 12-byte runt --
        # which would otherwise slice to b"" -- is dropped, not forwarded.
        delivered = []
        protocol = audio_bridge._RtpProtocol(delivered.append)

        protocol.datagram_received(rtp(b""), ("192.0.2.10", 5004))
        protocol.datagram_received(b"\x00" * (RTP_HEADER_LEN - 1), ("192.0.2.10", 5004))
        self.assertEqual(delivered, [])

        protocol.datagram_received(rtp(b"\xff"), ("192.0.2.10", 5004))
        self.assertEqual([p.payload for p in delivered], [b"\xff"])
        self.assertNotEqual(delivered[0], AUDIO_RESET)


class TestReadingTheHeader(unittest.TestCase):
    """Where the payload starts, and what the sender said about it."""

    def test_the_plain_case_is_twelve_bytes_of_header(self):
        packet = parse_rtp(rtp(b"\xff\x7f", seq=513, timestamp=64000))
        self.assertEqual((packet.payload, packet.seq, packet.timestamp),
                         (b"\xff\x7f", 513, 64000))

    def test_a_csrc_list_moves_the_payload_along(self):
        # Four bytes per contributing source, counted in the low nibble.
        packet = parse_rtp(rtp(b"\xaa" * 4, flags=0x82)[:RTP_HEADER_LEN]
                           + b"\x00" * 8 + b"\xaa" * 4)
        self.assertEqual(packet.payload, b"\xaa" * 4)

    def test_a_header_extension_moves_it_further(self):
        # The extension is a 4-byte preamble whose second half counts the
        # 32-bit words that follow.
        data = (rtp(b"", flags=0x90)[:RTP_HEADER_LEN]
                + b"\xbe\xde\x00\x02" + b"\x00" * 8 + b"\x55\x55")
        self.assertEqual(parse_rtp(data).payload, b"\x55\x55")

    def test_padding_is_trimmed_off_the_end(self):
        # The last byte counts the padding, itself included.
        self.assertEqual(parse_rtp(rtp(b"\x11\x22\x00\x03", flags=0xA0)).payload,
                         b"\x11")

    def test_a_datagram_with_no_payload_left_is_dropped(self):
        # What keeps AUDIO_RESET readable: nothing empty is ever forwarded.
        self.assertIsNone(parse_rtp(rtp(b"")))
        self.assertIsNone(parse_rtp(rtp(b"\x03", flags=0xA0)))  # all padding
        self.assertIsNone(parse_rtp(b"\x80\x00\x00"))
        self.assertIsNone(parse_rtp(rtp(b"", flags=0x90) + b"\xbe\xde\x00\x09"))


if __name__ == "__main__":
    unittest.main()
