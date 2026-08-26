"""Application configuration, driven entirely by environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class Config:
    """Base configuration shared by every environment."""

    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-insecure-key")

    # --- Database ---------------------------------------------------------
    MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
    MYSQL_PORT = _int("MYSQL_PORT", 3306)
    MYSQL_USER = os.getenv("MYSQL_USER", "clocking")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "clocking")

    # TLS to the database. "" means "decide from the host" - see mysql_ssl_mode.
    MYSQL_SSL_MODE = (os.getenv("MYSQL_SSL_MODE") or "").strip().lower()
    MYSQL_SSL_CA = (os.getenv("MYSQL_SSL_CA") or "").strip()

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- Face models ------------------------------------------------------
    MODEL_DIR = BASE_DIR / "models"
    FACE_DETECTOR_MODEL = MODEL_DIR / "face_detection_yunet_2023mar.onnx"
    FACE_RECOGNISER_MODEL = MODEL_DIR / "face_recognition_sface_2021dec.onnx"

    # --- Recognition tuning ----------------------------------------------
    # --- fingerprint readers that hand back a template for us to match ----
    # "simulator" needs no hardware and is what the test suite uses. Set this to
    # the vendor driver once the reader is on the desk.
    FINGERPRINT_DRIVER = os.getenv("FINGERPRINT_DRIVER", "simulator")
    # Scores run 0..1. As with faces, a match must clear the threshold *and*
    # beat the runner-up by the margin, so two similar fingers refuse rather
    # than guess - clocking the wrong person is worse than asking again.
    FINGERPRINT_MATCH_THRESHOLD = _float("FINGERPRINT_MATCH_THRESHOLD", 0.60)
    FINGERPRINT_MATCH_MARGIN = _float("FINGERPRINT_MATCH_MARGIN", 0.05)
    FINGERPRINT_ENROL_SAMPLES = _int("FINGERPRINT_ENROL_SAMPLES", 3)

    FACE_MATCH_THRESHOLD = _float("FACE_MATCH_THRESHOLD", 0.40)
    FACE_MATCH_MARGIN = _float("FACE_MATCH_MARGIN", 0.05)
    # 55px is evidence-based, not a guess: measured on real photos, a face is
    # still matched at ~0.92 cosine (against ~0.10 for a different person) down
    # to about 47px wide, and YuNet still detects at 30px. 55 leaves headroom
    # for a soft webcam frame while roughly halving the distance limit that an
    # 80px floor imposed. Check yours on the Camera check page.
    FACE_MIN_PIXELS = _int("FACE_MIN_PIXELS", 55)
    FACE_MIN_SHARPNESS = _float("FACE_MIN_SHARPNESS", 45.0)
    # Detection range scales directly with this: a face 40px wide in a 640px
    # frame is 60px in a 960px one. Detection costs about 28ms at 960 against
    # 11ms at 640 - irrelevant next to the frame capture time, so the pixels buy
    # range almost for free. Must be >= CAPTURE_MAX_WIDTH to be of any use.
    FACE_DETECT_MAX_SIDE = _int("FACE_DETECT_MAX_SIDE", 960)
    FACE_DETECT_CONFIDENCE = _float("FACE_DETECT_CONFIDENCE", 0.85)

    SCAN_FRAMES = _int("SCAN_FRAMES", 3)
    SCAN_MIN_AGREE = _int("SCAN_MIN_AGREE", 2)
    # In hands-free mode the nearest face must be this many times wider than the
    # next one for it to count as "the person at the kiosk".
    FACE_DOMINANT_RATIO = _float("FACE_DOMINANT_RATIO", 1.35)
    ENROL_MIN_SAMPLES = _int("ENROL_MIN_SAMPLES", 3)
    ENROL_MAX_SAMPLES = _int("ENROL_MAX_SAMPLES", 6)

    LIVENESS_REQUIRE_MOTION = _bool("LIVENESS_REQUIRE_MOTION", True)
    LIVENESS_MIN_MOTION = _float("LIVENESS_MIN_MOTION", 1.6)

    # --- Attendance rules -------------------------------------------------
    # Button presses only: guards against an accidental double-tap on Scan.
    # Short, because the buttons should otherwise toggle as readily as the
    # automatic path does. Hands-free entries ignore this entirely.
    CLOCK_COOLDOWN_SECONDS = _int("CLOCK_COOLDOWN_SECONDS", 5)
    TIMEZONE = os.getenv("TIMEZONE", "Europe/London")

    # --- Payroll ----------------------------------------------------------
    # Key for the encrypted hourly-rate columns (see services/payrates.py).
    # Generate one with "flask payroll-key". Left blank, a key is derived from
    # SECRET_KEY so a fresh install works - but then rotating SECRET_KEY makes
    # every stored rate unreadable, which is why production should set this.
    PAYROLL_KEY = os.getenv("PAYROLL_KEY", "")
    # Four-weekly payroll: the Monday period 1 of the payroll year starts on.
    # The master sheet counts 28-day periods from here to fill in "Period:".
    PAYROLL_PERIOD_1_START = os.getenv("PAYROLL_PERIOD_1_START", "2026-03-30")
    PAYROLL_PERIODS_PER_YEAR = _int("PAYROLL_PERIODS_PER_YEAR", 13)
    PAYROLL_COMPANY_NAME = os.getenv("PAYROLL_COMPANY_NAME", "Moduflex Ltd")

    # --- Dashboard --------------------------------------------------------
    # The office dashboard reloads itself so a screen left open on it stays
    # current. Each reload re-reads the running payroll period, so raise this
    # if the page is left up all day on a slow connection. 0 turns it off.
    DASHBOARD_REFRESH_SECONDS = _int("DASHBOARD_REFRESH_SECONDS", 30)

    # --- Kiosk ------------------------------------------------------------
    KIOSK_TOKEN = os.getenv("KIOSK_TOKEN", "")
    KIOSK_DEVICE_LABEL = os.getenv("KIOSK_DEVICE_LABEL", "Kiosk")
    # Let somebody clock by typing their payroll number, for when the camera
    # cannot see them - a plastered hand, a hood up in winter, a dusty lens, or
    # a new starter not yet enrolled.
    #
    # A payroll number is an IDENTIFIER, NOT A SECRET: it is printed on payslips
    # and known to colleagues, so anybody who knows a number can clock as that
    # person. Entries made this way are recorded with method "keypad" so the
    # office can see exactly which ones were typed rather than recognised. Turn
    # this off if that trade is not acceptable on your floor.
    KIOSK_KEYPAD_MODE = _bool("KIOSK_KEYPAD_MODE", True)

    # --- Hands-free (automatic) clocking ----------------------------------
    # The kiosk watches for a face and clocks people with no button press.
    KIOSK_AUTO_MODE = _bool("KIOSK_AUTO_MODE", True)
    # Seconds the on-screen countdown runs before an automatic entry is
    # committed, giving somebody who only walked past a chance to cancel.
    # 0 commits immediately.
    AUTO_CONFIRM_SECONDS = _int("AUTO_CONFIRM_SECONDS", 2)
    # What stops the kiosk clocking somebody twice is *absence*: after an entry
    # it waits until the person has walked away before clocking them again. That
    # is the only throttle, deliberately - see AUTO_REQUIRE_DEPARTURE.
    #
    # There is no minimum interval between entries. Whoever is recognised is
    # clocked to the opposite of their current state, and nothing is refused for
    # having clocked recently. A confirmation token is single use, which handles
    # replays without also blocking genuine clocking.
    # Require the person to leave the camera's view before they can be clocked
    # again.
    #
    # OFF by default, by choice: with it on, somebody testing at their desk - or
    # standing at a kiosk the camera can always see - gets clocked once and then
    # watches a screen that appears stuck. Off means any recognised face is
    # clocked immediately, the same face included, which is the behaviour most
    # people expect from a face-operated switch.
    #
    # The consequence is worth understanding: somebody who stays in front of the
    # camera is clocked in and out repeatedly, roughly every few seconds, and the
    # only thing between them and a messy timesheet is the AUTO_CONFIRM_SECONDS
    # countdown. Turn this back on for a kiosk sited where people loiter.
    AUTO_REQUIRE_DEPARTURE = _bool("AUTO_REQUIRE_DEPARTURE", False)
    # How long the scene must read empty for a departure to count. Long enough
    # not to be triggered by somebody shifting their weight, short enough that
    # stepping aside and back is not a chore.
    AUTO_DEPARTURE_MS = _int("AUTO_DEPARTURE_MS", 900)
    # Re-arm anyway after this many seconds, even if the camera never reports an
    # empty scene.
    #
    # Departure gating on its own has a hard failure mode: if the camera can
    # always see somebody - a kiosk facing a desk, a busy doorway that is never
    # empty, or a presence threshold set too low - it clocks once and then never
    # again, with no indication why. This fallback guarantees the kiosk always
    # comes back to life.
    #
    # The trade-off runs the other way: somebody permanently in view will be
    # offered a clock every AUTO_REARM_SECONDS, so keep it long enough that the
    # countdown is a real chance to cancel. Set 0 to disable the fallback and
    # rely on departure alone.
    AUTO_REARM_SECONDS = _int("AUTO_REARM_SECONDS", 30)
    # While latched, ask the server every so often whether anybody is still in
    # front of the camera.
    #
    # The browser's grey-difference presence check is a crude signal: one global
    # threshold against a reference image. It copes well with somebody walking in
    # (a large change) and badly with the marginal cases - a person in dark
    # clothing, a doorway that never fully clears, a queue at shift change where
    # the scene is never empty between two people. Relying on it alone is what
    # made clocking work on the way in but behave unpredictably afterwards.
    #
    # The face detector, by contrast, knows for certain whether a face is there
    # and whose it is. So it becomes the authority: the kiosk re-arms as soon as
    # the server reports no face, or reports somebody different. Costs one small
    # request every AUTO_LATCHED_POLL_MS while latched, and nothing at all once
    # the scene is quiet.
    AUTO_LATCHED_POLL_MS = _int("AUTO_LATCHED_POLL_MS", 1500)
    # Slow safety poll for when the presence check says "empty" but somebody is
    # actually standing there - low contrast, an odd camera angle, a threshold
    # set too high. Without it, a person the presence check cannot see is never
    # clocked at all and there is nothing on screen to say why. Set 0 to disable
    # and trust the presence check completely.
    AUTO_IDLE_POLL_MS = _int("AUTO_IDLE_POLL_MS", 4000)
    # How often the kiosk runs recognition once it thinks somebody is there.
    AUTO_POLL_MS = _int("AUTO_POLL_MS", 600)
    # How often it checks whether anybody has arrived (browser-side, cheap).
    AUTO_PRESENCE_MS = _int("AUTO_PRESENCE_MS", 200)
    # Mean grey-level difference from the empty-scene reference that counts as
    # "somebody is standing there". Raise it if the kiosk scans at shadows.
    AUTO_PRESENCE_THRESHOLD = _float("AUTO_PRESENCE_THRESHOLD", 7.0)
    # Frames per hands-free identification, and the gap between them. Two frames
    # is the minimum the liveness check can work with, and capture time is the
    # single largest component of "how long until my name appears" - three frames
    # at 320ms cost 640ms before the request even left the browser.
    AUTO_SCAN_FRAMES = _int("AUTO_SCAN_FRAMES", 2)
    # Do not shorten this much: the liveness check needs enough time between
    # frames for a real face to change measurably. Too short and live people get
    # rejected as photographs.
    AUTO_FRAME_GAP_MS = _int("AUTO_FRAME_GAP_MS", 300)
    # Width the browser downscales frames to before upload. This, not the server,
    # was the real limit on detection range: the server never saw more detail
    # than this however high FACE_DETECT_MAX_SIDE was set.
    CAPTURE_MAX_WIDTH = _int("CAPTURE_MAX_WIDTH", 960)

    # --- Uploads / limits -------------------------------------------------
    # A scan posts a handful of JPEG frames; 12 MB is generous headroom.
    MAX_CONTENT_LENGTH = 12 * 1024 * 1024
    # Hands-free mode polls while somebody is standing there, so this ceiling
    # is well above a press-to-scan kiosk's needs. It still caps a runaway
    # client or anything else on the network hammering the endpoint.
    RECOGNISE_RATE_LIMIT = _int("RECOGNISE_RATE_LIMIT", 150)
    RECOGNISE_RATE_WINDOW = _int("RECOGNISE_RATE_WINDOW", 60)
    LOGIN_RATE_LIMIT = _int("LOGIN_RATE_LIMIT", 10)
    LOGIN_RATE_WINDOW = _int("LOGIN_RATE_WINDOW", 300)

    # --- Session hardening ------------------------------------------------
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False  # switched on in ProductionConfig
    REMEMBER_COOKIE_HTTPONLY = True
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12

    LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:  # noqa: N802 - Flask config name
        return (
            f"mysql+pymysql://{quote_plus(self.MYSQL_USER)}:"
            f"{quote_plus(self.MYSQL_PASSWORD)}@{self.MYSQL_HOST}:{self.MYSQL_PORT}/"
            f"{self.MYSQL_DATABASE}?charset=utf8mb4"
        )

    @property
    def mysql_ssl_mode(self) -> str:
        """One of "disabled", "required" or "verify-identity".

        Defaults by host: a database on this machine needs no TLS, but anything
        reached over a network does - and a managed database (DigitalOcean, RDS,
        Azure) is reached across the public internet. Face templates are
        biometric data, so shipping them unencrypted is not an option.
        """
        if self.MYSQL_SSL_MODE in {"disabled", "required", "verify-identity"}:
            return self.MYSQL_SSL_MODE
        return "disabled" if self.MYSQL_HOST in self.LOCAL_HOSTS else "verify-identity"

    @property
    def mysql_connect_args(self) -> dict:
        """PyMySQL connect arguments implementing :attr:`mysql_ssl_mode`.

        Only these exact options actually negotiate TLS. Several plausible
        alternatives - ``ssl={}``, ``ssl_verify_cert=False``,
        ``ssl_disabled=False`` - connect in *plaintext* while looking as though
        they enabled encryption, so do not "simplify" this without checking
        ``SHOW STATUS LIKE 'Ssl_version'`` on a real connection afterwards.
        """
        mode = self.mysql_ssl_mode
        if mode == "disabled":
            return {}

        args: dict = {"ssl": {"ssl": True}}
        if mode == "verify-identity":
            args["ssl_verify_cert"] = True
            args["ssl_verify_identity"] = True
            if self.MYSQL_SSL_CA:
                args["ssl_ca"] = self.MYSQL_SSL_CA
        return args

    @property
    def SQLALCHEMY_ENGINE_OPTIONS(self) -> dict:  # noqa: N802 - Flask config name
        options: dict = {
            # Long-lived kiosks sit idle overnight; recycle before the server's
            # wait_timeout drops the connection underneath us. Managed databases
            # often use a much shorter timeout than MySQL's 8-hour default.
            "pool_pre_ping": True,
            "pool_recycle": 3600,
        }
        if self.SQLALCHEMY_DATABASE_URI.startswith("mysql"):
            connect_args = self.mysql_connect_args
            if connect_args:
                options["connect_args"] = connect_args
        return options


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    KIOSK_TOKEN = "test-kiosk-token"
    SECRET_KEY = "test-secret-key"

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:  # noqa: N802 - Flask config name
        return os.getenv("TEST_DATABASE_URI", "sqlite+pysqlite:///:memory:")


CONFIGS = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestConfig,
}


def get_config(name: str | None = None) -> Config:
    """Return a config instance for *name*, falling back to FLASK_ENV."""
    key = (name or os.getenv("FLASK_ENV") or "development").strip().lower()
    return CONFIGS.get(key, DevelopmentConfig)()
