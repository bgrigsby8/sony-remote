import os
import sys

import pytest

# Mirror the module entrypoint, which runs from src/ so `from models...` and
# `from session import ...` resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from binding.fake import FakeCamera  # noqa: E402
from session import CameraSession, SessionConfig  # noqa: E402


class RecordingLogger:
    """Collects log lines so tests can assert on the audit trail, which is a
    deliverable here rather than incidental output."""

    def __init__(self):
        self.lines = []

    def _record(self, level):
        def log(message, *args, **kwargs):
            self.lines.append((level, str(message)))

        return log

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error", "critical", "exception"):
            return self._record(name)
        raise AttributeError(name)

    def text(self, level=None):
        return "\n".join(m for lvl, m in self.lines if level is None or lvl == level)


@pytest.fixture
def logger():
    return RecordingLogger()


@pytest.fixture
def fake():
    camera = FakeCamera()
    yield camera
    camera.cancel_timers()


@pytest.fixture
def capture_dir(tmp_path):
    return str(tmp_path / "captures")


@pytest.fixture
def make_session(logger, capture_dir):
    """Build a started `CameraSession` and guarantee it is closed.

    Every test that touches a session goes through this, so a test that fails
    mid-capture can't leave an owner thread running into the next one.
    """
    sessions = []

    def build(binding=None, **overrides):
        config = SessionConfig(
            capture_dir=capture_dir,
            connect_timeout_s=1.0,
            capture_timeout_s=overrides.pop("capture_timeout_s", 2.0),
            autofocus_timeout_s=overrides.pop("autofocus_timeout_s", 1.0),
            **overrides,
        )
        session = CameraSession(binding or FakeCamera(), config, logger)
        sessions.append(session)
        session.start()
        return session

    yield build

    for session in sessions:
        session.close(timeout=2.0)


def wait_until(predicate, timeout=3.0, interval=0.01):
    """Spin until `predicate()` is true. Returns whether it became true.

    The session connects on a background thread, so tests wait for the state
    they need rather than sleeping a guessed amount.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
