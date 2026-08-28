/**
 * Exercises sds200-card.js's screen-wake logic (_acquireScreenLock and
 * friends) against a stubbed DOM. Run with any Node:
 *
 *     node tests/test_card_screen_lock.js
 *
 * Deliberately NOT part of `python3 -m unittest discover` -- it's the only
 * JavaScript test here and needs a JS runtime the rest of the suite doesn't.
 *
 * Why it exists: this code's whole point is behaviour on an Android phone
 * running the Home Assistant app, which is the one environment that can't
 * be reached from a dev box. The branch that matters most there (no
 * navigator.wakeLock, fall through to the video) is unreachable in a normal
 * desktop browser too, since desktop browsers have the API. Stubbing is the
 * only way to run these paths at all -- what it can't tell you is whether
 * the video trick actually holds the screen on in the WebView, which only
 * the phone (and the card's own status line) can answer.
 *
 * No dependencies beyond Node itself, matching the rest of the repo.
 */

const assert = require("assert");
const path = require("path");

// ---- minimal DOM ----------------------------------------------------------
const listeners = {};
class FakeEl {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.style = {};
    this.paused = true;
    this.srcObject = undefined;
  }
  appendChild(c) { this.children.push(c); return c; }
  remove() {}
  removeAttribute() {}
  load() {}
  pause() { this.paused = true; }
  addEventListener() {}
  removeEventListener() {}
  querySelectorAll() { return []; }
  getContext() {
    return { fillRect() {}, set fillStyle(v) {}, get fillStyle() { return "#000"; } };
  }
  captureStream(fps) {
    this.captureFps = fps;
    const tracks = [{ stopped: false, stop() { this.stopped = true; } }];
    return { getTracks: () => tracks };
  }
}

global.MutationObserver = class { observe() {} };
global.HTMLElement = class {};
global.Element = { prototype: {} };
global.customElements = { define() {} };
global.document = {
  visibilityState: "visible",
  body: new FakeEl("body"),
  createElement: (tag) => new FakeEl(tag),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
  removeEventListener: (type, fn) => {
    listeners[type] = (listeners[type] || []).filter((f) => f !== fn);
  },
};
global.navigator = {};
global.window = global;
global.console.warn = () => {}; // expected on the refusal paths; keep output clean

// Loaded as text and evaluated, rather than require()d: the file is a
// browser module that runs top-level registration code (customElements,
// MutationObserver, patching Element.prototype.attachShadow) against the
// stubs above, and has no exports of its own.
const cardPath = path.join(__dirname, "..", "custom_components", "sds200", "www", "sds200-card.js");
const src = require("fs").readFileSync(cardPath, "utf8");
const SDS200Card = new Function(`${src}; return SDS200Card;`)();

// ---- test scaffolding -----------------------------------------------------
function makeCard({ videoPlays = true } = {}) {
  const card = Object.create(SDS200Card.prototype);
  const video = new FakeEl("video");
  video.play = videoPlays
    ? async () => { video.paused = false; }
    : async () => { throw new Error("NotAllowedError"); };
  const els = { "#keep-awake": video, "#screen-status": new FakeEl("div") };
  els["#screen-status"].textContent = "";
  card.querySelector = (sel) => els[sel] || null;
  card._els = els;
  return card;
}
const session = () => ({ stopped: false, startedAt: Date.now() });
const status = (card) => card._els["#screen-status"].textContent;
const fireVisibility = () => (listeners.visibilitychange || []).forEach((f) => f());

const tests = {
  async "wake lock is used when available"() {
    const card = makeCard();
    const s = session();
    card._audioSession = s;
    let released = false;
    navigator.wakeLock = { request: async () => ({ release: async () => { released = true; } }) };
    await card._acquireScreenLock(s);
    assert.ok(s.wakeLock, "sentinel must be kept -- it is the only way to release");
    assert.match(status(card), /wake lock/);
    assert.ok(!s.keepAwake, "must not also start the video fallback");
    card._releaseScreenLock(s);
    await new Promise((r) => setImmediate(r));
    assert.ok(released, "stopping playback must release the lock");
    assert.strictEqual(s.wakeLock, null);
  },

  async "falls back to video when the API is missing (the WebView case)"() {
    const card = makeCard();
    const s = session();
    card._audioSession = s;
    delete navigator.wakeLock;
    await card._acquireScreenLock(s);
    assert.ok(s.keepAwake, "video fallback must engage");
    assert.strictEqual(s.keepAwake.video.paused, false, "video must actually be playing");
    assert.match(status(card), /video/);
    const track = s.keepAwake.stream.getTracks()[0];
    card._releaseScreenLock(s);
    assert.ok(track.stopped, "capture track must be stopped");
    assert.strictEqual(s.keepAwake, null);
  },

  async "falls back to video when the API refuses (hidden/low battery)"() {
    const card = makeCard();
    const s = session();
    card._audioSession = s;
    navigator.wakeLock = { request: async () => { throw new Error("NotAllowedError"); } };
    await card._acquireScreenLock(s);
    assert.ok(s.keepAwake, "a refused wake lock must not be the end of it");
  },

  async "points at the app setting when neither works"() {
    const card = makeCard({ videoPlays: false });
    const s = session();
    card._audioSession = s;
    delete navigator.wakeLock;
    await card._acquireScreenLock(s);
    assert.ok(!s.keepAwake);
    assert.match(status(card), /Keep screen on/);
  },

  async "a lock granted after Stop is released, not leaked"() {
    const card = makeCard();
    const s = session();
    card._audioSession = s;
    let released = false;
    navigator.wakeLock = {
      request: async () => {
        s.stopped = true;               // user hits Stop mid-request
        card._audioSession = null;
        return { release: async () => { released = true; } };
      },
    };
    await card._acquireScreenLock(s);
    await new Promise((r) => setImmediate(r));
    assert.ok(released, "a lock nobody holds a handle to keeps the screen on forever");
    assert.ok(!s.wakeLock);
  },

  async "re-acquires the lock when the page becomes visible again"() {
    const card = makeCard();
    const s = session();
    card._audioSession = s;
    let requests = 0;
    navigator.wakeLock = {
      request: async () => { requests++; return { released: false, release: async () => {} }; },
    };
    card._startScreenLockWatch();
    await card._acquireScreenLock(s);
    assert.strictEqual(requests, 1);

    // The platform released it on hide; coming back must re-request.
    s.wakeLock.released = true;
    fireVisibility();
    await new Promise((r) => setImmediate(r));
    assert.strictEqual(requests, 2, "a released lock must be re-requested on return");

    // Still-held lock: no duplicate request.
    s.wakeLock.released = false;
    fireVisibility();
    await new Promise((r) => setImmediate(r));
    assert.strictEqual(requests, 2);
  },

  async "resumes the fallback video when the page becomes visible again"() {
    const card = makeCard();
    const s = session();
    card._audioSession = s;
    delete navigator.wakeLock;
    card._startScreenLockWatch();
    await card._acquireScreenLock(s);
    s.keepAwake.video.pause(); // what the platform does on hide
    fireVisibility();
    await new Promise((r) => setImmediate(r));
    assert.strictEqual(s.keepAwake.video.paused, false, "video must resume on return");
  },

  async "does nothing on visibility change with no session"() {
    const card = makeCard();
    card._audioSession = null;
    navigator.wakeLock = { request: async () => assert.fail("must not request a lock") };
    card._startScreenLockWatch();
    fireVisibility();
    await new Promise((r) => setImmediate(r));
  },
};

(async () => {
  let failed = 0;
  for (const [name, fn] of Object.entries(tests)) {
    for (const k of Object.keys(listeners)) delete listeners[k];
    try {
      await fn();
      process.stdout.write(`ok   ${name}\n`);
    } catch (err) {
      failed++;
      process.stdout.write(`FAIL ${name}\n     ${err.message}\n`);
    }
  }
  process.stdout.write(failed ? `\n${failed} failed\n` : `\nall ${Object.keys(tests).length} passed\n`);
  process.exit(failed ? 1 : 0);
})();
