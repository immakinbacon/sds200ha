"""KEY command codes, from the "key code for KEY Command" table in the spec.

The spec's table is headed BCD536HP/SDS100; SDS100/SDS200/SDS150 share the
command set but the table has no SDS200-specific column. SDS200 is a
base/mobile unit with physical Volume and Squelch knobs (unlike the
handheld SDS100, where the table marks Q as "(none)") -- verify
squelch_push/service_type against real SDS200 hardware.
"""

KEY_CODES: dict[str, str] = {
    "menu": "M",
    "func": "F",
    "avoid": "L",
    "0": "0",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
    "5": "5",
    "6": "6",
    "7": "7",
    "8": "8",
    "9": "9",
    "dot": ".",
    "enter": "E",
    "rotary_right": ">",
    "rotary_left": "<",
    "rotary_push": "^",
    "volume_push": "V",
    "squelch_push": "Q",  # unverified on SDS200, see module docstring
    "replay": "Y",
    "soft1": "A",
    "soft2": "B",
    "soft3": "C",
    "zip": "Z",
    "service_type": "T",  # unverified on SDS200, see module docstring
    "range": "R",
}

CODE_TO_NAME: dict[str, str] = {v: k for k, v in KEY_CODES.items()}


def resolve(code_or_name: str) -> str:
    """Accept either a friendly name ("menu") or a raw code ("M") and return the code."""
    if code_or_name in KEY_CODES:
        return KEY_CODES[code_or_name]
    if code_or_name in CODE_TO_NAME:
        return code_or_name
    raise KeyError(f"unknown key code or name: {code_or_name!r}")
