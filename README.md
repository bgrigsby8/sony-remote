# sony-remote

A Viam camera module for Sony bodies driven over wired USB by the **Sony Camera
Remote SDK** ("CrSDK"). Built for the Nines AI robotic product-photography rig,
where an arm-mounted **A7R V** with a fixed 35mm prime shoots a product on a
turntable, and every station in the sweep needs a *known* focus position rather
than an autofocus guess.

It replaces `brad-grigsby:image-processing:ptp` (libgphoto2 / Canon EOS R5 II) as
the source camera, and is a drop-in for
`brad-grigsby:image-processing:color-correction`, which wraps it without changes.

## Models

- [`brad-grigsby:sony-remote:camera`](brad-grigsby_sony-remote_camera.md) — the
  camera component. Full attribute and DoCommand reference lives there.

## What it does

| | |
|---|---|
| **Stream** | `get_images` returns a live-view JPEG for the operator UI preview, throttled to `live_view_max_fps`. |
| **Capture** | `{"capture": {}}` fires the shutter and returns once the full-resolution RAW is **on the host's disk** — the SDK saves direct-to-host, so there is no download step and nothing is left on the card. |
| **Focus** | `get_focus_position` / `set_focus_position` in the SDK's raw units, with read-back and a retry. `autofocus_once` is for calibration only. |
| **Exposure** | Aperture, shutter speed, ISO, shutter type, white balance and file format, in machine config (`apply_on_connect`) or at runtime (`set_settings`). |
| **Survives** | USB drops, camera power cycles and viam-server restarts, with no operator intervention and no camera power cycle. |

What it deliberately does *not* do: RAW development and colour correction (that's
`color-correction`), sweep orchestration and per-station focus tables (that's
`nines-webapp`), zoom (fixed prime), video, wireless, or Windows.

## Quick start

```bash
make setup          # venv + dependencies
make test           # 200+ tests, no camera and no SDK required
```

Then, to talk to a real camera, you need the SDK — see below. To wire up the
machine config, webapp and `color-correction` **before** the hardware arrives,
set `"binding": "fake"` and the module drives a simulated A7R V that writes
synthetic files into `capture_dir`. Every DoCommand answers; nothing is real.

## SDK acquisition

The Camera Remote SDK is downloaded from
<https://support.d-imaging.sony.co.jp/app/sdk/en/index.html> after registering
and accepting Sony's licence. **It is not redistributable and is never committed
to this repo.**

```bash
export CRSDK_ROOT=/path/to/CrSDK_vX.YY.ZZ_<platform>   # the extracted directory
make ext                                               # builds src/_crsdk*.so
```

`CRSDK_ROOT` must contain the headers (`CameraRemote_SDK.h`, usually under
`app/CRSDK/`) and the shared libraries (`libCr_Core.so` and friends).
`make ext` without it prints these instructions and exits.

### Machine setup (per deployed machine, one time)

Sony's licence does not permit redistributing the SDK's shared libraries, so
they are **not** inside the module tarball. Each machine provides them at the
conventional path `/opt/sony-crsdk` (the extension's rpath looks there):

```bash
# 1. Download the Camera Remote SDK from Sony (registration + licence
#    acceptance), copy the zip to the machine, then:
cd ~ && unzip CrSDK_v*_Linux64PC.zip -d sony && cd sony && unzip RemoteCli.zip -d RemoteCli

# 2. Install the runtime libraries where the module expects them:
sudo mkdir -p /opt/sony-crsdk
sudo cp -r RemoteCli/external/crsdk/. /opt/sony-crsdk/
```

That's the whole host-side install: `/opt/sony-crsdk` ends up holding
`libCr_Core.so`, `libmonitor_protocol*.so` and the `CrAdapter/` directory
(whose location next to `libCr_Core` is load-bearing - `libCr_Core` dlopens
its adapters relative to itself, not via `LD_LIBRARY_PATH`). The usbfs memory
cap (see Host prerequisites) is handled automatically by the module's
`first_run` script.

If the module starts but every command reports

```
[configuration] the `_crsdk` extension is not importable ...
```

the libraries aren't at `/opt/sony-crsdk` (or aren't readable). The same text
shows up in `{"get_status": {}}` under `last_error`, so it is visible from the
webapp without reading logs.

For development only, `build.sh` can bundle the libraries into a local build
with `CRSDK_BUNDLE_LIBS=1 CRSDK_ROOT=/path/to/sdk make build` - never publish
such an artifact; that is redistribution.

### Camera setup (one time, on the body)

- **Setup → USB → USB Connection Mode → Remote Shoot (PC Remote).** If set to
  "Select When Connect", someone has to touch the camera every time the cable
  is plugged in - set it explicitly.
- In the Remote Shoot settings: **Still Img. Save Dest. → PC only** (the
  module also enforces this per session, but matching the menu avoids
  surprises when the camera is used standalone) and **Save Image Size →
  Original** (the 2M default silently downsizes transferred JPEGs).
- **Focus**: leave the lens barrel's AF/MF switch on **AF** and set MF from
  the body's focus-mode menu when manual focus is wanted - with the barrel
  switch on MF, some lenses drop off the electronic focus bus entirely.
- **Power save**: disable auto power-off (Setup → Power Setting Option) for
  rig use - a sleeping body drops the USB session. The module reconnects
  automatically, but a capture issued mid-drop fails.
- The module takes **PC-remote priority** at connect, so the body's physical
  dials will not respond while a session is active. This is deliberate.

### Host prerequisites

Linux caps userspace USB transfer buffers at **16MB** by default
(`usbfs_memory_mb`). Live view fits under that; a 60-120MB A7R V RAW does not,
and the failure is nasty: the transfer kills the USB session ~0.5s after the
shutter, with no kernel USB event. Raise the cap:

```bash
echo 1000 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb   # now
# persist: add usbcore.usbfs_memory_mb=1000 to the kernel command line, or
#   echo 'w /sys/module/usbcore/parameters/usbfs_memory_mb - - - - 1000' \
#     | sudo tee /etc/tmpfiles.d/sony-crsdk.conf
```

The module checks at startup and logs a warning when the cap is too low.

### Platforms

`linux/amd64` and `linux/arm64` are published. macOS is **not** — Sony does not
publish a Darwin build of CrSDK, so `make ext` has nothing to link against there.
Development on a Mac still works fully: `make test` needs neither the SDK nor a
camera, and `"binding": "fake"` runs the module end to end. Windows is a
non-goal.

## Configuration

```json
{
  "serial": "",
  "capture_dir": "/tmp/sony-remote",
  "retention_max_files": 200,
  "live_view_max_fps": 10,
  "connect_timeout_s": 10,
  "capture_timeout_s": 15,
  "autofocus_timeout_s": 5,
  "focus_tolerance": 2,
  "apply_on_connect": {
    "shutter_type": "mechanical",
    "aperture": "f/11",
    "shutter_speed": "1/160",
    "iso": 100,
    "white_balance": "flash",
    "file_format": "raw"
  }
}
```

Every attribute is optional. Full reference, including every accepted value for
each setting, is in
[`brad-grigsby_sony-remote_camera.md`](brad-grigsby_sony-remote_camera.md).

Two behaviours worth knowing up front:

- **`shutter_type` defaults to `"mechanical"`** even if you supply no
  `apply_on_connect` at all. The rig fires a strobe, and this body's electronic
  shutter reads the sensor progressively — a flash would light only the band of
  rows exposed while it fired. Set `"shutter_type": "auto"` to opt out.
- **A value the camera rejects is logged, not fatal.** A body that won't stop
  down to f/45 still connects and still shows live view; `get_status` reports
  what didn't stick in `apply_errors`. A config error must not leave you with a
  component too dead to debug.

## Compatibility with `ptp`

`color-correction` calls its source camera's `capture`, `trigger` and `download`
DoCommands and reads specific keys out of the responses. This module reproduces
those shapes exactly, so it can be swapped in by changing one `camera` reference
in the machine config. Four of them mean something slightly different here:

| Command | On `ptp` (Canon, PTP) | Here (Sony, direct-to-host) |
|---|---|---|
| `capture` | Trigger, then pull the file off the card | Trigger; the SDK has already written it to `capture_dir`. Same response keys, and `path` == `saved_to`. |
| `trigger` | Returns when the exposure ends, file still on the card | There is no card step to defer, so this runs the full capture and returns the `trigger` response shape. |
| `download` | Pulls a file off the card | No-op; returns the metadata for a file already on the host. |
| `list_files` | Lists the camera's card | Lists `capture_dir`. `new_only` is accepted and ignored — everything here has already "arrived". |

`color-correction`'s deferred-capture pipeline (`trigger` → `capture_result` →
`download`) therefore still works; it just no longer overlaps a card transfer
with rig motion, because there is no card transfer. That was worth less here
anyway — direct-to-host over USB 3 is a fraction of a PTP card pull.

## Architecture

```
src/
  main.py                 module entrypoint
  models/camera.py        the Viam model: config, camera API, lifecycle
  models/commands.py      DoCommand dispatch (ptp-parity + new commands)
  session.py              THE OWNER THREAD: connection state machine, capture
                          flow, focus retry, serialization
  settings.py             "f/11" <-> 1100, "1/160" <-> 0x000100A0, ...
  store.py                capture_dir: retention, shutter counter, file settling
  binding/
    interface.py          the CameraBinding contract + typed errors
    native.py             CameraBinding over the _crsdk extension
    fake.py               CameraBinding over a simulated A7R V
native/
  crsdk_ext.cpp           the pybind11 extension - the only file that includes
                          Sony's headers
  setup.py                builds it against CRSDK_ROOT
```

Two rules hold the design together:

1. **Every call into the SDK happens on one thread.** CrSDK is not thread-safe
   and delivers notifications on its own threads; callbacks in `crsdk_ext.cpp` do
   nothing but push a POD event onto a queue, and `session.py`'s owner thread is
   the only thing that ever calls in. Command serialization, live-view yielding
   to capture, and paired SDK init/release all fall out of that one choice.

2. **No policy below `binding/interface.py`.** The extension is symbol tables and
   marshalling; retry, tolerance, timeouts and unit conversion live in Python.
   That is what makes `fake.py` a faithful stand-in, and why the entire module
   above the seam is tested without hardware.

### Errors

Every failure carries a category the caller can branch on, as a prefix on the
message (a Viam client only ever sees the string):

| Prefix | Meaning |
|---|---|
| `[disconnected]` | No camera, or it vanished mid-operation. Retrying later will work. |
| `[timeout]` | The shutter fired but no file arrived in time. |
| `[unsupported_value]` | The camera rejected a setting. The message lists what it *does* accept, in config vocabulary. |
| `[busy]` | Queued behind other work, or the body is mid-write. Retryable now. |
| `[configuration]` | A human has to change something: wrong `serial`, two cameras and no `serial`, missing SDK libraries. |
| `[sdk_error]` | Anything else CrSDK reported. |

## Testing

```bash
make test
```

209 tests, no camera, no SDK, no USB. They run the real owner thread and the real
state machine against `binding/fake.py`, which models the four things that make
this camera awkward: asynchronous multi-file captures, completion notifications
that sometimes don't name the file, files that are visible before they're
complete, and focus that doesn't land exactly where you put it.

The native binding needs hardware. What can be tested without it is: that it
implements the whole interface with matching signatures, and that it translates
the extension's error strings into the right typed exceptions. The rest is
deliberately thin enough to be covered by the hardware smoke checklist, which
has been run against a real body.

## Status

Built against **CrSDK v2.02.00** and validated on hardware (ILCE-7RM5 over
USB): connect/reconnect, settings control, live view, and direct-to-host RAW
capture all verified end to end.

Known limitations:

- **Absolute focus position requires a compatible lens.** The body supports
  `CrDeviceProperty_FocusPositionSetting`, but the lens must provide position
  telemetry or the property never appears (the FE 35mm F1.8 does not; Sony's
  own RemoteCli reports "not supported" on the same combination). With an
  unsupported lens, `get/set_focus_position` return an `[unsupported_value]`
  error; a relative-stepping fallback via the near/far drive is a possible
  future addition, confined to `crsdk_ext.cpp`.
- **`linux/amd64` artifacts only** until an ARM build host or CI pipeline is
  set up. The test suite and `"binding": "fake"` work anywhere Python does.
