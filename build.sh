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
#   2. Sony's libraries are kept OUT of the payload (main.spec strips them -
#      PyInstaller would otherwise auto-collect libCr_Core because _crsdk.so
#      links it, and that is redistribution the moment the artifact is
#      published, as well as broken at runtime: the payload copy shadows
#      /opt/sony-crsdk but has no CrAdapter/ next to it). Deployed machines
#      install the libraries at /opt/sony-crsdk (README, "Machine setup").
#      CRSDK_BUNDLE_LIBS=1 + CRSDK_ROOT inverts that for local dev only.
cd `dirname $0`
set -e

VENV_NAME="venv"
PYTHON="$VENV_NAME/bin/python"

if ! $PYTHON -m pip install pyinstaller -Uqq; then
    exit 1
fi

if ! ls src/_crsdk*.so >/dev/null 2>&1; then
    echo "WARNING: src/_crsdk*.so not found - build it with 'make ext' if this"
    echo "         artifact is meant to drive a real camera."
fi

$PYTHON -m PyInstaller --clean -y main.spec

# Belt and braces for the licence rule: a publishable artifact must not
# contain Sony's libraries. (Bundled local-dev builds legitimately do.)
if [ "$CRSDK_BUNDLE_LIBS" != "1" ]; then
    if strings dist/main | grep -q "libCr_Core.so"; then
        # The extension's NEEDED entry names libCr_Core; that string alone is
        # fine. What must not exist is the library's payload TOC entry, which
        # pyinstaller lists via its archive viewer.
        if $PYTHON -m PyInstaller.utils.cliutils.archive_viewer -l dist/main 2>/dev/null \
            | grep -qE 'libCr_Core|libmonitor_protocol'; then
            echo "ERROR: dist/main contains Sony libraries; refusing to package." >&2
            exit 1
        fi
    fi
fi

TAR_FILES="meta.json ./dist/main"
FIRST_RUN=$($PYTHON -c "import json; print(json.load(open('meta.json')).get('first_run', ''))" 2>/dev/null)
if [ -n "$FIRST_RUN" ] && [ -f "$FIRST_RUN" ]; then
    TAR_FILES="$TAR_FILES $FIRST_RUN"
fi
tar -czvf dist/archive.tar.gz $TAR_FILES
