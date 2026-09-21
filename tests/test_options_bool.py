"""Boolean coercion for the add-on's settings, and the PoE reset built from them.

Regression: `main.py` used `bool(cfg.get(...))`, and a non-empty string is
truthy -- so `poe_reset_use_ssl: false` arriving as the *string* "false"
(rather than a JSON boolean) evaluated to True and the add-on kept dialling
https:// at a router with no www-ssl listener, while the add-on UI and the
saved options both plainly read "false".

Still worth testing now that settings come from the add-on's own UI rather
than /data/options.json: HTML form values are strings, so a checkbox or a
hand-posted body can just as easily hand "false" to the API. The coercion
moved to `config_store.normalize_scanner`, which every settings path (UI
save, file load, one-time options.json migration) goes through -- so these
tests now run the real end-to-end shape, normalize then build.

Doesn't need aiohttp installed -- it's stubbed below, since manager imports
mikrotik which imports it, but nothing here makes a request.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "addon" / "sds200_bridge" / "app"))

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

from config_store import as_bool, normalize_scanner  # noqa: E402
from manager import _build_poe_reset  # noqa: E402


class TestAsBool(unittest.TestCase):
    def test_real_booleans_pass_through(self):
        self.assertIs(as_bool(True, False), True)
        self.assertIs(as_bool(False, True), False)

    def test_string_falsehoods_are_false_not_truthy(self):
        # bool("false") is True -- the entire point of this helper.
        for text in ("false", "False", "FALSE", " false ", "no", "off", "0", ""):
            with self.subTest(text=text):
                self.assertIs(as_bool(text, True), False)

    def test_string_truths_are_true(self):
        for text in ("true", "True", "yes", "on", "1"):
            with self.subTest(text=text):
                self.assertIs(as_bool(text, False), True)

    def test_missing_falls_back_to_default(self):
        self.assertIs(as_bool(None, True), True)
        self.assertIs(as_bool(None, False), False)

    def test_unrecognised_string_falls_back_to_default(self):
        # Better to keep the documented default than to guess at "maybe".
        self.assertIs(as_bool("maybe", True), True)
        self.assertIs(as_bool("maybe", False), False)


class TestBuildPoeReset(unittest.TestCase):
    BASE = {
        "name": "Home",
        "host": "192.0.2.232",
        "poe_reset_host": "192.0.2.252",
        "poe_reset_username": "homeassistant",
        "poe_reset_password": "secret",
        "poe_reset_interface": "ether12",
    }

    def _build(self, **overrides):
        return _build_poe_reset(normalize_scanner({**self.BASE, **overrides}), "scan0")

    def test_string_false_selects_http(self):
        poe = self._build(poe_reset_use_ssl="false", poe_reset_verify_ssl="false")
        self.assertFalse(poe.use_ssl)
        self.assertFalse(poe.verify_ssl)
        self.assertTrue(poe.url.startswith("http://"), poe.url)

    def test_boolean_false_selects_http(self):
        poe = self._build(poe_reset_use_ssl=False)
        self.assertTrue(poe.url.startswith("http://"), poe.url)

    def test_defaults_to_https_when_unset(self):
        # Unchanged behaviour: the option is optional and defaults to https.
        self.assertTrue(self._build().url.startswith("https://"))

    def test_partial_config_is_ignored_entirely(self):
        cfg = dict(self.BASE)
        del cfg["poe_reset_password"]
        self.assertIsNone(_build_poe_reset(normalize_scanner(cfg), "scan0"))


if __name__ == "__main__":
    unittest.main()
