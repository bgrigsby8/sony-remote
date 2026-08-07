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

### Runtime library placement

`libCr_Core` **dlopen's its adapter libraries from the directory next to the
running executable**, not from `LD_LIBRARY_PATH`. On the machine running
viam-server, that means the CrSDK `.so` files must sit alongside the unpacked
module binary. If the module starts but every command reports

```
[configuration] the `_crsdk` extension is not importable ...
```

that is what to check first. `{"get_status": {}}` reports the same thing in
`last_error`, so it is visible from the webapp without reading logs.

Whether those libraries may ship *inside* the module tarball is
[open question 1](#open-questions). Until it's settled, `build.sh` bundles them
only when you opt in explicitly:

```bash
CRSDK_BUNDLE_LIBS=1 CRSDK_ROOT=/path/to/sdk make build
```

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
deliberately thin enough to be covered by [`SMOKE.md`](SMOKE.md), which is the
checklist to run the day the camera arrives.

## Status

`crsdk_ext.cpp` compiles and links against **CrSDK v2.02.00 (Linux x64)**. The
notable corrections from the first real compile: the still-file-format property
is `CrDeviceProperty_FileType`, and S1 half-press is a device property
(`CrDeviceProperty_S1` = `CrLockIndicator_Locked`), not a `SendCommand`.
Validation against a live body is next — [`SMOKE.md`](SMOKE.md) is the
checklist. Nothing above `binding/interface.py` changed, and the test suite is
unaffected.

## Open questions

Tracked from `scope.md` §10; none of them block bring-up.

1. **CrSDK redistribution.** May the `.so` files ship inside a Viam registry
   module for private org distribution? Read the licence text in the download.
   Until answered, `build.sh` bundles them only under `CRSDK_BUNDLE_LIBS=1`, the
   module is `"visibility": "private"` in `meta.json`, and the runtime error
   points an operator at where to put the libraries by hand.
2. **CI SDK storage.** Private GitHub release asset vs. publishing only from a
   build machine that already has the SDK. Until then CI runs the test suite
   (which needs neither) and `make publish` is local.
3. **Focus Position Setting on ILCE-7RM5.** Confirm in the SDK's per-model
   feature matrix before M2. If absent, the fallback is relative stepping via the
   near/far drive, re-homed each sweep — that change is confined to
   `crsdk_ext.cpp`, because Python only ever asks for `"focus_position"`.
4. **Live view during capture.** Answered structurally: one owner thread means
   live-view polling and capture cannot interleave, whatever the SDK turns out to
   require. Still worth confirming on hardware that frames resume promptly after
   a shot — it's on the smoke checklist.
