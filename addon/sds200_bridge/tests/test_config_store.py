"""Tests for the add-on's own settings file (config_store.py).

Two things here are worth testing rather than eyeballing, because getting
either wrong loses or leaks real configuration:

* the one-time migration off Supervisor's /data/options.json. It runs
  exactly once per install, on the first start after upgrading, and if it
  silently produces an empty config the user's scanners are gone with no
  obvious way back.
* the password sentinel. The UI is never sent a stored router password, so
  a save has to put it back -- and it has to put back the *right* one. An
  off-by-one there hands one scanner another scanner's router credentials.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config_store  # noqa: E402
from config_store import PASSWORD_SENTINEL, ConfigStore  # noqa: E402


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.config_path = self.dir / "config.json"
        self.options_path = self.dir / "options.json"

    def store(self) -> ConfigStore:
        return ConfigStore(str(self.config_path), str(self.options_path))

    def write_options(self, options: dict) -> None:
        self.options_path.write_text(json.dumps(options))


class TestNormalize(StoreTestCase):
    def test_fills_in_defaults_for_a_bare_scanner(self):
        scanner = config_store.normalize_scanner({"name": "Home", "host": "192.0.2.232"})
        self.assertEqual(scanner["control_port"], 50536)
        self.assertEqual(scanner["rtsp_port"], 554)
        self.assertIs(scanner["poe_reset_use_ssl"], True)
        self.assertIs(scanner["auto_reboot_on_audio_failure"], False)
        self.assertEqual(scanner["poe_reset_password"], "")

    def test_ports_arrive_from_a_form_as_strings(self):
        scanner = config_store.normalize_scanner(
            {"name": "Home", "host": "h", "control_port": "50537", "rtsp_port": "8554"}
        )
        self.assertEqual(scanner["control_port"], 50537)
        self.assertEqual(scanner["rtsp_port"], 8554)

    def test_blank_or_out_of_range_ports_fall_back_to_the_defaults(self):
        # A cleared number input posts "", and the working default is a far
        # better answer than 0 or a crash.
        for value in ("", "   ", "0", "70000", "abc", None):
            with self.subTest(value=value):
                scanner = config_store.normalize_scanner(
                    {"name": "Home", "host": "h", "control_port": value}
                )
                self.assertEqual(scanner["control_port"], 50536)

    def test_weather_return_to_scan_is_off_by_default(self):
        # An upgrade must not start pressing keys on a scanner nobody asked
        # it to touch.
        scanner = config_store.normalize_scanner({"name": "Home", "host": "h"})
        self.assertEqual(scanner["wx_return_to_scan_s"], 0)
        self.assertEqual(scanner["wx_return_to_scan_key"], "")

    def test_a_fallback_key_the_scanner_does_not_have_is_dropped(self):
        scanner = config_store.normalize_scanner(
            {"name": "Home", "host": "h", "wx_return_to_scan_key": "turbo"}
        )
        self.assertEqual(scanner["wx_return_to_scan_key"], "")
        kept = config_store.normalize_scanner(
            {"name": "Home", "host": "h", "wx_return_to_scan_key": "soft3"}
        )
        self.assertEqual(kept["wx_return_to_scan_key"], "soft3")

    def test_the_weather_wait_arrives_from_a_form_as_a_string_and_is_bounded(self):
        for value, expected in (("60", 60), ("", 0), ("-5", 0), ("999999", 3600)):
            with self.subTest(value=value):
                scanner = config_store.normalize_scanner(
                    {"name": "Home", "host": "h", "wx_return_to_scan_s": value}
                )
                self.assertEqual(scanner["wx_return_to_scan_s"], expected)

    def test_unknown_log_level_falls_back_to_info(self):
        self.assertEqual(config_store.normalize({"log_level": "chatty"})["log_level"], "info")
        self.assertEqual(config_store.normalize({"log_level": "DEBUG"})["log_level"], "debug")


class TestValidate(StoreTestCase):
    def validate(self, scanners, **rest):
        return config_store.validate(config_store.normalize({"scanners": scanners, **rest}))

    def test_a_good_config_is_clean(self):
        errors, warnings = self.validate([{"name": "Home", "host": "192.0.2.232"}])
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_missing_name_or_host_is_an_error(self):
        errors, _ = self.validate([{"name": "", "host": ""}])
        self.assertEqual(len(errors), 2, errors)

    def test_names_that_slugify_the_same_collide(self):
        # "Home" and "home!" both become "home", which is the id every
        # /scanners/{id} route and every HA entity is keyed on.
        errors, _ = self.validate(
            [{"name": "Home", "host": "a"}, {"name": "home!", "host": "b"}]
        )
        self.assertTrue(any("same id" in e for e in errors), errors)

    def test_a_name_with_no_alphanumerics_has_no_usable_id(self):
        errors, _ = self.validate([{"name": "---", "host": "a"}])
        self.assertTrue(any("no usable id" in e for e in errors), errors)

    def test_too_many_scanners_is_an_error(self):
        scanners = [{"name": f"s{i}", "host": "h"} for i in range(config_store.MAX_SCANNERS + 1)]
        errors, _ = self.validate(scanners)
        self.assertTrue(any("RTP audio" in e for e in errors), errors)

    def test_partial_poe_reset_warns_but_does_not_block(self):
        # The runtime already ignores it with a log line; refusing the save
        # would strand someone mid-edit.
        errors, warnings = self.validate(
            [{"name": "Home", "host": "a", "poe_reset_host": "198.51.100.1"}]
        )
        self.assertEqual(errors, [])
        self.assertTrue(any("incomplete" in w for w in warnings), warnings)

    def test_auto_recovery_without_poe_reset_warns(self):
        _errors, warnings = self.validate(
            [{"name": "Home", "host": "a", "auto_reboot_on_control_failure": True}]
        )
        self.assertTrue(any("nothing will happen" in w for w in warnings), warnings)


class TestPasswordSentinel(StoreTestCase):
    STORED = {
        "scanners": [
            {"name": "Home", "host": "a", "poe_reset_password": "home-secret"},
            {"name": "Shop", "host": "b", "poe_reset_password": "shop-secret"},
        ]
    }

    def test_redact_replaces_a_stored_password(self):
        redacted = config_store.redact(self.STORED)
        self.assertEqual(redacted["scanners"][0]["poe_reset_password"], PASSWORD_SENTINEL)

    def test_redact_leaves_an_unset_password_empty(self):
        redacted = config_store.redact({"scanners": [{"name": "Home", "host": "a"}]})
        self.assertEqual(redacted["scanners"][0]["poe_reset_password"], "")

    def test_sentinel_restores_the_stored_password(self):
        incoming = config_store.redact(self.STORED)
        restored = config_store.unredact(incoming, config_store.normalize(self.STORED))
        self.assertEqual(restored["scanners"][0]["poe_reset_password"], "home-secret")
        self.assertEqual(restored["scanners"][1]["poe_reset_password"], "shop-secret")

    def test_reordering_does_not_swap_passwords_between_scanners(self):
        # Matching by list position instead of by id would hand "Shop" the
        # home router's password here.
        incoming = config_store.redact(self.STORED)
        incoming["scanners"].reverse()
        restored = config_store.unredact(incoming, config_store.normalize(self.STORED))
        self.assertEqual(restored["scanners"][0]["name"], "Shop")
        self.assertEqual(restored["scanners"][0]["poe_reset_password"], "shop-secret")
        self.assertEqual(restored["scanners"][1]["poe_reset_password"], "home-secret")

    def test_a_typed_password_replaces_the_stored_one(self):
        incoming = config_store.redact(self.STORED)
        incoming["scanners"][0]["poe_reset_password"] = "new-secret"
        restored = config_store.unredact(incoming, config_store.normalize(self.STORED))
        self.assertEqual(restored["scanners"][0]["poe_reset_password"], "new-secret")

    def test_a_renamed_scanner_loses_its_stored_password(self):
        # Its id changes, so there is nothing to match it to. Documented
        # here as the accepted trade-off of matching by id rather than by
        # position: a rename means retyping the router password, which is
        # much better than a reorder silently leaking one.
        incoming = config_store.redact(self.STORED)
        incoming["scanners"][0]["name"] = "House"
        restored = config_store.unredact(incoming, config_store.normalize(self.STORED))
        self.assertEqual(restored["scanners"][0]["poe_reset_password"], "")


class TestLoadAndMigrate(StoreTestCase):
    def test_first_start_migrates_the_old_supervisor_options(self):
        self.write_options(
            {
                "log_level": "debug",
                "scanners": [
                    {"name": "Home", "host": "192.0.2.232", "poe_reset_use_ssl": False}
                ],
            }
        )
        config = self.store().load()
        self.assertEqual(config["log_level"], "debug")
        self.assertEqual(len(config["scanners"]), 1)
        self.assertEqual(config["scanners"][0]["host"], "192.0.2.232")
        self.assertIs(config["scanners"][0]["poe_reset_use_ssl"], False)
        self.assertTrue(self.config_path.exists(), "migration must write config.json")

    def test_options_are_only_read_once(self):
        self.write_options({"scanners": [{"name": "Home", "host": "192.0.2.232"}]})
        store = self.store()
        store.load()
        # Someone edits the (now legacy) Configuration tab afterwards, or
        # Supervisor rewrites options.json on an update. Neither may
        # resurrect the old values over what the UI has saved since.
        self.write_options({"scanners": [{"name": "Ghost", "host": "198.51.100.9"}]})
        config = store.load()
        self.assertEqual([s["name"] for s in config["scanners"]], ["Home"])

    def test_no_options_file_gives_an_empty_config(self):
        config = self.store().load()
        self.assertEqual(
            config,
            {
                "log_level": "info",
                "scanners": [],
                "history": {
                    "enabled": True,
                    "retention_days": config_store.DEFAULT_HISTORY_DAYS,
                    "max_records": config_store.DEFAULT_MAX_HISTORY_RECORDS,
                },
                # Present but unconfigured: an empty url is what "no
                # transcription" looks like, and it is the default rather
                # than something to warn about.
                "transcribe": {
                    "backend": "wyoming",
                    "url": "",
                    "model": "small.en",
                    "language": "en",
                    "prompt": "",
                    "upsample": True,
                    "silence_gap_s": 1.2,
                    "max_segment_s": 30.0,
                },
                "triggers": [],
            },
        )

    def test_unreadable_options_do_not_stop_the_addon_starting(self):
        self.options_path.write_text("{ this is not json")
        self.assertEqual(self.store().load()["scanners"], [])

    def test_a_corrupt_config_file_raises_rather_than_starting_empty(self):
        # Starting with no scanners would be indistinguishable from a fresh
        # install; failing loudly is the only way the log says what happened.
        self.config_path.write_text("{ truncated")
        with self.assertRaises(ValueError):
            self.store().load()

    def test_save_round_trips(self):
        store = self.store()
        store.save({"log_level": "warning", "scanners": [{"name": "Home", "host": "a"}]})
        config = self.store().load()
        self.assertEqual(config["log_level"], "warning")
        self.assertEqual(config["scanners"][0]["name"], "Home")

    def test_save_leaves_no_temp_files_behind(self):
        store = self.store()
        store.save({"scanners": [{"name": "Home", "host": "a"}]})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["config.json"])


class TestActionFieldTypes(unittest.TestCase):
    """An action's extra fields are service-call data, so their types matter.

    Home Assistant rejects a script field declared as a number when it
    arrives as "3", which is what these used to be coerced to. The values
    come from a text form, so the type has to be read back out of the text --
    without eating the templates, which are the other thing that field holds.
    """

    def data(self, raw: dict) -> dict:
        return config_store.normalize_trigger({"action": {"data": raw}})["action"]["data"]

    def test_a_number_stays_a_number(self):
        self.assertEqual(self.data({"priority": "3"}), {"priority": 3})
        self.assertEqual(self.data({"volume": "0.5"}), {"volume": 0.5})

    def test_a_boolean_stays_a_boolean(self):
        self.assertEqual(self.data({"urgent": "true"}), {"urgent": True})
        self.assertIs(self.data({"urgent": "false"})["urgent"], False)

    def test_a_list_stays_a_list(self):
        self.assertEqual(self.data({"tags": '["fire", "ems"]'}), {"tags": ["fire", "ems"]})

    def test_a_template_survives_as_text(self):
        # The whole point of the field. "{channel}" starts with a brace and
        # has to reach triggers.render as the text the user typed.
        self.assertEqual(self.data({"message": "{channel}"}), {"message": "{channel}"})
        self.assertEqual(
            self.data({"message": "{label} on {channel}"}), {"message": "{label} on {channel}"}
        )

    def test_plain_text_survives(self):
        self.assertEqual(self.data({"title": "Dispatch"}), {"title": "Dispatch"})

    def test_quoting_forces_text_where_a_number_would_be_read(self):
        self.assertEqual(self.data({"code": '"911"'}), {"code": "911"})

    def test_a_value_that_is_already_typed_is_left_alone(self):
        # Straight out of config.json, where JSON already carried the type.
        self.assertEqual(self.data({"priority": 3, "urgent": True}), {"priority": 3, "urgent": True})

    def test_a_nested_object_keeps_its_inner_strings_as_strings(self):
        # Only the value the user typed is maybe-JSON; a string inside a
        # parsed object is whatever JSON said it was.
        self.assertEqual(
            self.data({"payload": '{"code": "911", "n": 2}'}),
            {"payload": {"code": "911", "n": 2}},
        )

    def test_nesting_is_bounded(self):
        deep = {"a": {"b": {"c": {"d": {"e": {"f": "too far"}}}}}}
        # Parses, but the tail past the limit is dropped rather than stored.
        self.assertEqual(json.dumps(self.data({"deep": deep})).count("too far"), 0)

    def test_a_blank_value_stays_blank(self):
        self.assertEqual(self.data({"note": ""}), {"note": ""})

    def test_keys_without_a_name_are_dropped(self):
        self.assertEqual(self.data({"": "orphan", "kept": "yes"}), {"kept": "yes"})



class TestTheSilenceGapWarning(unittest.TestCase):
    """A setting that causes the symptom it gets turned down to fix.

    An ordinary pause between phrases is 0.3-0.5s, so ending a transmission
    after less than that cuts people off mid-sentence -- one transmission
    becomes two clips, two transcripts, and a worse job by the model on both
    halves. A real install was found set to 0.3s while chasing exactly that.
    A warning rather than a bound: clipped, disciplined traffic may mean it.
    """

    def _warnings(self, gap):
        config = config_store.normalize({"transcribe": {"silence_gap_s": gap}})
        _errors, warnings = config_store.validate(config)
        return [w for w in warnings if "quiet" in w]

    def test_a_gap_inside_an_ordinary_pause_is_called_out(self):
        self.assertTrue(self._warnings(0.3))

    def test_the_default_says_nothing(self):
        self.assertEqual(self._warnings(1.2), [])

    def test_it_is_a_warning_not_a_refusal(self):
        config = config_store.normalize({"transcribe": {"silence_gap_s": 0.3}})
        errors, _warnings = config_store.validate(config)
        self.assertEqual(errors, [])
        self.assertEqual(config["transcribe"]["silence_gap_s"], 0.3)


if __name__ == "__main__":
    unittest.main()
