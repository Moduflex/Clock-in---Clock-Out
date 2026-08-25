"""Clock-in / clock-out rules: alternation, cooldown, voiding and absence."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from app.models import (
    DIRECTION_IN,
    DIRECTION_OUT,
    AttendanceEvent,
    ShiftPattern,
    utcnow,
)
from app.services import attendance

from .conftest import make_employee

LONDON = ZoneInfo("Europe/London")


def test_first_scan_clocks_in(db):
    employee = make_employee(db)
    assert attendance.next_direction(employee.id) == DIRECTION_IN

    result = attendance.record_clock(employee, cooldown_seconds=0)
    assert result.recorded
    assert result.direction == DIRECTION_IN
    assert "Clocked in" in result.message


def test_directions_alternate(db):
    employee = make_employee(db)
    attendance.record_clock(employee, cooldown_seconds=0)
    assert attendance.next_direction(employee.id) == DIRECTION_OUT

    second = attendance.record_clock(employee, cooldown_seconds=0)
    assert second.direction == DIRECTION_OUT
    assert attendance.next_direction(employee.id) == DIRECTION_IN


def test_cooldown_suppresses_a_repeat_scan(db):
    employee = make_employee(db)
    first = attendance.record_clock(employee, cooldown_seconds=90)
    assert first.recorded

    # Same direction moments later: reported back, not written again.
    repeat = attendance.record_clock(
        employee, direction=DIRECTION_IN, cooldown_seconds=90
    )
    assert not repeat.recorded
    assert repeat.duplicate_of is not None
    assert repeat.duplicate_of.id == first.event.id
    assert "Already clocked in" in repeat.message
    assert db.session.query(AttendanceEvent).count() == 1


def test_cooldown_does_not_block_the_opposite_direction(db):
    """Somebody who arrives and immediately leaves must still be able to clock out."""
    employee = make_employee(db)
    attendance.record_clock(employee, cooldown_seconds=90)
    out = attendance.record_clock(employee, direction=DIRECTION_OUT, cooldown_seconds=90)
    assert out.recorded
    assert out.direction == DIRECTION_OUT


def test_cooldown_expires(db):
    employee = make_employee(db)
    old = utcnow() - dt.timedelta(seconds=200)
    attendance.record_clock(
        employee, direction=DIRECTION_IN, occurred_at=old, cooldown_seconds=90
    )
    again = attendance.record_clock(
        employee, direction=DIRECTION_IN, cooldown_seconds=90
    )
    assert again.recorded


def test_is_clocked_in_tracks_state(db):
    employee = make_employee(db)
    assert not attendance.is_clocked_in(employee.id)
    attendance.record_clock(employee, cooldown_seconds=0)
    assert attendance.is_clocked_in(employee.id)
    attendance.record_clock(employee, cooldown_seconds=0)
    assert not attendance.is_clocked_in(employee.id)


def test_voided_event_is_ignored_by_state(db, admin):
    employee = make_employee(db)
    result = attendance.record_clock(employee, cooldown_seconds=0)
    attendance.void_event(result.event, admin_id=admin.id, reason="Scanned by mistake")

    assert not attendance.is_clocked_in(employee.id)
    assert attendance.next_direction(employee.id) == DIRECTION_IN
    # The row itself survives for the audit trail.
    assert db.session.query(AttendanceEvent).count() == 1
    assert "Scanned by mistake" in result.event.note


def test_currently_on_site_lists_only_clocked_in_people(db):
    alice = make_employee(db, ref="E001", first="Alice")
    bob = make_employee(db, ref="E002", first="Bob")
    make_employee(db, ref="E003", first="Carol", is_active=False)

    attendance.record_clock(alice, cooldown_seconds=0)
    attendance.record_clock(bob, cooldown_seconds=0)
    attendance.record_clock(bob, cooldown_seconds=0)  # Bob clocks out again

    on_site = attendance.currently_on_site()
    assert [person.first_name for person in on_site] == ["Alice"]


def test_invalid_direction_is_rejected(db):
    employee = make_employee(db)
    try:
        attendance.record_clock(employee, direction="sideways")
    except ValueError as exc:
        assert "direction must be one of" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("An invalid direction should raise ValueError")


def test_lingering_at_the_kiosk_does_not_clock_you_back_out(db):
    """An automatic scan inside the cooldown must not alternate.

    Regression test: next_direction always returns the opposite of the last
    entry, so a cooldown that only compared directions never fired on the
    automatic path - two scans in a row clocked the person in and straight out.
    """
    employee = make_employee(db)
    first = attendance.record_clock(employee, cooldown_seconds=90)
    assert first.recorded and first.direction == DIRECTION_IN

    second = attendance.record_clock(employee, cooldown_seconds=90)
    assert not second.recorded
    assert second.direction == DIRECTION_IN  # reports the state they are in
    assert second.duplicate_of.id == first.event.id
    assert "Already clocked in" in second.message
    assert db.session.query(AttendanceEvent).count() == 1


def test_automatic_scan_alternates_once_the_cooldown_expires(db):
    employee = make_employee(db)
    attendance.record_clock(
        employee, occurred_at=utcnow() - dt.timedelta(seconds=200), cooldown_seconds=90
    )
    out = attendance.record_clock(employee, cooldown_seconds=90)
    assert out.recorded
    assert out.direction == DIRECTION_OUT


def test_explicit_clock_out_still_works_inside_the_cooldown(db):
    """Pressing Clock out states intent, so it is honoured immediately."""
    employee = make_employee(db)
    attendance.record_clock(employee, cooldown_seconds=90)
    out = attendance.record_clock(employee, direction=DIRECTION_OUT, cooldown_seconds=90)
    assert out.recorded
    assert out.direction == DIRECTION_OUT
    assert db.session.query(AttendanceEvent).count() == 2


# --- absence ------------------------------------------------------------------
# 12 January 2026 is a Monday in GMT, so local time and UTC coincide; the BST
# case is covered separately below.
DAY = dt.date(2026, 1, 12)


def _day_shift(db, **kwargs) -> ShiftPattern:
    """A 07:30-16:00 default shift, so everybody has an expected start."""
    defaults = dict(
        name="Standard day",
        start_time=dt.time(7, 30),
        end_time=dt.time(16, 0),
        unpaid_break_minutes=30,
        is_default=True,
    )
    defaults.update(kwargs)
    pattern = ShiftPattern(**defaults)
    db.session.add(pattern)
    db.session.commit()
    return pattern


def _clock(db, employee, direction, hour, minute=0, day=DAY):
    db.session.add(
        AttendanceEvent(
            employee_id=employee.id,
            direction=direction,
            occurred_at=dt.datetime.combine(day, dt.time(hour, minute)),
        )
    )
    db.session.commit()


def _by_name(records) -> dict[str, attendance.DayPresence]:
    return {record.employee.first_name: record for record in records}


def test_daily_presence_sorts_people_into_three_states(db):
    _day_shift(db)
    alice = make_employee(db, ref="E001", first="Alice")
    bob = make_employee(db, ref="E002", first="Bob")
    make_employee(db, ref="E003", first="Carol")
    make_employee(db, ref="E004", first="Dan", is_active=False)

    _clock(db, alice, DIRECTION_IN, 7, 28)
    _clock(db, bob, DIRECTION_IN, 7, 31)
    _clock(db, bob, DIRECTION_OUT, 12, 0)

    records = _by_name(
        attendance.daily_presence(DAY, LONDON, now=dt.datetime(2026, 1, 12, 13, 0))
    )

    assert records["Alice"].status == attendance.PRESENCE_ON_SITE
    assert records["Alice"].first_in == dt.datetime(2026, 1, 12, 7, 28)
    assert records["Bob"].status == attendance.PRESENCE_LEFT
    assert records["Bob"].last_out == dt.datetime(2026, 1, 12, 12, 0)
    assert records["Carol"].is_absent
    # An inactive employee is a leaver, not an absentee.
    assert "Dan" not in records


def test_an_absentee_is_overdue_only_once_the_shift_has_started(db):
    _day_shift(db)
    make_employee(db, ref="E001", first="Alice")

    early = attendance.daily_presence(
        DAY, LONDON, now=dt.datetime(2026, 1, 12, 6, 0)
    )[0]
    assert early.is_absent
    assert not early.is_due
    assert early.overdue_minutes is None

    late = attendance.daily_presence(
        DAY, LONDON, now=dt.datetime(2026, 1, 12, 8, 42)
    )[0]
    assert late.is_due
    assert late.overdue_minutes == 72
    assert late.overdue_text == "1h 12m"


def test_someone_with_no_shift_pattern_counts_as_due(db):
    """With nothing to wait for, a missing clock-in is worth showing straight away."""
    make_employee(db, ref="E001", first="Alice")

    record = attendance.daily_presence(
        DAY, LONDON, now=dt.datetime(2026, 1, 12, 3, 0)
    )[0]
    assert record.is_absent
    assert record.is_due
    assert record.expected_start is None
    assert record.overdue_text == ""


def test_a_night_shift_from_yesterday_is_on_site_not_absent(db):
    """Clocked in at 22:00 yesterday and still in: a person in the building."""
    alice = make_employee(db, ref="E001", first="Alice")
    _clock(db, alice, DIRECTION_IN, 22, 0, day=DAY - dt.timedelta(days=1))

    record = attendance.daily_presence(
        DAY, LONDON, now=dt.datetime(2026, 1, 12, 2, 0)
    )[0]
    assert record.status == attendance.PRESENCE_ON_SITE
    assert record.first_in is None  # nothing recorded on this day itself
    assert record.last_seen == dt.datetime(2026, 1, 11, 22, 0)


def test_a_voided_clock_in_leaves_the_person_absent(db):
    alice = make_employee(db, ref="E001", first="Alice")
    _clock(db, alice, DIRECTION_IN, 7, 30)
    event = db.session.query(AttendanceEvent).one()
    event.is_voided = True
    db.session.commit()

    record = attendance.daily_presence(
        DAY, LONDON, now=dt.datetime(2026, 1, 12, 9, 0)
    )[0]
    assert record.is_absent
    assert record.last_seen is None


def test_a_past_day_is_judged_at_its_own_midnight(db):
    """Yesterday's absentee is overdue by the shift-to-midnight gap, not by weeks."""
    _day_shift(db)
    make_employee(db, ref="E001", first="Alice")

    record = attendance.daily_presence(
        DAY, LONDON, now=dt.datetime(2026, 2, 1, 9, 0)
    )[0]
    assert record.is_absent
    # 07:30 to midnight, and no further.
    assert record.overdue_minutes == 16 * 60 + 30


def test_expected_start_follows_local_time_across_the_bst_change(db):
    """A 07:30 start is 07:30 on the shop floor, so 06:30 UTC in summer."""
    _day_shift(db)
    make_employee(db, ref="E001", first="Alice")

    summer = dt.date(2026, 7, 6)
    record = attendance.daily_presence(
        summer, LONDON, now=dt.datetime(2026, 7, 6, 9, 0)
    )[0]
    assert record.expected_start == dt.datetime(2026, 7, 6, 6, 30)


def test_presence_can_be_filtered_by_department(db):
    make_employee(db, ref="E001", first="Alice", department="Workshop")
    make_employee(db, ref="E002", first="Bob", department="Office")

    records = attendance.daily_presence(
        DAY, LONDON, now=dt.datetime(2026, 1, 12, 9, 0), department="Workshop"
    )
    assert [r.employee.first_name for r in records] == ["Alice"]
