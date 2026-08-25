"""Application factory for the face-recognition clocking system."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, render_template

from .config import BASE_DIR, Config, get_config
from .extensions import csrf, db, login_manager


def create_app(config: Config | str | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)

    settings = get_config(config) if isinstance(config, (str, type(None))) else config
    app.config.from_object(settings)
    # from_object skips properties, so the computed values are set by hand.
    app.config["SQLALCHEMY_DATABASE_URI"] = settings.SQLALCHEMY_DATABASE_URI
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = settings.SQLALCHEMY_ENGINE_OPTIONS
    app.config["MYSQL_SSL_MODE_EFFECTIVE"] = settings.mysql_ssl_mode

    _configure_logging(app)
    _warn_on_weak_secrets(app)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    from .models import AdminUser  # imported here to avoid a circular import

    @login_manager.user_loader
    def load_admin(user_id: str):
        try:
            return db.session.get(AdminUser, int(user_id))
        except (TypeError, ValueError):
            return None

    from .blueprints.admin import bp as admin_bp
    from .blueprints.auth import bp as auth_bp
    from .blueprints.kiosk import bp as kiosk_bp

    app.register_blueprint(kiosk_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)

    _register_error_handlers(app)
    _register_template_helpers(app)
    _register_cli(app)

    return app


def _configure_logging(app: Flask) -> None:
    if app.config.get("TESTING"):
        return
    log_dir = Path(app.instance_path)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "clocking.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    handler.setLevel(logging.INFO)
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)


def _warn_on_weak_secrets(app: Flask) -> None:
    """Refuse to start in production with placeholder secrets."""
    if app.config.get("TESTING"):
        return
    weak_secret = app.config["SECRET_KEY"] in {
        "",
        "dev-only-insecure-key",
        "change-me-before-going-live",
    }
    weak_kiosk = app.config["KIOSK_TOKEN"] in {"", "change-me-kiosk-token"}

    if app.config.get("DEBUG"):
        if weak_secret:
            app.logger.warning("SECRET_KEY is a placeholder - set a real one in .env")
        if weak_kiosk:
            app.logger.warning("KIOSK_TOKEN is a placeholder - set a real one in .env")
        if (
            app.config.get("MYSQL_HOST") not in {"localhost", "127.0.0.1", "::1"}
            and app.config.get("MYSQL_SSL_MODE_EFFECTIVE") == "disabled"
        ):
            app.logger.warning(
                "MYSQL_SSL_MODE is disabled against a remote database - face "
                "templates and the password cross the network unencrypted. "
                "Production mode refuses to start like this."
            )
        return

    problems = []
    if weak_secret:
        problems.append("SECRET_KEY")
    if weak_kiosk:
        problems.append("KIOSK_TOKEN")
    if problems:
        raise RuntimeError(
            "Refusing to start in production with placeholder "
            + " and ".join(problems)
            + ". Set real values in .env (see .env.example)."
        )

    _check_database_encryption(app)


def _check_database_encryption(app: Flask) -> None:
    """Refuse to send biometric data to a remote database in plaintext.

    A managed database is reached across the public internet. Face templates are
    biometric data, and the credentials travel on the same connection, so an
    unencrypted link is not an acceptable production configuration.
    """
    host = app.config.get("MYSQL_HOST", "")
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if host in local_hosts:
        return
    if app.config.get("MYSQL_SSL_MODE_EFFECTIVE") != "disabled":
        return

    raise RuntimeError(
        f"Refusing to start: MYSQL_SSL_MODE is disabled but the database ({host}) "
        "is not on this machine, so face templates and the database password "
        "would cross the network unencrypted. Set MYSQL_SSL_MODE=verify-identity "
        "in .env, or move the database onto this machine."
    )


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(403)
    def forbidden(error):  # pragma: no cover - trivial
        return render_template("error.html", code=403, message="Not allowed."), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.errorhandler(413)
    def too_large(error):
        return (
            render_template("error.html", code=413, message="That upload was too large."),
            413,
        )

    @app.errorhandler(500)
    def server_error(error):  # pragma: no cover - defensive
        app.logger.exception("Unhandled error")
        db.session.rollback()
        return (
            render_template("error.html", code=500, message="Something went wrong."),
            500,
        )


def _register_template_helpers(app: Flask) -> None:
    from .services.timesheet import get_timezone, to_local

    @app.template_filter("localtime")
    def localtime_filter(value, fmt: str = "%d/%m/%Y %H:%M"):
        """Render a stored UTC timestamp in the configured local timezone."""
        if value is None:
            return ""
        return to_local(value, get_timezone(app.config["TIMEZONE"])).strftime(fmt)

    @app.template_filter("hoursmins")
    def hoursmins_filter(hours):
        """7.25 -> "7h 15m" - easier to check against a paper timesheet."""
        if hours is None:
            return ""
        total_minutes = int(round(float(hours) * 60))
        return f"{total_minutes // 60}h {total_minutes % 60:02d}m"

    @app.context_processor
    def inject_globals():
        return {
            "app_name": "Moduflex Clocking",
            "timezone_name": app.config["TIMEZONE"],
        }


def _register_cli(app: Flask) -> None:
    import click

    @app.cli.command("init-db")
    def init_db_command() -> None:
        """Create any missing tables."""
        db.create_all()
        click.echo("Tables created (existing tables left untouched).")

    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    @click.option("--full-name", default="", help="Display name.")
    @click.password_option()
    def create_admin_command(username: str, full_name: str, password: str) -> None:
        """Create a back-office administrator."""
        from sqlalchemy import select

        from .models import AdminUser

        existing = db.session.scalars(
            select(AdminUser).where(AdminUser.username == username)
        ).first()
        if existing is not None:
            raise click.ClickException(f"User {username!r} already exists.")

        user = AdminUser(username=username, full_name=full_name or username)
        try:
            user.set_password(password)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        db.session.add(user)
        db.session.commit()
        click.echo(f"Created administrator {username!r}.")

    @app.cli.command("payroll-key")
    def payroll_key_command() -> None:
        """Generate a key for the encrypted pay-rate columns."""
        from .services.payrates import generate_key

        click.echo("Paste this into .env, then restart:")
        click.echo("")
        click.echo(f"PAYROLL_KEY={generate_key()}")
        click.echo("")
        click.echo(
            "Keep it with your other secrets and back it up separately from "
            "the database. Without it, stored pay rates cannot be read back."
        )

    @app.cli.command("rebuild-index")
    def rebuild_index_command() -> None:
        """Reload the in-memory face index from the database."""
        from .services.recognition import get_index, invalidate_index

        invalidate_index(app)
        index = get_index(app)
        click.echo(f"Index holds {index.size} templates for {index.employee_count} employees.")
