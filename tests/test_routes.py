"""Route-level tests: authentication, kiosk token, and the scan endpoint."""

from __future__ import annotations

import datetime as dt

from app.face.engine import FaceObservation, NoFaceFound
from app.face.liveness import LivenessResult
from app.models import AttendanceEvent
from app.services import recognition

from .conftest import add_template, make_employee, nudge, unit_vector

TOKEN = "test-kiosk-token"


# --- public pages -------------------------------------------------------------
def test_kiosk_page_is_public(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Scan" in response.data


def test_healthz_reports_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


# --- admin authentication -----------------------------------------------------
def test_admin_requires_login(client):
    response = client.get("/admin/", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_succeeds_and_dashboard_loads(logged_in):
    response = logged_in.get("/admin/")
    assert response.status_code == 200
    assert b"Dashboard" in response.data


def test_login_rejects_a_wrong_password(client, admin):
    response = client.post("/login", data={"username": "office", "password": "wrong-password"})
    assert response.status_code == 401
    assert b"Incorrect username or password" in response.data


def test_login_gives_the_same_message_for_an_unknown_user(client, admin):
    """The form must not reveal which usernames exist."""
    response = client.post("/login", data={"username": "nobody", "password": "wrong-password"})
    assert response.status_code == 401
    assert b"Incorrect username or password" in response.data


def test_disabled_account_cannot_sign_in(client, db, admin):
    admin.is_active_flag = False
    db.session.commit()
    response = client.post(
        "/login", data={"username": "office", "password": "correct-horse-battery"}
    )
    assert response.status_code == 403


def test_next_parameter_cannot_redirect_off_site(client, admin):
    response = client.post(
        "/login?next=https://evil.example/steal",
        data={"username": "office", "password": "correct-horse-battery"},
    )
    assert response.status_code == 302
    assert response.headers["Location"] in ("/admin/", "http://localhost/admin/")


# --- kiosk token --------------------------------------------------------------
def test_scan_without_a_token_is_refused(client):
    response = client.post("/api/kiosk/scan", json={"frames": ["x"]})
    assert response.status_code == 403
    assert response.get_json()["code"] == "kiosk_unauthorised"


def test_scan_with_a_wrong_token_is_refused(client):
    response = client.post(
        "/api/kiosk/scan", json={"frames": ["x"]}, headers={"X-Kiosk-Token": "guess"}
    )
    assert response.status_code == 403


def test_onsite_requires_the_token(client):
    assert client.get("/api/kiosk/onsite").status_code == 403
    response = client.get("/api/kiosk/onsite", headers={"X-Kiosk-Token": TOKEN})
    assert response.status_code == 200
    assert response.get_json()["count"] == 0


# --- scanning, with the face engine stubbed out -------------------------------
def _fake_observation(vector):
    """A FaceObservation carrying a chosen embedding, bypassing OpenCV."""
    import numpy as np

    return FaceObservation(
        box=(10, 10, 120, 150),
        score=0.95,
        sharpness=400.0,
        embedding=vector,
        frame_shape=(480, 640),
        aligned=np.zeros((112, 112, 3), dtype=np.uint8),
    )


def _stub_engine(monkeypatch, vectors, error=None):
    """Make recognition.scan see *vectors* instead of running the real models."""

    def fake_observe_frames(frames, engine, **kwargs):
        if error is not None:
            return [], error
        return [_fake_observation(v) for v in vectors], None

    monkeypatch.setattr(recognition, "observe_frames", fake_observe_frames)
    monkeypatch.setattr(recognition, "get_engine", lambda app=None: object())
    # Liveness is exercised in its own tests; here it should not interfere.
    monkeypatch.setattr(
        recognition, "assess", lambda obs, **kwargs: LivenessResult(True, 5.0, 1.0)
    )


def test_scan_recognises_and_records(client, db, monkeypatch):
    employee = make_employee(db)
    face = unit_vector(1)
    add_template(db, employee, face)

    _stub_engine(monkeypatch, [nudge(face, 0.2), nudge(face, 0.25)])
    response = client.post(
        "/api/kiosk/scan", json={"frames": ["a", "b"]}, headers={"X-Kiosk-Token": TOKEN}
    )
    payload = response.get_json()

    assert payload["ok"] is True
    assert payload["recorded"] is True
    assert payload["direction"] == "in"
    assert payload["employee"]["payroll_ref"] == "E001"
    assert payload["next_direction"] == "out"
    assert db.session.query(AttendanceEvent).count() == 1


def test_second_scan_within_cooldown_records_nothing_new(client, db, monkeypatch):
    employee = make_employee(db)
    face = unit_vector(1)
    add_template(db, employee, face)
    _stub_engine(monkeypatch, [nudge(face, 0.2)])

    first = client.post(
        "/api/kiosk/scan",
        json={"frames": ["a"], "direction": "in"},
        headers={"X-Kiosk-Token": TOKEN},
    ).get_json()
    second = client.post(
        "/api/kiosk/scan",
        json={"frames": ["a"], "direction": "in"},
        headers={"X-Kiosk-Token": TOKEN},
    ).get_json()

    assert first["recorded"] is True
    assert second["ok"] is True
    assert second["recorded"] is False
    assert second["code"] == "duplicate"
    assert db.session.query(AttendanceEvent).count() == 1


def test_stranger_is_not_recorded(client, db, monkeypatch):
    employee = make_employee(db)
    add_template(db, employee, unit_vector(1))
    _stub_engine(monkeypatch, [unit_vector(500), unit_vector(501)])

    payload = client.post(
        "/api/kiosk/scan", json={"frames": ["a", "b"]}, headers={"X-Kiosk-Token": TOKEN}
    ).get_json()

    assert payload["ok"] is False
    assert payload["code"] == "not_recognised"
    assert db.session.query(AttendanceEvent).count() == 0


def test_scan_with_no_enrolled_faces_explains_itself(client, db, monkeypatch):
    _stub_engine(monkeypatch, [unit_vector(1)])
    payload = client.post(
        "/api/kiosk/scan", json={"frames": ["a"]}, headers={"X-Kiosk-Token": TOKEN}
    ).get_json()
    assert payload["code"] == "no_templates"


def test_inactive_employee_cannot_clock_in(client, db, monkeypatch):
    employee = make_employee(db)
    face = unit_vector(1)
    add_template(db, employee, face)
    employee.is_active = False
    db.session.commit()

    _stub_engine(monkeypatch, [nudge(face, 0.2)])
    payload = client.post(
        "/api/kiosk/scan", json={"frames": ["a"]}, headers={"X-Kiosk-Token": TOKEN}
    ).get_json()

    # An inactive employee is dropped from the index, so they are simply unknown.
    assert payload["ok"] is False
    assert db.session.query(AttendanceEvent).count() == 0


def test_undecodable_frames_report_a_helpful_message(client, db, monkeypatch):
    employee = make_employee(db)
    add_template(db, employee, unit_vector(1))
    _stub_engine(monkeypatch, [], error=NoFaceFound("No face was found. Please face the camera."))

    payload = client.post(
        "/api/kiosk/scan", json={"frames": ["not-an-image"]}, headers={"X-Kiosk-Token": TOKEN}
    ).get_json()

    assert payload["ok"] is False
    assert payload["code"] == "no_face"
    assert "face the camera" in payload["message"]


def test_bad_direction_is_a_bad_request(client):
    response = client.post(
        "/api/kiosk/scan",
        json={"frames": ["a"], "direction": "sideways"},
        headers={"X-Kiosk-Token": TOKEN},
    )
    assert response.status_code == 400


def test_scan_is_rate_limited(client, db, monkeypatch, app):
    employee = make_employee(db)
    face = unit_vector(1)
    add_template(db, employee, face)
    _stub_engine(monkeypatch, [nudge(face, 0.2)])
    app.config["RECOGNISE_RATE_LIMIT"] = 3

    codes = [
        client.post(
            "/api/kiosk/scan", json={"frames": ["a"]}, headers={"X-Kiosk-Token": TOKEN}
        ).status_code
        for _ in range(5)
    ]
    assert codes.count(429) == 2


# --- admin pages load ---------------------------------------------------------
def test_admin_pages_render(logged_in, db):
    employee = make_employee(db)
    for path in (
        "/admin/",
        "/admin/employees",
        "/admin/employees/new",
        f"/admin/employees/{employee.id}",
        f"/admin/employees/{employee.id}/edit",
        f"/admin/employees/{employee.id}/enrol",
        "/admin/absence",
        "/admin/timesheets",
        "/admin/shifts",
        "/admin/events/manual",
        "/admin/camera-check",
    ):
        response = logged_in.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"


def test_absence_page_names_who_has_not_clocked_in(logged_in, db):
    from app.models import DIRECTION_IN, AttendanceEvent, utcnow

    present = make_employee(db, ref="E001", first="Alice", last="Turner")
    make_employee(db, ref="E002", first="Bob", last="Shaw")
    db.session.add(
        AttendanceEvent(
            employee_id=present.id, direction=DIRECTION_IN, occurred_at=utcnow()
        )
    )
    db.session.commit()

    response = logged_in.get("/admin/absence")
    assert response.status_code == 200
    body = response.data.decode()
    assert "Bob Shaw" in body
    assert "Not clocked in" in body
    # Alice is on site, so she must not be listed as an absentee.
    assert body.index("Not clocked in") < body.index("Bob Shaw")


def test_absence_page_accepts_a_past_day(logged_in, db):
    make_employee(db, ref="E001")
    response = logged_in.get("/admin/absence?day=2026-01-12")
    assert response.status_code == 200
    assert b"Monday 12 January 2026" in response.data


def test_absence_page_ignores_a_nonsense_day(logged_in, db):
    """A hand-edited query string falls back to today rather than erroring."""
    make_employee(db, ref="E001")
    response = logged_in.get("/admin/absence?day=not-a-date")
    assert response.status_code == 200


def test_shift_pattern_can_be_added_edited_and_deleted(logged_in, db):
    from app.models import ShiftPattern

    response = logged_in.post(
        "/admin/shifts",
        data={
            "name": "Standard day",
            "start_time": "07:30",
            "end_time": "16:00",
            "unpaid_break_minutes": "30",
            "is_default": "y",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    pattern = db.session.query(ShiftPattern).one()
    assert pattern.is_default
    assert pattern.unpaid_break_minutes == 30

    response = logged_in.post(
        f"/admin/shifts/{pattern.id}/edit",
        data={
            "name": "Standard day",
            "start_time": "07:30",
            "end_time": "16:30",
            "unpaid_break_minutes": "30",
            "is_default": "y",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert pattern.end_time.strftime("%H:%M") == "16:30"

    response = logged_in.post(f"/admin/shifts/{pattern.id}/delete", follow_redirects=True)
    assert response.status_code == 200
    assert db.session.query(ShiftPattern).count() == 0


# --- the add/edit popups on the Shifts and hours page -------------------------
def _dialog(body: str, dialog_id: str) -> str:
    """The opening tag of one dialog, where its data- attributes live."""
    start = body.index(f'id="{dialog_id}"')
    return body[body.rindex("<dialog", 0, start) : body.index(">", start) + 1]


def test_the_add_forms_start_closed_behind_a_button(logged_in, db):
    body = logged_in.get("/admin/shifts").data.decode("utf-8")

    # Both popups are in the page, with a button to open each.
    assert 'data-dialog-open="shift-dialog"' in body
    assert 'data-dialog-open="week-dialog"' in body
    assert ">Add a shift<" in body
    assert ">Add a standard week<" in body
    # ...and neither opens on a plain page load.
    assert "data-open" not in _dialog(body, "shift-dialog")
    assert "data-open" not in _dialog(body, "week-dialog")


def test_editing_a_shift_opens_its_popup_ready_filled(logged_in, db):
    from app.models import ShiftPattern

    pattern = ShiftPattern(
        name="Earlies", start_time=dt.time(6, 0), end_time=dt.time(14, 0)
    )
    db.session.add(pattern)
    db.session.commit()

    body = logged_in.get(f"/admin/shifts/{pattern.id}/edit").data.decode("utf-8")
    tag = _dialog(body, "shift-dialog")

    assert 'data-open="true"' in tag
    # Closing it any way at all returns to the plain page, so a later "Add"
    # click cannot re-open the form still pointed at this record.
    assert 'data-return-to="/admin/shifts"' in tag
    assert 'value="Earlies"' in body
    assert "Edit Earlies" in body
    assert "data-open" not in _dialog(body, "week-dialog")  # the other stays shut


def test_editing_a_standard_week_opens_its_own_popup(logged_in, db):
    from app.models import WorkingWeek

    week = WorkingWeek(name="32-hour week", hours=32.0)
    db.session.add(week)
    db.session.commit()

    body = logged_in.get(f"/admin/shifts/weeks/{week.id}/edit").data.decode("utf-8")

    assert 'data-open="true"' in _dialog(body, "week-dialog")
    assert "data-open" not in _dialog(body, "shift-dialog")
    assert 'value="32-hour week"' in body


def test_a_rejected_shift_reopens_the_popup_with_the_typed_values(logged_in, db):
    """A name clash must come back inside the dialog, not behind it."""
    from app.models import ShiftPattern

    data = {
        "name": "Standard day",
        "start_time": "07:30",
        "end_time": "16:00",
        "unpaid_break_minutes": "30",
    }
    logged_in.post("/admin/shifts", data=data, follow_redirects=True)
    body = logged_in.post("/admin/shifts", data=data).data.decode("utf-8")

    assert 'data-open="true"' in _dialog(body, "shift-dialog")
    # The message is on the field, inside the dialog - not a flash behind it.
    assert '<div class="mf-error">A shift called' in body
    assert 'value="Standard day"' in body  # what they typed is still there
    assert db.session.query(ShiftPattern).count() == 1


def test_a_rejected_standard_week_reopens_its_popup(logged_in, db):
    body = logged_in.post(
        "/admin/shifts/weeks", data={"name": "Silly", "hours": "500"}
    ).data.decode("utf-8")

    assert 'data-open="true"' in _dialog(body, "week-dialog")
    assert '<div class="mf-error">Between 1 and 168 hours.' in body
    assert "data-open" not in _dialog(body, "shift-dialog")


def test_the_popups_work_without_javascript(logged_in, db):
    """A dialog is hidden with JS off, so the page falls back to inline forms."""
    body = logged_in.get("/admin/shifts").data.decode("utf-8")
    assert "js/dialogs.js" in body
    assert "<noscript>" in body
    assert "position: static" in body


def test_pay_beyond_end_survives_a_round_trip_through_the_form(logged_in, db):
    """Ticking and unticking the late-finish switch must both stick."""
    from app.models import ShiftPattern

    data = {
        "name": "Standard day",
        "start_time": "07:30",
        "end_time": "16:00",
        "unpaid_break_minutes": "30",
        "pay_beyond_end": "y",
    }
    logged_in.post("/admin/shifts", data=data, follow_redirects=True)
    pattern = db.session.query(ShiftPattern).one()
    assert pattern.pay_beyond_end is True

    logged_in.post(
        f"/admin/shifts/{pattern.id}/edit",
        data={k: v for k, v in data.items() if k != "pay_beyond_end"},
        follow_redirects=True,
    )
    assert pattern.pay_beyond_end is False


def test_standard_week_can_be_added_edited_and_deleted(logged_in, db):
    from app.models import WorkingWeek

    response = logged_in.post(
        "/admin/shifts/weeks",
        data={"name": "40-hour week", "hours": "40", "is_default": "y"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    week = db.session.query(WorkingWeek).one()
    assert week.hours == 40.0
    assert week.is_default

    response = logged_in.post(
        f"/admin/shifts/weeks/{week.id}/edit",
        data={"name": "37.5-hour week", "hours": "37.5", "is_default": "y"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert week.hours == 37.5
    assert week.name == "37.5-hour week"

    response = logged_in.post(
        f"/admin/shifts/weeks/{week.id}/delete", follow_redirects=True
    )
    assert response.status_code == 200
    assert db.session.query(WorkingWeek).count() == 0


def test_only_one_standard_week_is_the_default(logged_in, db):
    from app.models import WorkingWeek

    for name, hours in (("40-hour week", "40"), ("32-hour week", "32")):
        logged_in.post(
            "/admin/shifts/weeks",
            data={"name": name, "hours": hours, "is_default": "y"},
            follow_redirects=True,
        )
    weeks = db.session.query(WorkingWeek).all()
    assert [w.name for w in weeks if w.is_default] == ["32-hour week"]


def test_duplicate_standard_week_name_is_rejected(logged_in, db):
    from app.models import WorkingWeek

    data = {"name": "40-hour week", "hours": "40"}
    logged_in.post("/admin/shifts/weeks", data=data, follow_redirects=True)
    response = logged_in.post("/admin/shifts/weeks", data=data, follow_redirects=True)
    assert b"already exists" in response.data
    assert db.session.query(WorkingWeek).count() == 1


def test_deleting_a_standard_week_falls_back_to_the_default(logged_in, db):
    from app.models import WorkingWeek

    week = WorkingWeek(name="32-hour week", hours=32.0)
    db.session.add(week)
    db.session.commit()
    employee = make_employee(db, working_week_id=week.id)

    logged_in.post(f"/admin/shifts/weeks/{week.id}/delete", follow_redirects=True)
    assert employee.working_week_id is None


def test_employee_form_saves_the_standard_week(logged_in, db):
    from app.models import Employee, WorkingWeek

    week = WorkingWeek(name="32-hour week", hours=32.0)
    db.session.add(week)
    db.session.commit()

    logged_in.post(
        "/admin/employees/new",
        data={
            "payroll_ref": "E200",
            "first_name": "Sam",
            "last_name": "Reid",
            "shift_pattern_id": "0",
            "working_week_id": str(week.id),
            "is_active": "y",
        },
        follow_redirects=True,
    )
    employee = db.session.query(Employee).filter_by(payroll_ref="E200").one()
    assert employee.working_week_id == week.id


def test_timesheets_default_to_whole_monday_to_sunday_weeks(logged_in, db):
    """The page's own dates come back as a Monday and a Sunday."""
    import re

    make_employee(db)
    response = logged_in.get("/admin/timesheets")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    dates = re.findall(r'name="(start|end)" value="(\d{4}-\d{2}-\d{2})"', body)
    found = dict(dates)
    assert dt.date.fromisoformat(found["start"]).weekday() == 0  # Monday
    assert dt.date.fromisoformat(found["end"]).weekday() == 6  # Sunday


def test_part_week_range_warns_about_the_overtime_figure(logged_in, db):
    make_employee(db)
    response = logged_in.get("/admin/timesheets?start=2026-01-06&end=2026-01-20")
    assert b"does not cover whole Monday" in response.data
    # And offers the widened range: Mon 05/01 to Sun 25/01.
    assert b"start=2026-01-05&amp;end=2026-01-25" in response.data


def test_weekly_sheet_csv_downloads(logged_in, db):
    make_employee(db)
    response = logged_in.get("/admin/timesheets/weekly.csv?start=2026-01-05&end=2026-01-18")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "weekly_sheet" in response.headers["Content-Disposition"]
    assert b"Overtime hours" in response.data


def test_duplicate_shift_name_is_rejected(logged_in, db):
    from app.models import ShiftPattern

    data = {
        "name": "Standard day",
        "start_time": "07:30",
        "end_time": "16:00",
        "unpaid_break_minutes": "30",
    }
    logged_in.post("/admin/shifts", data=data, follow_redirects=True)
    response = logged_in.post("/admin/shifts", data=data, follow_redirects=True)
    assert b"already exists" in response.data
    assert db.session.query(ShiftPattern).count() == 1


def test_timesheet_csv_downloads(logged_in, db):
    make_employee(db)
    response = logged_in.get("/admin/timesheets.csv?start=2026-01-01&end=2026-01-31")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    assert b"Payroll ref" in response.data


def test_master_sheet_csv_downloads(logged_in, db):
    make_employee(db)
    response = logged_in.get("/admin/timesheets/master.csv?start=2026-01-01&end=2026-01-31")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "master_sheet" in response.headers["Content-Disposition"]
    assert b"Paid hours" in response.data


def test_duplicate_payroll_ref_is_rejected(logged_in, db):
    make_employee(db, ref="E001")
    response = logged_in.post(
        "/admin/employees/new",
        data={
            "payroll_ref": "E001",
            "first_name": "Bob",
            "last_name": "Smith",
            "is_active": "y",
        },
    )
    assert response.status_code == 200
    assert b"already in use" in response.data


def test_manual_entry_records_an_event_with_an_audit_note(logged_in, db):
    employee = make_employee(db)
    response = logged_in.post(
        "/admin/events/manual",
        data={
            "employee_id": str(employee.id),
            "direction": "in",
            "occurred_at": "2026-01-12 07:30",
            "note": "Camera down, time confirmed with supervisor",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    event = db.session.query(AttendanceEvent).one()
    assert event.method == "manual"
    assert "office" in event.note
    assert "supervisor" in event.note
    # 07:30 local in January is 07:30 UTC.
    assert event.occurred_at.strftime("%Y-%m-%d %H:%M") == "2026-01-12 07:30"


# --- missing models -----------------------------------------------------------
def test_scan_explains_itself_when_models_are_absent(client, db, monkeypatch, app, tmp_path):
    """Forgetting fetch_models.py must give a clear message, not a 500."""
    employee = make_employee(db)
    add_template(db, employee, unit_vector(1))
    app.config["FACE_DETECTOR_MODEL"] = tmp_path / "absent.onnx"
    app.config["FACE_RECOGNISER_MODEL"] = tmp_path / "absent2.onnx"
    app.extensions.pop("face_engine", None)

    response = client.post(
        "/api/kiosk/scan", json={"frames": ["a"]}, headers={"X-Kiosk-Token": TOKEN}
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["code"] == "models_missing"
    assert db.session.query(AttendanceEvent).count() == 0


def test_healthz_reports_missing_models(client, app, tmp_path):
    app.config["FACE_DETECTOR_MODEL"] = tmp_path / "absent.onnx"
    response = client.get("/healthz")
    assert response.status_code == 503
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["models"] is False
    assert payload["database"] is True


def test_enrolment_explains_itself_when_models_are_absent(logged_in, db, app, tmp_path):
    employee = make_employee(db)
    app.config["FACE_DETECTOR_MODEL"] = tmp_path / "absent.onnx"
    app.config["FACE_RECOGNISER_MODEL"] = tmp_path / "absent2.onnx"
    app.extensions.pop("face_engine", None)

    payload = logged_in.post(
        f"/admin/employees/{employee.id}/enrol", json={"frames": ["a", "b", "c"]}
    ).get_json()
    assert payload["ok"] is False
    assert payload["code"] == "models_missing"
    assert "fetch_models" in payload["message"]


# --- the employee form, including the email field ------------------------------
def test_employee_can_be_created_with_an_email(logged_in, db):
    """Regression test: the Email() validator needs the email_validator package.

    Earlier tests omitted the email field, so Optional() short-circuited and the
    validator never ran - the missing dependency only surfaced in the browser.
    """
    from app.models import Employee

    response = logged_in.post(
        "/admin/employees/new",
        data={
            "payroll_ref": "E900",
            "first_name": "Priya",
            "last_name": "Shah",
            "department": "Joinery",
            "email": "priya.shah@example.com",
            "is_active": "y",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    employee = db.session.query(Employee).filter_by(payroll_ref="E900").one()
    assert employee.email == "priya.shah@example.com"


def test_a_malformed_email_is_rejected(logged_in, db):
    from app.models import Employee

    response = logged_in.post(
        "/admin/employees/new",
        data={
            "payroll_ref": "E901",
            "first_name": "Tom",
            "last_name": "Green",
            "email": "not-an-email-address",
            "is_active": "y",
        },
    )
    assert response.status_code == 200
    assert db.session.query(Employee).filter_by(payroll_ref="E901").first() is None


def test_employee_can_be_edited_with_an_email(logged_in, db):
    employee = make_employee(db, ref="E902")
    response = logged_in.post(
        f"/admin/employees/{employee.id}/edit",
        data={
            "payroll_ref": "E902",
            "first_name": "Alice",
            "last_name": "Turner",
            "email": "alice.turner@example.com",
            "is_active": "y",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    db.session.refresh(employee)
    assert employee.email == "alice.turner@example.com"
