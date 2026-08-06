"""
models/commands.py
------------------
DoCommand dispatch. Two families of command live here, and they answer in two
different shapes, on purpose.

**Compatibility commands** (`capture`, `trigger`, `download`, `list_files`,
`download_all`, `delete`, `cleanup`, `summary`) reproduce the `ptp` model's
contract byte for byte, nested under the command name:

    {"capture": {}}  ->  {"capture": {"name", "path", "saved_to", "mime_type", "size", ...}}

That nesting is not a style choice - `color-correction` does
``source_resp.get("capture", source_resp)`` and then ``capture.get("saved_to")
or capture.get("path")``, and its deferred pipeline calls ``trigger`` then
``download``. Matching the shape is what lets `color-correction` wrap this
module with no changes at all (scope.md §2.5).

**New commands** (`get_focus_position`, `set_focus_position`, `autofocus_once`,
`get_settings`, `set_settings`, `get_status`, `capture_count`) answer flat, in
the shapes scope.md §5 specifies:

    {"get_focus_position": {}}  ->  {"position": 1234, "units": "sdk_raw"}

Both families accept either invocation style: the ptp-style key-presence form
above, or comxim's ``{"command": "set_focus_position", "position": 1234}``. The
webapp uses both across the machine, so supporting both here costs one
normalisation step and saves every caller from remembering which module is
which.

Several commands are inherited from `ptp` but mean something slightly different
on a direct-to-host camera; each says so at its handler.
"""

import asyncio
import os
from typing import Any, Dict, List, Mapping, Optional

from viam.utils import ValueTypes

from binding import CameraError
from session import CameraSession
from store import mime_for

#: Commands whose result is nested under the command name, for ptp parity.
_NESTED = (
    "capture",
    "trigger",
    "download",
    "download_all",
    "list_files",
    "delete",
    "cleanup",
    "summary",
)

#: Commands whose result is returned flat, per scope.md §5.
_FLAT = (
    "get_focus_position",
    "set_focus_position",
    "autofocus_once",
    "get_settings",
    "set_settings",
    "get_status",
    "capture_count",
)

ALL_COMMANDS = _NESTED + _FLAT


class CommandHandler:
    """Turns a DoCommand mapping into work on a `CameraSession`."""

    def __init__(self, session: CameraSession, logger):
        self._session = session
        self._logger = logger

    async def dispatch(
        self, command: Mapping[str, ValueTypes], timeout: Optional[float] = None
    ) -> Mapping[str, ValueTypes]:
        requested = _normalize(command)
        if not requested:
            raise ValueError(
                "no recognized command. Supported: " + ", ".join(sorted(ALL_COMMANDS))
            )

        response: Dict[str, ValueTypes] = {}
        for name, opts in requested.items():
            handler = getattr(self, f"_cmd_{name}")
            result = await handler(opts)
            if name in _NESTED:
                response[name] = result
            else:
                response.update(result)
        return response

    # ------------------------------------------------------------------
    # Compatibility commands (ptp contract)
    # ------------------------------------------------------------------

    async def _cmd_capture(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        """Trip the shutter and return once the still is on the host.

        `opts` are accepted and ignored except `timeout_s`: `ptp` takes
        ``{"af": true}`` here, but this rig's whole point is that focus is set
        from stored calibration rather than found per shot (scope.md §2.3), so
        an AF request is deliberately not honoured. Use `autofocus_once` during
        calibration instead.
        """
        timeout_s = _optional_number(opts.get("timeout_s"))
        return await _to_thread(self._session.capture, timeout_s)

    async def _cmd_trigger(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        """The fast half of `capture` - except that on this camera there is no
        fast half.

        On the Canon, `trigger` returned as soon as the exposure ended and left
        the file on the card for a later `download`, so a gantry could move
        while the file transferred. CrSDK saves direct-to-host, so the transfer
        *is* the capture and there is nothing to defer. This runs the full
        capture and returns the ptp `trigger` shape.

        Kept because `color-correction`'s deferred pipeline calls `trigger` then
        `download`; that pipeline still works (the `download` below is a no-op
        that hands back the same path), it just doesn't overlap the transfer
        with rig motion any more. The gain that bought is smaller here anyway -
        USB 3 direct-to-host is a fraction of a PTP card pull.
        """
        result = await _to_thread(self._session.capture, _optional_number(opts.get("timeout_s")))
        return {
            "path": result["path"],
            "name": result["name"],
            "mime_type": result["mime_type"],
            "saved_to": result["saved_to"],
        }

    async def _cmd_download(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        """No-op that returns the metadata for a file already on the host.

        There is nothing to transfer - `capture` wrote it here. This exists so
        callers written against `ptp` (notably `color-correction`'s deferred
        capture) don't have to branch on camera type.
        """
        path = opts.get("path")
        if not path and opts.get("latest"):
            files = await _to_thread(
                self._session.run_offline, "list_files", self._session.store.list_images
            )
            path = files[-1] if files else None
        if not path:
            raise ValueError("`download` needs a `path`, or `latest: true`")
        return await _to_thread(self._session.run_offline, "download", lambda: _describe(str(path)))

    async def _cmd_download_all(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        """Every still on the host. Also a no-op transfer; see `download`."""

        def _work() -> Dict[str, ValueTypes]:
            files = self._session.store.list_images()
            return {"saved": files, "count": len(files)}

        return await _to_thread(self._session.run_offline, "download_all", _work)

    async def _cmd_list_files(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        """Stills in `capture_dir`.

        `ptp` listed the camera's card here. There is no card listing on this
        model - the host directory is the only storage the module knows about,
        which is also what makes `new_only` meaningless (everything here has
        already been "downloaded"). `new_only` is accepted and ignored so the
        webapp's existing calls don't fail.
        """

        def _work() -> Dict[str, ValueTypes]:
            files = self._session.store.list_images()
            return {"files": files, "count": len(files)}

        return await _to_thread(self._session.run_offline, "list_files", _work)

    async def _cmd_delete(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        """Delete host files. Accepts `path` or `paths` (the webapp sends both
        shapes across its call sites)."""
        paths: List[str] = []
        if opts.get("path"):
            paths.append(str(opts["path"]))
        for path in opts.get("paths") or []:
            paths.append(str(path))
        if not paths:
            raise ValueError("`delete` needs a `path` or a `paths` list")

        def _work() -> Dict[str, ValueTypes]:
            removed = self._session.store.remove(paths)
            return {"deleted": removed, "count": len(removed)}

        return await _to_thread(self._session.run_offline, "delete", _work)

    async def _cmd_cleanup(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        """Empty `capture_dir` to reclaim disk. `dry_run` reports without
        deleting. Same shape as ptp's `cleanup`."""
        dry_run = bool(opts.get("dry_run", False))

        def _work() -> Dict[str, ValueTypes]:
            files = self._session.store.list_images()
            removed = files if dry_run else self._session.store.remove(files)
            return {
                "directory": self._session.capture_dir,
                "removed": [os.path.basename(p) for p in removed],
                "count": len(removed),
                "dry_run": dry_run,
            }

        return await _to_thread(self._session.run_offline, "cleanup", _work)

    async def _cmd_summary(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        status = await _to_thread(self._session.device_status)
        return {
            "model": status["model"],
            "port": "usb",
            "summary": (
                f"{status['model'] or 'no camera'} "
                f"serial={status['serial'] or '?'} "
                f"connected={status['connected']} "
                f"battery={status['battery_pct']} "
                f"lens={status['lens']} "
                f"captures={status['capture_count']}"
            ),
        }

    # ------------------------------------------------------------------
    # New commands (scope.md §5)
    # ------------------------------------------------------------------

    async def _cmd_get_focus_position(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        position = await _to_thread(self._session.get_focus_position)
        return {"position": position, "units": "sdk_raw"}

    async def _cmd_set_focus_position(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        if "position" not in opts:
            raise ValueError('`set_focus_position` needs a `position` integer')
        position = opts["position"]
        if isinstance(position, bool) or not isinstance(position, (int, float)):
            raise ValueError(f"`position` must be an integer, got {position!r}")
        tolerance = _optional_number(opts.get("tolerance"))
        return await _to_thread(
            self._session.set_focus_position,
            int(position),
            None if tolerance is None else int(tolerance),
        )

    async def _cmd_autofocus_once(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        return await _to_thread(self._session.autofocus_once)

    async def _cmd_get_settings(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        return await _to_thread(self._session.get_settings)

    async def _cmd_set_settings(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        values = dict(opts)
        # Both `{"set_settings": {"iso": 200}}` and
        # `{"command": "set_settings", "settings": {"iso": 200}}` are natural
        # things to write; accept the nested form too.
        if "settings" in values and isinstance(values["settings"], Mapping):
            values = dict(values["settings"])
        if not values:
            raise ValueError("`set_settings` needs at least one setting to change")
        return await _to_thread(self._session.set_settings, values)

    async def _cmd_get_status(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        return await _to_thread(self._session.device_status)

    async def _cmd_capture_count(self, opts: Mapping[str, Any]) -> Dict[str, ValueTypes]:
        """Shutter actuations counted by this module, persisted in `capture_dir`.

        Not the body's own lifetime count - CrSDK doesn't expose that. Pass
        `{"set": n}` once to seed it from the body's service-menu figure so the
        number means total shutter life.
        """
        store = self._session.store
        if "set" in opts:
            seed = opts["set"]
            if isinstance(seed, bool) or not isinstance(seed, (int, float)):
                raise ValueError(f"`set` must be an integer, got {seed!r}")
            count = await _to_thread(
                self._session.run_offline,
                "capture_count",
                lambda: store.set_capture_count(int(seed)),
            )
        else:
            count = await _to_thread(
                self._session.run_offline, "capture_count", lambda: store.capture_count
            )
        return {"capture_count": count, "source": "module"}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _normalize(command: Mapping[str, ValueTypes]) -> Dict[str, Mapping[str, Any]]:
    """Reduce either invocation style to `{command_name: options}`.

    comxim style - `{"command": "set_focus_position", "position": 1234}` - puts
    the arguments alongside the command name, so the whole mapping minus
    `command` becomes the options.
    """
    named = command.get("command")
    if isinstance(named, str):
        if named not in ALL_COMMANDS:
            raise ValueError(
                f"unknown command {named!r}. Supported: " + ", ".join(sorted(ALL_COMMANDS))
            )
        opts = {k: v for k, v in command.items() if k != "command"}
        return {named: opts}

    requested: Dict[str, Mapping[str, Any]] = {}
    for name in ALL_COMMANDS:
        if name in command:
            opts = command.get(name)
            requested[name] = opts if isinstance(opts, Mapping) else {}
    return requested


def _describe(path: str) -> Dict[str, ValueTypes]:
    """ptp's file-metadata shape for a file that is already local."""
    if not os.path.exists(path):
        raise CameraError(f"{path} is not on this host")
    name = os.path.basename(path)
    return {
        "name": name,
        "path": path,
        "saved_to": path,
        "mime_type": mime_for(name),
        "size": os.path.getsize(path),
    }


def _optional_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


async def _to_thread(fn, *args):
    """Run a blocking session call off the event loop.

    Every `CameraSession` method blocks on the owner thread, so calling one
    directly from a coroutine would stall viam-server's event loop for the
    length of a capture.
    """
    return await asyncio.to_thread(fn, *args)
