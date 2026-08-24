"""Fingerprint readers that hand back a template for us to match.

Two kinds of fingerprint reader are supported, and they work quite differently:

* A **slot-based** reader (R307 and similar) matches the finger in its own
  memory and reports only which of its slots matched. Nothing biometric reaches
  us - see :class:`~app.models.FingerprintCredential`.
* A **desktop USB reader with an SDK** (ZKTeco ZK9500, DigitalPersona
  U.are.U) hands back a *template*, and matching happens here. That is what
  this module is for.

The vendor SDK is confined to a small driver interface, for three reasons: the
matching rules can then be tested without any hardware, swapping vendor does not
touch the rest of the app, and the one genuinely specialist part - comparing two
templates - stays where the vendor put it. We never invent a matching algorithm;
a home-made comparison would be the sort of bug that pays somebody for a shift
they did not work.

Matching mirrors the face matcher deliberately: a probe is scored against every
enrolled template, each employee keeps their single best score, and a match must
clear a threshold *and* beat the runner-up by a margin. Two similar fingers
therefore refuse rather than guess.
"""

from __future__ import annotations

import base64
import sys
import threading
from dataclasses import dataclass
from typing import Protocol

from flask import current_app
from sqlalchemy import select

from ..extensions import db
from ..models import Employee, FingerprintTemplate, visible_employee_clause


class FingerprintError(Exception):
    """A reader problem worth reporting to whoever is standing at the kiosk."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# The driver interface
# --------------------------------------------------------------------------
class FingerprintDriver(Protocol):
    """What this application needs from a fingerprint reader SDK.

    Deliberately tiny. Everything else - enrolment flow, storage, matching
    policy, clocking - is vendor-independent and lives outside the driver.
    """

    name: str

    def capture(self, timeout: float = 15.0) -> tuple[bytes, float | None]:
        """Wait for a finger and return (template bytes, quality or None)."""
        ...

    def compare(self, probe: bytes, candidate: bytes) -> float:
        """Score two templates for similarity, 0.0 to 1.0."""
        ...

    def close(self) -> None:
        """Release the reader."""
        ...


# --------------------------------------------------------------------------
# Simulator - no hardware required
# --------------------------------------------------------------------------
class SimulatorDriver:
    """A stand-in reader, so the whole path can be built and tested dry.

    A "template" is just a short identifying string. Two templates score by how
    much of their leading text agrees, which is enough to exercise the
    threshold, the margin and the refusal paths without any hardware.
    """

    name = "simulator"

    def __init__(self) -> None:
        self._queued: list[str] = []

    def queue(self, *fingers: str) -> None:
        """Line up the finger(s) that the next capture(s) will return."""
        self._queued.extend(fingers)

    @staticmethod
    def template_for(finger: str) -> bytes:
        return finger.encode("utf-8")

    def capture(self, timeout: float = 15.0) -> tuple[bytes, float | None]:
        if self._queued:
            return self.template_for(self._queued.pop(0)), 80.0
        # Run interactively, typing a name stands in for pressing a finger, so
        # the enrolment and clocking flow can be rehearsed before hardware
        # arrives. Under the test suite stdin is not a terminal, so this is
        # skipped and the absence of a finger is reported as normal.
        if sys.stdin is not None and sys.stdin.isatty():
            typed = input("  simulated finger (any name, e.g. alice-index): ").strip()
            if typed:
                return self.template_for(typed), 80.0
        raise FingerprintError("no_finger", "No finger was presented.")

    def compare(self, probe: bytes, candidate: bytes) -> float:
        if not probe or not candidate:
            return 0.0
        if probe == candidate:
            return 1.0
        left, right = probe.decode("utf-8", "replace"), candidate.decode("utf-8", "replace")
        shared = 0
        for a, b in zip(left, right):
            if a != b:
                break
            shared += 1
        return shared / max(len(left), len(right))

    def close(self) -> None:
        self._queued.clear()


# --------------------------------------------------------------------------
# Driver loading
# --------------------------------------------------------------------------
_drivers: dict[str, FingerprintDriver] = {}
_drivers_lock = threading.Lock()


def get_driver(name: str | None = None) -> FingerprintDriver:
    """Return the configured driver, creating it once per process.

    Readers are single-user devices: opening one twice tends to fail with
    "device busy", so the instance is cached rather than rebuilt per request.
    """
    name = name or current_app.config["FINGERPRINT_DRIVER"]
    with _drivers_lock:
        driver = _drivers.get(name)
        if driver is not None:
            return driver

        if name == "simulator":
            driver = SimulatorDriver()
        elif name in {"zkfinger", "digitalpersona"}:
            from .fingerprint_sdk import load_sdk_driver

            driver = load_sdk_driver(name)
        else:
            raise FingerprintError(
                "unknown_driver", f"No fingerprint driver called {name!r}."
            )
        _drivers[name] = driver
        return driver


def reset_drivers() -> None:
    """Drop cached drivers - used by the test suite and by --reload."""
    with _drivers_lock:
        for driver in _drivers.values():
            try:
                driver.close()
            except Exception:  # noqa: BLE001 - shutting down regardless
                pass
        _drivers.clear()


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FingerMatch:
    """Outcome of comparing one captured finger against everyone enrolled."""

    employee_id: int | None
    score: float
    runner_up_id: int | None = None
    runner_up_score: float = 0.0
    reason: str = "matched"

    @property
    def accepted(self) -> bool:
        return self.employee_id is not None

    @property
    def margin(self) -> float:
        return self.score - self.runner_up_score


def identify(
    probe: bytes,
    *,
    driver: FingerprintDriver | None = None,
    threshold: float | None = None,
    margin: float | None = None,
) -> FingerMatch:
    """Find whose finger *probe* is, or refuse and say why."""
    driver = driver or get_driver()
    if threshold is None:
        threshold = current_app.config["FINGERPRINT_MATCH_THRESHOLD"]
    if margin is None:
        margin = current_app.config["FINGERPRINT_MATCH_MARGIN"]

    # Templates from another vendor's SDK are not comparable, and inactive or
    # hidden employees are not candidates for clocking.
    rows = db.session.execute(
        select(FingerprintTemplate.employee_id, FingerprintTemplate.template)
        .join(Employee, Employee.id == FingerprintTemplate.employee_id)
        .where(
            FingerprintTemplate.driver == driver.name,
            Employee.is_active.is_(True),
            visible_employee_clause(),
        )
    ).all()

    if not rows:
        return FingerMatch(None, 0.0, reason="no_templates")

    # One employee normally has several fingers enrolled; each keeps their best
    # score, so extra enrolments can only help, never dilute.
    best: dict[int, float] = {}
    for employee_id, template in rows:
        score = driver.compare(probe, bytes(template))
        if score > best.get(employee_id, -1.0):
            best[employee_id] = score

    ranked = sorted(best.items(), key=lambda pair: pair[1], reverse=True)
    top_id, top_score = ranked[0]
    second_id, second_score = (ranked[1] if len(ranked) > 1 else (None, 0.0))

    if top_score < threshold:
        return FingerMatch(None, top_score, second_id, second_score, "below_threshold")
    if top_score - second_score < margin:
        # Two people scored almost the same. Refusing and asking again is the
        # only safe answer: a wrong match writes a wrong row into payroll.
        return FingerMatch(None, top_score, second_id, second_score, "ambiguous")
    return FingerMatch(top_id, top_score, second_id, second_score, "matched")


# --------------------------------------------------------------------------
# Enrolment
# --------------------------------------------------------------------------
@dataclass
class EnrolOutcome:
    ok: bool
    code: str
    message: str
    added: int = 0


def enrol(
    employee: Employee,
    templates: list[tuple[bytes, float | None]],
    *,
    driver: FingerprintDriver | None = None,
    position: int | None = None,
    admin_id: int | None = None,
    replace_existing: bool = False,
) -> EnrolOutcome:
    """Store captured templates against *employee*.

    Refuses if the finger already belongs to somebody else: enrolling one person
    twice under two names would let them clock in as either.
    """
    driver = driver or get_driver()
    if not templates:
        return EnrolOutcome(False, "no_samples", "No fingerprints were captured.")

    clash = identify(templates[0][0], driver=driver)
    if clash.accepted and clash.employee_id != employee.id:
        other = db.session.get(Employee, clash.employee_id)
        name = other.full_name if other else f"employee {clash.employee_id}"
        return EnrolOutcome(
            False,
            "already_enrolled",
            f"That finger is already enrolled to {name}.",
        )

    if replace_existing:
        for existing in list(employee.fingerprint_templates):
            if existing.driver == driver.name:
                db.session.delete(existing)

    for template, quality in templates:
        db.session.add(
            FingerprintTemplate(
                employee_id=employee.id,
                template=template,
                driver=driver.name,
                position=position,
                quality=quality,
                created_by_id=admin_id,
            )
        )
    db.session.commit()
    return EnrolOutcome(
        True,
        "enrolled",
        f"Enrolled {len(templates)} sample(s) for {employee.full_name}.",
        added=len(templates),
    )


def remove_templates(employee: Employee, *, driver_name: str | None = None) -> int:
    """Delete an employee's fingerprint templates. Returns how many went."""
    removed = 0
    for template in list(employee.fingerprint_templates):
        if driver_name is None or template.driver == driver_name:
            db.session.delete(template)
            removed += 1
    db.session.commit()
    return removed


# --------------------------------------------------------------------------
# Transport helpers
# --------------------------------------------------------------------------
def decode_template(raw: str) -> bytes:
    """Decode a base64 template from the reader agent."""
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001 - any malformed input is one error
        raise FingerprintError("bad_template", "That fingerprint could not be read.") from None
    if not data or len(data) > 4096:
        raise FingerprintError("bad_template", "That fingerprint was the wrong size.")
    return data


def encode_template(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
