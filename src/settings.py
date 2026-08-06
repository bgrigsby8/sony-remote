"""
settings.py
-----------
The capture recipe as a machine config: `"f/11"`, `"1/160"`, `100`, `"flash"`
in, CrSDK raw values out, and back again.

Why this is a file of its own rather than a dict in the model: the encodings are
Sony's, they are not obvious, and getting one wrong produces a camera that
silently shoots at the wrong aperture. They are pure functions of a string, so
they are unit-tested without a camera, and the tests are the documentation of
record for each one.

    aperture       f/11        -> 1100          (F-number x 100)
    shutter_speed  "1/160"     -> 0x000100A0    (numerator << 16 | denominator)
    shutter_speed  "2\""       -> 0x00020001    (2 seconds = 2/1)
    iso            100         -> 100           (plain value; mode bits stay 0)
    iso            "auto"      -> 0x00FFFFFF
    white_balance  "flash"     -> "Flash"       (symbolic; the enum lives in C++)
    file_format    "raw+jpeg"  -> "RAW_JPEG"
    shutter_type   "mechanical"-> "Mechanical"

The enum-valued settings stop at a symbolic string on purpose. Sony's
`CrWhiteBalanceSetting_*` constants only exist in the SDK headers, so
`crsdk_ext.cpp` owns that last hop - it is the one place where the real symbols
are in scope and a typo is a compile error rather than a wrong colour cast.

**Focus position is deliberately absent from this table.** It is a raw SDK
integer with no documented physical unit, and inventing one (millimetres,
dioptres, 0-1) would mean pretending to a precision we don't have. Calibration
stores whatever integer the camera reported at a station and plays it back; see
`session.set_focus_position`.
"""

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from binding import UnsupportedValueError

# ----------------------------------------------------------------------
# Aperture
# ----------------------------------------------------------------------

_APERTURE_RE = re.compile(r"^\s*(?:f\s*/?\s*)?([0-9]+(?:\.[0-9]+)?)\s*$", re.IGNORECASE)


def encode_aperture(value: Any) -> int:
    """`"f/11"` / `"11"` / `11` / `1.8` -> hundredths of an F-number."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        match = _APERTURE_RE.match(str(value))
        if not match:
            raise UnsupportedValueError(
                f"aperture {value!r} is not an f-number; write it like \"f/11\", "
                '"11" or 11'
            )
        number = float(match.group(1))
    if number <= 0:
        raise UnsupportedValueError(f"aperture must be positive, got {value!r}")
    return int(round(number * 100))


def decode_aperture(raw: Any) -> str:
    number = int(raw) / 100.0
    # f/1.8 keeps its decimal, f/11 doesn't grow a ".0" - matching how the
    # numbers are written on a lens barrel and in the config.
    text = f"{number:.1f}".rstrip("0").rstrip(".")
    return f"f/{text}"


# ----------------------------------------------------------------------
# Shutter speed
#
# CrSDK packs the speed as a rational in one 32-bit word: numerator in the high
# 16 bits, denominator in the low 16. "1/160" is 0x0001_00A0; a 2-second
# exposure is 0x0002_0001. Zero means Bulb.
# ----------------------------------------------------------------------

_SHUTTER_FRACTION_RE = re.compile(r"^\s*([0-9]+)\s*/\s*([0-9]+)\s*$")
_SHUTTER_SECONDS_RE = re.compile(r'^\s*([0-9]+(?:\.[0-9]+)?)\s*(?:s|sec|secs|"|\')?\s*$')

_BULB_RAW = 0


def encode_shutter_speed(value: Any) -> int:
    text = str(value).strip()
    if text.lower() in ("bulb", "b"):
        return _BULB_RAW

    match = _SHUTTER_FRACTION_RE.match(text)
    if match:
        numerator, denominator = int(match.group(1)), int(match.group(2))
        if numerator == 0 or denominator == 0:
            raise UnsupportedValueError(f"shutter_speed {value!r} has a zero term")
        return (numerator << 16) | denominator

    match = _SHUTTER_SECONDS_RE.match(text)
    if match:
        seconds = float(match.group(1))
        if seconds <= 0:
            raise UnsupportedValueError(f"shutter_speed {value!r} must be positive")
        if seconds == int(seconds):
            return (int(seconds) << 16) | 1
        # Sub-second decimals ("0.4") are how a person writes 2/5; the camera
        # only speaks rationals, so convert rather than round to 0/1.
        denominator = 10
        numerator = int(round(seconds * denominator))
        divisor = _gcd(numerator, denominator)
        return ((numerator // divisor) << 16) | (denominator // divisor)

    raise UnsupportedValueError(
        f"shutter_speed {value!r} is not a shutter speed; write it like "
        '"1/160", "2\\"" (two seconds) or "bulb"'
    )


def decode_shutter_speed(raw: Any) -> str:
    raw = int(raw)
    if raw == _BULB_RAW:
        return "bulb"
    numerator, denominator = (raw >> 16) & 0xFFFF, raw & 0xFFFF
    if denominator == 0:
        return "bulb"
    if denominator == 1:
        return f'{numerator}"'
    return f"{numerator}/{denominator}"


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a or 1


# ----------------------------------------------------------------------
# ISO
#
# The raw word carries a mode in its top byte (extended / multi-frame NR) and
# the sensitivity in the low three. We only ever write mode 0 - plain ISO - but
# we mask on read, because a body left in Multi Frame NR by a previous operator
# would otherwise report an absurd number.
# ----------------------------------------------------------------------

_ISO_AUTO_RAW = 0x00FFFFFF
_ISO_VALUE_MASK = 0x00FFFFFF


def encode_iso(value: Any) -> int:
    if isinstance(value, str) and value.strip().lower() == "auto":
        return _ISO_AUTO_RAW
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise UnsupportedValueError(f"iso {value!r} is not a number or \"auto\"")
    try:
        number = int(float(str(value)))
    except ValueError:
        raise UnsupportedValueError(f"iso {value!r} is not a number or \"auto\"") from None
    if number <= 0:
        raise UnsupportedValueError(f"iso must be positive, got {value!r}")
    return number


def decode_iso(raw: Any) -> Any:
    raw = int(raw) & _ISO_VALUE_MASK
    return "auto" if raw == _ISO_AUTO_RAW else raw


# ----------------------------------------------------------------------
# Enum-valued settings
#
# Config uses lowercase words; the binding uses Sony's symbolic names. Aliases
# exist where the obvious word and Sony's word differ ("tungsten" is what a
# photographer says; Sony says "Incandescent").
# ----------------------------------------------------------------------

_WHITE_BALANCE = {
    "auto": "AWB",
    "awb": "AWB",
    "daylight": "Daylight",
    "sun": "Daylight",
    "sunny": "Daylight",
    "shade": "Shade",
    "cloudy": "Cloudy",
    "incandescent": "Incandescent",
    "tungsten": "Incandescent",
    "fluorescent": "Fluorescent",
    "flash": "Flash",
    "strobe": "Flash",
    "underwater": "Underwater",
    "color_temp": "ColorTemp",
    "custom1": "Custom1",
}

_SHUTTER_TYPE = {
    "auto": "Auto",
    "mechanical": "Mechanical",
    "mech": "Mechanical",
    "electronic": "Electronic",
    "silent": "Electronic",
}

_FILE_FORMAT = {
    "raw": "RAW",
    "raw+jpeg": "RAW_JPEG",
    "raw_jpeg": "RAW_JPEG",
    "rawjpeg": "RAW_JPEG",
    "jpeg": "JPEG",
    "jpg": "JPEG",
    "raw+heif": "RAW_HEIF",
    "heif": "HEIF",
}


def _enum_encoder(name: str, table: Dict[str, str]) -> Callable[[Any], str]:
    def encode(value: Any) -> str:
        key = str(value).strip().lower().replace(" ", "_").replace("-", "_")
        symbolic = table.get(key)
        if symbolic is None:
            raise UnsupportedValueError(
                f"{name} {value!r} is not recognised", valid=sorted(set(table))
            )
        return symbolic

    return encode


def _enum_decoder(table: Dict[str, str]) -> Callable[[Any], Any]:
    # First alias wins, so "flash" decodes back to "flash" and not "strobe".
    reverse: Dict[str, str] = {}
    for friendly, symbolic in table.items():
        reverse.setdefault(symbolic, friendly)

    def decode(raw: Any) -> Any:
        # A body may report a symbolic value we have no friendly word for
        # (a white-balance preset this table doesn't cover). Pass it through
        # rather than erroring - reading settings should never fail.
        return reverse.get(str(raw), raw)

    return decode


# ----------------------------------------------------------------------
# The table
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Setting:
    """One user-facing setting and how it maps onto a camera property."""

    key: str
    prop: str
    encode: Callable[[Any], Any]
    decode: Callable[[Any], Any]
    #: Human-readable summary of what this setting accepts, used in errors.
    accepts: str


SETTINGS: Dict[str, Setting] = {
    s.key: s
    for s in (
        Setting(
            "aperture",
            "f_number",
            encode_aperture,
            decode_aperture,
            'an f-number like "f/11", "11" or 11',
        ),
        Setting(
            "shutter_speed",
            "shutter_speed",
            encode_shutter_speed,
            decode_shutter_speed,
            'a shutter speed like "1/160", "2\\"" or "bulb"',
        ),
        Setting("iso", "iso_sensitivity", encode_iso, decode_iso, 'a number or "auto"'),
        Setting(
            "white_balance",
            "white_balance",
            _enum_encoder("white_balance", _WHITE_BALANCE),
            _enum_decoder(_WHITE_BALANCE),
            "one of " + ", ".join(sorted(set(_WHITE_BALANCE))),
        ),
        Setting(
            "shutter_type",
            "shutter_type",
            _enum_encoder("shutter_type", _SHUTTER_TYPE),
            _enum_decoder(_SHUTTER_TYPE),
            "one of " + ", ".join(sorted(set(_SHUTTER_TYPE))),
        ),
        Setting(
            "file_format",
            "still_file_format",
            _enum_encoder("file_format", _FILE_FORMAT),
            _enum_decoder(_FILE_FORMAT),
            "one of " + ", ".join(sorted(set(_FILE_FORMAT))),
        ),
    )
}

SETTING_KEYS: List[str] = list(SETTINGS)

#: Applied at connect when `apply_on_connect` doesn't say otherwise. Mechanical
#: shutter is the default because the rig fires a strobe: the electronic shutter
#: on this body reads the sensor progressively, so a flash lights only the band
#: of rows exposed while it fired (scope.md §4).
DEFAULT_SHUTTER_TYPE = "mechanical"


def validate(key: str, value: Any) -> Any:
    """Encode one setting, raising `UnsupportedValueError` if it doesn't parse.

    Pure - no camera involved. This is what `validate_config` runs, so a typo in
    `apply_on_connect` is caught when the machine config is saved rather than
    hours later when the first capture comes out at f/1.8.
    """
    setting = SETTINGS.get(key)
    if setting is None:
        raise UnsupportedValueError(
            f"unknown setting {key!r}", valid=SETTING_KEYS
        )
    return setting.encode(value)


def validate_all(values: Dict[str, Any]) -> Dict[str, Any]:
    """Encode a whole `apply_on_connect` / `set_settings` block.

    Unknown keys are rejected loudly (scope.md §4) rather than ignored: a
    silently-dropped `"apeture"` is a config that looks applied and isn't.
    """
    unknown = [k for k in values if k not in SETTINGS]
    if unknown:
        raise UnsupportedValueError(
            f"unknown setting(s): {', '.join(sorted(unknown))}", valid=SETTING_KEYS
        )
    return {key: validate(key, value) for key, value in values.items()}


def describe_choices(key: str, choices: Sequence[Any]) -> List[Any]:
    """Turn the camera's raw list of accepted values into user-facing ones.

    Used to build the `valid: [...]` list on an `unsupported_value` error, so
    the caller learns "this lens only stops down to f/22" from the camera rather
    than from a table we'd have to keep in sync.
    """
    setting = SETTINGS.get(key)
    if setting is None:
        return list(choices)
    described = []
    for choice in choices:
        try:
            described.append(setting.decode(choice))
        except Exception:  # noqa: BLE001 - a value we can't name is still a value
            described.append(choice)
    return described


def setting_for_property(prop: str) -> Optional[Setting]:
    """Reverse lookup, for logging a property-change event in user terms."""
    for setting in SETTINGS.values():
        if setting.prop == prop:
            return setting
    return None
