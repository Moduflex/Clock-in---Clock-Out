/* Drives dialogs.js against a stub DOM: the add/edit popups on Shifts and hours.
 *
 * The forms inside these dialogs are ordinary server-posted forms, so the value
 * of testing here is the open/close wiring rather than the saving. In particular
 * the one behaviour that would corrupt data if it broke: an edit dialog lives at
 * its own URL, and every way of closing it - Cancel, the cross, Escape, the
 * backdrop - has to leave that URL, or the next "Add a shift" click re-opens a
 * form still aimed at the record that was being edited and overwrites it.
 */
const fs = require("fs");
const path = require("path");

const SCRIPT = path.resolve(__dirname, "..", "..", "app", "static", "js", "dialogs.js");

let failures = 0;
let checks = 0;

function check(name, actual, expected) {
  checks += 1;
  if (actual !== expected) {
    failures += 1;
    console.log(`FAIL ${name}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  } else {
    console.log(`ok   ${name}`);
  }
}

/* --- a stub DOM, only as much as dialogs.js touches ---------------------- */
function makeElement(tag, attrs) {
  const element = {
    tagName: tag.toUpperCase(),
    dataset: Object.assign({}, attrs || {}),
    parent: null,
    children: [],
    listeners: {},
    open: false,
    modal: false,
    attributes: {},
    addEventListener(type, handler) {
      (this.listeners[type] = this.listeners[type] || []).push(handler);
    },
    fire(type) {
      (this.listeners[type] || []).forEach((handler) => handler({ type }));
    },
    setAttribute(name, value) {
      this.attributes[name] = value;
      if (name === "open") this.open = true;
    },
    closest(selector) {
      // Only "dialog" is ever asked for.
      let node = this;
      while (node && node.tagName.toLowerCase() !== selector) node = node.parent;
      return node;
    },
    click() {
      this.fire("click");
    },
  };
  if (tag === "dialog") {
    element.showModal = function () {
      this.open = true;
      this.modal = true;
    };
    element.close = function () {
      this.open = false;
      this.modal = false;
      this.fire("close");
    };
  }
  return element;
}

let navigatedTo = null;

function buildPage(options) {
  const shiftDialog = makeElement("dialog", options.shift || {});
  shiftDialog.id = "shift-dialog";
  const weekDialog = makeElement("dialog", options.week || {});
  weekDialog.id = "week-dialog";

  const addShift = makeElement("button", { dialogOpen: "shift-dialog" });
  const addWeek = makeElement("button", { dialogOpen: "week-dialog" });

  const cancelShift = makeElement("button", { dialogClose: "" });
  cancelShift.parent = shiftDialog;
  const cancelWeek = makeElement("button", { dialogClose: "" });
  cancelWeek.parent = weekDialog;

  const bySelector = {
    "dialog.mf-dialog": [shiftDialog, weekDialog],
    "[data-dialog-open]": [addShift, addWeek],
    "[data-dialog-close]": [cancelShift, cancelWeek],
  };

  const loadHandlers = [];
  navigatedTo = null;

  global.document = {
    querySelectorAll: (selector) => bySelector[selector] || [],
    addEventListener(type, handler) {
      if (type === "DOMContentLoaded") loadHandlers.push(handler);
    },
  };
  global.window = {};
  // location.href is assigned to, so it needs a setter to observe.
  Object.defineProperty(global.window, "location", {
    value: Object.defineProperty({}, "href", {
      set(value) {
        navigatedTo = value;
      },
      get() {
        return navigatedTo;
      },
    }),
    writable: true,
  });

  // Fresh evaluation each time, so nothing leaks between scenarios.
  delete require.cache[SCRIPT];
  new Function(fs.readFileSync(SCRIPT, "utf8"))();
  loadHandlers.forEach((handler) => handler());

  return { shiftDialog, weekDialog, addShift, addWeek, cancelShift, cancelWeek };
}

/* --- 1. a plain page load leaves both popups shut ----------------------- */
let page = buildPage({});
check("shift popup starts closed", page.shiftDialog.open, false);
check("week popup starts closed", page.weekDialog.open, false);

/* --- 2. the buttons open their own popup, modally ----------------------- */
page.addShift.click();
check("Add a shift opens the shift popup", page.shiftDialog.open, true);
check("...as a modal", page.shiftDialog.modal, true);
check("...and not the week one", page.weekDialog.open, false);

page.addWeek.click();
check("Add a standard week opens the week popup", page.weekDialog.open, true);

/* --- 3. Cancel closes, and stays on the page ---------------------------- */
page.cancelShift.click();
check("Cancel closes the shift popup", page.shiftDialog.open, false);
check("Cancel does not navigate", navigatedTo, null);

/* --- 4. data-open opens it as the page loads ---------------------------- */
page = buildPage({ shift: { open: "true", returnTo: "/admin/shifts" } });
check("an editing page opens its popup on load", page.shiftDialog.open, true);
check("the other popup stays shut", page.weekDialog.open, false);

/* --- 5. every way of closing an edit popup leaves the edit URL ---------- */
page.cancelShift.click();
check("Cancel on an edit popup returns to the list", navigatedTo, "/admin/shifts");

page = buildPage({ shift: { open: "true", returnTo: "/admin/shifts" } });
page.shiftDialog.close(); // what Escape and the backdrop both do
check("Escape on an edit popup returns to the list", navigatedTo, "/admin/shifts");

/* --- 6. an add popup has no return URL, so closing it stays put --------- */
page = buildPage({ week: { open: "true" } });
page.weekDialog.close();
check("closing an add popup does not navigate", navigatedTo, null);

/* --- 7. a browser without showModal still reaches the form -------------- */
page = buildPage({});
delete page.shiftDialog.showModal;
page.addShift.click();
check("falls back to the open attribute", page.shiftDialog.attributes.open, "");
check("...so the form is reachable", page.shiftDialog.open, true);

console.log(`\n${checks - failures}/${checks} checks passed`);
process.exit(failures === 0 ? 0 : 1);
