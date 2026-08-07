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
# retries; too long and each station in a sweep pays for it. See SMOKE.md.
_FOCUS_SETTLE_S = 0.15

# Default tolerance for `set_focus_position`, in raw SDK units. Focus is a
# mechanism: commanding 128 and reading back 128 every time is not something to
# count on, so the contract is "within tolerance", not "exact".
_DEFAULT_FOCUS_TOLERANCE = 2

# Focus modes in which the focus position property is writable. Anything else
# and the lens is under the body's AF control, and a write either errors or is
# silently overridden on the next half-press.
_MANUAL_FOCUS_MODES = ("MF", "DMF")

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
        handing back a stale frame (scope.md §5).
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

    def set_settings(self, values: Dict[str, Any]) -> Dict[str, Any]:
        encoded = settings_mod.validate_all(values)  # raises before touching the camera
        return self._submit("set_settings", lambda: self._do_set_settings(encoded))

    def get_focus_position(self) -> int:
        return self._submit("get_focus_position", self._do_get_focus)

    def set_focus_position(
        self, position: int, tolerance: Optional[int] = None
    ) -> Dict[str, Any]:
        tol = int(self._config.focus_tolerance if tolerance is None else tolerance)
        return self._submit(
            "set_focus_position", lambda: self._do_set_focus(int(position), tol)
        )

    def autofocus_once(self) -> Dict[str, Any]:
        return self._submit(
            "autofocus_once", self._do_autofocus, timeout=self._config.autofocus_timeout_s
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
            self._apply_on_connect()
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

        state = self._read_state(refresh=False)

        # Flush anything the SDK queued before this trigger. A `file_written`
        # for the *previous* capture can still be sitting there - if the last
        # shot was resolved by the directory diff, or timed out, its event
        # arrives late - and picking it up here would make this capture return
        # the previous shot's path. Same hazard, same fix, as clearing the event
        # queue before a move in `comxim`.
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

        # The audit trail (scope.md §6): everything needed to answer "why does
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

    def _do_get_focus(self) -> int:
        value = self._binding.get_property("focus_position").value
        return int(value)

    def _do_set_focus(self, position: int, tolerance: int) -> Dict[str, Any]:
        self._ensure_manual_focus()

        achieved = None
        attempts = 0
        # One retry, per scope.md §5. A second miss is a real condition (the
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
        position = int(self._binding.get_property("focus_position").value)
        if not acquired:
            self._log("warning", f"one-shot AF did not lock; focus is at {position}")
        return {"position": position, "units": "sdk_raw", "acquired": acquired}

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
