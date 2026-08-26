"""The kiosk: the touchscreen by the workshop door.

The page is deliberately unauthenticated - a shop-floor employee should walk up
and be clocked, nothing more. The endpoints that write attendance rows are
protected by the kiosk shared secret instead, which the page is rendered with.

Two clocking paths exist:

* **Hands-free** (the normal one) - the browser watches for somebody arriving,
  then calls ``/identify``, which recognises them but writes nothing. The screen
  shows who was seen and what is about to happen, counts down, and only then
  calls ``/commit``. That pause is the whole point: without it, walking past the
  camera two hours into a shift would clock you out.
* **Button press** - ``/scan`` recognises and records in one step, for the Scan,
  Clock in and Clock out buttons.
* **Payroll number** - ``/payroll`` clocks whoever owns the number typed in on
  the keypad. This one recognises nobody: a payroll number is an identifier and
  not a secret, so the entry is somebody's own word for who they are. It is
  stored with method ``keypad`` for exactly that reason, and the office can see
  which entries were typed rather than recognised.

Whoever is recognised is clocked to the opposite of their current state: clocked
in becomes clocked out, clocked out becomes clocked in. Nothing is ever refused
for having clocked recently.
"""

from __future__ import annotations

import secrets
import threading
import time

from flask import Blueprint, current_app, jsonify, render_template, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..extensions import csrf, db
from ..models import (
    DIRECTIONS,
    METHOD_AUTO,
    METHOD_FACE,
    METHOD_FINGER,
    METHOD_KEYPAD,
    Employee,
    utcnow,
)
from ..security import rate_limit, require_kiosk_token
from ..services import attendance
from ..services.recognition import scan
from ..services.timesheet import get_timezone, to_local

bp = Blueprint("kiosk", __name__)

# Namespace for the short-lived tokens carrying an identification to /commit.
_CONFIRM_SALT = "kiosk-auto-confirm"


def _confirm_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_CONFIRM_SALT)


# Confirmation tokens are single use. This replaces the old minimum-interval rule
# as the protection against a captured or double-submitted token: the interval
# also blocked genuine clocking, whereas consuming the token blocks only the
# replay. Bounded and short-lived, so it needs no storage of its own - the same
# reasoning as the in-process rate limiter in app/security.py.
_used_tokens: dict[str, float] = {}
_used_tokens_lock = threading.Lock()


def _consume_token(nonce: str, ttl: int) -> bool:
    """Record *nonce* as used. Returns False if it had already been used."""
    now = time.monotonic()
    with _used_tokens_lock:
        for key, expires in list(_used_tokens.items()):
            if expires <= now:
                del _used_tokens[key]
        if nonce in _used_tokens:
            return False
        _used_tokens[nonce] = now + ttl
        return True


def _confirm_max_age() -> int:
    """How long an identification stays valid for committing.

    The countdown plus slack for a slow network. Deliberately short, so a token
    captured off the wire cannot be replayed later in the day.
    """
    return int(current_app.config["AUTO_CONFIRM_SECONDS"]) + 20


@bp.get("/")
def index():
    """Render the kiosk screen."""
    return render_template(
        "kiosk.html",
        kiosk_token=current_app.config["KIOSK_TOKEN"],
        device_label=current_app.config["KIOSK_DEVICE_LABEL"],
        scan_frames=current_app.config["SCAN_FRAMES"],
        auto_mode=current_app.config["KIOSK_AUTO_MODE"],
        auto_confirm_seconds=current_app.config["AUTO_CONFIRM_SECONDS"],
        auto_poll_ms=current_app.config["AUTO_POLL_MS"],
        auto_presence_ms=current_app.config["AUTO_PRESENCE_MS"],
        auto_presence_threshold=current_app.config["AUTO_PRESENCE_THRESHOLD"],
        auto_scan_frames=current_app.config["AUTO_SCAN_FRAMES"],
        auto_frame_gap_ms=current_app.config["AUTO_FRAME_GAP_MS"],
        capture_max_width=current_app.config["CAPTURE_MAX_WIDTH"],
        auto_require_departure=current_app.config["AUTO_REQUIRE_DEPARTURE"],
        auto_departure_ms=current_app.config["AUTO_DEPARTURE_MS"],
        auto_rearm_seconds=current_app.config["AUTO_REARM_SECONDS"],
        auto_latched_poll_ms=current_app.config["AUTO_LATCHED_POLL_MS"],
        auto_idle_poll_ms=current_app.config["AUTO_IDLE_POLL_MS"],
        keypad_mode=current_app.config["KIOSK_KEYPAD_MODE"],
    )


@bp.get("/healthz")
def healthz():
    """Cheap probe for a scheduled task or monitoring script.

    Reports the two things that actually stop clocking working: the database
    being unreachable and the face models being absent.
    """
    from sqlalchemy import text

    try:
        db.session.execute(text("SELECT 1"))
        database_ok = True
    except Exception:  # noqa: BLE001 - report the state, do not raise
        database_ok = False

    models_ok = (
        current_app.config["FACE_DETECTOR_MODEL"].is_file()
        and current_app.config["FACE_RECOGNISER_MODEL"].is_file()
    )

    healthy = database_ok and models_ok
    return (
        jsonify(ok=healthy, service="clocking", database=database_ok, models=models_ok),
        200 if healthy else 503,
    )


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def _employee_payload(employee: Employee) -> dict:
    return {
        "id": employee.id,
        "name": employee.full_name,
        "first_name": employee.first_name,
        "payroll_ref": employee.payroll_ref,
        "department": employee.department,
    }


def _frames_from_request():
    """Pull and bound the frame list from the JSON body.

    Returns ``(frames, None)`` or ``(None, error_response)``.
    """
    payload = request.get_json(silent=True) or {}
    frames = payload.get("frames") or []
    if not isinstance(frames, list):
        return None, (
            jsonify(ok=False, code="bad_request", message="frames must be a list."),
            400,
        )
    limit = max(1, int(current_app.config["SCAN_FRAMES"]) + 2)
    return [f for f in frames if isinstance(f, (str, bytes))][:limit], None


def _identify(frames, *, automatic: bool):
    """Recognise the person in *frames*, writing nothing.

    Returns ``(employee, score, None)`` or ``(None, score, error_response)``.
    """
    outcome = scan(frames, automatic=automatic)
    score = outcome.score

    if not outcome.ok:
        if not automatic:
            # Hands-free polling would fill the log with "no face" lines, so
            # only a deliberate press is worth recording as a refusal.
            current_app.logger.info(
                "Kiosk scan refused: %s (best score %.3f)", outcome.code, score
            )
        elif outcome.code.startswith("liveness_"):
            # Worth logging even when hands-free: a liveness refusal means a real
            # person was in front of the camera and was turned away, which is the
            # kind of thing that gets reported as "it just does not work".
            current_app.logger.info(
                "Hands-free liveness refusal: %s (motion %.2f, consistency %.3f)",
                outcome.code,
                outcome.liveness.motion if outcome.liveness else -1.0,
                outcome.liveness.consistency if outcome.liveness else -1.0,
            )

        body = {"ok": False, "code": outcome.code, "message": outcome.message}
        if outcome.liveness is not None:
            # Surfaced so the ?debug=1 overlay can show why, rather than leaving
            # somebody guessing at an unexplained refusal.
            body["motion"] = round(outcome.liveness.motion, 2)
            body["consistency"] = round(outcome.liveness.consistency, 3)
        return None, score, (jsonify(**body), 200)

    employee = db.session.get(Employee, outcome.employee_id)
    if employee is None or not employee.is_active:
        return None, score, (
            jsonify(
                ok=False,
                code="employee_inactive",
                message="Your record is not active. Please see the office.",
            ),
            200,
        )
    return employee, score, None


def _result_payload(employee: Employee, result, score: float | None) -> dict:
    tz = get_timezone(current_app.config["TIMEZONE"])
    current_app.logger.info(
        "%s %s (%s) recorded=%s method=%s",
        employee.payroll_ref,
        result.direction,
        f"{score:.3f}" if score is not None else "no score",
        result.recorded,
        result.event.method if result.event else "-",
    )
    return {
        "ok": True,
        "code": "recorded" if result.recorded else "duplicate",
        "message": result.message,
        "employee": _employee_payload(employee),
        "direction": result.direction,
        "recorded": result.recorded,
        "occurred_at": to_local(result.occurred_at, tz).strftime("%H:%M:%S"),
        "occurred_on": to_local(result.occurred_at, tz).strftime("%A %d %B %Y"),
        "confidence": round(score, 4) if score is not None else None,
        "next_direction": attendance.next_direction(employee.id),
    }


def _require_auto_mode():
    """Return an error response when hands-free clocking is switched off."""
    if current_app.config["KIOSK_AUTO_MODE"]:
        return None
    return (
        jsonify(ok=False, code="auto_disabled", message="Hands-free clocking is off."),
        403,
    )


# --------------------------------------------------------------------------
# Button-press clocking
# --------------------------------------------------------------------------
# CSRF is exempt because the caller is the kiosk JavaScript authenticating with
# the shared secret in a header, not a browser form carrying a session cookie.
@bp.post("/api/kiosk/scan")
@csrf.exempt
@require_kiosk_token
@rate_limit("kiosk_scan", "RECOGNISE_RATE_LIMIT", "RECOGNISE_RATE_WINDOW")
def api_scan():
    """Identify the person in the posted frames and record a clock event.

    Expects JSON: ``{"frames": ["data:image/jpeg;base64,..."], "direction": null}``
    where *direction* is optional and forces "in" or "out" if the employee used
    the explicit buttons rather than the automatic alternation.
    """
    frames, error = _frames_from_request()
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    direction = payload.get("direction")
    if direction is not None and direction not in DIRECTIONS:
        return jsonify(ok=False, code="bad_request", message="Unknown direction."), 400

    employee, score, refusal = _identify(frames, automatic=False)
    if refusal:
        return refusal

    result = attendance.record_clock(
        employee,
        direction=direction,
        confidence=score,
        method=METHOD_FACE,
        device_label=current_app.config["KIOSK_DEVICE_LABEL"],
        cooldown_seconds=current_app.config["CLOCK_COOLDOWN_SECONDS"],
    )
    return jsonify(**_result_payload(employee, result, score))


# --------------------------------------------------------------------------
# Hands-free clocking: identify, then commit
# --------------------------------------------------------------------------
@bp.post("/api/kiosk/identify")
@csrf.exempt
@require_kiosk_token
@rate_limit("kiosk_scan", "RECOGNISE_RATE_LIMIT", "RECOGNISE_RATE_WINDOW")
def api_identify():
    """Recognise whoever is at the kiosk **without recording anything**.

    Returns who was seen, what would be recorded, and a short-lived signed
    token. The signature is what stops a kiosk (or anything else holding the
    kiosk secret) clocking in an arbitrary employee: the employee id and the
    direction are decided here, server-side, and cannot be edited by the client
    without the application secret.
    """
    blocked = _require_auto_mode()
    if blocked:
        return blocked

    frames, error = _frames_from_request()
    if error:
        return error

    employee, score, refusal = _identify(frames, automatic=True)
    if refusal:
        return refusal

    # Simply the opposite of whatever they are now: clocked in becomes clocked
    # out, clocked out becomes clocked in. Nothing refuses on the grounds of
    # having clocked recently - the browser's departure check is what stops one
    # approach clocking twice.
    direction = attendance.next_direction(employee.id)
    token = _confirm_serializer().dumps(
        {
            "employee_id": employee.id,
            "direction": direction,
            "score": round(score, 4),
            "nonce": secrets.token_urlsafe(8),
        }
    )
    return jsonify(
        ok=True,
        code="pending",
        message="",
        employee=_employee_payload(employee),
        direction=direction,
        pending=True,
        confirm_token=token,
        confirm_seconds=int(current_app.config["AUTO_CONFIRM_SECONDS"]),
        confidence=round(score, 4),
    )


@bp.post("/api/kiosk/commit")
@csrf.exempt
@require_kiosk_token
@rate_limit("kiosk_scan", "RECOGNISE_RATE_LIMIT", "RECOGNISE_RATE_WINDOW")
def api_commit():
    """Record the entry a previous /identify offered, given its signed token."""
    blocked = _require_auto_mode()
    if blocked:
        return blocked

    payload = request.get_json(silent=True) or {}
    token = payload.get("confirm_token")
    if not isinstance(token, str) or not token:
        return jsonify(ok=False, code="bad_request", message="Missing token."), 400

    try:
        data = _confirm_serializer().loads(token, max_age=_confirm_max_age())
    except SignatureExpired:
        return (
            jsonify(
                ok=False,
                code="confirm_expired",
                message="That took too long. Please face the camera again.",
            ),
            200,
        )
    except BadSignature:
        current_app.logger.warning("Rejected a kiosk commit carrying a bad signature.")
        return jsonify(ok=False, code="bad_token", message="Invalid confirmation."), 403

    direction = data.get("direction")
    if direction not in DIRECTIONS:
        return jsonify(ok=False, code="bad_token", message="Invalid confirmation."), 403

    employee = db.session.get(Employee, data.get("employee_id"))
    if employee is None or not employee.is_active:
        return (
            jsonify(
                ok=False,
                code="employee_inactive",
                message="Your record is not active. Please see the office.",
            ),
            200,
        )

    nonce = data.get("nonce")
    if not isinstance(nonce, str) or not _consume_token(nonce, _confirm_max_age()):
        # Already used, so this is a replay or a double submit. Nothing is
        # recorded, and the caller is told plainly rather than being shown a
        # second entry that does not exist.
        return (
            jsonify(
                ok=False,
                code="already_used",
                message="That confirmation has already been used.",
            ),
            200,
        )

    score = float(data.get("score") or 0.0)
    result = attendance.record_clock(
        employee,
        direction=direction,
        confidence=score,
        method=METHOD_AUTO,
        device_label=current_app.config["KIOSK_DEVICE_LABEL"],
        # No cooldown: an automatic entry always records. Whoever is recognised
        # is clocked to the opposite of their current state, full stop. Replay is
        # handled by the token being single use, and one approach clocking twice
        # is handled by the browser requiring a departure first.
        cooldown_seconds=0,
        automatic=True,
    )
    return jsonify(**_result_payload(employee, result, score))


# --------------------------------------------------------------------------
# Fingerprint clocking
# --------------------------------------------------------------------------
@bp.post("/api/kiosk/fingerprint")
@csrf.exempt
@require_kiosk_token
@rate_limit("kiosk_finger", "RECOGNISE_RATE_LIMIT", "RECOGNISE_RATE_WINDOW")
def api_fingerprint():
    """Record a clock event from a fingerprint reader.

    Expects JSON ``{"finger_id": 7, "device_label": "Workshop reader",
    "direction": null}``.

    The reader has already matched the finger against its own memory, so
    *finger_id* is the slot number that matched - never a fingerprint, an image
    or a template. Resolving that slot to a person happens here, server-side,
    for the same reason the hands-free path signs its tokens: a caller holding
    the kiosk secret must not be able to name which employee to clock.
    """
    payload = request.get_json(silent=True) or {}

    try:
        finger_id = int(payload.get("finger_id"))
    except (TypeError, ValueError):
        return (
            jsonify(ok=False, code="bad_request", message="No fingerprint id supplied."),
            400,
        )

    direction = payload.get("direction")
    if direction is not None and direction not in DIRECTIONS:
        return jsonify(ok=False, code="bad_request", message="Unknown direction."), 400

    device_label = str(
        payload.get("device_label") or current_app.config["KIOSK_DEVICE_LABEL"]
    )[:64]

    credential = attendance.find_fingerprint(device_label, finger_id)
    if credential is None:
        # Logged, because a run of these means a reader was re-enrolled without
        # the office being told - not something to leave to guesswork.
        current_app.logger.info(
            "Unregistered fingerprint slot %s on %r", finger_id, device_label
        )
        return jsonify(
            ok=False,
            code="finger_unknown",
            message="That finger is not registered. Please see the office.",
        )

    employee = credential.employee
    if not employee.is_active:
        return jsonify(
            ok=False,
            code="employee_inactive",
            message="That record is not active. Please see the office.",
        )

    result = attendance.record_clock(
        employee,
        direction=direction,
        method=METHOD_FINGER,
        device_label=device_label,
        cooldown_seconds=current_app.config["CLOCK_COOLDOWN_SECONDS"],
        commit=False,
    )
    credential.last_used_at = utcnow()
    db.session.commit()

    # No confidence figure: the device reported a match, not a similarity.
    return jsonify(**_result_payload(employee, result, None))


# --------------------------------------------------------------------------
# Payroll-number clocking
# --------------------------------------------------------------------------
#: A payroll reference is at most 32 characters in the database, so anything
#: longer is a stuck key or a paste, not a number somebody meant to type.
_MAX_REF_LENGTH = 32


@bp.post("/api/kiosk/payroll")
@csrf.exempt
@require_kiosk_token
@rate_limit("kiosk_payroll", "RECOGNISE_RATE_LIMIT", "RECOGNISE_RATE_WINDOW")
def api_payroll():
    """Clock whoever owns the payroll number typed in at the kiosk.

    Expects JSON ``{"payroll_ref": "E042", "direction": null}``.

    **This path recognises nobody.** A payroll number is printed on payslips and
    known to colleagues, so anybody who knows a number can clock as that person.
    That is the deliberate trade for having a way in when the camera cannot see
    somebody, and it is why the entry is written with ``METHOD_KEYPAD``: the log,
    the timesheets and the dashboard can all tell a typed entry from a
    recognised one.

    Rate limited like the recognition endpoints, so the keypad cannot be used to
    work through the numbers from the front of the queue.
    """
    if not current_app.config["KIOSK_KEYPAD_MODE"]:
        return (
            jsonify(
                ok=False,
                code="keypad_disabled",
                message="Clocking by payroll number is switched off.",
            ),
            403,
        )

    payload = request.get_json(silent=True) or {}
    reference = str(payload.get("payroll_ref") or "").strip()
    if not reference or len(reference) > _MAX_REF_LENGTH:
        return (
            jsonify(
                ok=False,
                code="bad_request",
                message="Enter your payroll number.",
            ),
            400,
        )

    direction = payload.get("direction")
    if direction is not None and direction not in DIRECTIONS:
        return jsonify(ok=False, code="bad_request", message="Unknown direction."), 400

    employee = attendance.find_by_payroll_ref(reference)
    if employee is None:
        # Logged without the number itself being treated as a secret - it is not
        # one - because a run of these usually means the numbers on the shop
        # floor do not match the ones in the database.
        current_app.logger.info("Unknown payroll number at kiosk: %r", reference)
        return jsonify(
            ok=False,
            code="ref_unknown",
            message="That payroll number is not recognised. Please see the office.",
        )

    if not employee.is_active:
        return jsonify(
            ok=False,
            code="employee_inactive",
            message="That record is not active. Please see the office.",
        )

    result = attendance.record_clock(
        employee,
        direction=direction,
        method=METHOD_KEYPAD,
        device_label=current_app.config["KIOSK_DEVICE_LABEL"],
        cooldown_seconds=current_app.config["CLOCK_COOLDOWN_SECONDS"],
    )
    # No confidence figure: nothing was matched against anything.
    return jsonify(**_result_payload(employee, result, None))


@bp.post("/api/kiosk/fingerprint/verify")
@csrf.exempt
@require_kiosk_token
@rate_limit("kiosk_finger", "RECOGNISE_RATE_LIMIT", "RECOGNISE_RATE_WINDOW")
def api_fingerprint_verify():
    """Clock somebody from a captured fingerprint template.

    Expects JSON ``{"template": "<base64>", "direction": null}``.

    For readers that hand back a template rather than a slot number. Matching
    happens here, server-side, for the same reason the hands-free path signs its
    tokens: the caller supplies a fingerprint, never an employee id, so holding
    the kiosk secret does not let anything clock an arbitrary person. It also
    keeps the enrolled templates on the server instead of copying them out to
    every kiosk.
    """
    from ..services.fingerprint import FingerprintError, decode_template, identify

    payload = request.get_json(silent=True) or {}

    direction = payload.get("direction")
    if direction is not None and direction not in DIRECTIONS:
        return jsonify(ok=False, code="bad_request", message="Unknown direction."), 400

    raw = payload.get("template")
    if not isinstance(raw, str) or not raw:
        return (
            jsonify(ok=False, code="bad_request", message="No fingerprint supplied."),
            400,
        )

    try:
        probe = decode_template(raw)
        outcome = identify(probe)
    except FingerprintError as exc:
        return jsonify(ok=False, code=exc.code, message=exc.message), 400

    if not outcome.accepted:
        # Logged with the reason: a run of "ambiguous" means two people are
        # enrolled too similarly, which is a setup problem, not a user error.
        current_app.logger.info(
            "Fingerprint refused: %s (best %.3f, runner-up %.3f)",
            outcome.reason,
            outcome.score,
            outcome.runner_up_score,
        )
        messages = {
            "no_templates": "No fingerprints are enrolled yet. Please see the office.",
            "below_threshold": "Fingerprint not recognised. Please try again.",
            "ambiguous": "That reading was unclear. Please try again.",
        }
        return jsonify(
            ok=False,
            code=outcome.reason,
            message=messages.get(outcome.reason, "Fingerprint not recognised."),
        )

    employee = db.session.get(Employee, outcome.employee_id)
    if employee is None or not employee.is_active:
        return jsonify(
            ok=False,
            code="employee_inactive",
            message="That record is not active. Please see the office.",
        )

    result = attendance.record_clock(
        employee,
        direction=direction,
        confidence=round(outcome.score, 4),
        method=METHOD_FINGER,
        device_label=current_app.config["KIOSK_DEVICE_LABEL"],
        cooldown_seconds=current_app.config["CLOCK_COOLDOWN_SECONDS"],
    )
    return jsonify(**_result_payload(employee, result, outcome.score))


@bp.get("/api/kiosk/onsite")
@require_kiosk_token
def api_onsite():
    """Who is currently on site - drives the counter on the kiosk screen."""
    people = attendance.currently_on_site()
    return jsonify(
        ok=True,
        count=len(people),
        names=[person.full_name for person in people],
    )
