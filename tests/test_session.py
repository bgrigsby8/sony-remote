"""
Session tests - the whole module below the Viam API, driven end-to-end against
`FakeCamera`.

These run the real owner thread, the real reconnect state machine and the real
capture flow; only the SDK is simulated. That is deliberate: the parts most
likely to be wrong are the ones that involve two threads and a timeout, and
mocking the session out would test nothing.
"""

import os
import threading
import time

import pytest

from binding import (
    BusyError,
    CameraError,
    CaptureTimeoutError,
    ConfigurationError,
    DeviceInfo,
    NotConnectedError,
    UnsupportedValueError,
)
from binding.fake import FakeCamera
from conftest import wait_until


@pytest.fixture
def session(make_session, fake):
    """A connected session over `fake`."""
    session = make_session(fake)
    assert wait_until(lambda: session.connected), "session never connected"
    return session


# ----------------------------------------------------------------------
# Connection
# ----------------------------------------------------------------------


class TestConnect:
    def test_connects_and_reports_the_body(self, session, fake):
        status = session.device_status()
        assert status["connected"] is True
        assert status["model"] == "ILCE-7RM5"
        assert status["serial"] == "SN0000001"
        assert status["battery_pct"] == 87
        assert status["lens"] == "FE 35mm F1.8"
        assert status["last_error"] is None

    def test_save_destination_is_pointed_at_capture_dir(self, session, fake, capture_dir):
        # Direct-to-host is the whole reason `capture` can hand back a path.
        assert fake.save_destination == capture_dir
        assert os.path.isdir(capture_dir)

    def test_no_camera_is_reported_not_crashed(self, make_session, capture_dir):
        fake = FakeCamera(present=False)
        session = make_session(fake)
        assert wait_until(lambda: session.device_status()["last_error"] is not None)
        status = session.device_status()
        assert status["connected"] is False
        assert "no Sony camera found" in status["last_error"]

    def test_commands_fail_fast_while_disconnected(self, make_session):
        session = make_session(FakeCamera(present=False))
        wait_until(lambda: session.device_status()["last_error"] is not None)

        started = time.monotonic()
        with pytest.raises(NotConnectedError) as exc:
            session.capture()
        # "Fails fast" is the requirement (scope.md §6) - a caller must not sit
        # on a 15-second capture timeout for a camera that isn't there.
        assert time.monotonic() - started < 1.0
        assert str(exc.value).startswith("[disconnected]")

    def test_two_cameras_and_no_serial_is_a_configuration_error(self, make_session):
        fake = FakeCamera()
        fake.extra_devices = [DeviceInfo(model="ILCE-7RM5", serial="SN0000002", id=1)]
        session = make_session(fake)
        assert wait_until(lambda: session.device_status()["last_error"] is not None)
        error = session.device_status()["last_error"]
        assert "2 Sony cameras" in error and "`serial`" in error
        assert not session.connected

    def test_serial_picks_the_right_body(self, make_session):
        fake = FakeCamera(serial="SN0000001")
        fake.extra_devices = [DeviceInfo(model="ILCE-7RM5", serial="SN0000002", id=1)]
        session = make_session(fake, serial="SN0000002")
        assert wait_until(lambda: session.connected)
        assert fake.connected_device.serial == "SN0000002"

    def test_unknown_serial_says_what_it_did_find(self, make_session):
        session = make_session(FakeCamera(serial="SN0000001"), serial="SN9999999")
        assert wait_until(lambda: session.device_status()["last_error"] is not None)
        error = session.device_status()["last_error"]
        assert "SN9999999" in error and "SN0000001" in error

    def test_repeated_failures_are_not_logged_repeatedly(self, make_session, logger):
        session = make_session(FakeCamera(present=False))
        wait_until(lambda: session.device_status()["connect_attempts"] > 3)
        warnings = [m for lvl, m in logger.lines if lvl == "warning"]
        # A camera that's simply unplugged retries forever; the log must not
        # fill with the same line.
        assert len(warnings) == 1


class TestReconnect:
    def test_unplug_then_replug_recovers_on_its_own(self, session, fake):
        fake.unplug()
        assert wait_until(lambda: not session.connected)
        assert session.device_status()["connected"] is False

        fake.plug_in()
        assert wait_until(lambda: session.connected, timeout=5.0)
        assert fake.connects == 2
        assert session.device_status()["connected"] is True

    def test_settings_are_reapplied_after_a_reconnect(self, make_session, fake):
        session = make_session(fake, apply_on_connect={"aperture": "f/16"})
        assert wait_until(lambda: session.connected)
        assert fake.property_value("f_number") == 1600

        # The body comes back on its own settings, not ours - a power cycle or
        # a dial turned while it was unplugged.
        fake._properties["f_number"]["value"] = 280
        fake.unplug()
        assert wait_until(lambda: not session.connected)
        fake.plug_in()
        assert wait_until(lambda: session.connected, timeout=5.0)

        assert fake.property_value("f_number") == 1600

    def test_live_view_never_returns_a_stale_frame(self, session, fake):
        assert session.live_view()
        fake.unplug()
        assert wait_until(lambda: not session.connected)
        with pytest.raises(NotConnectedError):
            session.live_view()


# ----------------------------------------------------------------------
# apply_on_connect
# ----------------------------------------------------------------------


class TestApplyOnConnect:
    def test_applies_the_whole_recipe(self, make_session, fake):
        session = make_session(
            fake,
            apply_on_connect={
                "aperture": "f/11",
                "shutter_speed": "1/160",
                "iso": 100,
                "white_balance": "flash",
                "file_format": "raw",
                "shutter_type": "mechanical",
            },
        )
        assert wait_until(lambda: session.connected)
        assert fake.property_value("f_number") == 1100
        assert fake.property_value("shutter_speed") == (1 << 16) | 160
        assert fake.property_value("iso_sensitivity") == 100
        assert fake.property_value("white_balance") == "Flash"
        assert fake.property_value("still_file_format") == "RAW"
        assert fake.property_value("shutter_type") == "Mechanical"

    def test_pc_remote_priority_is_taken_before_any_setting(self, make_session, fake):
        # A real body boots owning its shooting settings and rejects (or
        # silently ignores) remote sets until the PC takes priority - so the
        # priority write must land first, or the whole recipe bounces.
        session = make_session(fake, apply_on_connect={"aperture": "f/11"})
        assert wait_until(lambda: session.connected)
        assert fake.property_value("priority_key") == "PCRemote"
        names = [name for name, _ in fake.property_writes]
        assert names[0] == "priority_key"
        assert "f_number" in names

    def test_a_busy_body_gets_one_settle_and_retry(self, make_session, fake, logger):
        # The A7R V rejects the first write fired too soon after the handshake
        # with a busy-class error, then accepts the same write moments later.
        # One retry must absorb that instead of losing the priority key or
        # recording a phantom apply error.
        fake.busy_once = True
        session = make_session(fake, apply_on_connect={"aperture": "f/11"})
        assert wait_until(lambda: session.connected)
        assert session.device_status()["apply_errors"] == []
        assert fake.property_value("priority_key") == "PCRemote"
        assert fake.property_value("f_number") == 1100
        assert "could not take PC-remote priority" not in logger.text("warning")

    def test_mechanical_shutter_is_the_default(self, make_session, fake):
        # The rig fires a strobe; the electronic shutter's rolling readout would
        # light only part of the frame.
        fake._properties["shutter_type"]["value"] = "Electronic"
        session = make_session(fake)
        assert wait_until(lambda: session.connected)
        assert fake.property_value("shutter_type") == "Mechanical"

    def test_the_default_can_be_overridden(self, make_session, fake):
        session = make_session(fake, apply_on_connect={"shutter_type": "electronic"})
        assert wait_until(lambda: session.connected)
        assert fake.property_value("shutter_type") == "Electronic"

    def test_a_rejected_value_is_reported_but_does_not_break_the_session(
        self, make_session, fake, logger
    ):
        # A body that won't stop down to f/45 must not leave the operator with a
        # component that can't even show live view to debug with.
        session = make_session(fake, apply_on_connect={"aperture": "f/45"})
        assert wait_until(lambda: session.connected)

        status = session.device_status()
        assert status["connected"] is True
        assert any("aperture" in e for e in status["apply_errors"])
        assert "apply_on_connect failed" in logger.text("error")
        assert session.live_view()


# ----------------------------------------------------------------------
# Capture
# ----------------------------------------------------------------------


class TestCapture:
    def test_returns_a_host_path_that_exists(self, session, capture_dir):
        result = session.capture()
        assert result["path"].startswith(capture_dir)
        assert os.path.exists(result["path"])
        assert result["size"] > 0
        assert result["name"].endswith(".ARW")
        assert result["mime_type"] == "application/octet-stream"

    def test_path_and_saved_to_are_both_present(self, session):
        # color-correction reads `saved_to or path`; the webapp reads `path`.
        # Direct-to-host means there's no separate on-camera location, so both
        # are the same host path rather than one being absent.
        result = session.capture()
        assert result["saved_to"] == result["path"]

    def test_counts_shutter_actuations(self, session):
        assert session.capture()["capture_count"] == 1
        assert session.capture()["capture_count"] == 2

    def test_records_focus_and_settings_for_the_audit_trail(self, session, logger):
        result = session.capture()
        assert result["focus_position"] == 128
        assert result["settings"]["aperture"] == "f/11"

        # scope.md §6: this line is the answer when Nines questions an image.
        line = logger.text("info")
        assert result["path"] in line
        assert "focus=128" in line
        assert "aperture" in line

    def test_falls_back_to_diffing_the_directory(self, make_session, capture_dir):
        # Some bodies report the capture without naming the file - the exact
        # case ptp.py hit on Canon. The pre-trigger snapshot makes the diff safe.
        fake = FakeCamera(emit_file_path=False)
        session = make_session(fake)
        assert wait_until(lambda: session.connected)

        result = session.capture()
        assert os.path.exists(result["path"])
        assert result["size"] > 0

    def test_waits_for_a_slow_write_to_finish(self, make_session):
        # A file that is visible but truncated must not be handed to a consumer.
        fake = FakeCamera(emit_file_path=False, slow_write_s=0.2)
        session = make_session(fake, capture_timeout_s=4.0)
        assert wait_until(lambda: session.connected)

        result = session.capture()
        assert result["size"] == os.path.getsize(result["path"])
        assert result["size"] > 1000

    def test_raw_plus_jpeg_returns_both_with_the_raw_first(self, make_session, fake):
        session = make_session(fake, apply_on_connect={"file_format": "raw+jpeg"})
        assert wait_until(lambda: session.connected)

        result = session.capture()
        assert len(result["paths"]) == 2
        assert result["path"].endswith(".ARW")
        assert all(os.path.exists(p) for p in result["paths"])

    def test_timeout_is_a_structured_error(self, make_session):
        fake = FakeCamera(drop_capture=True)
        session = make_session(fake, capture_timeout_s=0.4)
        assert wait_until(lambda: session.connected)

        with pytest.raises(CaptureTimeoutError) as exc:
            session.capture()
        assert str(exc.value).startswith("[timeout]")
        assert "capture_timeout_s" in str(exc.value)

    def test_timeout_after_a_completed_exposure_blames_the_save_destination(
        self, make_session
    ):
        fake = FakeCamera(drop_capture=True)
        session = make_session(fake, capture_timeout_s=0.4)
        assert wait_until(lambda: session.connected)

        # Exposure finished, nothing reached the host: the body is writing to
        # its card. The error should say so rather than blame the timeout.
        def announce():
            time.sleep(0.05)
            fake.push_event("capture_complete")

        threading.Thread(target=announce, daemon=True).start()
        with pytest.raises(CaptureTimeoutError) as exc:
            session.capture()
        assert "card" in str(exc.value)

    def test_disconnect_mid_capture_fails_fast(self, make_session):
        fake = FakeCamera(capture_delay_s=5.0)
        session = make_session(fake, capture_timeout_s=10.0)
        assert wait_until(lambda: session.connected)

        def yank():
            time.sleep(0.1)
            fake.unplug()

        threading.Thread(target=yank, daemon=True).start()

        started = time.monotonic()
        with pytest.raises(NotConnectedError) as exc:
            session.capture()
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, "capture should fail on disconnect, not wait for the timeout"
        assert "during capture" in str(exc.value)

    def test_retention_runs_after_each_capture(self, make_session, fake, capture_dir):
        session = make_session(fake, retention_max_files=2)
        assert wait_until(lambda: session.connected)

        for _ in range(4):
            session.capture()
        assert len(session.store.list_images()) == 2

    def test_captures_serialize(self, session, fake):
        # scope.md §6: concurrent DoCommands queue. One owner thread makes that
        # structural, and that in turn is why live view can't interleave with a
        # capture (open question §10.4).
        results = []
        errors = []

        def shoot():
            try:
                results.append(session.capture())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=shoot) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        assert not errors
        assert fake.triggers == 3
        assert sorted(r["capture_count"] for r in results) == [1, 2, 3]
        assert len({r["path"] for r in results}) == 3


# ----------------------------------------------------------------------
# Focus
# ----------------------------------------------------------------------


class TestFocus:
    def test_get_returns_the_raw_sdk_value(self, session):
        assert session.get_focus_position() == 128

    def test_set_lands_and_reports_ok(self, session):
        result = session.set_focus_position(200)
        assert result == {
            "position": 200,
            "target": 200,
            "tolerance": 2,
            "attempts": 1,
            "ok": True,
            "units": "sdk_raw",
        }
        assert session.get_focus_position() == 200

    def test_within_tolerance_counts_as_landed(self, session, fake):
        fake.focus_error_sequence = [2]
        result = session.set_focus_position(200)
        assert result["ok"] is True
        assert result["attempts"] == 1
        assert result["position"] == 202

    def test_a_miss_is_retried_once(self, session, fake):
        # Focus is a mechanism, not a register: the first move can fall short.
        fake.focus_error_sequence = [9, 0]
        result = session.set_focus_position(200)
        assert result["attempts"] == 2
        assert result["ok"] is True
        assert result["position"] == 200

    def test_a_persistent_miss_reports_not_ok_rather_than_retrying_forever(
        self, session, fake, logger
    ):
        fake.focus_error_sequence = [20, 20, 20, 20]
        result = session.set_focus_position(200)
        assert result["attempts"] == 2
        assert result["ok"] is False
        assert result["position"] == 220
        assert "focus did not reach 200" in logger.text("warning")

    def test_tolerance_can_be_overridden_per_call(self, session, fake):
        fake.focus_error_sequence = [5]
        assert session.set_focus_position(200, tolerance=10)["ok"] is True

    def test_the_lens_is_put_in_manual_focus_first(self, session, fake, logger):
        # An absolute focus position doesn't stick while the body owns the lens.
        fake._properties["focus_mode"]["value"] = "AF_S"
        session.set_focus_position(200)
        assert fake.property_value("focus_mode") == "MF"
        assert "switched to MF" in logger.text("info")

    def test_a_body_without_a_focus_mode_property_still_focuses(self, session, fake):
        del fake._properties["focus_mode"]
        assert session.set_focus_position(200)["ok"] is True

    def test_autofocus_once_reports_where_it_landed(self, session, fake):
        fake.autofocus_position = 175
        result = session.autofocus_once()
        assert result == {"position": 175, "units": "sdk_raw", "acquired": True}

    def test_autofocus_failure_still_reports_a_position(self, session, fake, logger):
        fake.autofocus_succeeds = False
        result = session.autofocus_once()
        assert result["acquired"] is False
        assert result["position"] == 128
        assert "did not lock" in logger.text("warning")


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


class TestSettings:
    def test_get_settings_speaks_config_vocabulary(self, session):
        assert session.get_settings() == {
            "aperture": "f/11",
            "shutter_speed": "1/160",
            "iso": 100,
            "white_balance": "auto",
            "shutter_type": "mechanical",
            "file_format": "raw",
        }

    def test_set_settings_round_trips(self, session):
        result = session.set_settings({"aperture": "f/8", "iso": 400})
        assert result["aperture"] == "f/8"
        assert result["iso"] == 400
        assert session.get_settings()["aperture"] == "f/8"

    def test_a_value_the_camera_rejects_lists_what_it_accepts(self, session):
        with pytest.raises(UnsupportedValueError) as exc:
            session.set_settings({"aperture": "f/45"})
        assert str(exc.value).startswith("[unsupported_value]")
        # The list comes from the camera, in the operator's vocabulary - the
        # mounted lens decides this, not a table we'd have to maintain.
        assert "f/11" in exc.value.details["valid"]

    def test_an_unparseable_value_never_reaches_the_camera(self, session, fake):
        before = len(fake.property_writes)
        with pytest.raises(UnsupportedValueError):
            session.set_settings({"aperture": "wide open"})
        assert len(fake.property_writes) == before

    def test_unknown_keys_are_rejected(self, session):
        with pytest.raises(UnsupportedValueError):
            session.set_settings({"apeture": "f/11"})

    def test_a_property_the_body_lacks_is_omitted_not_fatal(self, session, fake):
        del fake._properties["shutter_type"]
        settings = session.get_settings()
        assert "shutter_type" not in settings
        assert settings["aperture"] == "f/11"


# ----------------------------------------------------------------------
# Live view
# ----------------------------------------------------------------------


class TestLiveView:
    def test_returns_a_jpeg(self, session):
        frame = session.live_view()
        assert frame.startswith(b"\xff\xd8")

    def test_throttles_to_live_view_max_fps(self, make_session, fake):
        session = make_session(fake, live_view_max_fps=5.0)
        assert wait_until(lambda: session.connected)

        session.live_view()
        fake.live_view_calls = 0
        for _ in range(20):
            session.live_view()
        # Twenty calls inside one 200ms window must not become twenty USB round
        # trips - an idle preview should cost nothing while a capture runs.
        assert fake.live_view_calls == 0

        time.sleep(0.25)
        session.live_view()
        assert fake.live_view_calls == 1

    def test_a_body_with_no_frame_yet_says_so(self, session, fake, monkeypatch):
        monkeypatch.setattr(fake, "live_view_jpeg", lambda: None)
        with pytest.raises(CameraError) as exc:
            session.live_view()
        assert "live-view frame" in str(exc.value)


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------


class TestLifecycle:
    def test_close_releases_the_sdk(self, make_session, fake):
        session = make_session(fake)
        assert wait_until(lambda: session.connected)
        session.close()
        # Unpaired init/release is what forces an operator to power-cycle the
        # body after a viam-server restart (scope.md §6).
        assert fake.released is True
        assert fake.is_connected() is False

    def test_calls_after_close_fail_cleanly(self, make_session, fake):
        session = make_session(fake)
        assert wait_until(lambda: session.connected)
        session.close()
        with pytest.raises(NotConnectedError):
            session.capture()

    def test_close_is_idempotent(self, make_session, fake):
        session = make_session(fake)
        session.close()
        session.close()
