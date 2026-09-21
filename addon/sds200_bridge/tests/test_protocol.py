"""Tests for protocol.py's parsing logic and response-matching behavior,
built from real SDS200 captures. Run with:

    PYTHONPATH=<path-to-aiohttp-etc> python3 -m unittest discover -s addon/sds200_bridge/tests

(aiohttp itself isn't needed for this file -- protocol.py's parsing helpers
are pure stdlib -- but send_command's queue plumbing uses asyncio, hence
IsolatedAsyncioTestCase.)
"""

from __future__ import annotations

import asyncio
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
from protocol import (  # noqa: E402
    ScannerConnection,
    _strip_control_chars,
    parse_gst,
    parse_sts,
    split_fields,
)


class TestSplitFields(unittest.TestCase):
    def test_basic_split(self):
        self.assertEqual(split_fields("a,b,c"), ["a", "b", "c"])

    def test_tab_unescapes_to_comma(self):
        # Protocol escapes literal commas in a field as \t (see GLT,MSV docs).
        self.assertEqual(split_fields("a,b\tc,d"), ["a", "b,c", "d"])


class TestStripControlChars(unittest.TestCase):
    def test_strips_scroll_codes(self):
        # \x1a \x1b observed trailing a channel name mid marquee-scroll.
        self.assertEqual(_strip_control_chars("F0:0---------  \x1a\x1b"), "F0:0---------  ")

    def test_leaves_normal_text_alone(self):
        self.assertEqual(_strip_control_chars("Coast Guard - USA"), "Coast Guard - USA")

    def test_keeps_tab(self):
        self.assertEqual(_strip_control_chars("a\tb"), "a\tb")


class TestParseSts(unittest.TestCase):
    def test_real_capture_parses(self):
        result = parse_sts(fixtures.STS_RESPONSE.decode("ascii"))
        self.assertIsNotNone(result)
        self.assertEqual(len(result["lines"]), len(result["dsp_form"]))
        self.assertIn("Coast Guard - USA", result["lines"][4]["text"])

    def test_non_sts_returns_none(self):
        self.assertIsNone(parse_sts("VOL,4"))

    def test_control_chars_stripped_from_line_text(self):
        result = parse_sts(fixtures.STS_RESPONSE_WITH_CONTROL_CHARS.decode("ascii"))
        all_text = "".join(line["text"] for line in result["lines"])
        self.assertNotIn("\x1a", all_text)
        self.assertNotIn("\x1b", all_text)
        self.assertNotIn("\x06", all_text)
        self.assertNotIn("\x07", all_text)
        # and the surrounding real text should have survived
        self.assertTrue(any("Grissom Air Reserve Base" in line["text"] for line in result["lines"]))

    def test_undecodable_high_bytes_stripped_not_shown_as_replacement_char(self):
        # protocol.py decodes with errors="replace" (custom LCD glyph bytes
        # aren't valid ASCII); those become U+FFFD and must be stripped too,
        # not just the low control-byte range.
        decoded = fixtures.STS_RESPONSE_WITH_HIGH_BYTES.decode("ascii", errors="replace")
        result = parse_sts(decoded)
        all_text = "".join(line["text"] for line in result["lines"])
        self.assertNotIn("�", all_text)
        # the real timestamp text around it should have survived
        self.assertTrue(any("Jul21 22:46" in line["text"] for line in result["lines"]))


class TestSoftKeyLabels(unittest.TestCase):
    """The labels above the three soft keys, which weather.py reads to find
    the way back to scanning. Identified by their asterisk column mask, not
    by position: DSP_FORM counts them in one real capture and not in another
    (17 digits / 18 lines vs 17 / 17), so position alone would find them in
    one and the reserved fields in the other."""

    def test_found_whichever_side_of_the_dsp_form_count_they_land_on(self):
        for name in ("STS_RESPONSE", "STS_RESPONSE_WITH_CONTROL_CHARS",
                     "STS_RESPONSE_WITH_HIGH_BYTES"):
            with self.subTest(capture=name):
                result = parse_sts(getattr(fixtures, name).decode("ascii", errors="replace"))
                self.assertEqual(result["soft_keys"], ["SYSTEM", "DEPT", "CHANNEL"])

    def test_glyphs_in_the_label_row_do_not_shift_the_columns(self):
        # The weather-alert screen, captured live: " to Scan" in the first
        # column, ten custom-glyph bytes in the second, "RESUME" in the third.
        # A label is the slice of the row under its run of asterisks, so
        # stripping those glyphs before slicing shortens the row from 30
        # characters to 20 and reports RESUME as the *middle* key -- which is
        # the one the add-on would then press to get back to scanning.
        result = parse_sts(
            fixtures.STS_RESPONSE_WEATHER_ALERT.decode("ascii", errors="replace")
        )
        self.assertEqual(result["soft_keys"], ["to Scan", "", "RESUME"])

    def test_a_response_without_a_mask_reports_no_labels(self):
        # Rather than splitting whatever field happened to be there into
        # thirds and inventing three labels out of a reserved value.
        self.assertEqual(parse_sts("STS,11,one,,two,,0,1")["soft_keys"], [])


class TestParseGst(unittest.TestCase):
    def test_real_capture_parses_trailing_fields(self):
        result = parse_gst(fixtures.GST_RESPONSE.decode("ascii"))
        self.assertIsNotNone(result)
        self.assertEqual(len(result["lines"]), len(result["dsp_form"]))
        for key in (
            "mute", "led1", "led2", "wf_mode", "freq", "mod",
            "mf_pos", "cf", "lower", "upper", "color_mode", "fft_size",
        ):
            self.assertIn(key, result)

    def test_the_soft_key_labels_survive_the_named_fields(self):
        # The control tab renders these, and weather.py decides which key to
        # press from them. Computing them for STS only took them off both the
        # moment the fast poll moved to GST.
        sts = parse_sts(fixtures.STS_RESPONSE.decode("ascii"))
        gst = parse_gst(fixtures.GST_RESPONSE.decode("ascii"))
        self.assertIn("soft_keys", gst)
        self.assertEqual(len(sts["soft_keys"]), 3)

    def test_the_named_fields_are_read_from_the_end_of_the_response(self):
        # Counted forward from the last display line they land three fields
        # out on any screen where the soft-key row falls outside DSP_FORM's
        # count -- and then `mute`, which decides whether the scanner is
        # playing anything, reads as a row of asterisks.
        result = parse_gst(fixtures.GST_RESPONSE.decode("ascii"))
        self.assertEqual(result["mute"], "1")
        self.assertEqual(result["led1"], "OFF")

    def test_non_gst_returns_none(self):
        self.assertIsNone(parse_gst(fixtures.STS_RESPONSE.decode("ascii")))


class TestTheFastPollAsksForGst(unittest.IsolatedAsyncioTestCase):
    """The display mirror is polled with GST rather than STS.

    GST returns everything STS does and adds `mute`, which is the only field
    that distinguishes "the site has RF" from "this scanner is playing it".
    On a trunked system those are wildly different -- measured on real
    hardware as eight stretches of RF in one minute against a single unmute
    -- and everything that means "we heard this" is built on the second.
    """

    def _connect(self, response: bytes) -> ScannerConnection:
        conn = ScannerConnection(scanner_id="test", name="test", host="127.0.0.1")
        conn._transport = _FakeTransport(conn._queue, [response])
        return conn

    async def test_it_sends_gst(self):
        conn = self._connect(fixtures.GST_RESPONSE)
        await conn.refresh_display()
        self.assertEqual(conn._transport.sent, [b"GST\r"])

    async def test_the_mute_flag_reaches_the_listeners(self):
        conn = self._connect(fixtures.GST_RESPONSE)
        seen = []
        conn.add_status_listener(lambda scanner_id, status: seen.append(status))
        await conn.refresh_display()
        self.assertIn("mute", seen[-1])

    async def test_a_reply_that_is_not_a_display_mirror_changes_nothing(self):
        conn = self._connect(b"VOL,3\r")
        seen = []
        conn.add_status_listener(lambda scanner_id, status: seen.append(status))
        with self.assertRaises(Exception):
            await conn.refresh_display()
        self.assertEqual(seen, [])


class TestRefreshingTheStructuredData(unittest.IsolatedAsyncioTestCase):
    """`refresh_gsi` actually running.

    Nothing exercised its body, and it spent three versions raising a
    NameError on every poll -- caught by the loop, logged, and otherwise
    invisible, while the whole add-on ran with no system, department,
    channel, talkgroup or frequency reaching anything at all. A test that
    calls it is worth more than any amount of care in reading it.
    """

    def _connect(self):
        conn = ScannerConnection(scanner_id="test", name="test", host="127.0.0.1")

        async def fake_xml(command, timeout=2.0):
            return ET.fromstring(
                '<ScannerInfo Mode="Scan"><System Name="Countywide" /></ScannerInfo>'
            )

        conn.send_xml_command = fake_xml
        return conn

    async def test_it_stores_what_it_read(self):
        conn = self._connect()
        await conn.refresh_gsi()
        self.assertEqual(conn.last_status["gsi"]["System"]["Name"], "Countywide")

    async def test_it_stamps_when_the_reading_arrived(self):
        # Which is what tells a listener a display poll's copy of this block
        # is three seconds old rather than current.
        conn = self._connect()
        await conn.refresh_gsi()
        first = conn.last_status["gsi_at"]
        self.assertIsInstance(first, float)
        await conn.refresh_gsi()
        self.assertGreaterEqual(conn.last_status["gsi_at"], first)

    async def test_the_listeners_hear_about_it(self):
        conn = self._connect()
        seen = []
        conn.add_status_listener(lambda scanner_id, status: seen.append(status))
        await conn.refresh_gsi()
        self.assertEqual(len(seen), 1)
        self.assertIn("gsi", seen[0])


class _FakeTransport:
    """Minimal stand-in for asyncio.DatagramTransport. send_command() drains
    the queue *before* calling sendto() (discarding stale leftovers from a
    prior timed-out request -- real, intentional behavior), so canned
    "received" datagrams must be enqueued *by* sendto(), simulating the
    network round trip, not pre-loaded before the call.
    """

    def __init__(self, queue: "asyncio.Queue[bytes]", responses: list[bytes]):
        self._queue = queue
        self._responses = responses
        self.sent: list[bytes] = []

    def sendto(self, data: bytes) -> None:
        self.sent.append(data)
        for response in self._responses:
            self._queue.put_nowait(response)


class TestSendCommandMatching(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for the response-desync bug: the scanner
    interleaves unsolicited/stale packets with real replies, so send_command
    must match by prefix, not just take "the next datagram".
    """

    def _make_connection(self, responses: list[bytes]) -> ScannerConnection:
        conn = ScannerConnection(scanner_id="test", name="test", host="127.0.0.1")
        conn._transport = _FakeTransport(conn._queue, responses)
        return conn

    async def test_matches_expected_prefix_ignoring_stray_packet(self):
        # A stray unsolicited GSI push "arrives" first, then the real VOL reply.
        conn = self._make_connection([fixtures.STRAY_GSI_PUSH, b"VOL,3\r"])
        result = await conn.send_command("VOL", retries=0, timeout=1.0)
        self.assertEqual(result, "VOL,3")

    async def test_exact_bare_command_echo_matches(self):
        conn = self._make_connection([b"KAL\r"])
        result = await conn.send_command("KAL", retries=0, timeout=1.0)
        self.assertEqual(result, "KAL")

    async def test_times_out_if_nothing_matches(self):
        conn = self._make_connection([fixtures.STRAY_GSI_PUSH])  # never a VOL match
        with self.assertRaises(Exception):
            await conn.send_command("VOL", retries=0, timeout=0.2)


if __name__ == "__main__":
    unittest.main()
