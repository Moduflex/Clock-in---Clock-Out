"""Clocking through a Windows Hello fingerprint reader.

A Windows Hello dongle has no slot numbers of its own: it enrols fingerprints
against a *Windows user account*. What makes multi-person clocking possible is
that the Windows Biometric Framework also records **which finger position** each
enrolment used, and reports it back on every match. So one kiosk Windows account
holds up to ten enrolments, one per finger position, and the position is the
identity:

    position 2 (right index)  -> Alice Turner
    position 7 (left index)   -> Bob Ward

The position number is what gets posted to /api/kiosk/fingerprint as the
``finger_id``, so this reader plugs into exactly the same registration and
clocking path as a slot-based reader. No fingerprint or template is ever read by
this script - Windows keeps those, and only ever tells us a position number.

Two consequences you need to know about, both discussed in the README:

* **Ten people per Windows account**, because there are only ten fingers.
* Everyone enrolled can also **sign into that Windows account**, so the kiosk
  account must be a locked-down local account with nothing useful on it.

Usage, in the order you will need it:

    python scripts/windows_hello_reader.py --check          # is the reader usable?
    python scripts/windows_hello_reader.py --list           # what is enrolled
    python scripts/windows_hello_reader.py --enrol 2        # enrol right index
    python scripts/windows_hello_reader.py --probe          # who does a touch report?
    python scripts/windows_hello_reader.py --run            # the clocking loop
    python scripts/windows_hello_reader.py --delete 2       # remove an enrolment

``--run`` may need to be run **as administrator**: identifying fingers that
belong to a *different* Windows account is a privileged operation. If the agent
runs as the same kiosk account the fingers were enrolled on - the normal setup -
it will usually work unelevated. Try it first, and elevate only if it reports
access denied.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.platform != "win32":  # pragma: no cover - the reader is Windows-only
    raise SystemExit("This reader uses the Windows Biometric Framework, so it needs Windows.")


# --- Windows Biometric Framework constants ------------------------------------
WINBIO_TYPE_FINGERPRINT = 0x00000008
WINBIO_POOL_SYSTEM = 0x00000001
WINBIO_FLAG_DEFAULT = 0x00000000

WINBIO_ID_TYPE_SID = 3
SECURITY_MAX_SID_SIZE = 68

S_OK = 0x00000000

# Finger positions are the ANSI 381 numbering the framework uses. These ten
# values are the whole identity space available on one Windows account.
FINGER_POSITIONS = {
    1: "Right thumb",
    2: "Right index",
    3: "Right middle",
    4: "Right ring",
    5: "Right little",
    6: "Left thumb",
    7: "Left index",
    8: "Left middle",
    9: "Left ring",
    10: "Left little",
}

# Only the codes worth naming. Anything else is reported as raw hex rather than
# guessed at, which is also what makes --probe useful for diagnosis.
HRESULT_NAMES = {
    0x80070005: "Access denied - run this as administrator",
    0x80098005: "No match: that finger is not enrolled on this machine",
    0x80098008: "Bad capture - press more firmly and hold still",
    0x8009801C: "No fingerprint reader found",
    0x80098019: "The reader is busy",
    0x80098017: "An enrolment is already in progress",
    0x80098030: "Biometrics are disabled by policy on this machine",
}


# Errors that will not fix themselves by trying again.
_FATAL_CODES = frozenset({0x80070005, 0x8009801C, 0x80098030})


def hresult_text(code: int) -> str:
    name = HRESULT_NAMES.get(code)
    return f"{name} (0x{code:08X})" if name else f"HRESULT 0x{code:08X}"


def succeeded(code: int) -> bool:
    """True for any success HRESULT, S_OK or informational."""
    return not code & 0x80000000


# --- structures ---------------------------------------------------------------
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _AccountSid(ctypes.Structure):
    _fields_ = [
        ("Size", ctypes.c_ulong),
        ("Data", ctypes.c_ubyte * SECURITY_MAX_SID_SIZE),
    ]


class _IdentityValue(ctypes.Union):
    _fields_ = [
        ("Null", ctypes.c_ulong),
        ("Wildcard", ctypes.c_ulong),
        ("TemplateGuid", GUID),
        ("AccountSid", _AccountSid),
    ]


class WinBioIdentity(ctypes.Structure):
    _fields_ = [("Type", ctypes.c_ulong), ("Value", _IdentityValue)]


# --- library bindings ---------------------------------------------------------
try:
    _winbio = ctypes.WinDLL("winbio.dll")
except OSError as exc:  # pragma: no cover - missing on Windows N editions
    raise SystemExit(f"Could not load winbio.dll: {exc}") from None

_advapi32 = ctypes.WinDLL("advapi32.dll")
_kernel32 = ctypes.WinDLL("kernel32.dll")

_SESSION = ctypes.c_size_t  # WINBIO_SESSION_HANDLE is a ULONG_PTR

_winbio.WinBioOpenSession.argtypes = [
    wintypes.ULONG,
    wintypes.ULONG,
    wintypes.ULONG,
    ctypes.POINTER(wintypes.ULONG),
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.POINTER(_SESSION),
]
_winbio.WinBioOpenSession.restype = ctypes.c_uint32

_winbio.WinBioCloseSession.argtypes = [_SESSION]
_winbio.WinBioCloseSession.restype = ctypes.c_uint32

_winbio.WinBioIdentify.argtypes = [
    _SESSION,
    ctypes.POINTER(wintypes.ULONG),
    ctypes.POINTER(WinBioIdentity),
    ctypes.POINTER(ctypes.c_ubyte),
    ctypes.POINTER(wintypes.ULONG),
]
_winbio.WinBioIdentify.restype = ctypes.c_uint32

_winbio.WinBioLocateSensor.argtypes = [_SESSION, ctypes.POINTER(wintypes.ULONG)]
_winbio.WinBioLocateSensor.restype = ctypes.c_uint32

_winbio.WinBioEnrollBegin.argtypes = [_SESSION, ctypes.c_ubyte, wintypes.ULONG]
_winbio.WinBioEnrollBegin.restype = ctypes.c_uint32

_winbio.WinBioEnrollCapture.argtypes = [_SESSION, ctypes.POINTER(wintypes.ULONG)]
_winbio.WinBioEnrollCapture.restype = ctypes.c_uint32

_winbio.WinBioEnrollCommit.argtypes = [
    _SESSION,
    ctypes.POINTER(WinBioIdentity),
    ctypes.POINTER(ctypes.c_ubyte),
]
_winbio.WinBioEnrollCommit.restype = ctypes.c_uint32

_winbio.WinBioEnrollDiscard.argtypes = [_SESSION]
_winbio.WinBioEnrollDiscard.restype = ctypes.c_uint32

_winbio.WinBioEnumEnrollments.argtypes = [
    _SESSION,
    wintypes.ULONG,
    ctypes.POINTER(WinBioIdentity),
    ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
    ctypes.POINTER(ctypes.c_size_t),
]
_winbio.WinBioEnumEnrollments.restype = ctypes.c_uint32

_winbio.WinBioDeleteTemplate.argtypes = [
    _SESSION,
    wintypes.ULONG,
    ctypes.POINTER(WinBioIdentity),
    ctypes.c_ubyte,
]
_winbio.WinBioDeleteTemplate.restype = ctypes.c_uint32

_winbio.WinBioFree.argtypes = [ctypes.c_void_p]
_winbio.WinBioFree.restype = ctypes.c_uint32

_advapi32.ConvertSidToStringSidW.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.LPWSTR),
]
_advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

# Declared explicitly rather than left to ctypes' defaults: a default integer
# argument is a 32-bit C int, which silently truncates a 64-bit pointer.
_advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
_advapi32.OpenProcessToken.restype = wintypes.BOOL

_advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_advapi32.GetTokenInformation.restype = wintypes.BOOL

_advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
_advapi32.GetLengthSid.restype = wintypes.DWORD

_kernel32.GetCurrentProcess.argtypes = []
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


# --- identity helpers ---------------------------------------------------------
def sid_string(identity: WinBioIdentity) -> str:
    """The account SID of a returned identity, as S-1-5-21-... text."""
    if identity.Type != WINBIO_ID_TYPE_SID:
        return f"(identity type {identity.Type}, not an account SID)"
    text = wintypes.LPWSTR()
    ok = _advapi32.ConvertSidToStringSidW(
        ctypes.cast(identity.Value.AccountSid.Data, ctypes.c_void_p),
        ctypes.byref(text),
    )
    if not ok:
        return "(unreadable SID)"
    try:
        return text.value or "(empty SID)"
    finally:
        _kernel32.LocalFree(text)


def current_user_identity() -> WinBioIdentity:
    """A WINBIO_IDENTITY for the Windows account this script is running as.

    Enrolments belong to an account, so listing and deleting them needs the
    account named explicitly rather than inferred from a touch.
    """
    TOKEN_QUERY = 0x0008
    TokenUser = 1

    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise SystemExit("Could not read this process's Windows account token.")

    try:
        size = wintypes.DWORD()
        _advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not _advapi32.GetTokenInformation(
            token, TokenUser, buffer, size.value, ctypes.byref(size)
        ):
            raise SystemExit("Could not read the current Windows account SID.")

        # TOKEN_USER is a SID_AND_ATTRIBUTES: a pointer to the SID, then flags.
        sid_pointer = ctypes.cast(
            buffer, ctypes.POINTER(ctypes.c_void_p)
        ).contents.value
        length = _advapi32.GetLengthSid(sid_pointer)

        identity = WinBioIdentity()
        identity.Type = WINBIO_ID_TYPE_SID
        identity.Value.AccountSid.Size = length
        ctypes.memmove(identity.Value.AccountSid.Data, sid_pointer, length)
        return identity
    finally:
        _kernel32.CloseHandle(token)


# --- session ------------------------------------------------------------------
class Session:
    """An open WBF session on the system fingerprint pool."""

    def __init__(self) -> None:
        self.handle = _SESSION()

    def __enter__(self) -> "Session":
        code = _winbio.WinBioOpenSession(
            WINBIO_TYPE_FINGERPRINT,
            WINBIO_POOL_SYSTEM,
            WINBIO_FLAG_DEFAULT,
            None,
            0,
            None,
            ctypes.byref(self.handle),
        )
        if not succeeded(code):
            raise SystemExit(
                f"Could not open the fingerprint reader: {hresult_text(code)}\n"
                "If this says access denied, run as administrator. If it says "
                "disabled, Enhanced Sign-in Security may be blocking third-party "
                "access to the sensor - see the README."
            )
        return self

    def __exit__(self, *exc) -> None:
        if self.handle:
            _winbio.WinBioCloseSession(self.handle)

    def locate_sensor(self) -> int:
        """Wait for a touch and return which reader unit it came from."""
        unit = wintypes.ULONG(0)
        code = _winbio.WinBioLocateSensor(self.handle, ctypes.byref(unit))
        if not succeeded(code):
            raise SystemExit(f"Could not find the reader: {hresult_text(code)}")
        return unit.value

    def identify(self) -> tuple[int, str, int] | int:
        """Wait for a touch. Returns (position, sid, unit), or the error code."""
        unit = wintypes.ULONG(0)
        identity = WinBioIdentity()
        position = ctypes.c_ubyte(0)
        reject = wintypes.ULONG(0)
        code = _winbio.WinBioIdentify(
            self.handle,
            ctypes.byref(unit),
            ctypes.byref(identity),
            ctypes.byref(position),
            ctypes.byref(reject),
        )
        if not succeeded(code):
            return code
        return position.value, sid_string(identity), unit.value


# --- talking to the app -------------------------------------------------------
def post_scan(base_url: str, token: str, finger_id: int, device_label: str) -> dict:
    body = json.dumps({"finger_id": finger_id, "device_label": device_label}).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/kiosk/fingerprint",
        data=body,
        headers={"Content-Type": "application/json", "X-Kiosk-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "code": f"http_{exc.code}",
            "message": exc.read().decode("utf-8", "replace")[:200],
        }
    except (urllib.error.URLError, TimeoutError) as exc:
        # Somebody is standing at the door; a brief outage must not kill the agent.
        return {"ok": False, "code": "unreachable", "message": str(exc)}


# --- commands -----------------------------------------------------------------
def cmd_check() -> int:
    """Open the reader and report, without needing anyone to touch it."""
    print("Loading winbio.dll ... ok")
    with Session():
        print("Opening the system fingerprint pool ... ok")
    print("\nThe reader is reachable. Next: --enrol to add a finger, or --list.")
    return 0


def cmd_list() -> int:
    identity = current_user_identity()
    print(f"Windows account: {sid_string(identity)}\n")
    with Session() as session:
        print("Touch the reader so Windows can tell which unit to ask about ...")
        unit = session.locate_sensor()

        array = ctypes.POINTER(ctypes.c_ubyte)()
        count = ctypes.c_size_t(0)
        code = _winbio.WinBioEnumEnrollments(
            session.handle,
            unit,
            ctypes.byref(identity),
            ctypes.byref(array),
            ctypes.byref(count),
        )
        if not succeeded(code):
            print(f"Could not list enrolments: {hresult_text(code)}")
            return 1
        try:
            positions = [array[i] for i in range(count.value)]
        finally:
            _winbio.WinBioFree(array)

    if not positions:
        print("Nothing is enrolled on this account yet.")
    else:
        print(f"{len(positions)} of 10 positions enrolled:")
        for position in sorted(positions):
            print(f"  {position:2}  {FINGER_POSITIONS.get(position, 'unknown position')}")
    free = [p for p in FINGER_POSITIONS if p not in positions]
    print(f"\nFree positions: {', '.join(str(p) for p in free) or 'none - the account is full'}")
    return 0


def cmd_enrol(position: int) -> int:
    if position not in FINGER_POSITIONS:
        print("Position must be 1-10. See --help for the list.")
        return 2

    print(f"Enrolling position {position} ({FINGER_POSITIONS[position]}).")
    print("This finger will clock in whoever you register against position "
          f"{position} in the back office.\n")

    with Session() as session:
        print("Touch the reader once so Windows can find it ...")
        unit = session.locate_sensor()

        code = _winbio.WinBioEnrollBegin(session.handle, position, unit)
        if not succeeded(code):
            print(f"Could not start enrolment: {hresult_text(code)}")
            return 1

        try:
            presses = 0
            while True:
                reject = wintypes.ULONG(0)
                code = _winbio.WinBioEnrollCapture(
                    session.handle, ctypes.byref(reject)
                )
                presses += 1
                if code == S_OK:
                    break
                if succeeded(code):
                    print(f"  captured {presses} - press again")
                    continue
                # A bad press is normal during enrolment; keep going.
                print(f"  press {presses} not usable: {hresult_text(code)}")
                if presses > 25:
                    print("Too many failed presses. Starting again may help.")
                    _winbio.WinBioEnrollDiscard(session.handle)
                    return 1

            identity = WinBioIdentity()
            is_new = ctypes.c_ubyte(0)
            code = _winbio.WinBioEnrollCommit(
                session.handle, ctypes.byref(identity), ctypes.byref(is_new)
            )
            if not succeeded(code):
                print(f"Could not save the enrolment: {hresult_text(code)}")
                return 1
        except KeyboardInterrupt:
            _winbio.WinBioEnrollDiscard(session.handle)
            print("\nAbandoned; nothing was saved.")
            return 1

    print(f"\nEnrolled position {position} ({FINGER_POSITIONS[position]}).")
    print(f"Now register slot {position} against the right person in the back office.")
    return 0


def cmd_delete(position: int) -> int:
    if position not in FINGER_POSITIONS:
        print("Position must be 1-10.")
        return 2
    identity = current_user_identity()
    with Session() as session:
        print("Touch the reader once so Windows can find it ...")
        unit = session.locate_sensor()
        code = _winbio.WinBioDeleteTemplate(
            session.handle, unit, ctypes.byref(identity), position
        )
    if not succeeded(code):
        print(f"Could not delete position {position}: {hresult_text(code)}")
        return 1
    print(f"Deleted the enrolment at position {position}.")
    print("Remember to unregister the matching slot in the back office too.")
    return 0


def cmd_probe() -> int:
    """Report exactly what one touch produces - the setup and diagnosis tool."""
    with Session() as session:
        print("Touch the reader ... (Ctrl+C to stop)\n")
        while True:
            outcome = session.identify()
            if isinstance(outcome, int):
                print(f"  no result: {hresult_text(outcome)}")
                time.sleep(0.5)
                continue
            position, sid, unit = outcome
            name = FINGER_POSITIONS.get(position, "unknown position")
            print(f"  position {position} ({name})  unit {unit}  account {sid}")
            print(f"    -> would post finger_id={position}\n")
            time.sleep(0.8)


def cmd_run(url: str, token: str, device_label: str, allowed_sid: str | None) -> int:
    print(f"Posting to {url} as reader {device_label!r}.")
    if allowed_sid:
        print(f"Only accepting touches from Windows account {allowed_sid}.")
    else:
        print("Accepting any enrolled Windows account (use --require-sid to restrict).")
    print()

    with Session() as session:
        print("Ready. Touch the reader to clock in or out. Ctrl+C to stop.\n")
        while True:
            outcome = session.identify()
            if isinstance(outcome, int):
                if outcome in _FATAL_CODES:
                    # Retrying will never help, and a tight loop would bury the
                    # reason in a screenful of repeats.
                    print(f"\nStopping: {hresult_text(outcome)}")
                    return 1
                # No match is the everyday case of an unenrolled finger.
                print(f"  {hresult_text(outcome)}")
                time.sleep(0.6)
                continue

            position, sid, _unit = outcome
            if allowed_sid and sid.lower() != allowed_sid.lower():
                # Another Windows account on this PC also has Hello set up. Its
                # fingers must not clock anybody, or an IT login would clock in
                # whoever happens to be registered at that position.
                print(f"  ignored: position {position} belongs to account {sid}")
                continue

            name = FINGER_POSITIONS.get(position, f"position {position}")
            reply = post_scan(url, token, position, device_label)
            if reply.get("ok"):
                who = (reply.get("employee") or {}).get("name", "?")
                state = "recorded" if reply.get("recorded") else "ignored (too soon)"
                print(f"  {name}: {who} - {reply.get('direction')} {state}")
            else:
                print(f"  {name}: REFUSED [{reply.get('code')}] {reply.get('message')}")
            time.sleep(0.8)


# --- entry point --------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="Is the reader usable?")
    action.add_argument("--list", action="store_true", help="What is enrolled here.")
    action.add_argument("--enrol", type=int, metavar="POS", help="Enrol finger position 1-10.")
    action.add_argument("--delete", type=int, metavar="POS", help="Delete a position.")
    action.add_argument("--probe", action="store_true", help="Report what a touch produces.")
    action.add_argument("--run", action="store_true", help="The clocking loop.")

    parser.add_argument(
        "--url",
        default=os.getenv("CLOCKING_URL", "http://127.0.0.1:5000"),
        help="Where the clocking app is running.",
    )
    parser.add_argument(
        "--device-label",
        default=os.getenv("KIOSK_DEVICE_LABEL", "Kiosk"),
        help="Must match the reader name registered against each slot.",
    )
    parser.add_argument("--token", help="Kiosk token (defaults to KIOSK_TOKEN in .env).")
    parser.add_argument(
        "--require-sid",
        help="Only accept touches from this Windows account SID (see --probe).",
    )
    args = parser.parse_args()

    try:
        if args.check:
            return cmd_check()
        if args.list:
            return cmd_list()
        if args.enrol is not None:
            return cmd_enrol(args.enrol)
        if args.delete is not None:
            return cmd_delete(args.delete)
        if args.probe:
            return cmd_probe()

        try:
            from dotenv import load_dotenv

            load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        except ImportError:
            pass
        token = args.token or os.getenv("KIOSK_TOKEN", "")
        if not token:
            raise SystemExit(
                "No kiosk token. Set KIOSK_TOKEN in .env, or pass --token."
            )
        return cmd_run(args.url, token, args.device_label, args.require_sid)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
