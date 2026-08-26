"""The office dashboard: the four presence states, exceptions and payroll.

The page exists to answer "is anything wrong this morning?", so most of what is
tested here is the difference between something that *looks* wrong and something
that *is*: a shift still being worked, an afternoon starter at nine in the
morning, and a salaried employee who is not on the four-weekly sheet at all.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from app.models import (
    PAY_SALARY,
    DIRECTION_IN,
    DIRECTION_OUT,
    METHOD_MANUAL,
    AttendanceEvent,
    ShiftPattern,
)
from app.services import dashboard
from app.services.payroll_sheet import period_bounds
from app.services.timesheet import build_timesheet

from .conftest import make_employee

LONDON = ZoneInfo("Europe/London")

# A Wednesday in winter, so local time is UTC and the arithmetic in these tests
# reads the same as the wall clock the office would be looking at.
DAY = dt.date(2026, 1, 21)


def _now(hour: int, minute: int = 0) -> dt.datetime:
    """A naive-UTC "now" on the test day."""
    return dt.datetime.combine(DAY, dt.time(hour, minute))


def _clock(db, employee, direction, moment, **kwargs):
    db.session.add(
        AttendanceEvent(
            employee_id=employee.id,
            direction=direction,
            occurred_at=moment,
            **kwargs,
        )
    )
    db.session.commit()


def _day_shift(db, name="Day shift", start=dt.time(7, 30), end=dt.time(16, 0), default=True):
    pattern = ShiftPattern(
        name=name,
        start_time=start,
        end_time=end,
        is_default=default,
        unpaid_break_minutes=30,
        break_applies_after_minutes=300,
    )
    db.session.add(pattern)
    db.session.commit()
    return pattern


# --- the four states ----------------------------------------------------------
def test_presence_splits_the_day_into_four_states(db):
    _day_shift(db)
    here = make_employee(db, ref="E1", first="Here", last="Now")
    gone = make_employee(db, ref="E2", first="Gone", last="Home")
    late = make_employee(db, ref="E3", first="Late", last="Riser")
    afternoon_pattern = _day_shift(
        db, name="Afternoon", start=dt.time(14, 0), end=dt.time(22, 0), default=False
    )
    later = make_employee(db, ref="E4", first="Later", last="Starter")
    later.shift_pattern_id = afternoon_pattern.id
    db.session.commit()

    _clock(db, here, DIRECTION_IN, _now(7, 28))
    _clock(db, gone, DIRECTION_IN, _now(7, 30))
    _clock(db, gone, DIRECTION_OUT, _now(13, 0))

    records = dashboard.presence(DAY, LONDON, now=_now(14, 30))

    assert [r.employee.id for r in records.on_site] == [here.id]
    assert [r.employee.id for r in records.left] == [gone.id]
    # At 14:30 the afternoon starter is overdue as well: their 14:00 shift has
    # begun. Wind the clock back and they are simply not due yet, which is the
    # whole point of keeping the two apart.
    assert [r.employee.id for r in records.overdue] == [late.id, later.id]
    morning = dashboard.presence(DAY, LONDON, now=_now(9, 0))
    assert [r.employee.id for r in morning.not_due] == [later.id]
    assert [r.employee.id for r in morning.overdue] == [late.id]
    assert records.expected == 4


def test_shares_are_percentages_of_everybody_expected(db):
    _day_shift(db)
    for index in range(4):
        make_employee(db, ref=f"E{index}", first=f"P{index}", last="Person")
    records = dashboard.presence(DAY, LONDON, now=_now(9, 0))

    assert records.share(records.overdue) == 100.0
    assert records.share(records.on_site) == 0.0


def test_share_of_nobody_is_zero_not_a_crash(db):
    records = dashboard.presence(DAY, LONDON, now=_now(9, 0))

    assert records.expected == 0
    assert records.share(records.on_site) == 0.0


# --- the fire register --------------------------------------------------------
def test_departments_count_who_is_here_against_who_was_expected(db):
    _day_shift(db)
    welder = make_employee(db, ref="E1", first="Alys", last="Morgan", department="Welding")
    make_employee(db, ref="E2", first="Karl", last="Hendry", department="Welding")
    fitter = make_employee(db, ref="E3", first="Sam", last="Fletcher", department="Fabrication")
    _clock(db, welder, DIRECTION_IN, _now(7, 28))
    _clock(db, fitter, DIRECTION_IN, _now(7, 29))

    records = dashboard.presence(DAY, LONDON, now=_now(9, 0))
    grouped = {dept.name: dept for dept in dashboard.by_department(records)}

    assert grouped["Welding"].headcount == 2
    assert [e.full_name for e in grouped["Welding"].on_site] == ["Alys Morgan"]
    assert grouped["Fabrication"].headcount == 1


def test_employees_with_no_department_are_still_on_the_register(db):
    _day_shift(db)
    nobody = make_employee(db, ref="E1", first="No", last="Department")
    _clock(db, nobody, DIRECTION_IN, _now(7, 28))

    grouped = dashboard.by_department(dashboard.presence(DAY, LONDON, now=_now(9, 0)))

    assert [dept.name for dept in grouped] == ["No department"]


# --- what is and is not a problem ---------------------------------------------
def test_a_shift_being_worked_now_is_not_a_missed_clock_out(db):
    """Half the shop floor is mid-shift at eleven; none of it is a problem.

    ``end_is_assumed`` is measured against the real clock, so this uses a shift
    that runs to 23:59 today rather than a fixed date - a shift that ended
    hours ago is exactly the case the *next* test covers.
    """
    _day_shift(db, start=dt.time(0, 0), end=dt.time(23, 59))
    employee = make_employee(db)
    today = dt.date.today()
    _clock(db, employee, DIRECTION_IN, dt.datetime.combine(today, dt.time(12, 0)))

    open_shifts = build_timesheet(today, today, LONDON)

    assert len(open_shifts) == 1
    assert dashboard.in_progress(open_shifts[0])
    assert dashboard.missed_clockouts(LONDON, today) == []


def test_no_clock_out_after_the_shift_ended_is_a_missed_clock_out(db):
    _day_shift(db)
    employee = make_employee(db)
    # Two days back, so the 16:00 shift end is long past whatever "now" is.
    two_days_back = dt.datetime.combine(DAY - dt.timedelta(days=2), dt.time(7, 28))
    _clock(db, employee, DIRECTION_IN, two_days_back)

    missed = dashboard.missed_clockouts(LONDON, DAY)

    assert len(missed) == 1
    assert missed[0].employee.id == employee.id
    assert not dashboard.in_progress(missed[0])


def test_a_missed_clock_out_older_than_the_window_is_left_alone(db):
    _day_shift(db)
    employee = make_employee(db)
    old = dt.datetime.combine(DAY - dt.timedelta(days=30), dt.time(7, 28))
    _clock(db, employee, DIRECTION_IN, old)

    assert dashboard.missed_clockouts(LONDON, DAY) == []


# --- needs attention ----------------------------------------------------------
def test_overdue_turns_red_after_two_hours(db):
    _day_shift(db)
    make_employee(db, ref="E1", first="Just", last="Late")

    mild = dashboard.attention(
        dashboard.presence(DAY, LONDON, now=_now(8, 30)), [], DAY, LONDON
    )
    serious = dashboard.attention(
        dashboard.presence(DAY, LONDON, now=_now(11, 0)), [], DAY, LONDON
    )

    assert mild[0].severity == "warn"
    assert mild[0].issue == "1h 00m overdue"
    assert serious[0].severity == "danger"
    assert serious[0].due_to_start == dt.time(7, 30)


def test_somebody_missing_now_outranks_a_correction_from_last_week(db):
    _day_shift(db)
    late = make_employee(db, ref="E1", first="Late", last="Riser")
    forgetful = make_employee(db, ref="E2", first="For", last="Getful")
    _clock(
        db,
        forgetful,
        DIRECTION_IN,
        dt.datetime.combine(DAY - dt.timedelta(days=3), dt.time(7, 28)),
    )

    records = dashboard.presence(DAY, LONDON, now=_now(11, 0))
    rows = dashboard.attention(records, dashboard.missed_clockouts(LONDON, DAY), DAY, LONDON)

    assert [row.employee.id for row in rows] == [late.id, forgetful.id]
    assert rows[1].issue.startswith("No clock-out")


def test_nothing_to_report_when_everybody_due_is_here(db):
    _day_shift(db)
    employee = make_employee(db)
    _clock(db, employee, DIRECTION_IN, _now(7, 28))

    records = dashboard.presence(DAY, LONDON, now=_now(11, 0))

    assert dashboard.attention(records, [], DAY, LONDON) == []


def test_no_shift_pattern_reads_as_not_clocked_in_not_zero_overdue(db):
    """There is no start time to be late against, so do not invent one."""
    make_employee(db)

    rows = dashboard.attention(
        dashboard.presence(DAY, LONDON, now=_now(11, 0)), [], DAY, LONDON
    )

    assert rows[0].issue == "Not clocked in"
    assert rows[0].due_to_start is None
    assert rows[0].severity == "warn"


def test_never_clocked_shows_as_no_last_clocking(db):
    _day_shift(db)
    make_employee(db)

    rows = dashboard.attention(
        dashboard.presence(DAY, LONDON, now=_now(11, 0)), [], DAY, LONDON
    )

    assert rows[0].last_clocking is None


# --- payroll readiness --------------------------------------------------------
ANCHOR = dt.date(2026, 3, 30)


def test_period_bounds_finds_the_period_a_day_falls_in():
    period, start, end = period_bounds(dt.date(2026, 8, 26), ANCHOR)

    assert (period, start, end) == (6, dt.date(2026, 8, 17), dt.date(2026, 9, 13))
    assert (end - start).days + 1 == 28


def test_period_bounds_handles_a_day_before_the_anchor():
    """Floor division: the day belongs to the period running up to the anchor."""
    _, start, end = period_bounds(ANCHOR - dt.timedelta(days=1), ANCHOR)

    assert end == ANCHOR - dt.timedelta(days=1)
    assert start == ANCHOR - dt.timedelta(days=28)


def test_payroll_counts_the_period_so_far_not_the_whole_of_it(db):
    _day_shift(db)
    make_employee(db)

    snapshot = dashboard.payroll(dt.date(2026, 8, 26), LONDON, ANCHOR)

    assert snapshot.period == 6
    assert snapshot.week == 2
    assert snapshot.covered_to == dt.date(2026, 8, 26)
    assert snapshot.week_states == ["is-done", "is-current", "", ""]


def test_payroll_does_not_count_a_shift_still_being_worked_as_a_warning(db):
    """Otherwise every clocked-in employee reads as a payroll problem all day."""
    _day_shift(db, start=dt.time(0, 0), end=dt.time(23, 59))
    employee = make_employee(db)
    today = dt.date.today()
    _clock(db, employee, DIRECTION_IN, dt.datetime.combine(today, dt.time(12, 0)))

    snapshot = dashboard.payroll(today, LONDON, ANCHOR)

    assert snapshot.warnings == 0
    assert snapshot.rows_ready == snapshot.on_sheet


def test_payroll_counts_a_clock_out_with_no_clock_in(db):
    _day_shift(db)
    employee = make_employee(db)
    period_start = period_bounds(dt.date(2026, 8, 26), ANCHOR)[1]
    _clock(
        db,
        employee,
        DIRECTION_OUT,
        dt.datetime.combine(period_start, dt.time(16, 0)),
        method=METHOD_MANUAL,
    )

    snapshot = dashboard.payroll(dt.date(2026, 8, 26), LONDON, ANCHOR)

    assert snapshot.warnings == 1
    assert snapshot.rows_ready == 0


def test_payroll_counts_a_missing_pay_rate(db):
    _day_shift(db)
    paid = make_employee(db, ref="E1", first="Has", last="Rate")
    paid.basic_rate = "14.50"
    make_employee(db, ref="E2", first="No", last="Rate")
    db.session.commit()

    snapshot = dashboard.payroll(dt.date(2026, 8, 26), LONDON, ANCHOR)

    assert snapshot.on_sheet == 2
    assert snapshot.missing_rate == 1


def test_salaried_staff_are_not_on_the_four_weekly_sheet(db):
    _day_shift(db)
    make_employee(db, ref="E1", first="Wage", last="Earner")
    make_employee(db, ref="E2", first="Sal", last="Aried", pay_basis=PAY_SALARY)

    snapshot = dashboard.payroll(dt.date(2026, 8, 26), LONDON, ANCHOR)

    assert snapshot.on_sheet == 1


# --- the kiosk ----------------------------------------------------------------
def test_kiosk_reports_the_last_scan_and_calls_it_awake(db):
    employee = make_employee(db)
    _clock(db, employee, DIRECTION_IN, _now(9, 0), device_label="Workshop kiosk")

    state = dashboard.kiosk([employee], index_size=3, now=_now(9, 4))

    assert state.device == "Workshop kiosk"
    assert state.minutes_ago == 4
    assert state.since_text == "4 min ago"
    assert state.is_live


def test_a_quiet_kiosk_is_reported_as_quiet_not_offline(db):
    """There is no heartbeat, so "offline" would be a guess dressed up as fact."""
    employee = make_employee(db)
    _clock(db, employee, DIRECTION_IN, _now(9, 0), device_label="Workshop kiosk")

    state = dashboard.kiosk([employee], index_size=3, now=_now(14, 12))

    assert not state.is_live
    assert state.since_text == "5h 12m ago"


def test_a_manual_entry_says_nothing_about_the_kiosk(db):
    """Typing a correction in the office does not prove the screen is working."""
    employee = make_employee(db)
    _clock(db, employee, DIRECTION_IN, _now(9, 0), device_label="Workshop kiosk")
    _clock(db, employee, DIRECTION_OUT, _now(9, 30), method=METHOD_MANUAL)

    state = dashboard.kiosk([employee], index_size=3, now=_now(9, 35))

    assert state.last_scan == _now(9, 0)
    assert state.minutes_ago == 35


def test_kiosk_with_nothing_recorded_at_all(db):
    employee = make_employee(db)

    state = dashboard.kiosk([employee], index_size=0)

    assert state.last_scan is None
    assert state.since_text == "no scans yet"
    assert not state.is_live


def test_faces_still_to_enrol_is_active_minus_enrolled(db):
    active = make_employee(db, ref="E1", first="Not", last="Enrolled")

    state = dashboard.kiosk([active], index_size=0)

    assert state.active == 1
    assert state.enrolled == 0
    assert state.to_enrol == 1
