"""
Settings encoding tests.

These are the documentation of record for Sony's encodings: aperture as
hundredths, shutter speed as a packed rational, ISO's auto sentinel. Every
number in here was derived from the SDK's property reference, and if a value
turns out to be wrong on hardware the fix belongs here first - the hardware
smoke checklist verifies each one against what the body actually reports.
"""

import pytest

import settings as settings_mod
from binding import UnsupportedValueError


class TestAperture:
    @pytest.mark.parametrize(
        "value,raw",
        [
            ("f/11", 1100),
            ("f/1.8", 180),
            ("F/2.8", 280),
            ("f 11", 1100),
            ("11", 1100),
            (11, 1100),
            (1.8, 180),
            (22.0, 2200),
        ],
    )
    def test_encode(self, value, raw):
        assert settings_mod.encode_aperture(value) == raw

    @pytest.mark.parametrize("raw,text", [(1100, "f/11"), (180, "f/1.8"), (2200, "f/22")])
    def test_decode(self, raw, text):
        assert settings_mod.decode_aperture(raw) == text

    def test_round_trip(self):
        for text in ("f/1.8", "f/2.8", "f/4", "f/11", "f/22"):
            assert settings_mod.decode_aperture(settings_mod.encode_aperture(text)) == text

    @pytest.mark.parametrize("value", ["wide open", "f/", "", "-5", 0, "f/0"])
    def test_rejects_nonsense(self, value):
        with pytest.raises(UnsupportedValueError):
            settings_mod.encode_aperture(value)


class TestShutterSpeed:
    @pytest.mark.parametrize(
        "value,raw",
        [
            ("1/160", (1 << 16) | 160),
            ("1/1000", (1 << 16) | 1000),
            ("1 / 60", (1 << 16) | 60),
            ('2"', (2 << 16) | 1),
            ("2s", (2 << 16) | 1),
            ("30", (30 << 16) | 1),
            ("bulb", 0),
            ("B", 0),
        ],
    )
    def test_encode(self, value, raw):
        assert settings_mod.encode_shutter_speed(value) == raw

    def test_decimal_seconds_become_a_rational(self):
        # 0.4s is 2/5, not 0/1. Rounding it to an integer would silently shoot
        # a bulb exposure.
        assert settings_mod.encode_shutter_speed("0.4") == (2 << 16) | 5

    @pytest.mark.parametrize(
        "raw,text",
        [((1 << 16) | 160, "1/160"), ((2 << 16) | 1, '2"'), (0, "bulb")],
    )
    def test_decode(self, raw, text):
        assert settings_mod.decode_shutter_speed(raw) == text

    def test_round_trip(self):
        for text in ("1/160", "1/8000", '2"', '30"', "bulb"):
            raw = settings_mod.encode_shutter_speed(text)
            assert settings_mod.decode_shutter_speed(raw) == text

    def test_the_studio_default_is_what_we_think_it_is(self):
        # 1/160 mechanical is the strobe-sync setting the whole rig is built
        # around; pin its encoding explicitly.
        assert settings_mod.encode_shutter_speed("1/160") == 0x000100A0

    @pytest.mark.parametrize("value", ["fast", "1/0", "0/160", "-1", "1/"])
    def test_rejects_nonsense(self, value):
        with pytest.raises(UnsupportedValueError):
            settings_mod.encode_shutter_speed(value)


class TestIso:
    @pytest.mark.parametrize("value,raw", [(100, 100), ("400", 400), (1600, 1600)])
    def test_encode(self, value, raw):
        assert settings_mod.encode_iso(value) == raw

    def test_auto(self):
        assert settings_mod.encode_iso("auto") == 0x00FFFFFF
        assert settings_mod.decode_iso(0x00FFFFFF) == "auto"

    def test_decode_masks_the_mode_byte(self):
        # A body left in Multi Frame NR reports the mode in the top byte;
        # without the mask ISO 100 would read as some enormous number.
        assert settings_mod.decode_iso(0x01000064) == 100

    @pytest.mark.parametrize("value", ["fast", 0, -100, True, None])
    def test_rejects_nonsense(self, value):
        with pytest.raises(UnsupportedValueError):
            settings_mod.encode_iso(value)


class TestEnums:
    @pytest.mark.parametrize(
        "key,value,symbolic",
        [
            ("white_balance", "flash", "Flash"),
            ("white_balance", "Flash", "Flash"),
            ("white_balance", "strobe", "Flash"),
            ("white_balance", "tungsten", "Incandescent"),
            ("white_balance", "auto", "AWB"),
            ("shutter_type", "mechanical", "Mechanical"),
            ("shutter_type", "electronic", "Electronic"),
            ("shutter_type", "silent", "Electronic"),
            ("file_format", "raw", "RAW"),
            ("file_format", "raw+jpeg", "RAW_JPEG"),
            ("file_format", "RAW+JPEG", "RAW_JPEG"),
            ("file_format", "jpg", "JPEG"),
        ],
    )
    def test_encode(self, key, value, symbolic):
        assert settings_mod.validate(key, value) == symbolic

    def test_aliases_decode_to_the_canonical_word(self):
        setting = settings_mod.SETTINGS["white_balance"]
        assert setting.decode("Flash") == "flash"
        assert setting.decode("Incandescent") == "incandescent"

    def test_unknown_symbolic_value_passes_through_rather_than_failing(self):
        # Reading settings must never fail because the body reports a preset we
        # have no friendly word for.
        assert settings_mod.SETTINGS["white_balance"].decode("Fluorescent_CoolWhite") == (
            "Fluorescent_CoolWhite"
        )

    def test_error_lists_what_is_accepted(self):
        with pytest.raises(UnsupportedValueError) as exc:
            settings_mod.validate("white_balance", "moonlight")
        assert "flash" in exc.value.details["valid"]


class TestValidateAll:
    def test_encodes_a_whole_block(self):
        encoded = settings_mod.validate_all(
            {
                "shutter_type": "mechanical",
                "aperture": "f/11",
                "shutter_speed": "1/160",
                "iso": 100,
                "white_balance": "flash",
                "file_format": "raw",
            }
        )
        assert encoded == {
            "shutter_type": "Mechanical",
            "aperture": 1100,
            "shutter_speed": (1 << 16) | 160,
            "iso": 100,
            "white_balance": "Flash",
            "file_format": "RAW",
        }

    def test_unknown_keys_are_rejected_loudly(self):
        # Unknown apply_on_connect keys are rejected loudly. A silently
        # dropped "apeture" is a config that looks applied and isn't.
        with pytest.raises(UnsupportedValueError) as exc:
            settings_mod.validate_all({"apeture": "f/11"})
        assert "apeture" in str(exc.value)
        assert "aperture" in exc.value.details["valid"]

    def test_empty_block_is_fine(self):
        assert settings_mod.validate_all({}) == {}


def test_describe_choices_speaks_config_vocabulary():
    # This is what turns a rejection into an actionable error: the camera hands
    # back raw hundredths, the operator reads f-numbers.
    assert settings_mod.describe_choices("aperture", [180, 1100, 2200]) == [
        "f/1.8",
        "f/11",
        "f/22",
    ]
    assert settings_mod.describe_choices("white_balance", ["AWB", "Flash"]) == [
        "auto",
        "flash",
    ]
