"""Back office: employees, enrolment, timesheets and corrections."""

from __future__ import annotations

import datetime as dt

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import select
from wtforms import (
    BooleanField,
    FloatField,
    IntegerField,
    SelectField,
    StringField,
    TextAreaField,
    TimeField,
)
from wtforms.validators import DataRequired, Email, Length, NumberRange, Optional

from ..extensions import db
from ..models import (
    DIRECTION_IN,
    DIRECTION_OUT,
    DIRECTIONS,
    METHOD_MANUAL,
    FINGER_POSITIONS,
    AttendanceEvent,
    Employee,
    FingerprintCredential,
    ShiftPattern,
    WorkingWeek,
    visible_employee_clause,
)
from ..services import attendance
from ..services.enrolment import enrol_employee, remove_enrolment
from ..services.recognition import get_index
from ..services.timesheet import (
    build_timesheet,
    default_range,
    get_timezone,
    is_whole_weeks,
    list_departments,
    summarise,
    to_csv,
    to_local,
    to_master_csv,
    to_weekly_csv,
    week_end,
    week_start,
    weekly_totals,
    whole_weeks,
)

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.before_request
@login_required
def require_login():
    """Every route in this blueprint needs a signed-in administrator."""
    return None


# --------------------------------------------------------------------------
# Forms
# --------------------------------------------------------------------------
class EmployeeForm(FlaskForm):
    payroll_ref = StringField("Payroll reference", validators=[DataRequired(), Length(max=32)])
    first_name = StringField("First name", validators=[DataRequired(), Length(max=64)])
    last_name = StringField("Surname", validators=[DataRequired(), Length(max=64)])
    department = StringField("Department", validators=[Optional(), Length(max=64)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=190)])
    shift_pattern_id = SelectField("Shift", coerce=int, validators=[Optional()])
    working_week_id = SelectField(
        "Standard working hours per week", coerce=int, validators=[Optional()]
    )
    is_active = BooleanField("Active", default=True)


class ShiftPatternForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=64)])
    start_time = TimeField("Start (local time)", validators=[DataRequired()])
    end_time = TimeField("End (local time)", validators=[DataRequired()])
    unpaid_break_minutes = IntegerField(
        "Unpaid break (minutes)",
        default=0,
        validators=[NumberRange(min=0, max=480, message="Between 0 and 480 minutes.")],
    )
    break_applies_after_minutes = IntegerField(
        "Deduct break only after (minutes worked)",
        default=360,
        validators=[
            Optional(),
            NumberRange(min=0, max=1440, message="Between 0 and 1440 minutes."),
        ],
    )
    pay_beyond_end = BooleanField(
        "Pay time worked after the shift end (counts as overtime)", default=True
    )
    is_default = BooleanField("Default shift for employees without one assigned")


class WorkingWeekForm(FlaskForm):
    """A contracted week length. Hours past it in a Monday-Sunday week are overtime."""

    name = StringField("Name", validators=[DataRequired(), Length(max=64)])
    hours = FloatField(
        "Standard working hours per week",
        validators=[
            DataRequired(message="Enter the contracted hours per week."),
            NumberRange(min=1, max=168, message="Between 1 and 168 hours."),
        ],
    )
    is_default = BooleanField("Default for employees without one assigned")


class ManualEventForm(FlaskForm):
    employee_id = SelectField("Employee", coerce=int, validators=[DataRequired()])
    direction = SelectField(
        "Direction",
        choices=[(DIRECTION_IN, "Clock in"), (DIRECTION_OUT, "Clock out")],
        validators=[DataRequired()],
    )
    occurred_at = StringField(
        "Date and time (local)", validators=[DataRequired(), Length(max=32)]
    )
    note = TextAreaField("Reason", validators=[DataRequired(), Length(max=500)])


class FingerprintForm(FlaskForm):
    """Registers a slot on a reader against an employee.

    Enrolment itself happens on the reader, which keeps the fingerprint. This
    form only records whose slot is whose, so there is no biometric data on it.
    """

    finger_id = IntegerField(
        "Slot number on the reader",
        validators=[
            DataRequired(message="Enter the slot number the reader reported."),
            NumberRange(min=0, max=65535, message="Between 0 and 65535."),
        ],
    )
    device_label = StringField("Reader", validators=[DataRequired(), Length(max=64)])
    label = StringField("Which finger (optional)", validators=[Optional(), Length(max=64)])


class VoidForm(FlaskForm):
    """Just a CSRF-protected submit for voiding one event."""

    note = StringField("Reason", validators=[DataRequired(), Length(max=200)])


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _tz():
    return get_timezone(current_app.config["TIMEZONE"])


def _parse_date(raw: str | None, default: dt.date) -> dt.date:
    if not raw:
        return default
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return default


def _local_to_utc(raw: str) -> dt.datetime:
    """Parse an operator-entered local date/time into a naive UTC timestamp."""
    text = raw.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"):
        try:
            naive = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return (
            naive.replace(tzinfo=_tz())
            .astimezone(dt.timezone.utc)
            .replace(tzinfo=None)
        )
    raise ValueError("Use the format YYYY-MM-DD HH:MM")


def _employee_choices() -> list[tuple[int, str]]:
    employees = db.session.scalars(
        select(Employee)
        .where(visible_employee_clause())
        .order_by(Employee.last_name, Employee.first_name)
    ).all()
    return [(e.id, f"{e.last_name}, {e.first_name} ({e.payroll_ref})") for e in employees]


def _shift_choices() -> list[tuple[int, str]]:
    """Options for the employee form; 0 stands in for "use the default shift"."""
    patterns = db.session.scalars(select(ShiftPattern).order_by(ShiftPattern.name)).all()
    return [(0, "Default shift")] + [(p.id, p.label) for p in patterns]


def _working_week_choices() -> list[tuple[int, str]]:
    """Options for the employee form; 0 means "use the default standard week"."""
    weeks = db.session.scalars(select(WorkingWeek).order_by(WorkingWeek.hours)).all()
    default = next((w for w in weeks if w.is_default), None)
    label = f"Default ({default.hours_text} hours)" if default else "Default"
    return [(0, label)] + [(w.id, w.label) for w in weeks]


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@bp.get("/")
def dashboard():
    tz = _tz()
    today = dt.datetime.now(tz).date()
    on_site = attendance.currently_on_site()

    recent = db.session.scalars(
        select(AttendanceEvent)
        .join(Employee)
        .where(AttendanceEvent.is_voided.is_(False), visible_employee_clause())
        .order_by(AttendanceEvent.occurred_at.desc())
        .limit(15)
    ).all()

    employees = db.session.scalars(
        select(Employee).where(visible_employee_clause())
    ).all()
    index = get_index()

    return render_template(
        "admin/dashboard.html",
        today=today,
        on_site=on_site,
        recent=recent,
        employee_count=len(employees),
        active_count=sum(1 for e in employees if e.is_active),
        enrolled_count=sum(1 for e in employees if e.is_enrolled),
        index_size=index.size,
    )


# --------------------------------------------------------------------------
# Employees
# --------------------------------------------------------------------------
@bp.get("/employees")
def employees():
    query = (request.args.get("q") or "").strip()
    stmt = (
        select(Employee)
        .where(visible_employee_clause())
        .order_by(Employee.last_name, Employee.first_name)
    )
    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            Employee.first_name.ilike(like)
            | Employee.last_name.ilike(like)
            | Employee.payroll_ref.ilike(like)
            | Employee.department.ilike(like)
        )
    return render_template(
        "admin/employees.html", employees=db.session.scalars(stmt).all(), query=query
    )


@bp.route("/employees/new", methods=["GET", "POST"])
def employee_new():
    form = EmployeeForm()
    form.shift_pattern_id.choices = _shift_choices()
    form.working_week_id.choices = _working_week_choices()
    if form.validate_on_submit():
        clash = db.session.scalars(
            select(Employee).where(Employee.payroll_ref == form.payroll_ref.data)
        ).first()
        if clash is not None:
            flash(f"Payroll reference {form.payroll_ref.data} is already in use.", "error")
            return render_template("admin/employee_form.html", form=form, employee=None)

        employee = Employee(
            payroll_ref=(form.payroll_ref.data or "").strip(),
            first_name=(form.first_name.data or "").strip(),
            last_name=(form.last_name.data or "").strip(),
            department=(form.department.data or "").strip() or None,
            email=(form.email.data or "").strip() or None,
            shift_pattern_id=form.shift_pattern_id.data or None,
            working_week_id=form.working_week_id.data or None,
            is_active=bool(form.is_active.data),
        )
        db.session.add(employee)
        db.session.commit()
        flash(f"Added {employee.full_name}. Now enrol their face.", "success")
        return redirect(url_for("admin.employee_enrol", employee_id=employee.id))

    return render_template("admin/employee_form.html", form=form, employee=None)


@bp.route("/employees/<int:employee_id>/edit", methods=["GET", "POST"])
def employee_edit(employee_id: int):
    employee = db.get_or_404(Employee, employee_id)
    form = EmployeeForm(obj=employee)
    form.shift_pattern_id.choices = _shift_choices()
    form.working_week_id.choices = _working_week_choices()
    if request.method == "GET":
        form.shift_pattern_id.data = employee.shift_pattern_id or 0
        form.working_week_id.data = employee.working_week_id or 0
    if form.validate_on_submit():
        clash = db.session.scalars(
            select(Employee).where(
                Employee.payroll_ref == form.payroll_ref.data, Employee.id != employee.id
            )
        ).first()
        if clash is not None:
            flash(f"Payroll reference {form.payroll_ref.data} is already in use.", "error")
            return render_template("admin/employee_form.html", form=form, employee=employee)

        employee.payroll_ref = (form.payroll_ref.data or "").strip()
        employee.first_name = (form.first_name.data or "").strip()
        employee.last_name = (form.last_name.data or "").strip()
        employee.department = (form.department.data or "").strip() or None
        employee.email = (form.email.data or "").strip() or None
        employee.shift_pattern_id = form.shift_pattern_id.data or None
        employee.working_week_id = form.working_week_id.data or None
        employee.is_active = bool(form.is_active.data)
        db.session.commit()
        flash(f"Updated {employee.full_name}.", "success")
        return redirect(url_for("admin.employees"))

    return render_template("admin/employee_form.html", form=form, employee=employee)


@bp.get("/employees/<int:employee_id>")
def employee_detail(employee_id: int):
    employee = db.get_or_404(Employee, employee_id)
    events = db.session.scalars(
        select(AttendanceEvent)
        .where(AttendanceEvent.employee_id == employee.id)
        .order_by(AttendanceEvent.occurred_at.desc())
        .limit(50)
    ).all()
    return render_template(
        "admin/employee_detail.html",
        employee=employee,
        events=events,
        void_form=VoidForm(),
        finger_form=FingerprintForm(
            device_label=current_app.config["KIOSK_DEVICE_LABEL"]
        ),
        finger_positions=FINGER_POSITIONS,
        clocked_in=attendance.is_clocked_in(employee.id),
    )


# --------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------
@bp.post("/employees/<int:employee_id>/fingerprint")
def employee_fingerprint_add(employee_id: int):
    """Record which slot on the reader belongs to this employee."""
    employee = db.get_or_404(Employee, employee_id)
    form = FingerprintForm()
    if not form.validate_on_submit():
        for field in (form.finger_id, form.device_label):
            for error in field.errors:
                flash(error, "error")
        return redirect(url_for("admin.employee_detail", employee_id=employee.id))

    device_label = (form.device_label.data or "").strip()
    finger_id = int(form.finger_id.data)

    clash = db.session.scalars(
        select(FingerprintCredential).where(
            FingerprintCredential.device_label == device_label,
            FingerprintCredential.finger_id == finger_id,
        )
    ).first()
    if clash is not None:
        # Naming the holder matters: the usual cause is re-using a slot that was
        # never cleared, which would otherwise clock the wrong person.
        flash(
            f"Slot {finger_id} on {device_label!r} is already registered to "
            f"{clash.employee.full_name}. Remove that one first, or use a free slot.",
            "error",
        )
        return redirect(url_for("admin.employee_detail", employee_id=employee.id))

    db.session.add(
        FingerprintCredential(
            employee_id=employee.id,
            device_label=device_label,
            finger_id=finger_id,
            label=(form.label.data or "").strip() or None,
            created_by_id=current_user.id,
        )
    )
    db.session.commit()
    flash(
        f"Slot {finger_id} on {device_label!r} now clocks {employee.full_name}.",
        "success",
    )
    return redirect(url_for("admin.employee_detail", employee_id=employee.id))


@bp.post("/employees/<int:employee_id>/fingerprint-templates/delete")
def employee_fingerprint_templates_clear(employee_id: int):
    """Delete an employee's stored fingerprint templates (biometric data)."""
    from ..services.fingerprint import remove_templates

    employee = db.get_or_404(Employee, employee_id)
    removed = remove_templates(employee)
    flash(
        f"Removed {removed} fingerprint sample(s) for {employee.full_name}.", "success"
    )
    return redirect(url_for("admin.employee_detail", employee_id=employee.id))


@bp.post("/fingerprints/<int:credential_id>/delete")
def fingerprint_delete(credential_id: int):
    """Unregister a slot. The fingerprint itself is cleared on the reader."""
    credential = db.get_or_404(FingerprintCredential, credential_id)
    employee_id = credential.employee_id
    name = credential.employee.full_name
    device_label, finger_id = credential.device_label, credential.finger_id
    db.session.delete(credential)
    db.session.commit()
    flash(
        f"Slot {finger_id} on {device_label!r} no longer clocks {name}. "
        "Delete the fingerprint on the reader itself as well.",
        "success",
    )
    return redirect(url_for("admin.employee_detail", employee_id=employee_id))


# --------------------------------------------------------------------------
# Enrolment
# --------------------------------------------------------------------------
@bp.get("/employees/<int:employee_id>/enrol")
def employee_enrol(employee_id: int):
    employee = db.get_or_404(Employee, employee_id)
    return render_template(
        "admin/enrol.html",
        employee=employee,
        min_samples=current_app.config["ENROL_MIN_SAMPLES"],
        max_samples=current_app.config["ENROL_MAX_SAMPLES"],
    )


@bp.post("/employees/<int:employee_id>/enrol")
def employee_enrol_submit(employee_id: int):
    """Receive captured samples from the enrolment page (JSON, session-authenticated)."""
    employee = db.get_or_404(Employee, employee_id)
    payload = request.get_json(silent=True) or {}
    frames = payload.get("frames") or []
    if not isinstance(frames, list) or not frames:
        return jsonify(ok=False, code="no_frames", message="No samples were captured."), 400

    outcome = enrol_employee(
        employee,
        [f for f in frames if isinstance(f, str)][: current_app.config["ENROL_MAX_SAMPLES"] + 4],
        admin_id=current_user.id,
        replace_existing=bool(payload.get("replace")),
    )
    return jsonify(
        ok=outcome.ok,
        code=outcome.code,
        message=outcome.message,
        added=outcome.added,
        rejected=outcome.rejected,
        template_count=len(employee.templates),
    )


@bp.post("/employees/<int:employee_id>/enrol/clear")
def employee_enrol_clear(employee_id: int):
    employee = db.get_or_404(Employee, employee_id)
    removed = remove_enrolment(employee)
    flash(f"Removed {removed} face sample(s) for {employee.full_name}.", "success")
    return redirect(url_for("admin.employee_detail", employee_id=employee.id))


# --------------------------------------------------------------------------
# Timesheets
# --------------------------------------------------------------------------
def _timesheet_args():
    """The filters from the query string, defaulting to four whole weeks.

    The default range is Monday-Sunday aligned because overtime is settled per
    week: a range starting mid-week would compare part of a week against a full
    week's contract and under-report the overtime in it.
    """
    tz = _tz()
    today = dt.datetime.now(tz).date()
    default_start, default_end = default_range(today)
    start = _parse_date(request.args.get("start"), default_start)
    end = _parse_date(request.args.get("end"), default_end)
    if end < start:
        start, end = end, start
    raw_employee = request.args.get("employee_id")
    employee_id = int(raw_employee) if raw_employee and raw_employee.isdigit() else None
    department = (request.args.get("department") or "").strip() or None
    return start, end, employee_id, department, tz


def _week_shortcuts(today: dt.date) -> list[tuple[str, dt.date, dt.date]]:
    """Monday-Sunday quick ranges for the timesheet filter row."""
    this_monday = week_start(today)
    last_monday = this_monday - dt.timedelta(days=7)
    return [
        ("This week", this_monday, week_end(today)),
        ("Last week", last_monday, week_end(last_monday)),
        ("Last 4 weeks", *default_range(today)),
        ("Last 13 weeks", *default_range(today, weeks=13)),
    ]


@bp.get("/timesheets")
def timesheets():
    start, end, employee_id, department, tz = _timesheet_args()
    shifts = build_timesheet(
        start, end, tz, employee_id=employee_id, department=department
    )
    totals = summarise(shifts)
    snapped_start, snapped_end = whole_weeks(start, end)
    return render_template(
        "admin/timesheets.html",
        shifts=shifts,
        totals=totals,
        weeks=weekly_totals(totals),
        start=start,
        end=end,
        whole_weeks=is_whole_weeks(start, end),
        snapped_start=snapped_start,
        snapped_end=snapped_end,
        shortcuts=_week_shortcuts(dt.datetime.now(tz).date()),
        employee_id=employee_id,
        department=department,
        departments=list_departments(),
        employees=db.session.scalars(
            select(Employee)
            .where(visible_employee_clause())
            .order_by(Employee.last_name, Employee.first_name)
        ).all(),
    )


@bp.get("/timesheets.csv")
def timesheets_csv():
    start, end, employee_id, department, tz = _timesheet_args()
    shifts = build_timesheet(
        start, end, tz, employee_id=employee_id, department=department
    )
    filename = f"timesheet_{start.isoformat()}_to_{end.isoformat()}.csv"
    return Response(
        to_csv(shifts),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.get("/timesheets/master.csv")
def timesheets_master_csv():
    """One line per person, paid hours split into standard and overtime."""
    start, end, employee_id, department, tz = _timesheet_args()
    shifts = build_timesheet(
        start, end, tz, employee_id=employee_id, department=department
    )
    filename = f"master_sheet_{start.isoformat()}_to_{end.isoformat()}.csv"
    return Response(
        to_master_csv(summarise(shifts)),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.get("/timesheets/weekly.csv")
def timesheets_weekly_csv():
    """One line per person per Monday-Sunday week - where overtime is decided."""
    start, end, employee_id, department, tz = _timesheet_args()
    shifts = build_timesheet(
        start, end, tz, employee_id=employee_id, department=department
    )
    filename = f"weekly_sheet_{start.isoformat()}_to_{end.isoformat()}.csv"
    return Response(
        to_weekly_csv(summarise(shifts)),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------
# Shift patterns
# --------------------------------------------------------------------------
def _save_shift_pattern(form: ShiftPatternForm, pattern: ShiftPattern | None) -> bool:
    """Apply the form to *pattern* (or a new one). Returns False on a name clash."""
    clash = db.session.scalars(
        select(ShiftPattern).where(
            ShiftPattern.name == (form.name.data or "").strip(),
            ShiftPattern.id != (pattern.id if pattern else 0),
        )
    ).first()
    if clash is not None:
        flash(f"A shift called {form.name.data!r} already exists.", "error")
        return False

    if pattern is None:
        pattern = ShiftPattern()
        db.session.add(pattern)
    pattern.name = (form.name.data or "").strip()
    pattern.start_time = form.start_time.data
    pattern.end_time = form.end_time.data
    pattern.unpaid_break_minutes = int(form.unpaid_break_minutes.data or 0)
    threshold = form.break_applies_after_minutes.data
    pattern.break_applies_after_minutes = 360 if threshold is None else int(threshold)
    pattern.pay_beyond_end = bool(form.pay_beyond_end.data)

    if form.is_default.data:
        # Only one pattern can be the default at a time.
        for other in db.session.scalars(select(ShiftPattern)).all():
            other.is_default = other is pattern
        pattern.is_default = True
    else:
        pattern.is_default = False

    db.session.commit()
    return True


def _render_shifts(
    form: ShiftPatternForm | None = None,
    week_form: WorkingWeekForm | None = None,
    editing: ShiftPattern | None = None,
    editing_week: WorkingWeek | None = None,
):
    """The Shifts page: shift patterns and standard weeks, each with its form."""
    return render_template(
        "admin/shifts.html",
        patterns=db.session.scalars(select(ShiftPattern).order_by(ShiftPattern.name)).all(),
        weeks=db.session.scalars(select(WorkingWeek).order_by(WorkingWeek.hours)).all(),
        form=form or ShiftPatternForm(),
        week_form=week_form or WorkingWeekForm(),
        editing=editing,
        editing_week=editing_week,
    )


@bp.route("/shifts", methods=["GET", "POST"])
def shifts():
    form = ShiftPatternForm()
    if form.validate_on_submit() and _save_shift_pattern(form, None):
        flash(f"Added shift {form.name.data!r}.", "success")
        return redirect(url_for("admin.shifts"))
    return _render_shifts(form=form)


@bp.route("/shifts/<int:pattern_id>/edit", methods=["GET", "POST"])
def shift_edit(pattern_id: int):
    pattern = db.get_or_404(ShiftPattern, pattern_id)
    form = ShiftPatternForm(obj=pattern)
    if form.validate_on_submit() and _save_shift_pattern(form, pattern):
        flash(f"Updated shift {pattern.name!r}.", "success")
        return redirect(url_for("admin.shifts"))
    return _render_shifts(form=form, editing=pattern)


@bp.post("/shifts/<int:pattern_id>/delete")
def shift_delete(pattern_id: int):
    pattern = db.get_or_404(ShiftPattern, pattern_id)
    # Employees pointing at it fall back to the default shift (FK is SET NULL).
    for employee in list(pattern.employees):
        employee.shift_pattern_id = None
    name = pattern.name
    db.session.delete(pattern)
    db.session.commit()
    flash(
        f"Deleted shift {name!r}. Anyone assigned to it now uses the default shift.",
        "success",
    )
    return redirect(url_for("admin.shifts"))


# --------------------------------------------------------------------------
# Standard working weeks
# --------------------------------------------------------------------------
def _save_working_week(form: WorkingWeekForm, week: WorkingWeek | None) -> bool:
    """Apply the form to *week* (or a new one). Returns False on a name clash."""
    clash = db.session.scalars(
        select(WorkingWeek).where(
            WorkingWeek.name == (form.name.data or "").strip(),
            WorkingWeek.id != (week.id if week else 0),
        )
    ).first()
    if clash is not None:
        flash(f"A standard week called {form.name.data!r} already exists.", "error")
        return False

    if week is None:
        week = WorkingWeek()
        db.session.add(week)
    week.name = (form.name.data or "").strip()
    week.hours = float(form.hours.data)

    if form.is_default.data:
        # Only one standard week can be the default at a time.
        for other in db.session.scalars(select(WorkingWeek)).all():
            other.is_default = other is week
        week.is_default = True
    else:
        week.is_default = False

    db.session.commit()
    return True


@bp.post("/shifts/weeks")
def working_week_add():
    form = WorkingWeekForm()
    if form.validate_on_submit() and _save_working_week(form, None):
        flash(f"Added standard week {form.name.data!r}.", "success")
        return redirect(url_for("admin.shifts"))
    # Field errors are rendered inline next to the input, as on the shift form.
    return _render_shifts(week_form=form)


@bp.route("/shifts/weeks/<int:week_id>/edit", methods=["GET", "POST"])
def working_week_edit(week_id: int):
    week = db.get_or_404(WorkingWeek, week_id)
    form = WorkingWeekForm(obj=week)
    if form.validate_on_submit() and _save_working_week(form, week):
        flash(f"Updated standard week {week.name!r}.", "success")
        return redirect(url_for("admin.shifts"))
    return _render_shifts(week_form=form, editing_week=week)


@bp.post("/shifts/weeks/<int:week_id>/delete")
def working_week_delete(week_id: int):
    week = db.get_or_404(WorkingWeek, week_id)
    # Employees pointing at it fall back to the default week (FK is SET NULL).
    for employee in list(week.employees):
        employee.working_week_id = None
    name = week.name
    db.session.delete(week)
    db.session.commit()
    flash(
        f"Deleted standard week {name!r}. Anyone on it now uses the default week.",
        "success",
    )
    return redirect(url_for("admin.shifts"))


# --------------------------------------------------------------------------
# Corrections
# --------------------------------------------------------------------------
@bp.route("/events/manual", methods=["GET", "POST"])
def event_manual():
    form = ManualEventForm()
    form.employee_id.choices = _employee_choices()

    if form.validate_on_submit():
        employee = db.session.get(Employee, form.employee_id.data)
        if employee is None:
            flash("That employee no longer exists.", "error")
            return render_template("admin/event_manual.html", form=form)
        try:
            occurred_at = _local_to_utc(form.occurred_at.data or "")
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("admin/event_manual.html", form=form)

        result = attendance.record_clock(
            employee,
            direction=form.direction.data,
            method=METHOD_MANUAL,
            note=f"Manual entry by {current_user.username}: {form.note.data}",
            created_by_id=current_user.id,
            occurred_at=occurred_at,
            cooldown_seconds=0,  # an operator entering a correction means it
        )
        flash(
            f"Recorded manual {result.direction} for {employee.full_name} "
            f"at {to_local(occurred_at, _tz()).strftime('%d/%m/%Y %H:%M')}.",
            "success",
        )
        return redirect(url_for("admin.employee_detail", employee_id=employee.id))

    if not form.occurred_at.data:
        form.occurred_at.data = dt.datetime.now(_tz()).strftime("%Y-%m-%d %H:%M")
    return render_template("admin/event_manual.html", form=form)


@bp.post("/events/<int:event_id>/void")
def event_void(event_id: int):
    event = db.get_or_404(AttendanceEvent, event_id)
    form = VoidForm()
    if not form.validate_on_submit():
        flash("A reason is required to void an entry.", "error")
        return redirect(url_for("admin.employee_detail", employee_id=event.employee_id))

    attendance.void_event(event, admin_id=current_user.id, reason=form.note.data or "")
    flash("Entry voided. The original row is kept for the audit trail.", "success")
    return redirect(url_for("admin.employee_detail", employee_id=event.employee_id))


# --------------------------------------------------------------------------
# Camera diagnostics
# --------------------------------------------------------------------------
@bp.get("/camera-check")
def camera_check():
    """Measure what the kiosk camera actually produces.

    Face size, sharpness and motion vary enormously between cameras and
    lighting, so rather than guessing at thresholds this page reports the real
    numbers from a live capture. Set FACE_MIN_PIXELS and FACE_MIN_SHARPNESS in
    .env comfortably below what a cooperative employee measures here.
    """
    return render_template(
        "admin/camera_check.html",
        thresholds={
            "min_pixels": current_app.config["FACE_MIN_PIXELS"],
            "min_sharpness": current_app.config["FACE_MIN_SHARPNESS"],
            "match_threshold": current_app.config["FACE_MATCH_THRESHOLD"],
            "match_margin": current_app.config["FACE_MATCH_MARGIN"],
            "min_motion": current_app.config["LIVENESS_MIN_MOTION"],
            "presence_threshold": current_app.config["AUTO_PRESENCE_THRESHOLD"],
            "dominant_ratio": current_app.config["FACE_DOMINANT_RATIO"],
        },
    )


@bp.post("/camera-check")
def camera_check_submit():
    """Report measurements for posted frames without recording anything."""
    from ..face.engine import FaceError, ModelsMissing, decode_image
    from ..face.liveness import frame_consistency, frame_motion
    from ..services.recognition import get_engine

    payload = request.get_json(silent=True) or {}
    frames = [f for f in (payload.get("frames") or []) if isinstance(f, str)][:5]
    if not frames:
        return jsonify(ok=False, message="No frames were captured."), 400

    try:
        engine = get_engine()
    except ModelsMissing as exc:
        return jsonify(ok=False, message=str(exc)), 200
    observations = []
    problems = []
    for position, frame in enumerate(frames, start=1):
        try:
            # Quality gates are off here: the point is to measure, not to judge.
            observations.append(engine.observe(decode_image(frame), check_quality=False))
        except FaceError as exc:
            problems.append(f"Frame {position}: {exc.message}")

    if not observations:
        return jsonify(ok=False, message="No face was measured.", problems=problems)

    index = get_index()
    best = (
        index.match(
            observations[0].embedding,
            threshold=current_app.config["FACE_MATCH_THRESHOLD"],
            margin=current_app.config["FACE_MATCH_MARGIN"],
        )
        if index.size
        else None
    )
    matched = db.session.get(Employee, best.employee_id) if best and best.accepted else None

    return jsonify(
        ok=True,
        frames_measured=len(observations),
        problems=problems,
        face_pixels=[obs.width for obs in observations],
        sharpness=[round(obs.sharpness, 1) for obs in observations],
        detector_score=[round(obs.score, 3) for obs in observations],
        motion=round(frame_motion(observations), 2),
        consistency=round(frame_consistency(observations), 4),
        best_match={
            "employee": matched.full_name if matched else None,
            "score": round(best.score, 4) if best else None,
            "reason": best.reason if best else "no_templates",
        },
    )
