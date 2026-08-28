"""Tests for reception.py -- flattening a GSI status, and classifying the mode.

The extraction is worth testing rather than eyeballing because *which* GSI
element a field lives under depends on the scan mode, and getting that
lookup order wrong produces a plausible-looking snapshot with the wrong
values in it rather than an obvious failure. The mode classifier is worth
testing because everything downstream of it (history chips, "do something on
digital traffic") is only as good as its precedence rules.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import reception  # noqa: E402

# The conventional-scan shape confirmed against real hardware -- the same
# capture tests/fixtures.py carries as raw bytes, already parsed.
CONVENTIONAL = {
    "mode": "Scan Mode",
    "v_screen": "conventional_scan",
    "System": {
        "Name": "Family Radio Service (FRS) - USA", "SystemType": "Conventional",
        "Index": "20214", "Avoid": "Off",
    },
    "Department": {"Name": "Family Radio Service (FRS)", "Index": "20217", "Avoid": "Off"},
    "ConvFrequency": {
        "Name": "Channel 3", "Freq": " 462.612500MHz", "Mod": "NFM", "SvcType": "Other",
        "SAS": "All", "SAD": "None", "TGID": "TGID None", "U_Id": "UID None",
        # The list index AVD names its target by, and the state it reports
        # back once something has been avoided (see reception.channel_target).
        "Index": "20251", "Avoid": "Off",
    },
    "Property": {"VOL": "4", "SQL": "4", "Sig": "0", "P25Status": "None", "Rssi": "-999"},
}

TRUNKED_P25 = {
    "mode": "Scan Mode",
    "System": {"Name": "Countywide P25", "SystemType": "P25 Standard"},
    "Department": {"Name": "Fire"},
    "Site": {"Name": "North Site", "Mod": "NFM"},
    "SiteFrequency": {"Freq": " 851.012500MHz", "SAD": "NAC 261h"},
    "TGID": {"Name": "Fire Dispatch", "TGID": "1001", "U_Id": "4021", "Index": "31007"},
    "Property": {"Sig": "4", "P25Status": "P25", "Rssi": "-72"},
}


class TestExtract(unittest.TestCase):
    def test_conventional_reads_the_frequency_element(self):
        snapshot = reception.extract({"gsi": CONVENTIONAL})
        self.assertEqual(snapshot["channel"], "Channel 3")
        self.assertEqual(snapshot["frequency"], 462.6125)
        self.assertEqual(snapshot["mod"], "NFM")
        self.assertEqual(snapshot["system"], "Family Radio Service (FRS) - USA")
        self.assertEqual(snapshot["department"], "Family Radio Service (FRS)")

    def test_trunked_reads_the_site_and_talkgroup_elements(self):
        # The whole point of the mode-dependent lookup: none of these live
        # under ConvFrequency, which is absent entirely here.
        snapshot = reception.extract({"gsi": TRUNKED_P25})
        self.assertEqual(snapshot["channel"], "Fire Dispatch")
        self.assertEqual(snapshot["frequency"], 851.0125)
        self.assertEqual(snapshot["tgid"], "1001")
        self.assertEqual(snapshot["unit_id"], "4021")
        self.assertEqual(snapshot["site"], "North Site")
        self.assertEqual(snapshot["sub_audio"], "NAC 261h")

    def test_the_scanners_spellings_of_nothing_are_treated_as_absent(self):
        # "TGID None"/"UID None"/"None" are how this scanner writes an empty
        # field. A rule matching unit_id ~ "None" must not fire on every
        # silent poll, and the history must not show "TGID None" as a chip.
        snapshot = reception.extract({"gsi": CONVENTIONAL})
        self.assertIsNone(snapshot["tgid"])
        self.assertIsNone(snapshot["unit_id"])
        self.assertIsNone(snapshot["p25_status"])

    def test_sad_is_preferred_over_sas(self):
        # SAD is what was actually decoded; SAS is only what the channel is
        # set to look for.
        gsi = {"ConvFrequency": {"SAD": "127.3Hz", "SAS": "All"}}
        self.assertEqual(reception.extract({"gsi": gsi})["sub_audio"], "127.3Hz")

    def test_the_rssi_sentinel_means_not_receiving(self):
        snapshot = reception.extract({"gsi": CONVENTIONAL})
        self.assertFalse(snapshot["receiving"])
        self.assertIsNone(snapshot["rssi"])

    def test_a_real_rssi_means_receiving(self):
        snapshot = reception.extract({"gsi": TRUNKED_P25})
        self.assertTrue(snapshot["receiving"])
        self.assertEqual(snapshot["rssi"], -72.0)

    def test_signal_stands_in_when_there_is_no_rssi(self):
        # The mode matrix allows Property without an Rssi attribute; the bar
        # graph is the fallback rather than "assume idle".
        self.assertTrue(reception.extract({"gsi": {"Property": {"Sig": "3"}}})["receiving"])
        self.assertFalse(reception.extract({"gsi": {"Property": {"Sig": "0"}}})["receiving"])

    def test_a_status_with_no_gsi_yet_is_not_an_error(self):
        snapshot = reception.extract({"lines": [{"text": "Scanning...", "mode": " "}]})
        self.assertFalse(snapshot["receiving"])
        self.assertIsNone(snapshot["channel"])

    def test_identity_ignores_the_unit_id(self):
        # A trunked conversation keeps the same talkgroup while individual
        # radios key up in turn. Including the unit would shred one
        # conversation into a row per transmission.
        first = reception.extract({"gsi": TRUNKED_P25})
        other_unit = {**TRUNKED_P25, "TGID": {**TRUNKED_P25["TGID"], "U_Id": "4099"}}
        self.assertEqual(first["identity"], reception.extract({"gsi": other_unit})["identity"])

    def test_identity_changes_with_the_talkgroup(self):
        first = reception.extract({"gsi": TRUNKED_P25})
        other_tg = {**TRUNKED_P25, "TGID": {**TRUNKED_P25["TGID"], "TGID": "1002"}}
        self.assertNotEqual(first["identity"], reception.extract({"gsi": other_tg})["identity"])


class TestClassifyMode(unittest.TestCase):
    def test_conventional_analog(self):
        self.assertEqual(reception.classify_mode(CONVENTIONAL), "analog")

    def test_a_p25_system_type(self):
        self.assertEqual(reception.classify_mode(TRUNKED_P25), "p25")

    def test_mototrbo_resolves_to_dmr_not_motorola(self):
        # Ordering matters: a substring match on "motorola" would otherwise
        # be reachable for names containing "Moto".
        gsi = {"System": {"SystemType": "MotoTRBO Capacity Plus"}}
        self.assertEqual(reception.classify_mode(gsi), "dmr")

    def test_nexedge_resolves_to_nxdn(self):
        self.assertEqual(reception.classify_mode({"System": {"SystemType": "NEXEDGE 9600"}}), "nxdn")

    def test_an_observed_p25_decode_beats_the_programmed_system_type(self):
        # P25Status is the only field that reports what is actually being
        # decoded right now, so it outranks how the system was programmed.
        gsi = {"System": {"SystemType": "Conventional"}, "Property": {"P25Status": "P25"}}
        self.assertEqual(reception.classify_mode(gsi), "p25")

    def test_a_conventional_system_with_a_non_analog_modulation_is_unknown(self):
        # Not "analog": a digital conventional channel is a real thing this
        # classifier can't identify, and claiming it is analog would be
        # worse than admitting it doesn't know.
        gsi = {"System": {"SystemType": "Conventional"}, "ConvFrequency": {"Mod": "AUTO"}}
        self.assertEqual(reception.classify_mode(gsi), "unknown")

    def test_an_unrecognized_system_type_is_unknown(self):
        gsi = {"System": {"SystemType": "Something Uniden Ships Later"}}
        self.assertEqual(reception.classify_mode(gsi), "unknown")

    def test_no_data_at_all_is_unknown(self):
        self.assertEqual(reception.classify_mode({}), "unknown")

    def test_every_classification_is_a_declared_mode(self):
        # The UI's filter list and config_store's validation both come from
        # MODES; a slug that isn't in it would be unfilterable.
        for system_type, _ in reception._SYSTEM_TYPE_MODES:
            mode = reception.classify_mode({"System": {"SystemType": system_type}})
            self.assertIn(mode, reception.MODES, system_type)


class TestAvoidTarget(unittest.TestCase):
    """What a permanent avoid gets pointed at.

    Same mode-dependent lookup as `extract`, and wrong in the same quiet way
    if it drifts: an index picked out of the wrong element is still a valid
    index, so the scanner would answer AVD,OK having avoided something the
    operator never chose.
    """

    def test_conventional_targets_the_frequency_entry(self):
        target = reception.channel_target(CONVENTIONAL)
        self.assertEqual(target["element"], "ConvFrequency")
        self.assertEqual(target["index"], "20251")
        self.assertEqual(target["name"], "Channel 3")
        self.assertEqual(target["tkw"], "CFREQ")
        self.assertFalse(target["avoided"])

    def test_trunked_targets_the_talkgroup(self):
        target = reception.channel_target(TRUNKED_P25)
        self.assertEqual(target["element"], "TGID")
        self.assertEqual(target["index"], "31007")
        self.assertEqual(target["tkw"], "TGID")

    def test_conventional_wins_when_both_elements_are_present(self):
        # The mode matrix shouldn't produce this, but if it ever does, the
        # frequency entry is the one the display calls the channel.
        both = {**CONVENTIONAL, "TGID": {"Name": "Fire Dispatch", "Index": "31007"}}
        self.assertEqual(reception.channel_target(both)["element"], "ConvFrequency")

    def test_reports_an_entry_the_scanner_already_avoids(self):
        gsi = {"ConvFrequency": {"Name": "Channel 3", "Index": "20251", "Avoid": "On"}}
        self.assertTrue(reception.channel_target(gsi)["avoided"])

    def test_no_target_without_an_index(self):
        # A menu, the boot screen, a quick search: nothing on screen is an
        # entry in a list, so there is no index for AVD to name.
        self.assertIsNone(reception.channel_target({}))
        self.assertIsNone(reception.channel_target({"ConvFrequency": {"Name": "Channel 3"}}))
        self.assertIsNone(reception.channel_target({"ConvFrequency": {"Index": "  "}}))

    def test_an_entry_with_no_name_still_has_an_index(self):
        # Database entries can come through unnamed; the index is the part
        # AVD actually needs, so it is not a reason to refuse.
        target = reception.channel_target({"ConvFrequency": {"Index": "20251", "Name": "None"}})
        self.assertEqual(target["index"], "20251")
        self.assertIsNone(target["name"])



class TestNothingYet(unittest.TestCase):
    """The scanner's several spellings of "no value".

    A labelled one -- "TGID: ---" -- is the dangerous kind, because it is a
    string rather than an absence: a call that opens holding it compares
    unequal to its own talkgroup when that arrives a poll later, and the call
    splits in two. That is the bug 0.7.47 fixed for blanks and this one
    reintroduced by not recognising the placeholder as blank.
    """

    def test_a_labelled_placeholder_is_no_value(self):
        for spelling in ("TGID: ---", "TGID:---", "UID: ---", "---", "None", ""):
            with self.subTest(spelling=spelling):
                self.assertIsNone(reception._text(spelling))

    def test_a_real_value_with_a_label_survives(self):
        self.assertEqual(reception._text("TGID:10852"), "TGID:10852")
        self.assertEqual(reception._text("Dispatch"), "Dispatch")
        self.assertEqual(reception._text("P25 NAC:261h"), "P25 NAC:261h")


class TestReadingTheScreen(unittest.TestCase):
    """The identity the screen states outright.

    GSI is polled every three seconds and the display four times a second, so
    for up to three seconds after a transmission starts GSI is still
    describing the previous one. The screen is not: captured from the real
    scanner mid-call, the talkgroup line reads `TGID:10852` against GSI's
    `TGID="TGID:10852"`, character for character. That sameness is the point
    -- two spellings of one talkgroup would read as the talkgroup changing.
    """

    def screen(self, *lines):
        return {"lines": [{"text": text} for text in lines]}

    def test_the_talkgroup_is_read_exactly_as_gsi_spells_it(self):
        found = reception.from_display(self.screen(
            "Indiana University Campus Po", "TGID:10852",
            "Law Dispatch    P25 NAC:261h"))
        self.assertEqual(found["tgid"], "TGID:10852")

    def test_the_frequency_and_unit_come_off_it_too(self):
        found = reception.from_display(self.screen(
            "Sys ID: ---      857.937500MHz", "UID:9340180     RSSI: -82dBm"))
        self.assertAlmostEqual(found["frequency"], 857.9375)
        self.assertEqual(found["unit_id"], "9340180")

    def test_a_row_of_dashes_is_the_screen_saying_nothing_yet(self):
        self.assertEqual(reception.from_display(self.screen(
            "TGID:---", "UID: ---        RSSI: ---     ")), {})

    def test_a_conventional_screen_gives_the_frequency_and_no_talkgroup(self):
        found = reception.from_display(self.screen("Fire Dispatch   155.010000MHz"))
        self.assertEqual(found, {"frequency": 155.01})

    def test_a_screen_that_says_none_of_it_is_not_an_error(self):
        # A menu, a popup, the weather screen: nothing to read, fall back to
        # GSI, which is what happened before any of this existed.
        self.assertEqual(reception.from_display(self.screen("Menu", "Settings")), {})
        self.assertEqual(reception.from_display({}), {})

    def test_the_names_beside_them_are_deliberately_not_read(self):
        # They are cut to the display width, so a name read here would be a
        # different string from the one GSI gives for the same call.
        found = reception.from_display(self.screen(
            "Indiana University Campus Po", "Colleges & Universities", "TGID:10852"))
        self.assertEqual(set(found), {"tgid"})

if __name__ == "__main__":
    unittest.main()
