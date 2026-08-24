"""Enrol and clock with a desktop USB fingerprint reader (ZKTeco, DigitalPersona).

These readers hand back a *template* rather than a slot number, so matching
happens in our own code and the templates live in the database. That is real
biometric data at rest - see the README before rolling it out.

    # 1. Does the SDK binding actually work? Do this first.
    python scripts/usb_fingerprint.py --selftest

    # 2. Enrol somebody by payroll reference. Ask for their right index finger.
    python scripts/usb_fingerprint.py --enrol E001 --position 2

    # 3. See who is enrolled.
    python scripts/usb_fingerprint.py --list

    # 4. Run the kiosk loop: capture, identify, clock.
    python scripts/usb_fingerprint.py --run

    # And when somebody leaves:
    python scripts/usb_fingerprint.py --remove E001

Enrolment talks to the database directly, like scripts/init_db.py, because it is
an office tool run on the machine itself. Clocking goes over HTTP to the running
app, so that matching happens server-side and the app remains the only thing
that writes attendance rows.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import FINGER_POSITIONS, Employee, FingerprintTemplate  # noqa: E402
from app.services.fingerprint import (  # noqa: E402
    FingerprintError,
    encode_template,
    enrol,
    get_driver,
    identify,
    remove_templates,
)


def find_employee(payroll_ref: str) -> Employee | None:
    return db.session.scalars(
        select(Employee).where(Employee.payroll_ref == payroll_ref)
    ).first()


# --- commands -----------------------------------------------------------------
def cmd_selftest() -> int:
    """Exercise the SDK binding one call at a time and report what came back."""
    print(f"Driver configured: {db.get_app().config['FINGERPRINT_DRIVER']}")
    try:
        driver = get_driver()
    except FingerprintError as exc:
        print(f"  FAILED [{exc.code}] {exc.message}")
        return 1
    print(f"  driver loaded: {driver.name}")

    print("\nPress a finger on the reader (15 seconds)...")
    try:
        template, quality = driver.capture(timeout=15.0)
    except FingerprintError as exc:
        print(f"  capture FAILED [{exc.code}] {exc.message}")
        return 1
    print(f"  captured {len(template)} byte template, quality {quality}")

    print("\nPress the SAME finger again, to check comparison...")
    try:
        again, _ = driver.capture(timeout=15.0)
    except FingerprintError as exc:
        print(f"  second capture FAILED [{exc.code}] {exc.message}")
        return 1
    same = driver.compare(template, again)
    print(f"  same finger scores {same:.3f}   (want a high number)")

    print("\nNow press a DIFFERENT finger...")
    try:
        other, _ = driver.capture(timeout=15.0)
        different = driver.compare(template, other)
        print(f"  different finger scores {different:.3f}   (want a low number)")
    except FingerprintError as exc:
        print(f"  skipped: {exc.message}")
        different = None

    print("\nThe SDK binding works.")
    threshold = db.get_app().config["FINGERPRINT_MATCH_THRESHOLD"]
    print(f"FINGERPRINT_MATCH_THRESHOLD is {threshold}. Set it comfortably")
    print("between the two scores above.")
    if different is not None and same <= different:
        print("\nWARNING: the same finger did not score higher than a different")
        print("one. Do not go live until that is sorted - matching is unreliable.")
        return 1
    return 0


def cmd_enrol(payroll_ref: str, position: int | None, samples: int, replace: bool) -> int:
    employee = find_employee(payroll_ref)
    if employee is None:
        print(f"No employee with payroll reference {payroll_ref!r}.")
        return 1

    where = FINGER_POSITIONS.get(position or 0, "any finger")
    print(f"Enrolling {employee.full_name} ({employee.payroll_ref}) - {where}.")
    try:
        driver = get_driver()
    except FingerprintError as exc:
        print(f"  [{exc.code}] {exc.message}")
        return 1

    captured: list[tuple[bytes, float | None]] = []
    for number in range(1, samples + 1):
        print(f"  press {number} of {samples} ...", end=" ", flush=True)
        try:
            captured.append(driver.capture(timeout=20.0))
            print("got it")
        except FingerprintError as exc:
            print(f"failed: {exc.message}")
            return 1
        time.sleep(0.4)

    outcome = enrol(
        employee,
        captured,
        driver=driver,
        position=position,
        replace_existing=replace,
    )
    print(f"  {outcome.message}")
    return 0 if outcome.ok else 1


def cmd_list() -> int:
    rows = db.session.scalars(
        select(FingerprintTemplate).order_by(FingerprintTemplate.employee_id)
    ).all()
    if not rows:
        print("Nobody has a fingerprint enrolled.")
        return 0

    by_employee: dict[int, list[FingerprintTemplate]] = {}
    for row in rows:
        by_employee.setdefault(row.employee_id, []).append(row)

    print(f"{len(by_employee)} employee(s) enrolled:")
    for employee_id, templates in by_employee.items():
        employee = db.session.get(Employee, employee_id)
        name = employee.full_name if employee else f"employee {employee_id}"
        ref = employee.payroll_ref if employee else "?"
        fingers = ", ".join(
            sorted({t.position_name or "unspecified" for t in templates})
        )
        drivers = ", ".join(sorted({t.driver for t in templates}))
        print(f"  {ref:<10} {name:<24} {len(templates)} sample(s)  [{fingers}]  ({drivers})")
    return 0


def cmd_remove(payroll_ref: str) -> int:
    employee = find_employee(payroll_ref)
    if employee is None:
        print(f"No employee with payroll reference {payroll_ref!r}.")
        return 1
    removed = remove_templates(employee)
    print(f"Removed {removed} fingerprint sample(s) for {employee.full_name}.")
    return 0


def cmd_verify() -> int:
    """Identify a finger locally, without recording anything - a setup check."""
    try:
        driver = get_driver()
        print("Press a finger ...")
        template, _ = driver.capture(timeout=20.0)
    except FingerprintError as exc:
        print(f"  [{exc.code}] {exc.message}")
        return 1

    outcome = identify(template, driver=driver)
    if outcome.accepted:
        employee = db.session.get(Employee, outcome.employee_id)
        who = employee.full_name if employee else outcome.employee_id
        print(f"  matched {who} (score {outcome.score:.3f}, "
              f"margin {outcome.margin:.3f})")
    else:
        print(f"  no match: {outcome.reason} (best score {outcome.score:.3f})")
    return 0


def cmd_run(url: str, token: str, timeout: float) -> int:
    """Capture, post to the app, and report - the kiosk loop."""
    try:
        driver = get_driver()
    except FingerprintError as exc:
        print(f"[{exc.code}] {exc.message}")
        return 1

    endpoint = f"{url.rstrip('/')}/api/kiosk/fingerprint/verify"
    print(f"Posting to {endpoint}")
    print("Ready. Press a finger to clock in or out. Ctrl+C to stop.\n")

    while True:
        try:
            template, _ = driver.capture(timeout=timeout)
        except FingerprintError as exc:
            if exc.code != "no_finger":
                print(f"  reader: {exc.message}")
            continue
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0

        body = json.dumps({"template": encode_template(template)}).encode()
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json", "X-Kiosk-Token": token},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                reply = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            reply = {
                "ok": False,
                "code": f"http_{exc.code}",
                "message": exc.read().decode("utf-8", "replace")[:200],
            }
        except (urllib.error.URLError, TimeoutError) as exc:
            # Somebody is at the door; a brief outage must not kill the agent.
            reply = {"ok": False, "code": "unreachable", "message": str(exc)}

        if reply.get("ok"):
            who = (reply.get("employee") or {}).get("name", "?")
            state = "recorded" if reply.get("recorded") else "ignored (too soon)"
            print(f"  {who} - {reply.get('direction')} {state}")
        else:
            print(f"  REFUSED [{reply.get('code')}] {reply.get('message')}")
        time.sleep(0.6)


# --- entry point --------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--selftest", action="store_true", help="Check the SDK binding.")
    action.add_argument("--enrol", metavar="PAYROLL_REF", help="Enrol an employee.")
    action.add_argument("--list", action="store_true", help="Who is enrolled.")
    action.add_argument("--remove", metavar="PAYROLL_REF", help="Delete their samples.")
    action.add_argument("--verify", action="store_true", help="Identify without clocking.")
    action.add_argument("--run", action="store_true", help="The clocking loop.")

    parser.add_argument(
        "--position",
        type=int,
        choices=sorted(FINGER_POSITIONS),
        help="Which finger is being enrolled (1-10), for the records.",
    )
    parser.add_argument("--samples", type=int, default=None, help="Presses per enrolment.")
    parser.add_argument(
        "--replace", action="store_true", help="Drop existing samples first."
    )
    parser.add_argument("--url", default="http://127.0.0.1:5000", help="The app's address.")
    parser.add_argument("--token", help="Kiosk token (defaults to KIOSK_TOKEN in .env).")
    parser.add_argument("--timeout", type=float, default=20.0, help="Capture wait, seconds.")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # pragma: no cover
        pass

    app = create_app("development")
    with app.app_context():
        try:
            if args.selftest:
                return cmd_selftest()
            if args.list:
                return cmd_list()
            if args.enrol:
                samples = args.samples or app.config["FINGERPRINT_ENROL_SAMPLES"]
                return cmd_enrol(args.enrol, args.position, samples, args.replace)
            if args.remove:
                return cmd_remove(args.remove)
            if args.verify:
                return cmd_verify()

            token = args.token or app.config.get("KIOSK_TOKEN", "")
            if not token:
                raise SystemExit("No kiosk token. Set KIOSK_TOKEN in .env.")
            return cmd_run(args.url, token, args.timeout)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
