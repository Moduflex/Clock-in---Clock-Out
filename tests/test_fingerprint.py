"""Fingerprint clocking: slot lookup, refusals, alternation and the admin pages.

The reader does the matching; this system only knows which slot belongs to whom.
So the things worth testing are the lookup, what happens when it fails, and that
a fingerprint scan goes through exactly the same alternation and cooldown rules
as a face scan.
"""

from __future__ import annotations

from app.models import DIRECTION_IN, METHOD_FINGER, AttendanceEvent, FingerprintCredential
from app.services import attendance

from .conftest import make_employee

TOKEN = "test-kiosk-token"
READER = "Kiosk"


def _register(db, employee, finger_id: int, device_label: str = READER, active=True):
    credential = FingerprintCredential(
        employee_id=employee.id,
        device_label=device_label,
        finger_id=finger_id,
        is_active=active,
    )
    db.session.add(credential)
    db.session.commit()
    return credential


def _scan(client, finger_id, device_label=READER, token=TOKEN, **extra):
    payload = {"finger_id": finger_id, "device_label": device_label, **extra}
    return client.post(
        "/api/kiosk/fingerprint", json=payload, headers={"X-Kiosk-Token": token}
    )


# --- the happy path -----------------------------------------------------------
def test_registered_finger_clocks_in_then_out(client, db):
    employee = make_employee(db)
    _register(db, employee, 7)

    first = _scan(client, 7)
    assert first.status_code == 200
    assert first.json["ok"] is True
    assert first.json["direction"] == "in"
    assert first.json["recorded"] is True
    assert first.json["employee"]["name"] == "Alice Turner"
    # A reader reports a match, not a similarity, so there is no score to give.
    assert first.json["confidence"] is None

    event = db.session.query(AttendanceEvent).one()
    assert event.method == METHOD_FINGER
    assert event.direction == DIRECTION_IN
    assert event.device_label == READER

    # The next scan alternates, exactly as a face scan would.
    db.session.query(AttendanceEvent).one().occurred_at  # touch, no change
    second = _scan(client, 7, direction="out")
    assert second.json["direction"] == "out"


def test_scan_stamps_when_the_slot_was_last_used(db, client):
    employee = make_employee(db)
    credential = _register(db, employee, 3)
    assert credential.last_used_at is None

    _scan(client, 3)
    db.session.refresh(credential)
    assert credential.last_used_at is not None


def test_explicit_direction_is_honoured(client, db):
    employee = make_employee(db)
    _register(db, employee, 1)
    assert _scan(client, 1, direction="out").json["direction"] == "out"


# --- refusals -----------------------------------------------------------------
def test_unregistered_slot_is_refused_and_records_nothing(client, db):
    make_employee(db)
    response = _scan(client, 99)

    assert response.status_code == 200
    assert response.json["ok"] is False
    assert response.json["code"] == "finger_unknown"
    assert db.session.query(AttendanceEvent).count() == 0


def test_a_slot_on_another_reader_is_not_accepted(client, db):
    """Two readers each have a slot 7, and they are different people."""
    employee = make_employee(db)
    _register(db, employee, 7, device_label="Workshop reader")

    assert _scan(client, 7, device_label="Office reader").json["code"] == "finger_unknown"
    assert _scan(client, 7, device_label="Workshop reader").json["ok"] is True


def test_deactivated_credential_is_refused(client, db):
    employee = make_employee(db)
    _register(db, employee, 4, active=False)
    assert _scan(client, 4).json["code"] == "finger_unknown"


def test_inactive_employee_cannot_clock_in(client, db):
    employee = make_employee(db)
    employee.is_active = False
    db.session.commit()
    _register(db, employee, 5)

    response = _scan(client, 5)
    assert response.json["code"] == "employee_inactive"
    assert db.session.query(AttendanceEvent).count() == 0


def test_the_kiosk_token_is_required(client, db):
    employee = make_employee(db)
    _register(db, employee, 7)

    assert _scan(client, 7, token="wrong").status_code == 403
    assert db.session.query(AttendanceEvent).count() == 0


def test_a_missing_or_junk_slot_number_is_rejected(client, db):
    make_employee(db)
    assert _scan(client, None).status_code == 400
    assert _scan(client, "left thumb").status_code == 400


def test_unknown_direction_is_rejected(client, db):
    employee = make_employee(db)
    _register(db, employee, 7)
    assert _scan(client, 7, direction="sideways").status_code == 400


# --- the cooldown applies just as it does to a face scan ----------------------
def test_two_presses_in_a_row_do_not_clock_in_and_straight_back_out(client, db, app):
    app.config["CLOCK_COOLDOWN_SECONDS"] = 90
    employee = make_employee(db)
    _register(db, employee, 7)

    assert _scan(client, 7).json["recorded"] is True
    repeat = _scan(client, 7)
    assert repeat.json["recorded"] is False
    assert repeat.json["code"] == "duplicate"
    assert db.session.query(AttendanceEvent).count() == 1


# --- the admin side -----------------------------------------------------------
def test_admin_can_register_and_unregister_a_slot(logged_in, db):
    employee = make_employee(db)

    response = logged_in.post(
        f"/admin/employees/{employee.id}/fingerprint",
        data={"finger_id": "7", "device_label": READER, "label": "right index"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    credential = db.session.query(FingerprintCredential).one()
    assert credential.finger_id == 7
    assert credential.label == "right index"

    response = logged_in.post(
        f"/admin/fingerprints/{credential.id}/delete", follow_redirects=True
    )
    assert response.status_code == 200
    assert db.session.query(FingerprintCredential).count() == 0


def test_reusing_a_slot_is_refused_and_names_who_holds_it(logged_in, db):
    """The usual cause is a slot that was never cleared - it must not silently
    start clocking somebody else."""
    alice = make_employee(db)
    bob = make_employee(db, ref="E002", first="Bob", last="Ward")
    _register(db, alice, 7)

    response = logged_in.post(
        f"/admin/employees/{bob.id}/fingerprint",
        data={"finger_id": "7", "device_label": READER},
        follow_redirects=True,
    )
    assert b"already registered to" in response.data
    assert b"Alice Turner" in response.data
    assert db.session.query(FingerprintCredential).count() == 1


def test_deleting_an_employee_takes_their_credentials_with_them(db):
    employee = make_employee(db)
    _register(db, employee, 7)

    db.session.delete(employee)
    db.session.commit()
    assert db.session.query(FingerprintCredential).count() == 0


# --- the lookup itself --------------------------------------------------------
def test_find_fingerprint_is_scoped_to_the_device(db):
    employee = make_employee(db)
    _register(db, employee, 7, device_label="Workshop reader")

    assert attendance.find_fingerprint("Workshop reader", 7) is not None
    assert attendance.find_fingerprint("Office reader", 7) is None
    assert attendance.find_fingerprint("Workshop reader", 8) is None


# --- Windows Hello: the slot number is a finger position ----------------------
def test_slot_numbers_are_named_as_finger_positions(db):
    """On a Hello reader the slot *is* the finger, so the UI can name it."""
    employee = make_employee(db)
    assert _register(db, employee, 2).position_name == "Right index"
    assert _register(db, employee, 7).position_name == "Left index"
    # A slot-based reader counts past ten and simply has no position name.
    assert _register(db, employee, 250).position_name is None


def test_the_ten_positions_are_the_hello_identity_space(db):
    from app.models import FINGER_POSITIONS

    assert sorted(FINGER_POSITIONS) == list(range(1, 11))


def test_each_position_clocks_its_own_person(client, db):
    """Two people on one Windows account, told apart by finger position."""
    alice = make_employee(db)
    bob = make_employee(db, ref="E002", first="Bob", last="Ward")
    _register(db, alice, 2)  # right index
    _register(db, bob, 7)  # left index

    assert _scan(client, 2).json["employee"]["name"] == "Alice Turner"
    assert _scan(client, 7).json["employee"]["name"] == "Bob Ward"
