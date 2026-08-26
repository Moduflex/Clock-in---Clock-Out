"""Turning the raw event log into shifts, totals and a payroll CSV.

Timestamps are stored in UTC and converted to local time only for display and
reporting, so the twice-yearly BST/GMT change cannot corrupt stored data. A
shift is credited to the local date it *started*, which keeps a night shift
crossing midnight on one line instead of split across two days.

Unpaired events are always flagged. If somebody forgot to clock out and they are
on a shift whose end has already passed, they are assumed to have left at the
shift end - the paid figure uses that and the row says so, so payroll is not
held up by one forgotten scan. Without a shift (or while the shift is still
running) nothing is invented and the hours are left blank for a human to settle.

Paid hours are derived from actual hours by three rules, applied in this order:
the worked period is clipped to the employee's shift band (clock in early, paid
from the shift start; clock out late, paid to the shift end *only* on a shift
with ``pay_beyond_end`` turned off), the clipped times are snapped to the
15-minute pay grid (in rounds forward, out rounds back, so 07:34 is paid from
07:45), and the shift's unpaid break is deducted - but only when the paid time
is long enough to have contained the break. Actual times are always shown
alongside so nothing is hidden from whoever runs payroll.

Paid hours are then split into standard and overtime, week by week. A week runs
Monday to Sunday (``week_start``), which is the only sound basis for the split:
overtime is earned against a contracted weekly figure, so a reporting period of
four weeks must be settled as four separate weeks and not as one 160-hour lump.
Each week's paid hours up to the employee's standard week count as standard and
the remainder as overtime, so 65 paid hours on a 40-hour contract are 40
standard and 25 overtime. Somebody with no standard week set has all their hours
counted as standard - a missing contract must not silently create overtime.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..extensions import db
from ..models import (
    DIRECTION_IN,
    DIRECTION_OUT,
    AttendanceEvent,
    Employee,
    ShiftPattern,
    WorkingWeek,
    visible_employee_clause,
)

PAY_INTERVAL = dt.timedelta(minutes=15)
# Weeks run Monday to Sunday throughout: date.weekday() numbers Monday 0.
WEEK_LENGTH = 7


def get_timezone(name: str) -> ZoneInfo:
    """Load a timezone, falling back to UTC if the name is unknown."""
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - bad config should not take the app down
        return ZoneInfo("UTC")


def to_local(moment: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """Interpret a naive UTC timestamp and return it in *tz*."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(tz)


def local_day_bounds(day: dt.date, tz: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    """Naive UTC start/end covering one local calendar day."""
    start_local = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    end_local = start_local + dt.timedelta(days=1)
    return (
        start_local.astimezone(dt.timezone.utc).replace(tzinfo=None),
        end_local.astimezone(dt.timezone.utc).replace(tzinfo=None),
    )


def local_range_bounds(
    start: dt.date, end: dt.date, tz: ZoneInfo
) -> tuple[dt.datetime, dt.datetime]:
    """Naive UTC bounds covering local days *start* to *end* inclusive."""
    first, _ = local_day_bounds(start, tz)
    _, last = local_day_bounds(end, tz)
    return first, last


def week_start(day: dt.date) -> dt.date:
    """The Monday of the week containing *day*. Every week here is Monday-Sunday."""
    return day - dt.timedelta(days=day.weekday())


def week_end(day: dt.date) -> dt.date:
    """The Sunday of the week containing *day*."""
    return week_start(day) + dt.timedelta(days=WEEK_LENGTH - 1)


def whole_weeks(start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
    """Widen a date range outwards to whole Monday-Sunday weeks."""
    return week_start(start), week_end(end)


def is_whole_weeks(start: dt.date, end: dt.date) -> bool:
    """True when the range starts on a Monday and ends on a Sunday.

    Worth checking before trusting an overtime figure: half a week compared
    against a full week's contract understates the overtime earned in it.
    """
    return start.weekday() == 0 and end.weekday() == WEEK_LENGTH - 1


def default_range(today: dt.date, weeks: int = 4) -> tuple[dt.date, dt.date]:
    """The last *weeks* whole Monday-Sunday weeks, ending with this week."""
    end = week_end(today)
    return end - dt.timedelta(days=weeks * WEEK_LENGTH - 1), end


def pay_range(today: dt.date, weeks: int = 4) -> tuple[dt.date, dt.date]:
    """The *weeks* whole weeks up to last Sunday, leaving the current week out.

    This is the four-weekly wage period: the week in progress is not paid until
    it has finished, so a run made today covers the four completed weeks behind
    it. Counting back from today that spans five calendar weeks.
    """
    end = week_start(today) - dt.timedelta(days=1)  # last Sunday
    return end - dt.timedelta(days=weeks * WEEK_LENGTH - 1), end


def round_forward(moment: dt.datetime) -> dt.datetime:
    """Snap forward to the next pay-grid boundary (07:34 -> 07:45)."""
    anchor = moment.replace(minute=0, second=0, microsecond=0)
    intervals = -((anchor - moment) // PAY_INTERVAL)  # ceiling division
    return anchor + intervals * PAY_INTERVAL


def round_back(moment: dt.datetime) -> dt.datetime:
    """Snap back to the previous pay-grid boundary (16:07 -> 16:00)."""
    anchor = moment.replace(minute=0, second=0, microsecond=0)
    return anchor + ((moment - anchor) // PAY_INTERVAL) * PAY_INTERVAL


def get_default_pattern() -> ShiftPattern | None:
    return db.session.scalars(
        select(ShiftPattern).where(ShiftPattern.is_default.is_(True))
    ).first()


def get_default_working_week() -> WorkingWeek | None:
    """The standard week used by anyone not assigned one of their own."""
    return db.session.scalars(
        select(WorkingWeek).where(WorkingWeek.is_default.is_(True))
    ).first()


def standard_weekly_hours(
    employee: Employee, default: WorkingWeek | None
) -> float | None:
    """The employee's contracted weekly hours, or None when none is set."""
    week = employee.working_week or default
    return float(week.hours) if week is not None else None


def split_overtime(
    paid_hours: float, standard: float | None
) -> tuple[float, float]:
    """Split one week's paid hours into (standard, overtime).

    With no contracted week to measure against, everything counts as standard:
    inventing overtime from a missing setting would inflate a wage bill.
    """
    if standard is None:
        return round(paid_hours, 2), 0.0
    worked_standard = min(paid_hours, standard)
    return round(worked_standard, 2), round(max(0.0, paid_hours - standard), 2)


@dataclass
class Shift:
    """One clock-in paired with its clock-out, if there is one."""

    employee: Employee
    clock_in: AttendanceEvent | None
    clock_out: AttendanceEvent | None
    tz: ZoneInfo
    issue: str | None = None
    pattern: ShiftPattern | None = None

    @property
    def start_local(self) -> dt.datetime | None:
        return to_local(self.clock_in.occurred_at, self.tz) if self.clock_in else None

    @property
    def end_local(self) -> dt.datetime | None:
        return to_local(self.clock_out.occurred_at, self.tz) if self.clock_out else None

    @property
    def date(self) -> dt.date | None:
        anchor = self.start_local or self.end_local
        return anchor.date() if anchor else None

    @property
    def week_start(self) -> dt.date | None:
        """The Monday of the week this shift is credited to."""
        day = self.date
        return week_start(day) if day else None

    @property
    def is_complete(self) -> bool:
        return self.clock_in is not None and self.clock_out is not None

    @property
    def duration(self) -> dt.timedelta | None:
        if not self.is_complete:
            return None
        delta = self.clock_out.occurred_at - self.clock_in.occurred_at  # type: ignore[union-attr]
        return delta if delta.total_seconds() >= 0 else None

    @property
    def hours(self) -> float | None:
        duration = self.duration
        return round(duration.total_seconds() / 3600.0, 2) if duration else None

    # --- paid time ----------------------------------------------------------
    def _band(self) -> tuple[dt.datetime, dt.datetime] | None:
        """The paid time band for this shift's local day, or None if no pattern."""
        anchor = self.start_local or self.end_local
        if self.pattern is None or anchor is None:
            return None
        day = anchor.date()
        band_start = dt.datetime.combine(day, self.pattern.start_time, tzinfo=self.tz)
        end_day = day + dt.timedelta(days=1) if self.pattern.crosses_midnight else day
        band_end = dt.datetime.combine(end_day, self.pattern.end_time, tzinfo=self.tz)
        return band_start, band_end

    @property
    def end_is_assumed(self) -> bool:
        """True when a missing clock-out is stood in for by the shift end.

        Only once the shift end has actually passed: somebody still on site
        mid-shift genuinely has no leaving time yet, assumed or otherwise.
        """
        if self.clock_out is not None or self.clock_in is None:
            return False
        band = self._band()
        return band is not None and band[1] <= dt.datetime.now(self.tz)

    @property
    def effective_end_local(self) -> dt.datetime | None:
        """The recorded clock-out, or the shift end when one can be assumed."""
        if self.end_local is not None:
            return self.end_local
        if self.end_is_assumed:
            return self._band()[1]  # type: ignore[index]
        return None

    @property
    def paid_start_local(self) -> dt.datetime | None:
        if self.start_local is None:
            return None
        band = self._band()
        start = max(self.start_local, band[0]) if band else self.start_local
        return round_forward(start)

    @property
    def paid_end_local(self) -> dt.datetime | None:
        """When pay stops.

        Time worked past the shift end is paid by default and shows up as
        overtime. A shift with ``pay_beyond_end`` turned off trims back to the
        band end instead, for work where staying late is not authorised.
        """
        end = self.effective_end_local
        if end is None:
            return None
        band = self._band()
        if band and self.pattern is not None and not self.pattern.pay_beyond_end:
            end = min(end, band[1])
        return round_back(end)

    @property
    def paid_hours(self) -> float | None:
        """Hours to pay: band-clipped, grid-snapped, unpaid break deducted."""
        if self.is_complete and self.duration is None:
            return None  # clock-out precedes clock-in - a correction is needed
        start, end = self.paid_start_local, self.paid_end_local
        if start is None or end is None:
            return None
        seconds = (end - start).total_seconds()
        # The break comes off only when the paid time is long enough to have
        # contained it - a 2.5-hour afternoon stint has no lunch to deduct.
        if (
            self.pattern is not None
            and seconds > self.pattern.break_applies_after_minutes * 60
        ):
            seconds -= self.pattern.unpaid_break_minutes * 60
        return round(max(0.0, seconds) / 3600.0, 2)

    @property
    def display_issue(self) -> str | None:
        """The issue text, noting when the paid figure assumes the shift end."""
        if self.end_is_assumed:
            end = self._band()[1]  # type: ignore[index]
            return (
                "Did not clock out - assumed finished at their normal time "
                f"({end.strftime('%H:%M')})"
            )
        return self.issue


def pair_events(
    employee: Employee,
    events: list[AttendanceEvent],
    tz: ZoneInfo,
    pattern: ShiftPattern | None = None,
) -> list[Shift]:
    """Pair a chronological event list into shifts.

    The log is a plain alternating sequence in the normal case. The two ways it
    breaks are handled explicitly: an IN followed by another IN (forgot to clock
    out) and an OUT with no matching IN (forgot to clock in, or the shift started
    before the reporting window).
    """
    shifts: list[Shift] = []
    open_in: AttendanceEvent | None = None

    for event in events:
        if event.is_voided:
            continue
        if event.direction == DIRECTION_IN:
            if open_in is not None:
                shifts.append(
                    Shift(
                        employee,
                        open_in,
                        None,
                        tz,
                        issue="No clock-out recorded",
                        pattern=pattern,
                    )
                )
            open_in = event
        elif event.direction == DIRECTION_OUT:
            if open_in is None:
                shifts.append(
                    Shift(
                        employee,
                        None,
                        event,
                        tz,
                        issue="No clock-in recorded",
                        pattern=pattern,
                    )
                )
            else:
                shift = Shift(employee, open_in, event, tz, pattern=pattern)
                if shift.duration is None:
                    shift.issue = "Clock-out precedes clock-in"
                shifts.append(shift)
                open_in = None

    if open_in is not None:
        shifts.append(
            Shift(employee, open_in, None, tz, issue="Still clocked in", pattern=pattern)
        )

    return shifts


def build_timesheet(
    start: dt.date,
    end: dt.date,
    tz: ZoneInfo,
    *,
    employee_id: int | None = None,
    department: str | None = None,
    include_inactive: bool = False,
) -> list[Shift]:
    """Build shifts for a local date range, ordered by employee then time."""
    first, last = local_range_bounds(start, end, tz)

    employee_stmt = (
        select(Employee)
        .where(visible_employee_clause())
        .order_by(Employee.last_name, Employee.first_name)
    )
    if employee_id is not None:
        employee_stmt = employee_stmt.where(Employee.id == employee_id)
    else:
        if not include_inactive:
            employee_stmt = employee_stmt.where(Employee.is_active.is_(True))
        if department:
            employee_stmt = employee_stmt.where(Employee.department == department)
    employees = db.session.scalars(employee_stmt).all()

    default_pattern = get_default_pattern()
    shifts: list[Shift] = []
    for employee in employees:
        events = db.session.scalars(
            select(AttendanceEvent)
            .where(
                AttendanceEvent.employee_id == employee.id,
                AttendanceEvent.is_voided.is_(False),
                AttendanceEvent.occurred_at >= first,
                AttendanceEvent.occurred_at < last,
            )
            .order_by(AttendanceEvent.occurred_at, AttendanceEvent.id)
        ).all()
        pattern = employee.shift_pattern or default_pattern
        shifts.extend(pair_events(employee, list(events), tz, pattern=pattern))

    return shifts


def list_departments() -> list[str]:
    """Distinct non-empty department names, for the timesheet filter."""
    rows = db.session.scalars(
        select(Employee.department)
        .where(Employee.department.is_not(None), visible_employee_clause())
        .distinct()
        .order_by(Employee.department)
    ).all()
    return [row for row in rows if row]


@dataclass
class WeekTotal:
    """One employee's Monday-Sunday week - the unit overtime is settled in."""

    employee: Employee
    start: dt.date
    hours: float = 0.0
    paid_hours: float = 0.0
    standard_hours: float = 0.0
    overtime_hours: float = 0.0
    shifts: int = 0
    issues: int = 0

    @property
    def end(self) -> dt.date:
        return self.start + dt.timedelta(days=WEEK_LENGTH - 1)

    @property
    def label(self) -> str:
        return f"{self.start.strftime('%d/%m')}-{self.end.strftime('%d/%m/%Y')}"


@dataclass
class EmployeeTotal:
    employee: Employee
    hours: float
    paid_hours: float
    shifts: int
    issues: int
    issue_details: list[str] = field(default_factory=list)
    # Paid hours split against the contracted week, summed over whole weeks.
    standard_hours: float = 0.0
    overtime_hours: float = 0.0
    # The contract the split was measured against; None when none is set.
    standard_weekly_hours: float | None = None
    weeks: list[WeekTotal] = field(default_factory=list)


def summarise(shifts: list[Shift]) -> list[EmployeeTotal]:
    """Total hours per employee, split into standard and overtime week by week.

    Each detail names the day and what is wrong, so management can spot a
    discrepancy on the master sheet without opening every timesheet.

    The standard/overtime split is worked out on each Monday-Sunday week in turn
    and then added up. Doing it on the period total instead would let a quiet
    week cancel out a busy one, and overtime already worked is not repayable.
    """
    default_week = get_default_working_week()
    buckets: dict[int, EmployeeTotal] = {}
    weeks: dict[tuple[int, dt.date], WeekTotal] = {}

    for shift in shifts:
        total = buckets.get(shift.employee.id)
        if total is None:
            total = EmployeeTotal(shift.employee, 0.0, 0.0, 0, 0)
            total.standard_weekly_hours = standard_weekly_hours(
                shift.employee, default_week
            )
            buckets[shift.employee.id] = total

        monday = shift.week_start
        week = None
        if monday is not None:
            key = (shift.employee.id, monday)
            week = weeks.get(key)
            if week is None:
                week = WeekTotal(shift.employee, monday)
                weeks[key] = week

        if shift.hours is not None:
            total.hours = round(total.hours + shift.hours, 2)
            total.shifts += 1
            if week is not None:
                week.hours = round(week.hours + shift.hours, 2)
                week.shifts += 1
        if shift.paid_hours is not None:
            total.paid_hours = round(total.paid_hours + shift.paid_hours, 2)
            if week is not None:
                week.paid_hours = round(week.paid_hours + shift.paid_hours, 2)
        if shift.issue:
            total.issues += 1
            day = shift.date.strftime("%a %d/%m") if shift.date else "unknown day"
            total.issue_details.append(f"{day}: {shift.display_issue}")
            if week is not None:
                week.issues += 1

    for (employee_id, _), week in sorted(weeks.items(), key=lambda item: item[0]):
        total = buckets[employee_id]
        week.standard_hours, week.overtime_hours = split_overtime(
            week.paid_hours, total.standard_weekly_hours
        )
        total.weeks.append(week)
        total.standard_hours = round(total.standard_hours + week.standard_hours, 2)
        total.overtime_hours = round(total.overtime_hours + week.overtime_hours, 2)

    return sorted(
        buckets.values(), key=lambda t: (t.employee.last_name, t.employee.first_name)
    )


def weekly_totals(totals: list[EmployeeTotal]) -> list[WeekTotal]:
    """Every employee-week in one list, oldest week first then by surname."""
    rows = [week for total in totals for week in total.weeks]
    return sorted(
        rows, key=lambda w: (w.start, w.employee.last_name, w.employee.first_name)
    )


def to_csv(shifts: list[Shift]) -> str:
    """Render shifts as CSV for payroll. Excel-friendly (CRLF, ISO dates)."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(
        [
            "Payroll ref",
            "Surname",
            "First name",
            "Department",
            "Week beginning",
            "Date",
            "Clock in",
            "Clock out",
            "Hours",
            "Shift",
            "Paid from",
            "Paid to",
            "Paid hours",
            "Notes",
        ]
    )
    for shift in shifts:
        start = shift.start_local
        end = shift.end_local
        paid_start = shift.paid_start_local if shift.paid_hours is not None else None
        paid_end = shift.paid_end_local if shift.paid_hours is not None else None
        writer.writerow(
            [
                shift.employee.payroll_ref,
                shift.employee.last_name,
                shift.employee.first_name,
                shift.employee.department or "",
                shift.week_start.isoformat() if shift.week_start else "",
                shift.date.isoformat() if shift.date else "",
                start.strftime("%H:%M") if start else "",
                end.strftime("%H:%M") if end else "",
                f"{shift.hours:.2f}" if shift.hours is not None else "",
                shift.pattern.name if shift.pattern else "",
                paid_start.strftime("%H:%M") if paid_start else "",
                paid_end.strftime("%H:%M") if paid_end else "",
                f"{shift.paid_hours:.2f}" if shift.paid_hours is not None else "",
                shift.display_issue or "",
            ]
        )
    return buffer.getvalue()


def to_master_csv(totals: list[EmployeeTotal]) -> str:
    """One line per person, paid hours split into standard and overtime.

    Deliberately terse: management scan this for anything that looks wrong, then
    open that person's individual timesheet for the day-by-day detail. The split
    is the sum of each Monday-Sunday week's split, so it can be checked against
    the weekly sheet line by line.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(
        [
            "Payroll ref",
            "Surname",
            "First name",
            "Department",
            "Shift",
            "Standard week (hours)",
            "Weeks",
            "Clocked hours",
            "Paid hours",
            "Standard hours",
            "Overtime hours",
            "Rows needing attention",
        ]
    )
    for total in totals:
        writer.writerow(
            [
                total.employee.payroll_ref,
                total.employee.last_name,
                total.employee.first_name,
                total.employee.department or "",
                total.employee.shift_pattern.name if total.employee.shift_pattern else "",
                f"{total.standard_weekly_hours:g}"
                if total.standard_weekly_hours is not None
                else "",
                len(total.weeks),
                f"{total.hours:.2f}",
                f"{total.paid_hours:.2f}",
                f"{total.standard_hours:.2f}",
                f"{total.overtime_hours:.2f}",
                "; ".join(total.issue_details),
            ]
        )
    return buffer.getvalue()


def to_weekly_csv(totals: list[EmployeeTotal]) -> str:
    """One line per person per Monday-Sunday week - where overtime is decided.

    The master sheet's totals are these rows added up. Payroll that pays
    overtime at a different rate needs it week by week, which is what this is.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(
        [
            "Week beginning (Mon)",
            "Week ending (Sun)",
            "Payroll ref",
            "Surname",
            "First name",
            "Department",
            "Standard week (hours)",
            "Shifts",
            "Clocked hours",
            "Paid hours",
            "Standard hours",
            "Overtime hours",
            "Rows needing attention",
        ]
    )
    for total in totals:
        standard_week = (
            f"{total.standard_weekly_hours:g}"
            if total.standard_weekly_hours is not None
            else ""
        )
        for week in total.weeks:
            writer.writerow(
                [
                    week.start.isoformat(),
                    week.end.isoformat(),
                    total.employee.payroll_ref,
                    total.employee.last_name,
                    total.employee.first_name,
                    total.employee.department or "",
                    standard_week,
                    week.shifts,
                    f"{week.hours:.2f}",
                    f"{week.paid_hours:.2f}",
                    f"{week.standard_hours:.2f}",
                    f"{week.overtime_hours:.2f}",
                    week.issues,
                ]
            )
    return buffer.getvalue()
