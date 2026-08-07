"""
crsdk_install turns the operator's Sony download into /opt/sony-crsdk without
a human copying files. These tests build fake archives in every shape an
operator plausibly points `crsdk_archive` at.
"""

import io
import os
import zipfile

import pytest

import crsdk_install

_LIBS = {
    "external/crsdk/libCr_Core.so": b"core",
    "external/crsdk/libmonitor_protocol.so": b"monitor",
    "external/crsdk/CrAdapter/libCr_PTP_USB.so": b"usb",
    "external/crsdk/CrAdapter/libusb-1.0.so": b"libusb",
}


def _inner_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("CMakeLists.txt", "project(RemoteCli)")
        for name, content in _LIBS.items():
            z.writestr(name, content)
    return buf.getvalue()


@pytest.fixture
def target(tmp_path):
    return str(tmp_path / "opt-sony-crsdk")


def _assert_installed(target):
    assert crsdk_install.installed(target)
    assert (
        open(os.path.join(target, "CrAdapter", "libCr_PTP_USB.so"), "rb").read()
        == b"usb"
    )
    mode = os.stat(os.path.join(target, "libCr_Core.so")).st_mode
    assert mode & 0o111, "dlopen needs the execute bit"


class TestArchiveShapes:
    def test_sony_outer_zip(self, tmp_path, target):
        # The shape you actually download: RemoteCli.zip nested inside.
        outer = tmp_path / "CrSDK_v9.99.99_Linux64PC.zip"
        with zipfile.ZipFile(outer, "w") as z:
            z.writestr("Camera_Remote_SDK_Readme.pdf", "pdf")
            z.writestr("RemoteCli.zip", _inner_zip_bytes())
        assert crsdk_install.ensure_installed(str(outer), target) is True
        _assert_installed(target)

    def test_inner_zip_directly(self, tmp_path, target):
        inner = tmp_path / "RemoteCli.zip"
        inner.write_bytes(_inner_zip_bytes())
        assert crsdk_install.ensure_installed(str(inner), target) is True
        _assert_installed(target)

    def test_extracted_directory(self, tmp_path, target):
        source = tmp_path / "RemoteCli"
        for name, content in _LIBS.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        assert crsdk_install.ensure_installed(str(source), target) is True
        _assert_installed(target)


class TestFailureModes:
    def test_already_installed_is_a_noop(self, tmp_path, target):
        inner = tmp_path / "RemoteCli.zip"
        inner.write_bytes(_inner_zip_bytes())
        assert crsdk_install.ensure_installed(str(inner), target) is True
        # A second call must not touch the files (the archive could even be
        # gone by now - the attribute stays in the config forever).
        assert crsdk_install.ensure_installed("/nonexistent.zip", target) is False

    def test_wrong_zip_is_an_actionable_error(self, tmp_path, target):
        wrong = tmp_path / "holiday-photos.zip"
        with zipfile.ZipFile(wrong, "w") as z:
            z.writestr("beach.jpg", "not an sdk")
        with pytest.raises(ValueError, match="external/crsdk"):
            crsdk_install.ensure_installed(str(wrong), target)
        assert not crsdk_install.installed(target)

    def test_not_an_archive_at_all(self, tmp_path, target):
        stray = tmp_path / "notes.txt"
        stray.write_text("todo: download sdk")
        with pytest.raises(ValueError, match="crsdk_archive"):
            crsdk_install.ensure_installed(str(stray), target)
