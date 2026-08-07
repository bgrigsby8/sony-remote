"""
Model-level tests: config validation, the Viam camera API, and the DoCommand
contract.

The DoCommand tests are the ones that matter most for the rig. `color-correction`
wraps this model and calls `capture` / `trigger` / `download` with shapes it
learned from the `ptp` model; if any of those drift, a sweep fails at the point
where a photographer is standing over the arm, not in CI. So the response shapes
are asserted key by key, and there is a test that walks the exact call sequence
`color-correction` makes.
"""

import os

import pytest
from viam.components.camera import Camera as CameraBase
from viam.media.video import CameraMimeType
from viam.proto.app.robot import ComponentConfig
from viam.utils import dict_to_struct

import binding
from binding.fake import FakeCamera
from conftest import wait_until
from models.camera import Camera


def make_config(name="camera", **attributes):
    return ComponentConfig(name=name, attributes=dict_to_struct(attributes))


@pytest.fixture
def camera(monkeypatch, capture_dir):
    """A configured model instance wired to a `FakeCamera`.

    `binding: "fake"` is a supported config value, so this is how an operator
    would bring the module up before the hardware arrives - the test just holds
    on to the binding so it can poke at it.
    """
    fakes = []

    def make_binding(kind=None):
        fake = FakeCamera()
        fakes.append(fake)
        return fake

    monkeypatch.setattr("models.camera.make_binding", make_binding)

    instance = Camera.new(
        make_config(capture_dir=capture_dir, binding="fake", capture_timeout_s=3.0), {}
    )
    instance.fake = fakes[0]
    assert wait_until(lambda: instance._session.connected)
    yield instance
    instance._session.close(timeout=2.0)


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------


class TestValidateConfig:
    def test_an_empty_config_is_valid(self):
        assert Camera.validate_config(make_config()) == ([], [])

    def test_a_full_config_is_valid(self):
        required, optional = Camera.validate_config(
            make_config(
                serial="SN0000001",
                capture_dir="/tmp/sony-remote",
                retention_max_files=200,
                live_view_max_fps=10,
                connect_timeout_s=10,
                capture_timeout_s=15,
                focus_tolerance=2,
                apply_on_connect={
                    "shutter_type": "mechanical",
                    "aperture": "f/11",
                    "shutter_speed": "1/160",
                    "iso": 100,
                    "white_balance": "flash",
                    "file_format": "raw",
                },
            )
        )
        # This model owns its USB camera; it depends on no other resource.
        assert (required, optional) == ([], [])

    @pytest.mark.parametrize(
        "attributes,fragment",
        [
            ({"capture_dir": ""}, "capture_dir"),
            ({"retention_max_files": -1}, "retention_max_files"),
            ({"live_view_max_fps": 0}, "live_view_max_fps"),
            ({"capture_timeout_s": -3}, "capture_timeout_s"),
            ({"connect_timeout_s": "soon"}, "connect_timeout_s"),
            ({"focus_tolerance": -1}, "focus_tolerance"),
            ({"binding": "usb"}, "binding"),
            ({"apply_on_connect": "f/11"}, "apply_on_connect"),
        ],
    )
    def test_rejects_bad_attributes(self, attributes, fragment):
        with pytest.raises(ValueError, match=fragment):
            Camera.validate_config(make_config(**attributes))

    def test_a_mistyped_setting_is_caught_at_config_time(self):
        # Not at capture time, hours later, in a shot nobody looks at until
        # it's on a product page.
        with pytest.raises(ValueError, match="apeture"):
            Camera.validate_config(make_config(apply_on_connect={"apeture": "f/11"}))

    def test_an_unparseable_value_is_caught_at_config_time(self):
        with pytest.raises(ValueError, match="aperture"):
            Camera.validate_config(make_config(apply_on_connect={"aperture": "wide open"}))

    def test_the_error_says_what_is_valid(self):
        with pytest.raises(ValueError, match="flash"):
            Camera.validate_config(
                make_config(apply_on_connect={"white_balance": "moonlight"})
            )


# ----------------------------------------------------------------------
# Camera API
# ----------------------------------------------------------------------


class TestCameraApi:
    async def test_get_images_returns_a_live_view_jpeg(self, camera):
        images, _ = await camera.get_images()
        assert len(images) == 1
        assert images[0].name == "live_view"
        assert images[0].mime_type == CameraMimeType.JPEG
        assert bytes(images[0].data).startswith(b"\xff\xd8")

    async def test_get_images_raises_when_the_camera_is_gone(self, camera):
        camera.fake.unplug()
        assert wait_until(lambda: not camera._session.connected)
        with pytest.raises(binding.NotConnectedError):
            await camera.get_images()

    async def test_get_properties(self, camera):
        properties = await camera.get_properties()
        assert properties.supports_pcd is False
        assert CameraMimeType.JPEG in properties.mime_types

    async def test_no_point_cloud(self, camera):
        with pytest.raises(NotImplementedError):
            await camera.get_point_cloud()

    async def test_no_geometries(self, camera):
        assert await camera.get_geometries() == []

    def test_the_model_is_a_camera(self, camera):
        assert isinstance(camera, CameraBase)
        assert str(Camera.MODEL) == "brad-grigsby:sony-remote:camera"


# ----------------------------------------------------------------------
# DoCommand - ptp compatibility
# ----------------------------------------------------------------------


class TestPtpContract:
    async def test_capture_matches_the_ptp_response_shape(self, camera):
        response = await camera.do_command({"capture": {}})
        # Nested under the command name, exactly as ptp answers - this is what
        # `source_resp.get("capture", source_resp)` in color-correction reads.
        assert "capture" in response
        result = response["capture"]
        for key in ("name", "path", "mime_type", "saved_to", "size"):
            assert key in result, f"ptp callers expect a {key!r} key"
        assert os.path.exists(result["saved_to"])

    async def test_color_correction_can_find_the_raw(self, camera):
        # The exact expression color_correction._linear_from_capture_response
        # uses to locate the file it will demosaic.
        response = await camera.do_command({"capture": {"af": True}})
        capture = response.get("capture", response)
        path = capture.get("saved_to") or capture.get("path")
        assert path and path.lower().endswith(".arw")

    async def test_the_deferred_pipeline_still_works(self, camera):
        # color-correction's `defer` path: trigger, then download by the path
        # the trigger reported. On a direct-to-host camera the download is a
        # no-op, but the sequence must still produce a `saved_to`.
        triggered = (await camera.do_command({"trigger": {}}))["trigger"]
        assert triggered["path"]

        downloaded = (
            await camera.do_command({"download": {"path": triggered["path"]}})
        )["download"]
        assert downloaded["saved_to"] == triggered["path"]
        assert downloaded["size"] > 0

    async def test_download_latest(self, camera):
        await camera.do_command({"capture": {}})
        result = (await camera.do_command({"download": {"latest": True}}))["download"]
        assert os.path.exists(result["saved_to"])

    async def test_download_needs_a_path(self, camera):
        with pytest.raises(ValueError, match="path"):
            await camera.do_command({"download": {}})

    async def test_download_of_a_file_that_is_not_here(self, camera):
        with pytest.raises(binding.CameraError, match="not on this host"):
            await camera.do_command({"download": {"path": "/nope/DSC00001.ARW"}})

    async def test_list_files(self, camera):
        await camera.do_command({"capture": {}})
        await camera.do_command({"capture": {}})
        result = (await camera.do_command({"list_files": {}}))["list_files"]
        assert result["count"] == 2
        assert len(result["files"]) == 2

    async def test_list_files_accepts_new_only_and_ignores_it(self, camera):
        # The webapp passes it; there is no card-vs-host distinction here, so
        # it's accepted rather than made into an error.
        await camera.do_command({"capture": {}})
        result = (await camera.do_command({"list_files": {"new_only": True}}))["list_files"]
        assert result["count"] == 1

    async def test_cleanup_dry_run_then_real(self, camera):
        await camera.do_command({"capture": {}})
        dry = (await camera.do_command({"cleanup": {"dry_run": True}}))["cleanup"]
        assert dry["count"] == 1
        assert dry["dry_run"] is True
        assert os.listdir(camera._session.capture_dir)

        wet = (await camera.do_command({"cleanup": {}}))["cleanup"]
        assert wet["count"] == 1
        assert (await camera.do_command({"list_files": {}}))["list_files"]["count"] == 0

    async def test_delete_accepts_both_shapes(self, camera):
        first = (await camera.do_command({"capture": {}}))["capture"]["path"]
        second = (await camera.do_command({"capture": {}}))["capture"]["path"]

        assert (await camera.do_command({"delete": {"path": first}}))["delete"]["count"] == 1
        # The webapp sends `{"delete": {"paths": [...]}}` in cleanup.ts.
        assert (
            await camera.do_command({"delete": {"paths": [second]}})
        )["delete"]["count"] == 1
        assert not os.path.exists(first)

    async def test_summary(self, camera):
        result = (await camera.do_command({"summary": {}}))["summary"]
        assert result["model"] == "ILCE-7RM5"
        assert "connected=True" in result["summary"]


# ----------------------------------------------------------------------
# DoCommand - new commands
# ----------------------------------------------------------------------


class TestNewCommands:
    async def test_focus_round_trip(self, camera):
        assert (await camera.do_command({"get_focus_position": {}})) == {
            "position": 128,
            "units": "sdk_raw",
        }

        result = await camera.do_command({"set_focus_position": {"position": 240}})
        assert result["position"] == 240
        assert result["ok"] is True

        assert (await camera.do_command({"get_focus_position": {}}))["position"] == 240

    async def test_set_focus_position_needs_an_integer(self, camera):
        with pytest.raises(ValueError, match="position"):
            await camera.do_command({"set_focus_position": {}})
        with pytest.raises(ValueError, match="position"):
            await camera.do_command({"set_focus_position": {"position": "near"}})

    async def test_autofocus_once(self, camera):
        camera.fake.autofocus_position = 190
        result = await camera.do_command({"autofocus_once": {}})
        assert result == {"position": 190, "units": "sdk_raw", "acquired": True}

    async def test_settings_round_trip(self, camera):
        settings = await camera.do_command({"get_settings": {}})
        assert settings["aperture"] == "f/11"

        updated = await camera.do_command({"set_settings": {"iso": 800}})
        assert updated["iso"] == 800

    async def test_set_settings_accepts_a_nested_block(self, camera):
        updated = await camera.do_command({"set_settings": {"settings": {"iso": 200}}})
        assert updated["iso"] == 200

    async def test_set_settings_needs_something_to_set(self, camera):
        with pytest.raises(ValueError, match="at least one"):
            await camera.do_command({"set_settings": {}})

    async def test_unsupported_setting_value_lists_the_alternatives(self, camera):
        with pytest.raises(binding.UnsupportedValueError) as exc:
            await camera.do_command({"set_settings": {"aperture": "f/45"}})
        assert "f/11" in exc.value.details["valid"]

    async def test_get_status(self, camera):
        status = await camera.do_command({"get_status": {}})
        assert status["connected"] is True
        assert status["model"] == "ILCE-7RM5"
        assert status["serial"] == "SN0000001"
        assert status["battery_pct"] == 87
        assert status["lens"] == "FE 35mm F1.8"

    async def test_get_status_works_while_disconnected(self, camera):
        camera.fake.unplug()
        assert wait_until(lambda: not camera._session.connected)
        status = await camera.do_command({"get_status": {}})
        assert status["connected"] is False
        assert status["model"] == "ILCE-7RM5"  # last known

    async def test_capture_count_persists_and_can_be_seeded(self, camera):
        await camera.do_command({"capture": {}})
        assert (await camera.do_command({"capture_count": {}}))["capture_count"] == 1

        await camera.do_command({"capture_count": {"set": 150000}})
        await camera.do_command({"capture": {}})
        assert (await camera.do_command({"capture_count": {}}))["capture_count"] == 150001


# ----------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------


class TestDispatch:
    async def test_named_command_style(self, camera):
        # The webapp uses `{"command": "..."}` against other modules on the same
        # machine; supporting both styles saves every caller from remembering
        # which is which.
        result = await camera.do_command({"command": "set_focus_position", "position": 77})
        assert result["position"] == 77
        assert (await camera.do_command({"command": "get_focus_position"}))["position"] == 77

    async def test_several_commands_in_one_call(self, camera):
        response = await camera.do_command({"get_status": {}, "list_files": {}})
        assert response["connected"] is True
        assert response["list_files"]["count"] == 0

    async def test_unknown_command(self, camera):
        with pytest.raises(ValueError, match="no recognized command"):
            await camera.do_command({"take_picture": {}})

    async def test_unknown_named_command(self, camera):
        with pytest.raises(ValueError, match="unknown command"):
            await camera.do_command({"command": "take_picture"})

    async def test_errors_carry_their_category(self, camera):
        # timeout / disconnected / unsupported-value / busy have to be
        # distinguishable by a caller that only sees the message string.
        camera.fake.unplug()
        assert wait_until(lambda: not camera._session.connected)
        with pytest.raises(binding.CameraError) as exc:
            await camera.do_command({"capture": {}})
        assert str(exc.value).startswith("[disconnected]")
