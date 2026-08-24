"""Reads a fingerprint reader and posts each match to the clocking app.

The reader matches the finger in its own memory and reports *which slot*
matched. This agent forwards that slot number to /api/kiosk/fingerprint, which
looks up whose slot it is and records the clock event. No fingerprint, image or
template ever leaves the reader.

Three input modes:

    # No hardware at all: type slot numbers to test the whole path end to end.
    python scripts/fingerprint_reader.py --simulate

    # A reader (or a microcontroller in front of one) that prints the matched
    # slot number as a line of text.
    python scripts/fingerprint_reader.py --serial COM3

    # An R307 / ZFM-20 / FPM10A module driven directly over serial.
    python scripts/fingerprint_reader.py --r307 COM3

Run it on the kiosk machine, as a Windows scheduled task set to run at startup.
It needs KIOSK_TOKEN, which it reads from .env or the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# --- talking to the app -------------------------------------------------------
def post_scan(base_url: str, token: str, finger_id: int, device_label: str) -> dict:
    """Send one match to the clocking app and return its JSON reply."""
    body = json.dumps({"finger_id": finger_id, "device_label": device_label}).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/kiosk/fingerprint",
        data=body,
        headers={"Content-Type": "application/json", "X-Kiosk-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 403 bad token, 400 bad request
        detail = exc.read().decode("utf-8", "replace")[:200]
        return {"ok": False, "code": f"http_{exc.code}", "message": detail}
    except (urllib.error.URLError, TimeoutError) as exc:
        # The app being briefly unreachable must not kill the agent: somebody is
        # standing at the door and will simply press again.
        return {"ok": False, "code": "unreachable", "message": str(exc)}


def report(finger_id: int, reply: dict) -> None:
    if reply.get("ok"):
        who = (reply.get("employee") or {}).get("name", "?")
        state = "recorded" if reply.get("recorded") else "ignored (too soon)"
        print(f"  slot {finger_id}: {who} - {reply.get('direction')} {state}")
    else:
        print(f"  slot {finger_id}: REFUSED [{reply.get('code')}] {reply.get('message')}")


# --- R307 / ZFM-20 serial protocol -------------------------------------------
# The documented protocol, implemented here so the agent needs no extra library
# beyond pyserial.
#
# NOTE: written from the datasheet and exercised only against the simulator. No
# module was on the bench, so treat the port and baud rate as the first things
# to check when the hardware arrives.
_HEADER = b"\xef\x01"
_ADDRESS = b"\xff\xff\xff\xff"
_CMD_GEN_IMAGE = 0x01  # capture whatever is on the sensor
_CMD_IMAGE_TO_TZ = 0x02  # turn it into a feature set
_CMD_SEARCH = 0x04  # look for it in the module's own library
_ACK_OK = 0x00
_ACK_NO_FINGER = 0x02
_ACK_NO_MATCH = 0x09
_ACK_NOT_FOUND = 0x0A


def _command(data: bytes) -> bytes:
    length = len(data) + 2  # the data plus two checksum bytes
    body = bytes([0x01]) + length.to_bytes(2, "big") + data
    checksum = sum(body) & 0xFFFF
    return _HEADER + _ADDRESS + body + checksum.to_bytes(2, "big")


def _read_reply(port) -> bytes | None:
    head = port.read(9)
    if len(head) < 9 or head[0:2] != _HEADER:
        return None
    length = int.from_bytes(head[7:9], "big")
    rest = port.read(length)
    if len(rest) < length:
        return None
    return rest[:-2]  # strip the checksum


def _ask(port, data: bytes) -> bytes | None:
    port.reset_input_buffer()
    port.write(_command(data))
    return _read_reply(port)


def _require_pyserial():
    try:
        import serial  # pyserial
    except ImportError:
        raise SystemExit(
            "This mode needs pyserial. Install it with:  pip install pyserial"
        ) from None
    return serial


def r307_scans(port_name: str, baud: int):
    """Yield the slot number of each finger matched by an R307-style module."""
    serial = _require_pyserial()

    with serial.Serial(port_name, baud, timeout=1) as port:
        print(f"Listening to {port_name} at {baud} baud. Ctrl+C to stop.")
        while True:
            captured = _ask(port, bytes([_CMD_GEN_IMAGE]))
            if not captured or captured[0] != _ACK_OK:
                # No finger on the sensor is the normal state, not an error.
                time.sleep(0.2)
                continue

            converted = _ask(port, bytes([_CMD_IMAGE_TO_TZ, 0x01]))
            if not converted or converted[0] != _ACK_OK:
                print("  a finger was seen but could not be read - try again")
                time.sleep(1.0)
                continue

            # Search the whole library (slots 0 to 0xFFFF) using buffer 1.
            found = _ask(port, bytes([_CMD_SEARCH, 0x01, 0x00, 0x00, 0xFF, 0xFF]))
            if not found:
                continue
            if found[0] in (_ACK_NO_MATCH, _ACK_NOT_FOUND):
                print("  finger not recognised by the reader")
                time.sleep(1.5)
                continue
            if found[0] != _ACK_OK or len(found) < 5:
                continue

            slot = int.from_bytes(found[1:3], "big")
            score = int.from_bytes(found[3:5], "big")
            print(f"reader matched slot {slot} (score {score})")
            yield slot

            # Wait for the finger to come off, so one press is one clock event.
            while True:
                still_there = _ask(port, bytes([_CMD_GEN_IMAGE]))
                if not still_there or still_there[0] == _ACK_NO_FINGER:
                    break
                time.sleep(0.2)


# --- readers that simply print the slot number -------------------------------
def serial_line_scans(port_name: str, baud: int):
    """Yield slot numbers from a reader that prints them as lines of text."""
    serial = _require_pyserial()

    with serial.Serial(port_name, baud, timeout=1) as port:
        print(f"Listening to {port_name} at {baud} baud. Ctrl+C to stop.")
        while True:
            line = port.readline().decode("utf-8", "replace").strip()
            if not line:
                continue
            digits = "".join(c for c in line if c.isdigit())
            if digits:
                yield int(digits)
            else:
                print(f"  ignored unreadable line: {line!r}")


def simulated_scans():
    """Yield slot numbers typed at the keyboard - for testing with no reader."""
    print("Simulating a reader. Type a slot number and press Enter (Ctrl+C to stop).")
    while True:
        try:
            line = input("slot> ").strip()
        except EOFError:
            return
        digits = "".join(c for c in line if c.isdigit())
        if digits:
            yield int(digits)
        elif line:
            print("  numbers only, please")


# --- entry point --------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--simulate", action="store_true", help="Type slot numbers by hand."
    )
    source.add_argument(
        "--serial", metavar="PORT", help="Reader that prints the slot number."
    )
    source.add_argument(
        "--r307", metavar="PORT", help="R307 / ZFM-20 module over serial."
    )
    parser.add_argument(
        "--baud", type=int, default=57600, help="Serial baud rate (default 57600)."
    )
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
    args = parser.parse_args()

    try:  # .env is how the rest of the app is configured
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass

    token = args.token or os.getenv("KIOSK_TOKEN", "")
    if not token:
        raise SystemExit(
            "No kiosk token. Set KIOSK_TOKEN in .env, or pass --token.\n"
            "It must match the KIOSK_TOKEN the app is running with."
        )

    if args.simulate:
        scans = simulated_scans()
    elif args.serial:
        scans = serial_line_scans(args.serial, args.baud)
    else:
        scans = r307_scans(args.r307, args.baud)

    print(f"Posting to {args.url} as reader {args.device_label!r}.\n")
    try:
        for finger_id in scans:
            report(finger_id, post_scan(args.url, token, finger_id, args.device_label))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
