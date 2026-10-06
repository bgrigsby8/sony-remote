"""
binding/fake.py
---------------
A simulated A7R V, good enough that every layer above `CameraBinding` can be
tested with no hardware, no SDK and no USB.

It models the four behaviours that make the real camera awkward to program
against, because those are the ones the module exists to handle:

* **Capture is asynchronous and multi-part.** `trigger_capture` returns at once;
  the file appears later, announced by `capture_complete` and one
  `file_written` per file. In RAW+JPEG the body writes *two* files, and their
  order is not guaranteed.
* **A body may not tell you the path.** `emit_file_path=False` reproduces the
  case where `file_written` carries no name and the only way to find the still
  is to diff the save directory - the failure mode `ptp.py` learned the hard
  way on Canon.
* **A file is visible before it is complete.** `slow_write_s` writes a short
  prefix, then the rest after a delay, so the "wait for the size to settle"
  path is actually exercised.
* **Zoom is a motor, not a register.** `zoom_operation` starts a continuous
  drive that runs until a 0 write, the lens coasts a little after the stop,
  and the focal length reads back on a coarser grid than the body advertises
  (the FE PZ 16-35 says 0.1mm, reports 0.5mm) - the three things the
  closed-loop `set_zoom` has to cope with.
* **Focus doesn't land where you put it.** `focus_error_sequence` makes the
  first `set_focus_position` miss and the second land, which is the whole
  reason `set_focus_position` reads back and retries.
* **The directory is not yours alone.** `foreign_files` drops files nobody
  announces into the save directory just before the shot lands - the
  `color-correction` export that a production machine mistook for its capture.

Test knobs are plain attributes, settable at any time - a test can `unplug()`
mid-capture and watch the session fail fast.
"""

import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional

from .interface import (
    EVENT_CAPTURE_COMPLETE,
    EVENT_DISCONNECTED,
    EVENT_FILE_WRITTEN,
    EVENT_PROPERTY_CHANGED,
    BusyError,
    CameraBinding,
    DeviceInfo,
    Event,
    NotConnectedError,
    PropertyValue,
    UnsupportedValueError,
)

# Bytes written for a simulated still. Small enough to keep the suite fast,
# large enough that a truncated prefix is distinguishable from a whole file.
_RAW_BYTES = b"II*\x00" + b"\xa5" * 4096
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x5a" * 1024

# One live-view frame. A real JPEG header so anything downstream that sniffs the
# magic bytes (Viam's image handling, a browser preview) doesn't choke.
_LIVE_VIEW_FRAME = b"\xff\xd8\xff\xdb" + b"\x77" * 512

# Defaults chosen to match the A7R V's reported ranges closely enough that
# tolerance and rounding logic gets a realistic workout. Focus is the SDK's raw
# 0-255 scale (near to far); see `settings.py` on why nothing converts it.
_DEFAULT_PROPERTIES: Dict[str, Dict[str, Any]] = {
    "f_number": {"value": 1100, "choices": [180, 280, 400, 560, 800, 1100, 1600, 2200]},
    "shutter_speed": {
        "value": (1 << 16) | 160,
        "choices": [(1 << 16) | d for d in (60, 100, 125, 160, 200, 250, 500, 1000)],
    },
    "iso_sensitivity": {"value": 100, "choices": [100, 200, 400, 800, 1600, 0x00FFFFFF]},
    "white_balance": {
        "value": "AWB",
        "choices": ["AWB", "Daylight", "Shade", "Cloudy", "Incandescent", "Flash"],
    },
    "shutter_type": {"value": "Mechanical", "choices": ["Auto", "Mechanical", "Electronic"]},
    "still_file_format": {"value": "RAW", "choices": ["RAW", "RAW_JPEG", "JPEG"]},
    "focus_mode": {"value": "MF", "choices": ["AF_S", "AF_C", "DMF", "MF"]},
    "focus_position": {"value": 128, "choices": []},
    # Real bodies boot owning their own settings; remote sets bounce until the
    # session takes PCRemote priority (which it does right after connect).
    "priority_key": {"value": "CameraPosition", "choices": ["CameraPosition", "PCRemote"]},
    # The SDK session acts on this property regardless of the camera menu's
    # equivalent; direct-to-host saving requires HostPC.
    "store_destination": {
        "value": "MemoryCard",
        "choices": ["HostPC", "MemoryCard", "HostPCAndMemoryCard"],
    },
    # Relative focus drive (signed Int16 on the wire, two's complement).
    # Setting it moves focus_position; see set_property.
    "near_far": {"value": 0, "choices": []},
    # Raw CrExposureProgram: 0x1 is stills M, 0x8053 Movie M.
    "exposure_program_mode": {"value": 0x1, "choices": []},
    # Movie-mode follow focus: 1 while the lens publishes its position (movie
    # mode only), and that position (0xFFFF near stop .. 0 far stop), live in
    # movie mode and frozen at its last movie value in stills. Both computed
    # on read; see get_property.
    "lens_info_enable": {"value": 0, "choices": []},
    "follow_focus_position": {"value": 0, "choices": []},
    # Power zoom, modelled on an FE PZ 16-35mm F4 G. zoom_distance and
    # zoom_speed_range are Range properties: choices are {min, max, step}.
    # zoom_distance's value is computed from the drive model on every read.
    "zoom_distance": {"value": 16000, "choices": [16000, 35000, 100], "range": True},
    "zoom_speed_range": {"value": 0, "choices": [-8, 8, 1], "range": True},
    "zoom_operation": {"value": 0, "choices": [-8, 8, 1], "range": True},
    "zoom_operation_status": {"value": 1, "choices": [0, 1]},
    "zoom_type_status": {"value": 1, "choices": []},
    "zoom_scale": {"value": 1000, "choices": []},
    "zoom_bar": {"value": 0x01000000, "choices": []},
    "zoom_focus_preset_save": {"value": 0, "choices": []},
    "zoom_focus_preset_load": {"value": 0, "choices": []},
}


class FakeCamera(CameraBinding):
    """In-memory stand-in for one A7R V on USB."""

    def __init__(
        self,
        model: str = "ILCE-7RM5",
        serial: str = "SN0000001",
        present: bool = True,
        capture_delay_s: float = 0.02,
        emit_file_path: bool = True,
        slow_write_s: float = 0.0,
        drop_capture: bool = False,
    ):
        self.model = model
        self.serial = serial

        # -- test knobs ------------------------------------------------
        # Whether the body is on the bus at all. Drives `enumerate`, so a test
        # can model "camera unplugged, module retrying".
        self.present = present
        # Wall time between the shutter firing and the file appearing.
        self.capture_delay_s = capture_delay_s
        # False -> `file_written` arrives with no path, forcing the directory
        # diff fallback.
        self.emit_file_path = emit_file_path
        # >0 -> the file appears truncated, then completes after this long.
        self.slow_write_s = slow_write_s
        # True -> the shutter "fires" but nothing is ever written. Timeout path.
        self.drop_capture = drop_capture
        # Each `set_property("focus_position", n)` lands this far off target,
        # consuming one entry per call. Runs off the end -> perfect from then on.
        self.focus_error_sequence: List[int] = []
        # Whether one-shot AF succeeds, and where it leaves focus.
        self.autofocus_succeeds = True
        self.autofocus_position = 140
        # Set to raise BusyError from the next set_property / trigger_capture.
        self.busy_once = False
        # Basenames another program "writes" into the save directory after the
        # shutter fires and before the body's own files land, with no event
        # for any of them. Their mtime is backdated by `foreign_file_age_s` so
        # that "newest wins" is decidable when a test wants it to be.
        self.foreign_files: List[str] = []
        self.foreign_file_age_s = 1.0
        # near_far modelling: units of focus_position moved per unit of step
        # magnitude, and the travel stops the drive clamps against.
        self.near_far_units_per_step = 2
        self.focus_min = 0
        self.focus_max = 500
        # False models the ILCE-7RM5 over USB: the near/far drive works but
        # the absolute focus_position property is never reported, so the
        # session's emulation layer takes over. The internal focus_position
        # value keeps tracking the physical truth for assertions.
        self.absolute_focus_supported = True
        # Movie-mode follow focus, modelled on the ILCE-7RM5: switching
        # `exposure_program_mode` to a movie value makes `follow_focus_position`
        # track the lens. False -> the lens never publishes it (lens_info_enable
        # stays 0). `mode_switch_works=False` -> the body accepts mode writes
        # and ignores them.
        self.movie_focus_supported = True
        self.mode_switch_works = True
        # False models the lens's AF/MF switch on MF: near_far reads as not
        # writable, and writes are accepted and ignored - the lens never moves.
        self.near_far_enabled = True
        self._follow_latched = 0
        # Zoom drive model: focal length moves `zoom_um_per_s_per_speed` per
        # second per unit of speed, clamped to the lens's range, keeps moving
        # for `zoom_coast_s` after a stop, and reads back rounded to
        # `zoom_report_quantum_um`. `power_zoom=False` models a lens with no
        # zoom motor (Zoom_Operation_Status Disable, no focal length).
        self.power_zoom = True
        self.zoom_um_per_s_per_speed = 2400
        self.zoom_coast_s = 0.02
        self.zoom_report_quantum_um = 500
        self.zoom_presets: Dict[int, int] = {}
        # Extra cameras `enumerate` should report, to test the ambiguity guard.
        self.extra_devices: List[DeviceInfo] = []

        # -- observable history ----------------------------------------
        self.connects = 0
        self.disconnects = 0
        # The DeviceInfo the session picked, so a test can check that `serial`
        # selection chose the right body out of several.
        self.connected_device: Optional[DeviceInfo] = None
        self.triggers = 0
        self.live_view_calls = 0
        self.property_writes: List[tuple] = []
        self.save_destination: Optional[str] = None
        self.released = False
        self.initialized = False

        self._connected = False
        self._events: "queue.Queue[Event]" = queue.Queue()
        self._timers: List[threading.Timer] = []
        self._seq = 0
        self._battery = 87
        self._properties = {k: dict(v) for k, v in _DEFAULT_PROPERTIES.items()}
        # Zoom drive state: true position (um), speed, and when they were set.
        self._zoom_um = 16000.0
        self._zoom_speed = 0
        self._zoom_since = time.monotonic()
        self._zoom_coast_until = 0.0
        self._zoom_coast_speed = 0

    # ------------------------------------------------------------------
    # Test-side controls
    # ------------------------------------------------------------------

    def unplug(self) -> None:
        """Yank the cable: session drops, a `disconnected` event is queued, and
        every subsequent call raises until `plug_in()`."""
        self.present = False
        if self._connected:
            self._connected = False
            self.disconnects += 1
            self._events.put(Event(EVENT_DISCONNECTED, {"reason": "usb"}))

    def plug_in(self) -> None:
        self.present = True

    def push_event(self, kind: str, **data: Any) -> None:
        """Inject an arbitrary event, for the paths no knob covers."""
        self._events.put(Event(kind, data))

    def set_battery(self, pct: Optional[int]) -> None:
        self._battery = pct

    def property_value(self, name: str) -> Any:
        """Peek at a raw property without going through the module."""
        return self._properties[name]["value"]

    def in_movie_mode(self) -> bool:
        return self._properties["exposure_program_mode"]["value"] >= 0x8050

    def follow_focus_now(self) -> int:
        """What the body would report as FollowFocusPositionCurrentValue for
        the lens's physical position: 0xFFFF at the near stop, 0 at far."""
        span = max(1, self.focus_max - self.focus_min)
        physical = self._properties["focus_position"]["value"] - self.focus_min
        return 0xFFFF - int(round(physical * 0xFFFF / span))

    def zoom_true_um(self) -> float:
        """The simulated lens's exact focal length, for assertions."""
        self._advance_zoom()
        return self._zoom_um

    def zoom_is_driving(self) -> bool:
        return self._zoom_speed != 0

    def _advance_zoom(self) -> None:
        now = time.monotonic()
        lo, hi = self._properties["zoom_distance"]["choices"][:2]
        if self._zoom_speed:
            moved = self._zoom_speed * self.zoom_um_per_s_per_speed * (now - self._zoom_since)
        elif self._zoom_coast_until > self._zoom_since:
            coast_end = min(now, self._zoom_coast_until)
            moved = self._zoom_coast_speed * self.zoom_um_per_s_per_speed * max(
                0.0, coast_end - self._zoom_since
            )
        else:
            moved = 0.0
        self._zoom_um = max(lo, min(hi, self._zoom_um + moved))
        self._zoom_since = now

    def _set_zoom_speed(self, speed: int) -> None:
        self._advance_zoom()
        if speed == 0 and self._zoom_speed:
            self._zoom_coast_speed = self._zoom_speed
            self._zoom_coast_until = time.monotonic() + self.zoom_coast_s
        self._zoom_speed = speed

    def cancel_timers(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()

    # ------------------------------------------------------------------
    # CameraBinding - lifecycle
    # ------------------------------------------------------------------

    def init(self) -> None:
        self.initialized = True
        self.released = False

    def release(self) -> None:
        self.cancel_timers()
        self._connected = False
        self.initialized = False
        self.released = True

    def enumerate(self) -> List[DeviceInfo]:
        found = list(self.extra_devices)
        if self.present:
            found.insert(0, DeviceInfo(model=self.model, serial=self.serial, id=0))
        return found

    def connect(self, device: DeviceInfo, timeout_s: float) -> None:
        if not self.present:
            raise NotConnectedError(f"no camera at {device.serial}")
        self.connects += 1
        self.connected_device = device
        self._connected = True
        # A real connect flushes whatever the SDK queued while we were away;
        # not doing so would let a stale `disconnected` immediately tear down
        # the session we just built.
        self._drain()

    def disconnect(self) -> None:
        self.cancel_timers()
        if self._connected:
            self.disconnects += 1
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # CameraBinding - device state
    # ------------------------------------------------------------------

    def device_info(self) -> Dict[str, Any]:
        self._require_connected()
        return {
            "model": self.model,
            "serial": self.serial,
            "battery_pct": self._battery,
            "lens": "FE 35mm F1.8",
        }

    def set_save_destination(self, directory: str) -> None:
        self._require_connected()
        os.makedirs(directory, exist_ok=True)
        self.save_destination = directory

    def dump_properties(self) -> List[Dict[str, Any]]:
        self._require_connected()
        # Codes are symbolic here where the real body reports numeric ones -
        # this is a diagnostic view, not a wire format.
        return [
            {"code": name, "value": prop["value"], "enable": 1}
            for name, prop in self._properties.items()
        ]

    # ------------------------------------------------------------------
    # CameraBinding - properties
    # ------------------------------------------------------------------

    def get_property(self, name: str) -> PropertyValue:
        self._require_connected()
        if name == "focus_position" and not self.absolute_focus_supported:
            raise UnsupportedValueError("this body does not report property 'focus_position'")
        if name.startswith("zoom") and not self.power_zoom and name not in (
            "zoom_operation_status",
        ):
            raise UnsupportedValueError(f"this body does not report property {name!r}")
        prop = self._properties.get(name)
        if prop is None:
            raise UnsupportedValueError(f"this body has no property {name!r}")
        value = prop["value"]
        if name == "zoom_distance":
            self._advance_zoom()
            quantum = max(1, int(self.zoom_report_quantum_um))
            value = int(round(self._zoom_um / quantum) * quantum)
        elif name == "zoom_operation_status" and not self.power_zoom:
            value = 0
        elif name == "lens_info_enable":
            value = 1 if self.movie_focus_supported and self.in_movie_mode() else 0
        elif name == "follow_focus_position":
            if not self.movie_focus_supported:
                raise UnsupportedValueError(f"this body does not report property {name!r}")
            if self.in_movie_mode():
                self._follow_latched = self.follow_focus_now()
            value = self._follow_latched
        writable = bool(prop.get("writable", True))
        if name == "near_far":
            writable = self.near_far_enabled
        return PropertyValue(
            value=value,
            choices=list(prop.get("choices") or []),
            writable=writable,
            range=bool(prop.get("range", False)),
        )

    def set_property(self, name: str, value: Any) -> None:
        self._require_connected()
        if self.busy_once:
            self.busy_once = False
            raise BusyError(f"camera busy, cannot set {name}")

        if name == "focus_position" and not self.absolute_focus_supported:
            raise UnsupportedValueError("this body does not report property 'focus_position'")
        prop = self._properties.get(name)
        if prop is None:
            raise UnsupportedValueError(f"this body has no property {name!r}")
        choices = prop.get("choices") or []
        if prop.get("range"):
            lo, hi = choices[0], choices[1]
            if not lo <= value <= hi:
                raise UnsupportedValueError(f"{name} out of range: {value!r}", valid=[lo, hi])
        elif choices and value not in choices:
            raise UnsupportedValueError(
                f"{name} does not accept {value!r}", valid=choices
            )

        self.property_writes.append((name, value))
        if name == "focus_position":
            # Focus is a physical mechanism, not a register: it lands near the
            # commanded value, not on it.
            error = self.focus_error_sequence.pop(0) if self.focus_error_sequence else 0
            prop["value"] = value + error
        elif name == "zoom_operation":
            if not self.power_zoom:
                raise UnsupportedValueError("zoom operation is not available")
            self._set_zoom_speed(int(value))
        elif name == "zoom_focus_preset_save":
            self._advance_zoom()
            self.zoom_presets[int(value)] = int(self._zoom_um)
        elif name == "zoom_focus_preset_load":
            if int(value) not in self.zoom_presets:
                raise UnsupportedValueError(f"no zoom/focus preset in slot {value}")
            self._set_zoom_speed(0)
            self._zoom_um = float(self.zoom_presets[int(value)])
            self._zoom_since = time.monotonic()
        elif name == "exposure_program_mode":
            if self.mode_switch_works:
                if self.in_movie_mode() and self.movie_focus_supported:
                    self._follow_latched = self.follow_focus_now()
                prop["value"] = value
        elif name == "near_far" and not self.near_far_enabled:
            # Accepted and ignored, exactly like the body with the lens's
            # AF/MF switch on MF.
            prop["value"] = 0
        elif name == "near_far":
            # Signed Int16 on the wire (two's complement); each write nudges
            # focus by `near_far_units_per_step` per unit of magnitude, clamped
            # to the travel range like a real lens hitting its stops.
            step = value - 0x10000 if value > 0x7FFF else value
            moved = self._properties["focus_position"]["value"] + (
                step * self.near_far_units_per_step
            )
            self._properties["focus_position"]["value"] = max(
                self.focus_min, min(self.focus_max, moved)
            )
            prop["value"] = 0
        else:
            prop["value"] = value
        self._events.put(
            Event(EVENT_PROPERTY_CHANGED, {"property": name, "value": prop["value"]})
        )

    # ------------------------------------------------------------------
    # CameraBinding - imaging
    # ------------------------------------------------------------------

    def live_view_jpeg(self) -> Optional[bytes]:
        self._require_connected()
        self.live_view_calls += 1
        return _LIVE_VIEW_FRAME

    def trigger_capture(self) -> None:
        self._require_connected()
        if self.busy_once:
            self.busy_once = False
            raise BusyError("camera busy, shutter not released")
        self.triggers += 1
        if self.drop_capture:
            return
        self._schedule(self.capture_delay_s, self._complete_capture)

    def autofocus_once(self, timeout_s: float) -> bool:
        self._require_connected()
        if not self.autofocus_succeeds:
            return False
        self._properties["focus_position"]["value"] = self.autofocus_position
        return True

    def poll_event(self, timeout_s: float) -> Optional[Event]:
        try:
            return self._events.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._connected:
            raise NotConnectedError("camera is not connected")

    def _drain(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                return

    def _schedule(self, delay: float, fn) -> None:
        timer = threading.Timer(delay, fn)
        timer.daemon = True
        self._timers.append(timer)
        timer.start()

    def _complete_capture(self) -> None:
        """Write the file(s) and announce them, as the body would.

        `capture_complete` is emitted *before* the files, matching the SDK: the
        exposure being over and the data being on the host are two different
        moments, and only the second one gives you a path.
        """
        if not self._connected:
            return
        self._seq += 1
        fmt = self._properties["still_file_format"]["value"]
        names = []
        if fmt in ("RAW", "RAW_JPEG"):
            names.append((f"DSC{self._seq:05d}.ARW", _RAW_BYTES))
        if fmt in ("JPEG", "RAW_JPEG"):
            names.append((f"DSC{self._seq:05d}.JPG", _JPEG_BYTES))

        # Someone else's files land first: after the session's pre-trigger
        # snapshot, before anything this body announces.
        for name in self.foreign_files:
            path = os.path.join(self.save_destination or ".", name)
            payload = _RAW_BYTES if name.lower().endswith((".arw", ".raw", ".dng")) else _JPEG_BYTES
            with open(path, "wb") as handle:
                handle.write(payload)
            if self.foreign_file_age_s > 0:
                stamp = time.time() - self.foreign_file_age_s
                os.utime(path, (stamp, stamp))

        self._events.put(Event(EVENT_CAPTURE_COMPLETE, {}))

        for name, payload in names:
            path = os.path.join(self.save_destination or ".", name)
            if self.slow_write_s > 0:
                with open(path, "wb") as handle:
                    handle.write(payload[:64])
                self._schedule(self.slow_write_s, lambda p=path, b=payload: _finish_write(p, b))
            else:
                with open(path, "wb") as handle:
                    handle.write(payload)
            data = {"path": path} if self.emit_file_path else {}
            self._events.put(Event(EVENT_FILE_WRITTEN, data))


def _finish_write(path: str, payload: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(payload)
