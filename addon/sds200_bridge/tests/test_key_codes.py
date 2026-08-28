from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import key_codes  # noqa: E402


class TestKeyCodes(unittest.TestCase):
    def test_resolve_by_friendly_name(self):
        self.assertEqual(key_codes.resolve("menu"), "M")
        self.assertEqual(key_codes.resolve("enter"), "E")

    def test_resolve_by_raw_code(self):
        self.assertEqual(key_codes.resolve("M"), "M")
        self.assertEqual(key_codes.resolve("1"), "1")

    def test_unknown_raises(self):
        with self.assertRaises(KeyError):
            key_codes.resolve("not_a_real_key")

    def test_every_code_round_trips(self):
        for name, code in key_codes.KEY_CODES.items():
            self.assertEqual(key_codes.resolve(name), code)
            self.assertEqual(key_codes.resolve(code), code)


if __name__ == "__main__":
    unittest.main()
