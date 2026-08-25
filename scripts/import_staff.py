"""Import the payroll staff list into the employee table.

The names, departments and payroll references come from the four-weekly payroll
workbook (``Format.xlsx``). Everyone is created on the default shift and the
default standard week - 07:30-16:00 and 40 hours out of the box - by leaving
both foreign keys NULL, so changing either default later moves everybody who has
not been given one of their own.

Run once at setup:

    python scripts/import_staff.py

    --file Format.xlsx   Read the list from a spreadsheet instead of the table
                         built in below (columns: Forename, Surname,
                         Department, Payroll Ref, from row 10).
    --dry-run            Print what would change and write nothing.
    --adopt              Move a person already in the database under a different
                         payroll reference onto their payroll one, instead of
                         reporting the clash and skipping them.

Re-running is safe. An employee is matched on payroll reference: an existing
record has its name and department refreshed, and nobody is ever created twice.
Nothing is deleted - somebody who has left is deactivated on the Employees page,
which keeps their clocking history for payroll queries.

**Same name, different reference.** Somebody enrolled during testing already has
a record, under a made-up reference like ``test7``. Creating a second record for
them is worse than it looks: the face index would clock them onto the *old*
record while payroll reads the new one, so their hours would silently go
missing. The import therefore refuses to create a person whose name already
exists and prints what it found; ``--adopt`` moves the existing record onto the
payroll reference instead, keeping their face enrolment and clocking history.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Employee  # noqa: E402

# (payroll ref, forename, surname, department) exactly as the payroll sheet has
# them, tidied only of stray trailing spaces. Departments are left as written
# rather than normalised: "Wood Shop" and "Woodshop" both appear in the source,
# and guessing which is correct is the office's call, not this script's.
STAFF: tuple[tuple[str, str, str, str], ...] = (
    ("519", "Abu", "Saab", "Wood Shop"),
    ("534", "Archie", "Callaghan", "Night Shift"),
    ("39", "Alex", "Courtney", "Logistics"),
    ("320", "Andrew", "Cooper", "Assembly"),
    ("455", "Bobby", "Corney", "Assembly"),
    ("306", "Brett", "Davies", "Woodshop"),
    ("510", "Craig", "Taylor", "Paintline Night"),
    ("462", "Craig", "Stephens", "Wood Shop"),
    ("467", "Damian", "Wojtala", "Night Shift"),
    ("367", "David", "Payne", "Woodshop"),
    ("445", "Dylan", "Evans", "Assembly"),
    ("412", "Dillan", "Kerr", "PPW"),
    ("81", "Earl", "Dalbeth", "Factory Admin"),
    ("380", "Gabrielle", "Rogers", "Paint"),
    ("420", "Ganesh", "Adhav", "PPW"),
    ("57", "Gene", "Marshall", "Assembly"),
    ("225", "Grzegorz", "Dombek", "Night Shift"),
    ("248", "Grzegorz", "Mordarski", "PPW"),
    ("364", "Jake", "Murphy", "Woodshop"),
    ("454", "Jake", "Farley", "PPW"),
    ("85", "Jason", "Fifield", "PPW"),
    ("536", "Joseph", "Wilson", "Assembly"),
    ("503", "Josh", "Taylor", "Night Shift"),
    ("227", "Karl", "Lewis", "Non Prod"),
    ("505", "Karl", "Ellis", "Welding"),
    ("419", "Kewin", "Utan", "Paintline"),
    ("528", "Keye", "Mclean", "PPW"),
    ("392", "Kieran", "Dyer", "Paint"),
    ("324", "Klaudia", "Drozdowska", "Sales"),
    ("335", "Lukasz", "Strawa", "Night Shift"),
    ("203", "Malgorzata", "Reczycka", "Paint"),
    ("32", "Mark", "Mockridge", "Paint"),
    ("535", "Mathieu", "Farren", "Assembly"),
    ("523", "Max", "Genge", "Assembly"),
    ("302", "Mubarik", "Hassan", "Non Prod"),
    ("532", "Reinart", "Jensema", "Assembly"),
    ("513", "Rad", "Borowiec", "Night Shift"),
    ("527", "Reza", "Roozbeh", "PPW"),
    ("219", "Robert", "Gifford", "PPW"),
    ("376", "Richard", "Clarke", "PPW"),
    ("38", "Sam", "Courtney", "Assembly"),
    ("529", "Shane", "Roper", "Wood Shop"),
    ("533", "Thomas", "Panso", "Wood Shop"),
    ("514", "Willow", "Kiely", "Forming"),
)

FIRST_DATA_ROW = 10


def read_spreadsheet(path: Path) -> list[tuple[str, str, str, str]]:
    """Read the staff list from the payroll workbook's own columns."""
    import openpyxl

    workbook = openpyxl.load_workbook(path, data_only=True)
    sheet = workbook.active
    rows = []
    for row in sheet.iter_rows(min_row=FIRST_DATA_ROW, max_col=4):
        forename, surname, department, reference = (cell.value for cell in row)
        if not forename or not surname or reference is None:
            continue  # a blank row, or the tail of the sheet
        rows.append(
            (
                str(reference).strip(),
                str(forename).strip(),
                str(surname).strip(),
                str(department or "").strip(),
            )
        )
    return rows


def find_by_name(forename: str, surname: str) -> Employee | None:
    """An existing record for this person under any payroll reference."""
    return db.session.scalars(
        select(Employee).where(
            func.lower(Employee.first_name) == forename.strip().lower(),
            func.lower(Employee.last_name) == surname.strip().lower(),
        )
    ).first()


def import_staff(
    staff, *, dry_run: bool = False, adopt: bool = False
) -> tuple[int, int, int]:
    """Create or refresh each employee. Returns (created, updated, clashes)."""
    created = updated = clashes = 0
    for reference, forename, surname, department in staff:
        existing = db.session.scalars(
            select(Employee).where(Employee.payroll_ref == reference)
        ).first()

        if existing is None:
            # Before creating anybody, check they are not already here under a
            # test reference - see the note at the top of this file.
            twin = find_by_name(forename, surname)
            if twin is not None:
                faces = len(twin.templates)
                detail = f"already in the database as {twin.payroll_ref!r}"
                if faces:
                    detail += f" with {faces} face sample(s)"
                if not adopt:
                    print(f"  ! {reference:>4}  {forename} {surname}: {detail} - skipped")
                    print("         re-run with --adopt to move that record onto "
                          f"payroll ref {reference}")
                    clashes += 1
                    continue
                print(
                    f"  > {reference:>4}  {forename} {surname}: {detail}; "
                    f"moving it from {twin.payroll_ref!r} to {reference!r}"
                )
                updated += 1
                if not dry_run:
                    twin.payroll_ref = reference
                    twin.department = department or None
                continue

            print(f"  + {reference:>4}  {forename} {surname} ({department})")
            created += 1
            if not dry_run:
                db.session.add(
                    Employee(
                        payroll_ref=reference,
                        first_name=forename,
                        last_name=surname,
                        department=department or None,
                        is_active=True,
                        # NULL on both: follow whatever is marked as the default
                        # shift and default standard week.
                        shift_pattern_id=None,
                        working_week_id=None,
                    )
                )
            continue

        changes = []
        if existing.first_name != forename:
            changes.append(f"forename {existing.first_name!r}->{forename!r}")
        if existing.last_name != surname:
            changes.append(f"surname {existing.last_name!r}->{surname!r}")
        if (existing.department or "") != department:
            changes.append(f"department {existing.department!r}->{department!r}")
        if not changes:
            continue

        print(f"  ~ {reference:>4}  {forename} {surname}: {', '.join(changes)}")
        updated += 1
        if not dry_run:
            existing.first_name = forename
            existing.last_name = surname
            existing.department = department or None

    if not dry_run:
        db.session.commit()
    return created, updated, clashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, help="Read the list from a spreadsheet.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report changes without writing them."
    )
    parser.add_argument(
        "--adopt",
        action="store_true",
        help="Move a person already held under another payroll reference onto "
        "the payroll one, instead of skipping them.",
    )
    args = parser.parse_args()

    staff = read_spreadsheet(args.file) if args.file else list(STAFF)
    if not staff:
        raise SystemExit("No staff rows found.")

    app = create_app("development")
    with app.app_context():
        print(f"{len(staff)} row(s) to import:")
        created, updated, clashes = import_staff(
            staff, dry_run=args.dry_run, adopt=args.adopt
        )
        total = db.session.scalar(select(db.func.count()).select_from(Employee))

    verb = "would be" if args.dry_run else "were"
    print(f"\n{created} created, {updated} updated ({verb} written).")
    if clashes:
        print(
            f"{clashes} skipped: already in the database under a different "
            "payroll reference."
        )
        print(
            "Re-run with --adopt to move those records across, keeping their "
            "face enrolment and clocking history."
        )
    print(f"{total} employee record(s) in the database.")
    if not args.dry_run and created:
        print(
            "\nEveryone is on the default shift (07:30-16:00) and the default "
            "40-hour week.\nAdjust individuals on the Employees page, and add "
            "their pay rates there too."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
