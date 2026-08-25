"""Pay rates: encrypted at rest, readable back, and never a plaintext number."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.payrates import (
    PayRateError,
    decrypt_rate,
    encrypt_rate,
    generate_key,
    parse_rate,
    rate_text,
)

from .conftest import make_employee


# --- parsing ------------------------------------------------------------------
def test_a_rate_is_parsed_to_the_penny():
    assert parse_rate("14.50") == Decimal("14.5000")
    assert parse_rate("14") == Decimal("14.0000")


def test_a_pasted_rate_survives_its_pound_sign_and_commas():
    assert parse_rate("£14.50") == Decimal("14.5000")
    assert parse_rate(" 1,000 ") == Decimal("1000.0000")


def test_blank_means_no_rate_recorded():
    for blank in (None, "", "   "):
        assert parse_rate(blank) is None


def test_a_rate_that_is_not_a_number_is_rejected():
    with pytest.raises(PayRateError):
        parse_rate("fourteen fifty")


def test_a_salary_typed_into_the_hourly_box_is_rejected():
    """£2,400 an hour is a monthly figure in the wrong field, not a pay rise."""
    with pytest.raises(PayRateError):
        parse_rate("2400")


def test_rate_text_renders_two_places():
    assert rate_text(Decimal("14.5000")) == "14.50"
    assert rate_text(None) == ""


# --- encryption ---------------------------------------------------------------
def test_a_rate_survives_a_round_trip(app):
    with app.app_context():
        assert decrypt_rate(encrypt_rate("14.50")) == Decimal("14.5000")


def test_the_stored_bytes_do_not_contain_the_rate(app):
    """The whole point: a database dump must not list what anybody earns."""
    with app.app_context():
        blob = encrypt_rate("14.50")
    assert b"14.50" not in blob
    assert blob.startswith(b"gAAAAA")  # a Fernet token, not a number


def test_the_same_rate_encrypts_differently_each_time(app):
    """Identical ciphertext would leak who is on the same money as whom."""
    with app.app_context():
        assert encrypt_rate("14.50") != encrypt_rate("14.50")


def test_encrypting_nothing_stores_nothing(app):
    with app.app_context():
        assert encrypt_rate(None) is None
        assert encrypt_rate("") is None
    assert decrypt_rate(None) is None


def test_a_rate_written_with_another_key_reads_as_blank(app):
    """A changed key must blank the page, not return a 500 to the whole office."""
    with app.app_context():
        blob = encrypt_rate("14.50")

    app.config["PAYROLL_KEY"] = generate_key()
    app.extensions.pop("_payroll_fernet", None)
    with app.app_context():
        assert decrypt_rate(blob) is None


def test_a_configured_key_is_used_in_preference_to_the_derived_one(app):
    key = generate_key()
    app.config["PAYROLL_KEY"] = key
    app.extensions.pop("_payroll_fernet", None)
    with app.app_context():
        blob = encrypt_rate("19.25")
        assert decrypt_rate(blob) == Decimal("19.2500")


def test_a_nonsense_key_is_reported_rather_than_silently_ignored(app):
    app.config["PAYROLL_KEY"] = "not-a-real-key"
    app.extensions.pop("_payroll_fernet", None)
    with app.app_context():
        with pytest.raises(RuntimeError, match="PAYROLL_KEY"):
            encrypt_rate("14.50")


# --- on the employee record ---------------------------------------------------
def test_rates_round_trip_through_the_employee_card(app, db):
    with app.app_context():
        employee = make_employee(db)
        employee.basic_rate = "14.50"
        employee.overtime_rate = "21.75"
        db.session.commit()

        fresh = db.session.get(type(employee), employee.id)
        assert fresh.basic_rate == Decimal("14.5000")
        assert fresh.overtime_rate == Decimal("21.7500")
        # And the column really does hold ciphertext.
        assert b"14.5" not in fresh.basic_rate_enc


def test_clearing_a_rate_empties_the_column(app, db):
    with app.app_context():
        employee = make_employee(db)
        employee.basic_rate = "14.50"
        db.session.commit()

        employee.basic_rate = ""
        db.session.commit()
        assert employee.basic_rate_enc is None
        assert employee.basic_rate is None


def test_an_employee_with_no_rate_reads_as_none(db):
    employee = make_employee(db)
    assert employee.basic_rate is None
    assert employee.overtime_rate is None
