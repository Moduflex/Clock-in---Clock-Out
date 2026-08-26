"""Monday-Sunday weeks, the standard/overtime split, and the weekly export.

Overtime is only meaningful against a week, so nearly everything here is really
a test that the week boundary is where it should be: the split has to be settled
Monday to Sunday and then added up, never worked out on a whole reporting period.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.models import DIRECTION_IN, DIRECTION_OUT, AttendanceEvent, ShiftPattern, WorkingWeek
from app.services.timesheet import (
    build_timesheet,
    default_range,
    is_whole_weeks,
    pay_range,
    split_overtime,
    summarise,
    to_master_csv,
    to_weekly_csv,
    week_end,
    week_start,
    weekly_totals,
    whole_weeks,
)

from .conftest import make_employee

LONDON = ZoneInfo("Europe/London")

# Monday 5 January 2026 through Sunday 11 January 2026.
MONDAY = dt.date(2026, 1, 5)


def _event(employee, direction, moment):
    return AttendanceEvent(
        employee_id=employee.id, direction=direction, occurred_at=moment
    )


def _week(db, hours: float, name: str | None = None, default: bool = False) -> WorkingWeek:
    week = WorkingWeek(
        name=name or f"{hours:g}-hour week", hours=hours, is_default=default
    )
    db.session.add(week)
    db.session.commit()
    return week


def _pattern(db, **kwargs) -> ShiftPattern:
    """A wide 06:00-22:00 band so the day itself never limits the hours worked."""
    defaults = dict(
        name="Long day",
        start_time=dt.time(6, 0),
        end_time=dt.time(22, 0),
        unpaid_break_minutes=0,
        break_applies_after_minutes=0,
        is_default=True,
    )
    defaults.update(kwargs)
    pattern = ShiftPattern(**defaults)
    db.session.add(pattern)
    db.session.commit()
    return pattern


def _work(db, employee, day: dt.date, hours: float) -> None:
    """Clock *hours* of work on *day*, starting at 08:00 local."""
    start = dt.datetime.combine(day, dt.time(8, 0), tzinfo=LONDON)
    finish = start + dt.timedelta(hours=hours)
    db.session.add_all(
        [
            _event(
                employee,
                DIRECTION_IN,
                start.astimezone(dt.timezone.utc).replace(tzinfo=None),
            ),
            _event(
                employee,
                DIRECTION_OUT,
                finish.astimezone(dt.timezone.utc).replace(tzinfo=None),
            ),
        ]
    )
    db.session.commit()


# --- week boundaries ----------------------------------------------------------
def test_the_week_starts_on_monday_and_ends_on_sunday():
    for offset in range(7):
        day = MONDAY + dt.timedelta(days=offset)
        assert week_start(day) == MONDAY
        assert week_end(day) == dt.date(2026, 1, 11)

    # The Monday after is a new week, not the tail of the old one.
    assert week_start(dt.date(2026, 1, 12)) == dt.date(2026, 1, 12)


def test_sunday_belongs_to_the_week_that_began_on_monday():
    sunday = dt.date(2026, 1, 11)
    assert sunday.weekday() == 6
    assert week_start(sunday) == MONDAY


def test_whole_weeks_widens_outwards_only():
    start, end = whole_weeks(dt.date(2026, 1, 7), dt.date(2026, 1, 20))
    assert start == MONDAY  # back to the Monday
    assert end == dt.date(2026, 1, 25)  # on to the Sunday
    assert whole_weeks(MONDAY, dt.date(2026, 1, 11)) == (MONDAY, dt.date(2026, 1, 11))


def test_is_whole_weeks_only_accepts_monday_to_sunday():
    assert is_whole_weeks(MONDAY, dt.date(2026, 1, 11))
    assert not is_whole_weeks(MONDAY, dt.date(2026, 1, 10))  # ends Saturday
    assert not is_whole_weeks(dt.date(2026, 1, 6), dt.date(2026, 1, 11))  # starts Tuesday


def test_default_range_is_four_whole_weeks_ending_this_week():
    start, end = default_range(dt.date(2026, 1, 21))  # a Wednesday
    assert start == dt.date(2025, 12, 29)  # Monday
    assert end == dt.date(2026, 1, 25)  # Sunday of the current week
    assert (end - start).days + 1 == 28
    assert is_whole_weeks(start, end)


def test_default_range_takes_a_week_count():
    start, end = default_range(dt.date(2026, 1, 21), weeks=13)
    assert (end - start).days + 1 == 13 * 7
    assert is_whole_weeks(start, end)


def test_pay_range_is_four_whole_weeks_ending_last_sunday():
    start, end = pay_range(dt.date(2026, 1, 21))  # a Wednesday
    assert end == dt.date(2026, 1, 18)  # last Sunday, not this week's
    assert start == dt.date(2025, 12, 22)  # Monday four weeks earlier
    assert (end - start).days + 1 == 28
    assert is_whole_weeks(start, end)


def test_pay_range_excludes_the_week_in_progress():
    """Run on a Monday, the current week is still left out entirely."""
    monday = dt.date(2026, 1, 19)
    start, end = pay_range(monday)
    assert end == monday - dt.timedelta(days=1)
    assert end < week_start(monday)
    assert start == week_start(monday) - dt.timedelta(days=28)


def test_pay_range_is_default_range_shifted_back_one_week():
    for day in (dt.date(2026, 1, 19), dt.date(2026, 1, 21), dt.date(2026, 1, 25)):
        pay_start, pay_end = pay_range(day)
        default_start, default_end = default_range(day)
        assert pay_start == default_start - dt.timedelta(days=7)
        assert pay_end == default_end - dt.timedelta(days=7)


def test_pay_range_takes_a_week_count():
    start, end = pay_range(dt.date(2026, 1, 21), weeks=13)
    assert (end - start).days + 1 == 13 * 7
    assert end == dt.date(2026, 1, 18)
    assert is_whole_weeks(start, end)


# --- the split itself ---------------------------------------------------------
def test_a_standard_week_worked_has_no_overtime():
    assert split_overtime(40.0, 40.0) == (40.0, 0.0)
    assert split_overtime(31.5, 32.0) == (31.5, 0.0)


def test_hours_beyond_the_standard_week_are_overtime():
    """The figure from the spec: 65 hours on a 40-hour week is 40 + 25."""
    assert split_overtime(65.0, 40.0) == (40.0, 25.0)
    assert split_overtime(40.0, 32.0) == (32.0, 8.0)


def test_without_a_contracted_week_nothing_becomes_overtime():
    """A missing setting must not invent an overtime payment."""
    assert split_overtime(65.0, None) == (65.0, 0.0)


# --- per employee -------------------------------------------------------------
def test_master_sheet_splits_standard_and_overtime(db):
    _pattern(db)
    week = _week(db, 40.0, default=True)
    employee = make_employee(db, working_week_id=week.id)
    # Five 13-hour days: 65 hours in one Monday-Sunday week.
    for offset in range(5):
        _work(db, employee, MONDAY + dt.timedelta(days=offset), 13)

    total = summarise(build_timesheet(MONDAY, dt.date(2026, 1, 11), LONDON))[0]

    assert total.hours == pytest.approx(65.0)
    assert total.paid_hours == pytest.approx(65.0)
    assert total.standard_weekly_hours == 40.0
    assert total.standard_hours == pytest.approx(40.0)
    assert total.overtime_hours == pytest.approx(25.0)
    # The split always reconciles with the paid total.
    assert total.standard_hours + total.overtime_hours == pytest.approx(total.paid_hours)


def test_exactly_the_standard_week_earns_no_overtime(db):
    _pattern(db)
    week = _week(db, 40.0, default=True)
    employee = make_employee(db, working_week_id=week.id)
    for offset in range(5):
        _work(db, employee, MONDAY + dt.timedelta(days=offset), 8)

    total = summarise(build_timesheet(MONDAY, dt.date(2026, 1, 11), LONDON))[0]

    assert total.paid_hours == pytest.approx(40.0)
    assert total.standard_hours == pytest.approx(40.0)
    assert total.overtime_hours == 0.0


def test_a_thirty_two_hour_week_starts_overtime_sooner(db):
    """Two people, same 40 hours worked, different contracts."""
    _pattern(db)
    forty = _week(db, 40.0, default=True)
    thirty_two = _week(db, 32.0)
    full = make_employee(db, ref="E100", last="Adams", working_week_id=forty.id)
    short = make_employee(db, ref="E101", last="Brook", working_week_id=thirty_two.id)
    for employee in (full, short):
        for offset in range(5):
            _work(db, employee, MONDAY + dt.timedelta(days=offset), 8)

    totals = {t.employee.last_name: t for t in summarise(
        build_timesheet(MONDAY, dt.date(2026, 1, 11), LONDON)
    )}

    assert totals["Adams"].overtime_hours == 0.0
    assert totals["Brook"].standard_hours == pytest.approx(32.0)
    assert totals["Brook"].overtime_hours == pytest.approx(8.0)


def test_the_default_week_applies_to_anyone_without_one(db):
    _pattern(db)
    _week(db, 40.0, default=True)
    employee = make_employee(db)  # no working week of their own
    for offset in range(5):
        _work(db, employee, MONDAY + dt.timedelta(days=offset), 9)

    total = summarise(build_timesheet(MONDAY, dt.date(2026, 1, 11), LONDON))[0]

    assert total.standard_weekly_hours == 40.0
    assert total.overtime_hours == pytest.approx(5.0)


def test_no_standard_week_anywhere_leaves_every_hour_standard(db):
    _pattern(db)
    employee = make_employee(db)
    for offset in range(5):
        _work(db, employee, MONDAY + dt.timedelta(days=offset), 13)

    total = summarise(build_timesheet(MONDAY, dt.date(2026, 1, 11), LONDON))[0]

    assert total.standard_weekly_hours is None
    assert total.standard_hours == pytest.approx(65.0)
    assert total.overtime_hours == 0.0


def test_saturday_work_is_overtime_in_the_same_week(db):
    """Saturday is still the same Monday-Sunday week, so it tips into overtime."""
    _pattern(db)
    _week(db, 40.0, default=True)
    employee = make_employee(db)
    for offset in range(5):
        _work(db, employee, MONDAY + dt.timedelta(days=offset), 8)
    _work(db, employee, MONDAY + dt.timedelta(days=5), 6)  # Saturday

    total = summarise(build_timesheet(MONDAY, dt.date(2026, 1, 11), LONDON))[0]

    assert len(total.weeks) == 1
    assert total.paid_hours == pytest.approx(46.0)
    assert total.overtime_hours == pytest.approx(6.0)


# --- the week is the unit -----------------------------------------------------
def test_a_quiet_week_cannot_cancel_out_a_busy_one(db):
    """The whole point of settling per week rather than per period.

    50 hours then 30 hours is 80 over two weeks - under 2x40 - but the overtime
    already worked in the first week is not repayable by working less later.
    """
    _pattern(db)
    _week(db, 40.0, default=True)
    employee = make_employee(db)
    for offset in range(5):
        _work(db, employee, MONDAY + dt.timedelta(days=offset), 10)  # 50 hours
    next_monday = MONDAY + dt.timedelta(days=7)
    for offset in range(5):
        _work(db, employee, next_monday + dt.timedelta(days=offset), 6)  # 30 hours

    total = summarise(build_timesheet(MONDAY, dt.date(2026, 1, 18), LONDON))[0]

    assert total.paid_hours == pytest.approx(80.0)
    assert total.overtime_hours == pytest.approx(10.0)  # not 0
    assert total.standard_hours == pytest.approx(70.0)
    assert [w.overtime_hours for w in total.weeks] == [10.0, 0.0]


def test_weeks_are_reported_oldest_first_with_their_own_split(db):
    _pattern(db)
    _week(db, 40.0, default=True)
    employee = make_employee(db)
    _work(db, employee, MONDAY, 12)
    _work(db, employee, MONDAY + dt.timedelta(days=7), 9)

    total = summarise(build_timesheet(MONDAY, dt.date(2026, 1, 18), LONDON))[0]

    assert [w.start for w in total.weeks] == [MONDAY, MONDAY + dt.timedelta(days=7)]
    assert [w.end for w in total.weeks] == [
        dt.date(2026, 1, 11),
        dt.date(2026, 1, 18),
    ]
    assert [w.paid_hours for w in total.weeks] == [12.0, 9.0]
    assert total.weeks[0].label == "05/01-11/01/2026"


def test_a_shift_starting_sunday_night_stays_in_that_week(db):
    """A night shift is credited to the day it began - and so to that week."""
    _pattern(db, name="Nights", start_time=dt.time(20, 0), end_time=dt.time(4, 0))
    _week(db, 40.0, default=True)
    employee = make_employee(db)
    sunday = dt.date(2026, 1, 11)
    start = dt.datetime.combine(sunday, dt.time(20, 0), tzinfo=LONDON)
    db.session.add_all(
        [
            _event(employee, DIRECTION_IN, start.astimezone(dt.timezone.utc).replace(tzinfo=None)),
            _event(
                employee,
                DIRECTION_OUT,
                (start + dt.timedelta(hours=8)).astimezone(dt.timezone.utc).replace(tzinfo=None),
            ),
        ]
    )
    db.session.commit()

    total = summarise(build_timesheet(MONDAY, dt.date(2026, 1, 18), LONDON))[0]

    assert len(total.weeks) == 1
    assert total.weeks[0].start == MONDAY  # the week the Sunday belonged to


# --- exports ------------------------------------------------------------------
def test_master_csv_carries_the_split(db):
    _pattern(db)
    _week(db, 40.0, default=True)
    employee = make_employee(db, ref="E010", first="Nia", last="Owens")
    for offset in range(5):
        _work(db, employee, MONDAY + dt.timedelta(days=offset), 13)

    lines = to_master_csv(
        summarise(build_timesheet(MONDAY, dt.date(2026, 1, 11), LONDON))
    ).strip().split("\r\n")

    assert "Standard hours" in lines[0]
    assert "Overtime hours" in lines[0]
    assert len(lines) == 2
    assert "E010" in lines[1]
    assert "40.00,25.00" in lines[1]  # standard then overtime


def test_weekly_csv_has_one_row_per_person_per_week(db):
    _pattern(db)
    _week(db, 40.0, default=True)
    employee = make_employee(db, ref="E010", last="Owens")
    for offset in range(5):
        _work(db, employee, MONDAY + dt.timedelta(days=offset), 10)  # 50 hours
    next_monday = MONDAY + dt.timedelta(days=7)
    for offset in range(5):
        _work(db, employee, next_monday + dt.timedelta(days=offset), 8)  # 40 hours

    lines = to_weekly_csv(
        summarise(build_timesheet(MONDAY, dt.date(2026, 1, 18), LONDON))
    ).strip().split("\r\n")

    assert lines[0].startswith("Week beginning (Mon),Week ending (Sun)")
    assert len(lines) == 3  # header plus two weeks
    assert lines[1].startswith("2026-01-05,2026-01-11")
    assert "50.00,40.00,10.00" in lines[1]  # paid, standard, overtime
    assert lines[2].startswith("2026-01-12,2026-01-18")
    assert "40.00,40.00,0.00" in lines[2]


def test_weekly_totals_lists_every_employee_week_oldest_first(db):
    _pattern(db)
    _week(db, 40.0, default=True)
    first = make_employee(db, ref="E100", last="Adams")
    second = make_employee(db, ref="E101", last="Brook")
    _work(db, first, MONDAY, 8)
    _work(db, second, MONDAY, 8)
    _work(db, first, MONDAY + dt.timedelta(days=7), 8)

    rows = weekly_totals(summarise(build_timesheet(MONDAY, dt.date(2026, 1, 18), LONDON)))

    assert [(r.start, r.employee.last_name) for r in rows] == [
        (MONDAY, "Adams"),
        (MONDAY, "Brook"),
        (MONDAY + dt.timedelta(days=7), "Adams"),
    ]
