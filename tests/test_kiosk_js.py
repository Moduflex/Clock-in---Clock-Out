"""Runs the kiosk JavaScript state machine under Node.

The hands-free countdown lives in browser code, so the Python suite cannot reach
it - and that code holds the client half of the "do not clock people out as they
walk past" guarantee. ``tests/js/kiosk_harness.js`` stubs the DOM, the camera and
the network, then drives the real ``kiosk.js`` with fake timers and asserts that:

* an empty doorway produces no requests at all;
* nothing is committed while the countdown is running;
* letting the countdown finish commits exactly once;
* **Cancel prevents the commit**;
* an already-clocked or unrecognised person never commits;
* a person who never leaves the frame still clocks again;
* the kiosk re-arms when the *server* reports no face, even if the browser's
  presence check insists somebody is there;
* somebody the presence check cannot see is still clocked by the idle poll;
* the next person in a queue is served without the scene emptying;
* a second person clocks while the first person's result is still on screen.

``tests/js/dialogs_test.js`` covers the back-office add/edit popups, where the
failure that matters is an edit dialog that closes without leaving its URL - the
next "Add" click would then overwrite the record instead of adding one.

There is also ``tests/js/presence_test.js``, which drives the presence detector
through awkward departures (lingering at the edge of view, lighting drift, a brief
pass-through) to check its background model does not learn a person as scenery.

This caught a real bug: the recognition poll timer was not stopped when a
countdown began, so once the screen returned to idle the stale poll kept calling
/identify with nobody there and started a fresh countdown that then committed.

Skipped when Node is not installed - it is a development convenience, not a
runtime dependency of the application.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "js" / "kiosk_harness.js"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


@needs_node
def test_kiosk_state_machine():
    result = subprocess.run(
        [NODE, str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=HARNESS.parent.parent.parent,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"kiosk.js harness reported failures:\n{output}"
    assert "checks passed" in output
    assert "FAIL" not in output, output


@needs_node
def test_admin_dialogs():
    """The add/edit popups on the Shifts and hours page.

    Worth covering because one failure mode there is silent data loss: an edit
    popup lives at its own URL, and if closing it did not leave that URL the
    next "Add" click would re-open the form still aimed at the record being
    edited, and saving would overwrite it instead of adding a new one.
    """
    harness = HARNESS.parent / "dialogs_test.js"
    result = subprocess.run(
        [NODE, str(harness)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=HARNESS.parent.parent.parent,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"dialogs.js harness reported failures:\n{output}"
    assert "checks passed" in output
    assert "FAIL" not in output, output


@needs_node
@pytest.mark.parametrize("script", ["kiosk.js", "capture.js", "enrol.js", "dialogs.js"])
def test_browser_scripts_parse(script):
    """A syntax error in kiosk JavaScript breaks clocking with no server error."""
    path = HARNESS.parent.parent.parent / "app" / "static" / "js" / script
    result = subprocess.run(
        [NODE, "--check", str(path)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


@needs_node
def test_immediate_reclock_default():
    """Covers the shipped default, where any face is clocked without leaving.

    Also pins the stuck-result regression: the revert to "Ready" required a state
    the kiosk had already moved on from, so the panel stayed up indefinitely.
    """
    harness = HARNESS.parent / "immediate_test.js"
    result = subprocess.run(
        [NODE, str(harness)], capture_output=True, text=True, timeout=120
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"immediate-reclock harness failed:\n{output}"
    assert "FAIL" not in output, output


@needs_node
def test_presence_detector():
    """The background model must not learn a lingering person as empty scenery."""
    harness = HARNESS.parent / "presence_test.js"
    result = subprocess.run(
        [NODE, str(harness)], capture_output=True, text=True, timeout=120
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"presence detector harness failed:\n{output}"
    assert "FAIL" not in output, output
