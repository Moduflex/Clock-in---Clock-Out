"""Vendor SDK bindings for desktop USB fingerprint readers.

This is the only file that knows anything about a particular reader. Everything
else - enrolment, storage, matching policy, clocking - goes through the small
interface in :mod:`app.services.fingerprint`.

**Read this before wiring up hardware.**

The ZKTeco binding below is written against the documented ZKFinger SDK entry
points, but no reader was available to test it on. Treat it as a first draft to
verify, not as known-good code: check the DLL name, the calling convention and
the template buffer sizes against the SDK version you actually download. The
``--selftest`` mode of ``scripts/usb_fingerprint.py`` exercises each call in
order and prints what came back, which is the quickest way to confirm it.

What matters, and what must not be improvised: *comparison is the SDK's job*.
A home-made fingerprint comparison would be exactly the kind of bug that pays
somebody for a shift they did not work, so :meth:`compare` always delegates.

Installing the ZKTeco SDK
-------------------------
1. Download the ZKFinger / ZKFinger Reader SDK for Windows from ZKTeco (it ships
   with the ZK9500 and is also on their developer site).
2. Install it, then either put the SDK's ``lib`` directory on PATH or set
   ``FINGERPRINT_SDK_PATH`` in ``.env`` to the folder holding ``libzkfp.dll``.
3. Set ``FINGERPRINT_DRIVER=zkfinger`` in ``.env``.
4. Run ``python scripts/usb_fingerprint.py --selftest``.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

from .fingerprint import FingerprintError

# The SDK hands back a template of a few hundred bytes; this is the buffer we
# offer it. The model column is LargeBinary(4096), so keep the two in step.
MAX_TEMPLATE = 2048
# A 500 dpi image from a ZK9500 is roughly 300x400. Generous, and only ever a
# scratch buffer - the image is never stored.
MAX_IMAGE = 640 * 480


def _sdk_directory() -> Path | None:
    raw = os.getenv("FINGERPRINT_SDK_PATH", "").strip()
    return Path(raw) if raw else None


def _load_library(candidates: list[str]) -> ctypes.CDLL:
    """Load the first SDK library that turns up, with a useful error if none do."""
    directory = _sdk_directory()
    tried: list[str] = []

    if directory and hasattr(os, "add_dll_directory") and directory.is_dir():
        # Lets the SDK find its own dependent DLLs, which it will not do from
        # PATH alone on current Windows.
        os.add_dll_directory(str(directory))

    for name in candidates:
        for path in ([directory / name] if directory else []) + [Path(name)]:
            tried.append(str(path))
            try:
                return ctypes.WinDLL(str(path))
            except OSError:
                continue

    raise FingerprintError(
        "sdk_missing",
        "The fingerprint SDK library could not be loaded. Set FINGERPRINT_SDK_PATH "
        "in .env to the folder containing it. Tried: " + ", ".join(tried),
    )


class ZKFingerDriver:
    """ZKTeco ZK9500 and relatives, through the ZKFinger SDK.

    Two handles are involved and both matter: a *device* handle for capturing,
    and a *database* handle which is what performs template comparison. The
    second is easy to overlook - without it there is no matching.
    """

    name = "zkfinger"

    def __init__(self) -> None:
        if sys.platform != "win32":  # pragma: no cover - Windows-only SDK
            raise FingerprintError(
                "sdk_missing", "The ZKFinger SDK is only available for Windows."
            )
        self._lib = _load_library(["libzkfp.dll", "zkfp.dll"])
        self._device = None
        self._db = None
        self._declare()
        self._open()

    # -- binding -----------------------------------------------------------
    def _declare(self) -> None:
        lib = self._lib
        lib.ZKFPM_Init.restype = ctypes.c_int
        lib.ZKFPM_Terminate.restype = ctypes.c_int
        lib.ZKFPM_GetDeviceCount.restype = ctypes.c_int
        lib.ZKFPM_OpenDevice.argtypes = [ctypes.c_int]
        lib.ZKFPM_OpenDevice.restype = ctypes.c_void_p
        lib.ZKFPM_CloseDevice.argtypes = [ctypes.c_void_p]
        lib.ZKFPM_CloseDevice.restype = ctypes.c_int
        lib.ZKFPM_AcquireFingerprint.argtypes = [
            ctypes.c_void_p,               # device handle
            ctypes.POINTER(ctypes.c_ubyte),  # image buffer
            ctypes.c_uint,                 # image buffer size
            ctypes.POINTER(ctypes.c_ubyte),  # template buffer
            ctypes.POINTER(ctypes.c_uint),  # template size, in and out
        ]
        lib.ZKFPM_AcquireFingerprint.restype = ctypes.c_int
        lib.ZKFPM_DBInit.restype = ctypes.c_void_p
        lib.ZKFPM_DBFree.argtypes = [ctypes.c_void_p]
        lib.ZKFPM_DBFree.restype = ctypes.c_int
        lib.ZKFPM_DBMatch.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_uint,
        ]
        # Returns a similarity score, not a status code: above zero is a score.
        lib.ZKFPM_DBMatch.restype = ctypes.c_int

    def _open(self) -> None:
        if self._lib.ZKFPM_Init() != 0:
            raise FingerprintError("sdk_init", "The fingerprint SDK failed to start.")
        count = self._lib.ZKFPM_GetDeviceCount()
        if count <= 0:
            self._lib.ZKFPM_Terminate()
            raise FingerprintError(
                "no_reader",
                "No fingerprint reader was found. Check it is plugged in and that "
                "its driver installed cleanly in Device Manager.",
            )
        self._device = self._lib.ZKFPM_OpenDevice(0)
        if not self._device:
            self._lib.ZKFPM_Terminate()
            raise FingerprintError("no_reader", "The fingerprint reader would not open.")
        self._db = self._lib.ZKFPM_DBInit()
        if not self._db:
            self.close()
            raise FingerprintError(
                "sdk_init", "The SDK's matching database would not start."
            )

    # -- the driver interface ---------------------------------------------
    def capture(self, timeout: float = 15.0) -> tuple[bytes, float | None]:
        """Poll for a finger until *timeout*, then give up cleanly.

        The SDK's acquire call returns immediately when no finger is present, so
        the wait is a poll here rather than a blocking call - which also keeps
        Ctrl+C working.
        """
        import time

        image = (ctypes.c_ubyte * MAX_IMAGE)()
        template = (ctypes.c_ubyte * MAX_TEMPLATE)()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            size = ctypes.c_uint(MAX_TEMPLATE)
            code = self._lib.ZKFPM_AcquireFingerprint(
                self._device, image, MAX_IMAGE, template, ctypes.byref(size)
            )
            if code == 0 and size.value:
                return bytes(template[: size.value]), None
            time.sleep(0.15)

        raise FingerprintError("no_finger", "No finger was presented.")

    def compare(self, probe: bytes, candidate: bytes) -> float:
        """Score two templates, 0.0 to 1.0, using the SDK's own comparison."""
        if not probe or not candidate:
            return 0.0
        left = (ctypes.c_ubyte * len(probe)).from_buffer_copy(probe)
        right = (ctypes.c_ubyte * len(candidate)).from_buffer_copy(candidate)
        score = self._lib.ZKFPM_DBMatch(
            self._db, left, len(probe), right, len(candidate)
        )
        if score <= 0:
            return 0.0
        # ZKFinger scores run to about 100; clamp so a threshold of 0.6 means
        # the same thing whatever the SDK returns at the top end.
        return min(1.0, score / 100.0)

    def close(self) -> None:
        if self._db:
            self._lib.ZKFPM_DBFree(self._db)
            self._db = None
        if self._device:
            self._lib.ZKFPM_CloseDevice(self._device)
            self._device = None
        try:
            self._lib.ZKFPM_Terminate()
        except Exception:  # noqa: BLE001 - shutting down regardless
            pass


class DigitalPersonaDriver:
    """DigitalPersona U.are.U - not yet wired up.

    The modern U.are.U SDK is reached through .NET or COM rather than a flat C
    DLL, so binding it is a different job from ZKFinger and guessing at it would
    be worse than useless. If this is the reader you have, send me the SDK's
    documentation or header files and it is a small piece of work to complete.
    """

    name = "digitalpersona"

    def __init__(self) -> None:
        raise FingerprintError(
            "sdk_missing",
            "The DigitalPersona binding is not implemented yet. Use "
            "FINGERPRINT_DRIVER=zkfinger with a ZKTeco reader, or supply the "
            "U.are.U SDK documentation so it can be wired up.",
        )


def load_sdk_driver(name: str):
    """Build the named vendor driver."""
    if name == "zkfinger":
        return ZKFingerDriver()
    if name == "digitalpersona":
        return DigitalPersonaDriver()
    raise FingerprintError("unknown_driver", f"No fingerprint driver called {name!r}.")
