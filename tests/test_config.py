"""Database TLS configuration.

Getting this wrong is silent: several plausible PyMySQL option combinations
connect in plaintext while looking as though they enabled encryption. These tests
pin down the exact arguments that were verified against a real server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import BASE_DIR, Config


def _config(**overrides) -> Config:
    config = Config()
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


# --- choosing a mode ----------------------------------------------------------
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_a_local_database_needs_no_tls(host):
    assert _config(MYSQL_HOST=host, MYSQL_SSL_MODE="").mysql_ssl_mode == "disabled"


@pytest.mark.parametrize(
    "host",
    [
        "db.example.com",
        "moduflex-do-user-1234-0.a.db.ondigitalocean.com",
        "10.0.0.15",
    ],
)
def test_a_remote_database_defaults_to_full_verification(host):
    """The default must be safe: biometric data must not cross a network in clear."""
    assert _config(MYSQL_HOST=host, MYSQL_SSL_MODE="").mysql_ssl_mode == "verify-identity"


def test_an_explicit_mode_is_respected():
    assert _config(MYSQL_HOST="db.example.com", MYSQL_SSL_MODE="required").mysql_ssl_mode == (
        "required"
    )
    assert _config(MYSQL_HOST="localhost", MYSQL_SSL_MODE="verify-identity").mysql_ssl_mode == (
        "verify-identity"
    )


def test_a_nonsense_mode_falls_back_to_the_safe_default():
    assert _config(MYSQL_HOST="db.example.com", MYSQL_SSL_MODE="sort-of").mysql_ssl_mode == (
        "verify-identity"
    )


# --- the connect arguments ----------------------------------------------------
def test_disabled_sends_no_ssl_arguments():
    assert _config(MYSQL_HOST="localhost", MYSQL_SSL_MODE="").mysql_connect_args == {}


def test_required_uses_the_form_that_actually_negotiates_tls():
    """Regression guard: ssl={} and ssl_verify_cert=False stay PLAINTEXT.

    Verified against MySQL 8.4 - only the nested truthy dict turns TLS on.
    """
    args = _config(MYSQL_HOST="db.example.com", MYSQL_SSL_MODE="required").mysql_connect_args
    assert args == {"ssl": {"ssl": True}}
    assert args["ssl"], "an empty ssl dict silently disables TLS"


def test_verify_identity_checks_certificate_and_hostname():
    args = _config(
        MYSQL_HOST="db.example.com", MYSQL_SSL_MODE="verify-identity"
    ).mysql_connect_args
    assert args["ssl"] == {"ssl": True}
    assert args["ssl_verify_cert"] is True
    assert args["ssl_verify_identity"] is True
    assert "ssl_ca" not in args


def test_a_ca_certificate_is_passed_through_when_given():
    args = _config(
        MYSQL_HOST="db.example.com",
        MYSQL_SSL_MODE="verify-identity",
        MYSQL_SSL_CA="C:/certs/ca-certificate.crt",
    ).mysql_connect_args
    assert args["ssl_ca"] == "C:/certs/ca-certificate.crt"


# --- supplying the CA a managed database signs with ---------------------------
# Without one of these, verify-identity checks the server against the machine's
# own trust store, which holds no managed provider's private CA - so the
# handshake fails with "self-signed certificate in certificate chain", and it
# fails on the first query rather than at start-up, surfacing as a 500 on the
# login page with nothing to connect it to the database configuration.
PEM = "-----BEGIN CERTIFICATE-----\nMIIBex\n-----END CERTIFICATE-----"


def _fresh(config: Config) -> Config:
    """Forget any certificate a previous test wrote; each process writes once."""
    Config._ca_pem_file = None
    return config


def test_a_relative_ca_path_is_anchored_to_the_project():
    """So a certificate committed to the repo works on a dyno and on Windows."""
    args = _config(
        MYSQL_HOST="db.example.com",
        MYSQL_SSL_MODE="verify-identity",
        MYSQL_SSL_CA="certs/do-ca.crt",
    ).mysql_connect_args

    assert Path(args["ssl_ca"]) == BASE_DIR / "certs" / "do-ca.crt"
    assert Path(args["ssl_ca"]).is_absolute()


def test_a_certificate_given_as_text_is_written_to_a_file():
    """Platforms with config vars and no filesystem paste the PEM instead."""
    config = _fresh(
        _config(
            MYSQL_HOST="db.example.com",
            MYSQL_SSL_MODE="verify-identity",
            MYSQL_SSL_CA_PEM=PEM,
        )
    )

    ca_file = Path(config.mysql_connect_args["ssl_ca"])

    assert ca_file.is_file()
    assert ca_file.read_text().strip() == PEM
    Config._ca_pem_file = None


def test_escaped_newlines_in_a_pasted_certificate_are_repaired():
    """A config var pasted carelessly arrives with literal backslash-n."""
    config = _fresh(
        _config(
            MYSQL_HOST="db.example.com",
            MYSQL_SSL_MODE="verify-identity",
            MYSQL_SSL_CA_PEM=PEM.replace("\n", "\\n"),
        )
    )

    text = Path(config.mysql_connect_args["ssl_ca"]).read_text()

    assert "\\n" not in text
    assert text.strip() == PEM
    Config._ca_pem_file = None


def test_a_base64_encoded_certificate_is_decoded():
    """Some platforms hand the certificate over base64-encoded, not as PEM.

    DigitalOcean App Platform's CA_CERT binding is one. Getting this wrong
    fails at the handshake with an error that says nothing about the
    certificate being unreadable.
    """
    import base64

    config = _fresh(
        _config(
            MYSQL_HOST="db.example.com",
            MYSQL_SSL_MODE="verify-identity",
            MYSQL_SSL_CA_PEM=base64.b64encode(PEM.encode()).decode(),
        )
    )

    assert Path(config.mysql_connect_args["ssl_ca"]).read_text().strip() == PEM
    Config._ca_pem_file = None


def test_an_unreadable_certificate_is_left_for_the_ssl_module_to_report():
    """Better a clear TLS error than a silently empty certificate file."""
    config = _fresh(
        _config(
            MYSQL_HOST="db.example.com",
            MYSQL_SSL_MODE="verify-identity",
            MYSQL_SSL_CA_PEM="not a certificate at all",
        )
    )

    written = Path(config.mysql_connect_args["ssl_ca"]).read_text()

    assert written.strip() == "not a certificate at all"
    Config._ca_pem_file = None


def test_a_path_wins_over_pasted_text_when_both_are_set():
    args = _fresh(
        _config(
            MYSQL_HOST="db.example.com",
            MYSQL_SSL_MODE="verify-identity",
            MYSQL_SSL_CA="C:/certs/ca-certificate.crt",
            MYSQL_SSL_CA_PEM=PEM,
        )
    ).mysql_connect_args

    assert args["ssl_ca"] == "C:/certs/ca-certificate.crt"
    Config._ca_pem_file = None


def test_required_mode_ignores_a_ca_because_it_does_not_verify():
    """The documented escape hatch: encrypt, do not verify. Nothing to check."""
    args = _config(
        MYSQL_HOST="db.example.com",
        MYSQL_SSL_MODE="required",
        MYSQL_SSL_CA="C:/certs/ca-certificate.crt",
    ).mysql_connect_args

    assert args == {"ssl": {"ssl": True}}
    assert "ssl_ca" not in args


# --- engine options -----------------------------------------------------------
def test_engine_options_carry_the_ssl_arguments_for_mysql():
    options = _config(MYSQL_HOST="db.example.com", MYSQL_SSL_MODE="").SQLALCHEMY_ENGINE_OPTIONS
    assert options["pool_pre_ping"] is True
    assert options["connect_args"]["ssl_verify_identity"] is True


def test_engine_options_omit_ssl_for_a_local_database():
    options = _config(MYSQL_HOST="localhost", MYSQL_SSL_MODE="").SQLALCHEMY_ENGINE_OPTIONS
    assert "connect_args" not in options


def test_sqlite_never_receives_mysql_connect_arguments():
    """The test suite runs on SQLite, which would reject PyMySQL's ssl kwargs."""
    from app.config import TestConfig

    options = TestConfig().SQLALCHEMY_ENGINE_OPTIONS
    assert "connect_args" not in options


def test_password_with_special_characters_is_url_encoded():
    uri = _config(MYSQL_PASSWORD="p@ss:w/rd?#", MYSQL_HOST="db.example.com").SQLALCHEMY_DATABASE_URI
    assert "p%40ss%3Aw%2Frd%3F%23" in uri
    assert "p@ss:w/rd" not in uri


# --- the production start-up guard --------------------------------------------
def test_production_refuses_plaintext_to_a_remote_database(monkeypatch):
    """Nothing should be able to quietly ship biometric data in the clear."""
    from app import create_app
    from app.config import ProductionConfig

    settings = ProductionConfig()
    settings.SECRET_KEY = "a-real-secret-key-for-this-test"
    settings.KIOSK_TOKEN = "a-real-kiosk-token"
    settings.MYSQL_HOST = "db.example.com"
    settings.MYSQL_SSL_MODE = "disabled"

    with pytest.raises(RuntimeError, match="unencrypted"):
        create_app(settings)


def test_production_starts_when_the_link_is_encrypted():
    from app import create_app
    from app.config import ProductionConfig

    settings = ProductionConfig()
    settings.SECRET_KEY = "a-real-secret-key-for-this-test"
    settings.KIOSK_TOKEN = "a-real-kiosk-token"
    settings.MYSQL_HOST = "db.example.com"
    settings.MYSQL_SSL_MODE = "verify-identity"

    app = create_app(settings)
    assert app.config["MYSQL_SSL_MODE_EFFECTIVE"] == "verify-identity"
    # Cookies must be secure-only in production.
    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_production_refuses_a_placeholder_secret():
    from app import create_app
    from app.config import ProductionConfig

    settings = ProductionConfig()
    settings.SECRET_KEY = "change-me-before-going-live"
    settings.KIOSK_TOKEN = "a-real-kiosk-token"
    settings.MYSQL_HOST = "localhost"

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(settings)
