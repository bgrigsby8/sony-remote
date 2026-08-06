"""
Build the `_crsdk` extension against a downloaded Camera Remote SDK.

    export CRSDK_ROOT=/path/to/CrSDK_v1.xx.xx_LinuxARM64
    python native/setup.py build_ext --inplace   # or: make ext

`CRSDK_ROOT` must be the *extracted* SDK directory - the one containing
`app/CRSDK/` (headers) and `external/` / `lib*` (shared objects). The SDK is not
redistributable, so it is never vendored here; see README.md, "SDK acquisition".

The built `_crsdk.so` lands in `src/` so it imports alongside the module's own
packages, and so PyInstaller picks it up from the same place at package time.
"""

import os
import sys
from pathlib import Path

from setuptools import Extension, setup

try:
    import pybind11
except ImportError:  # pragma: no cover - build-time only
    sys.exit(
        "pybind11 is required to build the _crsdk extension:\n"
        "    ./venv/bin/pip install pybind11"
    )

REPO = Path(__file__).resolve().parent.parent
CRSDK_ROOT = os.environ.get("CRSDK_ROOT")

if not CRSDK_ROOT:  # pragma: no cover - build-time only
    sys.exit(
        "CRSDK_ROOT is not set.\n\n"
        "The Sony Camera Remote SDK is downloaded from\n"
        "  https://support.d-imaging.sony.co.jp/app/sdk/en/index.html\n"
        "after registering and accepting Sony's licence. It is NOT redistributable\n"
        "and is deliberately not vendored in this repo.\n\n"
        "Extract it, then:\n"
        "    export CRSDK_ROOT=/path/to/CrSDK_vX.YY.ZZ_<platform>\n"
        "    make ext\n"
    )

root = Path(CRSDK_ROOT)

# Sony has shuffled the layout between releases; look in the places it has
# lived rather than hardcoding one and failing with a confusing "no such file".
_INCLUDE_CANDIDATES = [root / "app" / "CRSDK", root / "CRSDK", root / "include" / "CRSDK", root]
_LIB_CANDIDATES = [root / "external" / "crsdk", root / "lib", root / "build" / "lib", root]

include_dirs = [str(p) for p in _INCLUDE_CANDIDATES if (p / "CameraRemote_SDK.h").exists()]
library_dirs = [
    str(p)
    for p in _LIB_CANDIDATES
    if any(p.glob("libCr_Core.*")) or any(p.glob("Cr_Core.*"))
]

if not include_dirs:  # pragma: no cover - build-time only
    sys.exit(
        f"CameraRemote_SDK.h not found under {root}. Looked in:\n  "
        + "\n  ".join(str(p) for p in _INCLUDE_CANDIDATES)
    )
if not library_dirs:  # pragma: no cover - build-time only
    sys.exit(
        f"libCr_Core not found under {root}. Looked in:\n  "
        + "\n  ".join(str(p) for p in _LIB_CANDIDATES)
    )

# The CrSDK adapters (libmonitor_protocol / libmonitor_protocol_pf, and the USB
# adapter .so) are dlopen'd by Cr_Core at runtime from the directory *next to
# the executable*, not from LD_LIBRARY_PATH. rpath covers the link-time libs;
# README documents the runtime placement, which no build flag can fix.
extra_link_args = [f"-Wl,-rpath,{library_dirs[0]}"]
if sys.platform == "darwin":
    extra_link_args += ["-Wl,-rpath,@loader_path"]
else:
    extra_link_args += ["-Wl,-rpath,$ORIGIN"]

setup(
    name="crsdk-binding",
    version="0.1.0",
    ext_modules=[
        Extension(
            "_crsdk",
            sources=[str(REPO / "native" / "crsdk_ext.cpp")],
            include_dirs=[pybind11.get_include(), *include_dirs],
            library_dirs=library_dirs,
            libraries=["Cr_Core"],
            extra_compile_args=["-std=c++17", "-fvisibility=hidden", "-O2"],
            extra_link_args=extra_link_args,
        )
    ],
    options={"build_ext": {"build_lib": str(REPO / "src")}},
)
