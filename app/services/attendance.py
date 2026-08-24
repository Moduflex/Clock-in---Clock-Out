"""Clock-in / clock-out rules.

The attendance log is append-only. Two rules do most of the work:

*Alternation* - a scan records whichever direction is the opposite of the
person's last entry, so nobody has to remember to press a button. Somebody who
forgot to clock out yesterday is flagged in the timesheet report rather than
being silently patched, because guessing a leaving time is a payroll error.

*Cooldown* - repeat scans within a short window are reported back as "already
clocked in" instead of writing a second row. Without this, standing in front of
the camera for three seconds would clock you in and straight back out again.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select

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


def currently_on_site() -> list[Employee]:
    """Employees whose latest event is a clock-in - the fire-register view."""
    employees = db.session.scalars(
        select(Employee)
        .where(Employee.is_active.is_(True), visible_employee_clause())
        .order_by(Employee.first_name)
    ).all()
    return [e for e in employees if is_clocked_in(e.id)]
