# Model brad-grigsby:sony-remote:camera

A `rdk:component:camera` for a Sony body on wired USB, driven by the Sony Camera
Remote SDK. Live-view streaming, full-resolution RAW capture saved direct to the
host, absolute focus control, and exposure settings in machine config.

Developed against the **A7R V (ILCE-7RM5)**; other CrSDK-supported bodies should
work, but only the A7R V is verified.

## Configuration

```json
{
  "serial": "",
  "crsdk_archive": "/home/viam/CrSDK_v2.02.00_Linux64PC.zip",
  "capture_dir": "/tmp/sony-remote",
  "retention_max_files": 200,
  "live_view_max_fps": 10,
  "connect_timeout_s": 10,
  "capture_timeout_s": 15,
  "autofocus_timeout_s": 5,
  "focus_tolerance": 2,
  "binding": "native",
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

### Attributes

Every attribute is optional.

| Name | Type | Default | Description |
|---|---|---|---|
| `serial` | string | — | Which body to claim. Required if more than one Sony camera is on USB; with two cameras and no `serial`, the component refuses to connect and says so in `get_status.last_error` rather than picking one at random. Matched exactly or as a suffix. |
| `crsdk_archive` | string | — | Path to the Camera Remote SDK zip downloaded from Sony (or an extracted copy). On configure, the module installs the SDK's runtime libraries from it into `/opt/sony-crsdk` if they aren't there yet, then never touches it again — the attribute can stay in the config. Sony's licence keeps these libraries out of the module itself; this automates the one manual install step (see the README's "Machine setup"). |
| `capture_dir` | string | `/tmp/sony-remote` | Where the SDK writes stills. Created if missing. The module owns retention here. `~` is expanded. |
| `retention_max_files` | number | `200` | Delete the oldest images beyond this after each capture. `0` disables retention. Non-image files (including the module's state file) are never touched. |
| `live_view_max_fps` | number | `10` | Ceiling on how often live view is actually fetched from the camera. `get_images` calls inside the interval return the cached frame without a USB round trip. |
| `connect_timeout_s` | number | `10` | Per-attempt connection timeout. Failures retry forever with capped exponential backoff. |
| `capture_timeout_s` | number | `15` | How long `capture` waits for the still to reach `capture_dir`. A ceiling, not a delay — capture returns as soon as the file lands. |
| `autofocus_timeout_s` | number | `5` | How long `autofocus_once` waits for focus lock before giving up and releasing the half-press. |
| `focus_tolerance` | number | `2` | How far, in raw SDK units, a focus read-back may differ from the target and still count as landed. Overridable per call. |
| `binding` | string | `"native"` | `"native"` drives a real camera through the `_crsdk` extension. `"fake"` drives a simulated A7R V that writes synthetic files — for bring-up before hardware arrives. A `"fake"` component logs a warning on every configure. |
| `apply_on_connect` | object | `{"shutter_type": "mechanical"}` | Settings pushed to the body on every connect, including after a reconnect. Keys are the same as `set_settings` (below). Unknown keys are rejected at config-validation time. |
| `focus_emulation` | string | `"auto"` | `"auto"`: when the body never reports an absolute focus position (the ILCE-7RM5 over USB doesn't, with any lens — verified against Sony's own RemoteCli), rebuild it over the relative near/far drive: home into the near stop, count nudges. `"off"` disables the emulation and focus-position commands fail with `[unsupported_value]`. |
| `emulated_step_size` | number | `3` | Near/far magnitude (1–7) used for every emulated nudge. One position unit = one nudge of this size, so changing it invalidates stored focus tables. |
| `emulated_travel_nudges` | number | `150` | Homing budget and position ceiling: this many nudges must cross the lens's full travel with margin. Calibrate per lens with `focus_near_far`. |
| `emulated_nudge_interval_s` | number | `0.03` | Pause after each nudge, letting the drive settle. |
| `focus_on_connect` | number | — | Drive focus to this position on every connect, including reconnects after a camera power cycle (which can physically move a power-zoom lens). With emulated focus this homes first. For a rig whose stations share one focus plane, this single number replaces all other focus handling; a failure is recorded in `apply_errors`, never fatal. |

`apply_on_connect` values are validated all the way down to their raw SDK
encoding when the machine config is saved, so a mistyped aperture is a config
error rather than a surprise in a shot nobody looks at until it's on a product
page. A value the *camera* rejects (it can only know at connect time) is logged
and reported in `get_status.apply_errors`, but does not stop the component
working.

**`shutter_type` defaults to `"mechanical"`** with or without an
`apply_on_connect` block: the rig fires a strobe and this body's electronic
shutter reads the sensor progressively, so a flash would light only part of the
frame. Set `"shutter_type": "auto"` to opt out.

### Settings vocabulary

Used by `apply_on_connect`, `set_settings` and returned by `get_settings`.

| Key | Accepts | Examples |
|---|---|---|
| `aperture` | An f-number, with or without the `f/` | `"f/11"`, `"11"`, `11`, `1.8` |
| `shutter_speed` | A fraction, a number of seconds, or bulb | `"1/160"`, `"1/8000"`, `"2\""`, `"30s"`, `"bulb"` |
| `iso` | A number or `"auto"` | `100`, `"400"`, `"auto"` |
| `white_balance` | `auto`/`awb`, `daylight`/`sun`, `shade`, `cloudy`, `incandescent`/`tungsten`, `fluorescent`, `flash`/`strobe`, `underwater`, `color_temp`, `custom1` | `"flash"` |
| `shutter_type` | `auto`, `mechanical`/`mech`, `electronic`/`silent` | `"mechanical"` |
| `file_format` | `raw`, `raw+jpeg`, `jpeg`, `raw+heif`, `heif` | `"raw"` |

Anything the *lens or body* won't accept produces an `[unsupported_value]` error
that lists what the camera says it does accept, in this same vocabulary — the
aperture range depends on the mounted lens, so the camera is the authority, not a
table.

## DoCommand

Two invocation styles work everywhere:

```json
{"set_focus_position": {"position": 1234}}
{"command": "set_focus_position", "position": 1234}
```

Compatibility commands answer **nested under the command name** (matching the
`ptp` model, which is what `color-correction` reads). New commands answer
**flat**, in the shapes below.

### `capture`

Fire the shutter; return once the full-resolution file is on the host.

```json
{"capture": {}}
```

Options: `timeout_s` overrides `capture_timeout_s` for this shot. `ptp`'s
`{"af": true}` is accepted and ignored — this camera's focus comes from stored
calibration, not from AF at capture time. Use `autofocus_once` during
calibration.

```json
{"capture": {
  "path": "/tmp/sony-remote/DSC00042.ARW",
  "saved_to": "/tmp/sony-remote/DSC00042.ARW",
  "name": "DSC00042.ARW",
  "mime_type": "application/octet-stream",
  "size": 87421952,
  "paths": ["/tmp/sony-remote/DSC00042.ARW"],
  "capture_count": 42,
  "duration_s": 1.83,
  "focus_position": 1180,
  "settings": {"aperture": "f/11", "shutter_speed": "1/160", "iso": 100,
               "white_balance": "flash", "shutter_type": "mechanical",
               "file_format": "raw"}
}}
```

`path` and `saved_to` are the same host path — direct-to-host means there's no
separate on-camera location. In `raw+jpeg`, `paths` holds both files and `path`
is the RAW. Every capture also writes this same information to the log, which is
the audit trail when someone questions an image.

### `trigger` / `download` / `download_all` / `list_files` / `delete` / `cleanup` / `summary`

`ptp` compatibility. See the table in [README.md](README.md#compatibility-with-ptp)
for how each differs on a direct-to-host camera. Response shapes are identical to
`ptp`'s.

### `get_focus_position`

```json
{"get_focus_position": {}}   ->  {"position": 1180, "units": "sdk_raw"}
```

The raw integer the SDK reports. **No unit conversion, deliberately** — the SDK
does not document a physical unit, and inventing one would be a precision we
don't have. Calibration stores whatever integer the camera reported at a station
and plays it back.

On bodies that never report an absolute position (the ILCE-7RM5 over USB),
`units` is `"emulated_nudges"` instead: the position is a count of near/far
nudges from the lens's near stop, maintained by the module (see the
`focus_emulation` attributes). The first focus operation after a connect,
autofocus, or manual nudge **homes automatically** — it drives the lens hard
into the near stop and calls that zero, which takes
`emulated_travel_nudges × emulated_nudge_interval_s` seconds. Calibration works
exactly as before: store the integer, play it back.

### `set_focus_position`

```json
{"set_focus_position": {"position": 1180}}
{"set_focus_position": {"position": 1180, "tolerance": 5}}
```

Puts the lens in MF if it isn't already, drives focus, reads back, and retries
once if the read-back is further than `tolerance` from the target.

```json
{"position": 1181, "target": 1180, "tolerance": 2,
 "attempts": 1, "ok": true, "units": "sdk_raw"}
```

A second miss returns `"ok": false` with the position it did reach, rather than
retrying forever — whether that's good enough for a given station is the
caller's call.

### `focus_near_far`

```json
{"focus_near_far": {"step": -3}}  ->  {"step": -3}
```

One raw relative focus nudge: sign is direction (negative = near), magnitude
1–7 is the step size. The bring-up and calibration primitive under emulated
focus — count how many `step: 7` nudges cross your lens's travel to size
`emulated_travel_nudges`. A manual nudge invalidates the emulated position
count until the next focus operation re-homes.

### `home_focus`

```json
{"home_focus": {}}  ->  {"emulated": true, "position": 0, "units": "emulated_nudges"}
```

Re-zero emulated focus against the near stop. Sweep orchestration should call
this at sweep start so per-station positions stay honest; it is otherwise
called automatically by the first focus operation that needs it. On bodies
with native absolute focus it reports `{"emulated": false}` and does nothing.

### `autofocus_once`

```json
{"autofocus_once": {}}  ->  {"position": 1204, "units": "sdk_raw", "acquired": true}
```

Under emulated focus, `position` comes back `null` and the stored count is
invalidated (AF moved the lens an unknown amount); the next focus operation
re-homes.

One-shot AF (half-press equivalent), then report where focus ended up. **For
calibration only**: run it once per station with the product in place, store the
resulting `position`, and drive focus from the stored value from then on.
`"acquired": false` means AF didn't lock — the reported position is wherever the
lens stopped.

### `get_settings` / `set_settings`

```json
{"get_settings": {}}
{"set_settings": {"iso": 400, "aperture": "f/8"}}
{"command": "set_settings", "settings": {"iso": 400}}
```

Both return the full settings block after the change. A setting this body doesn't
support is omitted from `get_settings` rather than failing the whole read.

### `get_status`

```json
{"get_status": {}}
```

```json
{"connected": true, "model": "ILCE-7RM5", "serial": "12345678",
 "battery_pct": 87, "lens": "FE 35mm F1.8",
 "capture_dir": "/tmp/sony-remote", "capture_count": 42,
 "connect_attempts": 1, "last_error": null, "apply_errors": []}
```

Works whether or not a camera is attached — `connected` reflects the truth and
`last_error` says why not. `model` and `serial` are the last-known values when
disconnected. `apply_errors` lists any `apply_on_connect` values the camera
refused.

### `capture_count`

```json
{"capture_count": {}}          ->  {"capture_count": 42, "source": "module"}
{"capture_count": {"set": 150000}}
```

Shutter actuations counted by this module, persisted in
`capture_dir/.sony-remote-state.json` so it survives restarts. It is **not** the
body's own lifetime count (CrSDK doesn't expose that); use `set` once to seed it
from the body's service-menu figure if you want the number to mean total shutter
life. Tracked because mechanical shutters wear out.

## Errors

Every failure is prefixed with a category a caller can branch on:
`[disconnected]`, `[timeout]`, `[unsupported_value]`, `[busy]`, `[configuration]`,
`[sdk_error]`. See [README.md](README.md#errors).

## Streaming

`get_images` returns a single `live_view` JPEG — the body's downsized viewfinder
frame, for a UI preview. It is not the capture path: a full-resolution RAW is far
too large to hand back over gRPC, which is why `capture` returns a file path
instead. When the camera is disconnected, `get_images` raises rather than
returning the last frame it saw.

`get_point_cloud` raises; `get_geometries` returns `[]` (the arm's frame system
owns where the camera is).
