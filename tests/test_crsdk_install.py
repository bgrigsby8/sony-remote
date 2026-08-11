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


class TestMirrorAdapters:
    def test_mirrors_into_process_dirs(self, tmp_path, target):
        inner = tmp_path / "RemoteCli.zip"
        inner.write_bytes(_inner_zip_bytes())
        crsdk_install.ensure_installed(str(inner), target)
        exe_dir = tmp_path / "exe"
        cwd_dir = tmp_path / "cwd"
        exe_dir.mkdir()
        cwd_dir.mkdir()
        crsdk_install.mirror_adapters([str(exe_dir), str(cwd_dir)], target)
        for d in (exe_dir, cwd_dir):
            assert (d / "CrAdapter" / "libCr_PTP_USB.so").read_bytes() == b"usb"

    def test_noop_when_nothing_installed(self, tmp_path, target):
        # No /opt install yet -> nothing to mirror, nothing created.
        dest = tmp_path / "exe"
        dest.mkdir()
        crsdk_install.mirror_adapters([str(dest)], target)
        assert not (dest / "CrAdapter").exists()

    def test_existing_copy_is_left_alone(self, tmp_path, target):
        inner = tmp_path / "RemoteCli.zip"
        inner.write_bytes(_inner_zip_bytes())
        crsdk_install.ensure_installed(str(inner), target)
        dest = tmp_path / "exe"
        (dest / "CrAdapter").mkdir(parents=True)
        marker = dest / "CrAdapter" / "libCr_PTP_USB.so"
        marker.write_bytes(b"operator-managed")
        crsdk_install.mirror_adapters([str(dest)], target)
        assert marker.read_bytes() == b"operator-managed"


class TestUsbfsWarning:
    def test_low_cap_warns_with_the_fix(self, tmp_path):
        knob = tmp_path / "usbfs_memory_mb"
        knob.write_text("16\n")
        message = crsdk_install.usbfs_warning(str(knob))
        assert "16MB" in message
        assert "usbcore.usbfs_memory_mb=1000" in message

    def test_high_cap_is_silent(self, tmp_path):
        knob = tmp_path / "usbfs_memory_mb"
        knob.write_text("1000\n")
        assert crsdk_install.usbfs_warning(str(knob)) is None

    def test_no_knob_is_silent(self, tmp_path):
        # macOS dev machines, containers without the sysfs file, ...
        assert crsdk_install.usbfs_warning(str(tmp_path / "missing")) is None


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
