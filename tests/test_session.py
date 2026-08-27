"""Staying signed in.

A back office is used from a laptop that roams between wifi points and, on a
hosting platform, reaches the app through a load balancer whose address is not
the browser's. Flask-Login can read either as "this is a different person" and
throw the session away. It does so silently - the user is simply anonymous
again - so a correct password appears to bounce straight off the login form
with no error at all. These tests pin down that it does not.
"""

from __future__ import annotations

import pytest

from app import create_app
from app.extensions import db as _db
from app.models import AdminUser
from app.security import reset_rate_limits

PASSWORD = "correct-horse-battery"


@pytest.fixture
def served_app():
    """An app answering requests the way a real server does.

    Deliberately not the shared ``client`` fixture. That one holds an app
    context open around the whole test, and Flask's ``g`` lives on the app
    context - so Flask-Login's cached user survives from one request to the
    next, the session is never re-read, and session protection never runs.
    Tests written through it pass no matter what the configuration says.
    ``test_strong_protection_still_drops_the_session`` is the canary: if these
    ever start passing vacuously, that one goes green when it should be red.
    """
    application = create_app("testing")
    with application.app_context():
        _db.create_all()
        user = AdminUser(username="office")
        user.set_password(PASSWORD)
        _db.session.add(user)
        _db.session.commit()

    yield application

    with application.app_context():
        _db.session.remove()
        _db.drop_all()
    reset_rate_limits()


def _sign_in(client, password=PASSWORD):
    return client.post("/login", data={"username": "office", "password": password})


def test_the_default_is_not_the_setting_that_drops_sessions(served_app):
    assert served_app.config["SESSION_PROTECTION"] == "basic"


def test_signing_in_reaches_the_dashboard(served_app):
    client = served_app.test_client()

    assert _sign_in(client).status_code == 302
    assert client.get("/admin/").status_code == 200


def test_a_changed_client_address_does_not_sign_you_out(served_app):
    """The regression: a load balancer, or a laptop swapping wifi for 4G."""
    client = served_app.test_client()
    _sign_in(client)

    response = client.get("/admin/", headers={"X-Forwarded-For": "203.0.113.9"})

    assert response.status_code == 200, "signed out by a change of address"


def test_a_changed_browser_string_does_not_sign_you_out(served_app):
    client = served_app.test_client()
    _sign_in(client)

    response = client.get("/admin/", headers={"User-Agent": "Something/2.0"})

    assert response.status_code == 200


def test_strong_protection_still_drops_the_session(served_app):
    """What the default avoids - and the canary for the fixture above.

    Left reachable for a deployment on one fixed network, where an address
    changing mid-session really is worth being suspicious about.
    """
    served_app.config["SESSION_PROTECTION"] = "strong"
    client = served_app.test_client()
    _sign_in(client)

    response = client.get("/admin/", headers={"X-Forwarded-For": "203.0.113.9"})

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_signing_out_still_works(served_app):
    client = served_app.test_client()
    _sign_in(client)

    client.post("/logout")

    assert client.get("/admin/").status_code == 302
