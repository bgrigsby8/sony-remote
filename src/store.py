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

All of it is blocking filesystem work and all of it runs on the session's owner
thread.
"""

import json
import os
import time
from typing import Dict, List, Optional, Set

# Extensions the camera can write. RAW first - when a capture produces both a
# RAW and a JPEG, the RAW is the one downstream wants (color-correction
# demosaics it), so `primary_file` prefers it.
RAW_EXTS = (".arw", ".raw", ".dng")
JPEG_EXTS = (".jpg", ".jpeg")
IMAGE_EXTS = RAW_EXTS + JPEG_EXTS + (".heif", ".heic")

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


class CaptureStore:
    """Owns one `capture_dir`."""

    def __init__(self, directory: str, max_files: int = 200, logger=None):
        self.directory = directory
        self.max_files = int(max_files)
        self._logger = logger
        self._state_path = os.path.join(directory, _STATE_FILE)
        self._capture_count = 0
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

    def new_files_since(self, before: Set[str]) -> List[str]:
        """Absolute paths of image files that appeared since `before`.

        Newest last, by mtime. The fallback for bodies (or SDK versions) whose
        completion notification carries no filename - see `session._capture`.
        """
        try:
            names = [n for n in os.listdir(self.directory) if is_image(n) and n not in before]
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

    def prune(self) -> List[str]:
        """Delete the oldest images beyond `max_files`. Returns what went.

        Only image files are considered, so the state file and anything an
        operator dropped in the directory survive. `max_files <= 0` disables
        retention entirely.
        """
        if self.max_files <= 0:
            return []
        images = self.list_images()
        excess = len(images) - self.max_files
        if excess <= 0:
            return []

        removed = []
        for path in images[:excess]:
            try:
                os.remove(path)
                removed.append(path)
            except OSError as exc:
                self._log("warning", f"could not remove {path}: {exc}")
        if removed:
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
        return removed

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
        self._capture_count = max(0, int(value))
        self._loaded = True
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
        except FileNotFoundError:
            self._capture_count = 0
        except (OSError, ValueError, TypeError) as exc:
            # A corrupt state file must not stop the module from taking
            # pictures. Losing the count is bad; refusing to shoot is worse.
            self._log("warning", f"ignoring unreadable {self._state_path}: {exc}")
            self._capture_count = 0

    def _save_state(self) -> None:
        state: Dict[str, object] = {
            "capture_count": self._capture_count,
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
