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
