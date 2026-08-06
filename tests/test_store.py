"""
`capture_dir` management: retention, the shutter counter's persistence, and the
"is this file finished?" check that stops a consumer opening a half-written RAW.
"""

import json
import os
import threading
import time

import pytest

from store import CaptureStore, is_raw, mime_for, primary_file


@pytest.fixture
def store(tmp_path, logger):
    store = CaptureStore(str(tmp_path / "captures"), max_files=3, logger=logger)
    store.ensure_dir()
    return store


def write(store, name, data=b"x" * 32, age=0.0):
    path = os.path.join(store.directory, name)
    with open(path, "wb") as handle:
        handle.write(data)
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


class TestListing:
    def test_only_images_are_listed(self, store):
        write(store, "DSC00001.ARW")
        write(store, "DSC00001.JPG")
        write(store, "notes.txt")
        names = [os.path.basename(p) for p in store.list_images()]
        assert names == ["DSC00001.ARW", "DSC00001.JPG"] or names == [
            "DSC00001.JPG",
            "DSC00001.ARW",
        ]
        assert "notes.txt" not in names

    def test_new_files_since_ignores_what_was_already_there(self, store):
        write(store, "old.ARW")
        before = store.snapshot()
        write(store, "new.ARW")
        assert [os.path.basename(p) for p in store.new_files_since(before)] == ["new.ARW"]

    def test_new_files_are_ordered_oldest_first(self, store):
        before = store.snapshot()
        write(store, "second.ARW", age=1)
        write(store, "first.ARW", age=2)
        assert [os.path.basename(p) for p in store.new_files_since(before)] == [
            "first.ARW",
            "second.ARW",
        ]

    def test_missing_directory_is_empty_not_an_error(self, tmp_path, logger):
        store = CaptureStore(str(tmp_path / "never-created"), logger=logger)
        assert store.list_images() == []
        assert store.snapshot() == set()


class TestSettling:
    def test_a_file_being_written_is_not_settled_until_it_stops_growing(self, store):
        path = os.path.join(store.directory, "growing.ARW")
        with open(path, "wb") as handle:
            handle.write(b"partial")

        def finish():
            time.sleep(0.15)
            with open(path, "wb") as handle:
                handle.write(b"x" * 4096)

        writer = threading.Thread(target=finish, daemon=True)
        writer.start()

        size = store.wait_until_settled(path, time.monotonic() + 2.0)
        writer.join()
        assert size == 4096

    def test_a_file_that_never_settles_reports_zero(self, store):
        path = os.path.join(store.directory, "forever.ARW")
        stop = threading.Event()

        def keep_growing():
            n = 1
            while not stop.is_set():
                with open(path, "wb") as handle:
                    handle.write(b"x" * n)
                n += 100
                time.sleep(0.01)

        writer = threading.Thread(target=keep_growing, daemon=True)
        writer.start()
        try:
            assert store.wait_until_settled(path, time.monotonic() + 0.3) == 0
        finally:
            stop.set()
            writer.join()

    def test_a_missing_file_reports_zero(self, store):
        assert store.wait_until_settled(
            os.path.join(store.directory, "nope.ARW"), time.monotonic() + 0.1
        ) == 0


class TestRetention:
    def test_prunes_oldest_beyond_the_limit(self, store):
        for index in range(5):
            write(store, f"DSC0000{index}.ARW", age=10 - index)
        removed = store.prune()
        assert [os.path.basename(p) for p in removed] == ["DSC00000.ARW", "DSC00001.ARW"]
        assert len(store.list_images()) == 3

    def test_under_the_limit_removes_nothing(self, store):
        write(store, "a.ARW")
        assert store.prune() == []

    def test_zero_disables_retention(self, tmp_path, logger):
        store = CaptureStore(str(tmp_path / "keep-all"), max_files=0, logger=logger)
        store.ensure_dir()
        for index in range(5):
            write(store, f"{index}.ARW")
        assert store.prune() == []
        assert len(store.list_images()) == 5

    def test_non_images_survive_retention(self, store):
        # The state file lives in this directory; pruning must never eat it.
        write(store, "notes.txt")
        for index in range(5):
            write(store, f"{index}.ARW", age=10 - index)
        store.prune()
        assert os.path.exists(os.path.join(store.directory, "notes.txt"))

    def test_remove_ignores_files_already_gone(self, store):
        path = write(store, "a.ARW")
        assert store.remove([path, path]) == [path]


class TestCaptureCounter:
    def test_starts_at_zero_and_increments(self, store):
        assert store.capture_count == 0
        assert store.increment_capture_count() == 1
        assert store.increment_capture_count() == 2
        assert store.capture_count == 2

    def test_survives_a_restart(self, store, logger):
        for _ in range(7):
            store.increment_capture_count()

        # A whole new CaptureStore over the same directory is what a
        # viam-server restart looks like.
        reopened = CaptureStore(store.directory, logger=logger)
        assert reopened.capture_count == 7
        assert reopened.increment_capture_count() == 8

    def test_can_be_seeded_from_the_bodys_own_count(self, store, logger):
        store.set_capture_count(150_000)
        assert CaptureStore(store.directory, logger=logger).capture_count == 150_000

    def test_a_corrupt_state_file_does_not_stop_the_camera(self, store, logger):
        with open(os.path.join(store.directory, ".sony-remote-state.json"), "w") as handle:
            handle.write("{not json")
        reopened = CaptureStore(store.directory, logger=logger)
        assert reopened.capture_count == 0
        assert reopened.increment_capture_count() == 1
        assert "unreadable" in logger.text("warning")

    def test_state_file_is_readable_json(self, store):
        store.increment_capture_count()
        with open(os.path.join(store.directory, ".sony-remote-state.json")) as handle:
            assert json.load(handle)["capture_count"] == 1

    def test_state_file_is_not_listed_as_an_image(self, store):
        store.increment_capture_count()
        assert store.list_images() == []


class TestHelpers:
    @pytest.mark.parametrize(
        "name,mime",
        [
            ("DSC00001.ARW", "application/octet-stream"),
            ("DSC00001.JPG", "image/jpeg"),
            ("DSC00001.heif", "image/heif"),
        ],
    )
    def test_mime_for(self, name, mime):
        # RAW is opaque bytes, deliberately not labelled as an image - nothing
        # downstream should try to render it without demosaicing first.
        assert mime_for(name) == mime

    def test_is_raw(self):
        assert is_raw("/x/DSC00001.ARW")
        assert not is_raw("/x/DSC00001.JPG")

    def test_primary_file_prefers_raw_whatever_the_order(self):
        # RAW+JPEG writes two files and doesn't promise which lands first;
        # color-correction wants the RAW either way.
        assert primary_file(["/x/a.JPG", "/x/a.ARW"]) == "/x/a.ARW"
        assert primary_file(["/x/a.ARW", "/x/a.JPG"]) == "/x/a.ARW"
        assert primary_file(["/x/a.JPG"]) == "/x/a.JPG"
        assert primary_file([]) is None
