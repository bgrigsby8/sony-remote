"""`focus_method: movie` - absolute focus from the movie-mode follow-focus
read-back, with the near/far emulation as the fallback.

The ILCE-7RM5 publishes the lens's real position only in movie mode
(verified on the rig), so every operation switches to movie, drives near/far
in a closed loop against that read-back, and switches back to stills. These
tests run that against the fake's physical model: positions in follow-focus
units, 0 = near stop .. 65535 = far stop.
"""

import pytest

from binding import ConfigurationError, SDKError
from conftest import wait_until

STILLS_M, MOVIE_M = 0x1, 0x8053

# The fake's lens: 500 physical units of travel, 2 per unit of nudge size, so
# one size-3 nudge is 6 physical units = 786 follow-focus units.
UNITS_PER_NUDGE = 786
FOLLOW_PER_PHYSICAL = 0xFFFF / 500
LINEAR_NUDGE_UNITS = tuple(2 * size * FOLLOW_PER_PHYSICAL for size in range(1, 8))


def follow_units(fake):
    """Where the fake's lens physically is, in the module's units."""
    return 0xFFFF - fake.follow_focus_now()


@pytest.fixture
def movie_session(make_session, fake):
    def build(**overrides):
        config = dict(
            focus_method="movie",
            emulated_nudge_interval_s=0,
            emulated_travel_nudges=100,
            emulated_step_size=3,
            movie_units_per_nudge=UNITS_PER_NUDGE,
            movie_mode_timeout_s=0.5,
            # The linear fake lens, read back instantly.
            movie_nudge_units=LINEAR_NUDGE_UNITS,
            movie_max_nudge_size=7,
            movie_read_timeout_s=0.05,
            movie_read_stable_s=0.0,
        )
        config.update(overrides)
        session = make_session(fake, **config)
        assert wait_until(lambda: session.connected)
        return session

    return build


class TestClosedLoop:
    def test_set_lands_within_tolerance_and_returns_to_stills(self, movie_session, fake):
        session = movie_session()
        result = session.set_focus_position(30000)

        assert result["ok"] is True
        assert result["units"] == "follow_focus"
        assert result["method"] == "movie"
        assert abs(result["position"] - 30000) <= 400
        # The reported position is the lens's, not a count.
        assert abs(follow_units(fake) - result["position"]) <= 1
        assert fake.property_value("exposure_program_mode") == STILLS_M

    def test_moves_both_ways_without_ever_homing(self, movie_session, fake, logger):
        session = movie_session()
        session.set_focus_position(50000)
        session.set_focus_position(10000)
        assert abs(follow_units(fake) - 10000) <= 400
        assert "homing" not in logger.text()

    def test_a_tighter_tolerance_still_lands(self, movie_session, fake):
        session = movie_session()
        # One size-1 nudge is 262 units here; 200 needs the loop to step down.
        result = session.set_focus_position(42000, tolerance=200)
        assert result["ok"] is True
        assert abs(follow_units(fake) - 42000) <= 200

    def test_near_the_target_it_nudges_once_and_stops(self, movie_session, fake, monkeypatch):
        session = movie_session()
        steps = []
        nudge = session._nudge_focus
        monkeypatch.setattr(session, "_nudge_focus", lambda step: (steps.append(step), nudge(step)))
        # A 50-unit window is far tighter than one size-1 nudge (262 here):
        # the old loop hunted back and forth across it until the reversal cap.
        for target in (42000, 41800, 30100, 52345, 10050):
            steps.clear()
            result = session.set_focus_position(target, tolerance=50)

            assert result["attempts"] == len(steps)
            # At most one size-1 nudge, and nothing after it.
            singles = [i for i, s in enumerate(steps) if abs(s) == 1]
            assert singles in ([], [len(steps) - 1]), (target, steps)
            # Within half a size-1 step: closer than one more nudge could get.
            assert abs(result["position"] - target) <= 262 / 2 + 1, (target, result)

    def test_a_long_final_nudge_is_corrected(self, movie_session, fake, monkeypatch):
        session = movie_session()
        session.set_focus_position(42000, tolerance=50)
        # From there 41800 is a single size-1 nudge away.
        steps = []
        nudge = session._nudge_focus

        def uneven(step):
            # Size-1 moves vary on the real lens: the first one here travels
            # three times as far, overshooting by more than half a nudge.
            long = abs(step) == 1 and not any(abs(s) == 1 for s in steps)
            steps.append(step)
            for _ in range(3 if long else 1):
                nudge(step)

        monkeypatch.setattr(session, "_nudge_focus", uneven)
        result = session.set_focus_position(41800, tolerance=50)

        singles = [s for s in steps if abs(s) == 1]
        # The long one, then at least one back the other way.
        assert len(singles) >= 2 and singles[1] == -singles[0], steps
        assert abs(result["position"] - 41800) <= 262 / 2 + 1, (steps, result)

    def test_a_landing_reports_why_it_stopped(self, movie_session, fake):
        session = movie_session()
        assert session.set_focus_position(30000)["stop_reason"] == "within_tolerance"
        result = session.set_focus_position(41800, tolerance=50)
        assert result["stop_reason"] == "within_half_nudge"

    def test_a_lens_that_stops_moving_reports_the_stall(
        self, movie_session, fake, monkeypatch, logger
    ):
        session = movie_session()
        session.set_focus_position(26000)
        # A lens resting against a stop the read-back doesn't call 0 or 65535
        # (or a hand on the ring): nudges are accepted and nothing moves.
        monkeypatch.setattr(session, "_nudge_focus", lambda step: None)
        result = session.set_focus_position(35000)

        assert result["ok"] is False
        assert result["stop_reason"] == "stalled"
        warning = logger.text("warning")
        assert "stopped: stalled" in warning
        # Every nudge and its read-back, so a miss explains itself.
        assert f"+7->{result['position']}" in warning

    def test_get_reads_the_real_position_after_an_outside_move(self, movie_session, fake):
        session = movie_session()
        session.set_focus_position(20000)
        # A hand on the ring (or anything else) moves the lens: the emulation
        # would still believe its count; the read-back can't be fooled.
        fake._properties["focus_position"]["value"] = 400
        assert abs(session.get_focus_position() - follow_units(fake)) <= 1
        assert fake.property_value("exposure_program_mode") == STILLS_M

    def test_restores_the_stills_mode_it_found(self, movie_session, fake):
        session = movie_session()
        fake._properties["exposure_program_mode"]["value"] = 0x3  # aperture priority
        session.set_focus_position(25000)
        assert fake.property_value("exposure_program_mode") == 0x3
        assert ("exposure_program_mode", 0x8051) in fake.property_writes  # Movie A

    def test_a_target_past_the_stop_ends_at_the_stop(self, movie_session, fake):
        session = movie_session()
        result = session.set_focus_position(70000)
        assert result["target"] == 0xFFFF
        assert follow_units(fake) == 0xFFFF
        assert result["ok"] is True

    def test_home_reads_instead_of_driving_into_the_stop(self, movie_session, fake):
        session = movie_session()
        before = follow_units(fake)
        result = session.home_focus()
        assert result["method"] == "movie"
        assert result["emulated"] is False
        assert abs(result["position"] - before) <= 1
        assert follow_units(fake) == before  # the lens didn't move

    def test_status_and_capture_report_the_read_back(self, movie_session, fake):
        session = movie_session()
        result = session.set_focus_position(33000)
        status = session.device_status()
        assert status["focus_method"] == "movie"
        assert status["focus_emulated"] is False
        assert status["focus_fallback_reason"] is None
        shot = session.capture()
        assert shot["focus_position"] == result["position"]

    def test_an_outside_nudge_forgets_the_last_read_back(self, movie_session, fake):
        session = movie_session()
        session.set_focus_position(33000)
        session.focus_near_far(-3)
        assert session.capture()["focus_position"] is None


# The rig's lens as measured (ILCE-7RM5 + FE PZ 16-35, settled read-backs):
# follow-focus units per nudge of size 1..7. Size 1 doesn't move it.
MEASURED_UNITS = (0, 382, 693, 2329, 6470, 18893, 40000)
# The same lens in the fake's physical units (500 of travel).
MEASURED_PHYSICAL = {s: round(u / FOLLOW_PER_PHYSICAL) for s, u in enumerate(MEASURED_UNITS, 1)}
# How late the fake publishes a move. The real body is slower (over a second);
# what matters is that a read straight after a nudge is stale.
LAG_S = 0.15


@pytest.fixture
def real_lens(movie_session, fake):
    """A session against a fake that behaves like the rig: exponential nudge
    sizes, and a follow-focus read-back that lags the lens."""

    def build(**overrides):
        fake.near_far_units_by_size = dict(MEASURED_PHYSICAL)
        fake.follow_focus_latency_s = LAG_S
        config = dict(
            movie_nudge_units=tuple(
                MEASURED_PHYSICAL[s] * FOLLOW_PER_PHYSICAL for s in range(1, 8)
            ),
            movie_max_nudge_size=6,
            movie_read_timeout_s=4 * LAG_S,
            movie_read_stable_s=LAG_S,
        )
        config.update(overrides)
        return movie_session(**config)

    return build


class TestRealLens:
    def test_lands_where_it_says_despite_a_lagging_read_back(
        self, real_lens, fake, monkeypatch
    ):
        session = real_lens()
        steps = []
        nudge = session._nudge_focus
        monkeypatch.setattr(session, "_nudge_focus", lambda s: (steps.append(s), nudge(s)))
        for target in (30000, 59142, 41000):
            steps.clear()
            result = session.set_focus_position(target)
            assert result["ok"] is True, (target, steps, result)
            # The read-back is the lens, not a value from one nudge ago.
            assert abs(result["position"] - follow_units(fake)) <= 1, (target, result)
            assert abs(follow_units(fake) - target) <= 400, (target, steps)
            # Size 1 doesn't move this lens, so it is never sent; nor is 7.
            assert all(2 <= abs(s) <= 6 for s in steps), steps
            assert result["stop_reason"] == "within_tolerance"

    def test_the_old_two_read_rule_is_fooled_by_the_lag(self, real_lens, fake):
        # What the rig did: trust a read-back as soon as it stops changing.
        session = real_lens(movie_read_timeout_s=0.01, movie_read_stable_s=0.0)
        result = session.set_focus_position(30000)
        assert not (result["ok"] and abs(follow_units(fake) - 30000) <= 400)

    def test_a_get_after_a_stills_nudge_waits_for_the_new_position(self, real_lens, fake):
        session = real_lens()
        session.set_focus_position(30000)
        # Moved outside the module's knowledge: in stills, where the body
        # doesn't publish, so the next movie session starts stale.
        session.focus_near_far(5)
        assert abs(session.get_focus_position() - follow_units(fake)) <= 1

    def test_a_known_position_is_read_without_waiting(self, real_lens, fake, monkeypatch):
        session = real_lens()
        session.set_focus_position(30000)
        waits = []
        read = session._movie_read
        monkeypatch.setattr(
            session, "_movie_read", lambda previous=None: (waits.append(previous), read(previous))[1]
        )
        session.get_focus_position()
        assert waits == [None]

    def test_trace_records_the_late_read_back(self, real_lens, fake):
        session = real_lens()
        session.set_focus_position(30000)
        before = follow_units(fake)
        trace = session.trace_focus_read(4, 4 * LAG_S)

        changes = trace["changes"]
        assert changes[0]["position"] == before
        assert changes[-1]["position"] == follow_units(fake) != before
        # Published only after the lag, not at the nudge.
        assert changes[1]["t_s"] - trace["nudge_at_s"] >= LAG_S * 0.8
        assert fake.property_value("exposure_program_mode") == STILLS_M
        # The trace moved the lens; nothing claims to know where it is.
        assert session.capture()["focus_position"] is None


class TestNudgeChoice:
    SIZES = [(2, 382.0), (3, 693.0), (4, 2329.0), (5, 6470.0), (6, 18893.0)]

    @pytest.mark.parametrize(
        "distance,cap,size",
        [
            (30000, 6, 6),   # a long way: the largest that fits
            (10000, 6, 5),
            (2400, 6, 4),
            (2300, 6, 3),    # 2329 would overshoot
            (500, 6, 2),
            (200, 6, 2),     # nothing fits: the smallest that moves
            (30000, 3, 3),   # capped after an overshoot
        ],
    )
    def test_largest_size_that_does_not_overshoot(self, distance, cap, size):
        from session import CameraSession

        assert CameraSession._pick_nudge(self.SIZES, distance, cap) == size

    def test_sizes_that_do_not_move_are_never_used(self, movie_session):
        session = movie_session(
            movie_nudge_units=MEASURED_UNITS, movie_max_nudge_size=6
        )
        assert [s for s, _ in session._nudge_sizes()] == [2, 3, 4, 5, 6]


class TestFallback:
    def test_no_follow_focus_falls_back_to_the_emulation(self, movie_session, fake, logger):
        fake.movie_focus_supported = False
        session = movie_session()
        result = session.set_focus_position(10 * UNITS_PER_NUDGE)

        assert result["method"] == "nudge_fallback"
        assert result["units"] == "follow_focus"
        assert result["estimated"] is True
        assert result["position"] == 10 * UNITS_PER_NUDGE
        # Homed, then 10 nudges of size 3 out from the near stop.
        assert fake.property_value("focus_position") == fake.focus_min + 10 * 6
        assert fake.property_value("exposure_program_mode") == STILLS_M
        status = session.device_status()
        assert status["focus_method"] == "nudge"
        assert "movie-mode focus failed" in status["focus_fallback_reason"]
        assert "falling back" in logger.text("warning")

    def test_a_mode_switch_that_never_takes_falls_back(self, movie_session, fake):
        fake.mode_switch_works = False
        session = movie_session()
        result = session.set_focus_position(5 * UNITS_PER_NUDGE)
        assert result["method"] == "nudge_fallback"
        assert "timed out" in session.device_status()["focus_fallback_reason"]

    def test_fallback_none_fails_the_operation(self, movie_session, fake):
        fake.movie_focus_supported = False
        session = movie_session(movie_focus_fallback="none")
        with pytest.raises(SDKError):
            session.set_focus_position(20000)
        assert fake.property_value("exposure_program_mode") == STILLS_M

    def test_home_gives_movie_another_chance(self, movie_session, fake):
        fake.movie_focus_supported = False
        session = movie_session()
        session.set_focus_position(20000)
        assert session.device_status()["focus_method"] == "nudge"

        fake.movie_focus_supported = True
        result = session.home_focus()
        assert result["method"] == "movie"
        assert session.device_status()["focus_method"] == "movie"
        assert session.device_status()["focus_fallback_reason"] is None


class TestDisabledDrive:
    """The lens's AF/MF switch on MF: the body accepts near/far writes and
    ignores them. Before this guard every nudge "succeeded" and the lens
    never moved."""

    def test_movie_set_fails_loudly_and_does_not_fall_back(self, movie_session, fake):
        fake.near_far_enabled = False
        session = movie_session()
        with pytest.raises(ConfigurationError, match="AF/MF switch"):
            session.set_focus_position(20000)
        assert session.device_status()["focus_method"] == "movie"

    def test_the_emulation_fails_loudly_too(self, make_session, fake):
        fake.absolute_focus_supported = False
        fake.near_far_enabled = False
        session = make_session(fake, emulated_nudge_interval_s=0, emulated_travel_nudges=10)
        assert wait_until(lambda: session.connected)
        with pytest.raises(ConfigurationError, match="AF/MF switch"):
            session.set_focus_position(4)

    def test_a_raw_nudge_fails_loudly(self, movie_session, fake):
        fake.near_far_enabled = False
        session = movie_session()
        with pytest.raises(ConfigurationError):
            session.focus_near_far(3)


class TestStillsSafety:
    def test_connect_puts_a_body_left_in_movie_back_in_stills(self, movie_session, fake, logger):
        fake._properties["exposure_program_mode"]["value"] = MOVIE_M
        movie_session()
        assert fake.property_value("exposure_program_mode") == STILLS_M
        assert "interrupted focus operation" in logger.text("warning")

    def test_capture_refuses_to_fire_in_movie_mode(self, movie_session, fake):
        session = movie_session()
        # The body is somehow in movie mode and won't switch back: the focus
        # operation falls back, and the stranded mode is remembered.
        fake._properties["exposure_program_mode"]["value"] = MOVIE_M
        fake.mode_switch_works = False
        session.set_focus_position(20000)
        with pytest.raises(SDKError, match="stills"):
            session.capture()
        assert fake.triggers == 0

        fake.mode_switch_works = True
        session.capture()
        assert fake.property_value("exposure_program_mode") == STILLS_M
        assert fake.triggers == 1
