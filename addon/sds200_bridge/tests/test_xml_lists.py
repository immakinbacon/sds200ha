"""Tests for xml_lists.reassemble()/element_to_dicts()/gsi_to_dict(), built
from real SDS200 captures -- see fixtures.py for provenance notes.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
from xml_lists import element_to_dicts, gsi_to_dict, reassemble  # noqa: E402


async def _queue_of(*packets: bytes) -> "asyncio.Queue[bytes]":
    queue: "asyncio.Queue[bytes]" = asyncio.Queue()
    for packet in packets:
        queue.put_nowait(packet)
    return queue


class TestReassembleGsi(unittest.IsolatedAsyncioTestCase):
    """GSI/PSI shape: single packet, complete document, no Footer at all."""

    async def test_parses_single_packet(self):
        queue = await _queue_of(fixtures.GSI_RESPONSE_SCAN_MODE)
        root = await reassemble(queue, expected_prefix="GSI,")
        self.assertEqual(root.tag, "ScannerInfo")

    async def test_root_attributes_preserved(self):
        # Regression: reassemble() used to keep only the root tag *name*,
        # silently dropping Mode/V_Screen when synthesizing the wrapper.
        queue = await _queue_of(fixtures.GSI_RESPONSE_SCAN_MODE)
        root = await reassemble(queue, expected_prefix="GSI,")
        self.assertEqual(root.attrib.get("Mode"), "Scan Mode")
        self.assertEqual(root.attrib.get("V_Screen"), "conventional_scan")

    async def test_gsi_to_dict_flattens_children(self):
        queue = await _queue_of(fixtures.GSI_RESPONSE_SCAN_MODE)
        root = await reassemble(queue, expected_prefix="GSI,")
        result = gsi_to_dict(root)
        self.assertEqual(result["mode"], "Scan Mode")
        self.assertEqual(result["v_screen"], "conventional_scan")
        self.assertEqual(result["System"]["Name"], "Family Radio Service (FRS) - USA")
        self.assertEqual(result["Property"]["VOL"], "4")
        self.assertEqual(result["view_description"]["OverWrite"]["Text"], "Scanning...")


class TestReassembleGltSinglePacket(unittest.IsolatedAsyncioTestCase):
    """GLT single-packet shape: complete document *and* an embedded Footer
    before the closing tag -- distinct from both GSI (no footer) and the
    multi-packet case below.
    """

    async def test_no_fake_footer_entry(self):
        queue = await _queue_of(fixtures.GLT_FL_RESPONSE)
        root = await reassemble(queue, expected_prefix="GLT,")
        entries = element_to_dicts(root)
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(e["_tag"] != "Footer" for e in entries))

    async def test_entry_contents(self):
        queue = await _queue_of(fixtures.GLT_FL_RESPONSE)
        root = await reassemble(queue, expected_prefix="GLT,")
        entries = element_to_dicts(root)
        names = [e["Name"] for e in entries]
        self.assertEqual(names, ["Full Database", "Search with Scan", "default"])


class TestReassembleGltMultiPacket(unittest.IsolatedAsyncioTestCase):
    """Real multi-packet capture (trimmed) from a genuine 40+ packet /
    359-system GLT,SYS exchange, plus one synthetic final (EOT=1) packet
    to complete the sequence for a full round trip.
    """

    async def test_all_packets_combine_no_footer_leak(self):
        queue = await _queue_of(
            fixtures.GLT_SYS_PACKET_1,
            fixtures.GLT_SYS_PACKET_2,
            fixtures.GLT_SYS_PACKET_3,
            fixtures.GLT_SYS_PACKET_4_FINAL_SYNTHETIC,
        )
        root = await reassemble(queue, expected_prefix="GLT,")
        entries = element_to_dicts(root)
        names = [e["Name"] for e in entries]
        self.assertEqual(names, ["Adams", "Allen", "Bartholomew", "Clark", "Clay", "Dubois", "Last County"])
        self.assertTrue(all(e["_tag"] != "Footer" for e in entries))

    async def test_times_out_if_eot_never_arrives(self):
        # All three real packets say EOT="0" -- if the final EOT=1 packet
        # never shows up, reassembly should time out, not silently return
        # a truncated-but-"successful" result.
        queue = await _queue_of(
            fixtures.GLT_SYS_PACKET_1, fixtures.GLT_SYS_PACKET_2, fixtures.GLT_SYS_PACKET_3
        )
        with self.assertRaises(TimeoutError):
            await reassemble(queue, expected_prefix="GLT,", timeout=0.3)


class TestReassemblePrefixFiltering(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for the response-desync bug: a stray unsolicited
    push (e.g. GSI) landing in front of the real reply must be discarded,
    not mistaken for the response.
    """

    async def test_ignores_stray_packet_before_real_response(self):
        queue = await _queue_of(fixtures.STRAY_GSI_PUSH, fixtures.GLT_FL_RESPONSE)
        root = await reassemble(queue, expected_prefix="GLT,")
        entries = element_to_dicts(root)
        self.assertEqual(len(entries), 3)


if __name__ == "__main__":
    unittest.main()
