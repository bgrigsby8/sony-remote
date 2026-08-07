"""
binding/interface.py
--------------------
The contract between this module and the Sony Camera Remote SDK ("CrSDK").

Everything above this file is pure Python and knows nothing about Sony's C++
API; everything below it is either the ``_crsdk`` pybind11 extension
(``native.py``) or the simulated device (``fake.py``). The whole module -
capture flow, focus retry, reconnect state machine, retention - is exercised in
tests against ``FakeCamera``, which is only possible because the seam is here
and it is narrow.

Three rules keep the seam narrow:

1. **One method per SDK operation, no business logic.** Retry, tolerance
   checking, timeouts and unit conversion all live in `session.py` /
   `settings.py`. If a method here needs a policy decision to implement, the
   policy belongs above it.

2. **Every method is blocking, and every method is called from exactly one
   thread** - the session's owner thread. Implementations do not need to be
   thread-safe beyond `poll_event`, which is fed by SDK callback threads.

3. **Callbacks never call back into the SDK.** CrSDK delivers connection,
   capture-complete and property-change notifications on its own threads.
   Implementations translate those into `Event` objects on an internal
   thread-safe queue and hand them over via `poll_event`. Nothing above this
   file ever runs on an SDK thread, and nothing on an SDK thread ever re-enters
   the SDK - which is the deadlock CrSDK's docs warn about.

Property names are symbolic strings (``"f_number"``, ``"focus_position"``),
never Sony's numeric ``CrDeviceProperty_*`` codes. The mapping from these names
to SDK enum values lives in `native.py`/`crsdk_module.cpp`, where the real
headers are available; keeping the codes out of Python means the parts we can't
compile-check are confined to one file.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# ----------------------------------------------------------------------
# Structured errors
#
# §6 of the scope: "timeout vs. disconnected vs. unsupported-value vs. busy are
# distinguishable by the caller". A Viam DoCommand fault reaches the client as a
# message string and nothing else, so the discriminator has to survive as text:
# every message is prefixed `[code]`. Callers that want to branch match the
# prefix; callers that just log get a message that says what happened first.
# ----------------------------------------------------------------------


class CameraError(Exception):
    """Base for every failure this module reports. Carries a stable `code`."""

    code = "camera_error"

    def __init__(self, message: str, **details: Any):
        self.details: Dict[str, Any] = details
        super().__init__(f"[{self.code}] {message}")

    @property
    def message(self) -> str:
        """The message without the `[code]` prefix."""
        return str(self).split("] ", 1)[-1]


class NotConnectedError(CameraError):
    """No camera attached, or it vanished mid-operation."""

    code = "disconnected"


class CaptureTimeoutError(CameraError):
    """The shutter fired but the file never landed within the deadline."""

    code = "timeout"


class UnsupportedValueError(CameraError):
    """A property value this body won't take.

    Always constructed with `valid=[...]` where the camera told us what it
    would accept - `set_settings` surfaces that list to the caller, so an
    operator fixing a config doesn't have to go read Sony's PDF.
    """

    code = "unsupported_value"

    def __init__(self, message: str, valid: Optional[Sequence[Any]] = None, **details: Any):
        super().__init__(message, valid=list(valid or []), **details)


class BusyError(CameraError):
    """The camera refused because it's mid-write / mid-AF. Retryable."""

    code = "busy"


class SDKError(CameraError):
    """CrSDK returned an error code we have no better name for."""

    code = "sdk_error"


class ConfigurationError(CameraError):
    """The rig is wrong, not the camera: no camera matching `serial`, two
    cameras and no `serial` to pick between them, missing SDK libraries.

    Distinct from `disconnected` because retrying doesn't help until a human
    changes something - but the reconnect loop keeps running anyway, so
    unplugging the second body fixes it without a viam-server restart.
    """

    code = "configuration"


# ----------------------------------------------------------------------
# Events
# ----------------------------------------------------------------------

EVENT_CONNECTED = "connected"
EVENT_DISCONNECTED = "disconnected"
EVENT_CAPTURE_COMPLETE = "capture_complete"
EVENT_FILE_WRITTEN = "file_written"
EVENT_PROPERTY_CHANGED = "property_changed"
EVENT_WARNING = "warning"


@dataclass(frozen=True)
class Event:
    """One asynchronous notification from the camera.

    `kind` is one of the EVENT_* constants; unknown kinds are logged and
    dropped rather than raising, so a firmware that grows a new notification
    doesn't take the module down.
    """

    kind: str
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeviceInfo:
    """One camera as seen by SDK enumeration, before connecting to it."""

    model: str
    serial: str
    # Opaque handle/index the implementation uses to open this specific body.
    # Never interpreted above this layer.
    id: Any = None


@dataclass(frozen=True)
class PropertyValue:
    """A property read.

    `choices` is what the *camera* says it will accept right now, which is
    narrower than what the model supports in general (aperture choices depend
    on the mounted lens; shutter speed depends on the exposure mode). Empty
    means the camera didn't tell us - not "nothing is valid".
    """

    value: Any
    choices: List[Any] = field(default_factory=list)
    writable: bool = True


class CameraBinding(ABC):
    """One process, one camera. See the module docstring for the threading
    contract - implementations may assume single-threaded access to everything
    except `poll_event`'s producer side."""

    # -- lifecycle -----------------------------------------------------

    @abstractmethod
    def init(self) -> None:
        """Initialise the SDK. Paired strictly with `release()`.

        Idempotent: calling it twice is a no-op, not an error. CrSDK's global
        init is process-wide and leaving it un-released is what forces an
        operator to power-cycle the body after a viam-server restart.
        """

    @abstractmethod
    def release(self) -> None:
        """Disconnect if connected, then release the SDK. Must not raise."""

    @abstractmethod
    def enumerate(self) -> List[DeviceInfo]:
        """Every Sony camera currently on USB. May be empty."""

    @abstractmethod
    def connect(self, device: DeviceInfo, timeout_s: float) -> None:
        """Open a remote-control session with one enumerated camera."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the session. Must not raise - it runs on teardown paths."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Whether the SDK still believes the session is live."""

    # -- device state --------------------------------------------------

    @abstractmethod
    def device_info(self) -> Dict[str, Any]:
        """`{"model", "serial", "battery_pct", "lens"}`.

        `battery_pct` and `lens` are None when the body doesn't report them
        (some lenses have no electrical contacts to identify themselves).
        """

    @abstractmethod
    def set_save_destination(self, directory: str) -> None:
        """Point the SDK's still-image save destination at a host directory.

        This is what makes captures land on the machine running viam-server
        instead of on the card, which is the whole reason the capture response
        can hand back a path the next component can open.
        """

    # -- properties ----------------------------------------------------

    @abstractmethod
    def get_property(self, name: str) -> PropertyValue:
        """Read one property by symbolic name.

        Raises `UnsupportedValueError` if this body has no such property, so
        `get_settings` can skip it rather than fail the whole read.
        """

    @abstractmethod
    def set_property(self, name: str, value: Any) -> None:
        """Write one property. Raises `UnsupportedValueError` (with `valid`)
        when the camera rejects the value."""

    # -- imaging -------------------------------------------------------

    @abstractmethod
    def live_view_jpeg(self) -> Optional[bytes]:
        """Latest live-view frame as JPEG bytes, or None if none is ready yet.

        None is a normal answer (the body needs a moment after connecting);
        callers retry rather than treat it as an error.
        """

    @abstractmethod
    def trigger_capture(self) -> None:
        """Fire the shutter and return immediately.

        Completion is *not* signalled by this returning - it arrives later as
        `capture_complete` / `file_written` events. Waiting on the events is
        the only way to know the file exists; sleeping a fixed delay is wrong
        for the same reason it was wrong on the Canon (see `ptp.py`).
        """

    @abstractmethod
    def autofocus_once(self, timeout_s: float) -> bool:
        """One-shot AF: half-press, wait for focus-acquired, release.

        Returns whether focus was acquired. The half-press is always released,
        including on timeout - a body left holding S1 stops accepting most
        other commands.
        """

    def dump_properties(self) -> List[Dict[str, Any]]:
        """Diagnostic: the body's whole property table, raw.

        Each entry has at least `code` and `value` (plus whatever else the
        implementation knows). Not part of the operating contract - it exists
        because bring-up keeps coming down to "which session-side property is
        in a state the camera's menu doesn't show". Default: nothing to report.
        """
        return []

    # -- events --------------------------------------------------------

    @abstractmethod
    def poll_event(self, timeout_s: float) -> Optional[Event]:
        """Pop one queued event, waiting up to `timeout_s`. None on timeout.

        The only method whose producer side runs on SDK threads.
        """
