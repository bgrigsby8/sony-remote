"""
session.py
----------
Owns the camera. One thread, one queue, one connection state machine.

CrSDK is not thread-safe and delivers notifications on its own threads, so this
module imposes a single rule that everything else follows from: **every call
into the binding happens on one owner thread**, and SDK callbacks only ever
enqueue events for that thread to pick up (see `binding/interface.py`). The
Viam model is async and calls in from arbitrary event-loop threads; those calls
become jobs on a `queue.Queue` and block on a `threading.Event` until the owner
thread has run them.

That single choice buys most of §6 of the scope for free:

* **Serialized commands** - concurrent DoCommands queue by construction.
* **Live view yields to capture** - a live-view job physically cannot run while
  a capture job is in progress on the same thread, which also answers open
  question §10.4 ("must polling pause during capture?") with "yes, structurally,
  whatever the SDK turns out to require".
* **No zombie SDK state** - init and release both happen on this thread, in the
  same `try/finally`, so a viam-server restart never leaves the body claimed.

What the thread does when it has no work is reconnect. Connection is not a
setup step that can fail the module; it is a loop that runs forever with capped
exponential backoff, so a camera that is unplugged, power-cycled or plugged in
ten minutes after viam-server started all end up in the same place.
"""

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import settings as settings_mod
from binding import (
    EVENT_CAPTURE_COMPLETE,
    EVENT_DISCONNECTED,
    EVENT_FILE_WRITTEN,
    EVENT_PROPERTY_CHANGED,
    EVENT_WARNING,
    BusyError,
    CameraBinding,
    CameraError,
    CaptureTimeoutError,
    ConfigurationError,
    NotConnectedError,
    SDKError,
    UnsupportedValueError,
)
from store import CaptureStore, mime_for, primary_file

# Reconnect backoff. Starts eager (a replug should recover in well under a
# second) and caps low enough that a camera powered on hours later is picked up
# promptly. It never gives up - "forever" is the requirement, not a bug.
_BACKOFF_START_S = 0.5
_BACKOFF_MAX_S = 15.0

# One settle-and-retry for apply_on_connect writes the body reports as busy.
# Observed on the A7R V: the first write after taking PC-remote priority
# bounces with Api_InvalidCalled while the body digests the priority change;
# the same write succeeds moments later.
_APPLY_RETRY_DELAY_S = 1.0

# How long the owner thread parks on the job queue when it has nothing to do.
# Also the ceiling on how long a disconnect event sits unnoticed, since events
# are drained on the same tick. 50ms is imperceptible to an operator and costs
# ~20 wakeups/second on an idle machine.
_IDLE_TICK_S = 0.05

# Event-queue wait inside a capture. Long enough not to spin, short enough that
# the "camera vanished mid-capture" check runs promptly.
_CAPTURE_TICK_S = 0.05

# Slack added to a job's own timeout before `submit` gives up waiting for the
# owner thread. This covers time spent queued behind other jobs; blowing
# through it means the queue is genuinely backed up, which is reported as
# `busy` rather than as a timeout of the command itself.
_JOB_QUEUE_GRACE_S = 30.0

# The lens needs a moment to physically move before a read-back means anything.
# TUNE ON HARDWARE - too short and every `set_focus_position` "misses" and
# retries; too long and each station in a sweep pays for it.
_FOCUS_SETTLE_S = 0.15

# Default tolerance for `set_focus_position`, in raw SDK units. Focus is a
# mechanism: commanding 128 and reading back 128 every time is not something to
# count on, so the contract is "within tolerance", not "exact".
_DEFAULT_FOCUS_TOLERANCE = 2

# Power zoom. The drive (`zoom_operation`) is continuous - a speed starts the
# lens moving and only a 0 write stops it - so every zoom path ends in a stop
# write, and a drive started without a duration is stopped by the owner thread
# after `zoom_max_drive_s` even if the caller never sends one.
# How often the closed loop re-reads `zoom_distance` while the lens moves.
_ZOOM_POLL_S = 0.03
# After a stop the lens coasts (seen on the FE PZ 16-35: a read 0.2s after
# the stop said 19.5mm, the next call found it at 20). A read-back is trusted
# once it has held still this long, or after the cap regardless.
_ZOOM_STABLE_S = 0.25
_ZOOM_SETTLE_MAX_S = 2.0
# A preset load is accepted at once but the lens starts moving noticeably
# later (longer than _ZOOM_STABLE_S on the FE PZ 16-35), and this body has no
# ZoomDrivingStatus to ask. Wait this long for motion to begin before settling;
# loading the preset you're already at pays it in full.
_ZOOM_PRESET_START_S = 2.0
# A drive whose reading hasn't changed for this long has hit an end stop (or
# the body ignored the write); stop instead of waiting out the timeout.
_ZOOM_STALL_S = 1.0
# Corrective passes before `set_zoom` reports ok=false. Each pass after an
# overshoot halves the speed, so this bounds a hunt, not a normal move.
_ZOOM_MAX_PASSES = 8
# Zoom_Operation_Status value meaning "the drive will be honoured".
_ZOOM_ENABLED = 1

# Focus modes in which the focus position property is writable. Anything else
# and the lens is under the body's AF control, and a write either errors or is
# silently overridden on the next half-press.
_MANUAL_FOCUS_MODES = ("MF", "DMF")

# Stills exposure program -> its movie counterpart (CrExposureProgram raw
# values: M, P, A, S -> Movie M, P, A, S). Pairing them means a body found in
# a movie mode at connect - a crash mid focus operation - can be put back in
# exactly the stills mode it left.
_STILLS_TO_MOVIE = {0x1: 0x8053, 0x2: 0x8050, 0x3: 0x8051, 0x4: 0x8052}
_MOVIE_TO_STILLS = {movie: stills for stills, movie in _STILLS_TO_MOVIE.items()}
_DEFAULT_MOVIE_MODE = 0x8053

# Follow-focus read-back scale. The body reports 0xFFFF at the near stop and
# 0 at the far stop; the module flips it so 0 is the near stop, matching the
# emulation's "count from the near stop".
_FOLLOW_FOCUS_MAX = 0xFFFF
# How often a stills<->movie switch is re-checked while waiting for it.
_MODE_POLL_S = 0.1
# A follow-focus read is trusted once two reads this far apart agree (the lens
# may still be moving after a nudge), or after the cap regardless.
_FOCUS_READ_POLL_S = 0.05
_FOCUS_READ_SETTLE_MAX_S = 1.0
# Closed-loop give-ups, so a move ends in ok=false rather than the job
# timeout: direction reversals (hunting around a target narrower than the
# smallest nudge), and nudges in a row that didn't move the lens (each retried
# one size bigger).
_FOCUS_MAX_REVERSALS = 4
_FOCUS_MAX_STALLS = 3

# Applied at connect underneath whatever the operator configured. Mechanical is
# the default because this rig fires a strobe: the electronic shutter reads the
# sensor progressively, so a flash lights only the rows exposed while it fired.
# Set `"shutter_type": "auto"` explicitly to opt out.
_DEFAULT_APPLY_ON_CONNECT: Dict[str, Any] = {
    "shutter_type": settings_mod.DEFAULT_SHUTTER_TYPE,
}


@dataclass
class SessionConfig:
    """Everything from the component config that the session needs."""

    capture_dir: str = "/tmp/sony-remote"
    serial: Optional[str] = None
    retention_max_files: int = 200
    live_view_max_fps: float = 10.0
    connect_timeout_s: float = 10.0
    capture_timeout_s: float = 15.0
    autofocus_timeout_s: float = 5.0
    focus_tolerance: int = _DEFAULT_FOCUS_TOLERANCE
    apply_on_connect: Dict[str, Any] = field(default_factory=dict)
    # Emulated absolute focus, for bodies that refuse FocusPositionSetting
    # over USB (the ILCE-7RM5 does, with every lens and mode tried - verified
    # against Sony's own RemoteCli). "auto": emulate over the near/far drive
    # when the real property is absent. "off": never emulate.
    # A position is then a count of nudges from the near stop; each nudge is
    # one near/far write of magnitude `emulated_step_size` (1-7), and homing
    # drives `emulated_travel_nudges` nudges toward the near stop - size it so
    # that many nudges crosses the lens's whole travel with margin.
    # The interval must exceed the lens's per-nudge move time: the body
    # silently drops near/far writes that arrive while the lens is still
    # moving (no error, no telemetry), which corrupts the position count.
    # Calibrate per lens and step size by lowering it until `home_focus`
    # stops short of the near stop, then back off with margin.
    focus_emulation: str = "auto"
    emulated_step_size: int = 3
    emulated_travel_nudges: int = 150
    emulated_nudge_interval_s: float = 0.2
    # Which absolute-focus mechanism to use.
    # "auto": the body's FocusPositionSetting when it reports one, else the
    #   near/far emulation above (the behaviour before `focus_method` existed).
    # "movie": closed loop over the movie-mode follow-focus channel. The
    #   ILCE-7RM5 publishes the lens's real position
    #   (FollowFocusPositionCurrentValue) only in movie mode, so each focus
    #   operation switches to movie, drives near/far against the read-back,
    #   and switches back to stills. Positions run 0 (near stop) .. 65535
    #   (far stop), units "follow_focus".
    # "nudge": always the near/far emulation.
    focus_method: str = "auto"
    # When "movie" can't run (no follow-focus channel, a mode switch that
    # won't take): "nudge" falls back to the emulation for the rest of the
    # connection, converting positions with `movie_units_per_nudge`; "none"
    # fails the focus operation instead.
    movie_focus_fallback: str = "nudge"
    # How close (follow-focus units) the read-back must land. A size-1 nudge
    # moves roughly 700 units on the FE PZ 16-35, so much tighter than half of
    # that can hunt.
    movie_focus_tolerance: int = 400
    # Longest wait for the body to finish a stills<->movie switch and for the
    # lens to start publishing its position.
    movie_mode_timeout_s: float = 5.0
    # Follow-focus units moved by one nudge of `emulated_step_size`: sizes each
    # closed-loop nudge, and converts positions when falling back to the
    # emulation. ~2100 measured for step 3 on the FE PZ 16-35 (single nudges
    # varied 550-2100, which is why the closed loop exists).
    movie_units_per_nudge: float = 2100.0
    # Nudge budget for one closed-loop move before it reports ok=false.
    movie_max_nudges: int = 60
    # Drive focus to this position on every connect (including reconnects
    # after a camera power cycle, which can physically move the lens). With
    # emulated focus this homes first, so the rig needs no focus logic
    # anywhere else: one number here keeps every shot at the same plane.
    focus_on_connect: Optional[int] = None
    # Power zoom (needs a PZ lens). Positions are focal lengths in mm, read
    # back from the body, so `set_zoom` is closed-loop - unlike focus, nothing
    # here is emulated or needs homing.
    # 0 = land exactly on the lens's reported step (0.5mm on the FE PZ
    # 16-35), which the closed loop reaches in 1-3 passes in practice.
    zoom_tolerance_mm: float = 0.0
    zoom_timeout_s: float = 20.0
    # Watchdog for `zoom_drive` without a duration (press-and-hold jogging):
    # the lens is stopped this long after the last drive command.
    zoom_max_drive_s: float = 10.0
    # Drive zoom to this focal length (mm) on every connect, before
    # focus_on_connect - zooming can move the focus group on a PZ lens.
    zoom_on_connect: Optional[float] = None


class _Job:
    """One unit of work for the owner thread."""

    __slots__ = ("name", "fn", "requires_connection", "done", "result", "error")

    def __init__(self, name: str, fn: Callable[[], Any], requires_connection: bool):
        self.name = name
        self.fn = fn
        self.requires_connection = requires_connection
        self.done = threading.Event()
        self.result: Any = None
        self.error: Optional[BaseException] = None


class CameraSession:
    """The camera, as the rest of the module sees it.

    Every public method blocks until the owner thread has answered, and raises
    one of the typed errors from `binding.interface` on failure. Safe to call
    from any thread; the Viam model calls them from an executor.
    """

    def __init__(self, binding: CameraBinding, config: SessionConfig, logger):
        self._binding = binding
        self._config = config
        self._logger = logger
        self._store = CaptureStore(
            config.capture_dir, config.retention_max_files, logger=logger
        )

        self._queue: "queue.Queue[_Job]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Connection state. `_connected` is written only by the owner thread but
        # read from everywhere, which is fine for a bool in CPython and is the
        # only thing `get_status` needs to be truthful without taking the queue.
        self._connected = False
        self._device: Dict[str, Any] = {}
        self._last_error: Optional[str] = None
        self._connect_attempts = 0
        self._apply_errors: List[str] = []

        # Live-view frame cache, guarded by its own lock so preview polling at
        # `live_view_max_fps` never touches the job queue on a cache hit.
        self._frame_lock = threading.Lock()
        self._frame: Optional[bytes] = None
        self._frame_at = 0.0
        self._min_frame_interval = (
            1.0 / config.live_view_max_fps if config.live_view_max_fps > 0 else 0.0
        )

        # Capture-in-flight bookkeeping, owner thread only.
        self._capturing = False
        self._capture_files: List[str] = []
        self._capture_complete = False

        # Property reads are USB round trips, and a capture wants a full
        # settings snapshot for the audit log. Cache it, and let a
        # property-changed event (a dial turned on the body, our own write)
        # invalidate it, rather than paying six round trips per shot.
        self._state_cache: Optional[Dict[str, Any]] = None

        # Focus state, owner thread only. `_focus_backend` is None until the
        # first focus operation resolves `focus_method` against the body:
        # "native" (FocusPositionSetting), "movie" (follow-focus read-back)
        # or "nudge" (emulation, whose counter is only meaningful while
        # `_focus_homed`). Reset on every connect.
        self._focus_backend: Optional[str] = None
        self._focus_fallback_reason: Optional[str] = None
        self._focus_homed = False
        self._focus_counter = 0
        # Movie backend: the last position read back, None once anything may
        # have moved the lens since. `_movie_restore` is the stills exposure
        # mode to return to while a focus operation has the body in movie
        # mode; not None means "the body may still be in movie mode".
        self._movie_known: Optional[int] = None
        self._movie_restore: Optional[int] = None

        # Open-ended zoom drive watchdog (owner thread only): monotonic time
        # at which a running `zoom_drive` without a duration gets stopped.
        self._zoom_drive_deadline: Optional[float] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._store.ensure_dir()
        self._thread = threading.Thread(
            target=self._run, name="sony-remote-camera", daemon=True
        )
        self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        """Stop the owner thread and release the SDK.

        The release itself happens on the owner thread (see `_run`'s finally),
        because CrSDK's init/release must be paired on the same thread that did
        everything else. If the thread doesn't come back - stuck inside a
        blocking SDK call - we release from here anyway and log it: a leaked
        session is bad, but hanging viam-server's shutdown is worse.
        """
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                self._log(
                    "error",
                    "camera thread did not stop within "
                    f"{timeout}s; releasing the SDK from the caller's thread. "
                    "If the body is unresponsive after this, power-cycle it.",
                )
                self._safe_release()
        else:
            self._safe_release()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def store(self) -> CaptureStore:
        return self._store

    @property
    def capture_dir(self) -> str:
        return self._config.capture_dir

    # ------------------------------------------------------------------
    # Public API - each of these is a job on the owner thread
    # ------------------------------------------------------------------

    def live_view(self) -> bytes:
        """Latest live-view JPEG, at most `live_view_max_fps` fresh.

        A cache hit doesn't queue a job at all, so a webapp preview polling at
        30fps costs nothing while a capture is running. A cache hit is only
        possible while connected - a disconnected camera raises rather than
        handing back a stale frame.
        """
        if not self._connected:
            raise NotConnectedError(
                self._last_error or "camera is not connected; no live view available"
            )
        now = time.monotonic()
        with self._frame_lock:
            if self._frame is not None and now - self._frame_at < self._min_frame_interval:
                return self._frame
        return self._submit("live_view", self._do_live_view, timeout=5.0)

    def capture(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """Fire the shutter and return once the file is on the host."""
        limit = float(timeout_s or self._config.capture_timeout_s)
        return self._submit(
            "capture", lambda: self._do_capture(limit), timeout=limit
        )

    def get_settings(self) -> Dict[str, Any]:
        return self._submit("get_settings", lambda: self._read_state(refresh=True)["settings"])

    def dump_properties(self) -> List[Dict[str, Any]]:
        return self._submit("dump_properties", self._binding.dump_properties)

    def set_property_raw(self, name: str, value: int, value_type: int) -> Dict[str, Any]:
        def run():
            self._binding.set_property_raw(name, value, value_type)
            self._state_cache = None
            return {"name": name, "value": value, "value_type": f"0x{value_type:X}"}

        return self._submit("set_property_raw", run)

    def set_settings(self, values: Dict[str, Any]) -> Dict[str, Any]:
        encoded = settings_mod.validate_all(values)  # raises before touching the camera
        return self._submit("set_settings", lambda: self._do_set_settings(encoded))

    def get_focus_position(self) -> int:
        # Under `focus_method: movie` a read is two mode switches, and may
        # fall back to homing the emulation.
        return self._submit(
            "get_focus_position", self._do_get_focus, timeout=self._focus_motion_budget_s()
        )

    def set_focus_position(
        self, position: int, tolerance: Optional[int] = None
    ) -> Dict[str, Any]:
        # The default tolerance depends on the backend's units, so it is
        # resolved on the owner thread.
        tol = None if tolerance is None else int(tolerance)
        return self._submit(
            "set_focus_position",
            lambda: self._do_set_focus(int(position), tol),
            timeout=self._focus_motion_budget_s(),
        )

    def autofocus_once(self) -> Dict[str, Any]:
        return self._submit(
            "autofocus_once", self._do_autofocus, timeout=self._config.autofocus_timeout_s
        )

    def home_focus(self) -> Dict[str, Any]:
        """Re-zero emulated focus against the near stop. Under
        `focus_method: movie`, read the real position instead (and retry movie
        mode after a fallback). No-op information on bodies with native
        absolute focus."""
        return self._submit(
            "home_focus", self._do_home_focus, timeout=self._focus_motion_budget_s()
        )

    def _focus_motion_budget_s(self) -> float:
        """Worst-case wall time for one emulated focus command.

        `set_focus_position` may home first (a full travel of nudges) and then
        move up to the full travel again, each nudge sleeping
        `emulated_nudge_interval_s` - so the budget scales with the configured
        motion instead of the fixed default, which a slow nudge interval or a
        long travel would otherwise blow through mid-move.
        """
        travel = int(self._config.emulated_travel_nudges)
        interval = max(float(self._config.emulated_nudge_interval_s), 0.005)
        budget = 2 * travel * interval + 10.0
        if self._config.focus_method == "movie":
            # Two mode switches (each waited out up to the timeout), plus a
            # read-back per nudge - and a failed movie attempt may still fall
            # back to the emulation inside the same command.
            budget += 2 * float(self._config.movie_mode_timeout_s) + int(
                self._config.movie_max_nudges
            ) * (interval + _FOCUS_READ_SETTLE_MAX_S)
        return budget

    def focus_near_far(self, step: int) -> Dict[str, Any]:
        """One relative focus nudge: sign is direction (negative = near),
        magnitude 1-7 is the step size. The raw primitive under emulated
        absolute focus, exposed directly for calibration and bring-up.
        """
        return self._submit("focus_near_far", lambda: self._do_near_far(int(step)))

    def _do_near_far(self, step: int) -> Dict[str, Any]:
        self._require_near_far()
        # The property is signed Int16; the binding layer speaks unsigned, so
        # encode two's complement here.
        self._binding.set_property("near_far", step & 0xFFFF)
        # A manual nudge moves the lens outside the emulated count, and away
        # from the last movie-mode read-back.
        self._focus_homed = False
        self._movie_known = None
        return {"step": step}

    def get_zoom(self) -> Dict[str, Any]:
        return self._submit("get_zoom", self._do_get_zoom)

    def zoom_drive(self, speed: int, duration_s: Optional[float] = None) -> Dict[str, Any]:
        """Run the power zoom at `speed` (positive = tele, negative = wide,
        0 = stop). With `duration_s` it blocks, then stops; without it the
        lens keeps moving until the next drive/stop or the watchdog
        (`zoom_max_drive_s`) - the press-and-hold jog primitive."""
        timeout = (float(duration_s) if duration_s else 0.0) + 10.0
        return self._submit(
            "zoom_drive", lambda: self._do_zoom_drive(int(speed), duration_s), timeout=timeout
        )

    def set_zoom(
        self, focal_length_mm: float, tolerance_mm: Optional[float] = None
    ) -> Dict[str, Any]:
        tol = float(self._config.zoom_tolerance_mm if tolerance_mm is None else tolerance_mm)
        return self._submit(
            "set_zoom",
            lambda: self._do_set_zoom(float(focal_length_mm), tol),
            timeout=float(self._config.zoom_timeout_s) + 5.0,
        )

    def zoom_preset(self, action: str, slot: int) -> Dict[str, Any]:
        """Save the current zoom+focus into a body preset slot, or drive back
        to one. Presets live in the camera and survive its init."""
        return self._submit(
            f"zoom_preset_{action}",
            lambda: self._do_zoom_preset(action, int(slot)),
            timeout=float(self._config.zoom_timeout_s) + 5.0,
        )

    def device_status(self) -> Dict[str, Any]:
        """Truthful status whether or not a camera is attached."""
        return self._submit("get_status", self._do_status, requires_connection=False)

    def run_offline(self, name: str, fn: Callable[[], Any]) -> Any:
        """Run `fn` on the owner thread without requiring a connection.

        For the filesystem-only commands (list / cleanup / delete / counter).
        They go through the queue anyway so that retention can't delete a file
        a capture is still settling.
        """
        return self._submit(name, fn, requires_connection=False)

    # ------------------------------------------------------------------
    # Job plumbing
    # ------------------------------------------------------------------

    def _submit(
        self,
        name: str,
        fn: Callable[[], Any],
        *,
        requires_connection: bool = True,
        timeout: Optional[float] = None,
    ) -> Any:
        if self._thread is None or not self._thread.is_alive():
            raise NotConnectedError(f"camera session is not running (cannot run {name})")

        job = _Job(name, fn, requires_connection)
        self._queue.put(job)

        wait = (timeout or 10.0) + _JOB_QUEUE_GRACE_S
        if not job.done.wait(wait):
            # The job never ran, or ran long. Either way it is still on the
            # owner thread; reporting `busy` rather than `timeout` tells the
            # caller the difference between "the camera didn't answer" and
            # "you're behind other work".
            raise BusyError(
                f"{name} did not complete within {wait:.0f}s; the camera queue is "
                "backed up (another capture may still be running)"
            )
        if job.error is not None:
            raise job.error
        return job.result

    def _execute(self, job: _Job) -> None:
        try:
            if job.requires_connection and not self._connected:
                raise NotConnectedError(
                    self._last_error
                    or f"camera is not connected; cannot run {job.name}"
                )
            job.result = job.fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller intact
            job.error = exc
        finally:
            job.done.set()

    # ------------------------------------------------------------------
    # Owner thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._binding.init()
        except Exception as exc:  # noqa: BLE001
            # Usually "the extension isn't built" or "the .so isn't on the
            # loader path" - a configuration problem, not a camera problem. Keep
            # the thread alive so `get_status` can report it instead of every
            # call failing with "session is not running".
            self._last_error = str(exc)
            self._log("error", f"camera SDK unavailable: {exc}")

        backoff = _BACKOFF_START_S
        next_attempt = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()

                if not self._connected and now >= next_attempt:
                    self._connect_attempts += 1
                    if self._try_connect():
                        backoff = _BACKOFF_START_S
                        next_attempt = 0.0
                    else:
                        next_attempt = time.monotonic() + backoff
                        backoff = min(backoff * 2, _BACKOFF_MAX_S)

                if (
                    self._zoom_drive_deadline is not None
                    and time.monotonic() >= self._zoom_drive_deadline
                ):
                    self._zoom_watchdog_stop()

                wait = _IDLE_TICK_S
                if not self._connected:
                    wait = max(0.01, min(_IDLE_TICK_S, next_attempt - time.monotonic()))

                try:
                    job = self._queue.get(timeout=wait)
                except queue.Empty:
                    if self._connected:
                        self._pump_events(0.0)
                    continue

                self._execute(job)
        finally:
            self._drain_queue()
            self._safe_release()

    def _drain_queue(self) -> None:
        """Fail anything still queued at shutdown rather than leaving callers
        parked until their grace period expires."""
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                return
            job.error = NotConnectedError(f"camera session is shutting down ({job.name})")
            job.done.set()

    def _safe_release(self) -> None:
        try:
            self._binding.release()
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            self._log("warning", f"error releasing the camera SDK: {exc}")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _try_connect(self) -> bool:
        try:
            device = self._select_device()
            self._binding.connect(device, self._config.connect_timeout_s)
            self._store.ensure_dir()
            self._binding.set_save_destination(self._config.capture_dir)
            self._connected = True
            self._state_cache = None
            # A (re)connect invalidates everything focus knew: the lens may
            # have moved while we weren't watching, and a fallback taken on
            # the last connection deserves a fresh probe.
            self._focus_backend = None
            self._focus_fallback_reason = None
            self._focus_homed = False
            self._movie_known = None
            self._movie_restore = None
            # The body boots owning its shooting settings; until the PC takes
            # priority, remote sets are rejected (Api_InvalidCalled) or
            # silently ignored. Non-fatal like every apply: a body that
            # refuses still connects, and apply_errors will say what stuck.
            try:
                self._retry_busy(
                    lambda: self._binding.set_property("priority_key", "PCRemote")
                )
            except CameraError as exc:
                self._log(
                    "warning",
                    f"could not take PC-remote priority; settings may be "
                    f"read-only from here: {exc}",
                )
            # The camera menu has an equivalent setting, but the SDK session
            # acts on the property - without HostPC here the exposure happens
            # and the image strands in the body's buffer, wedging later shots.
            try:
                self._retry_busy(
                    lambda: self._binding.set_property("store_destination", "HostPC")
                )
            except CameraError as exc:
                self._log(
                    "warning",
                    f"could not set the still-image store destination to the "
                    f"host; captures may strand in the body or go to the card: "
                    f"{exc}",
                )
            if self._config.focus_method == "movie":
                self._recover_stills_mode()
            self._apply_on_connect()
            self._zoom_drive_deadline = None
            if self._config.zoom_on_connect is not None:
                try:
                    result = self._do_set_zoom(
                        float(self._config.zoom_on_connect),
                        float(self._config.zoom_tolerance_mm),
                    )
                    self._log(
                        "info",
                        f"zoom_on_connect: {result['focal_length_mm']}mm "
                        f"(ok={result['ok']})",
                    )
                except CameraError as exc:
                    detail = f"zoom_on_connect={self._config.zoom_on_connect}: {exc}"
                    self._apply_errors.append(detail)
                    self._log("error", f"apply_on_connect failed for {detail}")
            if self._config.focus_on_connect is not None:
                try:
                    result = self._do_set_focus(int(self._config.focus_on_connect), None)
                    self._log(
                        "info",
                        f"focus_on_connect: position "
                        f"{result['position']} ({result['units']})",
                    )
                except CameraError as exc:
                    detail = f"focus_on_connect={self._config.focus_on_connect}: {exc}"
                    self._apply_errors.append(detail)
                    self._log("error", f"apply_on_connect failed for {detail}")
            try:
                self._device = dict(self._binding.device_info())
            except CameraError:
                self._device = {"model": device.model, "serial": device.serial}
            self._last_error = None
            self._log(
                "info",
                f"connected to {self._device.get('model') or device.model} "
                f"(serial {self._device.get('serial') or device.serial or '?'}); "
                f"stills save to {self._config.capture_dir}",
            )
            return True
        except Exception as exc:  # noqa: BLE001 - every failure is retryable
            self._connected = False
            self._clear_frame()
            self._note_connect_failure(exc)
            try:
                self._binding.disconnect()
            except Exception:  # noqa: BLE001
                pass
            return False

    def _select_device(self):
        """Pick the camera to use, or explain why we can't.

        `serial` is matched as a suffix as well as exactly, because what's
        printed on the body, what the SDK reports and what an operator types
        into a config are not reliably the same string.
        """
        devices = self._binding.enumerate()
        if not devices:
            raise NotConnectedError("no Sony camera found on USB")

        wanted = (self._config.serial or "").strip()
        if wanted:
            for device in devices:
                serial = (device.serial or "").strip()
                if serial == wanted or serial.endswith(wanted) or wanted.endswith(serial):
                    return device
            found = ", ".join(d.serial or f"<{d.model}, no serial>" for d in devices)
            raise ConfigurationError(
                f"no camera with serial {wanted!r} on USB; found: {found}"
            )

        if len(devices) > 1:
            found = ", ".join(f"{d.model} {d.serial}".strip() for d in devices)
            raise ConfigurationError(
                f"{len(devices)} Sony cameras are connected ({found}); set `serial` "
                "in the component config to say which one this component owns"
            )
        return devices[0]

    def _note_connect_failure(self, exc: BaseException) -> None:
        """Log a failed attempt without filling the log with the same line.

        A camera that is simply not plugged in produces one failure every
        backoff period, forever. The first of each distinct message is worth an
        operator's attention; the repeats are not.
        """
        message = str(exc)
        first_time = message != self._last_error
        self._last_error = message
        if first_time:
            self._log("warning", f"camera not available: {message} (retrying)")
        else:
            self._log("debug", f"camera still not available: {message}")

    def _apply_on_connect(self) -> None:
        """Push the configured capture recipe onto the body.

        A rejected value is logged and recorded, not raised: a config that the
        camera won't take must not leave the operator with a component that
        can't even show live view to debug with. `get_status.apply_errors`
        reports what didn't stick.
        """
        wanted = dict(_DEFAULT_APPLY_ON_CONNECT)
        wanted.update(self._config.apply_on_connect or {})
        self._apply_errors = []
        if not wanted:
            return

        for key, value in wanted.items():
            try:
                raw = settings_mod.validate(key, value)
                self._retry_busy(lambda: self._set_property(key, raw))
                self._log("debug", f"apply_on_connect: {key} = {value!r}")
            except CameraError as exc:
                detail = f"{key}={value!r}: {exc.message}"
                valid = exc.details.get("valid") if isinstance(exc, CameraError) else None
                if valid:
                    detail += f" (camera accepts: {valid})"
                self._apply_errors.append(detail)
                self._log("error", f"apply_on_connect failed for {detail}")
        self._state_cache = None

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _pump_events(self, timeout_s: float) -> None:
        """Drain the binding's event queue. Owner thread only.

        Called both from the idle loop and from inside a capture wait, which is
        why the first poll may block and subsequent ones never do.
        """
        while True:
            try:
                event = self._binding.poll_event(timeout_s)
            except CameraError as exc:
                self._log("debug", f"event poll failed: {exc}")
                return
            if event is None:
                return
            self._handle_event(event)
            timeout_s = 0.0

    def _handle_event(self, event) -> None:
        kind = event.kind

        if kind == EVENT_DISCONNECTED:
            if self._connected:
                self._log(
                    "warning",
                    f"camera disconnected ({event.data.get('reason', 'sdk')}); "
                    "reconnecting",
                )
            self._connected = False
            self._last_error = "camera disconnected"
            self._zoom_drive_deadline = None
            self._clear_frame()
            self._state_cache = None
            try:
                self._binding.disconnect()
            except Exception:  # noqa: BLE001
                pass
            return

        if kind == EVENT_FILE_WRITTEN:
            path = event.data.get("path") or ""
            if self._capturing:
                # An empty string is recorded on purpose: it means "a file
                # exists but the SDK didn't name it", which is what tips
                # `_await_files` into diffing the directory.
                self._capture_files.append(path)
            elif path:
                self._log("debug", f"file written outside a capture: {path}")
            return

        if kind == EVENT_CAPTURE_COMPLETE:
            self._capture_complete = True
            return

        if kind == EVENT_PROPERTY_CHANGED:
            # Advisory - the payload may not say which property. Drop the cache
            # and re-read next time someone asks.
            self._state_cache = None
            return

        if kind == EVENT_WARNING:
            # Body-initiated and rare; hex because that is how CrError.h reads.
            value = event.data.get("value")
            code = f"0x{int(value):X}" if isinstance(value, (int, float)) else value
            self._log("warning", f"camera warning: {code}")
            return

        self._log("debug", f"unhandled camera event {kind!r}: {event.data}")

    # ------------------------------------------------------------------
    # Job bodies - owner thread only, may block
    # ------------------------------------------------------------------

    def _do_live_view(self) -> bytes:
        self._pump_events(0.0)
        frame = self._binding.live_view_jpeg()
        if frame:
            with self._frame_lock:
                self._frame = frame
                self._frame_at = time.monotonic()
            return frame

        with self._frame_lock:
            cached = self._frame
        if cached is not None:
            return cached
        raise CameraError(
            "camera has not produced a live-view frame yet; it typically needs a "
            "moment after connecting, and the body must not be in playback mode"
        )

    def _do_capture(self, timeout_s: float) -> Dict[str, Any]:
        started = time.monotonic()
        deadline = started + timeout_s

        # A focus operation that couldn't get the body back out of movie mode
        # left this set. Retry the switch, and refuse to fire if it still
        # won't take - a "still" shot in movie mode is not the shot we want.
        if self._movie_restore is not None:
            self._exit_movie()

        state = self._read_state(refresh=False)

        # Flush anything the SDK queued before this trigger. A `file_written`
        # for the *previous* capture can still be sitting there - if the last
        # shot was resolved by the directory diff, or timed out, its event
        # arrives late - and picking it up here would make this capture return
        # the previous shot's path. Clearing the queue first makes "files from
        # now on" mean exactly that.
        self._pump_events(0.0)

        before = self._store.snapshot()
        self._capture_files = []
        self._capture_complete = False
        self._capturing = True
        try:
            self._binding.trigger_capture()
            paths, named_by_sdk = self._await_files(before, deadline, state["settings"])
        finally:
            self._capturing = False

        # A file the SDK announced by name is already complete - that event is
        # the completion signal. Only the directory-diff path has to guess, and
        # guessing costs a quarter-second per shot, so it's paid only there.
        settle_deadline = max(deadline, time.monotonic() + 2.0)
        sizes = {}
        for path in paths:
            size = (
                _file_size(path)
                if named_by_sdk
                else self._store.wait_until_settled(path, settle_deadline)
            )
            if size == 0:
                raise CaptureTimeoutError(
                    f"{os.path.basename(path)} never finished writing within "
                    f"{timeout_s:.0f}s; raise `capture_timeout_s` if the card or "
                    "host disk is slow",
                    path=path,
                )
            sizes[path] = size

        count = self._store.increment_capture_count()
        removed = self._store.prune()
        primary = primary_file(paths)
        duration = time.monotonic() - started

        # The audit trail: everything needed to answer "why does
        # this image look like that" from the log alone.
        self._log(
            "info",
            f"capture #{count} -> {primary} ({sizes.get(primary, 0)} bytes) in "
            f"{duration:.2f}s; focus={state['focus_position']} "
            f"settings={state['settings']}"
            + (f"; retention removed {len(removed)}" if removed else ""),
        )

        return {
            # `path` and `saved_to` are the same host path. Both keys are
            # present because `color-correction` reads `saved_to or path` and
            # the webapp reads `path`; direct-to-host means there is no separate
            # on-camera location to distinguish them (see README, "ptp parity").
            "path": primary,
            "saved_to": primary,
            "name": os.path.basename(primary or ""),
            "mime_type": mime_for(primary or ""),
            "size": sizes.get(primary, 0),
            "paths": paths,
            "capture_count": count,
            "duration_s": round(duration, 3),
            "focus_position": state["focus_position"],
            "settings": state["settings"],
        }

    def _await_files(
        self, before: Set[str], deadline: float, snapshot: Dict[str, Any]
    ) -> Tuple[List[str], bool]:
        """Wait for the still(s) this trigger produced.

        Two independent ways of learning the answer, because bodies differ:

        1. `file_written` events carrying a path - authoritative when present.
        2. Diffing `capture_dir` against the pre-trigger snapshot - the fallback
           for an SDK build that reports completion without a filename. Safe
           because the snapshot was taken *after* the previous capture settled,
           so nothing old can be mistaken for new.

        In RAW+JPEG the body writes two files and doesn't promise an order, so
        we wait for the expected count rather than the first arrival.

        Returns `(paths, named_by_sdk)`; the caller uses the flag to decide
        whether it still has to wait for the files to finish writing.
        """
        expected = 2 if snapshot.get("file_format") in ("raw+jpeg", "raw+heif") else 1
        found: List[str] = []
        found_named = False

        while time.monotonic() < deadline:
            if not self._connected:
                raise NotConnectedError(
                    "camera disconnected during capture; the shutter may or may "
                    "not have fired - check `capture_dir` before re-shooting"
                )

            remaining = deadline - time.monotonic()
            self._pump_events(min(_CAPTURE_TICK_S, max(0.0, remaining)))

            # Belt and braces against a straggler event from the previous shot:
            # a path that already existed before the trigger is not this
            # capture's, whatever the SDK says.
            named = [
                p
                for p in self._capture_files
                if p and os.path.basename(p) not in before
            ]
            if len(named) >= expected:
                return named, True

            diffed = self._store.new_files_since(before)
            if len(diffed) >= expected:
                return diffed, False

            found_named = bool(named)
            found = named or diffed

        if found:
            # Partial: RAW landed, the JPEG is still coming. Better to hand back
            # what exists than to fail a shot that mostly worked - but say so.
            self._log(
                "warning",
                f"capture produced {len(found)} of {expected} expected files before "
                "the timeout; returning what landed",
            )
            return found, found_named

        if self._capture_complete:
            # The exposure finished but nothing reached the host. Almost always
            # the save destination: the body is writing to its card instead of
            # to us, which `set_save_destination` at connect is supposed to fix.
            raise CaptureTimeoutError(
                "the exposure completed but no file reached "
                f"{self._config.capture_dir}. The body is most likely saving to "
                "its card rather than to the host - check that PC Remote save "
                "destination is set to the PC, and that the card isn't in an "
                "error state"
            )
        raise CaptureTimeoutError(
            "no file appeared in "
            f"{self._config.capture_dir} within the capture timeout. The shutter "
            "was released; check that a card error isn't blocking the write, and "
            "that `capture_timeout_s` allows for the exposure time"
        )

    def _do_set_settings(self, encoded: Dict[str, Any]) -> Dict[str, Any]:
        for key, raw in encoded.items():
            self._set_property(key, raw)
        self._state_cache = None
        return self._read_state(refresh=True)["settings"]

    # -- absolute focus --------------------------------------------------
    #
    # Three backends, picked by `focus_method` and resolved lazily against the
    # body at the first focus operation of each connection:
    #
    # * native - FocusPositionSetting, written and read back directly.
    # * movie  - the ILCE-7RM5 reports the lens's real position only in movie
    #   mode (FollowFocusPositionCurrentValue; frozen at its last value in
    #   stills). Each operation switches to movie, drives near/far in a closed
    #   loop against that read-back, and switches back to stills. Nothing is
    #   counted, so nothing needs homing and nothing can drift.
    # * nudge  - the near/far drive is relative and blind, so absolute
    #   positioning is rebuilt as: drive hard into the near stop (clamping is
    #   safe - a lens at its stop ignores further near nudges), call that
    #   zero, count nudges from there. Honest only while nothing else moves
    #   the lens: autofocus, zoom and reconnects invalidate it, and the next
    #   focus operation re-homes. Also the fallback when movie can't run.

    def _resolve_focus_backend(self) -> str:
        if self._focus_backend is not None:
            return self._focus_backend
        method = self._config.focus_method
        if method == "movie":
            # Not probed up front: the first movie operation is the probe, and
            # a failure there falls back (see `_movie_or_fallback`).
            self._focus_backend = "movie"
        elif method == "nudge":
            self._focus_backend = "nudge"
            self._log(
                "info",
                "focus_method nudge: positions are nudge counts from the near "
                "stop; autofocus, zoom and reconnects re-home.",
            )
        elif self._config.focus_emulation == "off":
            self._focus_backend = "native"
        else:
            try:
                self._binding.get_property("focus_position")
                self._focus_backend = "native"
            except UnsupportedValueError:
                self._focus_backend = "nudge"
                self._log(
                    "info",
                    "body does not report an absolute focus position; emulating "
                    "it over the near/far drive. Positions are nudge counts "
                    "from the near stop; autofocus and reconnects re-home.",
                )
            except CameraError:
                return "native"  # transient - probe again next time
        return self._focus_backend

    def _focus_is_emulated(self) -> bool:
        return self._resolve_focus_backend() == "nudge"

    def _movie_or_fallback(
        self, movie_op: Callable[[], Any], nudge_op: Callable[[], Any]
    ) -> Any:
        """Run `movie_op`; if movie mode can't do the job, fall back.

        A disconnect or a rig problem the emulation shares (the near/far drive
        disabled) is raised as is - nudging can't help with either. Anything
        else switches this connection to the emulation when
        `movie_focus_fallback` allows it, and runs `nudge_op` in its place.
        """
        try:
            return movie_op()
        except (NotConnectedError, ConfigurationError):
            raise
        except CameraError as exc:
            if self._config.movie_focus_fallback != "nudge":
                raise
            reason = f"movie-mode focus failed: {exc.message}"
            self._focus_backend = "nudge"
            self._focus_fallback_reason = reason
            self._focus_homed = False
            self._movie_known = None
            self._log(
                "warning",
                f"{reason}; falling back to the near/far emulation until the "
                "next connect or home_focus",
            )
            return nudge_op()

    def _require_near_far(self) -> None:
        """Fail loudly when the body has disabled the near/far drive.

        It does so without an error: writes are accepted and ignored, so every
        nudge "succeeds" and the lens never moves. On the ILCE-7RM5 that is
        the lens's AF/MF switch in MF - the body then hands focus to the ring
        and reports NearFar as not settable.
        """
        try:
            prop = self._binding.get_property("near_far")
        except CameraError:
            return  # a body that doesn't report it can't tell us; try anyway
        if not prop.writable:
            raise ConfigurationError(
                "the body has disabled the near/far focus drive, so focus can't "
                "be moved remotely - nudges would be silently ignored. On the "
                "ILCE-7RM5 this is the lens's AF/MF switch set to MF: set it to "
                "AF (the body stays in MF)."
            )

    def _nudge_focus(self, step: int) -> None:
        # Signed Int16 on the wire; the binding layer speaks unsigned.
        self._binding.set_property("near_far", step & 0xFFFF)
        time.sleep(self._config.emulated_nudge_interval_s)

    # -- movie-mode follow focus -----------------------------------------

    def _exposure_mode(self) -> int:
        return int(self._binding.get_property("exposure_program_mode").value)

    def _wait_for(self, predicate: Callable[[], bool], what: str) -> None:
        timeout = float(self._config.movie_mode_timeout_s)
        deadline = time.monotonic() + timeout
        while True:
            try:
                if predicate():
                    return
            except BusyError:
                pass
            if time.monotonic() >= deadline:
                raise SDKError(f"timed out after {timeout:g}s waiting for {what}")
            time.sleep(_MODE_POLL_S)

    def _enter_movie(self) -> None:
        current = self._exposure_mode()
        if current in _MOVIE_TO_STILLS:
            # Already in movie: an earlier operation never made it back.
            if self._movie_restore is None:
                self._movie_restore = _MOVIE_TO_STILLS[current]
        else:
            movie = _STILLS_TO_MOVIE.get(current, _DEFAULT_MOVIE_MODE)
            # Recorded before the write, so a switch that half-happens is
            # still undone.
            self._movie_restore = current
            self._state_cache = None
            self._retry_busy(
                lambda: self._binding.set_property("exposure_program_mode", movie)
            )
            self._wait_for(
                lambda: self._exposure_mode() == movie,
                "the body to switch to movie mode",
            )
        self._wait_for(
            lambda: int(self._binding.get_property("lens_info_enable").value) == 1,
            "the lens to publish its focus position (LensInformationEnableStatus)",
        )

    def _exit_movie(self) -> None:
        stills = self._movie_restore
        if stills is None:
            return
        self._state_cache = None
        self._retry_busy(
            lambda: self._binding.set_property("exposure_program_mode", stills)
        )
        self._wait_for(
            lambda: self._exposure_mode() == stills,
            "the body to switch back to stills mode",
        )
        self._movie_restore = None

    def _in_movie(self, fn: Callable[[], Any]) -> Any:
        """Run `fn` with the body in movie mode, and always try to leave it.

        A failure to get back to stills is raised (the caller's work is moot
        if the next capture would record in movie mode), and `_movie_restore`
        stays set so the next capture retries the switch before firing.
        """
        try:
            self._enter_movie()
            result = fn()
        except BaseException:
            try:
                self._exit_movie()
            except CameraError as exc:
                self._log(
                    "error",
                    f"could not return the body to stills mode ({exc}); "
                    "captures retry the switch and fail until it takes",
                )
            raise
        self._exit_movie()
        return result

    def _recover_stills_mode(self) -> None:
        """At connect: a body left in movie mode by an interrupted focus
        operation goes back to the stills mode it was in."""
        try:
            current = self._exposure_mode()
        except CameraError as exc:
            self._log("warning", f"could not read the exposure program mode: {exc}")
            return
        stills = _MOVIE_TO_STILLS.get(current)
        if stills is None:
            return
        self._log(
            "warning",
            f"body is in movie mode (exposure program 0x{current:X}) at connect, "
            f"most likely from an interrupted focus operation; switching back "
            f"to stills (0x{stills:X})",
        )
        self._movie_restore = stills
        try:
            self._exit_movie()
        except CameraError as exc:
            detail = f"return to stills mode: {exc}"
            self._apply_errors.append(detail)
            self._log("error", f"apply_on_connect failed for {detail}")

    def _movie_read(self) -> int:
        """The lens's position, 0 (near stop) .. 65535 (far stop), once the
        read-back has stopped changing (the lens may still be moving)."""

        def once() -> int:
            raw = int(self._binding.get_property("follow_focus_position").value)
            return _FOLLOW_FOCUS_MAX - max(0, min(_FOLLOW_FOCUS_MAX, raw))

        value = once()
        deadline = time.monotonic() + _FOCUS_READ_SETTLE_MAX_S
        while time.monotonic() < deadline:
            time.sleep(_FOCUS_READ_POLL_S)
            again = once()
            if again == value:
                break
            value = again
        return value

    def _movie_drive(self, target: int, tolerance: int) -> Tuple[int, int]:
        """Closed loop: nudge toward `target` until the read-back is within
        `tolerance`. Returns (position, nudges). Must run in movie mode.

        Nudge size follows the remaining distance (one size-1 nudge is about
        `movie_units_per_nudge / emulated_step_size` units). An overshoot caps
        the size below the one that overshot; a nudge the lens ignored (a
        size-1 sometimes moves nothing) earns a bigger one.
        """
        per_size = float(self._config.movie_units_per_nudge) / max(
            1, int(self._config.emulated_step_size)
        )
        pos = self._movie_read()
        nudges = stalls = reversals = 0
        cap, last_sign, last_size = 7, 0, 0
        while abs(target - pos) > tolerance and nudges < int(self._config.movie_max_nudges):
            error = target - pos
            sign = 1 if error > 0 else -1  # positive near/far drives toward far
            if last_sign and sign != last_sign:
                reversals += 1
                if reversals > _FOCUS_MAX_REVERSALS:
                    break  # hunting around a target the steps can't land in
                cap = max(1, last_size - 1)
            size = max(1, min(cap, round(abs(error) / per_size)))
            size = min(7, size + stalls)
            self._nudge_focus(sign * size)
            nudges += 1
            last_sign, last_size = sign, size
            moved_to = self._movie_read()
            if moved_to == pos:
                at_stop = moved_to <= 0 if sign < 0 else moved_to >= _FOLLOW_FOCUS_MAX
                stalls += 1
                if at_stop or stalls > _FOCUS_MAX_STALLS:
                    break
            else:
                stalls = 0
            pos = moved_to
        return pos, nudges

    def _do_set_focus_movie(self, target: int, tolerance: int) -> Dict[str, Any]:
        self._require_near_far()
        clamped = max(0, min(int(target), _FOLLOW_FOCUS_MAX))
        if clamped != target:
            self._log(
                "warning",
                f"focus target {target} clamped to {clamped} "
                f"(valid range 0..{_FOLLOW_FOCUS_MAX})",
            )
        self._movie_known = None
        position, nudges = self._in_movie(lambda: self._movie_drive(clamped, tolerance))
        self._movie_known = position
        self._state_cache = None
        ok = abs(position - clamped) <= tolerance
        if not ok:
            self._log(
                "warning",
                f"focus did not reach {clamped} after {nudges} nudge(s): read "
                f"back {position} (tolerance {tolerance}). If it never moved, "
                "check that nothing is holding the focus ring.",
            )
        return {
            "position": position,
            "target": clamped,
            "tolerance": tolerance,
            "attempts": nudges,
            "ok": ok,
            "units": "follow_focus",
            "method": "movie",
        }

    def _do_read_focus_movie(self) -> int:
        position = self._in_movie(self._movie_read)
        self._movie_known = position
        return position

    # -- emulated (nudge) focus ------------------------------------------

    def _nudge_units(self) -> bool:
        """Whether positions are follow-focus units even though the emulation
        is driving: the movie backend's fallback keeps the caller's units."""
        return self._config.focus_method == "movie"

    def _from_nudges(self, count: int) -> int:
        if self._nudge_units():
            return int(round(count * float(self._config.movie_units_per_nudge)))
        return count

    def _ensure_focus_homed(self) -> None:
        if self._focus_homed:
            return
        budget = int(self._config.emulated_travel_nudges)
        step = int(self._config.emulated_step_size)
        self._require_near_far()
        self._log(
            "info",
            f"homing focus: {budget} nudges of size {step} toward the near stop",
        )
        self._ensure_manual_focus()
        for _ in range(budget):
            self._nudge_focus(-step)
        self._focus_counter = 0
        self._focus_homed = True
        self._state_cache = None

    def _home_emulated(self) -> Dict[str, Any]:
        self._focus_homed = False
        self._ensure_focus_homed()
        units = "follow_focus" if self._nudge_units() else "emulated_nudges"
        return {"emulated": True, "position": 0, "units": units}

    def _do_home_focus(self) -> Dict[str, Any]:
        if self._config.focus_method == "movie" and self._focus_backend == "nudge":
            # A fallback taken earlier gets another chance at every home: the
            # webapp homes once per session, so one bad moment doesn't stick
            # for the rest of the connection.
            self._focus_backend = None
            self._focus_fallback_reason = None
        backend = self._resolve_focus_backend()
        if backend == "movie":

            def movie() -> Dict[str, Any]:
                position = self._do_read_focus_movie()
                return {
                    "emulated": False,
                    "method": "movie",
                    "position": position,
                    "units": "follow_focus",
                }

            return self._movie_or_fallback(movie, self._home_emulated)
        if backend == "nudge":
            return self._home_emulated()
        return {
            "emulated": False,
            "note": "this body reports absolute focus natively; homing is "
            "not used",
        }

    def _do_set_focus_emulated(self, target: int) -> Dict[str, Any]:
        self._require_near_far()
        self._ensure_focus_homed()
        per_nudge = float(self._config.movie_units_per_nudge)
        wanted = int(round(target / per_nudge)) if self._nudge_units() else int(target)
        limit = int(self._config.emulated_travel_nudges)
        clamped = max(0, min(wanted, limit))
        if clamped != wanted:
            self._log(
                "warning",
                f"emulated focus target {wanted} clamped to {clamped} "
                f"(valid range 0..{limit})",
            )
        step = int(self._config.emulated_step_size)
        sign = 1 if clamped > self._focus_counter else -1
        for _ in range(abs(clamped - self._focus_counter)):
            self._nudge_focus(sign * step)
            self._focus_counter += sign
        self._state_cache = None
        if self._nudge_units():
            return {
                "position": self._from_nudges(self._focus_counter),
                "target": int(target),
                "tolerance": 0,
                "attempts": 1,
                "ok": True,
                "units": "follow_focus",
                "method": "nudge_fallback",
                # Converted from a nudge count, not read back.
                "estimated": True,
            }
        return {
            "position": self._focus_counter,
            "target": clamped,
            "tolerance": 0,
            "attempts": 1,
            "ok": True,
            "units": "emulated_nudges",
        }

    def _do_get_focus(self) -> int:
        backend = self._resolve_focus_backend()
        if backend == "movie":

            def nudge() -> int:
                self._ensure_focus_homed()
                return self._from_nudges(self._focus_counter)

            return self._movie_or_fallback(self._do_read_focus_movie, nudge)
        if backend == "nudge":
            self._ensure_focus_homed()
            return self._from_nudges(self._focus_counter)
        value = self._binding.get_property("focus_position").value
        return int(value)

    def _do_set_focus(self, position: int, tolerance: Optional[int]) -> Dict[str, Any]:
        self._ensure_manual_focus()
        backend = self._resolve_focus_backend()
        if backend == "movie":
            tol = int(self._config.movie_focus_tolerance if tolerance is None else tolerance)
            return self._movie_or_fallback(
                lambda: self._do_set_focus_movie(position, tol),
                lambda: self._do_set_focus_emulated(position),
            )
        if backend == "nudge":
            return self._do_set_focus_emulated(position)
        tolerance = int(self._config.focus_tolerance if tolerance is None else tolerance)

        achieved = None
        attempts = 0
        # One retry A second miss is a real condition (the
        # lens is at a mechanical stop, or the body took focus back) and is
        # reported as ok=false rather than retried forever - the caller decides
        # whether a slightly-off focus is acceptable for that station.
        for _ in range(2):
            attempts += 1
            self._binding.set_property("focus_position", position)
            time.sleep(_FOCUS_SETTLE_S)
            achieved = int(self._binding.get_property("focus_position").value)
            if abs(achieved - position) <= tolerance:
                break

        self._state_cache = None
        ok = achieved is not None and abs(achieved - position) <= tolerance
        if not ok:
            self._log(
                "warning",
                f"focus did not reach {position} after {attempts} attempt(s): "
                f"read back {achieved} (tolerance {tolerance})",
            )
        return {
            "position": achieved,
            "target": position,
            "tolerance": tolerance,
            "attempts": attempts,
            "ok": ok,
            "units": "sdk_raw",
        }

    def _ensure_manual_focus(self) -> None:
        """Put the lens under our control before commanding a position.

        Best-effort: a body that doesn't expose `focus_mode` isn't a reason to
        refuse the focus command, and the read-back in `_do_set_focus` will
        catch it if the write doesn't take.
        """
        try:
            mode = self._binding.get_property("focus_mode")
        except CameraError:
            return
        if str(mode.value) in _MANUAL_FOCUS_MODES:
            return
        try:
            self._binding.set_property("focus_mode", "MF")
            self._log(
                "info",
                f"focus mode was {mode.value!r}; switched to MF so the focus "
                "position can be set",
            )
        except CameraError as exc:
            self._log(
                "warning",
                f"could not switch focus mode from {mode.value!r} to MF ({exc}); "
                "setting an absolute focus position may not stick",
            )

    def _do_autofocus(self) -> Dict[str, Any]:
        acquired = self._binding.autofocus_once(self._config.autofocus_timeout_s)
        self._state_cache = None
        backend = self._resolve_focus_backend()
        if backend == "movie":
            # Reading where AF landed would cost two mode switches; the
            # caller can ask get_focus_position if it wants to know.
            self._movie_known = None
            return {"position": None, "units": "follow_focus", "acquired": acquired}
        if backend == "nudge":
            # AF moved the lens an unknown amount; the counter is now a lie.
            self._focus_homed = False
            self._log(
                "info",
                "autofocus moved the lens; emulated focus positions are invalid "
                "until the next focus operation re-homes",
            )
            return {"position": None, "units": "emulated_nudges", "acquired": acquired}
        position = int(self._binding.get_property("focus_position").value)
        if not acquired:
            self._log("warning", f"one-shot AF did not lock; focus is at {position}")
        return {"position": position, "units": "sdk_raw", "acquired": acquired}

    # ------------------------------------------------------------------
    # Power zoom - owner thread only
    #
    # Zoom_Operation is a continuous drive and ZoomPositionSetting is refused
    # over USB, but ZoomDistance (the focal length, 0.001mm) is readable. So
    # absolute zoom is a closed loop - drive toward the target, poll, stop,
    # re-read, correct at lower speed - and positions are real millimetres
    # rather than counts. Zooming a PZ lens can move its focus group, so any
    # zoom movement invalidates emulated focus and the next focus op re-homes.
    # ------------------------------------------------------------------

    def _read_optional(self, name: str):
        try:
            return self._binding.get_property(name)
        except CameraError:
            return None

    def _zoom_distance_um(self) -> int:
        prop = self._read_optional("zoom_distance")
        if prop is None or prop.value is None:
            raise UnsupportedValueError(
                "this body/lens does not report a zoom focal length; set_zoom "
                "needs a power-zoom lens"
            )
        return int(prop.value)

    def _zoom_speed_limits(self) -> Tuple[int, int]:
        """(slowest-wide, fastest-tele) bounds; (-1, 1) when the body doesn't
        report a range, which is what Sony's sample assumes too."""
        prop = self._read_optional("zoom_speed_range")
        if prop is not None and len(prop.choices) >= 2:
            lo, hi = int(prop.choices[0]), int(prop.choices[1])
            if lo < 0 < hi:
                return lo, hi
        return -1, 1

    def _require_zoom_drive(self) -> None:
        prop = self._read_optional("zoom_operation_status")
        if prop is None or int(prop.value or 0) != _ZOOM_ENABLED:
            raise UnsupportedValueError(
                "the zoom drive is not available (Zoom_Operation_Status is not "
                "Enable): it needs a power-zoom lens, optical zoom, and the "
                "body not busy"
            )

    def _write_zoom_speed(self, speed: int) -> None:
        self._binding.set_property("zoom_operation", int(speed))
        if speed != 0:
            self._note_zoom_moved()

    def _stop_zoom(self) -> None:
        """Must not raise: it runs on every exit path of a drive."""
        self._zoom_drive_deadline = None
        try:
            self._binding.set_property("zoom_operation", 0)
        except CameraError as exc:
            self._log("warning", f"zoom stop write failed: {exc}")

    def _zoom_watchdog_stop(self) -> None:
        self._log(
            "warning",
            f"zoom drive still running after {self._config.zoom_max_drive_s}s "
            "with no new command; stopping it",
        )
        self._stop_zoom()

    def _note_zoom_moved(self) -> None:
        self._state_cache = None
        self._movie_known = None
        if self._focus_backend == "nudge" and self._focus_homed:
            self._focus_homed = False
            self._log(
                "info",
                "zoom moved the lens; emulated focus re-homes on the next "
                "focus operation",
            )

    def _do_get_zoom(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"units": "mm"}
        dist = self._read_optional("zoom_distance")
        if dist is not None and dist.value is not None:
            out["focal_length_mm"] = int(dist.value) / 1000.0
            if dist.range and len(dist.choices) >= 2:
                out["min_mm"] = int(dist.choices[0]) / 1000.0
                out["max_mm"] = int(dist.choices[1]) / 1000.0
                if len(dist.choices) >= 3 and int(dist.choices[2]) > 0:
                    out["step_mm"] = int(dist.choices[2]) / 1000.0
        else:
            out["focal_length_mm"] = None
        lo, hi = self._zoom_speed_limits()
        out["speed_range"] = [lo, hi]
        status = self._read_optional("zoom_operation_status")
        out["drive_available"] = (
            status is not None and int(status.value or 0) == _ZOOM_ENABLED
        )
        bar = self._read_optional("zoom_bar")
        if bar is not None and bar.value is not None:
            raw = int(bar.value)
            # Bits 31-24 total boxes, 23-16 current box, 15-0 position 0-100
            # within it: the same bar the body draws on its own screen.
            out["bar"] = {
                "boxes": (raw >> 24) & 0xFF,
                "box": (raw >> 16) & 0xFF,
                "position_pct": raw & 0xFFFF,
            }
        scale = self._read_optional("zoom_scale")
        if scale is not None and scale.value is not None:
            out["scale"] = int(scale.value) / 1000.0
        ztype = self._read_optional("zoom_type_status")
        if ztype is not None and ztype.value is not None:
            out["zoom_type"] = {1: "optical", 2: "smart", 3: "clear_image", 4: "digital"}.get(
                int(ztype.value), int(ztype.value)
            )
        out["driving"] = self._zoom_drive_deadline is not None
        return out

    def _clamp_speed(self, speed: int) -> int:
        lo, hi = self._zoom_speed_limits()
        clamped = max(lo, min(hi, int(speed)))
        if clamped != speed:
            self._log("warning", f"zoom speed {speed} clamped to {clamped} (range {lo}..{hi})")
        return clamped

    def _do_zoom_drive(self, speed: int, duration_s: Optional[float]) -> Dict[str, Any]:
        if speed == 0:
            self._stop_zoom()
            return {"speed": 0, "driving": False, **self._zoom_reading()}
        self._require_zoom_drive()
        speed = self._clamp_speed(speed)
        if duration_s:
            try:
                self._write_zoom_speed(speed)
                time.sleep(max(0.0, float(duration_s)))
            finally:
                self._stop_zoom()
            return {
                "speed": speed,
                "driving": False,
                "focal_length_mm": self._settled_zoom_um() / 1000.0,
            }
        self._write_zoom_speed(speed)
        self._zoom_drive_deadline = time.monotonic() + float(self._config.zoom_max_drive_s)
        return {"speed": speed, "driving": True, **self._zoom_reading()}

    def _settled_zoom_um(self) -> int:
        """Read the focal length once the lens has stopped coasting."""
        current = self._zoom_distance_um()
        stable_since = time.monotonic()
        give_up = stable_since + _ZOOM_SETTLE_MAX_S
        while time.monotonic() < give_up:
            time.sleep(_ZOOM_POLL_S)
            reading = self._zoom_distance_um()
            if reading != current:
                current = reading
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= _ZOOM_STABLE_S:
                break
        return current

    def _zoom_reading(self) -> Dict[str, Any]:
        try:
            return {"focal_length_mm": self._zoom_distance_um() / 1000.0}
        except CameraError:
            return {"focal_length_mm": None}

    def _do_set_zoom(self, target_mm: float, tolerance_mm: float) -> Dict[str, Any]:
        self._require_zoom_drive()
        dist = self._binding.get_property("zoom_distance")
        target = int(round(target_mm * 1000))
        tol = max(0, int(round(tolerance_mm * 1000)))
        if dist.range and len(dist.choices) >= 2:
            lo_um, hi_um = int(dist.choices[0]), int(dist.choices[1])
            clamped = max(lo_um, min(hi_um, target))
            if clamped != target:
                self._log(
                    "warning",
                    f"zoom target {target_mm}mm clamped to {clamped / 1000}mm "
                    f"(lens range {lo_um / 1000}-{hi_um / 1000}mm)",
                )
                target = clamped
            span = max(1, hi_um - lo_um)
            step = int(dist.choices[2]) if len(dist.choices) >= 3 else 0
            if step > 0:
                # The body only reports focal lengths on its step grid; a
                # target between grid points can never read back, so aim for
                # the nearest one instead of hunting.
                snapped = lo_um + int(round((target - lo_um) / step)) * step
                snapped = max(lo_um, min(hi_um, snapped))
                if snapped != target:
                    self._log(
                        "debug",
                        f"zoom target {target / 1000}mm snapped to the lens's "
                        f"{step / 1000}mm grid: {snapped / 1000}mm",
                    )
                    target = snapped
        else:
            span = max(1, abs(target - int(dist.value)) * 4)

        _, max_speed = self._zoom_speed_limits()
        deadline = time.monotonic() + float(self._config.zoom_timeout_s)
        current = int(dist.value)
        passes = 0
        speed_cap = max_speed
        last_direction = 0
        min_speed_reversals = 0
        best = current
        resolution_limited = False
        try:
            while abs(target - current) > tol and passes < _ZOOM_MAX_PASSES:
                if time.monotonic() >= deadline:
                    break
                error = target - current
                direction = 1 if error > 0 else -1
                if last_direction and direction != last_direction:
                    if speed_cap == 1:
                        # Overshooting back and forth at the slowest speed:
                        # the target sits between two positions this lens can
                        # report (the FE PZ 16-35 advertises a 0.1mm step but
                        # reads back in 0.5mm), so more passes only hunt.
                        min_speed_reversals += 1
                        if min_speed_reversals >= 2:
                            resolution_limited = True
                            break
                    # Overshot: come back slower so the next stop lands closer.
                    speed_cap = max(1, speed_cap // 2)
                last_direction = direction
                passes += 1
                fraction = abs(error) / span
                if fraction > 0.25:
                    magnitude = max_speed
                elif fraction > 0.05:
                    magnitude = max(1, (max_speed + 1) // 2)
                else:
                    magnitude = 1
                magnitude = min(magnitude, speed_cap)

                self._write_zoom_speed(direction * magnitude)
                last_change = time.monotonic()
                previous = current
                while time.monotonic() < deadline:
                    time.sleep(_ZOOM_POLL_S)
                    current = self._zoom_distance_um()
                    if (target - current) * direction <= tol:
                        break  # reached or crossed the target window
                    if current != previous:
                        previous = current
                        last_change = time.monotonic()
                    elif time.monotonic() - last_change >= _ZOOM_STALL_S:
                        break  # end stop, or the body ignored the drive
                self._stop_zoom()
                current = self._settled_zoom_um()
                if abs(target - current) < abs(target - best):
                    best = current
            if resolution_limited and best != current:
                # Finish on the closer of the two bracketing positions.
                direction = 1 if best > current else -1
                self._write_zoom_speed(direction)
                while time.monotonic() < deadline:
                    time.sleep(_ZOOM_POLL_S)
                    if (best - self._zoom_distance_um()) * direction <= 0:
                        break
                self._stop_zoom()
                current = self._settled_zoom_um()
        finally:
            self._stop_zoom()

        ok = abs(target - current) <= tol
        if not ok:
            self._log(
                "warning",
                f"zoom did not reach {target / 1000}mm after {passes} pass(es): "
                f"at {current / 1000}mm (tolerance {tol / 1000}mm)",
            )
        return {
            "focal_length_mm": current / 1000.0,
            "target_mm": target / 1000.0,
            "tolerance_mm": tol / 1000.0,
            "passes": passes,
            "ok": ok,
            "resolution_limited": resolution_limited,
            "units": "mm",
        }

    def _do_zoom_preset(self, action: str, slot: int) -> Dict[str, Any]:
        if not 0 <= slot <= 255:
            raise UnsupportedValueError(f"preset slot must be 0..255, got {slot}")
        if action == "save":
            self._binding.set_property("zoom_focus_preset_save", slot)
        elif action == "load":
            before = self._zoom_distance_um()
            self._binding.set_property("zoom_focus_preset_load", slot)
            give_up = time.monotonic() + _ZOOM_PRESET_START_S
            while time.monotonic() < give_up and self._zoom_distance_um() == before:
                time.sleep(_ZOOM_POLL_S)
            # The body restores focus as well as zoom, behind the emulation's
            # back; _note_zoom_moved invalidates both.
            self._note_zoom_moved()
            self._settled_zoom_um()
        else:
            raise UnsupportedValueError(f"unknown preset action {action!r}")
        return {"action": action, "slot": slot, **self._zoom_reading()}

    def _do_status(self) -> Dict[str, Any]:
        info = dict(self._device)
        if self._connected:
            try:
                info.update(self._binding.device_info())
            except CameraError as exc:
                self._log("debug", f"device_info unavailable: {exc}")

        return {
            "connected": bool(self._connected),
            "model": info.get("model") or "",
            "serial": info.get("serial") or "",
            "battery_pct": info.get("battery_pct"),
            "lens": info.get("lens"),
            "capture_dir": self._config.capture_dir,
            "capture_count": self._store.capture_count,
            "connect_attempts": self._connect_attempts,
            "last_error": self._last_error,
            "apply_errors": list(self._apply_errors),
            "focus_emulated": self._focus_backend == "nudge",
            "focus_homed": (
                bool(self._focus_homed) if self._focus_backend == "nudge" else None
            ),
            # The backend actually in use (None until the first focus
            # operation of this connection), and why a configured "movie"
            # isn't it.
            "focus_method": self._focus_backend,
            "focus_fallback_reason": self._focus_fallback_reason,
        }

    # ------------------------------------------------------------------
    # Property helpers - owner thread only
    # ------------------------------------------------------------------

    def _retry_busy(self, write: Callable[[], None]) -> None:
        """One settle-and-retry for a write the body reports as busy.

        Observed on the A7R V during the connect sequence: a write fired too
        soon after the handshake or the priority-key change bounces with
        Api_InvalidCalled (busy category), and the same write succeeds moments
        later. Anything still busy after the settle is a real error.
        """
        try:
            write()
        except BusyError:
            time.sleep(_APPLY_RETRY_DELAY_S)
            write()

    def _set_property(self, key: str, raw: Any) -> None:
        """Write one setting, turning a rejection into an actionable error.

        The camera's own list of accepted values is decoded back into config
        vocabulary, so the error says `f/1.4 ... camera accepts: f/1.8, f/2 ...`
        rather than quoting raw hundredths.
        """
        setting = settings_mod.SETTINGS[key]
        try:
            self._binding.set_property(setting.prop, raw)
        except UnsupportedValueError as exc:
            valid = settings_mod.describe_choices(key, exc.details.get("valid") or [])
            if not valid:
                try:
                    valid = settings_mod.describe_choices(
                        key, self._binding.get_property(setting.prop).choices
                    )
                except CameraError:
                    valid = []
            raise UnsupportedValueError(
                f"the camera rejected {key}: {exc.message}", valid=valid, setting=key
            ) from exc

    def _read_state(self, refresh: bool) -> Dict[str, Any]:
        """Current settings + focus position, cached between property changes.

        A capture wants all of this for its audit line, and every entry is a USB
        round trip. Caching is safe because the only things that change it are
        our own writes and the body's `property_changed` notification, and both
        drop the cache.
        """
        if self._state_cache is not None and not refresh:
            return self._state_cache

        values: Dict[str, Any] = {}
        for key, setting in settings_mod.SETTINGS.items():
            try:
                raw = self._binding.get_property(setting.prop).value
                values[key] = setting.decode(raw)
            except CameraError:
                # A property this body doesn't have is absent from the answer
                # rather than fatal - `get_settings` must not fail because one
                # entry is unsupported.
                continue

        # Never resolves a "movie" backend here: that would switch modes in
        # the middle of a capture's audit read. Movie reports the last
        # read-back, or None once anything may have moved the lens since.
        if self._config.focus_method == "movie" and self._focus_backend != "nudge":
            focus = self._movie_known
        elif self._focus_is_emulated():
            focus = self._from_nudges(self._focus_counter) if self._focus_homed else None
        else:
            try:
                focus = int(self._binding.get_property("focus_position").value)
            except (CameraError, TypeError, ValueError):
                focus = None

        self._state_cache = {"settings": values, "focus_position": focus}
        return self._state_cache

    def _clear_frame(self) -> None:
        with self._frame_lock:
            self._frame = None
            self._frame_at = 0.0

    def _log(self, level: str, message: str) -> None:
        if self._logger is None:
            return
        getattr(self._logger, level, self._logger.info)(message)


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
