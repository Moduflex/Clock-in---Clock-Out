"""What the office dashboard shows, worked out away from the route.

The page answers one question - *is anything wrong this morning?* - so every
figure on it is an exception, or something an exception is measured against.
The four states an active employee can be in on a given day are the ones the
Absence page already names, and they are used here unchanged:

    On site now     their last clocking was an IN
    Finished        they clocked in and out again
    Not due yet     nothing recorded, but their shift has not started
    Not clocked in  nothing recorded and their shift started a while ago

Nothing here invents a state the rest of the system does not have. There is no
booked-absence record in this database, so nobody is shown as on holiday: a
person who is off is counted as not due, or not clocked in, like anybody else.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..extensions import db
from ..models import (
    METHOD_MANUAL,
    AttendanceEvent,
    Employee,
    utcnow,
    visible_employee_clause,
)
from . import attendance
from .payroll_sheet import period_bounds, sheet_employees
from .timesheet import Shift, build_timesheet, summarise, to_local

#: Past this many minutes late, a missing clock-in stops being a warning and
#: becomes a red one. Two hours is long enough to rule out a late train and
#: short enough that the office still has the morning to chase it.
OVERDUE_ALERT_MINUTES = 120

#: How far back to look for a shift somebody never clocked out of. A week
#: covers the "I forgot on Friday" case that only surfaces on Monday, without
#: dredging up corrections that have already been made.
MISSED_CLOCKOUT_DAYS = 7

#: A kiosk that has taken a scan this recently is treated as awake. There is no
#: heartbeat in this system, so anything longer is reported as the time since
#: the last scan and nothing more - calling a silent kiosk "offline" at eleven
#: at night would be a guess dressed up as a fact.
KIOSK_LIVE_MINUTES = 30

#: How many weeks a four-weekly payroll period runs for. Named rather than
#: written as 4 in three places.
PERIOD_WEEKS = 4


# --------------------------------------------------------------------------
# Who is where
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Presence:
    """The day split into the four states, kept as lists so names can be shown."""

    on_site: list[attendance.DayPresence]
    left: list[attendance.DayPresence]
    not_due: list[attendance.DayPresence]
    overdue: list[attendance.DayPresence]

    @property
    def expected(self) -> int:
        """Active employees the split was made over."""
        return len(self.on_site) + len(self.left) + len(self.not_due) + len(self.overdue)

    def share(self, group: list[attendance.DayPresence]) -> float:
        """*group* as a percentage of everyone expected, for the bar widths."""
        if not self.expected:
            return 0.0
        return round(100.0 * len(group) / self.expected, 2)


def presence(day: dt.date, tz: ZoneInfo, *, now: dt.datetime | None = None) -> Presence:
    """Split every active employee across the four states for *day*."""
    records = attendance.daily_presence(day, tz, now=now)
    return Presence(
        on_site=[r for r in records if r.status == attendance.PRESENCE_ON_SITE],
        left=[r for r in records if r.status == attendance.PRESENCE_LEFT],
        not_due=[r for r in records if r.is_absent and not r.is_due],
        overdue=[r for r in records if r.is_absent and r.is_due],
    )


@dataclass(frozen=True)
class Department:
    """One department's line on the fire register."""

    name: str
    on_site: list[Employee]
    #: Everybody expected today, not everybody employed - so "11 of 14" reads
    #: as a comparison a supervisor can check by looking round the shop.
    headcount: int


def by_department(records: Presence) -> list[Department]:
    """Who is on site, grouped by department, fullest department first."""
    counts: dict[str, int] = {}
    here: dict[str, list[Employee]] = {}
    for group in (records.on_site, records.left, records.not_due, records.overdue):
        for record in group:
            name = record.employee.department or "No department"
            counts[name] = counts.get(name, 0) + 1
            here.setdefault(name, [])
    for record in records.on_site:
        here[record.employee.department or "No department"].append(record.employee)

    return sorted(
        (Department(name, here[name], counts[name]) for name in counts),
        key=lambda dept: (-len(dept.on_site), dept.name),
    )


# --------------------------------------------------------------------------
# Needs attention
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Attention:
    """One row of the "needs attention today" table.

    Both kinds of problem answer the same five questions, so they share a row
    type and sit in one table: an office looking for what to fix should not
    have to read two lists to find it.
    """

    employee: Employee
    issue: str
    #: "danger" or "warn" - which badge the issue text gets.
    severity: str
    #: The local wall-clock time they were due to start, if a shift applies.
    due_to_start: dt.time | None
    #: Their last clocking of any kind, stored UTC for the ``localtime`` filter.
    last_clocking: dt.datetime | None
    #: Sorts the table worst-first, whatever kind of problem the row is. The
    #: longest overdue leads; missed clock-outs follow, most recent first.
    sort_key: tuple[int, int]


def in_progress(shift: Shift) -> bool:
    """True for a shift somebody is still working: clocked in, not yet due out.

    Worth naming because ``pair_events`` marks these "Still clocked in", which
    reads as a warning and is counted as one on a timesheet. On a page looking
    at the day as it happens it is not: half the shop floor is mid-shift at
    eleven in the morning, and a payroll figure that counts them all as
    problems is a figure nobody will look at twice.
    """
    return (
        shift.clock_in is not None
        and shift.clock_out is None
        and not shift.end_is_assumed
    )


def missed_clockouts(
    tz: ZoneInfo, today: dt.date, *, days: int = MISSED_CLOCKOUT_DAYS
) -> list[Shift]:
    """Shifts whose clock-out never happened and whose shift end has passed.

    ``Shift.end_is_assumed`` is the test rather than a missing clock-out on its
    own, because somebody halfway through a shift has no leaving time yet and
    is not a problem. Paid hours already stand the shift end in for these; the
    point of listing them is that the real leaving time may have been later.
    """
    start = today - dt.timedelta(days=days - 1)
    return [shift for shift in build_timesheet(start, today, tz) if shift.end_is_assumed]


def attention(
    records: Presence, shifts: list[Shift], today: dt.date, tz: ZoneInfo
) -> list[Attention]:
    """The overdue and the never-clocked-out in one list, worst first."""
    rows: list[Attention] = []

    for record in records.overdue:
        minutes = record.overdue_minutes
        start = record.expected_start
        rows.append(
            Attention(
                employee=record.employee,
                # With no shift pattern there is no start to be late against,
                # so the row says what is true rather than "0m overdue".
                issue=(
                    f"{record.overdue_text} overdue"
                    if minutes is not None
                    else "Not clocked in"
                ),
                severity=(
                    "danger"
                    if minutes is not None and minutes >= OVERDUE_ALERT_MINUTES
                    else "warn"
                ),
                due_to_start=to_local(start, tz).time() if start else None,
                last_clocking=record.last_seen,
                sort_key=(0, -(minutes or 0)),
            )
        )

    for shift in shifts:
        day = shift.date
        when = "today" if day == today else day.strftime("%a") if day else ""
        rows.append(
            Attention(
                employee=shift.employee,
                issue=f"No clock-out {when}".strip(),
                severity="warn",
                due_to_start=shift.pattern.start_time if shift.pattern else None,
                last_clocking=shift.clock_in.occurred_at if shift.clock_in else None,
                sort_key=(1, (today - day).days if day else 0),
            )
        )

    return sorted(rows, key=lambda row: row.sort_key)


# --------------------------------------------------------------------------
# Payroll readiness
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Payroll:
    """How ready the running four-weekly period is to be sent to payroll."""

    period: int
    start: dt.date
    end: dt.date
    #: Which week of the four is running, 1 to 4.
    week: int
    #: The last day the figures below cover: today, not the period end.
    covered_to: dt.date
    on_sheet: int
    rows_ready: int
    warnings: int
    missing_rate: int
    overtime_hours: float

    @property
    def week_states(self) -> list[str]:
        """One CSS class per week, for the four-segment progress bar."""
        return [
            "is-done" if n < self.week else "is-current" if n == self.week else ""
            for n in range(1, PERIOD_WEEKS + 1)
        ]


def payroll(
    today: dt.date, tz: ZoneInfo, anchor: dt.date, periods_per_year: int = 13
) -> Payroll:
    """Payroll readiness for the period *today* falls in.

    Counted over the period so far rather than the whole of it: the question
    being asked in week 2 is "what will I have to chase when this closes", and
    a fortnight that has not happened yet cannot be short of anything.
    """
    period, start, end = period_bounds(today, anchor, periods_per_year)
    covered_to = min(today, end)

    on_sheet = sheet_employees()
    # Salaried staff are deliberately not on the four-weekly sheet, so their
    # hours are not what makes it ready - see payroll_sheet.sheet_employees.
    ids = {employee.id for employee in on_sheet}

    shifts = build_timesheet(start, covered_to, tz)
    totals = [total for total in summarise(shifts) if total.employee.id in ids]
    # Counted off the shifts rather than off EmployeeTotal.issues so that a
    # shift still being worked is not a warning - see in_progress().
    problems = [
        shift
        for shift in shifts
        if shift.employee.id in ids and shift.issue and not in_progress(shift)
    ]
    flagged = {shift.employee.id for shift in problems}

    return Payroll(
        period=period,
        start=start,
        end=end,
        week=min((covered_to - start).days // 7 + 1, PERIOD_WEEKS),
        covered_to=covered_to,
        on_sheet=len(on_sheet),
        rows_ready=len(on_sheet) - len(flagged),
        warnings=len(problems),
        missing_rate=sum(1 for employee in on_sheet if employee.basic_rate is None),
        overtime_hours=round(sum(total.overtime_hours for total in totals), 2),
    )


# --------------------------------------------------------------------------
# The kiosk
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Kiosk:
    """What can honestly be said about the kiosk from the clocking log alone."""

    #: The device label on the most recent scan, or None if nothing has scanned.
    device: str | None
    last_scan: dt.datetime | None
    minutes_ago: int | None
    enrolled: int
    active: int
    index_size: int

    @property
    def is_live(self) -> bool:
        """True when a scan landed recently enough to call the kiosk awake."""
        return self.minutes_ago is not None and self.minutes_ago <= KIOSK_LIVE_MINUTES

    @property
    def since_text(self) -> str:
        """"4 min ago" / "3h 12m ago" / "no scans yet"."""
        minutes = self.minutes_ago
        if minutes is None:
            return "no scans yet"
        if minutes < 1:
            return "just now"
        hours, remainder = divmod(minutes, 60)
        if hours >= 24:
            days = hours // 24
            return f"{days} day{'s' if days > 1 else ''} ago"
        return f"{hours}h {remainder:02d}m ago" if hours else f"{minutes} min ago"

    @property
    def to_enrol(self) -> int:
        """Active employees who cannot use the kiosk because they have no face."""
        return max(0, self.active - self.enrolled)


def kiosk(
    employees: list[Employee], index_size: int, *, now: dt.datetime | None = None
) -> Kiosk:
    """Kiosk and recognition state, read off the clocking log.

    The last scan is the most recent automatic or face clocking of anybody: a
    manual entry typed in the office says nothing about whether the screen in
    the workshop is working.
    """
    latest = db.session.scalars(
        select(AttendanceEvent)
        .join(Employee)
        .where(
            AttendanceEvent.is_voided.is_(False),
            AttendanceEvent.method != METHOD_MANUAL,
            visible_employee_clause(),
        )
        .order_by(AttendanceEvent.occurred_at.desc())
        .limit(1)
    ).first()

    minutes = None
    if latest is not None:
        elapsed = (now or utcnow()) - latest.occurred_at
        minutes = max(0, int(elapsed.total_seconds() // 60))

    return Kiosk(
        device=latest.device_label if latest else None,
        last_scan=latest.occurred_at if latest else None,
        minutes_ago=minutes,
        enrolled=sum(1 for e in employees if e.is_enrolled),
        active=sum(1 for e in employees if e.is_active),
        index_size=index_size,
    )
