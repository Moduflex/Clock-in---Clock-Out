/* Add/edit popups for the back office, on the native <dialog> element.

   The browser supplies the backdrop, Escape-to-close and the focus trap, so
   there is no modal library here to keep patched. The form inside a dialog is
   an ordinary server-posted form: validation, errors and the name-clash check
   all still happen on the server, and a rejected submission comes back with
   the dialog re-opened and the fields as they were typed.

   Elements this looks for:
     dialog.mf-dialog                 a dialog, opened by its id
       data-open="true"               open it as soon as the page loads
       data-return-to="/some/url"     closing it goes there, however it closed
     [data-dialog-open="dialog-id"]   a button that opens that dialog
     [data-dialog-close]              a button inside a dialog that closes it
*/
(function () {
    "use strict";

    function show(dialog) {
        if (typeof dialog.showModal === "function") {
            dialog.showModal();
        } else {
            // A browser with <dialog> but no showModal: no backdrop, but the
            // form is still reachable, which matters more than the dimming.
            dialog.setAttribute("open", "");
        }
    }

    function wire(dialog) {
        // An edit dialog is reached at its own URL (/shifts/3/edit). Closing it
        // any way at all - Cancel, the cross, Escape - returns to the plain
        // page, so the address bar never disagrees with what is on screen. Left
        // as is, the next "Add" click would re-open the form still pointed at
        // record 3 and quietly overwrite it.
        var returnTo = dialog.dataset.returnTo;
        if (returnTo) {
            dialog.addEventListener("close", function () {
                window.location.href = returnTo;
            });
        }
    }

    document.addEventListener("DOMContentLoaded", function () {
        var dialogs = {};

        Array.prototype.forEach.call(
            document.querySelectorAll("dialog.mf-dialog"),
            function (dialog) {
                dialogs[dialog.id] = dialog;
                wire(dialog);
                if (dialog.dataset.open === "true") {
                    show(dialog);
                }
            }
        );

        Array.prototype.forEach.call(
            document.querySelectorAll("[data-dialog-open]"),
            function (button) {
                button.addEventListener("click", function () {
                    var dialog = dialogs[button.dataset.dialogOpen];
                    if (dialog) {
                        show(dialog);
                    }
                });
            }
        );

        Array.prototype.forEach.call(
            document.querySelectorAll("[data-dialog-close]"),
            function (button) {
                button.addEventListener("click", function () {
                    var dialog = button.closest("dialog");
                    if (dialog) {
                        dialog.close();
                    }
                });
            }
        );
    });
})();
