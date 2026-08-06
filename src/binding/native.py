"""
binding/native.py
-----------------
`CameraBinding` on top of the `_crsdk` pybind11 extension (`native/crsdk_ext.cpp`).

There is deliberately almost nothing here. The extension already speaks in
symbolic property names, plain dicts and bytes, so this file's whole job is:

* import `_crsdk` lazily and turn "it isn't built / the .so isn't on the loader
  path" into one actionable error instead of an ImportError traceback;
* translate the extension's flat error strings into the typed exceptions in
  `interface.py`;
* translate event dicts into `Event`.

Anything more interesting than that belongs above this file (in `session.py`)
or below it (in the extension). The reason to keep this file boring is that it
is the one Python file that cannot be tested without hardware.

Error convention with the extension: every failure raises `RuntimeError` whose
message is ``category|code|text``, where `category` is one of
``disconnected``, ``busy``, ``unsupported``, ``configuration``, ``sdk`` and
`code` is the raw `CrError` value (0 when there isn't one). A flat string
rather than a custom exception type keeps the C++ side to one helper and
survives the pybind11 exception translation without a registered type.
"""

import threading
from typing import Any, Dict, List, Optional

from .interface import (
    BusyError,
    CameraBinding,
    CameraError,
    ConfigurationError,
    DeviceInfo,
    Event,
    NotConnectedError,
    PropertyValue,
    SDKError,
    UnsupportedValueError,
)

_ERROR_CLASSES = {
    "disconnected": NotConnectedError,
    "busy": BusyError,
    "unsupported": UnsupportedValueError,
    "configuration": ConfigurationError,
    "sdk": SDKError,
}

_IMPORT_HINT = (
    "the `_crsdk` extension is not importable. Build it with `make ext` after "
    "setting CRSDK_ROOT to the extracted Sony Camera Remote SDK, and make sure "
    "the SDK's shared libraries are on the loader path (LD_LIBRARY_PATH on "
    "Linux, DYLD_LIBRARY_PATH on macOS) - see README.md, 'SDK acquisition'"
)


def _translate(exc: BaseException) -> CameraError:
    """Map an extension error onto one of our typed exceptions."""
    text = str(exc)
    parts = text.split("|", 2)
    if len(parts) == 3 and parts[0] in _ERROR_CLASSES:
        category, code, message = parts
        return _ERROR_CLASSES[category](message, sdk_code=code)
    # Anything not following the convention is still a real failure; report it
    # rather than swallowing it into a misleading category.
    return SDKError(text)


class NativeCamera(CameraBinding):
    """The real thing. All calls land on the session's owner thread."""

    def __init__(self):
        self._crsdk = None
        # The extension's event queue is fed from SDK callback threads, but the
        # calls below are single-threaded by contract. This lock exists only to
        # make `release()` safe to call from a teardown path that races the
        # owner thread's last `poll_event`.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def _ext(self):
        if self._crsdk is None:
            try:
                import _crsdk  # type: ignore
            except ImportError as exc:
                raise ConfigurationError(f"{_IMPORT_HINT} ({exc})") from exc
            self._crsdk = _crsdk
        return self._crsdk

    def _call(self, name: str, *args):
        fn = getattr(self._ext(), name)
        try:
            return fn(*args)
        except CameraError:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed error
            raise _translate(exc) from exc

    # -- lifecycle -----------------------------------------------------

    def init(self) -> None:
        self._call("init")

    def release(self) -> None:
        with self._lock:
            if self._crsdk is None:
                return
            try:
                self._crsdk.release()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass

    def enumerate(self) -> List[DeviceInfo]:
        return [
            DeviceInfo(
                model=str(d.get("model", "")),
                serial=str(d.get("serial", "")),
                id=d.get("index"),
            )
            for d in self._call("enumerate")
        ]

    def connect(self, device: DeviceInfo, timeout_s: float) -> None:
        self._call("connect", int(device.id), int(timeout_s * 1000))

    def disconnect(self) -> None:
        if self._crsdk is None:
            return
        try:
            self._crsdk.disconnect()
        except Exception:  # noqa: BLE001 - teardown must not raise
            pass

    def is_connected(self) -> bool:
        if self._crsdk is None:
            return False
        return bool(self._call("is_connected"))

    # -- device state --------------------------------------------------

    def device_info(self) -> Dict[str, Any]:
        return dict(self._call("device_info"))

    def set_save_destination(self, directory: str) -> None:
        # Prefix and start number are the SDK's own file-naming controls; we
        # leave the body's own naming alone (empty prefix, -1 = "keep going"),
        # so filenames stay consistent with what the camera writes to card.
        self._call("set_save_info", directory, "", -1)

    # -- properties ----------------------------------------------------

    def get_property(self, name: str) -> PropertyValue:
        raw = self._call("get_property", name)
        return PropertyValue(
            value=raw.get("value"),
            choices=list(raw.get("choices") or []),
            writable=bool(raw.get("writable", True)),
        )

    def set_property(self, name: str, value: Any) -> None:
        self._call("set_property", name, value)

    # -- imaging -------------------------------------------------------

    def live_view_jpeg(self) -> Optional[bytes]:
        data = self._call("live_view_jpeg")
        return bytes(data) if data else None

    def trigger_capture(self) -> None:
        self._call("trigger_capture")

    def autofocus_once(self, timeout_s: float) -> bool:
        return bool(self._call("autofocus_once", int(timeout_s * 1000)))

    # -- events --------------------------------------------------------

    def poll_event(self, timeout_s: float) -> Optional[Event]:
        raw = self._call("poll_event", int(max(0.0, timeout_s) * 1000))
        if not raw:
            return None
        kind = str(raw.pop("kind", ""))
        return Event(kind, dict(raw))
