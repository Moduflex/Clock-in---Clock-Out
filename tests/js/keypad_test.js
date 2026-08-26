/* Drives the payroll-number keypad in kiosk.js under Node.
 *
 * The question being answered: can somebody clock by typing their number with a
 * finger, a mouse or a keyboard, and does adding that box break the two keyboard
 * bindings the kiosk already had?
 *
 * That last part is the reason this file exists. The kiosk treats Space and
 * Enter as "scan", so a cheap USB footswitch wired as a keyboard works as the
 * trigger. Drop a text box onto the same screen and the obvious implementation
 * breaks it: focus is left in the box, the footswitch is pressed, and a space
 * lands in the payroll number instead of a scan happening. Nobody would report
 * that as a keypad bug - they would report that the footswitch had stopped
 * working, which is much harder to trace.
 */
const fs = require("fs");
const path = require("path");

const APP = path.resolve(__dirname, "..", "..", "app", "static", "js");

// --- DOM with real enough events -------------------------------------------
/* The shared kiosk harness fires click handlers with no event object at all.
 * The keypad grid reads event.target.getAttribute("data-key") and its keyboard
 * handling turns on which element the event came from, so this needs a stub
 * that carries a target and honours stopPropagation. */
const els = {};

function mkEl(id) {
  return {
    id,
    textContent: "",
    innerHTML: "",
    className: "",
    value: "",
    hidden: false,
    disabled: false,
    style: {},
    dataset: {},
    _attrs: {},
    _handlers: {},
    addEventListener(ev, fn) {
      (this._handlers[ev] = this._handlers[ev] || []).push(fn);
    },
    getAttribute(name) {
      return name in this._attrs ? this._attrs[name] : null;
    },
    setAttribute(name, val) {
      this._attrs[name] = val;
    },
    focus() {
      global.document.activeElement = this;
    },
    blur() {
      if (global.document.activeElement === this) {
        global.document.activeElement = null;
      }
    },
    srcObject: null,
    videoWidth: 640,
    videoHeight: 480,
    play: () => Promise.resolve(),
    getContext: () => ({
      drawImage() {},
      getImageData: (x, y, w, h) => ({ data: new Uint8ClampedArray(w * h * 4) }),
    }),
    toDataURL: () => "data:image/jpeg;base64,AAAA",
  };
}

[
  "kiosk-video", "kiosk-hint", "scan-btn", "scan-in", "scan-out", "cancel-btn",
  "kiosk-result", "result-name", "result-action", "result-time", "result-detail",
  "onsite", "kiosk-clock", "kiosk-date", "kiosk-mode", "kiosk-debug",
  "keypad", "keypad-input", "keypad-keys", "keypad-go",
].forEach((id) => (els[id] = mkEl(id)));

const documentHandlers = {};
global.document = {
  activeElement: null,
  getElementById: (id) => els[id] || (els[id] = mkEl(id)),
  createElement: () => mkEl("canvas"),
  addEventListener(ev, fn) {
    (documentHandlers[ev] = documentHandlers[ev] || []).push(fn);
  },
};

/* Dispatch to the element's own handlers, then bubble to the document unless
 * something called stopPropagation - which is exactly the mechanism the box
 * relies on to keep its keys away from the scan binding. */
function press(key, target) {
  let propagate = true;
  let defaultPrevented = false;
  const event = {
    key,
    code: key === " " ? "Space" : key === "Enter" ? "Enter" : "Key" + key,
    target: target || null,
    ctrlKey: false,
    altKey: false,
    preventDefault() {
      defaultPrevented = true;
    },
    stopPropagation() {
      propagate = false;
    },
  };
  if (target) {
    (target._handlers.keydown || []).forEach((fn) => fn(event));
  }
  if (propagate) {
    (documentHandlers.keydown || []).forEach((fn) => fn(event));
  }
  return { defaultPrevented, reachedDocument: propagate };
}

/* A tap and a mouse click are the same event here, which is the point: the grid
 * is wired with click, so both work and neither needs its own code path. */
function tap(el, target) {
  (el._handlers.click || []).forEach((fn) => fn({ target: target || el }));
}

function typeInto(el, text) {
  el.value += text;
  (el._handlers.input || []).forEach((fn) => fn({ target: el }));
}

function keyEl(dataKey) {
  const el = mkEl("key-" + dataKey);
  el.setAttribute("data-key", dataKey);
  return el;
}

// --- timers, network, environment ------------------------------------------
let now = 0;
let seq = 0;
const timers = new Map();
global.setTimeout = (fn, ms) => {
  const id = ++seq;
  timers.set(id, { fn, at: now + (ms || 0), every: 0 });
  return id;
};
global.setInterval = (fn, ms) => {
  const id = ++seq;
  timers.set(id, { fn, at: now + (ms || 0), every: ms || 1 });
  return id;
};
global.clearTimeout = (id) => timers.delete(id);
global.clearInterval = global.clearTimeout;

const flush = async () => {
  for (let i = 0; i < 12; i++) await new Promise((r) => process.nextTick(r));
};

/* Step time forward in 10ms slices, flushing microtasks so promise chains that
 * wait on a timer actually progress. The frame gaps in a camera capture are
 * timers, so the footswitch check below needs this to reach a scan. */
async function advance(ms) {
  const target = now + ms;
  while (now < target) {
    now += Math.min(10, target - now);
    const due = [...timers.entries()].filter(([, t]) => t.at <= now).sort((a, b) => a[1].at - b[1].at);
    for (const [id, t] of due) {
      if (t.every) t.at = now + t.every;
      else timers.delete(id);
      t.fn();
      await flush();
    }
    await flush();
  }
}

const calls = [];
let payrollReply = null;
global.fetch = (url, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  calls.push({ url, body });
  let payload = { ok: true, count: 0 };
  if (url.includes("payroll")) {
    payload = payrollReply;
  }
  return Promise.resolve({ json: () => Promise.resolve(payload) });
};

Object.defineProperty(globalThis, "navigator", {
  value: { mediaDevices: { getUserMedia: () => Promise.resolve({ getTracks: () => [] }) } },
  writable: true,
  configurable: true,
});
global.window = global;
const RealDate = Date;
global.Date = class extends RealDate {
  constructor(...a) {
    super(...(a.length ? a : [now]));
  }
  static now() {
    return now;
  }
};

/* Hands-free off, so nothing but the keypad is moving. */
global.KIOSK_CONFIG = {
  token: "tok",
  scanUrl: "/api/kiosk/scan",
  identifyUrl: "/api/kiosk/identify",
  commitUrl: "/api/kiosk/commit",
  onsiteUrl: "/api/kiosk/onsite",
  payrollUrl: "/api/kiosk/payroll",
  frames: 3,
  autoMode: false,
  keypadMode: true,
  confirmSeconds: 2,
  pollMs: 600,
  presenceMs: 200,
  presenceThreshold: 7.0,
  autoFrames: 2,
  frameGapMs: 300,
  captureMaxWidth: 960,
  requireDeparture: true,
  departureMs: 900,
  rearmSeconds: 30,
  latchedPollMs: 1500,
  idlePollMs: 4000,
  debug: false,
};

eval(fs.readFileSync(path.join(APP, "capture.js"), "utf8"));
eval(fs.readFileSync(path.join(APP, "kiosk.js"), "utf8"));

// --- helpers ----------------------------------------------------------------
const input = els["keypad-input"];
const grid = els["keypad-keys"];
const go = els["keypad-go"];
const nameEl = () => els["result-name"].textContent;

const posts = (fragment) => calls.filter((c) => c.url.includes(fragment));
const lastPayroll = () => {
  const sent = posts("payroll");
  return sent.length ? sent[sent.length - 1].body : null;
};

const results = [];
function check(label, ok, extra) {
  results.push({ label, ok: !!ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${extra ? "   [" + extra + "]" : ""}`);
}

const RECOGNISED = {
  ok: true,
  code: "recorded",
  recorded: true,
  direction: "in",
  employee: { id: 7, name: "Sam Fletcher", first_name: "Sam" },
  occurred_at: "07:31:02",
  occurred_on: "Monday 19 August 2026",
};
const UNKNOWN = {
  ok: false,
  code: "ref_unknown",
  message: "That payroll number is not recognised. Please see the office.",
};

(async () => {
  payrollReply = RECOGNISED;
  /* Let the camera finish starting. On the real page it has resolved long
   * before anybody walks up and types, and its resolve calls showIdle() - which
   * would otherwise wipe a result out from under the checks below. */
  await advance(200);

  // 1. Nothing typed: the Clock button is not offerable, but the keys are
  //    there to be seen. A camera that has just failed is exactly when somebody
  //    needs to spot the way round it.
  check("empty box leaves Clock disabled", go.disabled === true);
  check("keys are on show before anybody touches anything", grid.hidden === false);

  // 2. Touch / mouse: the on-screen keys build the number.
  input.focus();
  tap(grid, keyEl("0"));
  tap(grid, keyEl("4"));
  tap(grid, keyEl("2"));
  check("tapping keys builds the number", input.value === "042", `value="${input.value}"`);
  check("Clock becomes available", go.disabled === false);

  // 3. Correcting a mistake.
  tap(grid, keyEl("9"));
  tap(grid, keyEl("back"));
  check("delete removes the last digit only", input.value === "042", `value="${input.value}"`);

  // 4. Tapping Clock sends it.
  tap(go);
  await flush();
  check("tapping Clock posts the number", lastPayroll() && lastPayroll().payroll_ref === "042",
        JSON.stringify(lastPayroll()));
  check("a recognised number shows the name", nameEl() === "Sam Fletcher", `name="${nameEl()}"`);
  check("a recognised number clears the box for the next person", input.value === "");
  check("keys stay on show after a clock", grid.hidden === false);

  // 5. Keyboard: type into the box and press Enter.
  const before = posts("payroll").length;
  input.focus();
  typeInto(input, "E77");
  const entered = press("Enter", input);
  await flush();
  check("Enter in the box posts the number",
        posts("payroll").length === before + 1 && lastPayroll().payroll_ref === "E77",
        JSON.stringify(lastPayroll()));
  check("Enter in the box does not also fire a camera scan",
        posts("scan").length === 0 && entered.reachedDocument === false);

  // 6. Clear wipes it.
  input.focus();
  typeInto(input, "123");
  tap(grid, keyEl("clear"));
  check("Clear empties the box", input.value === "" && go.disabled === true);

  // 7. A number that is not recognised stays put, so one digit can be fixed.
  payrollReply = UNKNOWN;
  input.focus();
  typeInto(input, "999");
  tap(go);
  await flush();
  check("an unrecognised number is left on screen to correct", input.value === "999",
        `value="${input.value}"`);
  check("and says so", nameEl() === "Not recorded", `name="${nameEl()}"`);
  payrollReply = RECOGNISED;
  tap(grid, keyEl("clear"));

  // 8. The USB number-pad path: a digit with nothing focused adopts the box.
  input.blur();
  press("7", null);
  press("3", null);
  check("a digit typed with nothing focused lands in the box", input.value === "73",
        `value="${input.value}"`);

  // 9. The footswitch must survive. Space with focus in the box has to reach
  //    the scan handler and must not put a space in a payroll number.
  input.focus();
  input.value = "042";
  const scansBefore = posts("scan").length;
  const space = press(" ", input);
  await advance(2000);
  check("Space in the box does not become part of the number", input.value === "042",
        `value="${input.value}"`);
  check("Space in the box still reaches the footswitch scan",
        space.reachedDocument === true && posts("scan").length === scansBefore + 1,
        `scans=${posts("scan").length}`);

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
})();
