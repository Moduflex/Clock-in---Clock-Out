"""The check-schema command: does the database match what the code expects?

Worth its own file because the failure it catches is a nasty one. ``init-db``
runs ``create_all()``, which adds missing *tables* but never missing *columns*.
So a database that predates a new column keeps working until something selects
that column - and then every page behind the login returns a bare 500 with
nothing on screen to say why, which reads as "the login is broken".
"""

from __future__ import annotations

from sqlalchemy import text

from app.extensions import db


def _run(app):
    return app.test_cli_runner().invoke(args=["check-schema"])


def test_a_matching_database_reports_nothing_to_do(app):
    result = _run(app)

    assert result.exit_code == 0
    assert "up to date" in result.output


def test_a_missing_column_is_named_with_the_alter_that_fixes_it(app, db):
    """The exact case that took a production sign-in down: employee.pay_basis."""
    db.session.execute(text("ALTER TABLE employee DROP COLUMN pay_basis"))
    db.session.commit()

    result = _run(app)

    assert result.exit_code == 1
    assert "MISSING COLUMN  employee.pay_basis" in result.output
    assert "ALTER TABLE employee ADD COLUMN pay_basis" in result.output
    # The default matters: without it the ALTER fails on a table that has rows.
    assert "four_weekly" in result.output


def test_a_missing_table_points_at_init_db(app, db):
    db.session.execute(text("DROP TABLE fingerprint_template"))
    db.session.commit()

    result = _run(app)

    assert result.exit_code == 1
    assert "MISSING TABLE   fingerprint_template" in result.output
    assert "init-db" in result.output


def test_it_checks_every_table_the_models_define(app):
    result = _run(app)

    assert f"{len(db.metadata.sorted_tables)} tables checked" in result.output
