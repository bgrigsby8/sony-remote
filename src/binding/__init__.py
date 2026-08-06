"""
binding
-------
The swappable seam between the Viam module and Sony's C++ SDK.

`interface.py` defines the contract, `native.py` implements it against the
`_crsdk` pybind11 extension, `fake.py` implements it in memory. Nothing above
this package imports `native` or `fake` directly - they ask `make_binding()`
for one, which is what lets the test suite run the entire module against a
simulated body.
"""

from typing import Optional

from .interface import (
    EVENT_CAPTURE_COMPLETE,
    EVENT_CONNECTED,
    EVENT_DISCONNECTED,
    EVENT_FILE_WRITTEN,
    EVENT_PROPERTY_CHANGED,
    EVENT_WARNING,
    BusyError,
    CameraBinding,
    CameraError,
    CaptureTimeoutError,
    ConfigurationError,
    DeviceInfo,
    Event,
    NotConnectedError,
    PropertyValue,
    SDKError,
    UnsupportedValueError,
)

__all__ = [
    "CameraBinding",
    "CameraError",
    "BusyError",
    "CaptureTimeoutError",
    "ConfigurationError",
    "DeviceInfo",
    "Event",
    "NotConnectedError",
    "PropertyValue",
    "SDKError",
    "UnsupportedValueError",
    "EVENT_CAPTURE_COMPLETE",
    "EVENT_CONNECTED",
    "EVENT_DISCONNECTED",
    "EVENT_FILE_WRITTEN",
    "EVENT_PROPERTY_CHANGED",
    "EVENT_WARNING",
    "make_binding",
]


def make_binding(kind: Optional[str] = None) -> CameraBinding:
    """Build the binding named by `kind` ("native" or "fake").

    Defaults to "native"; `fake` exists for tests and for bringing up the
    module (config, webapp wiring, color-correction plumbing) before the
    hardware arrives. Setting `"binding": "fake"` in the component config is a
    supported, documented thing to do - it produces synthetic files in
    `capture_dir` and answers every DoCommand.

    The import of `fake` is local so that the native path never drags the
    simulator into a production process, and vice versa: a machine with no
    `_crsdk` built can still run the fake.
    """
    kind = (kind or "native").lower()
    if kind == "native":
        from .native import NativeCamera

        return NativeCamera()
    if kind == "fake":
        from .fake import FakeCamera

        return FakeCamera()
    raise ConfigurationError(f"unknown binding {kind!r}; expected 'native' or 'fake'")
