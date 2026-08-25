"""Pay rates, encrypted at rest.

Hourly rates are the most sensitive non-biometric field in this database: a
leaked backup that lists what everybody earns is a serious problem, and read
access to MySQL is wider than read access to payroll. So the rate columns hold
ciphertext, not numbers, and the key lives in ``.env`` rather than in the
database - somebody who walks off with a ``.sql`` dump gets nothing.

**Why this encrypts rather than hashes.** A hash is one way. A hashed pay rate
could never be shown on the employee's card or multiplied by their hours, which
is the whole point of storing it; and because an hourly rate has only a few
thousand plausible values (£11.44 to £40.00 in penny steps), a hash of one is
recovered by trying them all in well under a second. Hashing here would destroy
the feature *and* provide no real secrecy. Encryption gives the property that
was actually wanted: unreadable in the database, readable by the application.

Fernet is AES-128-CBC with an HMAC, from ``cryptography`` - already a dependency
for the MySQL driver, so this adds nothing to install or maintain.
"""

from __future__ import annotations

import base64
import hashlib
from decimal import Decimal, InvalidOperation

from flask import current_app

# Rates are stored to the penny. Four places leaves room for a rate quoted per
# thousand or an agency uplift without rounding it away at rest.
RATE_PLACES = 4
# Nobody in a manufacturing business is on £0/hour or £1,000/hour; a rate
# outside this is a typo (a monthly salary in the hourly box, most likely).
MIN_RATE = Decimal("0")
MAX_RATE = Decimal("1000")

_KEY_ATTR = "_payroll_fernet"


class PayRateError(ValueError):
    """A rate that cannot be stored - out of range, or not a number."""


def generate_key() -> str:
    """A fresh Fernet key, ready to paste after ``PAYROLL_KEY=`` in .env."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("ascii")


def _derive_from_secret(secret: str) -> bytes:
    """A Fernet key derived from SECRET_KEY, used when PAYROLL_KEY is unset.

    A convenience so a fresh install works without a second key to manage, and
    documented as such: rotating SECRET_KEY then makes stored rates unreadable,
    which is exactly why production should set its own PAYROLL_KEY.
    """
    digest = hashlib.sha256(f"payroll-rate-key:{secret}".encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def get_cipher(app=None):
    """The Fernet cipher for this application, built once and cached on it."""
    from cryptography.fernet import Fernet

    app = app or current_app
    cipher = app.extensions.get(_KEY_ATTR)
    if cipher is not None:
        return cipher

    configured = (app.config.get("PAYROLL_KEY") or "").strip()
    if configured:
        try:
            cipher = Fernet(configured.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "PAYROLL_KEY is not a valid Fernet key. Generate one with "
                "'flask payroll-key' and paste it into .env."
            ) from exc
    else:
        cipher = Fernet(_derive_from_secret(app.config["SECRET_KEY"]))

    app.extensions[_KEY_ATTR] = cipher
    return cipher


def parse_rate(raw) -> Decimal | None:
    """Validate an operator-entered rate. Blank means "no rate recorded"."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    text = str(raw).strip().lstrip("£").replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise PayRateError("Enter a rate like 14.50.") from exc
    if not MIN_RATE <= value <= MAX_RATE:
        raise PayRateError(f"Enter an hourly rate between {MIN_RATE:g} and {MAX_RATE:g}.")
    return value.quantize(Decimal(10) ** -RATE_PLACES)


def encrypt_rate(value, *, app=None) -> bytes | None:
    """Encrypt a rate for storage. None (or blank) clears the stored value."""
    rate = value if isinstance(value, Decimal) else parse_rate(value)
    if rate is None:
        return None
    return get_cipher(app).encrypt(format(rate, "f").encode("ascii"))


def decrypt_rate(blob: bytes | None, *, app=None) -> Decimal | None:
    """Read a stored rate back, or None if there is none.

    Returns None rather than raising when the ciphertext will not open, which
    happens if the key has changed. A payroll page that renders a blank rate is
    recoverable; one that returns a 500 to everybody is not.
    """
    if not blob:
        return None
    from cryptography.fernet import InvalidToken

    try:
        plain = get_cipher(app).decrypt(bytes(blob))
    except (InvalidToken, RuntimeError):
        return None
    try:
        return Decimal(plain.decode("ascii"))
    except (InvalidOperation, UnicodeDecodeError):
        return None


def rate_text(value: Decimal | None) -> str:
    """A rate rendered for a form or a page: 14.5000 -> "14.50"."""
    if value is None:
        return ""
    return f"{value:.2f}"
