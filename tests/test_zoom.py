"""Power zoom: the continuous drive, the closed loop on top of it, and the
safety nets (stop on every exit, the open-ended-drive watchdog)."""


import pytest

from binding import UnsupportedValueError
from conftest import wait_until


@pytest.fixture
def zoom_session(make_session, fake):
    def build(**overrides):
        session = make_session(fake, **overrides)
        assert wait_until(lambda: session.connected)
        return session

    return build


class TestGetZoom:
    def test_reports_focal_length_range_and_speeds(self, zoom_session):
        info = zoom_session().get_zoom()
        assert info["focal_length_mm"] == 16.0
        assert (info["min_mm"], info["max_mm"], info["step_mm"]) == (16.0, 35.0, 0.1)
        assert info["speed_range"] == [-8, 8]
        assert info["drive_available"] is True
        assert info["zoom_type"] == "optical"
        assert info["bar"] == {"boxes": 1, "box": 0, "position_pct": 0}
        assert info["driving"] is False

    def test_prime_lens_reports_no_drive(self, zoom_session, fake):
        fake.power_zoom = False
        info = zoom_session().get_zoom()
        assert info["drive_available"] is False
        assert info["focal_length_mm"] is None


class TestZoomDrive:
    def test_timed_drive_moves_both_ways_and_always_stops(self, zoom_session, fake):
        session = zoom_session()
        tele = session.zoom_drive(4, duration_s=0.3)
        assert tele["driving"] is False and not fake.zoom_is_driving()
        assert tele["focal_length_mm"] > 16.0
        wide = session.zoom_drive(-4, duration_s=0.1)
        assert not fake.zoom_is_driving()
        assert wide["focal_length_mm"] < tele["focal_length_mm"]
        # Negative speeds reach the binding as negative ints; the extension
        # owns the wire encoding (RemoteCli's sign-extended UInt16Array).
        assert ("zoom_operation", -4) in fake.property_writes

    def test_open_ended_drive_is_stopped_by_the_watchdog(self, zoom_session, fake, logger):
        session = zoom_session(zoom_max_drive_s=0.2)
        assert session.zoom_drive(1)["driving"] is True
        assert fake.zoom_is_driving()
        assert wait_until(lambda: not fake.zoom_is_driving(), timeout=2.0)
        assert "stopping it" in logger.text("warning")

    def test_zero_speed_stops_an_open_ended_drive(self, zoom_session, fake):
        session = zoom_session()
        session.zoom_drive(2)
        result = session.zoom_drive(0)
        assert result["driving"] is False and not fake.zoom_is_driving()

    def test_speed_is_clamped_to_the_reported_range(self, zoom_session, fake):
        session = zoom_session()
        session.zoom_drive(20, duration_s=0.05)
        assert ("zoom_operation", 8) in fake.property_writes

    def test_drive_without_a_power_zoom_lens_is_unsupported(self, zoom_session, fake):
        fake.power_zoom = False
        with pytest.raises(UnsupportedValueError):
            zoom_session().zoom_drive(3, duration_s=0.1)


class TestSetZoom:
    @pytest.mark.parametrize("target", [24.0, 35.0, 20.5, 16.0])
    def test_lands_exactly_on_reportable_targets(self, zoom_session, fake, target):
        session = zoom_session()
        session.set_zoom(28.0)  # start from somewhere mid-range
        result = session.set_zoom(target)
        assert result["ok"] is True, result
        assert result["focal_length_mm"] == target
        assert not fake.zoom_is_driving()

    def test_target_between_readable_positions_stops_hunting(self, zoom_session, fake):
        # The lens advertises 0.1mm but reads back in 0.5mm, so 25.2 can never
        # read back exactly: the loop must notice and finish on the closest
        # reportable position (25.0) rather than burning every pass.
        session = zoom_session()
        session.set_zoom(30.0)
        result = session.set_zoom(25.2, tolerance_mm=0.0)
        assert result["ok"] is False
        assert result["resolution_limited"] is True
        assert result["focal_length_mm"] == 25.0
        assert not fake.zoom_is_driving()

    def test_tolerance_accepts_a_neighbouring_position(self, zoom_session):
        result = zoom_session().set_zoom(25.2, tolerance_mm=0.5)
        assert result["ok"] is True
        assert abs(result["focal_length_mm"] - 25.2) <= 0.5

    def test_target_outside_the_lens_is_clamped(self, zoom_session, logger):
        result = zoom_session().set_zoom(70.0)
        assert result["target_mm"] == 35.0
        assert result["focal_length_mm"] == 35.0
        assert "clamped" in logger.text("warning")

    def test_zoom_invalidates_emulated_focus(self, zoom_session, fake):
        fake.absolute_focus_supported = False
        session = zoom_session(emulated_travel_nudges=5, emulated_nudge_interval_s=0.0)
        session.home_focus()
        assert session.device_status()["focus_homed"] is True
        session.set_zoom(24.0)
        assert session.device_status()["focus_homed"] is False

    def test_zoom_on_connect(self, zoom_session, fake):
        # `connected` flips before the on-connect moves finish, as with
        # focus_on_connect, so wait for the lens rather than the flag.
        zoom_session(zoom_on_connect=28.0)
        assert wait_until(lambda: abs(fake.zoom_true_um() - 28000) <= 250, timeout=5.0)


class TestPresets:
    def test_save_then_load_restores_zoom(self, zoom_session):
        session = zoom_session()
        session.set_zoom(30.0)
        session.zoom_preset("save", 3)
        session.set_zoom(18.0)
        result = session.zoom_preset("load", 3)
        assert result["focal_length_mm"] == 30.0

    def test_loading_an_empty_slot_fails(self, zoom_session):
        with pytest.raises(UnsupportedValueError):
            zoom_session().zoom_preset("load", 9)
