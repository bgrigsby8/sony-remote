# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the module binary. A spec (rather than CLI flags)
# because the one thing this build MUST do cannot be done from the CLI:
# filter Sony's libraries back OUT of the payload.
#
# PyInstaller auto-collects linked shared libraries - it sees that _crsdk.so
# links libCr_Core and packs it (plus its deps) into the onefile payload. Two
# problems with that:
#
#   1. Licence: Sony's SDK is not redistributable, and a payload containing
#      libCr_Core IS redistribution once the artifact is published.
#   2. Function: the bootloader's LD_LIBRARY_PATH makes the payload copy of
#      libCr_Core win over /opt/sony-crsdk - but the dlopen'd CrAdapter/ libs
#      are invisible to dependency analysis and DON'T get packed, and
#      libCr_Core only looks for adapters next to itself. Result: a payload
#      copy that can never enumerate a camera.
#
# So: strip everything Sony from the collected binaries. The loader then
# misses in the payload and falls through to the extension's rpath,
# /opt/sony-crsdk, where core and adapters sit together (README, "Machine
# setup").
#
# CRSDK_BUNDLE_LIBS=1 (+ CRSDK_ROOT) inverts this for LOCAL DEV ONLY:
# everything is bundled, adapters under CrAdapter/, and the artifact must
# never be published.

import glob
import os

from PyInstaller.utils.hooks import collect_all

_SONY_PREFIXES = ("libCr_", "libmonitor_protocol", "libssh2", "libusb-1.0")

binaries = []
datas = []
hiddenimports = ["googleapiclient"]

ext = sorted(glob.glob("src/_crsdk*.so"))
if ext:
    binaries.append((ext[0], "."))

viam_datas, viam_binaries, viam_hidden = collect_all("viam")
datas += viam_datas
binaries += viam_binaries
hiddenimports += viam_hidden

bundle = os.environ.get("CRSDK_BUNDLE_LIBS") == "1" and os.environ.get("CRSDK_ROOT")
if bundle:
    root = os.environ["CRSDK_ROOT"]
    for pattern in ("libCr_Core*.so*", "libmonitor_protocol*.so*"):
        for lib in glob.glob(os.path.join(root, "**", pattern), recursive=True):
            if "CrAdapter" not in lib:
                binaries.append((lib, "."))
    for adapter_dir in glob.glob(os.path.join(root, "**", "CrAdapter"), recursive=True):
        for lib in glob.glob(os.path.join(adapter_dir, "*.so*")):
            binaries.append((lib, "CrAdapter"))
        break

a = Analysis(
    ["src/main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

if not bundle:
    kept, dropped = [], []
    for entry in a.binaries:
        name = os.path.basename(entry[0])
        (dropped if name.startswith(_SONY_PREFIXES) else kept).append(entry)
    a.binaries = kept
    for entry in dropped:
        print(f"main.spec: excluding Sony library from the payload: {entry[0]}")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="main",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
