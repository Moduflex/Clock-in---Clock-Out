/* Kiosk screen behaviour.
 *
 * Hands-free clocking is a small state machine:
 *
 *   IDLE ---- somebody arrives ----> LOOKING ---- recognised ----> CONFIRMING
 *     ^                                 |                              |
 *     |                            not recognised                  countdown
 *     |                                 |                        ends / cancelled
 *     +------------- RESULT <-----------+<-----------------------------+
 *
 * The CONFIRMING pause is a safety feature, not decoration. Without it, walking
 * past the camera two hours into a shift would clock you out; with it, the
 * screen says "Clocking OUT in 2..." and the person can cancel or simply walk
 * away. Nothing is written to the database until the countdown finishes.
 *
 * The buttons remain available throughout, and take priority over the automatic
 * path - useful for the person who really is leaving straight after arriving.
 *
 * The payroll-number keypad is a third path that touches none of the above. It
 * recognises nobody and needs no camera, which is the point: it is what somebody
 * uses when the camera cannot see them. It is wired up outside the camera
 * start-up promise for the same reason - if the camera fails, the keypad has to
 * be the thing that still works.
 */
(function () {
    "use strict";

    var config = window.KIOSK_CONFIG;
    var video = document.getElementById("kiosk-video");
    var hint = document.getElementById("kiosk-hint");
    var scanBtn = document.getElementById("scan-btn");
    var scanIn = document.getElementById("scan-in");
    var scanOut = document.getElementById("scan-out");
    var cancelBtn = document.getElementById("cancel-btn");
    var resultBox = document.getElementById("kiosk-result");
    var nameEl = document.getElementById("result-name");
    var actionEl = document.getElementById("result-action");
    var timeEl = document.getElementById("result-time");
    var detailEl = document.getElementById("result-detail");
    var onsiteEl = document.getElementById("onsite");
    var clockEl = document.getElementById("kiosk-clock");
    var dateEl = document.getElementById("kiosk-date");
    var modeEl = document.getElementById("kiosk-mode");
    var debugEl = document.getElementById("kiosk-debug");
    var keypadInput = document.getElementById("keypad-input");
    var keypadKeys = document.getElementById("keypad-keys");
    var keypadGo = document.getElementById("keypad-go");

    var capture = new window.FaceCapture(video, { maxWidth: config.captureMaxWidth });
    var presence = new window.PresenceDetector(capture, {
        threshold: config.presenceThreshold
    });

    /* RESULT is deliberately NOT a state. Showing somebody their result must not
     * stop the kiosk scanning, or the next person in a queue waits for the screen
     * to clear before they can clock. A result is a display with an expiry. */
    var STATE = { IDLE: "idle", LOOKING: "looking", CONFIRMING: "confirming" };
    var state = STATE.IDLE;
    var busy = false;              /* a request is in flight */
    var pending = null;            /* { token, employee, direction } */
    var countdownTimer = null;
    var resultTimer = null;
    var presenceTimer = null;
    var lookTimer = null;
    var backoffUntil = 0;          /* set when the server rate-limits us */
    var lastPersonId = null;       /* suppress immediate re-scan of one person */
    var lastPersonAt = 0;
    /* Set after any resolved automatic outcome. While it is true the kiosk will
     * not offer another automatic entry, however long somebody stands there. It
     * clears only once the scene has read empty for AUTO_DEPARTURE_MS.
     *
     * This is what makes the kiosk a toggle. Gating on elapsed time instead
     * either blocks genuine clocking out (a long interval) or clocks a
     * stationary person in and straight back out (a short one). Gating on
     * absence matches what people actually expect: you are clocked when you
     * arrive, and not clocked again until you have been away. */
    /* Who must leave before *they* can be clocked again. Per person on purpose:
     * "Sam has just clocked, so do not clock Sam again yet" must not be read as
     * "nobody may clock", which is what stalled a queue. */
    var awaitingDeparture = false;
    var latchedPersonId = null;
    var resultUntil = 0;
    var lastLatchedPollAt = 0;
    var absentSince = 0;
    var latchedAt = 0;
    var latchedPollTimer = null;
    var idlePollTimer = null;
    var noFaceRuns = 0;
    var lastMotion = null;

    var RESULT_SECONDS = 6;
    /* With departure gating off (the default) this is the ONLY thing stopping two
     * overlapping polls of one sighting producing two entries. It must stay at
     * least as long as a capture plus the countdown, or the same face gets
     * clocked twice for a single approach. */
    var REPEAT_SUPPRESS_MS = 2000;
    /* Hands-free capture is deliberately leaner than a button press: two frames
     * instead of three. Capture time dominates how long somebody waits for their
     * name to appear, far more than the recognition itself does. */
    var AUTO_FRAMES = config.autoFrames || 2;
    var FRAME_GAP_MS = config.frameGapMs || 300;
    var REQUIRE_DEPARTURE = config.requireDeparture !== false;
    var DEPARTURE_MS = config.departureMs || 900;
    /* Backstop for the case where the camera can never see an empty scene - a
     * kiosk facing a desk, or a doorway that is never clear. Without it,
     * departure gating fails closed: clocks once, then nothing, with no
     * explanation. 0 disables the fallback. */
    var REARM_MS = (config.rearmSeconds === 0 ? 0 : (config.rearmSeconds || 30)) * 1000;
    var LATCHED_POLL_MS = config.latchedPollMs || 1500;
    /* How long after a clock the kiosk keeps polling at full rate, to serve the
     * next person in a queue without delay. */
    var QUEUE_WINDOW_MS = (RESULT_SECONDS + 2) * 1000;
    var IDLE_POLL_MS = config.idlePollMs === 0 ? 0 : (config.idlePollMs || 4000);
    /* Consecutive "no face" answers from the server needed to call it a
     * departure. Two, so a single blurred frame as somebody turns away does not
     * count as them having left. */
    var NO_FACE_TO_REARM = 2;

    var missStreak = 0;
    var MISS_HINT_AFTER = 3;
    var lastRearmReason = "start";

    /* Turn a refusal code into something the person can act on. */
    function missHint(code) {
        if (code === "face_too_small") {
            return "Please come a little closer to the camera";
        }
        if (code === "face_too_blurred") {
            return "Hold still — the image is blurred";
        }
        if (code === "multiple_faces") {
            return "One at a time, please — step up to the camera";
        }
        if (code && code.indexOf("liveness_") === 0) {
            return "Look at the camera — move your head slightly";
        }
        if (code === "not_recognised") {
            return "Not recognised — try the Scan button, or see the office";
        }
        if (code === "no_templates" || code === "models_missing") {
            return "Face recognition is not set up — please see the office";
        }
        return "Face the camera, or use the buttons";
    }

    /* --- Diagnostics (?debug=1) ------------------------------------------ */
    var lastScore = 0;
    var lastCode = "-";

    function paintDebug() {
        if (!config.debug || !debugEl) {
            return;
        }
        var latch = awaitingDeparture
            ? "LATCHED (" +
              (REARM_MS
                  ? Math.max(0, Math.ceil((REARM_MS - (Date.now() - latchedAt)) / 1000)) +
                    "s to auto re-arm"
                  : "departure only") +
              ")"
            : "armed";
        debugEl.textContent = [
            "state        " + state,
            "presence     " +
                lastScore.toFixed(2) +
                "  (threshold " +
                config.presenceThreshold +
                " -> " +
                (lastScore >= config.presenceThreshold ? "SOMEBODY THERE" : "empty") +
                ")",
            "clocking     " +
                latch +
                (latchedPersonId === null ? "" : " person#" + latchedPersonId) +
                "  [last re-arm: " + lastRearmReason + "]",
            "last reply   " +
                lastCode +
                (lastMotion === null ? "" : "   (motion " + lastMotion + ")"),
            "no-face runs " + noFaceRuns + " / " + NO_FACE_TO_REARM,
            "misses       " + missStreak,
            "busy         " + busy
        ].join("\n");
    }

    /* --- Wall clock ----------------------------------------------------- */
    function tickClock() {
        var now = new Date();
        clockEl.textContent = now.toLocaleTimeString("en-GB", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit"
        });
        dateEl.textContent = now.toLocaleDateString("en-GB", {
            weekday: "long",
            day: "numeric",
            month: "long",
            year: "numeric"
        });
    }

    /* --- Result panel --------------------------------------------------- */
    function setResult(kind, name, action, time, detail) {
        resultBox.className = "mf-kiosk-result" + (kind ? " is-" + kind : "");
        nameEl.textContent = name || "";
        actionEl.innerHTML = action || "";
        timeEl.textContent = time || "";
        detailEl.textContent = detail || "";
    }

    function showIdle() {
        state = STATE.IDLE;
        pending = null;
        cancelBtn.hidden = true;
        resultUntil = 0;
        missStreak = 0;
        setResult(
            "",
            "Ready",
            config.autoMode
                ? "Step up to the camera"
                : "Press <strong>Scan</strong> to clock in or out",
            "",
            ""
        );
        hint.textContent = config.autoMode
            ? "Clocking happens automatically — just look at the camera"
            : "Stand square to the camera and press Scan";
    }

    /* Show an outcome for a while. Scanning continues underneath, so a different
     * person stepping up is recognised straight away and their countdown simply
     * replaces what is on screen. */
    function showResultFor(seconds, personId) {
        state = STATE.IDLE;
        pending = null;
        cancelBtn.hidden = true;
        resultUntil = Date.now() + (seconds || RESULT_SECONDS) * 1000;

        if (REQUIRE_DEPARTURE && personId !== null && personId !== undefined) {
            awaitingDeparture = true;
            latchedPersonId = personId;
            absentSince = 0;
            latchedAt = Date.now();
            noFaceRuns = 0;
        }

        clearTimer("countdown");
        clearTimer("result");
        resultTimer = window.setTimeout(function () {
            /* Fall back to Ready unless something newer has taken the screen.
             *
             * This deliberately does NOT require state === IDLE: scanning resumes
             * immediately after a result, so the state is normally LOOKING by the
             * time this fires. Checking for IDLE meant the revert never ran and
             * the result panel stayed up for ever - the "it is stuck" report. */
            if (state !== STATE.CONFIRMING && Date.now() >= resultUntil) {
                showIdle();
            }
        }, (seconds || RESULT_SECONDS) * 1000);
    }

    function resultOnScreen() {
        return Date.now() < resultUntil;
    }

    function clearTimer(which) {
        if (which === "countdown" && countdownTimer) {
            window.clearInterval(countdownTimer);
            countdownTimer = null;
        }
        if (which === "result" && resultTimer) {
            window.clearTimeout(resultTimer);
            resultTimer = null;
        }
    }

    /* --- On-site counter ------------------------------------------------ */
    function refreshOnsite() {
        window
            .fetch(config.onsiteUrl, {
                headers: { "X-Kiosk-Token": config.token },
                credentials: "same-origin"
            })
            .then(function (response) {
                return response.json();
            })
            .then(function (data) {
                if (data && data.ok) {
                    onsiteEl.textContent =
                        data.count === 1 ? "1 person on site" : data.count + " people on site";
                }
            })
            .catch(function () {
                /* A failed counter refresh must never disturb clocking. */
            });
    }

    /* --- Button-press clocking ------------------------------------------ */
    function setButtonsBusy(isBusy) {
        scanBtn.disabled = isBusy;
        scanIn.disabled = isBusy;
        scanOut.disabled = isBusy;
        scanBtn.textContent = isBusy ? "Scanning…" : "Scan";
    }

    function manualScan(direction) {
        if (busy) {
            return;
        }
        /* A deliberate press wins over anything the automatic path is doing. */
        abandonPending();
        busy = true;
        setButtonsBusy(true);
        setResult("", "Hold still…", "Looking at the camera", "", "");

        capture
            .grabSeries(config.frames, 340)
            .then(function (frames) {
                if (!frames.length) {
                    throw new Error("The camera did not return an image.");
                }
                return window.postJson(
                    config.scanUrl,
                    { frames: frames, direction: direction || null },
                    { "X-Kiosk-Token": config.token }
                );
            })
            .then(handleRecorded)
            .catch(function (error) {
                setResult("error", "Problem", error.message || "Please try again", "", "");
                showResultFor(6);
            })
            .then(function () {
                busy = false;
                setButtonsBusy(false);
            });
    }

    /* --- Shared: render a recorded (or duplicate) entry ------------------ */
    function handleRecorded(data) {
        if (!data.ok) {
            if (data.code === "rate_limited") {
                backoffUntil = Date.now() + 15000;
            }
            if (data.code === "already_used") {
                /* A double submit of one confirmation. Nothing was recorded twice;
                 * there is nothing useful to show, so go quiet. */
                showIdle();
                return;
            }
            setResult("error", "Not recorded", data.message || "Please try again", "", "");
            showResultFor(6);
            return;
        }

        var verb = data.direction === "in" ? "Clocked IN" : "Clocked OUT";
        if (data.recorded === false) {
            setResult(
                "warning",
                data.employee.name,
                "Already " + verb.toLowerCase(),
                data.occurred_at,
                "No new entry recorded."
            );
        } else {
            setResult("success", data.employee.name, verb, data.occurred_at, data.occurred_on);
        }
        if (data.employee) {
            lastPersonId = data.employee.id;
            lastPersonAt = Date.now();
        }
        refreshOnsite();
        showResultFor(RESULT_SECONDS, data.employee ? data.employee.id : null);
    }

    /* --- Hands-free: look for a known face ------------------------------ */
    function lookForFace() {
        /* Only an in-progress countdown blocks scanning. Showing a result does
         * not, which is what keeps a queue moving. */
        if (busy || state === STATE.CONFIRMING) {
            return;
        }
        if (Date.now() < backoffUntil) {
            return;
        }
        /* Polling rate while latched.
         *
         * A queue forms in the seconds right after somebody clocks, so for that
         * window we keep the full rate - throttling here is what made the next
         * person wait. Somebody merely lingering in view stays for minutes, so
         * after the window we drop to the gentler rate and stop burning requests
         * on them. */
        if (awaitingDeparture && Date.now() - latchedAt > QUEUE_WINDOW_MS) {
            if (Date.now() - lastLatchedPollAt < LATCHED_POLL_MS) {
                return;
            }
            lastLatchedPollAt = Date.now();
        }
        busy = true;

        capture
            .grabSeries(AUTO_FRAMES, FRAME_GAP_MS)
            .then(function (frames) {
                if (!frames.length) {
                    return null;
                }
                return window.postJson(
                    config.identifyUrl,
                    { frames: frames },
                    { "X-Kiosk-Token": config.token }
                );
            })
            .then(function (data) {
                if (!data) {
                    return;
                }
                lastCode = data.code || (data.ok ? "ok" : "?");
                if (typeof data.motion === "number") {
                    lastMotion = data.motion;
                }
                if (!data.ok) {
                    if (data.code === "rate_limited") {
                        /* Stop hammering; the kiosk is polling too fast. */
                        backoffUntil = Date.now() + 15000;
                        return;
                    }
                    if (data.code === "no_face") {
                        /* The server is the reliable judge of whether anybody is
                         * in front of the camera, so this is how a departure gets
                         * confirmed even when the presence check disagrees. */
                        if (awaitingDeparture) {
                            noFaceRuns += 1;
                            if (noFaceRuns >= NO_FACE_TO_REARM) {
                                rearm("no face");
                            }
                        }
                        return;
                    }
                    /* A single miss is normal while somebody walks up, so the
                     * screen stays quiet rather than flickering. But staying
                     * quiet for ever is worse than a flicker: somebody the
                     * recogniser keeps refusing would be left watching a screen
                     * that appears to be doing nothing. After a few consecutive
                     * misses, say what would help. */
                    missStreak += 1;
                    if (missStreak >= MISS_HINT_AFTER) {
                        hint.textContent = missHint(data.code);
                    }
                    return;
                }
                missStreak = 0;

                noFaceRuns = 0;

                if (data.code === "pending" && data.confirm_token) {
                    /* The latched person must leave before being clocked again. */
                    if (awaitingDeparture && data.employee.id === latchedPersonId) {
                        if (!resultOnScreen()) {
                            hint.textContent = "All set - step away from the camera";
                        }
                        return;
                    }
                    /* Anybody else is served straight away. At a shift change the
                     * scene never empties between two people, so waiting for that
                     * would leave the queue stuck behind whoever clocked first. */
                    if (awaitingDeparture) {
                        rearm("different person");
                    }
                    if (
                        data.employee.id === lastPersonId &&
                        Date.now() - lastPersonAt < REPEAT_SUPPRESS_MS
                    ) {
                        return; /* two polls around the same sighting */
                    }
                    beginCountdown(data);
                }
            })
            .catch(function () {
                /* Network hiccup: try again on the next tick. */
            })
            .then(function () {
                busy = false;
            });
    }

    /* --- Hands-free: the cancellable countdown -------------------------- */
    function beginCountdown(data) {
        state = STATE.CONFIRMING;
        /* One countdown at a time: stop looking for faces until it resolves. */
        stopLooking();
        pending = {
            token: data.confirm_token,
            employee: data.employee,
            direction: data.direction
        };

        var remaining = typeof data.confirm_seconds === "number" ? data.confirm_seconds : 4;
        var verb = data.direction === "in" ? "Clocking IN" : "Clocking OUT";
        cancelBtn.hidden = false;

        function paint() {
            setResult(
                data.direction === "in" ? "success" : "warning",
                data.employee.name,
                verb,
                remaining > 0 ? String(remaining) : "",
                remaining > 0 ? "Press Cancel if this is not right" : "Recording…"
            );
        }

        if (remaining <= 0) {
            paint();
            commitPending();
            return;
        }

        paint();
        clearTimer("countdown");
        countdownTimer = window.setInterval(function () {
            remaining -= 1;
            if (remaining <= 0) {
                clearTimer("countdown");
                paint();
                commitPending();
                return;
            }
            paint();
        }, 1000);
    }

    function commitPending() {
        if (!pending || busy) {
            return;
        }
        var token = pending.token;
        busy = true;

        window
            .postJson(
                config.commitUrl,
                { confirm_token: token },
                { "X-Kiosk-Token": config.token }
            )
            .then(handleRecorded)
            .catch(function (error) {
                setResult("error", "Problem", error.message || "Please try again", "", "");
                showResultFor(6);
            })
            .then(function () {
                busy = false;
            });
    }

    function abandonPending() {
        clearTimer("countdown");
        pending = null;
        cancelBtn.hidden = true;
    }

    function cancelPending() {
        if (state !== STATE.CONFIRMING) {
            return;
        }
        var name = pending && pending.employee ? pending.employee.first_name : "";
        /* Remember who was cancelled. Without this they count as "somebody new"
         * to the latched watcher, which re-arms and clocks them anyway. */
        if (pending && pending.employee) {
            lastPersonId = pending.employee.id;
            lastPersonAt = Date.now();
        }
        abandonPending();
        setResult("", "Cancelled", name ? "Nothing recorded, " + name + "." : "Nothing recorded.", "", "");
        /* Suppress this person briefly so the countdown does not restart at once. */
        showResultFor(3);
    }

    /* --- Hands-free: the presence loop ---------------------------------- */
    function watchForArrivals() {
        /* Measure every tick, even mid-countdown: that is how a departure gets
         * noticed promptly rather than only once the screen returns to idle. */
        var score = presence.measure();
        lastScore = score;
        var somebodyThere = score >= config.presenceThreshold;
        paintDebug();

        if (!somebodyThere) {
            if (!absentSince) {
                absentSince = Date.now();
            } else if (awaitingDeparture && Date.now() - absentSince >= DEPARTURE_MS) {
                /* They have gone. The next arrival is a fresh clocking, which is
                 * what turns "clocked in" into "clocked out" next time. */
                rearm("departed");
            }
        } else {
            absentSince = 0;
        }

        /* Never stay latched for ever. If the camera cannot tell us the scene is
         * empty, time gets us moving again rather than failing silently. */
        if (awaitingDeparture && REARM_MS > 0 && Date.now() - latchedAt >= REARM_MS) {
            rearm("timeout");
        }

        if (state === STATE.CONFIRMING) {
            return;
        }

        if (somebodyThere && state === STATE.IDLE) {
            state = STATE.LOOKING;
            /* Do not stamp over a result somebody is still reading, and do not
             * nag the latched person - they are simply waiting to walk away. */
            if (!resultOnScreen() && !awaitingDeparture) {
                setResult("", "Hold still…", "Checking who you are", "", "");
                hint.textContent = "Look at the camera";
            }
            lookForFace();
            if (!lookTimer) {
                lookTimer = window.setInterval(lookForFace, config.pollMs);
            }
        } else if (!somebodyThere && state === STATE.LOOKING) {
            /* Nobody in view: stop polling until somebody arrives. */
            state = STATE.IDLE;
            stopLooking();
            if (!resultOnScreen()) {
                showIdle();
            }
        }
    }

    /* Allow automatic clocking again. */
    function rearm(why) {
        awaitingDeparture = false;
        latchedPersonId = null;
        lastPersonId = null;
        lastRearmReason = why;
        noFaceRuns = 0;
    }

    /* Slow safety poll, independent of the presence check.
     *
     * The grey-difference check can miss somebody standing still in dark
     * clothing, or at an awkward angle, or if the threshold is set too high for
     * the room. Previously that meant they were never clocked and nothing on
     * screen said why. This asks the server directly every few seconds, so the
     * presence check only ever makes clocking *faster*, never impossible. */
    function startIdlePoll() {
        if (idlePollTimer || IDLE_POLL_MS <= 0) {
            return;
        }
        idlePollTimer = window.setInterval(function () {
            if (busy || state === STATE.CONFIRMING || Date.now() < backoffUntil) {
                return;
            }
            /* Only needed when presence thinks the scene is empty; if it can see
             * somebody, the normal fast path is already running. */
            if (lastScore >= config.presenceThreshold) {
                return;
            }
            lookForFace();
        }, IDLE_POLL_MS);
    }

    function stopLooking() {
        if (lookTimer) {
            window.clearInterval(lookTimer);
            lookTimer = null;
        }
    }

    /* --- Clocking by payroll number -------------------------------------- */
    /* Three ways in, all landing on the same value:
     *
     *   touch / mouse   the on-screen keys, and tapping the box itself
     *   keyboard        typing straight into the box, which is a real <input>
     *   number pad      a digit pressed with nothing focused adopts the box, so
     *                   a USB wedge clocks somebody without a finger going near
     *                   the screen
     *
     * The keys stay folded away until somebody starts, because a permanent 4x3
     * grid does not fit above the fold on a 720p kiosk screen.
     */
    var keypadBusy = false;

    function keypadPresent() {
        return Boolean(config.keypadMode && keypadInput);
    }

    function keypadValue() {
        return (keypadInput.value || "").trim();
    }

    function paintKeypad() {
        keypadGo.disabled = !keypadValue() || keypadBusy;
    }

    /* Unfold the keys and keep them out until the entry is finished with. They
     * deliberately do NOT fold away on blur: tapping a key blurs the box for an
     * instant, and a grid that flickered out from under a thumb would be worse
     * than one that stays put. */
    function openKeypad() {
        keypadKeys.hidden = false;
    }

    function resetKeypad() {
        keypadInput.value = "";
        keypadKeys.hidden = true;
        keypadInput.blur();
        paintKeypad();
    }

    function keypadType(digit) {
        if (keypadInput.value.length >= 32) {
            return;
        }
        keypadInput.value += digit;
        openKeypad();
        paintKeypad();
    }

    function keypadBackspace() {
        keypadInput.value = keypadInput.value.slice(0, -1);
        paintKeypad();
    }

    function setKeypadBusy(isBusy) {
        keypadBusy = isBusy;
        keypadInput.disabled = isBusy;
        keypadGo.textContent = isBusy ? "Clocking…" : "Clock";
        paintKeypad();
    }

    function submitKeypad() {
        var reference = keypadValue();
        if (!reference || keypadBusy || busy) {
            return;
        }
        /* A typed number is as deliberate as a button press, so it wins over
         * whatever the automatic path is in the middle of. */
        abandonPending();
        setKeypadBusy(true);
        setResult("", "Checking…", "Payroll number " + reference, "", "");

        window
            .postJson(
                config.payrollUrl,
                { payroll_ref: reference, direction: null },
                { "X-Kiosk-Token": config.token }
            )
            .then(function (data) {
                /* A number that was not recognised stays on screen, so a single
                 * mistyped digit can be corrected instead of retyped. */
                if (data && data.ok) {
                    resetKeypad();
                }
                handleRecorded(data);
            })
            .catch(function (error) {
                setResult("error", "Problem", error.message || "Please try again", "", "");
                showResultFor(6);
            })
            .then(function () {
                setKeypadBusy(false);
            });
    }

    /* True when this key belongs to the box rather than to the kiosk at large.
     * Space is the exception on purpose: a payroll number has no space in it, so
     * a space arriving here is the footswitch being pressed with focus left in
     * the box, and it has to reach the scan handler. */
    function keypadWantsKey(event) {
        return event.target === keypadInput && event.key !== " ";
    }

    function startKeypad() {
        keypadInput.addEventListener("input", paintKeypad);
        keypadInput.addEventListener("focus", openKeypad);

        keypadInput.addEventListener("keydown", function (event) {
            if (event.key === " ") {
                /* Swallow the character, but let the event carry on bubbling to
                 * the document handler so the footswitch still scans. */
                event.preventDefault();
                return;
            }
            event.stopPropagation();
            if (event.key === "Enter") {
                event.preventDefault();
                submitKeypad();
            } else if (event.key === "Escape") {
                event.preventDefault();
                resetKeypad();
            }
        });

        /* One handler for the whole grid rather than twelve. Focus returns to
         * the box after every key, so a mixed tap-then-type entry works. */
        keypadKeys.addEventListener("click", function (event) {
            var key = event.target.getAttribute("data-key");
            if (key === null || keypadBusy) {
                return;
            }
            if (key === "clear") {
                keypadInput.value = "";
                paintKeypad();
            } else if (key === "back") {
                keypadBackspace();
            } else {
                keypadType(key);
            }
            keypadInput.focus();
        });

        keypadGo.addEventListener("click", submitKeypad);
        paintKeypad();
    }

    /* --- Start up ------------------------------------------------------- */
    tickClock();
    window.setInterval(tickClock, 1000);

    if (keypadPresent()) {
        startKeypad();
    }

    scanBtn.addEventListener("click", function () {
        manualScan(null);
    });
    scanIn.addEventListener("click", function () {
        manualScan("in");
    });
    scanOut.addEventListener("click", function () {
        manualScan("out");
    });
    cancelBtn.addEventListener("click", cancelPending);

    /* Space or Enter triggers a scan, so a cheap USB footswitch wired as a
     * keyboard works as the trigger. Escape cancels a pending automatic entry. */
    document.addEventListener("keydown", function (event) {
        /* Never scan on a key meant for the payroll box. Its own handler stops
         * propagation, so this is the belt to that braces - and it lets a
         * footswitch press through, which keypadWantsKey() excludes. */
        if (keypadPresent() && keypadWantsKey(event)) {
            return;
        }
        if (event.code === "Escape") {
            event.preventDefault();
            cancelPending();
            return;
        }
        /* A digit typed with nothing focused adopts the payroll box. That is
         * what makes a USB number pad work as a clocking device: press 0-4-2,
         * press Enter, done, without anybody touching the screen. */
        if (keypadPresent() && /^[0-9]$/.test(event.key) && !event.ctrlKey && !event.altKey) {
            event.preventDefault();
            keypadInput.focus();
            keypadType(event.key);
            return;
        }
        if (event.code === "Space" || event.code === "Enter" || event.code === "NumpadEnter") {
            event.preventDefault();
            manualScan(null);
        }
    });

    capture
        .start()
        .then(function () {
            scanBtn.disabled = false;
            showIdle();
            refreshOnsite();
            window.setInterval(refreshOnsite, 60000);

            if (config.debug && debugEl) {
                debugEl.hidden = false;
                window.setInterval(paintDebug, 400);
            }

            if (config.autoMode) {
                modeEl.textContent = "Automatic";
                modeEl.className = "mf-badge mf-badge-in";
                /* Let the detector learn the empty scene before watching. */
                window.setTimeout(function () {
                    presence.reset();
                    presenceTimer = window.setInterval(watchForArrivals, config.presenceMs);
                    startIdlePoll();
                }, 1200);
            } else {
                modeEl.textContent = "Press to scan";
                modeEl.className = "mf-badge";
            }
        })
        .catch(function (error) {
            hint.textContent = error.message;
            setResult("error", "Camera unavailable", error.message, "", "");
        });
})();
