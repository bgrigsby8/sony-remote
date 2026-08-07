"""
Binding-layer tests.

The native binding needs the SDK and the camera, so it can't be exercised here -
but the two things about it that *can* go wrong without hardware are covered:
the interface it must implement, and the error translation that turns the
extension's flat error strings into the typed exceptions everything upstream
branches on. Both are pure Python.

The rest of the native path is deliberately thin enough to be covered by the
hardware smoke checklist.
"""

import inspect
import sys

import pytest

from binding import (
    BusyError,
    CameraBinding,
    CameraError,
    ConfigurationError,
    NotConnectedError,
    SDKError,
    UnsupportedValueError,
    make_binding,
)
from binding.fake import FakeCamera
from binding.native import NativeCamera, _translate


class TestInterfaceCompliance:
    """Both implementations must cover the whole contract.

    This is what makes `binding: "fake"` a real thing an operator can configure
    rather than a test-only shortcut - and it catches a method added to the
    interface but not to the simulator, which would otherwise show up as an
    AttributeError in the middle of a sweep.
    """

    @pytest.mark.parametrize("implementation", [FakeCamera, NativeCamera])
    def test_implements_every_abstract_method(self, implementation):
        missing = [
            name
            for name in CameraBinding.__abstractmethods__
            if getattr(implementation, name, None) is getattr(CameraBinding, name, None)
        ]
        assert missing == []

    @pytest.mark.parametrize("implementation", [FakeCamera, NativeCamera])
    def test_signatures_match_the_interface(self, implementation):
        for name in CameraBinding.__abstractmethods__:
            expected = inspect.signature(getattr(CameraBinding, name))
            actual = inspect.signature(getattr(implementation, name))
            assert actual.parameters.keys() == expected.parameters.keys(), name

    @pytest.mark.parametrize("implementation", [FakeCamera, NativeCamera])
    def test_is_instantiable(self, implementation):
        assert isinstance(implementation(), CameraBinding)


class TestErrorTranslation:
    """`category|code|text` is the extension's whole error protocol."""

    @pytest.mark.parametrize(
        "category,cls",
        [
            ("disconnected", NotConnectedError),
            ("busy", BusyError),
            ("unsupported", UnsupportedValueError),
            ("configuration", ConfigurationError),
            ("sdk", SDKError),
        ],
    )
    def test_each_category_maps_to_its_type(self, category, cls):
        error = _translate(RuntimeError(f"{category}|32770|Connect failed"))
        assert isinstance(error, cls)
        assert "Connect failed" in str(error)
        assert error.details["sdk_code"] == "32770"

    def test_the_code_prefix_survives(self):
        # A Viam client only ever sees the message string, so the discriminator
        # has to be in the text.
        assert str(_translate(RuntimeError("busy|0|mid-write"))).startswith("[busy]")

    def test_an_unconventional_message_is_still_a_real_failure(self):
        # Never swallowed into a misleading category, and never lost.
        error = _translate(RuntimeError("Segmentation fault in Cr_Core"))
        assert isinstance(error, SDKError)
        assert "Cr_Core" in str(error)


class TestMakeBinding:
    def test_fake(self):
        assert isinstance(make_binding("fake"), FakeCamera)

    def test_native_is_the_default(self):
        # Constructing it must not import `_crsdk` - a machine with no extension
        # built still has to configure, so the failure surfaces in get_status
        # rather than at import time.
        assert isinstance(make_binding(), NativeCamera)
        assert isinstance(make_binding("native"), NativeCamera)

    def test_unknown_kind(self):
        with pytest.raises(ConfigurationError):
            make_binding("gphoto2")


class TestNativeWithoutTheSdk:
    def test_a_missing_extension_is_an_actionable_configuration_error(self, monkeypatch):
        # Force the import to fail even on a machine where `make ext` has been
        # run (None in sys.modules makes `import _crsdk` raise ImportError) -
        # what's under test is the error message, not the machine's state.
        monkeypatch.setitem(sys.modules, "_crsdk", None)
        camera = NativeCamera()
        with pytest.raises(ConfigurationError) as exc:
            camera.init()
        message = str(exc.value)
        assert "CRSDK_ROOT" in message
        assert "make ext" in message

    def test_release_is_safe_before_anything_was_built(self):
        # `close()` runs on teardown paths that may never have connected.
        NativeCamera().release()

    def test_is_connected_is_false_rather_than_an_error(self):
        assert NativeCamera().is_connected() is False


class TestFakeCamera:
    def test_unplug_queues_a_disconnect_event(self, fake):
        fake.init()
        fake.connect(fake.enumerate()[0], 1.0)
        fake.unplug()
        assert fake.poll_event(0.1).kind == "disconnected"
        assert fake.is_connected() is False

    def test_calls_after_an_unplug_raise(self, fake):
        fake.init()
        fake.connect(fake.enumerate()[0], 1.0)
        fake.unplug()
        with pytest.raises(NotConnectedError):
            fake.live_view_jpeg()

    def test_connect_flushes_stale_events(self, fake):
        # Otherwise a `disconnected` queued while we were away would tear down
        # the session we just built.
        fake.init()
        fake.push_event("disconnected", reason="stale")
        fake.connect(fake.enumerate()[0], 1.0)
        assert fake.poll_event(0.05) is None

    def test_a_property_the_body_lacks_is_unsupported_not_a_key_error(self, fake):
        fake.init()
        fake.connect(fake.enumerate()[0], 1.0)
        with pytest.raises(UnsupportedValueError):
            fake.get_property("shutter_count")

    def test_a_value_the_body_rejects_carries_the_valid_list(self, fake):
        fake.init()
        fake.connect(fake.enumerate()[0], 1.0)
        with pytest.raises(UnsupportedValueError) as exc:
            fake.set_property("f_number", 4500)
        assert 1100 in exc.value.details["valid"]

    def test_release_is_idempotent(self, fake):
        fake.init()
        fake.release()
        fake.release()
        assert fake.released is True


def test_every_error_type_carries_a_stable_code():
    # The codes are API: callers match on the `[code]` prefix.
    codes = {
        CameraError: "camera_error",
        NotConnectedError: "disconnected",
        BusyError: "busy",
        UnsupportedValueError: "unsupported_value",
        ConfigurationError: "configuration",
        SDKError: "sdk_error",
    }
    for cls, code in codes.items():
        assert cls("boom").code == code
        assert str(cls("boom")) == f"[{code}] boom"
        assert cls("boom").message == "boom"
