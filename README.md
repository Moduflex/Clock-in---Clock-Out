# Face-recognition clocking system

A clock-in / clock-out system for a small manufacturing site. An employee stands
at a kiosk screen, presses **Scan**, and the system recognises their face and
records the entry. The office gets timesheets and a payroll CSV.

Built with Flask and MySQL. Face recognition runs on two small ONNX models
through OpenCV's DNN module — no PyTorch, no onnxruntime, no dlib build step,
no GPU, and nothing to install beyond `pip install -r requirements.txt`.

---

## What it does

**Kiosk** (`/`, no login)
- **Hands-free by default**: walk up, and you are clocked in or out with no
  button press. See [Hands-free clocking](#hands-free-clocking) below.
- Live camera preview, wall clock, and a large Scan button as a fallback.
- Automatic direction: a scan records the opposite of your last entry, so nobody
  has to remember which button to press. Explicit **Clock in** / **Clock out**
  buttons are there when needed, and override the hands-free interval.
- Shows a count of who is on site.
- Space or Enter also triggers a scan, so a cheap USB footswitch wired as a
  keyboard works as the trigger. Escape cancels a pending automatic entry.

**Office** (`/admin`, login required)
- Employees: add, edit, search, deactivate.
- Enrolment: capture several face samples through the browser, with checks that
  they are all the same person and not somebody already enrolled.
- Fingerprints: register which slot on which reader belongs to whom. The
  reader keeps the fingerprint and does the matching; this system stores no
  biometric data for fingerprints at all, only the slot number.
- Absence: who has not clocked in, for any day. The active list is sorted into
  not clocked in, not due yet (their shift starts later, so nothing is wrong),
  on site, and clocked out — with how overdue each absentee is against their
  shift start and when they last clocked anything, so a first missed morning
  looks different from a fifth. Defaults to today, filterable by department. A
  night shift started yesterday counts as on site rather than absent, and a past
  day is judged at its own midnight rather than against the clock now.
- Payroll master sheet (Excel): the four-weekly wage sheet in the office's own
  workbook layout &mdash; same columns, banding and colour coding, so nothing
  downstream changes. See [The payroll master sheet](#the-payroll-master-sheet).
- Pay basis: each person is **four-weekly** or **salary**. Four-weekly staff are
  paid from clocked hours and appear on the payroll master sheet; salaried staff
  are paid a fixed amount whatever they clock, so they are left off it &mdash; while
  still clocking in and out and still appearing on timesheets and the absence
  board.
- Pay rates: one basic hourly rate per person, held **encrypted** so a database
  dump does not list what anybody earns. Overtime is time and a half on it,
  worked out rather than stored, so the two can never disagree.
- Timesheets: date range (defaults to the last four whole weeks), department and
  per-employee filters, clocked and paid hours per shift, standard and overtime
  hours per person, a week-by-week breakdown, day-by-day drill-down, and three
  CSV exports — a one-line-per-person master sheet for payroll, the same split
  week by week, and the full daily detail.
- Shifts and hours: two settings decide what gets paid.
  - **Shifts** are paid time bands (e.g. 07:30–16:00 with a 30-minute unpaid
    lunch). Clocking in early pays from the shift start, pay is counted in
    15-minute steps — a 07:34 arrival is paid from 07:45 — and a late finish is
    paid past the shift end, where it becomes overtime. That last rule can be
    turned off per shift where staying on is not authorised work. One shift is
    the default; employees can be assigned another.
  - **Standard working weeks** are the contracted hours per week — 40 and 32 out
    of the box, and you can add as many more as you need (37.5, 39, whatever
    your contracts say). Paid hours beyond the standard week are reported as
    overtime. One is the default; employees can be assigned another.
- Weeks run Monday to Sunday everywhere, and overtime is settled one week at a
  time. Somebody on a 40-hour week who is paid for 65 hours in a week has 40
  standard hours and 25 overtime hours.
- Manual entry and voiding for corrections — both fully audited.
- Camera check: measures what your camera actually produces so the recognition
  thresholds can be set from real numbers rather than guesses.

---

## Fingerprint clocking

An alternative to the camera, useful where a face scan is awkward - gloves off
but hands dirty, or a doorway with poor light.

**How it is wired.** The reader stores and matches fingerprints in its own
memory and reports only *which of its numbered slots* matched. A small agent on
the kiosk machine forwards that slot number to `/api/kiosk/fingerprint`, which
looks up whose slot it is and records the clock event through exactly the same
alternation and cooldown rules as a face scan.

    reader --> scripts/fingerprint_reader.py --> POST /api/kiosk/fingerprint
                                                 { "finger_id": 7 }

**No fingerprint is ever stored by this system.** The `fingerprint_credential`
table holds "slot 7 on the workshop reader is Bob" and nothing else. A
fingerprint that never reaches the database cannot leak from a database backup,
and it keeps the amount of special category data held to a minimum. The
trade-off is that the reader becomes the system of record for the fingerprint
itself: removing somebody means unregistering the slot here **and** deleting it
on the reader.

**Setting somebody up**

1. Enrol the finger on the reader, following its own instructions. Note the slot
   number it reports.
2. Open that person in the back office and, under Fingerprints, enter the slot
   number and the reader name. The reader name must match the agent's
   `--device-label` (default: `KIOSK_DEVICE_LABEL` from `.env`).
3. Test it.

**Running the agent**

    # No hardware needed: type slot numbers to test the whole path.
    python scripts/fingerprint_reader.py --simulate

    # A reader that prints the matched slot number as a line of text.
    python scripts/fingerprint_reader.py --serial COM3

    # An R307 / ZFM-20 / FPM10A module over serial (needs pyserial).
    python scripts/fingerprint_reader.py --r307 COM3

In production, run it as a Windows scheduled task set to "run at startup". It
reads `KIOSK_TOKEN` from `.env`, the same secret the kiosk page uses.

### Desktop USB readers with an SDK (recommended)

A ZKTeco ZK9500 or DigitalPersona U.are.U plugs into a USB port and comes with a
documented SDK. These are the standard choice for time-and-attendance on a PC,
and they avoid every limitation of a Windows Hello dongle: no Windows account
involvement, no ten-person ceiling, no shared login, no elevation.

The reader hands back a **template** and matching happens in our code, so unlike
every other option here the fingerprint data really is stored in your database.
That is a deliberate trade for capability, and it has consequences:

- `fingerprint_template` rows are biometric data at rest, the same category as
  the face templates. They belong in the same DPIA, and the database backups
  need the same care.
- Deleting an employee removes their templates (cascade), and
  `--remove PAYROLL_REF` or the **Remove fingerprint data** button on their page
  does it without deleting the person - which is what an erasure request needs.
- Templates are **not portable between vendors**. Each row records which SDK
  produced it and the matcher only ever compares like with like, so swapping
  reader means re-enrolling rather than silently mismatching.

**Matching policy.** A probe is scored against every enrolled template; each
employee keeps their best score. A match must clear `FINGERPRINT_MATCH_THRESHOLD`
*and* beat the runner-up by `FINGERPRINT_MATCH_MARGIN`. Two people the reader
cannot tell apart therefore get a refusal and a retry, never a guess - clocking
the wrong person puts a wrong figure into somebody's pay. Enrolment applies the
same test in reverse and refuses a finger already enrolled to somebody else.

**Setting up**

```bash
# Rehearse the whole flow with no hardware at all: typing a name stands in
# for pressing a finger. FINGERPRINT_DRIVER=simulator is the default.
python scripts/usb_fingerprint.py --enrol E001 --position 2
python scripts/usb_fingerprint.py --verify
python scripts/usb_fingerprint.py --list
```

With the real reader:

1. Install the vendor SDK. For ZKTeco, set `FINGERPRINT_SDK_PATH` in `.env` to
   the folder holding `libzkfp.dll`, and `FINGERPRINT_DRIVER=zkfinger`.
2. **Check the binding before anything else.** This captures the same finger
   twice and then a different one, and prints the scores:

   ```bash
   python scripts/usb_fingerprint.py --selftest
   ```

   Set `FINGERPRINT_MATCH_THRESHOLD` comfortably between the two numbers it
   reports. If the same finger does not score clearly higher than a different
   one, do not go live - matching is not reliable yet.
3. Enrol each person (three presses by default):

   ```bash
   python scripts/usb_fingerprint.py --enrol E001 --position 2
   ```

4. Run the kiosk loop:

   ```bash
   python scripts/usb_fingerprint.py --run
   ```

Matching happens **server-side**: the agent posts the captured template to
`/api/kiosk/fingerprint/verify` and the app decides who it is. So a caller
holding the kiosk token supplies a fingerprint, never an employee id, and the
enrolled templates never leave the server.

**A note on the SDK binding.** `app/services/fingerprint_sdk.py` is written
against the documented ZKFinger entry points but was never run against hardware,
so treat it as a draft to verify: check the DLL name, the calling convention and
the buffer sizes against the SDK version you download. `--selftest` exists to
make that quick. The DigitalPersona binding is deliberately *not* written - that
SDK is reached through .NET or COM rather than a flat C DLL, and guessing would
be worse than useless.

### Windows Hello readers

A consumer Windows Hello dongle has no slot numbers of its own - it enrols
fingerprints against a *Windows user account*. What makes multi-person clocking
work anyway is that the Windows Biometric Framework records **which finger
position** each enrolment used, and reports that position back on every match.
So one kiosk Windows account holds up to ten enrolments, one per finger, and the
position is the identity:

    position 2 (right index)  -> Alice Turner
    position 7 (left index)   -> Bob Ward

The position number is posted as the `finger_id`, so registration and clocking
work exactly as they do for a slot-based reader.

#### Step 0: the driver (do this first)

A Hello dongle is a vendor-class USB device and is useless until its driver is
installed. If Device Manager shows it with a warning triangle, or

```powershell
Get-PnpDevice | Where-Object { $_.FriendlyName -match 'finger' }
```

reports `Error` / `The drivers for this device are not installed. (Code 28)`,
then nothing else in this section will work yet. Windows Update does **not**
reliably offer these drivers, because they are published against particular OEM
machines rather than against the USB hardware ID.

Find the right driver by hardware ID (`USB\VID_347D&PID_0302` for a
BLESTECH/Betterlife sensor) in **Microsoft's Update Catalog** at
<https://www.catalog.update.microsoft.com>, then check before installing:

- the INF's `[Version]` section says `Class=Biometric`;
- the INF actually lists your hardware ID;
- the `.cat` file's signature is *Valid* and signed by "Microsoft Windows
  Hardware Compatibility Publisher".

Install it from an **administrator** PowerShell:

```powershell
pnputil /add-driver .\BetterlifeFingerprintDevice.inf /install
pnputil /scan-devices
```

Use the Microsoft catalogue, not a third-party "driver download" site - those
repackage kernel drivers and are a known malware route onto a work machine.

Confirm the framework can now see a sensor:

```powershell
.venv\Scripts\python.exe scripts\windows_hello_reader.py --check
```

Note that `--check` opening the pool is necessary but not sufficient: the system
pool opens even with no reader attached. `--list` or `--probe` is what proves a
sensor is really there.

**Two limits to plan around**, both inherent to the hardware:

- **Ten people per Windows account**, because there are ten fingers. For a
  bigger workforce, create a second locked-down local Windows account, enrol ten
  more fingers while signed in as it, and give each account its own reader name:

  ```powershell
  python scripts\windows_hello_reader.py --run ^
      --account "S-1-5-21-...-1001=Kiosk account A" ^
      --account "S-1-5-21-...-1002=Kiosk account B"
  ```

  Each account gets its own reader name, so "position 2" means a different
  person on each - the `(reader, slot)` pair stays unique. Any account not
  listed is ignored, which is what stops an IT login clocking somebody in.

- **Everyone enrolled can also sign into that Windows account.** Windows cannot
  separate "may clock in" from "may log in" here. So the kiosk must run on a
  locked-down local account with no access to anything - which is good practice
  for a shop-floor machine regardless.

Neither applies to a slot-based reader, which is why one is still the better buy
if you have the choice.

**Setting it up.** First confirm a sensor is actually there - this reports the
units the framework can see, and needs nobody to touch anything:

```bash
python scripts/windows_hello_reader.py --check
```

If it says `Sensors the framework can see: 0`, stop and do Step 0 above; nothing
else will work. There are then two ways to enrol, and the first is the one to
reach for.

*Route A - enrol through Windows, then read the positions back.* This uses the
supported Windows path, so it works even where a driver does not implement
programmatic enrolment (the Betterlife one does not implement everything).

1. Sign in as the kiosk Windows account and go to **Settings > Accounts >
   Sign-in options > Fingerprint recognition**. Add one finger per person, up to
   ten. Windows does not ask which finger it is, which is what step 2 is for.
2. Run `--probe` and have each person touch the reader in turn. It prints the
   position number their finger actually reports:

   ```
   position 2 (Right index)  unit 4  account S-1-5-21-...
     -> would post finger_id=2
   ```

3. Register that number against that person in the back office, and note the
   account SID for `--require-sid`.

*Route B - enrol at a chosen position.* Gives deterministic position numbers
rather than discovering them, if the driver supports it:

```bash
python scripts/windows_hello_reader.py --enrol 2      # right index
```

Then run the clocking loop:

```bash
python scripts/windows_hello_reader.py --run --require-sid S-1-5-21-...
```

`--list` shows what a Windows account has enrolled, but only on drivers that
implement enrolment enumeration. On the Betterlife reader it reports
`0x80098003` and points you at Settings and `--probe` instead; that is a missing
convenience, not a fault in the reader.

`--run` may need to run **as administrator**: identifying fingers belonging to
a *different* Windows account is privileged. Run the agent as the same kiosk
account the fingers were enrolled on and it will usually work unelevated - try
it first, and only tick "run with highest privileges" on the scheduled task if
it reports access denied.

`--require-sid` is worth setting. Without it, any Windows account on the machine
that has Hello configured can clock somebody - so an IT administrator signing in
with their own fingerprint at position 2 would clock in whoever is registered
against position 2. `--probe` prints the SID to use.

When somebody leaves, `--delete POS` removes the enrolment from Windows, and
unregistering the slot in the back office removes the mapping. Both are needed.

**If `--check` fails** with access denied, run it as administrator. If it
reports biometrics disabled, Enhanced Sign-in Security may be blocking
third-party access to the sensor; that is a per-device Windows setting, and
without it a Hello dongle cannot be used for clocking at all.

**Better hardware, if you get the choice.** A reader that matches on-device and
reports a slot number (R307 and similar, around £15) has neither the ten-person
limit nor the shared-login problem, and needs no elevation.

---

## The payroll master sheet

**Timesheets &rarr; Payroll master sheet (Excel)** produces the four-weekly wage
sheet in the layout the office already sends to payroll &mdash; the same columns,
merged group headings, borders and yellow/amber input shading as the workbook it
was modelled on. Nothing downstream has to change.

The point of it is that almost nothing is retyped. What fills in where:

| Column | Where it comes from |
|---|---|
| Forename, Surname, Department, Payroll Ref | The employee record |
| Basic (F) &mdash; *rate* | The employee's card, if a rate is recorded; blank otherwise |
| O/T 1.50 (H) &mdash; *rate* | `=F*1.5` |
| Basic (J), O/T 1.50 (K) &mdash; *hours* | The clock, split standard/overtime week by week |
| Holiday (M), SSP Days (Q) | Typed in |
| Back Pay (W), Adjustments (X), Deductions (Y) | Typed in |
| Total Hours (O) | `=J+K+M` |
| Basic (S), O/T 1.50 (U), Holiday (V) &mdash; *pay* | `=J*F`, `=K*H`, `=M*F` |
| TOTAL PAY (Z) | `=S+U+V+W+X-Y` |
| Notes (AD) | Any timesheet warning, e.g. a missed clock-out |
| Start / leaving dates (AB, AC) | Typed in |

Because those are live formulas and not numbers, typing a holiday figure or
correcting a rate re-totals the row in front of whoever is checking it.

Points worth knowing:

- **Every active four-weekly employee gets a row**, including anyone who clocked
  nothing in the period &mdash; payroll still has to put their holiday or sick days
  somewhere, and a name silently missing from a wage sheet is how somebody ends
  up unpaid. To drop somebody from the sheet, untick Active on their record;
  their history is kept either way.
- **Salaried staff are the one deliberate exception.** They are paid a fixed
  amount whatever they clock, so a row of hours and rates for them would be a
  wage this system has no business working out. The Timesheets page names who
  has been left off and links to them, because an exclusion nobody is told about
  is indistinguishable from a bug. Set the basis on the employee's record under
  **Paid**; everyone is four-weekly unless changed.
- **A missing rate leaves the cell blank, not zero.** A blank prompts payroll to
  type the rate; a zero would quietly pay nothing. Nothing here ever guesses a
  wage, the same rule the timesheet follows for a missing standard week.
- **Only the basic rate is ever entered.** Column H is `=F*1.5`, not a second
  figure to keep in step, so a rise typed into F carries through to overtime by
  itself. That formula is on every row even where no rate is on file yet &mdash;
  it is the rule rather than a value, so typing a rate into F in Excel fills in
  the overtime rate without anybody touching column H. Column F is shaded yellow
  because it is typed in; H is not, because it is worked out.
- **SSP sits outside TOTAL PAY.** The sheet records the number of days, not a
  rate, so statutory sick pay is settled by the payroll bureau.
- **Period and dates** fill in automatically. Four-weekly periods are counted
  from `PAYROLL_PERIOD_1_START` in `.env` (the Monday period 1 began), so
  17/08/26&ndash;13/09/26 comes out as period 6. Add `&period=9` to the URL to
  override it.

### Pay rates are encrypted, not hashed

One rate is stored per person &mdash; the basic hourly one &mdash; as ciphertext. The
overtime rate is not stored at all: it is basic &times; 1.5, worked out on
demand, which is one fewer number to keep in step and one fewer to leak. The key
lives in `.env` as `PAYROLL_KEY`, never in the database, so a stolen backup or a
`.sql` dump reveals no wages.

It is **encrypted rather than hashed deliberately.** A hash is one way: a
hashed rate could never be shown back on the card or multiplied by anybody's
hours, which is the entire purpose of storing it. Worse, it would not even be
secret &mdash; an hourly rate has only a few thousand plausible values, so every
one of them can be hashed and compared in well under a second. Hashing would
destroy the feature and buy nothing. Encryption gives the property actually
wanted: unreadable in the database, readable by the application.

Generate a key before going live:

```bash
flask --app wsgi payroll-key      # prints a line to paste into .env
```

Leave `PAYROLL_KEY` blank and one is derived from `SECRET_KEY` so a fresh install
works out of the box &mdash; but rotating `SECRET_KEY` then makes every stored rate
unreadable. Set a real one in production, keep it with your other secrets, and
**back it up separately from the database**: without it the rates cannot be
recovered. If the key ever does change, the rate simply shows as blank rather
than taking the page down.

### Importing the staff list

`scripts/import_staff.py` loads the payroll list into the employee table:

```bash
python scripts/import_staff.py --dry-run     # show what would change
python scripts/import_staff.py               # write it
python scripts/import_staff.py --file Format.xlsx    # read from the workbook
```

Everyone is created on the **default shift and default standard week** (07:30&ndash;16:00
and 40 hours out of the box) by leaving both keys NULL, so changing either
default later moves everybody who has not been given one of their own. Re-running
is safe: people are matched on payroll reference, so nobody is created twice.

One case it handles rather than blunders into: somebody enrolled during testing
already exists under a made-up reference like `test7`. Creating a second record
for them would be worse than it looks &mdash; the face index would clock them onto
the *old* record while payroll reads the new one, so their hours would silently
go missing. The import reports the clash and skips them; `--adopt` moves the
existing record onto the payroll reference instead, keeping their face enrolment
and clocking history.

---

## Requirements

- Python 3.11 or newer (developed and tested on 3.14).
- MySQL 8 (or MariaDB 10.6+).
- A webcam on the kiosk machine.
- Roughly 100 MB of disk for the models and virtual environment.

No GPU. Recognition takes a few milliseconds per frame on an ordinary office PC.

---

## Installation

```bash
# 1. Virtual environment
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux / macOS

# 2. Dependencies
pip install -r requirements.txt

# 3. Face models (~39 MB, fetched from the OpenCV Model Zoo)
python scripts/fetch_models.py

# 4. Configuration
copy .env.example .env            # cp on Linux / macOS
```

Now edit `.env`. At minimum set the MySQL credentials, and generate real secrets:

```bash
python -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))"
python -c "import secrets; print('KIOSK_TOKEN=' + secrets.token_urlsafe(32))"
```

The application refuses to start in production mode while either is still a
placeholder.

Create a MySQL user and the database:

```sql
CREATE DATABASE clocking CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'clocking'@'localhost' IDENTIFIED BY 'a-strong-password';
GRANT ALL PRIVILEGES ON clocking.* TO 'clocking'@'localhost';
FLUSH PRIVILEGES;
```

Then create the tables and your first administrator:

```bash
python scripts/init_db.py --admin office
```

`scripts/init_db.py --create-database --root-user root` will also issue the
`CREATE DATABASE` for you if you would rather not do it by hand. The script
never drops anything, so it is safe to re-run.

**Upgrading an existing installation** is the same command: re-running
`scripts/init_db.py` adds any missing tables and columns and seeds the 40-hour
and 32-hour standard weeks. Adding overtime brings in a `working_week` table,
`employee.working_week_id` and `shift_pattern.pay_beyond_end`, the last of which
is switched **on** for every existing shift — so time worked after the shift end
starts being paid as overtime. If a shift should not work that way, untick
"pay time worked after the shift end" for it on the Shifts and hours page before
the next payroll run.

Start it:

```bash
python run.py            # development, http://127.0.0.1:5000
python wsgi.py           # production via Waitress, port 8000
```

Sign in at `/login`, add an employee, enrol their face, then open `/` on the
kiosk machine.

`schema.sql` holds the MySQL DDL if a DBA wants to review or apply it directly.

### If the database is not on this machine

A managed database (DigitalOcean, RDS, Azure) is reached across the public
internet. Face templates are biometric data and the password travels on the same
connection, so **the link must be encrypted**.

This is handled for you: `MYSQL_SSL_MODE` defaults to `verify-identity` for any
non-local host, and to `disabled` for `localhost`. Production mode refuses to
start if you point it at a remote database with TLS switched off.

| `MYSQL_SSL_MODE` | Behaviour |
|---|---|
| *(blank)* | Automatic: `disabled` for localhost, `verify-identity` otherwise. Recommended. |
| `verify-identity` | Encrypt, and verify the server certificate and hostname. |
| `required` | Encrypt, but do not verify the certificate. Use only if verification fails and you accept the risk. |
| `disabled` | No encryption. Acceptable only for a database on this machine. |

If your provider issues its own CA certificate, download it and set
`MYSQL_SSL_CA` to its path — for DigitalOcean it is `ca-certificate.crt` on the
database's Connection Details page.

To confirm a live connection really is encrypted:

```sql
SHOW STATUS LIKE 'Ssl_version';   -- should report TLSv1.2 or TLSv1.3, not blank
```

Do not take an absence of errors as proof: several plausible PyMySQL settings
(`ssl={}`, `ssl_verify_cert=False`, `ssl_disabled=False`) connect in **plaintext**
while appearing to enable TLS. `app/config.py` uses the combination that was
checked against a real server, and `tests/test_config.py` guards it.

---

## Hands-free clocking

By default nobody touches anything: the kiosk notices somebody arrive,
recognises them, and records the entry.

```
somebody arrives          recognised                countdown ends
      │                       │                           │
      ▼                       ▼                           ▼
   LOOKING ──────────▶  "Sam Fletcher                 entry written
 (presence seen)         Clocking IN  4"              to the database
                          [ Cancel ]
                              │
                     walk away / press Cancel
                              │
                              ▼
                       nothing recorded
```

### It works as a toggle

Clocked in? The next time the kiosk sees you, it clocks you out. Clocked out? It
clocks you in. Nothing to press, no direction to choose.

Nothing is ever refused. There is no minimum interval, no "already clocked in"
message, and no requirement to walk away first. **Any recognised face is clocked,
including the same face again**, to the opposite of its current state:

```
        face detected  ─────▶  "Clocking IN 2..."  ─────▶  clocked IN
                                                                │
        still there    ─────▶  "Clocking OUT 2..." ─────▶  clocked OUT
                                                                │
        still there    ─────▶  "Clocking IN 2..."  ─────▶  clocked IN
```

The result panel never holds the kiosk up: scanning continues underneath it, and a
new recognition simply overwrites what is on screen.

**This puts all the weight on the countdown.** A person who stays in front of the
camera is clocked roughly every three seconds, and the only thing between a passing
glance and a recorded entry is `AUTO_CONFIRM_SECONDS` and the Cancel button. That
is a deliberate choice for a kiosk in a doorway people walk up to and leave. If
yours is somewhere people loiter — facing a desk, or a walkway — set
`AUTO_REQUIRE_DEPARTURE=true` and the old behaviour returns: a person must leave
the camera's view before they can be clocked again, held **per person** so a queue
still moves.

Replayed or double-submitted confirmations are handled separately, by making each
confirmation token **single use**. That was previously the minimum interval's job,
and it was the wrong tool: it blocked genuine clocking as well as replays.

Two settings control it:

- `AUTO_REQUIRE_DEPARTURE` (default `true`) — the rule above.
- `AUTO_DEPARTURE_MS` (default 900) — how long the scene must read empty to count
  as having left. Long enough to ignore somebody shifting their weight.

Because the departure check is now the *only* throttle, it matters that it cannot
get stuck — hence the three independent re-arm signals below. Do not set
`AUTO_REQUIRE_DEPARTURE=false` while hands-free is on: with no throttle at all,
somebody standing in front of the camera would be clocked every few seconds.

#### When departure gating is switched on

Everything in this subsection applies only with `AUTO_REQUIRE_DEPARTURE=true`. With
the default (off) there is nothing to re-arm — a recognised face is always clocked.

#### Three ways to re-arm, because one is not enough

The browser's presence check is a crude signal: one global threshold against a
reference image of the empty scene. It handles somebody walking in well — a large,
obvious change — and handles the marginal cases badly:

- a person in dark clothing against a dark background;
- a doorway that never fully clears;
- a queue at shift change, where the scene is never empty between two people;
- a threshold set slightly too high or too low for the room.

Relying on it alone is what made clocking work reliably on the way in and behave
unpredictably afterwards. The face detector, by contrast, knows for certain
whether a face is in front of the camera and whose it is. So the kiosk now re-arms
on **any** of three signals:

| Signal | Setting | Speed | Reliability |
|---|---|---|---|
| Presence check reports an empty scene | `AUTO_DEPARTURE_MS` | fastest (~1 s) | fragile |
| **Server reports no face** (or a different person) | `AUTO_LATCHED_POLL_MS` | ~3 s | reliable |
| Timeout | `AUTO_REARM_SECONDS` | slowest | always works |

Any one of them is enough, so no single unreliable signal can wedge the kiosk. The
test suite fails if the two reliable ones are both switched off.

The "different person" case matters in its own right: at a shift change the scene
never goes empty between two people, so waiting for that would leave the second
person in the queue unable to clock.

#### And the reverse: somebody the presence check cannot see

If the presence check says "empty" while somebody really is standing there, the
fast path never starts. `AUTO_IDLE_POLL_MS` (default 4000) is a slow poll that
asks the server directly every few seconds regardless, so the presence check can
only ever make clocking *faster*, never impossible. Set it to `0` to trust the
presence check completely.

#### If the camera never sees an empty scene

Departure gating on its own fails closed. Point the kiosk at a desk you sit at, or
a doorway that is never clear, and the scene never reads empty — so the kiosk
clocks you once and then never again. That is a real trap, and it is why
`AUTO_REARM_SECONDS` (default 30) exists: the kiosk re-arms after that long
regardless, and the screen counts it down ("All set — step away from the camera
(ready again in 12s)") so the wait is never a mystery.

Departure remains the fast path — walk away and it re-arms in about a second. The
timer is only there so it can never get stuck. The trade-off runs the other way:
somebody permanently in view is offered a clock every `AUTO_REARM_SECONDS`, with
the countdown as the guard, so do not set it very low. `0` disables the fallback
and relies on departure alone.

### Why there is a countdown

The obvious design — recognise a face, write the row immediately — has a nasty
failure mode. Walk past the kiosk mid-shift and it clocks you *out*, quietly
losing the rest of your day's pay. Two things stand between that and a wrong
entry:

1. **Nothing is written until the countdown finishes.** Recognition and recording
   are separate steps (`/identify` then `/commit`). Walking away, or pressing
   **Cancel** or Escape, means no entry ever existed — nothing to undo and
   nothing for the office to correct.
2. **A button press always wins.** If you genuinely arrive and leave straight
   away, press **Clock out** — a pressed button states intent, so it overrides
   the interval.

**Be aware of the trade-off.** Because toggling is deliberately easy, the
countdown is doing most of the safety work: somebody who walks up to the camera
mid-shift, for any reason, *will* be offered a clock-out, and only the countdown
stops it. That is the price of the kiosk behaving like a switch.

This is a question of siting more than settings. A kiosk in a doorway people stop
at is fine; one in a corridor people walk through is not, because they will be
clocked in passing. If you cannot avoid that, lengthen `AUTO_CONFIRM_SECONDS` so
there is more time to cancel, or set `KIOSK_AUTO_MODE=false` and use the buttons.

Check the timesheet report for a week after go-live: unexpectedly short shifts, or
rows flagged "No clock-out recorded", are the symptom of accidental clocking.

Automatic entries are stored with `method = "auto"`, so a payroll query can tell
them from a deliberate scan (`face`) or an office correction (`manual`).

### Speed, and how far away it works

Both were measured rather than guessed (`FACE_DETECT_MAX_SIDE` etc. are all
tunable in `.env`):

| | Time to show the name | Time to write the entry |
|---|---|---|
| Before | 1.03 s | 5.03 s |
| Now | **0.54 s** | **2.54 s** |

The interesting part is where the time was *not* going. Recognition itself takes
about 42 ms; what cost a second was grabbing three frames 320 ms apart before the
request even left the browser, and then a four-second countdown. So the wins were
two frames instead of three, a 2 s countdown instead of 4 s, and a faster
presence check — not faster maths.

That also means detection range was available almost free. Detection costs ~11 ms
at 640 px and ~28 ms at 960 px, both irrelevant beside the capture time, so the
frames are now 960 px wide. **The browser was the real limit**: it downscaled
every upload to 640 px, and no server setting can recover detail that has already
been thrown away. `CAPTURE_MAX_WIDTH` controls that, and the test suite asserts it
is never below `FACE_DETECT_MAX_SIDE`.

`FACE_MIN_PIXELS` dropped from 80 to 55 on measured evidence: on real photographs
a face is still matched at ~0.92 cosine (against ~0.10 for a different person)
down to about 47 px wide, and YuNet still detects one at 30 px. 55 keeps headroom
for a soft webcam frame. Together — 1.5× the pixels and a 1.45× lower floor — a
face is recognised at roughly **twice** the distance it used to be.

If people are still not picked up far enough away, in order of effect: check the
camera actually delivers more than 720p (`CAPTURE_MAX_WIDTH` cannot invent
detail), raise `CAPTURE_MAX_WIDTH` and `FACE_DETECT_MAX_SIDE` together to 1280,
then lower `FACE_MIN_PIXELS` towards 45. Use **Camera check** to see the real
face-pixel figures at the distance people actually stand.

### Seeing what the kiosk is thinking

Open the kiosk with `?debug=1` — for example `http://localhost:8000/?debug=1` — and
it overlays its live state: the presence score against the threshold, whether it is
armed or latched (and how long until it re-arms), the last reply from the server,
and the miss count. Tuning `AUTO_PRESENCE_THRESHOLD` by guesswork is miserable;
this shows the number you are trying to set. It is off unless asked for, so the
shop-floor screen stays clean.

### When it cannot recognise somebody

A single miss is normal while somebody walks up, so the screen stays quiet rather
than flickering. But after a few consecutive misses it says what would help —
"come a little closer", "hold still", "one at a time", "not recognised, try the
Scan button". Failing silently would leave somebody watching a screen that looks
broken.

### A queue keeps moving

Showing somebody their result does not pause the kiosk. The "must have left first"
rule is held **per person** — "Sam has just clocked, so do not clock Sam again
yet", never "nobody may clock" — so while Sam's result is still on screen the next
person is recognised, their countdown replaces the display, and they are clocked.
Measured in the browser harness: the second person waits about 2.4 seconds, of
which 2 seconds is their own confirmation countdown.

For the first few seconds after a clock the kiosk deliberately keeps polling at
full rate, because that is exactly when a queue forms. Once that window passes it
drops to `AUTO_LATCHED_POLL_MS`, so somebody merely lingering in view does not
generate requests indefinitely.

Two people alternating at the kiosk each toggle independently — verified end to end
with the real models:

```
Alys  -> direction=in    recorded=True
Bryn  -> direction=in    recorded=True
Alys  -> direction=out   recorded=True
Bryn  -> direction=out   recorded=True
```

### Bystanders

A shop floor is busy, and refusing every frame containing two faces would make
hands-free clocking unusable. Instead the nearest face wins, provided it is at
least `FACE_DOMINANT_RATIO` (default 1.35) times wider than the next — somebody
clearly closest to the camera is the person using the kiosk. If two people are
equally close, nobody is clearly "at" the kiosk, so it refuses and asks them to
step up one at a time rather than guessing.

The stricter one-person-only rule still applies to button presses.

### It does not run recognition all day

Face recognition on every frame, all day, for an empty doorway would be a waste
of the machine. So the browser answers the cheap question first — "has anybody
arrived?" — by comparing a small greyscale frame against a reference image of
the empty scene, and only then asks the server the expensive question, "who is
this?".

Comparing against the *empty scene* rather than the previous frame is deliberate:
somebody standing still, waiting to be clocked, produces almost no frame-to-frame
change but a large difference from the empty doorway — and that is exactly the
person we must not miss. The reference is re-learned whenever the scene reads as
empty, so daylight changing through the workshop windows does not slowly become
a permanent false trigger.

If the kiosk keeps waking at shadows or passing forklifts, raise
`AUTO_PRESENCE_THRESHOLD`. If it ignores people who approach slowly, lower it.

### Turning it off

Set `KIOSK_AUTO_MODE=false` and the kiosk reverts to press-to-scan. The badge at
the top of the kiosk screen always shows which mode is active.

---

## Important: the camera needs a secure origin

Browsers only grant camera access on a *secure origin*. In practice:

- `http://localhost` and `http://127.0.0.1` **work**.
- `https://anything` **works**.
- `http://192.168.1.50` (a plain-http LAN address) is **refused** — the camera
  will not start and the page will say so.

For a small site, cheapest first:

1. **Run the browser on the server machine.** Point the kiosk at
   `http://localhost:8000`. Nothing else to configure. This is the recommended
   setup for a single kiosk.
2. **Reverse proxy with a certificate.** Put Caddy or nginx in front with an
   internal or self-signed certificate, and trust that certificate on the kiosk
   machine. Needed if the kiosk is a separate device from the server.

---

## Tuning recognition

Every threshold lives in `.env`. Before changing any of them, open
**Camera check** in the office pages and measure your actual camera and lighting
— it reports face size, sharpness, motion and match scores without recording
anything.

| Setting | Default | What it does |
|---|---|---|
| `FACE_MATCH_THRESHOLD` | `0.40` | Cosine similarity needed to accept a match. Raise towards `0.45` to reduce the chance of a wrong match; lower it if genuine employees are being refused. OpenCV's reference figure for this model is `0.363`. |
| `FACE_MATCH_MARGIN` | `0.05` | The best match must beat the runner-up by this much. Guards against look-alikes; raising it makes the system say "see the office" rather than guess. |
| `FACE_MIN_PIXELS` | `55` | Minimum detected face width — the main control on how far away recognition works. Measured floor is ~47px; below ~45 accuracy falls away. |
| `FACE_MIN_SHARPNESS` | `45.0` | Blur gate, measured on the aligned crop. Set to roughly half of a good reading from Camera check. |
| `SCAN_FRAMES` / `SCAN_MIN_AGREE` | `3` / `2` | Frames captured per scan, and how many must name the same person. Requiring agreement stops one unlucky frame writing the wrong name into the log. |
| `CLOCK_COOLDOWN_SECONDS` | `5` | Button presses only: guards an accidental double-tap on Scan. Hands-free entries ignore it. |
| `LIVENESS_REQUIRE_MOTION` | `true` | See the honest assessment below. |
| `KIOSK_AUTO_MODE` | `true` | Hands-free clocking. `false` reverts to press-to-scan. |
| `AUTO_CONFIRM_SECONDS` | `2` | Cancellable countdown before an automatic entry is written. `0` records instantly (not advised — it removes the only guard against being clocked out in passing). |
| `AUTO_REQUIRE_DEPARTURE` | `false` | Off: any recognised face is clocked, the same face included. `true` requires the person to leave the view first (held per person, so a queue still moves) — use it where people loiter in front of the camera. |
| `AUTO_DEPARTURE_MS` | `900` | How long the scene must read empty to count as having left. |
| `AUTO_REARM_SECONDS` | `30` | Re-arm after this long even if the scene never reads empty. Stops the kiosk getting permanently stuck when the camera can always see somebody. `0` disables it. |
| `AUTO_LATCHED_POLL_MS` | `1500` | How often to ask the server "is anybody still there?" while latched. The reliable re-arm signal. `0` disables it. |
| `AUTO_IDLE_POLL_MS` | `4000` | Slow poll so somebody the presence check cannot see is still clocked. `0` disables it. |
| `AUTO_PRESENCE_THRESHOLD` | `7.0` | How much the scene must change to count as somebody arriving. Raise if it wakes at shadows. |
| `FACE_DOMINANT_RATIO` | `1.35` | How much nearer the kiosk user must be than a bystander behind them. |
| `CAPTURE_MAX_WIDTH` | `960` | Width the browser downscales to before upload. The binding constraint on range — the server cannot recover detail discarded here. |
| `FACE_DETECT_MAX_SIDE` | `960` | Width the server detects at. Keep equal to `CAPTURE_MAX_WIDTH`. |
| `AUTO_SCAN_FRAMES` | `2` | Frames per hands-free check. Two is the liveness minimum; capture time dominates responsiveness. |
| `AUTO_FRAME_GAP_MS` | `300` | Gap between those frames. Shortening it makes live people look like photographs to the liveness check. |
| `TIMEZONE` | `Europe/London` | Used for day boundaries and all displayed times. |

For good recognition, enrol people **at the kiosk, under the kiosk's lighting**,
with a few different head angles, and including safety glasses or hair nets if
those are normally worn.

---

## What the liveness check is and is not

`app/face/liveness.py` compares the aligned face crops across the frames of one
scan and requires that something about the face changed. A live face is never
perfectly still; a photo held up to the camera produces near-identical crops.

**It stops:** a frozen or stalled camera feed, and a photograph held perfectly
still in front of a clean sensor.

**It does not stop:** a video played back on a screen, a mask, or — measured, not
assumed — **a photograph held in the hand**. Because alignment removes translation,
the frame-to-frame difference for a *settled live face* is dominated by sensor
noise: measured at 2.8–5.2 grey levels against a 1.6 threshold. A hand-held photo
produces the same noise, so it passes too. The check reliably catches only an
input that is *identical* frame to frame.

Treat it as a deterrent against the laziest attack, and nothing more. If your risk
assessment needs better, the honest options are a supervised kiosk, a second factor
(a payroll PIN alongside the face), or a camera with real depth or infra-red
liveness hardware.

For a kiosk inside a workshop, in sight of a supervisor, that is usually the
right trade-off. If your risk assessment says otherwise, the honest options are
a supervised kiosk, a second factor alongside the face, or a camera with genuine
depth or infra-red liveness hardware. Do not assume this code is more than it is.

---

## Data protection

Face templates are numeric vectors, not photographs: the captured images are
used to compute a template and then discarded. **This is still biometric data,
and under UK GDPR biometric data used to identify someone is special-category
data** (Article 9). Before you enrol a single employee:

- Identify your lawful basis, and an Article 9 condition. Consent from an
  employee is often not considered freely given, because of the imbalance of
  power in an employment relationship — so consent is usually the *weaker*
  choice here, not the safer one.
- Complete a Data Protection Impact Assessment. For biometric monitoring of
  staff, the ICO regards a DPIA as required, not optional.
- Offer a genuine, non-detrimental alternative for anyone who objects (the
  manual-entry feature exists partly for this).
- Update your privacy notice, retention schedule and records of processing.
- Set a retention period for attendance data and face templates, and apply it.

**This section is a pointer, not legal advice.** Biometric monitoring of staff
is an area where the ICO has taken enforcement action against employers. Have
your DPIA reviewed by someone qualified — a data protection adviser or
employment solicitor — before go-live.

Practical measures already in the code: face data lives only in your MySQL
database and never leaves the server; the admin area requires a login;
`Remove face data` on an employee deletes their templates immediately; and
deactivating an employee drops them from the recognition index.

---

## Running the tests

```bash
python -m pytest
```

148 tests, no MySQL needed — the suite runs against SQLite in memory. With face
photos added (see below) that becomes 156. The browser harnesses add 55 more
checks on the kiosk JavaScript.

### The kiosk JavaScript

The hands-free countdown lives in browser code, so `tests/js/kiosk_harness.js`
stubs the DOM, camera and network and drives the real `kiosk.js` with fake
timers, checking that an empty doorway produces no requests, that nothing is
committed while the countdown runs, that letting it finish commits exactly once,
that **Cancel prevents the commit**, that standing still does not clock you
repeatedly while leaving and returning does, that a camera which never sees an
empty scene still recovers, and that repeated misses produce an
actionable hint. `pytest` runs it automatically when
Node is installed, and skips it otherwise. To run it directly:

```bash
node tests/js/kiosk_harness.js
```

A second harness, `tests/js/immediate_test.js`, covers the shipped default (any
face clocks immediately) and pins the stuck-result regression.

They earn their keep: they caught the recognition poll timer not being
stopped when a countdown began, which meant that once the screen returned to
idle the stale poll kept calling `/identify` with nobody in front of the camera
and started a fresh countdown that then committed an entry.

### Checking accuracy with your own photos

Eight tests are skipped by default because they need real faces, which cannot be
committed to a repository. To enable them, drop a few photos into
`tests/fixtures/faces/`:

```
tests/fixtures/faces/
    sam_1.jpg     # two or more photos of one person
    sam_2.jpg
    ada_1.jpg     # and at least one of somebody else
```

Photos sharing the prefix before the first underscore are treated as the same
person. That folder is git-ignored — the photos are personal data and must stay
local.

With those in place, `pytest` additionally verifies end to end that:

- enrolment, clock-in, clock-out and CSV export all work through real HTTP calls
  with the real models;
- **a different person is not recognised** as an enrolled employee;
- **a still photo held up to the camera is refused** by the liveness check;
- the same face cannot be enrolled twice under two payroll references;
- two photos of one person score above the match threshold, and two different
  people score below it.

That last group is worth running with photos of your own staff before go-live —
it is the closest thing to a site acceptance test.

---

## How it works

```
Browser (kiosk)                  Flask                        MySQL
--------------                   -----                        -----
presence check                                               (nothing yet)
 (browser only, cheap)
      │ somebody there
      ▼
capture 3 frames  ──POST /identify─▶  blueprints/kiosk.py
                                       └▶ services/recognition.scan()
                                           ├▶ face/engine.py    YuNet detect
                                           │                    SFace embed (128 floats)
                                           ├▶ face/liveness.py  frames must differ
                                           └▶ face/matcher.py   cosine vs every template
                  ◀──JSON────────────  who, direction, signed token
      │
      │ countdown; Cancel stops here
      ▼
     ──POST /commit────────────────▶  verify signature
                                       services/attendance.py
                                        └▶ apply interval  ──────────▶ attendance_event
                  ◀──JSON────────────  name, direction, time
```

The button path (`/scan`) collapses both steps into one request, because a press
already states intent.

| Path | Purpose |
|---|---|
| `app/face/engine.py` | Detection, alignment, embedding, quality gates. |
| `app/face/matcher.py` | The in-memory index and the threshold/margin/voting rules. |
| `app/face/liveness.py` | The presentation-attack deterrent. |
| `app/services/recognition.py` | Ties the engine and index to Flask and MySQL. |
| `app/services/attendance.py` | Alternation and cooldown rules, and the daily present/absent split. |
| `app/services/enrolment.py` | Enrolment with same-person and duplicate checks. |
| `app/services/timesheet.py` | Pairing events into shifts, paid hours, Monday–Sunday weeks, the standard/overtime split, CSV, timezones. |
| `app/services/payroll_sheet.py` | The four-weekly master sheet: the payroll workbook's layout, and its live formulas. |
| `app/services/payrates.py` | The encrypted basic rate, the derived overtime rate, and the key handling behind them. |
| `scripts/import_staff.py` | Loading the payroll staff list into the employee table. |
| `scripts/fingerprint_reader.py` | Agent that reads a fingerprint reader and posts matches. |
| `app/blueprints/` | Kiosk, auth and admin routes. |
| `app/security.py` | Rate limiting and the kiosk shared secret. |

Design decisions worth knowing:

- **Timestamps are stored in UTC** and converted to local time only for display,
  so the BST/GMT change cannot corrupt stored data. A shift is credited to the
  local date it started, keeping night shifts on one line.
- **The event log is append-only.** A wrong entry is voided, never overwritten,
  and a correction is added — so the audit trail survives.
- **Unpaired entries are always flagged.** If somebody forgot to clock out on a
  shift that has already ended, the paid hours assume they left at the shift end
  and the row says so. Without a shift, or while the shift is still running,
  the hours are left blank for a human to settle.
- **Overtime is settled per Monday–Sunday week**, never on the reporting period
  as a whole. Fifty hours one week and thirty the next is ten hours of overtime,
  not none: overtime already worked is not repayable by working less later. It
  is why the timesheet warns when the chosen range is not whole weeks — half a
  week measured against a full week's contract reads low — and why the
  week-by-week CSV exists, since overtime is usually paid at another rate.
- **A missing standard week never invents overtime.** Somebody with no
  contracted week set (and no default) has every paid hour counted as standard.
  Guessing here would inflate a wage bill.
- **Matching is a linear scan** — one matrix-vector product against every
  template. At small-manufacturer headcount this is sub-millisecond, and it
  avoids an approximate-nearest-neighbour index that would need maintaining.
- **The rate limiter is in-process**, not Redis. This runs as one Waitress
  process on an office PC; an extra service to install and back up would cost
  more than it adds.

---

## Running as a Windows service

Waitress runs happily under [NSSM](https://nssm.cc/):

```
nssm install ModuflexClocking "D:\Clock in Clock Out\Clock-in---Clock-Out\.venv\Scripts\python.exe"
nssm set    ModuflexClocking AppDirectory "D:\Clock in Clock Out\Clock-in---Clock-Out"
nssm set    ModuflexClocking AppParameters "wsgi.py"
nssm start  ModuflexClocking
```

Monitor `GET /healthz` — it returns 503 if the database is unreachable or the
face models are missing.

**Back up the MySQL database.** The face templates live there, and losing them
means re-enrolling everybody.

```bash
mysqldump -u clocking -p clocking > clocking-backup.sql
```

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Camera unavailable" on the kiosk | Not a secure origin. Use `localhost` or put HTTPS in front — see above. |
| Kiosk clocks people out as they walk past | Lengthen `AUTO_CONFIRM_SECONDS` so there is more time to cancel, or move the camera so it only sees people who stop at it. |
| Kiosk keeps waking with nobody there | Raise `AUTO_PRESENCE_THRESHOLD`. |
| Hands-free never triggers | Lower `AUTO_PRESENCE_THRESHOLD`; check the mode badge says "Automatic"; check **Camera check** sees a face. |
| Not picked up until you are close | Raise `CAPTURE_MAX_WIDTH` and `FACE_DETECT_MAX_SIDE` to 1280, then lower `FACE_MIN_PIXELS` towards 45. Check the camera is better than 720p. |
| Live people rejected as photographs | The liveness gap is too short or the camera too noisy. Raise `AUTO_FRAME_GAP_MS`, or lower `LIVENESS_MIN_MOTION` (weakens photo protection). |
| Clocked in but wanted to stay in | Press **Cancel** during the countdown. |
| It keeps clocking me while I stand there | Expected with the default `AUTO_REQUIRE_DEPARTURE=false`. Set it to `true`, or lengthen `AUTO_CONFIRM_SECONDS` so there is more time to cancel. |
| Result panel stays on screen | Fixed: the revert used to require a state the kiosk had already left. If you see it again, check the browser console for a JavaScript error. |
| It clocked me twice | Check `AUTO_REQUIRE_DEPARTURE` is `true`. Departure is the only throttle; with it off, anybody in view is clocked repeatedly. |
| A queue is slow at shift change | Each person still gets their own `AUTO_CONFIRM_SECONDS` countdown, which is the floor. Shorten it if the queue matters more than the chance to cancel. |
| Screen says "step away from the camera" | Only appears with `AUTO_REQUIRE_DEPARTURE=true`: you have been clocked and must leave the view before clocking again. |
| Works walking in, unpredictable afterwards | The presence check is missing the departure. The server-side check (`AUTO_LATCHED_POLL_MS`) now covers this; confirm with `?debug=1` that "last re-arm" changes after somebody leaves. |
| Clocks once, then never again | The camera can always see somebody, so the departure check never clears. It now frees itself after `AUTO_REARM_SECONDS`; open the kiosk with `?debug=1` to watch the presence score and latch state live. Move the camera so it sees an empty scene, or raise `AUTO_PRESENCE_THRESHOLD`. |
| Will not clock me out when I come back | The kiosk has not seen the doorway empty. Check **Camera check**: the presence score must drop below the threshold when nobody is there. Lower `AUTO_DEPARTURE_MS` or raise `AUTO_PRESENCE_THRESHOLD`. |
| "Face recognition is not set up on this server" | Run `python scripts/fetch_models.py`. |
| "Face not recognised" for a known employee | Re-enrol at the kiosk under kiosk lighting. Check Camera check readings; consider lowering `FACE_MATCH_THRESHOLD` slightly. |
| "Could not tell you apart from another record" | Two enrolments are too similar — often the same person enrolled twice. Check the employee list, remove the duplicate. |
| "Live camera check failed" | Someone is very still, or the feed has frozen. Raise nothing yet: check the feed first, then consider lowering `LIVENESS_MIN_MOTION`. |
| Refuses to start: "placeholder SECRET_KEY" | Set real secrets in `.env`. |
| Refuses to start: "would cross the network unencrypted" | Remote database with TLS off. Set `MYSQL_SSL_MODE=verify-identity`. |
| `SSL: CERTIFICATE_VERIFY_FAILED` connecting to the database | Your provider uses its own CA. Set `MYSQL_SSL_CA` to its certificate, or fall back to `MYSQL_SSL_MODE=required`. |
| `ZoneInfoNotFoundError` | `pip install tzdata` — Windows has no system timezone database. |

---

## Licence

See `LICENSE`.
