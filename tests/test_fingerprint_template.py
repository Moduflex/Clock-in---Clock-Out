"""Template-based fingerprint readers: matching policy, enrolment, clocking.

These are the readers where *we* do the matching, so the rules that matter are
the refusals. A wrong match writes a wrong row into somebody's pay, so the
threshold, the margin and the vendor-isolation are all pinned here.

The simulator driver stands in for the reader, which keeps the suite hardware
free and deterministic.
"""

from __future__ import annotations

import pytest

from app.models import METHOD_FINGER, AttendanceEvent, FingerprintTemplate
from app.services.fingerprint import (
    FingerprintError,
    SimulatorDriver,
    decode_template,
    encode_template,
    enrol,
    identify,
    remove_templates,
    reset_drivers,
)

from .conftest import make_employee

TOKEN = "test-kiosk-token"


@pytest.fixture
def driver():
    reset_drivers()
    yield SimulatorDriver()
    reset_drivers()


def _template(finger: str) -> tuple[bytes, float | None]:
    return SimulatorDriver.template_for(finger), 80.0


def _enrol(db, employee, finger: str, driver, **kwargs):
    return enrol(employee, [_template(finger)], driver=driver, **kwargs)


# --- matching -----------------------------------------------------------------
def test_enrolled_finger_is_identified(db, driver):
    employee = make_employee(db)
    assert _enrol(db, employee, "alice-right-index", driver).ok

    outcome = identify(SimulatorDriver.template_for("alice-right-index"), driver=driver)
    assert outcome.accepted
    assert outcome.employee_id == employee.id
    assert outcome.score == pytest.approx(1.0)


def test_unknown_finger_is_refused(db, driver):
    employee = make_employee(db)
    _enrol(db, employee, "alice-right-index", driver)

    outcome = identify(SimulatorDriver.template_for("zzzzzzzzzzzz"), driver=driver)
    assert not outcome.accepted
    assert outcome.reason == "below_threshold"


def test_nobody_enrolled_is_reported_distinctly(db, driver):
    make_employee(db)
    outcome = identify(SimulatorDriver.template_for("anything"), driver=driver)
    assert outcome.reason == "no_templates"


def _add_template_directly(db, employee, finger: str, driver_name="simulator"):
    """Insert a template past the enrolment checks, to test the matcher alone."""
    db.session.add(
        FingerprintTemplate(
            employee_id=employee.id,
            template=SimulatorDriver.template_for(finger),
            driver=driver_name,
        )
    )
    db.session.commit()


def test_two_similar_fingers_refuse_rather_than_guess(db, driver):
    """An ambiguous reading must never be resolved by picking the top score."""
    alice = make_employee(db)
    bob = make_employee(db, ref="E002", first="Bob", last="Ward")
    # Inserted directly: enrolment would (rightly) refuse the second as a
    # duplicate, and what is under test here is the matcher, not that check.
    _add_template_directly(db, alice, "shared-prefix-aa")
    _add_template_directly(db, bob, "shared-prefix-ab")

    # Scores the same against both, so no margin separates them.
    outcome = identify(SimulatorDriver.template_for("shared-prefix-a"), driver=driver)
    assert not outcome.accepted
    assert outcome.reason == "ambiguous"
    assert outcome.margin < 0.05


def test_a_finger_too_like_an_enrolled_one_is_refused_at_enrolment(db, driver):
    """The other side of the same coin: near-duplicates never both get in.

    Two people whose fingers the reader cannot tell apart would be able to clock
    as each other, so the second enrolment is refused and a human is told.
    """
    alice = make_employee(db)
    bob = make_employee(db, ref="E002", first="Bob", last="Ward")
    _enrol(db, alice, "shared-prefix-a", driver)

    outcome = _enrol(db, bob, "shared-prefix-b", driver)
    assert not outcome.ok
    assert outcome.code == "already_enrolled"
    assert db.session.query(FingerprintTemplate).count() == 1


def test_several_fingers_per_person_only_help(db, driver):
    employee = make_employee(db)
    enrol(
        employee,
        [_template("alice-left-thumb"), _template("alice-right-index")],
        driver=driver,
    )
    for finger in ("alice-left-thumb", "alice-right-index"):
        outcome = identify(SimulatorDriver.template_for(finger), driver=driver)
        assert outcome.employee_id == employee.id


def test_inactive_employee_is_not_a_candidate(db, driver):
    employee = make_employee(db)
    _enrol(db, employee, "alice-right-index", driver)
    employee.is_active = False
    db.session.commit()

    outcome = identify(SimulatorDriver.template_for("alice-right-index"), driver=driver)
    assert not outcome.accepted
    assert outcome.reason == "no_templates"


def test_templates_from_another_vendor_are_never_compared(db, driver):
    """ZKTeco bytes must not be scored against DigitalPersona bytes."""
    employee = make_employee(db)
    db.session.add(
        FingerprintTemplate(
            employee_id=employee.id,
            template=SimulatorDriver.template_for("alice-right-index"),
            driver="some-other-vendor",
        )
    )
    db.session.commit()

    outcome = identify(SimulatorDriver.template_for("alice-right-index"), driver=driver)
    assert outcome.reason == "no_templates"


def test_threshold_and_margin_are_configurable(db, driver, app):
    alice = make_employee(db)
    _enrol(db, alice, "abcdefghij", driver)

    # A partial finger: 5 of 10 characters agree, so it scores 0.5.
    probe = SimulatorDriver.template_for("abcde12345")
    assert not identify(probe, driver=driver).accepted  # default threshold 0.60
    assert identify(probe, driver=driver, threshold=0.4, margin=0.0).accepted


# --- enrolment ----------------------------------------------------------------
def test_enrolling_somebody_elses_finger_is_refused(db, driver):
    """Otherwise one person could clock in under two names."""
    alice = make_employee(db)
    bob = make_employee(db, ref="E002", first="Bob", last="Ward")
    _enrol(db, alice, "alice-right-index", driver)

    outcome = _enrol(db, bob, "alice-right-index", driver)
    assert not outcome.ok
    assert outcome.code == "already_enrolled"
    assert "Alice Turner" in outcome.message
    assert db.session.query(FingerprintTemplate).count() == 1


def test_enrol_records_which_finger_and_driver(db, driver):
    employee = make_employee(db)
    _enrol(db, employee, "alice-right-index", driver, position=2)

    row = db.session.query(FingerprintTemplate).one()
    assert row.position == 2
    assert row.position_name == "Right index"
    assert row.driver == "simulator"
    assert row.quality == pytest.approx(80.0)


def test_enrol_with_no_samples_is_refused(db, driver):
    employee = make_employee(db)
    outcome = enrol(employee, [], driver=driver)
    assert not outcome.ok
    assert outcome.code == "no_samples"


def test_replace_drops_the_old_samples(db, driver):
    employee = make_employee(db)
    _enrol(db, employee, "alice-old-finger", driver)
    _enrol(db, employee, "alice-new-finger", driver, replace_existing=True)

    row = db.session.query(FingerprintTemplate).one()
    assert row.template == SimulatorDriver.template_for("alice-new-finger")


def test_removing_templates_and_deleting_an_employee(db, driver):
    employee = make_employee(db)
    enrol(employee, [_template("a-finger"), _template("b-finger")], driver=driver)
    assert remove_templates(employee) == 2
    assert db.session.query(FingerprintTemplate).count() == 0

    _enrol(db, employee, "another-finger", driver)
    db.session.delete(employee)
    db.session.commit()
    assert db.session.query(FingerprintTemplate).count() == 0


# --- transport ----------------------------------------------------------------
def test_template_round_trips_through_base64():
    data = b"\x00\x01\x02 a template \xff"
    assert decode_template(encode_template(data)) == data


def test_malformed_or_oversized_templates_are_rejected():
    for bad in ("not base64!!", "", encode_template(b"x" * 5000)):
        with pytest.raises(FingerprintError):
            decode_template(bad)


# --- the clocking endpoint ----------------------------------------------------
def _post(client, finger: str, **extra):
    payload = {
        "template": encode_template(SimulatorDriver.template_for(finger)),
        **extra,
    }
    return client.post(
        "/api/kiosk/fingerprint/verify", json=payload, headers={"X-Kiosk-Token": TOKEN}
    )


def test_verify_endpoint_clocks_the_matched_employee(client, db, driver):
    employee = make_employee(db)
    _enrol(db, employee, "alice-right-index", driver)

    response = _post(client, "alice-right-index")
    assert response.status_code == 200
    assert response.json["ok"] is True
    assert response.json["employee"]["name"] == "Alice Turner"
    assert response.json["direction"] == "in"
    assert response.json["confidence"] == pytest.approx(1.0)

    event = db.session.query(AttendanceEvent).one()
    assert event.method == METHOD_FINGER


def test_verify_endpoint_refuses_an_unknown_finger(client, db, driver):
    employee = make_employee(db)
    _enrol(db, employee, "alice-right-index", driver)

    response = _post(client, "totally-different")
    assert response.json["ok"] is False
    assert response.json["code"] == "below_threshold"
    assert db.session.query(AttendanceEvent).count() == 0


def test_verify_endpoint_needs_the_kiosk_token(client, db, driver):
    employee = make_employee(db)
    _enrol(db, employee, "alice-right-index", driver)

    response = client.post(
        "/api/kiosk/fingerprint/verify",
        json={"template": encode_template(SimulatorDriver.template_for("alice-right-index"))},
        headers={"X-Kiosk-Token": "wrong"},
    )
    assert response.status_code == 403
    assert db.session.query(AttendanceEvent).count() == 0


def test_verify_endpoint_rejects_junk(client, db, driver):
    make_employee(db)
    assert client.post(
        "/api/kiosk/fingerprint/verify", json={}, headers={"X-Kiosk-Token": TOKEN}
    ).status_code == 400
    assert client.post(
        "/api/kiosk/fingerprint/verify",
        json={"template": "not base64!!"},
        headers={"X-Kiosk-Token": TOKEN},
    ).status_code == 400
    assert client.post(
        "/api/kiosk/fingerprint/verify",
        json={"template": encode_template(b"x"), "direction": "sideways"},
        headers={"X-Kiosk-Token": TOKEN},
    ).status_code == 400


def test_a_client_cannot_name_the_employee_to_clock(client, db, driver):
    """The caller supplies a fingerprint, never an identity."""
    alice = make_employee(db)
    bob = make_employee(db, ref="E002", first="Bob", last="Ward")
    _enrol(db, alice, "alice-right-index", driver)

    response = client.post(
        "/api/kiosk/fingerprint/verify",
        json={
            "template": encode_template(SimulatorDriver.template_for("alice-right-index")),
            "employee_id": bob.id,  # ignored
        },
        headers={"X-Kiosk-Token": TOKEN},
    )
    assert response.json["employee"]["name"] == "Alice Turner"
