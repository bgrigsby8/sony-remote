"""
store.py
--------
Everything about `capture_dir`: what's in it, what's new, when a file is
finished being written, what to delete, and how many times the shutter has
fired.

The camera writes into this directory behind our back (the SDK does the actual
file I/O once `set_save_destination` points at it), so this module is written
defensively: it never assumes a file it can see is complete, and never assumes
the SDK told us the name.

It also never assumes it is alone in the directory. A production machine had
`capture_dir` shared with `color-correction`'s output directory, and a
`DSC00432.jpg` that component exported between the pre-shutter snapshot and the
SDK's `file_written` was returned as the capture (the real `DSC00470.ARW`
landed unreported, and retention later deleted the other component's files).
So a file is only ever *this module's* if it passes two filters - the extension
the body's current file format can produce, and the camera's `DSCnnnnn.<ext>`
naming - and retention only counts files this module itself recorded writing.

All of it is blocking filesystem work and all of it runs on the session's owner
thread.
"""

import json
import os
import re
import time
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Extensions the camera can write. RAW first - when a capture produces both a
# RAW and a JPEG, the RAW is the one downstream wants (color-correction
# demosaics it), so `primary_file` prefers it.
RAW_EXTS = (".arw", ".raw", ".dng")
JPEG_EXTS = (".jpg", ".jpeg")
HEIF_EXTS = (".heif", ".heic")
IMAGE_EXTS = RAW_EXTS + JPEG_EXTS + HEIF_EXTS

# What each `file_format` (as `settings.py` decodes it, plus the SDK's own
# symbolic spelling in case a body reports one we don't decode) can write. A
# capture in "raw" can never legitimately be a .jpg, however plausible its name.
_FORMAT_EXTS: Dict[str, Tuple[str, ...]] = {
    "raw": RAW_EXTS,
    "jpeg": JPEG_EXTS,
    "jpg": JPEG_EXTS,
    "heif": HEIF_EXTS,
    "raw+jpeg": RAW_EXTS + JPEG_EXTS,
    "raw_jpeg": RAW_EXTS + JPEG_EXTS,
    "rawjpeg": RAW_EXTS + JPEG_EXTS,
    "raw+heif": RAW_EXTS + HEIF_EXTS,
    "raw_heif": RAW_EXTS + HEIF_EXTS,
}

# How Sony bodies name stills: DSC + five digits + extension. The optional
# leading underscore is real - a body set to the AdobeRGB colour space writes
# `_DSC00001.ARW` - and costs nothing against the failure this guards, which
# is another program's file with a different shape entirely.
CAMERA_NAME_RE = re.compile(r"^_?DSC\d{5}\.[A-Za-z0-9]+$", re.IGNORECASE)

_EXT_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".heif": "image/heif",
    ".heic": "image/heic",
}

# Name of the small state file kept alongside the captures. Dot-prefixed so it
# never shows up in an image listing, and JSON so an operator can read it.
_STATE_FILE = ".sony-remote-state.json"

# How a file is judged "finished": non-zero, and the same size for this long.
# The SDK writes an 80MB RAW in chunks and a consumer that opens it too early
# gets a truncated frame - the failure `ptp.py` hit downloading from a Canon
# mid-write.
#
# Size stability is a heuristic, not a proof: a writer that stalls for longer
# than the window looks finished. That is why `session` only uses this on the
# directory-diff path, where nothing else tells us when the write ended. When
# the SDK names the file in a `file_written` event, the event itself is the
# completion signal and this wait is skipped entirely - which also keeps a
# quarter-second off every shot in a sweep.
_SETTLE_INTERVAL_S = 0.05
_SETTLE_WINDOW_S = 0.25
_SETTLE_STABLE_READS = max(2, int(_SETTLE_WINDOW_S / _SETTLE_INTERVAL_S))


def mime_for(name: str) -> str:
    """MIME type for a capture. RAW is opaque bytes, not a previewable image."""
    _, ext = os.path.splitext(name.lower())
    return _EXT_TO_MIME.get(ext, "application/octet-stream")


def is_raw(name: str) -> bool:
    return name.lower().endswith(RAW_EXTS)


def is_image(name: str) -> bool:
    return name.lower().endswith(IMAGE_EXTS)


def is_camera_name(name: str) -> bool:
    """Whether `name` (a path or basename) is shaped like a still the body wrote."""
    return CAMERA_NAME_RE.match(os.path.basename(name)) is not None


def exts_for_format(file_format: Optional[str]) -> Tuple[str, ...]:
    """Extensions a capture can produce under `file_format`.

    Unknown or missing (a body whose format property we couldn't read) falls
    back to every image type rather than refusing to shoot: the name filter
    still applies, and a wrong format is a settings problem, not a capture one.
    """
    if file_format is None:
        return IMAGE_EXTS
    return _FORMAT_EXTS.get(str(file_format).strip().lower(), IMAGE_EXTS)


class CaptureStore:
    """Owns one `capture_dir`."""

    def __init__(
        self,
        directory: str,
        max_files: int = 200,
        logger=None,
        strict_names: bool = True,
    ):
        self.directory = directory
        self.max_files = int(max_files)
        # Whether a capture candidate must also look like `DSCnnnnn.<ext>`.
        # Off only for a body configured with a custom file-name prefix.
        self.strict_names = bool(strict_names)
        self._logger = logger
        self._state_path = os.path.join(directory, _STATE_FILE)
        self._capture_count = 0
        # Basenames this module has recorded as its own captures, oldest first.
        # Retention works from this list, never from the directory listing.
        self._written: List[str] = []
        self._loaded = False

    # ------------------------------------------------------------------
    # Directory
    # ------------------------------------------------------------------

    def ensure_dir(self) -> None:
        os.makedirs(self.directory, exist_ok=True)

    def snapshot(self) -> Set[str]:
        """Filenames present right now - the "before" half of a capture diff."""
        try:
            return {n for n in os.listdir(self.directory) if is_image(n)}
        except FileNotFoundError:
            return set()

    def accepts(self, name: str, exts: Sequence[str] = IMAGE_EXTS) -> bool:
        """Whether `name` could be a still this body just wrote.

        Two filters: the extension must be one the current file format can
        produce (`exts_for_format`), and unless `strict_names` is off the
        basename must be shaped like the camera's own `DSCnnnnn.<ext>`. Another
        program's export landing in the same directory fails one or both.
        """
        base = os.path.basename(name)
        if not base.lower().endswith(tuple(exts)):
            return False
        return not self.strict_names or is_camera_name(base)

    def new_files_since(
        self, before: Set[str], exts: Sequence[str] = IMAGE_EXTS
    ) -> List[str]:
        """Absolute paths of candidate stills that appeared since `before`.

        Oldest first, newest last, by mtime. The fallback for bodies (or SDK
        versions) whose completion notification carries no filename - see
        `session._await_files`. Only files passing `accepts(name, exts)`
        count, so a file another component dropped here mid-capture is not
        mistaken for the shot.
        """
        try:
            names = [
                n
                for n in os.listdir(self.directory)
                if n not in before and self.accepts(n, exts)
            ]
        except FileNotFoundError:
            return []
        paths = [os.path.join(self.directory, n) for n in names]
        paths.sort(key=lambda p: (_safe_mtime(p), p))
        return paths

    def list_images(self) -> List[str]:
        """Every image in the directory, oldest first."""
        try:
            names = [n for n in os.listdir(self.directory) if is_image(n)]
        except FileNotFoundError:
            return []
        paths = [os.path.join(self.directory, n) for n in names]
        paths.sort(key=lambda p: (_safe_mtime(p), p))
        return paths

    def wait_until_settled(self, path: str, deadline: float) -> int:
        """Block until `path`'s size stops changing; return the final size.

        Returns 0 if the deadline passes first, which the caller treats as a
        capture timeout - a file we can see but can't trust is not a capture.
        """
        stable = 0
        last = -1
        while time.monotonic() < deadline:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = -1
            if size > 0 and size == last:
                stable += 1
                if stable >= _SETTLE_STABLE_READS:
                    return size
            else:
                stable = 0
            last = size
            time.sleep(_SETTLE_INTERVAL_S)
        return 0

    def record_written(self, paths: Iterable[str]) -> None:
        """Remember `paths` as captures this module produced.

        Called once per successful capture; the basenames go into the state
        file so retention still knows what is ours after a restart.
        """
        self._load_state()
        added = False
        for path in paths:
            base = os.path.basename(path)
            if base and base not in self._written:
                self._written.append(base)
                added = True
        if added:
            self._save_state()

    def list_owned(self) -> List[str]:
        """Captures this module recorded writing and that still exist, oldest
        first. Entries whose file has gone (an operator's `rm`, a `cleanup`)
        are dropped from the record on the way through."""
        self._load_state()
        present = [
            os.path.join(self.directory, n)
            for n in self._written
            if os.path.exists(os.path.join(self.directory, n))
        ]
        if len(present) != len(self._written):
            self._written = [os.path.basename(p) for p in present]
            self._save_state()
        present.sort(key=lambda p: (_safe_mtime(p), p))
        return present

    def prune(self) -> List[str]:
        """Delete the oldest of *our* captures beyond `max_files`. Returns what went.

        Only files this module recorded writing (`record_written`) are counted
        or deleted, so the state file, anything an operator dropped in the
        directory, and another component's output in a shared directory all
        survive - retention must never reach past this module's own work.
        `max_files <= 0` disables retention entirely.
        """
        if self.max_files <= 0:
            return []
        owned = self.list_owned()
        excess = len(owned) - self.max_files
        if excess <= 0:
            return []

        removed = []
        for path in owned[:excess]:
            try:
                os.remove(path)
                removed.append(path)
            except OSError as exc:
                self._log("warning", f"could not remove {path}: {exc}")
        if removed:
            self._forget(removed)
            self._log(
                "info",
                f"retention: removed {len(removed)} file(s) from {self.directory} "
                f"(max_files={self.max_files})",
            )
        return removed

    def remove(self, paths: List[str]) -> List[str]:
        """Delete specific files, ignoring ones already gone."""
        removed = []
        for path in paths:
            try:
                os.remove(path)
                removed.append(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                self._log("warning", f"could not remove {path}: {exc}")
        if removed:
            self._load_state()
            self._forget(removed)
        return removed

    def _forget(self, paths: Iterable[str]) -> None:
        gone = {os.path.basename(p) for p in paths}
        kept = [n for n in self._written if n not in gone]
        if len(kept) != len(self._written):
            self._written = kept
            self._save_state()

    # ------------------------------------------------------------------
    # Shutter counter
    #
    # Mechanical-shutter wear tracking. The body has its own
    # internal count that CrSDK doesn't expose, so this counts what *we* fired.
    # It has to survive restarts to mean anything, hence the state file.
    # ------------------------------------------------------------------

    @property
    def capture_count(self) -> int:
        self._load_state()
        return self._capture_count

    def increment_capture_count(self) -> int:
        self._load_state()
        self._capture_count += 1
        self._save_state()
        return self._capture_count

    def set_capture_count(self, value: int) -> int:
        """Seed the counter - e.g. from the body's own actuation count read off
        a service menu, so the number means total shutter life rather than
        life-since-this-module."""
        self._load_state()
        self._capture_count = max(0, int(value))
        self._save_state()
        return self._capture_count

    def _load_state(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            self._capture_count = int(state.get("capture_count", 0))
            written = state.get("written")
            if written is None:
                # A state file from before ownership tracking. Nothing already
                # here is provably ours, so nothing already here is ever pruned:
                # the operator decides what to do with it (`cleanup` empties
                # the directory), and retention applies from this shot on.
                self._written = []
                legacy = len(self.list_images())
                if legacy:
                    self._log(
                        "warning",
                        f"retention: {legacy} pre-existing image(s) in "
                        f"{self.directory} predate ownership tracking and will "
                        "not be pruned; delete them by hand or with `cleanup` "
                        "if they are this module's",
                    )
            else:
                self._written = [str(n) for n in written if isinstance(n, str)]
        except FileNotFoundError:
            self._capture_count = 0
            self._written = []
        except (OSError, ValueError, TypeError) as exc:
            # A corrupt state file must not stop the module from taking
            # pictures. Losing the count is bad; refusing to shoot is worse.
            self._log("warning", f"ignoring unreadable {self._state_path}: {exc}")
            self._capture_count = 0
            self._written = []

    def _save_state(self) -> None:
        state: Dict[str, object] = {
            "capture_count": self._capture_count,
            "written": list(self._written),
            "updated": time.time(),
        }
        temp = self._state_path + ".tmp"
        try:
            self.ensure_dir()
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            # Rename is atomic on POSIX, so a crash mid-write leaves the old
            # count rather than a half-written file that reads as zero.
            os.replace(temp, self._state_path)
        except OSError as exc:
            self._log("warning", f"could not persist capture count: {exc}")

    # ------------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        if self._logger is not None:
            getattr(self._logger, level, self._logger.info)(message)


def primary_file(paths: List[str]) -> Optional[str]:
    """The file a caller means when it says "the capture".

    RAW wins over JPEG: `color-correction` demosaics the RAW and only falls back
    to a rendered file if there isn't one. With RAW+JPEG the body writes both
    and the order they're announced in isn't guaranteed, so this can't just be
    "the first one".
    """
    if not paths:
        return None
    for path in paths:
        if is_raw(path):
            return path
    return paths[0]


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0
