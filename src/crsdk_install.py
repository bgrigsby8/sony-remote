"""
crsdk_install.py
----------------
Install the Sony Camera Remote SDK's runtime libraries from the operator's own
downloaded archive into `/opt/sony-crsdk`, where the `_crsdk` extension's
rpath finds them.

Sony's licence does not permit redistributing the SDK, so the module tarball
cannot contain the libraries - but unpacking the copy the operator downloaded
from Sony onto this same machine is just automation of the manual install.
Point the `crsdk_archive` attribute at the zip and this runs at configure
time; after that the attribute can stay (it's a no-op once installed) and the
zip can be kept for the next machine.

Accepted `crsdk_archive` shapes, because operators will hand us any of them:

* the zip as downloaded from Sony (contains `RemoteCli.zip`, which contains
  `external/crsdk/`),
* the inner `RemoteCli.zip` itself,
* a directory that has `external/crsdk/` somewhere under it (an extracted
  SDK), or a directory that already looks like `/opt/sony-crsdk`.
"""

import io
import os
import shutil
import zipfile
from typing import Callable, Optional

TARGET = "/opt/sony-crsdk"

#: The file whose presence means "already installed" - everything else
#: (adapters, monitor libs) travels with it.
_MARKER = "libCr_Core.so"

#: Path fragment that identifies the runtime-library tree inside any of the
#: accepted archive shapes.
_LIB_SUBDIR = "external/crsdk/"


def installed(target: str = TARGET) -> bool:
    return os.path.exists(os.path.join(target, _MARKER))


def ensure_installed(
    archive: str, target: str = TARGET, log: Optional[Callable[[str], None]] = None
) -> bool:
    """Install the SDK libraries from `archive` unless they're already there.

    Returns True when an install happened, False when it was already done.
    Raises OSError/ValueError with an actionable message on failure - callers
    surface it, they don't crash: the module must still configure so
    `get_status` can report what's wrong.
    """
    log = log or (lambda _msg: None)
    if installed(target):
        return False

    if os.path.isdir(archive):
        _install_from_dir(archive, target)
    elif zipfile.is_zipfile(archive):
        _install_from_zip(archive, target)
    else:
        raise ValueError(
            f"`crsdk_archive` ({archive}) is neither a zip nor a directory; "
            f"point it at the CrSDK zip downloaded from Sony"
        )

    if not installed(target):
        raise ValueError(
            f"no '{_LIB_SUBDIR}' tree with {_MARKER} found in {archive}; is this "
            f"the Camera Remote SDK download for this platform?"
        )
    log(f"installed the CrSDK runtime libraries from {archive} into {target}")
    return True


def _install_from_dir(source: str, target: str) -> None:
    # An extracted SDK (find external/crsdk below it), or a bare lib dir.
    candidates = [os.path.join(source, _LIB_SUBDIR.rstrip("/"))]
    for root, dirs, _files in os.walk(source):
        for d in dirs:
            if os.path.join(root, d).endswith(_LIB_SUBDIR.rstrip("/")):
                candidates.append(os.path.join(root, d))
    if os.path.exists(os.path.join(source, _MARKER)):
        candidates.append(source)
    for candidate in candidates:
        if os.path.exists(os.path.join(candidate, _MARKER)):
            os.makedirs(target, exist_ok=True)
            shutil.copytree(candidate, target, dirs_exist_ok=True)
            for root, _dirs, files in os.walk(target):
                for f in files:
                    os.chmod(os.path.join(root, f), 0o755)
            return
    raise ValueError(f"{source} does not contain {_LIB_SUBDIR}{_MARKER}")


def _install_from_zip(archive: str, target: str) -> None:
    with zipfile.ZipFile(archive) as z:
        # Sony's outer download nests the SDK inside RemoteCli.zip.
        inner_name = next(
            (n for n in z.namelist() if n.endswith("RemoteCli.zip")), None
        )
        if inner_name is not None and not _has_libs(z):
            with z.open(inner_name) as inner:
                with zipfile.ZipFile(io.BytesIO(inner.read())) as inner_zip:
                    _extract_libs(inner_zip, target)
            return
        _extract_libs(z, target)


def _has_libs(z: zipfile.ZipFile) -> bool:
    return any(_LIB_SUBDIR in n for n in z.namelist())


def _extract_libs(z: zipfile.ZipFile, target: str) -> None:
    members = [n for n in z.namelist() if _LIB_SUBDIR in n and not n.endswith("/")]
    if not members:
        raise ValueError(f"archive has no {_LIB_SUBDIR} entries")
    os.makedirs(target, exist_ok=True)
    for name in members:
        relative = name.split(_LIB_SUBDIR, 1)[1]  # keeps CrAdapter/ structure
        dest = os.path.join(target, relative)
        os.makedirs(os.path.dirname(dest) or target, exist_ok=True)
        with z.open(name) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
        # dlopen needs the execute bit on some setups; zips don't carry modes
        # reliably, so set them explicitly.
        os.chmod(dest, 0o755)
