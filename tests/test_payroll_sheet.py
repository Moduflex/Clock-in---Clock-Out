"""The payroll master sheet: the workbook's layout, and its live formulas."""

from __future__ import annotations

import datetime as dt
import io
from zoneinfo import ZoneInfo

import openpyxl
import pytest

from app.models import DIRECTION_IN, DIRECTION_OUT, AttendanceEvent, ShiftPattern, WorkingWeek
from app.services.payroll_sheet import (
    FIRST_DATA_ROW,
    HEADER_LABEL_ROW,
    build_master_workbook,
    payroll_period,
    period_label,
    sheet_employees,
    to_master_xlsx,
)
from app.services.timesheet import build_timesheet, summarise

from .conftest import make_employee

LONDON = ZoneInfo("Europe/London")
# The period the sample workbook covers, so the layout can be checked against it.
PERIOD_START = dt.date(2026, 8, 17)
PERIOD_END = dt.date(2026, 9, 13)
ANCHOR = dt.date(2026, 3, 30)


@pytest.fixture
def payroll_db(db):
    """A default shift and standard week, as init_db seeds on a fresh install."""
    db.session.add(
        ShiftPattern(
            name="Standard day",
            start_time=dt.time(7, 30),
            end_time=dt.time(16, 0),
            unpaid_break_minutes=30,
            is_default=True,
        )
    )
    db.session.add(WorkingWeek(name="40-hour week", hours=40.0, is_default=True))
    db.session.commit()
    return db


def _work(db, employee, day: dt.date, start_hour: int, end_hour: int) -> None:
    """One clocked day. Hours are naive UTC, as stored."""
    db.session.add_all(
        [
            AttendanceEvent(
                employee_id=employee.id,
                direction=DIRECTION_IN,
                occurred_at=dt.datetime.combine(day, dt.time(start_hour, 30)),
            ),
            AttendanceEvent(
                employee_id=employee.id,
                direction=DIRECTION_OUT,
                occurred_at=dt.datetime.combine(day, dt.time(end_hour, 0)),
            ),
        ]
    )
    db.session.commit()


def _sheet(db, **kwargs):
    shifts = build_timesheet(PERIOD_START, PERIOD_END, LONDON)
    workbook = build_master_workbook(
        summarise(shifts),
        PERIOD_START,
        PERIOD_END,
        employees=sheet_employees(),
        anchor=ANCHOR,
        **kwargs,
    )
    return workbook["Sheet1"]


# --- the period header --------------------------------------------------------
def test_the_period_matches_the_payroll_workbook():
    """17/08/26-13/09/26 is period 6, which is what the office's sheet says."""
    assert payroll_period(PERIOD_START, ANCHOR) == 6


def test_periods_run_every_four_weeks_and_wrap_at_thirteen():
    assert payroll_period(ANCHOR, ANCHOR) == 1
    assert payroll_period(ANCHOR + dt.timedelta(days=28), ANCHOR) == 2
    assert payroll_period(ANCHOR + dt.timedelta(days=27), ANCHOR) == 1
    # Thirteen four-weekly periods make a year, then it starts again.
    assert payroll_period(ANCHOR + dt.timedelta(days=28 * 13), ANCHOR) == 1


def test_the_dates_line_is_written_the_way_the_office_writes_it():
    assert period_label(PERIOD_START, PERIOD_END) == "17/08/26 - 13/09/26"


# --- the layout ---------------------------------------------------------------
def test_the_headings_sit_where_the_payroll_workbook_puts_them(payroll_db):
    make_employee(payroll_db, ref="519", first="Abu", last="Saab", department="Wood Shop")
    sheet = _sheet(payroll_db)

    assert sheet["A1"].value == "Moduflex Ltd"
    assert sheet["A2"].value == "Four Weekly Payroll"
    assert sheet["B4"].value == 6
    assert sheet["B5"].value == "17/08/26 - 13/09/26"

    assert sheet["F7"].value == "Rates of Pay"
    assert sheet["J7"].value == "Hours to Pay"
    assert sheet["S7"].value == "Total Pay"
    assert sheet["AB7"].value == "Notes"

    expected = {
        "A": "Forename",
        "B": "Surname",
        "C": "Department",
        "D": "Payroll Ref",
        "F": "Basic",
        "H": "O/T 1.50",
        "J": "Basic",
        "K": "O/T 1.50",
        "M": "Holiday",
        "O": "Total Hours",
        "Q": "SSP Days",
        "S": "Basic",
        "U": "O/T 1.50",
        "V": "Holiday",
        "W": "Back Pay",
        "X": "Adjustments",
        "Y": "Deductions",
        "Z": "TOTAL PAY",
        "AB": "Start Date for new staff",
        "AC": "Leaving date for leavers",
        "AD": "Notes",
    }
    for letter, heading in expected.items():
        cell = sheet[f"{letter}{HEADER_LABEL_ROW}"]
        assert cell.value == heading, f"{letter}{HEADER_LABEL_ROW} should be {heading!r}"


def test_the_banded_groups_are_merged_across_their_columns(payroll_db):
    make_employee(payroll_db, ref="519")
    merged = {str(r) for r in _sheet(payroll_db).merged_cells.ranges}
    assert {"F7:H7", "J7:M7", "S7:Z7", "AB7:AD7"} <= merged


def test_the_input_columns_keep_their_colour_coding(payroll_db):
    """Yellow marks the rates, amber the hours and the headline total."""
    make_employee(payroll_db, ref="519")
    sheet = _sheet(payroll_db)
    row = FIRST_DATA_ROW

    assert sheet[f"F{row}"].fill.start_color.rgb == "FFFFFF00"
    for letter in ("J", "K", "L", "M", "Z"):
        assert sheet[f"{letter}{row}"].fill.start_color.rgb == "FFFFC000", letter


# --- what goes in the cells ---------------------------------------------------
def test_hours_come_from_the_clock_split_into_basic_and_overtime(payroll_db):
    employee = make_employee(payroll_db, ref="519", first="Abu", last="Saab")
    # A 40-hour week, then a week with ten hours of overtime in it.
    for day in range(5):
        _work(payroll_db, employee, PERIOD_START + dt.timedelta(days=day), 6, 15)
    for day in range(5):
        _work(payroll_db, employee, PERIOD_START + dt.timedelta(days=7 + day), 6, 17)

    sheet = _sheet(payroll_db)
    row = FIRST_DATA_ROW
    assert sheet[f"A{row}"].value == "Abu"
    assert sheet[f"B{row}"].value == "Saab"
    assert sheet[f"J{row}"].value == pytest.approx(80.0)  # two standard weeks
    assert sheet[f"K{row}"].value == pytest.approx(10.0)  # the overtime


def test_everyone_active_gets_a_row_even_with_nothing_clocked(payroll_db):
    """A name missing from a wage sheet is how somebody ends up unpaid."""
    make_employee(payroll_db, ref="519", first="Abu", last="Saab")
    make_employee(payroll_db, ref="534", first="Archie", last="Callaghan")
    make_employee(payroll_db, ref="999", first="Zoe", last="Gone", is_active=False)

    sheet = _sheet(payroll_db)
    names = [
        sheet[f"A{FIRST_DATA_ROW + offset}"].value for offset in range(2)
    ]
    assert names == ["Abu", "Archie"]
    # Nothing clocked, so zero hours - but the row, and its formulas, are there.
    assert sheet[f"J{FIRST_DATA_ROW + 1}"].value == 0.0
    assert sheet[f"A{FIRST_DATA_ROW + 2}"].value is None  # the leaver is not listed


def test_a_numeric_payroll_ref_is_written_as_a_number(payroll_db):
    make_employee(payroll_db, ref="519")
    assert _sheet(payroll_db)[f"D{FIRST_DATA_ROW}"].value == 519


def test_a_payroll_ref_with_letters_stays_text(payroll_db):
    make_employee(payroll_db, ref="TMP-4")
    assert _sheet(payroll_db)[f"D{FIRST_DATA_ROW}"].value == "TMP-4"


# --- the formulas -------------------------------------------------------------
def test_the_right_hand_columns_are_formulas_not_typed_numbers(payroll_db):
    make_employee(payroll_db, ref="519")
    sheet = _sheet(payroll_db)
    row = FIRST_DATA_ROW

    assert sheet[f"H{row}"].value == f"=F{row}*1.5"
    assert sheet[f"O{row}"].value == f"=J{row}+K{row}+M{row}"
    assert sheet[f"S{row}"].value == f"=J{row}*F{row}"
    assert sheet[f"U{row}"].value == f"=K{row}*H{row}"
    assert sheet[f"V{row}"].value == f"=M{row}*F{row}"
    assert sheet[f"Z{row}"].value == f"=S{row}+U{row}+V{row}+W{row}+X{row}-Y{row}"


def test_the_formulas_add_up(payroll_db):
    """Worked by hand the way Excel will, so a wrong formula is caught here."""
    employee = make_employee(payroll_db, ref="519")
    employee.basic_rate = "14.50"
    payroll_db.session.commit()
    for day in range(5):
        _work(payroll_db, employee, PERIOD_START + dt.timedelta(days=day), 6, 17)

    sheet = _sheet(payroll_db)
    row = FIRST_DATA_ROW
    basic_rate = sheet[f"F{row}"].value
    overtime_rate = basic_rate * 1.5  # what =F*1.5 evaluates to
    basic_hours = sheet[f"J{row}"].value
    overtime_hours = sheet[f"K{row}"].value
    holiday, back_pay, adjustments, deductions = 8.0, 0.0, 0.0, 12.40

    assert basic_hours == pytest.approx(40.0)
    assert overtime_hours == pytest.approx(10.0)
    assert basic_hours + overtime_hours + holiday == pytest.approx(58.0)

    basic_pay = basic_hours * basic_rate
    overtime_pay = overtime_hours * overtime_rate
    holiday_pay = holiday * basic_rate
    total = basic_pay + overtime_pay + holiday_pay + back_pay + adjustments - deductions

    assert basic_pay == pytest.approx(580.00)
    assert overtime_pay == pytest.approx(217.50)
    assert holiday_pay == pytest.approx(116.00)
    assert total == pytest.approx(901.10)


# --- pay rates ----------------------------------------------------------------
def test_the_recorded_basic_rate_is_written_into_column_f(payroll_db):
    employee = make_employee(payroll_db, ref="519")
    employee.basic_rate = "14.50"
    payroll_db.session.commit()

    assert _sheet(payroll_db)[f"F{FIRST_DATA_ROW}"].value == pytest.approx(14.50)


def test_the_overtime_rate_is_a_formula_off_the_basic_one(payroll_db):
    """Column H is the rule, not a second figure to keep in step."""
    employee = make_employee(payroll_db, ref="519")
    employee.basic_rate = "14.50"
    payroll_db.session.commit()

    sheet = _sheet(payroll_db)
    assert sheet[f"H{FIRST_DATA_ROW}"].value == f"=F{FIRST_DATA_ROW}*1.5"
    # 14.50 x 1.5 = 21.75, which is what Excel will show.
    assert sheet[f"F{FIRST_DATA_ROW}"].value * 1.5 == pytest.approx(21.75)


def test_no_recorded_rate_leaves_the_basic_cell_blank_rather_than_zero(payroll_db):
    """A blank prompts payroll to type the rate. A zero would pay nothing."""
    make_employee(payroll_db, ref="519")
    assert _sheet(payroll_db)[f"F{FIRST_DATA_ROW}"].value is None


def test_the_overtime_formula_is_written_even_with_no_rate_on_file(payroll_db):
    """So typing a rate into F in Excel fills in the overtime rate by itself."""
    make_employee(payroll_db, ref="519")
    sheet = _sheet(payroll_db)
    assert sheet[f"F{FIRST_DATA_ROW}"].value is None
    assert sheet[f"H{FIRST_DATA_ROW}"].value == f"=F{FIRST_DATA_ROW}*1.5"


def test_the_rates_band_keeps_only_column_f_as_an_input(payroll_db):
    """F is yellow because it is typed in; H is a formula, so it is not."""
    make_employee(payroll_db, ref="519")
    sheet = _sheet(payroll_db)
    assert sheet[f"F{FIRST_DATA_ROW}"].fill.start_color.rgb == "FFFFFF00"
    assert sheet[f"H{FIRST_DATA_ROW}"].fill.fill_type is None


# --- notes --------------------------------------------------------------------
def test_a_timesheet_warning_reaches_the_notes_column(payroll_db):
    """A forgotten clock-out must be visible to whoever checks the sheet."""
    employee = make_employee(payroll_db, ref="519")
    payroll_db.session.add(
        AttendanceEvent(
            employee_id=employee.id,
            direction=DIRECTION_IN,
            occurred_at=dt.datetime.combine(PERIOD_START, dt.time(6, 30)),
        )
    )
    payroll_db.session.commit()

    note = _sheet(payroll_db)[f"AD{FIRST_DATA_ROW}"].value
    assert note and "17/08" in note


def test_the_notes_column_is_left_empty_when_nothing_is_wrong(payroll_db):
    employee = make_employee(payroll_db, ref="519")
    _work(payroll_db, employee, PERIOD_START, 6, 15)
    assert _sheet(payroll_db)[f"AD{FIRST_DATA_ROW}"].value is None


# --- the download --------------------------------------------------------------
def test_the_workbook_serialises_to_a_real_xlsx(payroll_db):
    make_employee(payroll_db, ref="519", first="Abu", last="Saab")
    shifts = build_timesheet(PERIOD_START, PERIOD_END, LONDON)
    data = to_master_xlsx(
        summarise(shifts),
        PERIOD_START,
        PERIOD_END,
        employees=sheet_employees(),
        anchor=ANCHOR,
    )
    assert data[:2] == b"PK"  # a zip, which is what an xlsx is
    reopened = openpyxl.load_workbook(io.BytesIO(data))["Sheet1"]
    assert reopened[f"A{FIRST_DATA_ROW}"].value == "Abu"


# --- the staff import ---------------------------------------------------------
def test_the_staff_list_imports_onto_the_default_shift_and_week(payroll_db):
    """Everyone lands with NULL keys, so both defaults apply without setup."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from import_staff import STAFF, import_staff

    created, updated, clashes = import_staff(STAFF)
    assert created == len(STAFF)
    assert (updated, clashes) == (0, 0)

    people = sheet_employees()
    assert len(people) == len(STAFF)
    assert all(p.shift_pattern_id is None for p in people)
    assert all(p.working_week_id is None for p in people)
    # And the defaults they inherit are the ones asked for.
    assert people[0].shift_pattern is None  # i.e. "use the default"

    abu = next(p for p in people if p.payroll_ref == "519")
    assert abu.full_name == "Abu Saab"
    assert abu.department == "Wood Shop"


def test_re_running_the_import_creates_nobody_twice(payroll_db):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from import_staff import STAFF, import_staff

    import_staff(STAFF)
    created, updated, clashes = import_staff(STAFF)
    assert (created, updated, clashes) == (0, 0, 0)
    assert len(sheet_employees()) == len(STAFF)


def test_the_import_refreshes_a_changed_department(payroll_db):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from import_staff import import_staff

    import_staff([("519", "Abu", "Saab", "Wood Shop")])
    created, updated, _ = import_staff([("519", "Abu", "Saab", "Assembly")])
    assert (created, updated) == (0, 1)
    assert sheet_employees()[0].department == "Assembly"


def test_every_payroll_reference_in_the_list_is_unique():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from import_staff import STAFF

    references = [row[0] for row in STAFF]
    assert len(references) == len(set(references))
    assert len(STAFF) == 44


def test_a_person_already_held_under_a_test_reference_is_not_duplicated(payroll_db):
    """Two records for one person sends their hours to the wrong one."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from import_staff import import_staff

    make_employee(payroll_db, ref="test7", first="Earl", last="Dalbeth", department="test")
    created, updated, clashes = import_staff([("81", "Earl", "Dalbeth", "Factory Admin")])
    assert (created, updated, clashes) == (0, 0, 1)
    assert len(sheet_employees()) == 1


def test_adopt_moves_that_record_onto_its_payroll_reference(payroll_db):
    """Their face enrolment and clocking history come with them."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from import_staff import import_staff

    from .conftest import add_template, unit_vector

    earl = make_employee(
        payroll_db, ref="test7", first="Earl", last="Dalbeth", department="test"
    )
    add_template(payroll_db, earl, unit_vector(1))
    _work(payroll_db, earl, PERIOD_START, 6, 15)

    created, updated, clashes = import_staff(
        [("81", "Earl", "Dalbeth", "Factory Admin")], adopt=True
    )
    assert (created, updated, clashes) == (0, 1, 0)

    people = sheet_employees()
    assert len(people) == 1
    assert people[0].payroll_ref == "81"
    assert people[0].department == "Factory Admin"
    assert len(people[0].templates) == 1  # the enrolment survived
    assert people[0].events  # and so did the clocking history
