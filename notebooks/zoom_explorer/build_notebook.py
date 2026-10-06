"""Generates sony_zoom_explorer.ipynb. Edit the cells here, then re-run."""
import json

cells = []
def md(s): cells.append({"cell_type": "markdown", "metadata": {}, "source": s.strip("\n")})
def code(s): cells.append({"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": s.strip("\n")})

md(r"""
# Sony power zoom: what comes from the camera, and what the module does

There are three layers between you and the lens:

| Layer | Where | What it does with zoom |
|---|---|---|
| **Camera body** | ILCE-7RM5 + FE PZ 16-35mm | Exposes numbered *device properties*. `0x14D` ZoomDistance holds the focal length; `0x126` Zoom_Operation runs the zoom motor. |
| **Native extension** | `sony-remote/native/crsdk_ext.cpp` | Maps names like `zoom_distance` to those codes, decodes the raw bytes, and picks the wire encoding for writes. |
| **Session** | `sony-remote/src/session.py` | The logic: unit conversion, the closed loop in `set_zoom`, settling, the watchdog. |

This notebook reads the camera **two ways**: through the zoom commands (`get_zoom`, which uses all three layers), and through `dump_properties`, a raw dump of every property the camera reports that skips the zoom code entirely. Comparing the two shows what's measured and what's computed.

**Modes**
- `LIVE = True`: connects to Nines-Photographer. Needs `VIAM_API_KEY` and `VIAM_API_KEY_ID` in the environment (Viam app → machine → *Connect* → *API keys*).
- `LIVE = False`: replays `snapshot.json`, real readings captured on 2026-10-05/06. Everything runs except the hardware section.

Nothing moves the lens unless you set `MOVE_LENS = True` in section 5.
""")

code(r"""
import os, json, time, ast, re, inspect, pathlib, textwrap

LIVE = bool(os.environ.get("VIAM_API_KEY"))   # flip by hand if you like
ADDRESS = os.environ.get("VIAM_ADDRESS", "nines-photographer-main.g02wbvukyo.viam.cloud")
CAMERA = "sony"
# This notebook lives in sony-remote/notebooks/zoom_explorer/; the code it shows
# is read from the checkout it sits in, so it always matches.
REPO = pathlib.Path(os.environ.get("SONY_REMOTE_REPO", pathlib.Path.cwd().parents[1])).expanduser()
SNAPSHOT = json.loads(pathlib.Path("snapshot.json").read_text())
print(f"LIVE={LIVE}  address={ADDRESS if LIVE else '(replaying snapshot.json)'}  repo={REPO} exists={REPO.exists()}")
""")

md(r"""
## 1. Connect

`do(cmd)` sends one DoCommand to the `sony` component. That is exactly what the webapp, the Viam app's control tab, or the MCP do. In replay mode it answers `get_zoom` and `dump_properties` from the snapshot.
""")

code(r"""
robot = cam = None
if LIVE:
    from viam.robot.client import RobotClient
    from viam.components.camera import Camera
    opts = RobotClient.Options.with_api_key(
        api_key=os.environ["VIAM_API_KEY"], api_key_id=os.environ["VIAM_API_KEY_ID"])
    robot = await RobotClient.at_address(ADDRESS, opts)
    cam = Camera.from_robot(robot, CAMERA)

async def do(cmd: dict) -> dict:
    if LIVE:
        return dict(await cam.do_command(cmd))
    name = next(iter(cmd))
    if name == "get_zoom":
        return SNAPSHOT["today"]["get_zoom"]
    if name == "dump_properties":
        return {"properties": SNAPSHOT["today"]["dump_properties"]}
    raise RuntimeError(f"{name!r} needs LIVE mode (replay only has get_zoom and dump_properties)")

status = await do({"get_status": {}}) if LIVE else {"connected": "(replay)"}
status
""")

md(r"""
## 2. The raw property table: straight from the camera

`dump_properties` returns every property as `{code, value, enable, value_type}` in hex, exactly as the SDK hands it over. The names below come from Sony's header `CrDeviceProperty.h` (SDK v2.02.00). The camera itself only sends numbers.

- **enable**: what the camera allows right now. 1 = read/write, 2 = read-only, 3 = write-only, 0 = disabled.
- **value_type**: Sony's `CrDataType`. Low bits give the width; `0x1000` = signed, `0x2000` = array, `0x4000` = range.
""")

code(r"""
ZOOM_CODES = {   # CrDeviceProperty.h, SDK v2.02.00
    0x124: "Zoom_Scale", 0x125: "Zoom_Setting", 0x126: "Zoom_Operation",
    0x134: "ZoomAndFocusPosition_Save", 0x135: "ZoomAndFocusPosition_Load",
    0x14D: "ZoomDistance", 0x50C: "Remocon_Zoom_Speed_Type",
    0x71E: "Zoom_Operation_Status", 0x71F: "Zoom_Bar_Information",
    0x720: "Zoom_Type_Status", 0x724: "Zoom_Speed_Range", 0x765: "LensModelName",
}
ENABLE = {-1: "not supported", 0: "disabled", 1: "read/write", 2: "read-only", 3: "write-only"}
WIDTH = {1: "8", 2: "16", 3: "32", 4: "64", 5: "128"}

def describe_type(t: int) -> str:
    if t == 0xFFFF: return "string"
    if t == 0: return "undefined"
    s = ("Int" if t & 0x1000 else "UInt") + WIDTH.get(t & 0xF, "?")
    if t & 0x2000: s += "Array"
    if t & 0x4000: s += "Range"
    return s

def zoom_rows(props):
    rows = []
    for p in props:
        code = int(p["code"], 16)
        if code in ZOOM_CODES:
            rows.append({"code": p["code"], "name": ZOOM_CODES[code], "raw value": p["value"],
                         "decimal": int(p["value"], 16), "enable": ENABLE.get(int(p["enable"], 16), p["enable"]),
                         "type": describe_type(int(p["value_type"], 16))})
    return sorted(rows, key=lambda r: int(r["code"], 16))

def show(rows):
    cols = list(rows[0])
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows: print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))

raw = (await do({"dump_properties": {}}))["properties"]
note = "" if LIVE else " (snapshot.json keeps only the zoom codes; the live camera reports ~400)"
print(f"{len(raw)} properties{note}. The zoom-related ones:\n")
show(zoom_rows(raw))
""")

md(r"""
## 3. Decode the raw values yourself, then compare with `get_zoom`

These three lines are the whole conversion for the main fields. Everything in the left column is camera data:
""")

code(r"""
by_code = {int(p["code"], 16): int(p["value"], 16) for p in raw}

mine = {
    "focal_length_mm": by_code[0x14D] / 1000,             # ZoomDistance is in 0.001 mm
    "bar.position_pct": by_code[0x71F] & 0xFFFF,          # bits 15-0
    "bar.box":          (by_code[0x71F] >> 16) & 0xFF,    # bits 23-16
    "bar.boxes":        (by_code[0x71F] >> 24) & 0xFF,    # bits 31-24
    "zoom_type":        {1: "optical", 2: "smart", 3: "clear_image", 4: "digital"}[by_code[0x720]],
    "scale":            by_code[0x124] / 1000,
    "drive_available":  by_code[0x71E] == 1,
}
gz = await do({"get_zoom": {}})
module = {"focal_length_mm": gz["focal_length_mm"], "bar.position_pct": gz["bar"]["position_pct"],
          "bar.box": gz["bar"]["box"], "bar.boxes": gz["bar"]["boxes"], "zoom_type": gz["zoom_type"],
          "scale": gz["scale"], "drive_available": gz["drive_available"]}

print(f"{'field':18} {'decoded from raw':>18} {'get_zoom':>12}  match")
for k in mine:
    print(f"{k:18} {str(mine[k]):>18} {str(module[k]):>12}  {'yes' if mine[k] == module[k] else 'NO'}")
""")

md(r"""
**Not in the raw dump:** `min_mm`, `max_mm`, `step_mm` and `speed_range`. These are camera data too: they're the *range* (min/max/step) the camera attaches to `0x14D` and `0x724`. `dump_properties` just doesn't print ranges. `driving` is module-only: it means the module has an open-ended drive running.
""")

code(r"""
print({k: gz[k] for k in ("min_mm", "max_mm", "step_mm", "speed_range", "driving")})
""")

md(r"""
### Before any zoom code existed

This is the same dump, taken yesterday from the unmodified registry module (0.1.8), which has no zoom support at all. The camera was already reporting the focal length (`0x14D`). The only thing that changed since is `0x135`: the camera enabled preset loading once a preset was saved.
""")

code(r"""
base = SNAPSHOT["baseline_before_zoom_code"]
print(base["module"], "|", base["captured_at"], "\n")
before = {r["code"]: r for r in zoom_rows(base["dump_properties"])}
after = {r["code"]: r for r in zoom_rows(raw)}
print(f"{'code':6} {'name':26} {'0.1.8 (no zoom code)':>22} {'now':>12}")
for c in before:
    b, a = before[c], after.get(c, {})
    flag = "  <- changed" if (b["raw value"], b["enable"]) != (a.get("raw value"), a.get("enable")) else ""
    print(f"{c:6} {b['name']:26} {b['raw value'] + ' ' + b['enable']:>22} {a.get('raw value','?') + ' ' + a.get('enable','?'):>12}{flag}")
print(f"\n0x14D then: {int(before['0x14D']['raw value'],16)/1000} mm   now: {int(after['0x14D']['raw value'],16)/1000} mm")
""")

md(r"""
## 4. Two independent camera properties agree

The zoom bar (`0x71F`) and the focal length (`0x14D`) are separate values. If both report the real lens position, then bar % ≈ (mm − 16) / (35 − 16). The module never computes one from the other, so this agreement comes from the camera.
""")

code(r"""
import matplotlib.pyplot as plt

# Chart tokens (light): surface, inks, grid; series 1 = measured, series 2 = predicted.
INK, INK2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
BLUE, ORANGE = "#2a78d6", "#eb6834"
plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "axes.edgecolor": AXIS,
    "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "lines.linewidth": 2, "font.size": 10,
})

pairs = list(SNAPSHOT["bar_vs_focal_length"]["pairs"])
today = [mine["focal_length_mm"], mine["bar.position_pct"]]       # this run's reading
if today not in pairs:
    pairs.append(today)
pairs.sort()
lo, hi = gz["min_mm"], gz["max_mm"]
mm = [p[0] for p in pairs]; bar = [p[1] for p in pairs]

fig, ax = plt.subplots(figsize=(7, 4.2))
ax.plot([lo, hi], [0, 100], color=ORANGE, label="predicted: (mm − 16) / 19")
ax.scatter(mm, bar, s=64, color=BLUE, edgecolor=SURFACE, linewidth=2, zorder=3, label="camera: 0x14D vs 0x71F")
# Labels sit below-right of each point (the line rises to the right, so that
# side is empty); close neighbours alternate height so they don't collide.
for i, (x, y) in enumerate(pairs):
    dy = -16 if i % 2 == 0 else -4
    ax.annotate(f"{x:g} mm → {y}%", (x, y), xytext=(10, dy), textcoords="offset points",
                fontsize=8, color=INK2, va="center")
ax.set_xlim(lo - 1, hi + 3.5)
ax.set_ylim(-10, 108)
ax.set_xlabel("focal length from ZoomDistance 0x14D (mm)")
ax.set_ylabel("zoom bar from 0x71F (%)")
ax.set_title("Zoom bar vs focal length: two camera properties", loc="left", color=INK)
ax.legend(frameon=False, loc="upper left")
plt.show()

for x, y in pairs:
    print(f"{x:5.1f} mm  bar {y:3d}%  predicted {100*(x-lo)/(hi-lo):5.1f}%")
""")

md(r"""
## 5. Watch the camera's telemetry while the lens moves (LIVE only; moves the lens)

This cell starts an open-ended drive at a low speed, polls `get_zoom` as fast as the network allows, stops the drive, and keeps polling while the lens coasts. Then it returns the lens to where it started with `set_zoom`. It shows:
- the reading updating mid-move: the camera reports the position live, not just at the end;
- 0.5 mm steps, even though the camera advertises `step_mm = 0.1`;
- a little motion after the stop, which is why the module waits for a steady reading.

The module's watchdog stops the drive after 10 s if this cell dies part-way. You can also run `await do({"zoom_stop": {}})`.
""")

code(r"""
MOVE_LENS = False   # set True to run; the zoom moves, nothing else does
SPEED, DRIVE_S, COAST_S = 2, 1.2, 1.2

trace = []
if LIVE and MOVE_LENS:
    start_mm = (await do({"get_zoom": {}}))["focal_length_mm"]
    speed = SPEED if start_mm < 26 else -SPEED          # head toward the far end
    t0 = time.monotonic()
    await do({"zoom_drive": {"speed": speed}})
    stop_at = None
    try:
        while time.monotonic() - t0 < DRIVE_S + COAST_S:
            if stop_at is None and time.monotonic() - t0 >= DRIVE_S:
                await do({"zoom_stop": {}}); stop_at = time.monotonic() - t0
            z = await do({"get_zoom": {}})
            trace.append((time.monotonic() - t0, z["focal_length_mm"], z["bar"]["position_pct"]))
    finally:
        await do({"zoom_stop": {}})
    back = await do({"set_zoom": {"focal_length_mm": start_mm}})
    print(f"started {start_mm} mm, speed {speed}, stopped at t={stop_at:.2f}s; returned: {back}")
else:
    print("skipped (needs LIVE and MOVE_LENS = True)")
""")

code(r"""
if trace:
    t = [p[0] for p in trace]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(7, 5), sharex=True)
    for ax, ys, label in ((a1, [p[1] for p in trace], "focal length, 0x14D (mm)"),
                          (a2, [p[2] for p in trace], "zoom bar, 0x71F (%)")):
        ax.axvspan(0, stop_at, color=GRID, alpha=0.6, lw=0)
        ax.step(t, ys, where="post", color=BLUE)
        ax.plot(t, ys, "o", ms=4, color=BLUE)
        ax.set_ylabel(label)
    a1.set_title("Camera telemetry during a zoom drive (shaded: drive running)", loc="left", color=INK)
    a1.annotate("stop sent", (stop_at, a1.get_ylim()[1]), xytext=(4, -12), textcoords="offset points", fontsize=8, color=INK2)
    a2.set_xlabel("time since drive start (s)")
    plt.show()
    steps = sorted({round(b - a, 3) for a, b in zip(sorted({p[1] for p in trace}), sorted({p[1] for p in trace})[1:])})
    print("distinct gaps between readings (mm):", steps)
""")

md(r"""
## 6. The module code that turns those numbers into answers

Pulled from the checkout this notebook sits in (override with `SONY_REMOTE_REPO`), so it always matches what you're reading. First the extension's name → code table for zoom, and the one encoding override that made zoom-out work:
""")

code(r"""
cpp = (REPO / "native/crsdk_ext.cpp").read_text()
start = cpp.index("    // Power zoom.")
print(cpp[start:cpp.index("};", start) + 2])
print()
fstart = cpp.index("static const std::map<std::string, cr::CrDataType> kForcedValueTypes")
print(cpp[fstart:cpp.index("};", fstart) + 2])
""")

md(r"""
Why the override matters: the module sends a speed of −4 (zoom out). Declared as the camera-reported `Int8`, it went out as one byte, and the camera accepted it and did nothing. Declared as `UInt16Array` with the value sign-extended, the way Sony's RemoteCli sample does it, the camera zooms out:
""")

code(r"""
speed = -4
print(f"Int8 encoding (ignored by the body):        0x{speed & 0xFF:02X}")
print(f"RemoteCli encoding (UInt16Array, honoured): 0x{speed & 0xFFFFFFFFFFFFFFFF:016X}")
""")

code(r"""
def show_function(name, path=REPO / "src/session.py"):
    src = path.read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            print("\n".join(src.splitlines()[node.lineno - 1 - len(node.decorator_list):node.end_lineno]))
            print(f"# -- {path.name}:{node.lineno}\n")
            return
    print(f"{name} not found")

# get_zoom: every field is a property read plus a unit conversion or bit-unpack.
show_function("_do_get_zoom")
""")

md(r"""
`set_zoom` is where the module does real work. The camera only offers *run the motor at speed N* and *here's the current focal length*, so the module closes the loop: pick a speed from the distance left, poll `0x14D` until the target is reached or crossed, stop, wait for a steady reading, and correct at half speed after any overshoot. It gives up early (`resolution_limited`) when it's bouncing between two 0.5 mm positions on either side of the target.
""")

code(r"""
show_function("_do_set_zoom")
show_function("_settled_zoom_um")
""")

md(r"""
## 7. Try it (LIVE)

`set_zoom` returns what the loop did. Compare its `focal_length_mm` with a raw `0x14D` read straight afterwards. These cells move the lens.
""")

code(r"""
if LIVE and MOVE_LENS:
    result = await do({"set_zoom": {"focal_length_mm": 24}})
    raw_now = {int(p["code"], 16): int(p["value"], 16) for p in (await do({"dump_properties": {}}))["properties"]}
    print("set_zoom said:", result)
    print("camera 0x14D says:", raw_now[0x14D] / 1000, "mm")
else:
    print("skipped (needs LIVE and MOVE_LENS = True)")
""")

md(r"""
## Summary: camera vs module

| `get_zoom` / `set_zoom` field | Source |
|---|---|
| `focal_length_mm` | camera `0x14D` ÷ 1000 |
| `min_mm`, `max_mm`, `step_mm` | camera: the range on `0x14D` |
| `speed_range` | camera `0x724` range (the module falls back to −1…1 if a body doesn't report it) |
| `bar` | camera `0x71F`, bit-unpacked |
| `zoom_type`, `scale`, `drive_available` | camera `0x720`, `0x124`, `0x71E` |
| `driving` | module: open-ended drive running |
| `ok`, `passes`, `resolution_limited`, `target_mm` | module: the closed loop's bookkeeping |
""")

code(r"""
if robot is not None:
    await robot.close()
""")

nb = {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
      "language_info": {"name": "python"}}, "nbformat": 4, "nbformat_minor": 5}
for i, c in enumerate(nb["cells"]):
    c["id"] = f"cell-{i:02d}"
    c["source"] = c["source"].splitlines(keepends=True)
open("sony_zoom_explorer.ipynb", "w").write(json.dumps(nb, indent=1, ensure_ascii=False) + "\n")
print(f"wrote sony_zoom_explorer.ipynb ({len(cells)} cells)")
