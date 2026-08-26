"""Clock-in / clock-out rules.

The attendance log is append-only. Two rules do most of the work:

*Alternation* - a scan records whichever direction is the opposite of the
person's last entry, so nobody has to remember to press a button. Somebody who
forgot to clock out yesterday is flagged in the timesheet report rather than
being silently patched, because guessing a leaving time is a payroll error.

*Cooldown* - repeat scans within a short window are reported back as "already
clocked in" instead of writing a second row. Without this, standing in front of
the camera for three seconds would clock you in and straight back out again.

The same log answers the other daily question - who is *not* here. See
:func:`daily_presence`, which sorts the active list into on site, gone home and
never arrived for a chosen local day.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from ..extensions import db
from ..models import (
    DIRECTION_IN,
    DIRECTION_OUT,
    DIRECTIONS,
    METHOD_FACE,
    AttendanceEvent,
    Employee,
    FingerprintCredential,
    utcnow,
    visible_employee_clause,
)
from .timesheet import get_default_pattern, local_day_bounds


@dataclass(frozen=True)
class ClockResult:
    """What happened when somebody scanned."""

    employee: Employee
    direction: str
    event: AttendanceEvent | None
    recorded: bool
    duplicate_of: AttendanceEvent | None = None

    @property
    def occurred_at(self) -> dt.datetime:
        if self.event is not None:
            return self.event.occurred_at
        assert self.duplicate_of is not None  # recorded=False implies a duplicate
        return self.duplicate_of.occurred_at

    @property
    def message(self) -> str:
        verb = "Clocked in" if self.direction == DIRECTION_IN else "Clocked out"
        if self.recorded:
            return f"{verb}, {self.employee.first_name}."
        return f"Already {verb.lower()}, {self.employee.first_name}."


def last_event(employee_id: int, *, before: dt.datetime | None = None) -> AttendanceEvent | None:
    """Most recent non-voided event for an employee."""
    stmt = (
        select(AttendanceEvent)
        .where(
            AttendanceEvent.employee_id == employee_id,
            AttendanceEvent.is_voided.is_(False),
        )
        .order_by(AttendanceEvent.occurred_at.desc(), AttendanceEvent.id.desc())
        .limit(1)
    )
    if before is not None:
        stmt = stmt.where(AttendanceEvent.occurred_at < before)
    return db.session.scalars(stmt).first()


def next_direction(employee_id: int) -> str:
    """The direction a scan should record next: the opposite of the last one."""
    previous = last_event(employee_id)
    if previous is None or previous.direction == DIRECTION_OUT:
        return DIRECTION_IN
    return DIRECTION_OUT


def is_clocked_in(employee_id: int) -> bool:
    previous = last_event(employee_id)
    return previous is not None and previous.direction == DIRECTION_IN


def record_clock(
    employee: Employee,
    *,
    direction: str | None = None,
    confidence: float | None = None,
    method: str = METHOD_FACE,
    device_label: str | None = None,
    note: str | None = None,
    created_by_id: int | None = None,
    cooldown_seconds: int = 90,
    occurred_at: dt.datetime | None = None,
    commit: bool = True,
    automatic: bool = False,
) -> ClockResult:
    """Record a clock event for *employee*.

    *direction* defaults to the alternating value from :func:`next_direction`.
    Pass it explicitly only when the user or an administrator has chosen it.

    *automatic* marks a hands-free entry that nobody asked for by pressing
    anything. Such an entry never overrides the cooldown, even when a direction
    is supplied, because merely being seen by the camera states no intent.
    """
    # Whether anybody actually asked for this direction governs the cooldown.
    stated_intent = direction is not None and not automatic
    if direction is None:
        direction = next_direction(employee.id)
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS!r}, got {direction!r}")

    moment = occurred_at or utcnow()

    if cooldown_seconds > 0:
        previous = last_event(employee.id)
        within_cooldown = (
            previous is not None
            and (moment - previous.occurred_at).total_seconds() < cooldown_seconds
        )
        if within_cooldown:
            assert previous is not None  # implied by within_cooldown
            # With no stated intent - the Scan button, or hands-free - the
            # direction is merely the alternation of the last entry. So any entry
            # inside the window means "you have only just clocked": report that
            # state back instead of alternating. Without this, lingering in front
            # of the camera clocks you in and straight back out again.
            #
            # An explicitly chosen direction does state intent, so it is honoured
            # unless it merely repeats the last entry. Somebody who genuinely
            # arrives and leaves again immediately can still press Clock out.
            if not stated_intent:
                return ClockResult(
                    employee=employee,
                    direction=previous.direction,
                    event=None,
                    recorded=False,
                    duplicate_of=previous,
                )
            if previous.direction == direction:
                return ClockResult(
                    employee=employee,
                    direction=direction,
                    event=None,
                    recorded=False,
                    duplicate_of=previous,
                )

    event = AttendanceEvent(
        employee_id=employee.id,
        direction=direction,
        occurred_at=moment,
        method=method,
        confidence=confidence,
        device_label=device_label,
        note=note,
        created_by_id=created_by_id,
    )
    db.session.add(event)
    if commit:
        db.session.commit()
    else:
        db.session.flush()

    return ClockResult(
        employee=employee, direction=direction, event=event, recorded=True
    )


def void_event(event: AttendanceEvent, *, admin_id: int, reason: str) -> None:
    """Mark an event as void, keeping the original row for the audit trail."""
    event.is_voided = True
    stamp = f"Voided by admin {admin_id}: {reason}".strip()
    event.note = f"{event.note}\n{stamp}" if event.note else stamp
    db.session.commit()


def find_fingerprint(device_label: str, finger_id: int) -> FingerprintCredential | None:
    """The registered credential for one slot on one reader, if there is one.

    Scoped to the device as well as the slot: two readers each have a slot 7,
    and they belong to different people.
    """
    return db.session.scalars(
        select(FingerprintCredential).where(
            FingerprintCredential.device_label == device_label,
            FingerprintCredential.finger_id == finger_id,
            FingerprintCredential.is_active.is_(True),
        )
    ).first()


def find_by_payroll_ref(reference: str) -> Employee | None:
    """The employee whose payroll reference is *reference*, if there is one.

    Matched case-insensitively and with the surrounding whitespace trimmed,
    because this is typed in on a workshop touchscreen: "e042" and " E042 "
    are the same person as "E042", and refusing them would only teach people
    that the keypad does not work.

    The joke record is excluded like everywhere else, so it cannot be clocked
    by typing its reference.
    """
    cleaned = (reference or "").strip()
    if not cleaned:
        return None
    return db.session.scalars(
        select(Employee).where(
            func.lower(Employee.payroll_ref) == cleaned.lower(),
            visible_employee_clause(),
        )
    ).first()


def currently_on_site() -> list[Employee]:
    """Employees whose latest event is a clock-in - the fire-register view."""
    employees = db.session.scalars(
        select(Employee)
        .where(Employee.is_active.is_(True), visible_employee_clause())
        .order_by(Employee.first_name)
    ).all()
    return [e for e in employees if is_clocked_in(e.id)]


# --------------------------------------------------------------------------
# Who is not here: the absence view
# --------------------------------------------------------------------------
# Three states cover an active employee on any one day. They are kept as short
# strings for the same reason the directions are - adding a fourth later is a
# code change rather than a schema migration.
PRESENCE_ON_SITE = "on_site"
PRESENCE_LEFT = "left"
PRESENCE_ABSENT = "absent"


@dataclass(frozen=True)
class DayPresence:
    """One employee's attendance state across one local day.

    Timestamps are naive UTC like everything else stored, so the ``localtime``
    template filter renders them the same way it renders an event.
    """

    employee: Employee
    status: str
    first_in: dt.datetime | None
    last_out: dt.datetime | None
    #: Their most recent clocking of any kind, which for somebody absent says
    #: whether this is the first day missed or the fifth.
    last_seen: dt.datetime | None
    #: When their shift pattern says they were due to start, if one applies.
    expected_start: dt.datetime | None
    #: The moment the state was judged at: now for today, midnight for a day
    #: already finished. Nobody is "late" measured against a clock still running.
    reference: dt.datetime

    @property
    def is_absent(self) -> bool:
        return self.status == PRESENCE_ABSENT

    @property
    def is_due(self) -> bool:
        """True once their shift has started, so a missing clock-in is a problem.

        Somebody on an afternoon shift has not failed to turn up at nine in the
        morning, and listing them as absent alongside people who genuinely have
        not arrived would make the whole page easy to ignore. With no shift set
        at all there is nothing to wait for, so they count as due.
        """
        if self.expected_start is None:
            return True
        return self.reference >= self.expected_start

    @property
    def overdue_minutes(self) -> int | None:
        """How long ago the shift should have started, or None if not yet due."""
        if self.expected_start is None or self.reference < self.expected_start:
            return None
        return int((self.reference - self.expected_start).total_seconds() // 60)

    @property
    def overdue_text(self) -> str:
        """"1h 12m" - the overdue figure ready for the page."""
        minutes = self.overdue_minutes
        if minutes is None:
            return ""
        hours, remainder = divmod(minutes, 60)
        return f"{hours}h {remainder:02d}m" if hours else f"{remainder}m"


def _shift_start(
    employee: Employee, day: dt.date, tz: ZoneInfo, default_pattern
) -> dt.datetime | None:
    """When *employee* was due to start on *day*, as a naive UTC timestamp.

    Shift times are local wall-clock times, so this is built in the site's
    timezone and converted - 07:30 is 07:30 either side of the BST change.
    """
    pattern = employee.shift_pattern or default_pattern
    if pattern is None:
        return None
    local = dt.datetime.combine(day, pattern.start_time, tzinfo=tz)
    return local.astimezone(dt.timezone.utc).replace(tzinfo=None)


def daily_presence(
    day: dt.date,
    tz: ZoneInfo,
    *,
    now: dt.datetime | None = None,
    department: str | None = None,
) -> list[DayPresence]:
    """Attendance state for every active employee on one local day.

    Anyone whose last entry before the day was a clock-in counts as on site even
    with nothing recorded on the day itself: a night shift that started at 22:00
    yesterday is still a person in the building, and calling them absent would
    make the list wrong precisely when it is being used as a fire register.
    """
    start, end = local_day_bounds(day, tz)
    # Past days are judged at their own midnight, not against the clock now.
    reference = min(now or utcnow(), end)

    stmt = (
        select(Employee)
        .where(Employee.is_active.is_(True), visible_employee_clause())
        .order_by(Employee.last_name, Employee.first_name)
    )
    if department:
        stmt = stmt.where(Employee.department == department)
    employees = db.session.scalars(stmt).all()
    if not employees:
        return []

    events = db.session.scalars(
        select(AttendanceEvent)
        .where(
            AttendanceEvent.employee_id.in_([e.id for e in employees]),
            AttendanceEvent.is_voided.is_(False),
            AttendanceEvent.occurred_at >= start,
            AttendanceEvent.occurred_at < end,
        )
        .order_by(AttendanceEvent.occurred_at, AttendanceEvent.id)
    ).all()
    by_employee: dict[int, list[AttendanceEvent]] = {}
    for event in events:
        by_employee.setdefault(event.employee_id, []).append(event)

    default_pattern = get_default_pattern()
    records = []
    for employee in employees:
        today_events = by_employee.get(employee.id)
        if today_events:
            latest = today_events[-1]
            on_site = latest.direction == DIRECTION_IN
            first_in = next(
                (e.occurred_at for e in today_events if e.direction == DIRECTION_IN), None
            )
            last_out = None if on_site else latest.occurred_at
            last_seen = latest.occurred_at
        else:
            previous = last_event(employee.id, before=start)
            on_site = previous is not None and previous.direction == DIRECTION_IN
            first_in = last_out = None
            last_seen = previous.occurred_at if previous is not None else None

        if on_site:
            status = PRESENCE_ON_SITE
        elif today_events:
            status = PRESENCE_LEFT
        else:
            status = PRESENCE_ABSENT

        records.append(
            DayPresence(
                employee=employee,
                status=status,
                first_in=first_in,
                last_out=last_out,
                last_seen=last_seen,
                expected_start=_shift_start(employee, day, tz, default_pattern),
                reference=reference,
            )
        )
    return records
