"""`focus_method: movie` - absolute focus from the movie-mode follow-focus
read-back, with the near/far emulation as the fallback.

The ILCE-7RM5 publishes the lens's real position only in movie mode
(verified on the rig), so every operation switches to movie, drives near/far
in a closed loop against that read-back, and switches back to stills. These
tests run that against the fake's physical model: positions in follow-focus
units, 0 = near stop .. 65535 = far stop.
"""

import math

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


# CONSULT-2297, Nines-Photographer 2026-11-05: a shoes sweep asked for 40000
# (tolerance 400) and the lens hunted. Every nudge size ran ~4x the table
# (size 2: ~1600 units, not 382), so "within half a nudge" never fired and
# the loop flipped across the target until the reversal cap. The sony log:
#   Nudges: -5->28529, +4->38465, +3->42542, -2->40999, -2->39366, +2->40999, -2->39366
# and the webapp's retry from there:
#   Nudges: +2->40999, -2->39366, +2->40999, -2->39366, +2->40999  (failed at 40999)
TICKET_TARGET = 40000
TICKET_TRACE = [
    (-5, 28529), (+4, 38465), (+3, 42542), (-2, 40999), (-2, 39366), (+2, 40999), (-2, 39366),
]
TICKET_RETRY_START = 39366
TICKET_RETRY_TRACE = [(+2, 40999), (-2, 39366), (+2, 40999), (-2, 39366), (+2, 40999)]
# Where the first move started: where a size-5 nudge 4x the table (25880)
# lands on 28529. (The log doesn't say; any start that picks size 5 first
# replays the same nudges.)
TICKET_START = 28529 + 4 * 6470
# What one nudge of each size moved in that trace; size 6 wasn't sent, so 4x.
TICKET_UNITS = {2: 1633, 3: 4077, 4: 9936, 5: 25880, 6: 4 * 18893}


class TicketLens:
    """The rig's lens as it behaved in the ticket: every read-back in the two
    traces above, exactly, and the trace's per-size travel anywhere else.
    Read back instantly (the lag has its own tests above)."""

    def __init__(self, start, units=TICKET_UNITS):
        self.pos = start
        self.units = units
        self.steps = []
        self.path = [start]
        self.jumps = {}
        for first, trace in ((TICKET_START, TICKET_TRACE), (TICKET_RETRY_START, TICKET_RETRY_TRACE)):
            at = first
            for step, read in trace:
                assert self.jumps.setdefault((at, step), read) == read
                at = read

    def nudge(self, step):
        self.steps.append(step)
        sign = 1 if step > 0 else -1
        to = self.jumps.get((self.pos, step), self.pos + sign * self.units.get(abs(step), 0))
        self.pos = max(0, min(0xFFFF, to))
        self.path.append(self.pos)

    def install(self, session, monkeypatch):
        monkeypatch.setattr(session, "_nudge_focus", self.nudge)
        monkeypatch.setattr(session, "_movie_read_once", lambda: self.pos)
        return self


@pytest.fixture
def ticket_lens(movie_session, monkeypatch):
    """A session configured like the rig (the 2026-10-27 table, sizes 2-6,
    tolerance 400) driving a `TicketLens`."""

    def build(start, **overrides):
        config = dict(movie_nudge_units=MEASURED_UNITS, movie_max_nudge_size=6)
        config.update(overrides)
        session = movie_session(**config)
        return session, TicketLens(start).install(session, monkeypatch)

    return build


def closer_of_last_two(lens, target):
    """Whether the lens ended at the closer of its last two positions."""
    if len(lens.path) < 2:
        return True
    return abs(target - lens.path[-1]) <= abs(target - lens.path[-2])


class TestLongNudges:
    """CONSULT-2297: nudges far longer than `movie_nudge_units` says."""

    def test_the_fixture_lens_runs_about_4x_the_table(self):
        for size, units in TICKET_UNITS.items():
            assert 3.5 <= units / MEASURED_UNITS[size - 1] <= 6.5, size
        # And it replays the ticket's traces exactly.
        for start, trace in ((TICKET_START, TICKET_TRACE), (TICKET_RETRY_START, TICKET_RETRY_TRACE)):
            lens = TicketLens(start)
            for step, read in trace:
                lens.nudge(step)
                assert lens.pos == read

    @pytest.mark.parametrize("start", [TICKET_START, TICKET_RETRY_START, 40999])
    def test_the_ticket_move_no_longer_fails_by_hunting(self, ticket_lens, start, logger):
        session, lens = ticket_lens(start)
        result = session.set_focus_position(TICKET_TARGET)

        assert result["ok"] is True, (lens.steps, result)
        assert result["stop_reason"] != "hunting", (lens.steps, result)
        assert result["position"] == lens.pos
        miss = abs(lens.pos - TICKET_TARGET)
        assert miss <= 400 or closer_of_last_two(lens, TICKET_TARGET), (lens.steps, result)
        assert result["miss"] == miss
        if miss > 400:
            # Outside the tolerance: accepted, but said so.
            assert result["near_miss"] is True
            assert str(miss) in result["warning"]
            assert "near miss" in logger.text("warning")
        else:
            assert result["near_miss"] is False
            assert "warning" not in result

    def test_the_first_ticket_move_learns_the_long_step_and_stops(self, ticket_lens):
        session, lens = ticket_lens(TICKET_START)
        result = session.set_focus_position(TICKET_TARGET)
        # Fewer nudges than the 7 it hunted through, ending inside the window
        # or at most half a (measured) size-2 step off.
        assert result["attempts"] < len(TICKET_TRACE), lens.steps
        assert abs(lens.pos - TICKET_TARGET) <= TICKET_UNITS[2] / 2

    @pytest.mark.parametrize(
        "start,trace",
        [(TICKET_START, TICKET_TRACE), (TICKET_RETRY_START, TICKET_RETRY_TRACE)],
    )
    def test_without_learning_it_still_stops_at_the_closer_side(
        self, ticket_lens, monkeypatch, start, trace
    ):
        # The table as the only estimate (the 0.3.0 loop's view of the lens):
        # it flips across the target, and must stop at the closer position -
        # the retry in the ticket ended on the farther one, 40999.
        session, lens = ticket_lens(start)
        monkeypatch.setattr(
            session, "_learned_sizes", lambda sizes, seen: (list(sizes), sizes[0][1])
        )
        result = session.set_focus_position(TICKET_TARGET)

        assert result["stop_reason"] == "hunting"
        assert lens.pos == 39366, lens.steps  # 634 off, not 999
        assert result["ok"] is True and result["near_miss"] is True
        # It spots the back and forth instead of waiting out the reversal cap.
        assert len(lens.steps) <= len(trace), lens.steps

    @pytest.mark.parametrize("target", list(range(20000, 60001, 1777)))
    def test_any_target_on_a_long_lens_lands_or_stops_at_the_closer_side(
        self, ticket_lens, target
    ):
        # The same 4x lens without the ticket's exact read-backs: wherever the
        # target falls between two size-2 positions, the loop ends inside the
        # tolerance or within half a size-2 step - never by hunting.
        session, lens = ticket_lens(TICKET_START)
        lens.jumps.clear()
        result = session.set_focus_position(target)
        assert result["stop_reason"] in ("within_tolerance", "within_half_nudge"), (
            target, lens.steps, result,
        )
        assert abs(lens.pos - target) <= TICKET_UNITS[2] / 2, (target, lens.steps)


class TestAcceptTolerance:
    def test_defaults_to_twice_the_tolerance(self, ticket_lens):
        from session import _DEFAULT_FOCUS_ACCEPT_FACTOR

        session, _ = ticket_lens(TICKET_RETRY_START)
        result = session.set_focus_position(TICKET_TARGET)
        assert result["accept_tolerance"] == round(_DEFAULT_FOCUS_ACCEPT_FACTOR * 400)
        result = session.set_focus_position(TICKET_TARGET, tolerance=100)
        assert result["accept_tolerance"] == round(_DEFAULT_FOCUS_ACCEPT_FACTOR * 100)

    def test_a_miss_past_the_accept_window_is_not_ok(self, ticket_lens, logger):
        session, lens = ticket_lens(TICKET_RETRY_START)
        result = session.set_focus_position(TICKET_TARGET, accept_tolerance=500)
        assert abs(lens.pos - TICKET_TARGET) == 634
        assert result["ok"] is False
        assert result["near_miss"] is False
        assert "warning" not in result
        assert "focus did not reach 40000" in logger.text("warning")

    def test_config_sets_the_window_and_the_call_overrides_it(self, ticket_lens):
        session, _ = ticket_lens(TICKET_RETRY_START, movie_focus_accept_tolerance=500)
        assert session.set_focus_position(TICKET_TARGET)["ok"] is False
        result = session.set_focus_position(TICKET_TARGET, accept_tolerance=700)
        assert result["ok"] is True and result["accept_tolerance"] == 700

    def test_never_narrower_than_the_tolerance(self, ticket_lens):
        session, _ = ticket_lens(TICKET_RETRY_START)
        result = session.set_focus_position(TICKET_TARGET, tolerance=700, accept_tolerance=100)
        assert result["accept_tolerance"] == 700
        assert result["ok"] is True and result["near_miss"] is False


# Step 1.1 of CONSULT-2297, measured on the rig 2026-11-05: |movement| medians
# per nudge size at the two zooms the products shoot at.
RIG_16MM = {2: 1690, 3: 4160, 4: 10980}
RIG_35MM = {2: 354, 3: 995, 4: 2878}


class ZoomLens:
    """A lens with the rig's measured nudge sizes at one zoom: sizes 2-4 the
    medians, the others the module's table for that zoom (not measured), and
    each move `growth` larger per unit of position above 40000 (size 2 at
    16mm ran ~1555 at 29k and ~1850 at 50k). Read back instantly."""

    def __init__(self, start, measured, table, growth=0.0):
        self.pos = start
        self.units = {s: table[s - 1] for s in range(1, 8)}
        self.units.update(measured)
        self.growth = growth
        self.steps = []
        self.path = [start]

    def nudge(self, step):
        self.steps.append(step)
        travel = self.units[abs(step)] * (1 + self.growth * (self.pos - 40000))
        self.pos = max(0, min(0xFFFF, round(self.pos + (1 if step > 0 else -1) * travel)))
        self.path.append(self.pos)

    def install(self, session, monkeypatch):
        monkeypatch.setattr(session, "_nudge_focus", self.nudge)
        monkeypatch.setattr(session, "_movie_read_once", lambda: self.pos)
        return self


@pytest.fixture
def zoomed(movie_session, fake):
    """A session with the module's default per-zoom tables and near-miss
    windows, the fake lens zoomed to `mm`."""
    from session import _DEFAULT_FOCUS_ACCEPT_BY_ZOOM, _DEFAULT_MOVIE_NUDGE_UNITS_BY_ZOOM

    def build(mm, **overrides):
        config = dict(
            movie_nudge_units=MEASURED_UNITS,
            movie_nudge_units_by_zoom=dict(_DEFAULT_MOVIE_NUDGE_UNITS_BY_ZOOM),
            movie_focus_accept_by_zoom=dict(_DEFAULT_FOCUS_ACCEPT_BY_ZOOM),
            movie_max_nudge_size=6,
        )
        config.update(overrides)
        session = movie_session(**config)
        fake._zoom_um = float(mm * 1000)
        return session

    return build


class TestZoomTables:
    def test_the_table_follows_the_focal_length(self, zoomed, fake):
        from session import _NUDGE_UNITS_16MM, _NUDGE_UNITS_35MM

        session = zoomed(16)
        assert session._nudge_table() == (_NUDGE_UNITS_16MM, 16.0)
        fake._zoom_um = 35000.0
        assert session._nudge_table() == (_NUDGE_UNITS_35MM, 35.0)
        # Outside the calibrated range: the nearest end.
        fake._zoom_um = 12000.0
        assert session._nudge_table()[0] == _NUDGE_UNITS_16MM
        # Between: every moving size strictly between the two, log-linear.
        fake._zoom_um = 24000.0
        mid, mm = session._nudge_table()
        assert mm == 24.0
        for a, m, b in zip(_NUDGE_UNITS_35MM, mid, _NUDGE_UNITS_16MM):
            assert (a == m == b == 0) or a < m < b
        t = math.log(24 / 16) / math.log(35 / 16)
        assert mid[1] == pytest.approx(math.exp((1 - t) * math.log(1690) + t * math.log(354)))

    def test_no_focal_length_or_no_tables_falls_back_to_the_flat_table(
        self, zoomed, movie_session, fake
    ):
        session = zoomed(16)
        fake.power_zoom = False
        assert session._nudge_table() == (tuple(MEASURED_UNITS), None)

    def test_without_tables_the_flat_table_is_used(self, movie_session):
        session = movie_session(movie_nudge_units=MEASURED_UNITS)
        assert session._nudge_table() == (tuple(MEASURED_UNITS), None)

    def test_at_16mm_a_short_move_is_one_size_2_nudge(self, zoomed, monkeypatch):
        from session import CameraSession

        # The 35mm-ish flat table would send a size 3 here (693 <= 1000) -
        # 4160 units at 16mm, overshooting by 3000 and starting the hunt.
        flat = [(s, float(u)) for s, u in enumerate(MEASURED_UNITS, 1) if u > 0]
        assert CameraSession._pick_nudge(flat, 1000, 6) == 3

        for start, target in ((39000, 40000), (41000, 40000)):
            session = zoomed(16)
            lens = ZoomLens(start, RIG_16MM, MEASURED_UNITS).install(session, monkeypatch)
            result = session.set_focus_position(target)
            assert lens.steps[0] in (2, -2), lens.steps
            assert result["attempts"] == 1, lens.steps
            assert result["ok"] is True and result["stop_reason"] != "hunting", result

    @pytest.mark.parametrize(
        "start", [8000, 20000, 30000, 37000, 38500, 39300, 40700, 41500, 43000, 50000, 59142]
    )
    @pytest.mark.parametrize(
        # 16mm growth: size 2 ran ~1555 at 29k and ~1850 at 50k, ~9e-6 per unit.
        "mm,measured,growth", [(16, RIG_16MM, 9e-6), (35, RIG_35MM, 0.0)]
    )
    def test_the_rig_lens_lands_from_both_sides_of_40000(
        self, zoomed, monkeypatch, mm, measured, growth, start
    ):
        from session import _DEFAULT_MOVIE_NUDGE_UNITS_BY_ZOOM

        session = zoomed(mm)
        table = _DEFAULT_MOVIE_NUDGE_UNITS_BY_ZOOM[float(mm)]
        lens = ZoomLens(start, measured, table, growth).install(session, monkeypatch)
        # At 16mm a size-2 nudge is ~1700, so the closest reachable position
        # can be ~850 off: the 16mm default near-miss window (1000) covers it;
        # 35mm keeps 2 x tolerance.
        result = session.set_focus_position(40000)
        assert result["accept_tolerance"] == {16: 1000, 35: 800}[mm]

        assert result["stop_reason"] in ("within_tolerance", "within_half_nudge"), (
            lens.path, result,
        )
        # As close as the smallest nudge allows: within half of one (10%
        # margin for the position growth), so another would land further off.
        assert result["miss"] <= max(400, 1.1 * lens.units[2] / 2), (lens.path, result)
        # Inside the tolerance, or a near miss (ok, flagged) - never a failure.
        assert result["ok"] is True, (lens.path, result)
        assert result["near_miss"] == (result["miss"] > 400)


class TestAcceptByZoom:
    @pytest.mark.parametrize(
        "zoom,tolerance,window",
        [
            (16, 400, 1000),
            (35, 400, 800),   # 35mm: 2 x tolerance
            (12, 400, 1000),  # outside the range: the nearest end
            (50, 400, 800),
            (16, 600, 1200),  # never below 2 x tolerance
            (35, 100, 200),
        ],
    )
    def test_the_default_window_follows_the_focal_length(self, zoomed, zoom, tolerance, window):
        session = zoomed(zoom)
        assert session._accept_tolerance(tolerance, None, float(zoom)) == window

    def test_between_focal_lengths_it_blends(self, zoomed):
        session = zoomed(24)
        t = math.log(24 / 16) / math.log(35 / 16)
        assert session._accept_tolerance(400, None, 24.0) == round((1 - t) * 1000 + t * 800)

    def test_no_focal_length_is_the_factor(self, zoomed):
        assert zoomed(16)._accept_tolerance(400, None, None) == 800

    def test_the_attribute_and_the_call_win(self, zoomed):
        session = zoomed(16, movie_focus_accept_tolerance=600)
        assert session._accept_tolerance(400, None, 16.0) == 600
        assert session._accept_tolerance(400, 900, 16.0) == 900

    def test_a_move_at_16mm_reports_the_16mm_window(self, zoomed, monkeypatch):
        session = zoomed(16)
        ZoomLens(39000, RIG_16MM, MEASURED_UNITS).install(session, monkeypatch)
        assert session.set_focus_position(40000)["accept_tolerance"] == 1000


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
