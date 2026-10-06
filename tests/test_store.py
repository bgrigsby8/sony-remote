"""
`capture_dir` management: retention, the shutter counter's persistence, and the
"is this file finished?" check that stops a consumer opening a half-written RAW.
"""

import json
import os
import threading
import time

import pytest

from store import (
    IMAGE_EXTS,
    JPEG_EXTS,
    RAW_EXTS,
    CaptureStore,
    exts_for_format,
    is_camera_name,
    is_raw,
    mime_for,
    primary_file,
)


@pytest.fixture
def store(tmp_path, logger):
    store = CaptureStore(str(tmp_path / "captures"), max_files=3, logger=logger)
    store.ensure_dir()
    return store


def write(store, name, data=b"x" * 32, age=0.0):
    """Drop a file into the directory as *someone else* would - the store is
    not told about it."""
    path = os.path.join(store.directory, name)
    with open(path, "wb") as handle:
        handle.write(data)
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


def capture(store, name, data=b"x" * 32, age=0.0):
    """Write a file *and* record it as this module's capture, which is what the
    session does after every shot."""
    path = write(store, name, data, age)
    store.record_written([path])
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
        write(store, "DSC00001.ARW")
        before = store.snapshot()
        write(store, "DSC00002.ARW")
        assert [os.path.basename(p) for p in store.new_files_since(before)] == [
            "DSC00002.ARW"
        ]

    def test_new_files_are_ordered_oldest_first(self, store):
        before = store.snapshot()
        write(store, "DSC00002.ARW", age=1)
        write(store, "DSC00001.ARW", age=2)
        assert [os.path.basename(p) for p in store.new_files_since(before)] == [
            "DSC00001.ARW",
            "DSC00002.ARW",
        ]

    def test_missing_directory_is_empty_not_an_error(self, tmp_path, logger):
        store = CaptureStore(str(tmp_path / "never-created"), logger=logger)
        assert store.list_images() == []
        assert store.snapshot() == set()


class TestCandidateFilter:
    """What may be mistaken for a capture. A production machine shared
    `capture_dir` with color-correction's output directory and a `DSC00432.jpg`
    that component exported mid-shot came back as the capture; these are the
    two filters that make that impossible."""

    def test_exts_for_format_follows_the_settings_vocabulary(self):
        assert exts_for_format("raw") == RAW_EXTS
        assert exts_for_format("jpeg") == JPEG_EXTS
        assert set(exts_for_format("raw+jpeg")) == set(RAW_EXTS + JPEG_EXTS)
        assert ".heif" in exts_for_format("raw+heif")
        assert ".arw" in exts_for_format("raw+heif")
        assert ".jpg" not in exts_for_format("raw+heif")
        # The SDK's own spelling, should a body report one we don't decode.
        assert exts_for_format("RAW_JPEG") == exts_for_format("raw+jpeg")

    def test_unknown_or_missing_format_falls_back_to_every_image_type(self):
        # A body whose format property couldn't be read must still shoot.
        assert exts_for_format(None) == IMAGE_EXTS
        assert exts_for_format("something-new") == IMAGE_EXTS

    def test_a_jpg_is_never_a_raw_capture(self, store):
        assert not store.accepts("DSC00432.jpg", exts_for_format("raw"))
        assert store.accepts("DSC00432.jpg", exts_for_format("raw+jpeg"))
        assert store.accepts("DSC00432.jpg", exts_for_format("jpeg"))
        assert store.accepts("DSC00470.ARW", exts_for_format("raw"))
        assert not store.accepts("DSC00470.ARW", exts_for_format("jpeg"))

    def test_the_name_must_be_the_cameras(self, store):
        assert store.accepts("DSC00001.ARW", RAW_EXTS)
        assert store.accepts("dsc00001.arw", RAW_EXTS)
        # AdobeRGB bodies prefix an underscore; that is still the camera.
        assert store.accepts("_DSC00001.ARW", RAW_EXTS)
        assert not store.accepts("export.ARW", RAW_EXTS)
        assert not store.accepts("DSC1.ARW", RAW_EXTS)
        assert not store.accepts("DSC000011.ARW", RAW_EXTS)
        assert not store.accepts("corrected-DSC00001.ARW", RAW_EXTS)

    def test_strict_names_can_be_disabled(self, tmp_path, logger):
        lax = CaptureStore(str(tmp_path / "lax"), logger=logger, strict_names=False)
        assert lax.accepts("export.ARW", RAW_EXTS)
        # The extension filter is not optional, whatever the naming.
        assert not lax.accepts("export.jpg", RAW_EXTS)

    def test_is_camera_name_takes_a_path_or_a_basename(self):
        assert is_camera_name("/home/viam/images/DSC00470.ARW")
        assert is_camera_name("DSC00470.ARW")
        assert not is_camera_name("/home/viam/images/notes.txt")

    def test_new_files_since_skips_a_foreign_jpg_in_raw(self, store):
        before = store.snapshot()
        # color-correction's export lands first, then the body's RAW.
        write(store, "DSC00432.jpg", age=1)
        write(store, "DSC00470.ARW")
        found = store.new_files_since(before, exts_for_format("raw"))
        assert [os.path.basename(p) for p in found] == ["DSC00470.ARW"]

    def test_new_files_since_skips_a_stranger_with_the_right_extension(self, store):
        before = store.snapshot()
        write(store, "render.ARW", age=1)
        write(store, "DSC00470.ARW")
        found = store.new_files_since(before, exts_for_format("raw"))
        assert [os.path.basename(p) for p in found] == ["DSC00470.ARW"]

    def test_snapshot_is_broad(self, store):
        # `before` is an exclusion set: the more it holds, the safer the diff.
        write(store, "DSC00432.jpg")
        write(store, "anything.ARW")
        assert store.snapshot() == {"DSC00432.jpg", "anything.ARW"}


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
            capture(store, f"DSC0000{index}.ARW", age=10 - index)
        removed = store.prune()
        assert [os.path.basename(p) for p in removed] == ["DSC00000.ARW", "DSC00001.ARW"]
        assert len(store.list_images()) == 3

    def test_only_files_this_module_wrote_are_counted_or_deleted(self, store):
        # Another component's output in a shared directory: older than
        # everything of ours, image-typed, camera-named - and untouchable.
        foreign = write(store, "DSC00432.jpg", age=100)
        stranger = write(store, "DSC00433.ARW", age=99)
        for index in range(5):
            capture(store, f"DSC0000{index}.ARW", age=10 - index)

        removed = store.prune()

        assert [os.path.basename(p) for p in removed] == ["DSC00000.ARW", "DSC00001.ARW"]
        assert os.path.exists(foreign)
        assert os.path.exists(stranger)
        assert len(store.list_owned()) == 3
        assert len(store.list_images()) == 5

    def test_foreign_files_do_not_push_ours_out_early(self, store):
        # max_files=3. Two strangers plus two of ours is four images, but only
        # two are ours, so nothing is pruned.
        write(store, "DSC00432.jpg")
        write(store, "DSC00433.jpg")
        capture(store, "DSC00001.ARW")
        capture(store, "DSC00002.ARW")
        assert store.prune() == []

    def test_ownership_survives_a_restart(self, store, logger):
        for index in range(3):
            capture(store, f"DSC0000{index}.ARW", age=10 - index)
        write(store, "DSC00432.jpg", age=100)

        reopened = CaptureStore(store.directory, max_files=1, logger=logger)
        removed = reopened.prune()
        assert [os.path.basename(p) for p in removed] == ["DSC00000.ARW", "DSC00001.ARW"]
        assert os.path.exists(os.path.join(store.directory, "DSC00432.jpg"))

    def test_ownership_is_in_the_state_file(self, store):
        capture(store, "DSC00001.ARW")
        with open(os.path.join(store.directory, ".sony-remote-state.json")) as handle:
            assert json.load(handle)["written"] == ["DSC00001.ARW"]

    def test_a_state_file_without_ownership_adopts_nothing(self, store, logger):
        # An upgrade over a directory of existing captures: none of them is
        # provably ours, so none is pruned, and the operator is told.
        for index in range(5):
            write(store, f"DSC0000{index}.ARW", age=10 - index)
        with open(os.path.join(store.directory, ".sony-remote-state.json"), "w") as handle:
            json.dump({"capture_count": 5}, handle)

        reopened = CaptureStore(store.directory, max_files=3, logger=logger)
        assert reopened.prune() == []
        assert len(reopened.list_images()) == 5
        assert reopened.capture_count == 5
        assert "predate ownership tracking" in logger.text("warning")

        # From the next shot on, retention works as usual - over ours only.
        for index in range(5, 9):
            capture(reopened, f"DSC0000{index}.ARW", age=0)
        removed = reopened.prune()
        assert [os.path.basename(p) for p in removed] == ["DSC00005.ARW"]
        assert len(reopened.list_images()) == 8

    def test_files_deleted_behind_our_back_drop_out_of_the_record(self, store):
        path = capture(store, "DSC00001.ARW")
        capture(store, "DSC00002.ARW")
        os.remove(path)
        assert [os.path.basename(p) for p in store.list_owned()] == ["DSC00002.ARW"]

    def test_remove_forgets_what_it_deleted(self, store):
        path = capture(store, "DSC00001.ARW")
        store.remove([path])
        assert store.list_owned() == []
        with open(os.path.join(store.directory, ".sony-remote-state.json")) as handle:
            assert json.load(handle)["written"] == []

    def test_seeding_the_counter_keeps_ownership(self, store, logger):
        capture(store, "DSC00001.ARW")
        store.set_capture_count(150_000)
        reopened = CaptureStore(store.directory, logger=logger)
        assert reopened.capture_count == 150_000
        assert [os.path.basename(p) for p in reopened.list_owned()] == ["DSC00001.ARW"]

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
            capture(store, f"DSC0000{index}.ARW", age=10 - index)
        store.prune()
        assert os.path.exists(os.path.join(store.directory, "notes.txt"))
        assert os.path.exists(os.path.join(store.directory, ".sony-remote-state.json"))

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
