"""
camera.py
---------
`brad-grigsby:sony-remote:camera` - a Viam camera component for the Sony A7R V
(and other bodies supported by Sony's Camera Remote SDK) over wired USB.

It exists to replace the `ptp` model on the Nines product-photography rig with
something that can be told **exactly where to focus**. The Canon body could be
told to autofocus and hope; this one takes an absolute focus position, so the
webapp can play back a per-station value measured once during calibration and
every frame of a sweep comes out sharp with no AF at runtime and no operator
touching the lens.

Two ways to get images out, matching `ptp` so `color-correction` can wrap this
model without changes:

1. Streaming path - ``get_images`` returns a live-view frame for the operator
   UI's preview, throttled to `live_view_max_fps`.

2. DoCommand path - the studio workflow. ``{"capture": {}}`` fires the shutter
   and returns once the full-resolution RAW is on the host's disk, with the
   path to it. The SDK saves direct-to-host, so unlike the PTP model there is no
   separate download step and nothing is left on the card.

   See `commands.py` for the full command surface: focus get/set, one-shot AF
   for calibration, exposure settings, status, and the ptp-compatibility
   commands.

Everything that touches the camera runs on a single owner thread inside
`session.py`; this file is configuration, the Viam API surface, and nothing
else. That layering is what makes the whole module testable against a simulated
body - see `tests/`.
"""

import asyncio
import os
import sys
from typing import (
    Any,
    ClassVar,
    Dict,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from typing_extensions import Self
from viam.components.camera import Camera as CameraBase
from viam.media.video import CameraMimeType, NamedImage
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import Geometry, ResourceName, ResponseMetadata
from viam.resource.base import ResourceBase
from viam.resource.easy_resource import EasyResource
from viam.resource.types import Model, ModelFamily
from viam.utils import ValueTypes, struct_to_dict

import crsdk_install
import settings as settings_mod
from binding import CameraError, make_binding
from models.commands import CommandHandler
from session import CameraSession, SessionConfig

# Defaults for every optional attribute, in one place so `validate_config`,
# `reconfigure` and the README can't drift apart.
_DEFAULTS: Dict[str, Any] = {
    "capture_dir": "/tmp/sony-remote",
    "retention_max_files": 200,
    "live_view_max_fps": 10.0,
    "connect_timeout_s": 10.0,
    "capture_timeout_s": 15.0,
    "autofocus_timeout_s": 5.0,
    "focus_tolerance": 2,
    "binding": "native",
    "focus_emulation": "auto",
    "emulated_step_size": 3,
    "emulated_travel_nudges": 150,
    "emulated_nudge_interval_s": 0.03,
}

_POSITIVE_NUMBERS = (
    "live_view_max_fps",
    "connect_timeout_s",
    "capture_timeout_s",
    "autofocus_timeout_s",
)


class Camera(CameraBase, EasyResource):
    # To enable debug-level logging, either run viam-server with the --debug
    # option, or configure your resource/machine to display debug logs.
    MODEL: ClassVar[Model] = Model(ModelFamily("brad-grigsby", "sony-remote"), "camera")

    @classmethod
    def new(
        cls, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ) -> Self:
        """Create a new instance of this Camera component.

        ``EasyResource.new`` only constructs the instance - it does *not* call
        ``reconfigure``, and viam-server tears the resource down and re-adds it
        on a config change rather than calling ``reconfigure`` either. So the
        wiring has to happen here, or ``self._session`` won't exist by the time
        the first request arrives.
        """
        instance = cls(config.name)
        instance.reconfigure(config, dependencies)
        return instance

    @classmethod
    def validate_config(
        cls, config: ComponentConfig
    ) -> Tuple[Sequence[str], Sequence[str]]:
        """Range-check every attribute. This model owns its USB camera, so it
        has no dependencies on other resources.

        `apply_on_connect` is validated all the way down to the raw SDK
        encoding, which means a mistyped aperture is caught when the machine
        config is saved rather than hours later, in a shot nobody looks at until
        it's on a product page.
        """
        attrs = struct_to_dict(config.attributes)

        serial = attrs.get("serial")
        if serial is not None and not isinstance(serial, str):
            raise ValueError("`serial` must be a string")

        capture_dir = attrs.get("capture_dir")
        if capture_dir is not None and (
            not isinstance(capture_dir, str) or not capture_dir.strip()
        ):
            raise ValueError("`capture_dir` must be a non-empty path")

        retention = attrs.get("retention_max_files")
        if retention is not None and (not _is_number(retention) or retention < 0):
            raise ValueError(
                "`retention_max_files` must be a non-negative number (0 disables retention)"
            )

        for key in _POSITIVE_NUMBERS:
            value = attrs.get(key)
            if value is not None and (not _is_number(value) or value <= 0):
                raise ValueError(f"`{key}` must be a positive number")

        tolerance = attrs.get("focus_tolerance")
        if tolerance is not None and (not _is_number(tolerance) or tolerance < 0):
            raise ValueError("`focus_tolerance` must be a non-negative number")

        binding = attrs.get("binding")
        if binding is not None and binding not in ("native", "fake"):
            raise ValueError('`binding` must be "native" or "fake"')

        crsdk_archive = attrs.get("crsdk_archive")
        if crsdk_archive is not None and (
            not isinstance(crsdk_archive, str) or not crsdk_archive.strip()
        ):
            raise ValueError(
                "`crsdk_archive` must be the path to the Camera Remote SDK "
                "zip downloaded from Sony (or an extracted copy)"
            )

        focus_emulation = attrs.get("focus_emulation")
        if focus_emulation is not None and focus_emulation not in ("auto", "off"):
            raise ValueError('`focus_emulation` must be "auto" or "off"')
        step_size = attrs.get("emulated_step_size")
        if step_size is not None and (
            not _is_number(step_size) or not 1 <= step_size <= 7
        ):
            raise ValueError("`emulated_step_size` must be 1..7")
        travel = attrs.get("emulated_travel_nudges")
        if travel is not None and (not _is_number(travel) or travel < 1):
            raise ValueError("`emulated_travel_nudges` must be a positive number")
        interval = attrs.get("emulated_nudge_interval_s")
        if interval is not None and (not _is_number(interval) or interval < 0):
            raise ValueError("`emulated_nudge_interval_s` must be >= 0")
        focus_on_connect = attrs.get("focus_on_connect")
        if focus_on_connect is not None and (
            not _is_number(focus_on_connect) or focus_on_connect < 0
        ):
            raise ValueError("`focus_on_connect` must be a non-negative number")

        apply_on_connect = attrs.get("apply_on_connect")
        if apply_on_connect is not None:
            if not isinstance(apply_on_connect, dict):
                raise ValueError("`apply_on_connect` must be an object")
            try:
                settings_mod.validate_all(apply_on_connect)
            except CameraError as exc:
                # Re-raised as ValueError so viam-server reports it as a config
                # error against this component rather than as a runtime fault.
                valid = exc.details.get("valid")
                suffix = f" (valid: {', '.join(str(v) for v in valid)})" if valid else ""
                raise ValueError(f"`apply_on_connect` {exc.message}{suffix}") from exc

        return [], []

    def reconfigure(
        self, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ):
        """Read attributes and (re)start the camera session.

        Any previous session is closed first, so a reconfigure releases the USB
        claim before the new one tries to take it - otherwise the SDK would
        refuse the second connect and the component would come back dead.
        """
        attrs = struct_to_dict(config.attributes)

        def attr(key: str) -> Any:
            value = attrs.get(key)
            return _DEFAULTS.get(key) if value is None else value

        serial = attrs.get("serial")
        self._binding_kind = str(attr("binding"))
        self._session_config = SessionConfig(
            capture_dir=os.path.expanduser(str(attr("capture_dir"))),
            serial=(str(serial).strip() or None) if serial else None,
            retention_max_files=int(attr("retention_max_files")),
            live_view_max_fps=float(attr("live_view_max_fps")),
            connect_timeout_s=float(attr("connect_timeout_s")),
            capture_timeout_s=float(attr("capture_timeout_s")),
            autofocus_timeout_s=float(attr("autofocus_timeout_s")),
            focus_tolerance=int(attr("focus_tolerance")),
            apply_on_connect=dict(attrs.get("apply_on_connect") or {}),
            focus_emulation=str(attr("focus_emulation")),
            emulated_step_size=int(attr("emulated_step_size")),
            emulated_travel_nudges=int(attr("emulated_travel_nudges")),
            emulated_nudge_interval_s=float(attr("emulated_nudge_interval_s")),
            focus_on_connect=(
                int(attrs["focus_on_connect"])
                if attrs.get("focus_on_connect") is not None
                else None
            ),
        )

        # Host checks only a real camera cares about; a fake-binding component
        # must not nag about a machine it never touches.
        if self._binding_kind == "native":
            usbfs = crsdk_install.usbfs_warning()
            if usbfs:
                self.logger.warning(usbfs)

        # Operator-provided SDK archive: install Sony's runtime libraries into
        # /opt/sony-crsdk before the session first tries to import _crsdk.
        # Failure is logged, not fatal - the session still starts, and
        # get_status carries the actionable import error.
        crsdk_archive = attrs.get("crsdk_archive")
        if self._binding_kind == "native" and crsdk_archive:
            try:
                crsdk_install.ensure_installed(
                    os.path.expanduser(str(crsdk_archive)), log=self.logger.info
                )
            except Exception as exc:  # noqa: BLE001 - surfaced, not fatal
                self.logger.error(
                    f"could not install the CrSDK libraries from "
                    f"{crsdk_archive}: {exc}"
                )
        if self._binding_kind == "native" and getattr(sys, "frozen", False):
            # libCr_Core finds its adapters relative to the process, so they
            # must exist next to the packaged binary and in the working
            # directory - wherever viam-server unpacked us this time.
            crsdk_install.mirror_adapters(
                [os.path.dirname(sys.executable), os.getcwd()],
                log=self.logger.info,
            )

        old = getattr(self, "_session", None)
        if old is not None:
            old.close()

        self._session = CameraSession(
            make_binding(self._binding_kind), self._session_config, self.logger
        )
        self._commands = CommandHandler(self._session, self.logger)
        # Starting the session never blocks on the camera: the owner thread
        # connects in the background and retries forever, so a component whose
        # camera is unplugged (or whose operator hasn't switched it on yet)
        # configures cleanly and starts working when the hardware shows up.
        self._session.start()

        if self._binding_kind == "fake":
            self.logger.warning(
                'configured with `binding: "fake"` - this component is driving a '
                "simulated camera and will write synthetic files to "
                f"{self._session_config.capture_dir}. No real images will be taken."
            )

    async def close(self):
        """Release the camera and the SDK when the resource goes away.

        Strictly paired with the `init()` in the session's owner thread; getting
        this wrong is what forces an operator to power-cycle the body after a
        viam-server restart.
        """
        session = getattr(self, "_session", None)
        if session is not None:
            await asyncio.to_thread(session.close)

    # ------------------------------------------------------------------
    # Camera API
    # ------------------------------------------------------------------

    async def get_images(
        self,
        *,
        filter_source_names: Optional[Sequence[str]] = None,
        extra: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> Tuple[Sequence[NamedImage], ResponseMetadata]:
        """One live-view frame, for the operator UI's preview.

        This is *not* the capture path - live view is a downsized JPEG the body
        generates for its own viewfinder. Full-resolution stills come from the
        `capture` DoCommand, because a RAW is far too large to hand back over
        gRPC.

        Raises rather than returning the last frame when the camera is gone: a
        preview that keeps showing a stale image while the cable is unplugged is
        how an operator ends up shooting a whole SKU into the void.
        """
        frame = await asyncio.to_thread(self._session.live_view)
        return [NamedImage("live_view", frame, CameraMimeType.JPEG)], ResponseMetadata()

    async def get_properties(
        self, *, timeout: Optional[float] = None, **kwargs
    ) -> CameraBase.Properties:
        return CameraBase.Properties(
            supports_pcd=False,
            intrinsic_parameters=None,
            distortion_parameters=None,
            mime_types=[CameraMimeType.JPEG],
        )

    async def get_point_cloud(
        self,
        *,
        extra: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> Tuple[bytes, str]:
        raise NotImplementedError("a still camera does not produce point clouds")

    async def get_geometries(
        self, *, extra: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None
    ) -> Sequence[Geometry]:
        # The module knows nothing about where the body sits on the arm; an
        # empty list is the honest answer. The arm's frame system owns that.
        return []

    async def do_command(
        self,
        command: Mapping[str, ValueTypes],
        *,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> Mapping[str, ValueTypes]:
        return await self._commands.dispatch(command, timeout)


def _is_number(value: Any) -> bool:
    """True for real numbers. Excludes bool, which is an int in Python and
    would otherwise sail through a range check as 0 or 1."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)
