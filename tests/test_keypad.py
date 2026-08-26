"""Clocking by typing a payroll number at the kiosk.

This path recognises nobody, which is the whole point of it: it is what somebody
uses when the camera cannot see them. A payroll number is an identifier and not
a secret - it is printed on payslips and known to colleagues - so the things
worth testing are that it goes through exactly the same alternation and cooldown
rules as a face scan, that it refuses the cases it should, and above all that
every entry it writes is marked ``keypad`` so the office can tell a typed entry
from a recognised one.
"""

from __future__ import annotations

import datetime as dt

from app.models import (
    DIRECTION_IN,
    DIRECTION_OUT,
    METHOD_KEYPAD,
    PAY_SALARY,
    AttendanceEvent,
    utcnow,
)
from app.services import attendance

from .conftest import make_employee

TOKEN = "test-kiosk-token"


def _clock(client, reference, token=TOKEN, **extra):
    return client.post(
        "/api/kiosk/payroll",
        json={"payroll_ref": reference, **extra},
        headers={"X-Kiosk-Token": token},
    )


# --- the happy path -----------------------------------------------------------
def test_a_typed_number_clocks_in_then_out(client, db, app):
    employee = make_employee(db, ref="E042")

    first = _clock(client, "E042")
    assert first.status_code == 200
    assert first.json["ok"] is True
    assert first.json["direction"] == "in"
    assert first.json["recorded"] is True
    assert first.json["employee"]["name"] == "Alice Turner"
    # Nothing was matched against anything, so there is no score to report.
    assert first.json["confidence"] is None

    event = db.session.query(AttendanceEvent).one()
    assert event.direction == DIRECTION_IN
    assert event.device_label == app.config["KIOSK_DEVICE_LABEL"]

    second = _clock(client, "E042", direction="out")
    assert second.json["direction"] == "out"
    assert second.json["recorded"] is True
    assert db.session.query(AttendanceEvent).count() == 2


def test_the_entry_is_marked_keypad_not_face(client, db):
    """The audit trail has to say this was typed, not recognised.

    Everything downstream leans on it: the dashboard's activity feed, the
    timesheet detail CSV, and anybody asking later how a given entry was made.
    """
    make_employee(db, ref="E042")
    _clock(client, "E042")

    assert db.session.query(AttendanceEvent).one().method == METHOD_KEYPAD


def test_alternation_follows_the_last_entry_whatever_made_it(client, db):
    """A face scan in, a typed number out. One log, one set of rules."""
    employee = make_employee(db, ref="E042")
    # An hour ago, so the cooldown is long past and what is being tested is the
    # alternation rather than the duplicate guard.
    attendance.record_clock(
        employee,
        direction=DIRECTION_IN,
        cooldown_seconds=0,
        occurred_at=utcnow() - dt.timedelta(hours=1),
    )

    reply = _clock(client, "E042")

    assert reply.json["direction"] == DIRECTION_OUT


# --- typed on a workshop touchscreen ------------------------------------------
def test_case_and_stray_spaces_are_forgiven(client, db):
    """Refusing " e042 " would only teach people the keypad does not work."""
    make_employee(db, ref="E042")

    assert _clock(client, "  e042 ").json["ok"] is True


def test_an_unknown_number_is_refused_and_nothing_is_written(client, db):
    make_employee(db, ref="E042")

    reply = _clock(client, "E999")

    assert reply.status_code == 200
    assert reply.json["ok"] is False
    assert reply.json["code"] == "ref_unknown"
    assert db.session.query(AttendanceEvent).count() == 0


def test_a_blank_number_is_rejected(client, db):
    assert _clock(client, "   ").status_code == 400
    assert _clock(client, "").status_code == 400


def test_an_absurdly_long_entry_is_rejected(client, db):
    """Longer than the column: a stuck key or a paste, not a typed number."""
    assert _clock(client, "9" * 33).status_code == 400


def test_an_inactive_employee_cannot_clock(client, db):
    make_employee(db, ref="E042", is_active=False)

    reply = _clock(client, "E042")

    assert reply.json["ok"] is False
    assert reply.json["code"] == "employee_inactive"
    assert db.session.query(AttendanceEvent).count() == 0


def test_a_salaried_employee_can_still_clock(client, db):
    """Pay basis decides what a wage sheet says, not who may use the door."""
    make_employee(db, ref="E042", pay_basis=PAY_SALARY)

    assert _clock(client, "E042").json["ok"] is True


def test_the_hidden_record_cannot_be_clocked_by_typing_its_number(client, db):
    """It is filtered out of every list and count; the keypad is no exception."""
    make_employee(db, ref="E000", first="Claude", last="AI")

    reply = _clock(client, "E000")

    assert reply.json["ok"] is False
    assert reply.json["code"] == "ref_unknown"


# --- the same guards as every other kiosk endpoint ----------------------------
def test_the_kiosk_token_is_required(client, db):
    make_employee(db, ref="E042")

    assert _clock(client, "E042", token="wrong").status_code == 403
    assert db.session.query(AttendanceEvent).count() == 0


def test_the_cooldown_applies_just_as_it_does_to_a_face_scan(client, db):
    """Two presses of Clock in a row must not clock somebody in and out."""
    make_employee(db, ref="E042")

    _clock(client, "E042")
    second = _clock(client, "E042")

    assert second.json["recorded"] is False
    assert db.session.query(AttendanceEvent).count() == 1


def test_an_unknown_direction_is_rejected(client, db):
    make_employee(db, ref="E042")

    assert _clock(client, "E042", direction="sideways").status_code == 400


# --- the switch ---------------------------------------------------------------
def test_the_keypad_can_be_switched_off(client, db, app):
    """Some floors will not accept an identifier standing in for a credential."""
    make_employee(db, ref="E042")
    app.config["KIOSK_KEYPAD_MODE"] = False

    reply = _clock(client, "E042")

    assert reply.status_code == 403
    assert reply.json["code"] == "keypad_disabled"
    assert db.session.query(AttendanceEvent).count() == 0


def test_the_kiosk_page_hides_the_box_when_it_is_off(client, app):
    app.config["KIOSK_KEYPAD_MODE"] = False

    body = client.get("/").data.decode("utf-8")

    assert "keypad-input" not in body


def test_the_kiosk_page_shows_the_box_when_it_is_on(client):
    body = client.get("/").data.decode("utf-8")

    assert 'id="keypad-input"' in body
    assert "payroll number" in body.lower()


# --- the lookup itself --------------------------------------------------------
def test_find_by_payroll_ref_matches_case_insensitively(db):
    employee = make_employee(db, ref="E042")

    assert attendance.find_by_payroll_ref("e042").id == employee.id
    assert attendance.find_by_payroll_ref(" E042  ").id == employee.id


def test_find_by_payroll_ref_returns_none_for_nothing_useful(db):
    make_employee(db, ref="E042")

    assert attendance.find_by_payroll_ref("") is None
    assert attendance.find_by_payroll_ref("   ") is None
    assert attendance.find_by_payroll_ref("E999") is None
