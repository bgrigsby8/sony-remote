#!/bin/sh
# Package the module for the Viam registry.
#
# Two things beyond the generated default:
#
#   1. The `_crsdk` extension, if it has been built (`make ext`). Without it the
#      module still starts and reports an actionable error from `get_status`,
#      and `"binding": "fake"` still works - so a build machine with no SDK
#      produces a usable-for-bring-up artifact rather than failing.
#
#   2. The CrSDK shared libraries, if `CRSDK_ROOT` is set AND
#      `CRSDK_BUNDLE_LIBS=1`. That is opt-in on purpose: whether Sony's licence
#      permits redistributing the .so files inside a module tarball is scope.md
#      §10 open question 1, and defaulting to "yes" would answer it by
#      accident. With it off, the operator places the libraries on the machine
#      and the README says where.
cd `dirname $0`
set -e

VENV_NAME="venv"
PYTHON="$VENV_NAME/bin/python"

if ! $PYTHON -m pip install pyinstaller -Uqq; then
    exit 1
fi

EXTRA_ARGS=""

EXT=$(ls src/_crsdk*.so 2>/dev/null | head -n 1)
if [ -n "$EXT" ]; then
    echo "bundling $EXT"
    EXTRA_ARGS="$EXTRA_ARGS --add-binary $EXT:."
else
    echo "WARNING: src/_crsdk*.so not found - build it with 'make ext' if this"
    echo "         artifact is meant to drive a real camera."
fi

if [ "$CRSDK_BUNDLE_LIBS" = "1" ] && [ -n "$CRSDK_ROOT" ]; then
    # Core libs sit at the payload root, next to the unpacked libCr_Core.
    for lib in $(find "$CRSDK_ROOT" \( -name 'libCr_Core*.so*' -o -name 'libmonitor_protocol*.so*' \) -not -path '*/CrAdapter/*' 2>/dev/null); do
        echo "bundling $lib -> ."
        EXTRA_ARGS="$EXTRA_ARGS --add-binary $lib:."
    done
    # libCr_Core dlopens its adapters from a CrAdapter/ directory next to
    # itself - inside a --onefile build that means next to the unpacked
    # libCr_Core in _MEIPASS, so the subdirectory must be preserved. Take the
    # whole directory: the PTP adapters need libusb/libssh2 riding along.
    ADAPTER_DIR=$(find "$CRSDK_ROOT" -type d -name CrAdapter 2>/dev/null | head -n 1)
    if [ -n "$ADAPTER_DIR" ]; then
        for lib in "$ADAPTER_DIR"/*.so*; do
            echo "bundling $lib -> CrAdapter/"
            EXTRA_ARGS="$EXTRA_ARGS --add-binary $lib:CrAdapter"
        done
    fi
fi

$PYTHON -m PyInstaller --onefile --collect-all viam --hidden-import="googleapiclient" \
    $EXTRA_ARGS src/main.py

TAR_FILES="meta.json ./dist/main"
FIRST_RUN=$($PYTHON -c "import json; print(json.load(open('meta.json')).get('first_run', ''))" 2>/dev/null)
if [ -n "$FIRST_RUN" ] && [ -f "$FIRST_RUN" ]; then
    TAR_FILES="$TAR_FILES $FIRST_RUN"
fi
tar -czvf dist/archive.tar.gz $TAR_FILES
