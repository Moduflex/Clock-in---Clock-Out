/* Office dashboard: a ticking clock, a periodic reload, and the fire register.
 *
 * The clock is drawn in the browser so the minute changes on screen without a
 * round trip; the reload is what actually refreshes the figures. Both are
 * deliberately plain - this page is left open all day on an office machine and
 * anything clever here is something to maintain later.
 */
(function () {
    "use strict";

    var script = document.currentScript ||
        document.querySelector('script[src*="dashboard.js"]');

    function tick() {
        var clock = document.getElementById("mf-clock");
        if (!clock) {
            return;
        }
        var now = new Date();
        clock.textContent =
            String(now.getHours()).padStart(2, "0") + ":" +
            String(now.getMinutes()).padStart(2, "0");
    }

    /* The server renders the time in the site's timezone, which is the one that
       matters. Only take over the clock when this machine agrees with it, so a
       laptop set to another zone shows the site's time rather than its own. */
    function browserAgreesWithServer() {
        var clock = document.getElementById("mf-clock");
        if (!clock) {
            return false;
        }
        var now = new Date();
        var here = String(now.getHours()).padStart(2, "0") + ":" +
            String(now.getMinutes()).padStart(2, "0");
        var served = clock.textContent.trim();
        // Within a minute of each other: the page may have been rendered just
        // before the minute rolled over.
        return Math.abs(toMinutes(here) - toMinutes(served)) <= 1;
    }

    function toMinutes(text) {
        var parts = text.split(":");
        return parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10);
    }

    if (browserAgreesWithServer()) {
        window.setInterval(tick, 1000);
    }

    var seconds = parseInt(script && script.dataset.refreshSeconds, 10);
    if (seconds > 0) {
        window.setTimeout(function () {
            window.location.reload();
        }, seconds * 1000);
    }

    var print = document.getElementById("mf-print-register");
    if (print) {
        print.addEventListener("click", function () {
            window.print();
        });
    }
}());
