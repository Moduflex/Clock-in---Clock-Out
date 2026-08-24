"""Database models.

Column types are deliberately generic (LargeBinary / String / DateTime) so the
same models run on MySQL in production and on SQLite in the test suite.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import bcrypt
from flask_login import UserMixin
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .extensions import db

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

# Direction / method values are stored as short strings rather than SQL ENUMs:
# adding a value later is then a code change, not a schema migration.
DIRECTION_IN = "in"
DIRECTION_OUT = "out"
DIRECTIONS = (DIRECTION_IN, DIRECTION_OUT)

METHOD_FACE = "face"
# Recorded hands-free, with no button press. Kept distinct from METHOD_FACE so a
# payroll query can tell a deliberate scan from an automatic one.
METHOD_AUTO = "auto"
METHOD_MANUAL = "manual"
# Recorded by a fingerprint reader. The reader matches the finger itself and
# reports which of its own slots matched; see FingerprintCredential.
METHOD_FINGER = "finger"


def utcnow() -> dt.datetime:
    """Timezone-naive UTC timestamp (MySQL DATETIME stores no offset)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class ShiftPattern(db.Model):
    """A paid time band, e.g. 07:30-16:00 with a 30-minute unpaid lunch.

    Times are *local* wall-clock times (the timezone the site runs in), not UTC:
    a 07:30 start means 07:30 on the shop floor all year round, either side of
    the BST/GMT change. An end time at or before the start time means the shift
    runs overnight into the next day.
    """

    __tablename__ = "shift_pattern"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    start_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    end_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    unpaid_break_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The break is only deducted once the paid time exceeds this - a short
    # morning or afternoon stint contains no lunch to deduct. 360 minutes
    # matches the UK working-time rule on when a rest break is due.
    break_applies_after_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=360
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    employees: Mapped[list["Employee"]] = relationship(back_populates="shift_pattern")

    @property
    def crosses_midnight(self) -> bool:
        return self.end_time <= self.start_time

    @property
    def label(self) -> str:
        return (
            f"{self.name} ({self.start_time.strftime('%H:%M')}"
            f"–{self.end_time.strftime('%H:%M')})"
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ShiftPattern {self.name} {self.start_time}-{self.end_time}>"


class Employee(db.Model):
    __tablename__ = "employee"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payroll_ref: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    first_name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_name: Mapped[str] = mapped_column(String(64), nullable=False)
    department: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(190))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # NULL means "use the default shift pattern", so new starters need no setup.
    shift_pattern_id: Mapped[int | None] = mapped_column(
        ForeignKey("shift_pattern.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    templates: Mapped[list["FaceTemplate"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", lazy="selectin"
    )
    events: Mapped[list["AttendanceEvent"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", lazy="select"
    )
    shift_pattern: Mapped[ShiftPattern | None] = relationship(back_populates="employees")
    fingerprints: Mapped[list["FingerprintCredential"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_enrolled(self) -> bool:
        return len(self.templates) > 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Employee {self.payroll_ref} {self.full_name}>"


# --- a bit of fun -------------------------------------------------------------
# Claude AI was clocked in once and is never clocked out. It is not a real
# employee, so it is filtered out of every list, count and payroll figure: a
# fictional name in a payroll export would be a genuine problem, joke or not.
# Delete the row and this constant to remove it entirely.
HIDDEN_EMPLOYEE_NAMES = frozenset({("claude", "ai")})


def visible_employee_clause():
    """SQL criterion excluding the joke records from any Employee query."""
    from sqlalchemy import and_, func, not_, true

    if not HIDDEN_EMPLOYEE_NAMES:
        return true()
    return and_(
        *[
            not_(
                and_(
                    func.lower(Employee.first_name) == first,
                    func.lower(Employee.last_name) == last,
                )
            )
            for first, last in HIDDEN_EMPLOYEE_NAMES
        ]
    )


def is_hidden_employee(employee: Employee | None) -> bool:
    """True for a record that never appears in reports, lists or counts."""
    if employee is None:
        return False
    key = (employee.first_name.strip().lower(), employee.last_name.strip().lower())
    return key in HIDDEN_EMPLOYEE_NAMES


class FaceTemplate(db.Model):
    """One enrolled face embedding: 128 float32 values, L2-normalised."""

    __tablename__ = "face_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False, index=True
    )
    embedding: Mapped[bytes] = mapped_column(LargeBinary(2048), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False, default=128)
    sharpness: Mapped[float | None] = mapped_column(Float)
    face_pixels: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_user.id", ondelete="SET NULL")
    )

    employee: Mapped[Employee] = relationship(back_populates="templates")

    def as_vector(self) -> "np.ndarray":
        """Return the stored embedding as a 1-D float32 numpy array."""
        import numpy as np

        return np.frombuffer(self.embedding, dtype=np.float32)

    @staticmethod
    def pack(vector: "np.ndarray") -> bytes:
        """Serialise a numpy embedding for storage."""
        import numpy as np

        return np.ascontiguousarray(vector, dtype=np.float32).tobytes()


# Finger positions as the Windows Biometric Framework numbers them (ANSI 381).
# A Windows Hello reader has no slots of its own: the finger *position* is the
# identity, so on that hardware these ten values are the whole identity space
# available on one Windows account. A slot-based reader ignores this and just
# counts from 1, which is why finger_id is a plain integer rather than an enum.
FINGER_POSITIONS = {
    1: "Right thumb",
    2: "Right index",
    3: "Right middle",
    4: "Right ring",
    5: "Right little",
    6: "Left thumb",
    7: "Left index",
    8: "Left middle",
    9: "Left ring",
    10: "Left little",
}


class FingerprintCredential(db.Model):
    """Links one slot on a fingerprint reader to an employee.

    **No biometric data is stored here.** A reader of the supported kind holds
    the fingerprint in its own memory, matches the finger on the device, and
    reports back only which of its numbered slots matched. All this table keeps
    is "slot 7 on the workshop reader is Bob" - a reference, not a fingerprint.

    That is a deliberate choice rather than an accident of the hardware: a
    fingerprint that never reaches the database cannot leak from a database
    backup, and it keeps the amount of special category data held to a minimum.
    """

    __tablename__ = "fingerprint_credential"
    __table_args__ = (
        # One slot on one reader identifies exactly one person. Without this a
        # mis-typed slot number could silently clock in the wrong employee.
        UniqueConstraint("device_label", "finger_id", name="uq_fingerprint_device_slot"),
        Index("ix_fingerprint_lookup", "device_label", "finger_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Which reader this slot belongs to: two readers each have a slot 7.
    device_label: Mapped[str] = mapped_column(String(64), nullable=False)
    finger_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Free text for whoever maintains it, e.g. "right index".
    label: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_user.id", ondelete="SET NULL")
    )
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    employee: Mapped[Employee] = relationship(back_populates="fingerprints")

    @property
    def position_name(self) -> str | None:
        """The finger this slot means, on a reader where slots are positions."""
        return FINGER_POSITIONS.get(self.finger_id)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FingerprintCredential {self.device_label}#{self.finger_id} -> {self.employee_id}>"


class AttendanceEvent(db.Model):
    """An append-only clock-in / clock-out log entry.

    Nothing is ever overwritten: an incorrect entry is voided (is_voided) and a
    corrected one added, so the audit trail stays intact for payroll queries.
    """

    __tablename__ = "attendance_event"
    __table_args__ = (Index("ix_attendance_employee_time", "employee_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )
    method: Mapped[str] = mapped_column(String(16), nullable=False, default=METHOD_FACE)
    confidence: Mapped[float | None] = mapped_column(Float)
    device_label: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    is_voided: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_user.id", ondelete="SET NULL")
    )

    employee: Mapped[Employee] = relationship(back_populates="events")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AttendanceEvent {self.employee_id} {self.direction} {self.occurred_at}>"


class AdminUser(UserMixin, db.Model):
    """A back-office login. Kiosk users never authenticate as one of these."""

    __tablename__ = "admin_user"
    __table_args__ = (UniqueConstraint("username", name="uq_admin_username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(128))
    is_active_flag: Mapped[bool] = mapped_column(
        "is_active", Boolean, nullable=False, default=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    # --- password handling -------------------------------------------------
    def set_password(self, password: str) -> None:
        if not password or len(password) < 10:
            raise ValueError("Password must be at least 10 characters long.")
        # bcrypt silently truncates beyond 72 bytes; reject rather than mislead.
        if len(password.encode("utf-8")) > 72:
            raise ValueError("Password must be 72 bytes or fewer.")
        self.password_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt()
        ).decode("ascii")

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        try:
            return bcrypt.checkpw(password.encode("utf-8"), self.password_hash.encode("ascii"))
        except (ValueError, TypeError):
            return False

    # Flask-Login reads is_active to block disabled accounts.
    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return bool(self.is_active_flag)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AdminUser {self.username}>"
