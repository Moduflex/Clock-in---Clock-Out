"""What the login rate limit is allowed to spend itself on.

The limit exists to slow password guessing. Spent on anything else it locks
real people out of their own back office, and it does so with a bare "Too many
attempts" page that looks exactly like the password having been wrong - so the
failure is easy to misread and hard to trace.
"""

from __future__ import annotations

import pytest

from app import create_app, _trust_proxy_headers
from app.extensions import db as _db
from app.models import AdminUser
from app.security import reset_rate_limits


def _sign_in(client, password, ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return client.post(
        "/login",
        data={"username": "office", "password": password},
        headers=headers,
    )


def test_opening_the_login_page_is_never_rate_limited(client, admin):
    """Reloading a form is not a sign-in attempt."""
    for _ in range(30):
        assert client.get("/login").status_code == 200


def test_signing_in_correctly_does_not_spend_the_allowance(client, admin):
    """Or an office where several people share one address locks itself out."""
    for _ in range(15):
        response = _sign_in(client, "correct-horse-battery")
        assert response.status_code == 302
        client.post("/logout")


def test_repeated_wrong_passwords_are_stopped(client, admin, app):
    limit = app.config["LOGIN_RATE_LIMIT"]

    for _ in range(limit):
        assert _sign_in(client, "wrong").status_code == 401

    blocked = _sign_in(client, "wrong")
    assert blocked.status_code == 429


def test_the_right_password_is_refused_once_locked_out(client, admin, app):
    """The lockout is the point; it must not be walked past by guessing right."""
    for _ in range(app.config["LOGIN_RATE_LIMIT"]):
        _sign_in(client, "wrong")

    assert _sign_in(client, "correct-horse-battery").status_code == 429


# --- who "the caller" is, behind a load balancer ------------------------------
# Every request on a hosting platform arrives from the platform's balancer. If
# that is taken as the client address, all users share one bucket and one person
# guessing badly locks out everybody else.
@pytest.fixture
def proxied_app():
    application = create_app("testing")
    application.config["TRUSTED_PROXY_COUNT"] = 1
    _trust_proxy_headers(application)

    with application.app_context():
        _db.create_all()
        user = AdminUser(username="office")
        user.set_password("correct-horse-battery")
        _db.session.add(user)
        _db.session.commit()
        yield application
        _db.session.remove()
        _db.drop_all()
    reset_rate_limits()


def test_one_bad_guest_does_not_lock_out_everybody_else(proxied_app):
    client = proxied_app.test_client()
    for _ in range(proxied_app.config["LOGIN_RATE_LIMIT"]):
        _sign_in(client, "wrong", ip="198.51.100.4")

    assert _sign_in(client, "wrong", ip="198.51.100.4").status_code == 429
    assert _sign_in(client, "correct-horse-battery", ip="203.0.113.99").status_code == 302


def test_forwarded_headers_are_ignored_without_a_proxy_in_front(client, admin, app):
    """On the LAN nothing strips these, so trusting them would hand out a
    free pass: a new address per request and the limit never bites."""
    for _ in range(app.config["LOGIN_RATE_LIMIT"]):
        _sign_in(client, "wrong", ip="198.51.100.4")

    assert _sign_in(client, "wrong", ip="10.9.8.7").status_code == 429
