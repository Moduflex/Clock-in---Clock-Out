"""Create the MySQL database, tables, and the first administrator.

Run once at install time:

    python scripts/init_db.py --create-database --admin office

Options:
  --create-database   Also issue CREATE DATABASE IF NOT EXISTS. Needs an account
                      with CREATE rights (see --root-user).
  --admin USERNAME    Create an administrator; the password is prompted for.

Nothing here drops or truncates anything, so it is safe to re-run.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime as dt  # noqa: E402

from sqlalchemy import create_engine, inspect, select, text  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402

from app import create_app  # noqa: E402
from app.config import get_config  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import AdminUser, ShiftPattern, WorkingWeek  # noqa: E402


def create_database(config, root_user: str | None, root_password: str | None) -> None:
    """Issue CREATE DATABASE using an account that has the rights for it."""
    user = root_user or config.MYSQL_USER
    password = root_password if root_password is not None else config.MYSQL_PASSWORD

    url = URL.create(
        "mysql+pymysql",
        username=user,
        password=password,
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
    )
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    name = config.MYSQL_DATABASE
    # Identifier, not a value, so it cannot be a bound parameter. The name comes
    # from our own .env rather than user input, and is validated first.
    if not name.replace("_", "").isalnum():
        raise SystemExit(f"Refusing to use unsafe database name {name!r}.")
    with engine.connect() as connection:
        connection.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS `{name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
    engine.dispose()
    print(f"Database `{name}` present.")


def upgrade_existing_tables() -> None:
    """Add columns that create_all cannot add to tables that already exist.

    Databases created before the shifts feature lack employee.shift_pattern_id.
    ADD COLUMN works on both MySQL and SQLite; the foreign key is enforced by
    the application for upgraded databases (fresh installs get the constraint
    from create_all).
    """
    columns = {c["name"] for c in inspect(db.engine).get_columns("employee")}
    if "shift_pattern_id" not in columns:
        db.session.execute(text("ALTER TABLE employee ADD COLUMN shift_pattern_id INTEGER"))
        db.session.commit()
        print("Added employee.shift_pattern_id column.")

    if "working_week_id" not in columns:
        db.session.execute(text("ALTER TABLE employee ADD COLUMN working_week_id INTEGER"))
        db.session.commit()
        print("Added employee.working_week_id column.")

    # The encrypted basic hourly rate. BLOB on MySQL, and SQLite takes it
    # verbatim. There is no overtime column: that rate is derived (basic x 1.5).
    if "basic_rate_enc" not in columns:
        db.session.execute(text("ALTER TABLE employee ADD COLUMN basic_rate_enc BLOB"))
        db.session.commit()
        print("Added employee.basic_rate_enc column (encrypted basic pay rate).")

    if "overtime_rate_enc" in columns:
        # Left over from the build that stored both rates. Nothing reads it now.
        # Dropping it is deliberately not automatic: a column is only removed by
        # somebody who has looked at what is in it first.
        print(
            "Note: employee.overtime_rate_enc is no longer used - the overtime "
            "rate is worked out as basic x 1.5. Drop it when you are ready:
"
            "  ALTER TABLE employee DROP COLUMN overtime_rate_enc;"
        )

    shift_columns = {c["name"] for c in inspect(db.engine).get_columns("shift_pattern")}
    if "break_applies_after_minutes" not in shift_columns:
        db.session.execute(
            text(
                "ALTER TABLE shift_pattern ADD COLUMN break_applies_after_minutes "
                "INTEGER NOT NULL DEFAULT 360"
            )
        )
        db.session.commit()
        print("Added shift_pattern.break_applies_after_minutes column (default 360).")

    if "pay_beyond_end" not in shift_columns:
        # On by default, including for shifts that already exist: a late finish
        # is paid and becomes overtime. Untick it per shift on the Shifts and
        # hours page where staying on is not authorised work.
        db.session.execute(
            text(
                "ALTER TABLE shift_pattern ADD COLUMN pay_beyond_end "
                "BOOLEAN NOT NULL DEFAULT 1"
            )
        )
        db.session.commit()
        print(
            "Added shift_pattern.pay_beyond_end column, on for every shift - "
            "time worked after the shift end is now paid as overtime. Untick it "
            "per shift on the Shifts and hours page to keep the old behaviour."
        )


def seed_default_shift() -> None:
    """Create the standard day shift once; never touch existing patterns."""
    if db.session.scalars(select(ShiftPattern)).first() is not None:
        return
    db.session.add(
        ShiftPattern(
            name="Standard day",
            start_time=dt.time(7, 30),
            end_time=dt.time(16, 0),
            unpaid_break_minutes=30,
            is_default=True,
        )
    )
    db.session.commit()
    print("Seeded default shift 'Standard day' (07:30-16:00, 30 min unpaid lunch).")


def seed_working_weeks() -> None:
    """Create the standard week lengths once; never touch existing rows."""
    if db.session.scalars(select(WorkingWeek)).first() is not None:
        return
    db.session.add_all(
        [
            WorkingWeek(name="40-hour week", hours=40.0, is_default=True),
            WorkingWeek(name="32-hour week", hours=32.0),
        ]
    )
    db.session.commit()
    print("Seeded standard weeks: 40 hours (default) and 32 hours.")


def create_admin(username: str) -> None:
    existing = db.session.scalars(
        select(AdminUser).where(AdminUser.username == username)
    ).first()
    if existing is not None:
        print(f"Administrator {username!r} already exists - leaving it alone.")
        return

    password = getpass.getpass(f"Password for {username!r} (min 10 characters): ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise SystemExit("Passwords did not match.")

    user = AdminUser(username=username, full_name=username)
    try:
        user.set_password(password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    db.session.add(user)
    db.session.commit()
    print(f"Created administrator {username!r}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-database", action="store_true")
    parser.add_argument("--root-user", help="Account with CREATE DATABASE rights.")
    parser.add_argument("--root-password", help="Password for --root-user (prompted if omitted).")
    parser.add_argument("--admin", help="Username of an administrator to create.")
    args = parser.parse_args()

    config = get_config("development")

    if args.create_database:
        root_password = args.root_password
        if args.root_user and root_password is None:
            root_password = getpass.getpass(f"MySQL password for {args.root_user!r}: ")
        create_database(config, args.root_user, root_password)

    app = create_app("development")
    with app.app_context():
        db.create_all()
        print("Tables created (existing tables untouched).")
        upgrade_existing_tables()
        seed_default_shift()
        seed_working_weeks()
        if args.admin:
            create_admin(args.admin)

    print("\nNext steps:")
    print("  1. python scripts/fetch_models.py   (if you have not already)")
    print("  2. python run.py                    then open http://127.0.0.1:5000/login")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
