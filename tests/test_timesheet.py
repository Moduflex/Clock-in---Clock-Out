"""Pairing events into shifts, totals, CSV, and the BST/GMT boundary."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.models import DIRECTION_IN, DIRECTION_OUT, AttendanceEvent, ShiftPattern
from app.services.timesheet import (
    build_timesheet,
    local_day_bounds,
    pair_events,
    round_back,
    round_forward,
    summarise,
    to_csv,
    to_local,
)

from .conftest import make_employee

LONDON = ZoneInfo("Europe/London")


def _event(employee, direction, moment, voided=False):
    return AttendanceEvent(
        employee_id=employee.id,
        direction=direction,
        occurred_at=moment,
        is_voided=voided,
    )


def _utc(year, month, day, hour, minute=0):
    return dt.datetime(year, month, day, hour, minute)


# --- pairing ------------------------------------------------------------------
def test_complete_shift_gives_hours(db):
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0)),
    ]
    shifts = pair_events(employee, events, LONDON)

    assert len(shifts) == 1
    assert shifts[0].is_complete
    assert shifts[0].hours == pytest.approx(8.5)
    assert shifts[0].issue is None


def test_missing_clock_out_is_flagged_not_guessed(db):
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
        _event(employee, DIRECTION_IN, _utc(2026, 1, 13, 7, 30)),
    ]
    shifts = pair_events(employee, events, LONDON)

    assert len(shifts) == 2
    assert shifts[0].issue == "No clock-out recorded"
    assert shifts[0].hours is None  # never invented
    assert shifts[1].issue == "Still clocked in"


def test_orphan_clock_out_is_reported(db):
    employee = make_employee(db)
    shifts = pair_events(
        employee, [_event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0))], LONDON
    )
    assert shifts[0].issue == "No clock-in recorded"
    assert shifts[0].hours is None


def test_voided_events_are_skipped(db):
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 0), voided=True),
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0)),
    ]
    shifts = pair_events(employee, events, LONDON)
    assert len(shifts) == 1
    assert shifts[0].hours == pytest.approx(8.5)


def test_night_shift_credited_to_the_starting_day(db):
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 22, 0)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 13, 6, 0)),
    ]
    shifts = pair_events(employee, events, LONDON)
    assert len(shifts) == 1
    assert shifts[0].date == dt.date(2026, 1, 12)
    assert shifts[0].hours == pytest.approx(8.0)


# --- timezone handling --------------------------------------------------------
def test_utc_is_converted_to_british_summer_time():
    """A 07:30 UTC arrival in July is 08:30 on the shop floor."""
    local = to_local(_utc(2026, 7, 1, 7, 30), LONDON)
    assert local.strftime("%H:%M") == "08:30"
    # In winter, UTC and local agree.
    assert to_local(_utc(2026, 1, 1, 7, 30), LONDON).strftime("%H:%M") == "07:30"


def test_local_day_bounds_follow_the_clock_change():
    """A summer local day starts at 23:00 UTC the previous evening."""
    start, end = local_day_bounds(dt.date(2026, 7, 1), LONDON)
    assert start == dt.datetime(2026, 6, 30, 23, 0)
    assert end == dt.datetime(2026, 7, 1, 23, 0)

    winter_start, winter_end = local_day_bounds(dt.date(2026, 1, 15), LONDON)
    assert winter_start == dt.datetime(2026, 1, 15, 0, 0)
    assert winter_end == dt.datetime(2026, 1, 16, 0, 0)


def test_timesheet_query_respects_local_day_boundaries(db):
    """An 08:00 BST clock-in must appear on that local day, not the day before."""
    employee = make_employee(db)
    db.session.add_all(
        [
            _event(employee, DIRECTION_IN, _utc(2026, 7, 1, 7, 0)),  # 08:00 local
            _event(employee, DIRECTION_OUT, _utc(2026, 7, 1, 15, 0)),  # 16:00 local
        ]
    )
    db.session.commit()

    shifts = build_timesheet(dt.date(2026, 7, 1), dt.date(2026, 7, 1), LONDON)
    assert len(shifts) == 1
    assert shifts[0].start_local.strftime("%H:%M") == "08:00"
    assert shifts[0].hours == pytest.approx(8.0)

    # The previous day must be empty.
    assert build_timesheet(dt.date(2026, 6, 30), dt.date(2026, 6, 30), LONDON) == []


def test_unknown_timezone_falls_back_to_utc():
    from app.services.timesheet import get_timezone

    assert str(get_timezone("Mars/Olympus_Mons")) == "UTC"


# --- paid hours -----------------------------------------------------------------
def _pattern(**kwargs) -> ShiftPattern:
    """The default 07:30-16:00 day shift with a 30-minute unpaid lunch."""
    defaults = dict(
        name="Standard day",
        start_time=dt.time(7, 30),
        end_time=dt.time(16, 0),
        unpaid_break_minutes=30,
        break_applies_after_minutes=360,
    )
    defaults.update(kwargs)
    return ShiftPattern(**defaults)


def test_rounding_snaps_to_the_15_minute_grid():
    assert round_forward(dt.datetime(2026, 1, 12, 7, 34, tzinfo=LONDON)).strftime("%H:%M") == "07:45"
    assert round_back(dt.datetime(2026, 1, 12, 16, 7, tzinfo=LONDON)).strftime("%H:%M") == "16:00"
    on_grid = dt.datetime(2026, 1, 12, 7, 45, tzinfo=LONDON)
    assert round_forward(on_grid) == on_grid
    assert round_back(on_grid) == on_grid


def test_early_clock_in_is_paid_from_shift_start(db):
    """In at 07:02 for a 07:30 shift: paid from 07:30, lunch deducted."""
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 2)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0)),
    ]
    shifts = pair_events(employee, events, LONDON, pattern=_pattern())

    assert shifts[0].paid_start_local.strftime("%H:%M") == "07:30"
    assert shifts[0].hours == pytest.approx(8.97)  # actual time is untouched
    assert shifts[0].paid_hours == pytest.approx(8.0)  # 8.5h band minus lunch


def test_late_clock_in_is_paid_from_the_next_quarter_hour(db):
    """The 07:34 arrival from the spec is paid from 07:45."""
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 34)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0)),
    ]
    shifts = pair_events(employee, events, LONDON, pattern=_pattern())

    assert shifts[0].paid_start_local.strftime("%H:%M") == "07:45"
    assert shifts[0].paid_hours == pytest.approx(7.75)  # 8.25h minus lunch


def test_late_finish_is_paid_past_the_shift_end(db):
    """Staying on is paid by default, snapped back to the grid: 16:23 -> 16:15."""
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 23)),
    ]
    shifts = pair_events(employee, events, LONDON, pattern=_pattern())

    assert shifts[0].paid_end_local.strftime("%H:%M") == "16:15"
    assert shifts[0].paid_hours == pytest.approx(8.25)  # 8.75h minus lunch


def test_late_finish_is_trimmed_when_the_shift_says_not_to_pay_it(db):
    """With pay_beyond_end off, pay stops at the shift end as it used to."""
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 23)),
    ]
    shifts = pair_events(
        employee, events, LONDON, pattern=_pattern(pay_beyond_end=False)
    )

    assert shifts[0].paid_end_local.strftime("%H:%M") == "16:00"
    assert shifts[0].paid_hours == pytest.approx(8.0)


def test_an_unsaved_pattern_still_pays_past_the_shift_end(db):
    """The default must hold on an object that has never been flushed.

    A column default only lands on INSERT, so without a Python-side default a
    pattern built in code would silently read as "do not pay past the end" and
    quietly underpay somebody.
    """
    assert ShiftPattern(
        name="Nights", start_time=dt.time(20, 0), end_time=dt.time(4, 0)
    ).pay_beyond_end is True


def test_early_arrival_is_never_paid_before_the_shift_start(db):
    """The other end of the band still holds: waiting about is not overtime."""
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 5, 0)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 18, 0)),
    ]
    shifts = pair_events(employee, events, LONDON, pattern=_pattern())

    assert shifts[0].paid_start_local.strftime("%H:%M") == "07:30"
    assert shifts[0].paid_end_local.strftime("%H:%M") == "18:00"
    assert shifts[0].paid_hours == pytest.approx(10.0)  # 10.5h minus lunch


def test_early_clock_out_rounds_back_to_the_grid(db):
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 15, 20)),
    ]
    shifts = pair_events(employee, events, LONDON, pattern=_pattern())

    assert shifts[0].paid_end_local.strftime("%H:%M") == "15:15"
    assert shifts[0].paid_hours == pytest.approx(7.25)  # 7.75h minus lunch


def test_without_a_pattern_pay_still_uses_the_grid_but_no_lunch(db):
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 34)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 7)),
    ]
    shifts = pair_events(employee, events, LONDON)

    assert shifts[0].paid_start_local.strftime("%H:%M") == "07:45"
    assert shifts[0].paid_end_local.strftime("%H:%M") == "16:00"
    assert shifts[0].paid_hours == pytest.approx(8.25)


def test_short_attendance_keeps_its_break(db):
    """A 2.5-hour afternoon stint contains no lunch, so nothing is deducted."""
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 12, 45)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 15, 15)),
    ]
    shifts = pair_events(employee, events, LONDON, pattern=_pattern())
    assert shifts[0].paid_hours == pytest.approx(2.5)  # not 2.0


def test_break_deducted_once_paid_time_exceeds_the_threshold(db):
    """Just over six paid hours: the lunch comes off as usual."""
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 13, 45)),
    ]
    shifts = pair_events(employee, events, LONDON, pattern=_pattern())
    assert shifts[0].paid_hours == pytest.approx(5.75)  # 6.25h paid minus lunch


def test_break_threshold_of_zero_always_deducts(db):
    pattern = _pattern(break_applies_after_minutes=0)
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 12, 45)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 15, 15)),
    ]
    shifts = pair_events(employee, events, LONDON, pattern=pattern)
    assert shifts[0].paid_hours == pytest.approx(2.0)


def test_paid_hours_never_go_negative(db):
    """A five-minute visit with a 30-minute lunch deduction floors at zero."""
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 8, 0)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 8, 5)),
    ]
    shifts = pair_events(employee, events, LONDON, pattern=_pattern())
    assert shifts[0].paid_hours == 0.0


def test_overnight_pattern_spans_midnight(db):
    employee = make_employee(db)
    night = _pattern(
        name="Nights", start_time=dt.time(22, 0), end_time=dt.time(6, 0),
        unpaid_break_minutes=0,
    )
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 21, 50)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 13, 6, 10)),
    ]
    shifts = pair_events(employee, events, LONDON, pattern=night)

    assert shifts[0].paid_start_local.strftime("%H:%M") == "22:00"
    assert shifts[0].paid_end_local.strftime("%H:%M") == "06:00"
    assert shifts[0].paid_hours == pytest.approx(8.0)


def test_build_timesheet_applies_default_and_assigned_patterns(db):
    default = _pattern(name="Days", is_default=True)
    earlies = _pattern(
        name="Earlies", start_time=dt.time(6, 0), end_time=dt.time(14, 0),
        unpaid_break_minutes=0,
    )
    db.session.add_all([default, earlies])
    db.session.commit()

    alice = make_employee(db)  # no assignment -> default pattern
    bob = make_employee(db, ref="E002", first="Bob", last="Ward")
    bob.shift_pattern_id = earlies.id
    db.session.add_all(
        [
            _event(alice, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
            _event(alice, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0)),
            _event(bob, DIRECTION_IN, _utc(2026, 1, 12, 5, 40)),
            _event(bob, DIRECTION_OUT, _utc(2026, 1, 12, 14, 0)),
        ]
    )
    db.session.commit()

    shifts = build_timesheet(dt.date(2026, 1, 12), dt.date(2026, 1, 12), LONDON)
    by_name = {s.employee.first_name: s for s in shifts}
    assert by_name["Alice"].pattern.name == "Days"
    assert by_name["Alice"].paid_hours == pytest.approx(8.0)
    assert by_name["Bob"].pattern.name == "Earlies"
    assert by_name["Bob"].paid_start_local.strftime("%H:%M") == "06:00"
    assert by_name["Bob"].paid_hours == pytest.approx(8.0)


def test_missing_clock_out_on_a_finished_shift_is_paid_to_shift_end(db):
    """Forgot to scan out: pay to the shift end and say so on the row."""
    employee = make_employee(db)
    events = [_event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30))]
    shifts = pair_events(employee, events, LONDON, pattern=_pattern())

    assert shifts[0].end_is_assumed
    assert shifts[0].end_local is None  # the record itself is not invented
    assert shifts[0].hours is None  # actual hours stay blank
    assert shifts[0].paid_end_local.strftime("%H:%M") == "16:00"
    assert shifts[0].paid_hours == pytest.approx(8.0)
    assert shifts[0].display_issue == (
        "Did not clock out - assumed finished at their normal time (16:00)"
    )


def test_no_assumption_while_the_shift_is_still_running(db):
    """A clock-in on a shift that has not ended yet is genuinely still open."""
    employee = make_employee(db)
    events = [_event(employee, DIRECTION_IN, _utc(2030, 1, 14, 7, 30))]
    shifts = pair_events(employee, events, LONDON, pattern=_pattern())

    assert not shifts[0].end_is_assumed
    assert shifts[0].paid_hours is None
    assert shifts[0].display_issue == "Still clocked in"


def test_no_assumption_without_a_shift_pattern(db):
    """With no shift there is no end time to assume, so nothing is invented."""
    employee = make_employee(db)
    events = [_event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30))]
    shifts = pair_events(employee, events, LONDON)

    assert not shifts[0].end_is_assumed
    assert shifts[0].paid_hours is None


def test_orphan_clock_out_is_never_assumed_a_start(db):
    employee = make_employee(db)
    shifts = pair_events(
        employee,
        [_event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0))],
        LONDON,
        pattern=_pattern(),
    )
    assert shifts[0].paid_hours is None
    assert shifts[0].display_issue == "No clock-in recorded"


def test_build_timesheet_filters_by_department(db):
    assembly = make_employee(db, department="Assembly")
    joinery = make_employee(db, ref="E002", first="Bob", last="Ward", department="Joinery")
    db.session.add_all(
        [
            _event(assembly, DIRECTION_IN, _utc(2026, 1, 12, 8, 0)),
            _event(assembly, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0)),
            _event(joinery, DIRECTION_IN, _utc(2026, 1, 12, 8, 0)),
            _event(joinery, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0)),
        ]
    )
    db.session.commit()

    shifts = build_timesheet(
        dt.date(2026, 1, 12), dt.date(2026, 1, 12), LONDON, department="Assembly"
    )
    assert {s.employee.department for s in shifts} == {"Assembly"}


# --- the hidden record ----------------------------------------------------------
def test_claude_never_appears_in_any_report(db):
    """The joke record is filtered out of timesheets, filters and the fire register.

    A fictional name reaching a payroll export would be a real problem, so this
    is pinned by a test rather than left to whoever edits the queries next.
    """
    from app.services.attendance import currently_on_site
    from app.services.timesheet import list_departments

    real = make_employee(db, department="Assembly")
    claude = make_employee(
        db, ref="CLAUDE", first="Claude", last="AI", department="Anthropic"
    )
    db.session.add_all(
        [
            _event(real, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
            _event(real, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0)),
            _event(claude, DIRECTION_IN, _utc(2026, 1, 12, 0, 0)),  # never clocked out
        ]
    )
    db.session.commit()

    shifts = build_timesheet(dt.date(2026, 1, 12), dt.date(2026, 1, 12), LONDON)
    assert [s.employee.first_name for s in shifts] == ["Alice"]
    assert "Claude" not in to_csv(shifts)
    assert "Anthropic" not in list_departments()
    assert claude not in currently_on_site()

    # Even asking for Claude by name gets nothing back.
    assert build_timesheet(
        dt.date(2026, 1, 12), dt.date(2026, 1, 12), LONDON, employee_id=claude.id
    ) == []


# --- summaries and export -----------------------------------------------------
def test_summarise_totals_hours_and_counts_issues(db):
    employee = make_employee(db)
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 0)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 15, 0)),
        _event(employee, DIRECTION_IN, _utc(2026, 1, 13, 7, 0)),  # never clocked out
    ]
    totals = summarise(pair_events(employee, events, LONDON))

    assert len(totals) == 1
    assert totals[0].hours == pytest.approx(8.0)
    assert totals[0].shifts == 1
    assert totals[0].issues == 1


def test_csv_has_a_header_and_one_row_per_shift(db):
    employee = make_employee(db, ref="E010", first="Nia", last="Owens")
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0)),
    ]
    csv_text = to_csv(pair_events(employee, events, LONDON))
    lines = csv_text.strip().split("\r\n")

    assert lines[0].startswith("Payroll ref,Surname,First name")
    assert len(lines) == 2
    assert "E010" in lines[1]
    assert "Owens" in lines[1]
    assert "07:30" in lines[1]
    assert "8.50" in lines[1]


def test_csv_leaves_hours_blank_for_an_unpaired_shift(db):
    employee = make_employee(db)
    events = [_event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 30))]
    row = to_csv(pair_events(employee, events, LONDON)).strip().split("\r\n")[1]
    assert row.endswith("Still clocked in")
    assert ",," in row  # blank clock-out and blank hours


def test_master_csv_has_one_line_per_person_with_paid_totals(db):
    from app.services.timesheet import to_master_csv

    employee = make_employee(db, ref="E010", first="Nia", last="Owens")
    events = [
        _event(employee, DIRECTION_IN, _utc(2026, 1, 12, 7, 34)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 12, 16, 0)),
        _event(employee, DIRECTION_IN, _utc(2026, 1, 13, 7, 30)),
        _event(employee, DIRECTION_OUT, _utc(2026, 1, 13, 16, 0)),
        _event(employee, DIRECTION_IN, _utc(2026, 1, 14, 7, 30)),  # never clocked out
    ]
    totals = summarise(pair_events(employee, events, LONDON, pattern=_pattern()))
    lines = to_master_csv(totals).strip().split("\r\n")

    assert lines[0].startswith("Payroll ref,Surname,First name,Department")
    assert len(lines) == 2  # one person, one line
    assert "E010" in lines[1]
    assert "23.75" in lines[1]  # 7.75 + 8.00 + 8.00 (assumed) paid hours
    # The attention column names the exact shift and what is wrong with it.
    assert "Wed 14/01: Did not clock out - assumed finished at their normal time (16:00)" in lines[1]
