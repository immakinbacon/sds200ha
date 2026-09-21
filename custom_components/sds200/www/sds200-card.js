/**
 * SDS200 front-panel card.
 *
 * Has a visual config editor (pick "SDS200 Scanner" when adding a card from
 * the Lovelace UI) -- no need to hand-write YAML. Raw config, if you do:
 *   type: custom:sds200-card
 *   scanner_id: <the add-on's scanner id, e.g. "home">
 *
 * scanner_id must match what the add-on assigned (GET /scanners on the
 * add-on, or the sds200_<scanner_id>_* entity ids in Settings > Entities --
 * it's a slug of the scanner's configured name, see main.py's slugify()).
 * Deliberately NOT re-derived from a display name here: an earlier version
 * tried to slugify a friendly "device_name" in JS to match the add-on's
 * Python slugify(), and the two didn't actually agree for names with
 * consecutive punctuation (e.g. "Coast Guard - USA") -- two independent
 * implementations of the same algorithm drifting apart. Taking scanner_id
 * directly removes that whole class of bug.
 *
 * Shows what the scanner is hearing, with Listen, Hold and Avoid. The
 * mirrored display, the keypad, the soft/nav keys, the volume and squelch
 * steppers and the power-cycle button all used to be here; driving the
 * scanner is what the add-on's web UI is for, and a dashboard card that can
 * power-cycle hardware on a mis-tap earns its removal on its own. Hold and
 * Avoid come back because they act on whatever the scanner is on right now
 * -- the thing this card is already showing you -- and Avoid is a temporary
 * one, so neither can do lasting damage from a phone in a pocket.
 *
 * Styled as a Home Assistant card, not as a scanner: theme variables
 * throughout, no imitation of the unit's green-on-black face. The card that
 * did imitate it looked like a foreign object on every dashboard, and it was
 * imitating a display that no longer lives here anyway.
 *
 * Reads entities by convention: sensor.sds200_<scanner_id>_{channel,
 * department,system,frequency,mode,ctcss_dcs,rssi},
 * media_player.sds200_<scanner_id>_media_player -- entity.py sets these
 * explicitly (not left to HA's default name-based entity_id generation) so
 * this convention is guaranteed stable. The media_player is also where the
 * audio stream_url comes from.
 */

const DOMAIN = "sds200";

// Must match what audio_bridge.py's ffmpeg actually emits (fragmented
// MP4/AAC-LC). Checked with MediaSource.isTypeSupported() before use, since
// a wrong/unsupported string makes addSourceBuffer() throw.
const AUDIO_MIME = 'audio/mp4; codecs="mp4a.40.2"';
// How long to give the MediaSource path to reach an actually-playing state
// before giving up on it and falling back to the <iframe> navigation. The
// known failure (Firefox's service worker, see _startAudio) rejects the
// fetch() well inside this, so this is only the backstop for a stream that
// connects but never produces decodable audio.
const AUDIO_START_TIMEOUT_MS = 6000;
// Cap how much decoded audio the SourceBuffer holds. This is a live stream
// that runs for hours, and a SourceBuffer that's only ever appended to hits
// QuotaExceededError eventually -- much sooner on mobile, where the buffer
// budget is far smaller than on desktop.
//
// Deliberately NOT 30, which is what it was first written as: audio stopping
// after ~30 seconds is a symptom this project has already chased down twice
// (see docs/protocol-notes.md), and a client-side timer that first fires at
// exactly the reported failure time makes it impossible to tell whether the
// eviction caused the stop or merely coincided with it. At 120s the first
// eviction is nowhere near the symptom, so the next test result means
// something. Cheap either way: this stream is mono 8 kHz AAC, so even two
// minutes of it is a fraction of a megabyte.
const AUDIO_BUFFER_KEEP_S = 120;
// The sandboxed relay iframe's whole document (see _openRelayStream). It
// fetches and forwards bytes; it never plays anything, so it needs no
// autoplay permission and no media APIs. Kept deliberately tiny: it runs at
// an opaque origin where nothing else of this card's is reachable, and it's
// the one piece of code here that can't be debugged from the page.
const RELAY_HTML = `<!doctype html><meta charset="utf-8"><script>
(function () {
  var ac = null;
  function post(m) { parent.postMessage(m, "*"); }
  addEventListener("message", function (e) {
    var msg = e.data || {};
    if (msg.type === "start") start(msg.url);
    else if (msg.type === "stop" && ac) ac.abort();
  });
  function start(url) {
    ac = new AbortController();
    // credentials omitted: cross-origin from here, and the URL is signed.
    fetch(url, { signal: ac.signal, credentials: "omit" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        if (!r.body) throw new Error("no response body to read");
        var reader = r.body.getReader();
        return (function read() {
          return reader.read().then(function (res) {
            if (res.done) { post({ type: "end" }); return; }
            post({ type: "chunk", data: res.value });
            return read();
          });
        })();
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        post({ type: "error", message: (err && err.name) + ": " + (err && err.message) });
      });
  }
  post({ type: "ready" });
})();
<\/script>`;

// If playback falls further than this behind the newest buffered audio --
// which happens whenever a mobile browser suspends the page (screen off,
// app backgrounded) while bytes keep arriving -- skip forward instead of
// playing an ever-growing backlog of stale scanner traffic.
const AUDIO_MAX_LAG_S = 10;

// A scanner's device identifiers are [DOMAIN, scanner_id] (see entity.py's
// DeviceInfo) -- these two helpers convert between a device_id (what
// ha-selector's device picker returns) and our scanner_id (what the card
// actually needs), shared by the card and its config editor below.
function deviceIdForScannerId(hass, scannerId) {
  if (!scannerId || !hass?.devices) return undefined;
  for (const [deviceId, device] of Object.entries(hass.devices)) {
    if (device.identifiers?.some(([domain, id]) => domain === DOMAIN && id === scannerId)) {
      return deviceId;
    }
  }
  return undefined;
}

function scannerIdForDeviceId(hass, deviceId) {
  const device = hass?.devices?.[deviceId];
  const match = device?.identifiers?.find(([domain]) => domain === DOMAIN);
  return match?.[1];
}

class SDS200Card extends HTMLElement {
  static getConfigElement() {
    return document.createElement("sds200-card-editor");
  }

  static getStubConfig(hass) {
    const firstScannerId = Object.values(hass?.devices || {})
      .flatMap((device) => device.identifiers || [])
      .find(([domain]) => domain === DOMAIN)?.[1];
    return { scanner_id: firstScannerId || "" };
  }

  setConfig(config) {
    if (!config.scanner_id) {
      throw new Error("sds200-card: 'scanner_id' is required");
    }
    this._config = config;
    this._slug = config.scanner_id;
    this._cachedDeviceId = undefined; // a different scanner is a different device
    this._built = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) {
      this._build();
      this._built = true;
      this._startHassPolling();
      this._startScreenLockWatch();
    }
    this._render();
  }

  getCardSize() {
    // Roughly: the header, the label, a row of fields and the button row. It
    // was 6 when the card carried the whole front panel.
    return 4;
  }

  /**
   * When this element was created via the self-heal workaround (see below),
   * its parent (e.g. hui-vertical-stack-card) keeps its own cached
   * reference to the *original*, now-detached error card and keeps pushing
   * every hass update there instead of here -- forever, since that cache is
   * only ever populated once. Tried repointing that cached reference
   * directly (see git history); confirmed on a real install that it doesn't
   * reliably stick (parent-internal caching shape isn't consistent/known
   * enough to patch from outside). Sidesteps needing to understand or patch
   * whatever the parent does at all: independently poll the frontend's root
   * `<home-assistant>` element -- a single, stable, always-present light-DOM
   * element holding the master, always-current hass object -- and push it
   * in ourselves whenever it changes. Cheap (one reference check per tick),
   * and harmless even when this card *did* get wired into the normal
   * cascade correctly (the check just finds nothing to do most ticks).
   */
  _startHassPolling() {
    clearInterval(this._hassPollInterval); // setConfig() can re-trigger _build()
    this._hassPollInterval = setInterval(() => {
      const fresh = document.querySelector("home-assistant")?.hass;
      if (fresh && fresh !== this._hass) {
        this.hass = fresh;
      }
    }, 1000);
  }

  disconnectedCallback() {
    clearInterval(this._hassPollInterval);
    clearTimeout(this._actionStatusTimer);
    if (this._visibilityHandler) {
      document.removeEventListener("visibilitychange", this._visibilityHandler);
      this._visibilityHandler = null;
    }
    // Playback used to stop by itself here, because it lived entirely inside
    // an <iframe> that went away with the card's DOM. The MediaSource path
    // doesn't: its <audio> element and read loop are still referenced by
    // this._audioSession and keep playing/fetching after the card is
    // detached (switch dashboard views and the scanner follows you around,
    // with no button left to stop it).
    this._stopAudio();
  }

  _entityId(domain, suffix) {
    return `${domain}.sds200_${this._slug}_${suffix}`;
  }

  _build() {
    this.innerHTML = `
      <ha-card>
        <style>
          /* Every class here is prefixed "sds-" because this card renders
             into the light DOM (the audio machinery reaches its elements
             with this.querySelector, see _startAudio), and a <style> block
             in light DOM is document-wide -- an unprefixed .panel or .title
             would quietly restyle every other card on the dashboard.

             Colours are all theme variables, with a literal fallback for
             the few that older themes may not define. Nothing here is a
             hardcoded dark panel any more: the card used to imitate the
             scanner's own green-on-black face, which read as a foreign
             object on every dashboard it was put on, and the mirrored
             display it was imitating for is long gone (it lives in the
             add-on's web UI). This is a Home Assistant card that happens to
             be about a scanner. */
          .sds-body {
            padding: 16px;
          }
          .sds-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
          }
          .sds-name {
            font-size: 14px;
            font-weight: 500;
            color: var(--secondary-text-color);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
          }
          /* The logo as a mask rather than an <img>, so it paints in the
             theme's own text colour. The asset is black-filled: the old
             card inverted it to read on its black panel, which would have
             made it invisible the moment the card stopped being black. */
          .sds-brand {
            flex: none;
            width: 56px;
            height: 12px;
            opacity: 0.5;
            background-color: var(--secondary-text-color);
            -webkit-mask: url(/sds200_static/uniden-logo.svg) no-repeat center / contain;
            mask: url(/sds200_static/uniden-logo.svg) no-repeat center / contain;
          }
          /* What the scanner is hearing: the channel as the card's headline,
             with department and system under it. Same fields the add-on web
             UI shows for a collapsed scanner panel, and the same "only show
             a row that has a value" rule, so the two don't drift into
             showing different things about the same scanner. */
          .sds-now {
            margin-top: 12px;
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 12px;
          }
          .sds-title {
            font-size: 22px;
            line-height: 1.25;
            color: var(--primary-text-color);
            word-break: break-word;
          }
          .sds-sub {
            margin-top: 2px;
            font-size: 13px;
            color: var(--secondary-text-color);
            word-break: break-word;
          }
          .sds-sub:empty {
            display: none;
          }
          /* Squelch open: the one piece of state worth seeing from across a
             room. The old card said it with a green border round a black
             box; a chip that goes green and breathes says it without the
             card having to be black. */
          .sds-chip {
            flex: none;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 5px 10px;
            border-radius: 14px;
            font-size: 12px;
            white-space: nowrap;
            color: var(--secondary-text-color);
            background: var(--secondary-background-color);
          }
          .sds-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--disabled-text-color, #9e9e9e);
          }
          #sds-panel.receiving .sds-chip {
            color: var(--success-color, #4caf50);
            background: color-mix(in srgb, var(--success-color, #4caf50) 16%, transparent);
          }
          #sds-panel.receiving .sds-dot {
            background: var(--success-color, #4caf50);
            animation: sds-pulse 1.6s ease-in-out infinite;
          }
          @media (prefers-reduced-motion: reduce) {
            #sds-panel.receiving .sds-dot {
              animation: none;
            }
          }
          @keyframes sds-pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.35; }
          }
          .sds-fields {
            margin-top: 16px;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
            gap: 12px;
          }
          .sds-field {
            min-width: 0;
          }
          .sds-flabel {
            font-size: 11px;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            color: var(--secondary-text-color);
          }
          .sds-value {
            margin-top: 2px;
            font-size: 15px;
            color: var(--primary-text-color);
            font-variant-numeric: tabular-nums;
            overflow-wrap: anywhere;
          }
          .sds-empty {
            margin-top: 14px;
            font-size: 13px;
            color: var(--secondary-text-color);
          }
          /* Listen, Hold, Avoid. Sized for a thumb (44px) rather than for a
             mouse, since the dashboard this lives on is mostly a phone. */
          .sds-actions {
            margin-top: 16px;
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 8px;
          }
          .sds-btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            min-height: 44px;
            padding: 0 10px;
            border: none;
            border-radius: 12px;
            font-family: inherit;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            color: var(--primary-text-color);
            background: var(--secondary-background-color);
          }
          .sds-btn:hover {
            background: color-mix(in srgb, var(--primary-text-color) 8%, var(--secondary-background-color));
          }
          .sds-btn:active {
            transform: scale(0.98);
          }
          .sds-btn[disabled] {
            opacity: 0.5;
            cursor: default;
            transform: none;
          }
          .sds-btn ha-icon {
            --mdc-icon-size: 20px;
          }
          .sds-btn-primary {
            color: var(--text-primary-color, #fff);
            background: var(--primary-color);
          }
          .sds-btn-primary:hover {
            background: color-mix(in srgb, #000 8%, var(--primary-color));
          }
          #audio-toggle.playing {
            color: var(--text-primary-color, #fff);
            background: var(--error-color, #db4437);
          }
          #audio-toggle.playing:hover {
            background: color-mix(in srgb, #000 8%, var(--error-color, #db4437));
          }
          .sds-status {
            /* Playback failures here are mostly invisible otherwise: the
               Home Assistant Android app has no reachable JS console, so a
               console.warn() is unreportable on the one platform whose
               problems needed diagnosing. Keep whatever this says in sync
               with what's actually knowable -- it's the only diagnostic
               channel that survives the app. */
            margin-top: 6px;
            min-height: 13px;
            font-size: 11px;
            line-height: 1.3;
            color: var(--secondary-text-color);
            word-break: break-word;
          }
          .sds-status:empty {
            margin-top: 0;
            min-height: 0;
          }
          .keep-awake {
            /* The screen-wake fallback's <video> (see _startKeepAwakeVideo).
               Deliberately NOT zero-size, display:none, visibility:hidden or
               opacity:0, unlike .audio-frame-container below: the whole
               mechanism is Chromium's own "a video is playing, keep the
               screen on" behaviour, and Chromium decides that with an
               intersection/visibility check on the element. A video it
               considers invisible is exactly a video it won't hold the
               screen on for, so this has to stay technically visible.

               12px of near-transparent black in the corner of an already
               near-black panel is the compromise: big and opaque enough to
               count as on-screen, small and faint enough that nobody sees
               it. If the fallback ever stops working, suspect this block
               first -- shrinking it to 0 or hiding it "for tidiness" would
               silently disable the feature while looking harmless. */
            width: 12px;
            height: 12px;
            opacity: 0.02;
            pointer-events: none;
          }
          .audio-frame-container,
          .audio-frame-container iframe,
          .audio-frame-container audio {
            /* Holds whichever player _startAudio ended up with: the
               MediaSource-driven <audio> element, or the fallback <iframe>
               and its native controls.

               Zero-size, not display:none/visibility:hidden -- those can
               get an iframe deprioritized/suspended by the browser (no
               longer "visible"), which risks pausing the audio playing
               inside it. A zero-size box in normal flow still renders and
               keeps running, just contributes no visible footprint. */
            width: 0;
            height: 0;
            border: none;
            overflow: hidden;
          }
        </style>
        <div class="sds-body" id="sds-panel">
          <div class="sds-head">
            <div class="sds-name" id="sds-name"></div>
            <div class="sds-brand" role="img" aria-label="Uniden"></div>
          </div>
          <div class="sds-now">
            <div>
              <div class="sds-title" id="np-label">&mdash;</div>
              <div class="sds-sub" id="np-sub"></div>
            </div>
            <div class="sds-chip" id="sds-chip">
              <span class="sds-dot"></span><span id="sds-chip-text">Scanning</span>
            </div>
          </div>
          <div class="sds-fields" id="np-fields"></div>
          <div class="sds-actions">
            <button class="sds-btn sds-btn-primary" id="audio-toggle" type="button">
              <ha-icon id="audio-toggle-icon" icon="mdi:play"></ha-icon>
              <span id="audio-toggle-label">Listen</span>
            </button>
            <button class="sds-btn" id="hold-button" type="button" title="Hold on what the scanner is on now">
              <ha-icon icon="mdi:pause"></ha-icon><span>Hold</span>
            </button>
            <button class="sds-btn" id="avoid-button" type="button" title="Temporarily avoid what the scanner is on now">
              <ha-icon icon="mdi:cancel"></ha-icon><span>Avoid</span>
            </button>
          </div>
          <div class="sds-status" id="action-status"></div>
          <div class="sds-status" id="audio-status"></div>
          <div class="sds-status" id="screen-status"></div>
          <div class="audio-frame-container" id="audio-frame-container"></div>
          <video class="keep-awake" id="keep-awake" muted playsinline disableremoteplayback></video>
        </div>
      </ha-card>
    `;

    this.querySelector("#audio-toggle").addEventListener("click", () => this._toggleAudio());
    this.querySelector("#hold-button").addEventListener("click", () => this._hold());
    this.querySelector("#avoid-button").addEventListener("click", () => this._avoid());
  }

  /** Renders the same thing the add-on's web UI shows for a scanner whose
   *  panel is closed: what it is hearing, and a way to listen to it.
   *
   *  Every value here is read from this scanner's entities rather than from
   *  the add-on's status feed, which is the whole point of the integration
   *  -- but the *selection* of them deliberately tracks the add-on's
   *  collapsed panel (www/control.js `_renderNowPlaying`). Fields with no
   *  entity behind them (talkgroup, unit, site, system type) are simply
   *  absent rather than faked. */
  _render() {
    const hass = this._hass;
    const state = (domain, suffix) => {
      const value = hass.states[this._entityId(domain, suffix)]?.state;
      return value === undefined || value === null ||
        ["unknown", "unavailable", "None", ""].includes(value)
        ? ""
        : value;
    };

    // Whichever of these the scanner actually has is the headline; the rest
    // go under it. A conventional channel with no department still gets a
    // real title rather than "Channel / / System".
    const parts = [state("sensor", "channel"), state("sensor", "department"),
                   state("sensor", "system")].filter(Boolean);
    this.querySelector("#np-label").textContent = parts.shift() || "Scanning…";
    this.querySelector("#np-sub").textContent = parts.join(" · ");

    const device = hass.devices?.[this._deviceId()];
    this.querySelector("#sds-name").textContent =
      this._config.name || device?.name_by_user || device?.name || this._slug;

    // The RSSI sensor is None whenever the squelch is closed (the -999
    // sentinel is mapped out in sensor.py), so it doubles as "is this
    // scanner receiving right now".
    const rssi = state("sensor", "rssi");
    this.querySelector("#sds-panel").classList.toggle("receiving", Boolean(rssi));
    this.querySelector("#sds-chip-text").textContent = rssi ? "Receiving" : "Scanning";

    const frequency = state("sensor", "frequency");
    const rows = [
      ["Frequency", frequency ? `${frequency} MHz` : ""],
      ["Modulation", state("sensor", "mode")],
      ["Tone / NAC", state("sensor", "ctcss_dcs")],
      ["RSSI", rssi ? `${rssi} dBm` : ""],
    ].filter(([, value]) => value);

    const fields = this.querySelector("#np-fields");
    fields.textContent = "";
    for (const [name, value] of rows) {
      const field = document.createElement("div");
      field.className = "sds-field";
      const nameEl = document.createElement("div");
      nameEl.className = "sds-flabel";
      nameEl.textContent = name;
      const valueEl = document.createElement("div");
      valueEl.className = "sds-value";
      valueEl.textContent = value;
      field.appendChild(nameEl);
      field.appendChild(valueEl);
      fields.appendChild(field);
    }
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "sds-empty";
      empty.textContent = hass.states[this._entityId("sensor", "display")]
        ? "Nothing being received."
        : "No data from the add-on yet.";
      fields.appendChild(empty);
    }

    // Not shown, but still needed: this is where the audio button gets the
    // stream to play.
    this._streamUrl =
      hass.states[this._entityId("media_player", "media_player")]?.attributes?.stream_url;
    // Nothing to play means nothing to press -- a Listen button that silently
    // does nothing (the early return in _toggleAudio) is worse than a
    // visibly unavailable one.
    const audioButton = this.querySelector("#audio-toggle");
    audioButton.disabled = !this._streamUrl && !this._audioSession;
  }

  /** Hold and Avoid: the two front-panel keys that are worth having on a
   *  dashboard, and the only two.
   *
   *  Everything else about driving the scanner lives in the add-on's web UI,
   *  and the keypad/soft keys/power-cycle button were removed from this card
   *  for good reasons that haven't changed -- but these two act on whatever
   *  the scanner is on *right now*, which is exactly the thing you are
   *  looking at when you're looking at this card.
   *
   *  Avoid presses the unit's own AVOID key rather than sending the AVD
   *  command, which is what it did first and what didn't work. AVD names its
   *  target -- a system, department, channel or frequency, by list index --
   *  and this card has names, not indices, so the only AVD it could send was
   *  one with an empty target ("AVD,,,,2"). The key press is what "avoid
   *  this" means on the front panel: one press is a temporary avoid of
   *  whatever is being received, lasting until the scanner is power-cycled
   *  rather than editing a favourites list from a dashboard. The cost is
   *  that a key press means whatever the current screen says it means --
   *  acceptable here, since the screen this card is showing you is the
   *  scanning one.
   *
   *  Hold still goes through sds200.hold: HLD with no target is the
   *  command's own "hold here", and there is no front-panel Hold key to
   *  press instead (the unit holds via its soft keys, whose meaning moves
   *  with the screen). If it turns out to need a target too, it will now say
   *  so instead of quietly doing nothing -- the services report a refusal
   *  rather than dropping it (see _async_register_services._ack). */
  _hold() {
    this._callScanner("hold", {}, "hold", "holding on the current channel");
  }

  _avoid() {
    this._callScanner("key", { code: "avoid", mode: "P" }, "avoid", "temporarily avoided");
  }

  /** The device_id behind this scanner_id, looked up once. _render runs on
   *  every hass update (once a second), and this walks the whole device
   *  registry -- but a device that exists keeps its id, so the only lookups
   *  worth repeating are the ones that came back empty. */
  _deviceId() {
    if (!this._cachedDeviceId) {
      this._cachedDeviceId = deviceIdForScannerId(this._hass, this._slug);
    }
    return this._cachedDeviceId;
  }

  async _callScanner(service, data, label, done) {
    const deviceId = this._deviceId();
    if (!deviceId) {
      this._setActionStatus(`no device registered for scanner "${this._slug}"`);
      return;
    }
    const buttons = [this.querySelector("#hold-button"), this.querySelector("#avoid-button")];
    buttons.forEach((button) => (button.disabled = true));
    this._setActionStatus("sending…");
    try {
      await this._hass.callService(DOMAIN, service, { device_id: deviceId, ...data });
      this._setActionStatus(done);
    } catch (err) {
      // The service raises ServiceValidationError for a scanner the add-on
      // isn't currently connected to, and for one that answered the command
      // with NG -- both worth seeing on the card, since the buttons look
      // identical whether or not anything happened behind them.
      this._setActionStatus(`${label} failed: ${err?.message || err}`);
    } finally {
      buttons.forEach((button) => (button.disabled = false));
    }
  }

  _setActionStatus(text) {
    const el = this.querySelector("#action-status");
    if (!el) return;
    el.textContent = text;
    clearTimeout(this._actionStatusTimer);
    this._actionStatusTimer = setTimeout(() => {
      if (el.textContent === text) el.textContent = "";
    }, 6000);
  }

  /** The Listen button carries an icon as well as a label, so its text can't
   *  just be assigned over the top of it. */
  _setAudioButton(playing) {
    const button = this.querySelector("#audio-toggle");
    if (!button) return;
    button.classList.toggle("playing", playing);
    this.querySelector("#audio-toggle-label").textContent = playing ? "Stop" : "Listen";
    this.querySelector("#audio-toggle-icon").setAttribute("icon", playing ? "mdi:stop" : "mdi:play");
  }

  _toggleAudio() {
    if (this._audioSession) {
      this._stopAudio(true);
      return;
    }
    if (!this._streamUrl) return;
    this._setAudioButton(true);
    // Everything below this point is async, but it all runs off this click,
    // which is what gives the page the "sticky user activation" Chrome and
    // Android WebView require before any audio element is allowed to play.
    const session = { stopped: false, startedAt: Date.now() };
    this._audioSession = session;
    this._setAudioStatus(session, "connecting");
    this._acquireScreenLock(session);
    this._startAudio(session, this._streamUrl);
  }

  /**
   * Keep the phone's screen on for as long as audio is playing -- listening
   * to a scanner is exactly the case where nobody is touching the screen,
   * so the display sleeps within a minute and takes the card (and any
   * glance at what's being received) with it.
   *
   * Two mechanisms, in order, because the platform that most needs this is
   * the one least likely to have the standard API:
   *
   *   1. Screen Wake Lock (`navigator.wakeLock`) -- the real thing, and all
   *      that's needed in a normal browser.
   *   2. A playing <video> -- Chromium keeps the screen on by itself while
   *      a visible video element plays, which is a side effect rather than
   *      an API, but it's the one that predates and outlives WebView's
   *      support for the API above. The frames come from a canvas capture
   *      stream, so this ships no encoded video asset.
   *
   * Why the fallback exists at all: the Home Assistant Android app renders
   * the frontend in an Android WebView, and per MDN/caniwebview the Screen
   * Wake Lock API is *unsupported in WebView* even though Chrome for
   * Android has had it since 84 -- WebView holds no window it could set the
   * keep-screen-on flag on. So on the target platform, tier 1 is expected
   * to be missing and tier 2 is expected to carry this. The status line
   * says which one actually took, since (as with the audio transports)
   * that's not knowable from here.
   *
   * The guaranteed option, if both fail, is the companion app's own
   * Settings > Companion app > Keep screen on -- an app setting the page
   * can't reach, hence the pointer to it in the status line rather than
   * anything automatic.
   */
  async _acquireScreenLock(session) {
    if (await this._requestWakeLock(session)) return;
    if (await this._startKeepAwakeVideo(session)) return;
    this._setScreenStatus(
      session, "screen may sleep -- turn on Settings > Companion app > Keep screen on"
    );
  }

  async _requestWakeLock(session) {
    if (!navigator.wakeLock) return false;
    try {
      const sentinel = await navigator.wakeLock.request("screen");
      // Stopped while the request was in flight -- release it rather than
      // leaving the screen pinned on by a session that no longer exists.
      if (session.stopped || this._audioSession !== session) {
        sentinel.release().catch(() => {});
        return true;
      }
      session.wakeLock = sentinel;
      this._setScreenStatus(session, "screen held awake (wake lock)");
      return true;
    } catch (err) {
      // Rejects (NotAllowedError) if the document is hidden or the platform
      // refuses -- e.g. a low battery. Not fatal: fall through to the video.
      console.warn("sds200-card: screen wake lock refused", err);
      return false;
    }
  }

  /**
   * Tier 2 (see _acquireScreenLock): play a 12px canvas-capture stream in
   * the card's <video>, purely so Chromium's "don't sleep during video"
   * behaviour applies.
   *
   * The canvas is repainted on a timer rather than drawn once: a capture
   * stream only produces a frame when the canvas actually changes, and a
   * video whose frames stopped arriving is not reliably a video that's
   * still "playing" as far as that behaviour is concerned. 2fps of a
   * 12x12 canvas alternating between two near-black fills is cheap enough
   * to leave running for hours.
   */
  async _startKeepAwakeVideo(session) {
    const video = this.querySelector("#keep-awake");
    if (!video || typeof video.play !== "function") return false;
    const canvas = document.createElement("canvas");
    canvas.width = 12;
    canvas.height = 12;
    const ctx = canvas.getContext("2d");
    if (!ctx || typeof canvas.captureStream !== "function") return false;

    let flip = false;
    const paint = () => {
      flip = !flip;
      ctx.fillStyle = flip ? "#000000" : "#010101";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
    };
    paint();
    const stream = canvas.captureStream(2);
    const timer = setInterval(paint, 500);
    const keepAwake = { video, stream, timer };

    video.srcObject = stream;
    video.muted = true; // or the play() below is subject to autoplay policy
    try {
      await video.play();
    } catch (err) {
      console.warn("sds200-card: keep-awake video would not play", err);
      this._teardownKeepAwake(keepAwake);
      return false;
    }
    if (session.stopped || this._audioSession !== session) {
      this._teardownKeepAwake(keepAwake);
      return true;
    }
    session.keepAwake = keepAwake;
    this._setScreenStatus(session, "screen held awake (video)");
    return true;
  }

  _teardownKeepAwake(keepAwake) {
    clearInterval(keepAwake.timer);
    keepAwake.video.pause();
    keepAwake.video.srcObject = null;
    for (const track of keepAwake.stream.getTracks()) track.stop();
  }

  _releaseScreenLock(session) {
    if (!session) return;
    if (session.wakeLock) {
      session.wakeLock.release().catch(() => {});
      session.wakeLock = null;
    }
    if (session.keepAwake) {
      this._teardownKeepAwake(session.keepAwake);
      session.keepAwake = null;
    }
  }

  /**
   * A screen wake lock is released automatically whenever the document
   * becomes hidden (screen off, app backgrounded, another dashboard view)
   * and is NOT reinstated on the way back -- the spec requires re-requesting
   * it. Audio keeps playing through all of that, so without this the lock
   * silently covers only the first visible stretch of a session. The video
   * fallback is paused by the same transitions, so it gets the same
   * treatment.
   */
  _startScreenLockWatch() {
    if (this._visibilityHandler) {
      document.removeEventListener("visibilitychange", this._visibilityHandler);
    }
    this._visibilityHandler = () => {
      const session = this._audioSession;
      if (document.visibilityState !== "visible" || !session || session.stopped) return;
      if (session.keepAwake) {
        session.keepAwake.video.play().catch(() => {});
        return;
      }
      if (!session.wakeLock || session.wakeLock.released) this._acquireScreenLock(session);
    };
    document.addEventListener("visibilitychange", this._visibilityHandler);
  }

  _setScreenStatus(session, text) {
    if (session && this._audioSession !== session) return;
    const el = this.querySelector("#screen-status");
    if (el) el.textContent = text;
  }

  /**
   * Show what playback is doing, on the card itself. Not decoration: the
   * two symptoms this has to tell apart -- the bytes stopping (`waiting`,
   * or the reader finishing) versus the decoder rejecting them (`error`)
   * -- point at completely different halves of the system, and the Home
   * Assistant Android app gives no way to read a console.warn().
   */
  _setAudioStatus(session, text) {
    if (session && this._audioSession !== session) return;
    const el = this.querySelector("#audio-status");
    if (!el) return;
    const at = session ? ` at ${((Date.now() - session.startedAt) / 1000).toFixed(1)}s` : "";
    el.textContent = text ? `${text}${at}` : "";
  }

  /**
   * Two playback paths, tried in order: fetch()+MediaSource first, and an
   * embedded <iframe src=streamUrl> only if that fails to start. Neither one
   * works everywhere on its own -- each covers the other's known breakage.
   *
   * Real-install finding (see docs/protocol-notes.md): HA's own frontend
   * service worker breaks BOTH a plain <audio src=...> load and an in-page
   * fetch() of this long-lived stream ("A ServiceWorker intercepted the
   * request and encountered an unexpected error" / "TypeError: Error in
   * input stream"). What does work there is a genuine top-level navigation
   * to the same signed URL, and an <iframe>'s own document load is that same
   * "navigate" request type, so the iframe sidesteps it. Note both of those
   * error strings are Firefox's, and the underlying bug is Firefox's
   * handling of a never-ending response body through a service worker -- not
   * something every browser does.
   *
   * Real-install finding (2026-07-25): the iframe alone is *desktop-only* in
   * practice -- audio played in Firefox but never in the Home Assistant
   * Android app. That app renders the frontend in an Android WebView, and a
   * WebView navigation to a non-HTML content type (here `audio/mp4`) does
   * not get the built-in media-document player desktop browsers render for
   * it; it goes to the download handler or nowhere at all, so a zero-size
   * iframe pointed at the stream is silently a no-op. Nothing about the
   * add-on, the proxy, or the stream itself is Android-specific -- it's
   * purely that the iframe trick has no in-page media element to play.
   *
   * So: drive an actual in-page <audio> element via MediaSource, which needs
   * no media document and no navigate-type request, and keep the iframe as
   * the fallback for the browser where fetch() is the broken half.
   *
   * Real-install finding (2026-07-25, third round): that in-page fetch()
   * plays for exactly 30.0s and then dies with Firefox's "TypeError: Error
   * in input stream" -- the service-worker bug again, except it takes half
   * a minute to bite rather than failing immediately, which is why the
   * fallback (which only triggers if playback never *starts*) never fired.
   * Reading HA's own service worker settles what's possible here:
   * the `(api|auth)` route is NetworkOnly, and behind it sits a catch-all
   * `registerRoute` whose regex matches every path -- so every same-origin
   * request is intercepted, and no amount of moving this endpoint escapes
   * it. (Moving it off /api/ would be actively worse: it'd land on that
   * catch-all's StaleWhileRevalidate, which would try to *cache* an
   * endless stream.)
   *
   * What does escape it: a document the service worker doesn't control. An
   * <iframe sandbox> without allow-same-origin has an opaque origin, and an
   * opaque origin has no service worker registration, so requests made from
   * inside it go straight to the network. Hence `_openRelayStream()`: a
   * sandboxed, scripts-only iframe whose entire job is to run the fetch()
   * and postMessage the bytes back. It plays nothing -- decoding stays in
   * this document, on the <audio> element that already has the user
   * activation from the Play click -- so nothing depends on autoplay
   * working inside a sandboxed frame.
   *
   * Three transports, in order, each covering the one before it:
   *   1. relay   -- sandboxed-iframe fetch, service-worker-free.
   *   2. direct  -- plain in-page fetch; what 1 replaces, kept because it's
   *                 the one confirmed to decode on Android (for 30s).
   *   3. iframe  -- navigate an iframe at the stream and let the browser's
   *                 media document play it. Desktop-only, but it's the
   *                 longest-running thing Firefox has ever done here.
   * The status line names the winner, so the next real-world run says which
   * of these are still earning their place.
   */
  async _startAudio(session, url) {
    for (const transport of ["relay", "direct"]) {
      const started = await this._playViaMediaSource(session, url, transport);
      if (session.stopped || this._audioSession !== session) return;
      if (started) return;
      this._teardownMediaSource(session);
    }

    console.warn("sds200-card: no MediaSource transport worked; falling back to an <iframe> navigation");
    this._setAudioStatus(session, "MediaSource didn't start; using the iframe fallback");
    const frame = document.createElement("iframe");
    frame.src = url;
    frame.allow = "autoplay";
    this.querySelector("#audio-frame-container").appendChild(frame);
    session.frame = frame;
  }

  /**
   * Byte source that runs its fetch() inside a sandboxed iframe, out of
   * reach of the frontend's service worker (see _startAudio). The iframe is
   * `srcdoc` + `sandbox="allow-scripts"`: no allow-same-origin, which is
   * the whole point -- that's what makes its origin opaque. Consequences
   * worth knowing: its requests are cross-origin (so the proxy view sends
   * Access-Control-Allow-Origin) and carry no cookies (fine -- the stream
   * URL is signed, which is why it needs none).
   */
  _openRelayStream(session, url) {
    const frame = document.createElement("iframe");
    // setAttribute, not `frame.sandbox = ...`: the property is a
    // DOMTokenList, and assigning a plain string to it is only reliable in
    // browsers that implement the stringifier setter.
    frame.setAttribute("sandbox", "allow-scripts");
    frame.srcdoc = RELAY_HTML;
    this.querySelector("#audio-frame-container").appendChild(frame);

    const queue = [];
    let waiting = null;
    let ended = null;
    const wake = () => {
      if (!waiting) return;
      const w = waiting;
      if (queue.length) {
        waiting = null;
        w.resolve({ done: false, value: queue.shift() });
      } else if (ended) {
        waiting = null;
        if (ended.error) w.reject(ended.error);
        else w.resolve({ done: true });
      }
    };
    const onMessage = (event) => {
      // The relay's origin is opaque ("null"), so identify it by its window
      // rather than by origin -- and ignore every other frame's messages.
      if (event.source !== frame.contentWindow) return;
      const msg = event.data || {};
      if (msg.type === "ready") {
        frame.contentWindow.postMessage({ type: "start", url: new URL(url, location.href).href }, "*");
      } else if (msg.type === "chunk") {
        queue.push(msg.data);
        wake();
      } else if (msg.type === "end") {
        ended = {};
        wake();
      } else if (msg.type === "error") {
        ended = { error: new Error(msg.message || "relay fetch failed") };
        wake();
      }
    };
    window.addEventListener("message", onMessage);

    return {
      next: () =>
        new Promise((resolve, reject) => {
          if (queue.length) resolve({ done: false, value: queue.shift() });
          else if (ended?.error) reject(ended.error);
          else if (ended) resolve({ done: true });
          else waiting = { resolve, reject };
        }),
      close: () => {
        window.removeEventListener("message", onMessage);
        try {
          frame.contentWindow?.postMessage({ type: "stop" }, "*");
        } catch {
          // Frame already gone; nothing to abort.
        }
        frame.remove();
        // Release a pending next(): with the listener gone, nothing else
        // would ever settle it, and _pumpStream would await it forever.
        ended = ended || {};
        wake();
      },
    };
  }

  /** Byte source that fetches in this document -- i.e. through the service
   * worker, with everything that implies (see _startAudio). */
  _openDirectStream(session, url) {
    const controller = new AbortController();
    let reader = null;
    const connected = (async () => {
      const response = await fetch(url, { signal: controller.signal, credentials: "same-origin" });
      if (!response.ok) throw new Error(`stream responded ${response.status}`);
      if (!response.body) throw new Error("stream response has no readable body");
      reader = response.body.getReader();
    })();
    return {
      next: async () => {
        await connected;
        return reader.read();
      },
      close: () => controller.abort(),
    };
  }

  /**
   * Resolves true once the <audio> element is genuinely playing, false if
   * this path can't get there (unsupported, fetch blocked, no decodable
   * audio in time). Resolving true leaves the read loop running for the life
   * of the session; a failure *after* that point is logged, not retried into
   * the iframe fallback -- restarting from a half-played state would be a
   * worse experience than the gap in audio the user can already see.
   */
  _playViaMediaSource(session, url, transport) {
    return new Promise((resolve) => {
      if (!window.MediaSource || !window.MediaSource.isTypeSupported(AUDIO_MIME)) {
        console.warn("sds200-card: MediaSource can't play", AUDIO_MIME);
        resolve(false);
        return;
      }

      let settled = false;
      let playing = false;
      let timer = null;
      const settle = (ok) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(ok);
      };
      // A stream that dies *after* playback started can't fall back to the
      // iframe (see _startAudio), so end the session instead -- otherwise
      // the button sits on "Stop" over a player that's finished, with
      // no way to restart it short of a click that only stops nothing.
      const ended = () => {
        if (playing && this._audioSession === session) this._stopAudio();
        settle(false);
      };
      timer = setTimeout(() => {
        console.warn("sds200-card: no audio playing within %dms via MediaSource", AUDIO_START_TIMEOUT_MS);
        settle(false);
      }, AUDIO_START_TIMEOUT_MS);

      const audio = document.createElement("audio");
      audio.autoplay = true;
      // Without this, mobile WebKit/WebView can insist on taking playback
      // fullscreen -- meaningless for audio, and it would hijack the page.
      audio.playsInline = true;
      audio.addEventListener("playing", () => {
        playing = true;
        this._setAudioStatus(session, `playing (${transport})`);
        settle(true);
      });
      // "waiting" is the decisive one: it means the element ran the buffer
      // dry, i.e. bytes stopped arriving -- a scanner/RTSP/add-on problem,
      // not a browser one. An "error" here means the opposite: bytes
      // arrived and the decoder rejected them.
      audio.addEventListener("waiting", () =>
        this._setAudioStatus(session, "buffer empty, waiting for data")
      );
      audio.addEventListener("stalled", () => this._setAudioStatus(session, "stalled"));
      audio.addEventListener("ended", () => this._setAudioStatus(session, "playback ended"));
      audio.addEventListener("error", () => {
        console.warn("sds200-card: audio element error", audio.error);
        this._setAudioStatus(
          session, `decode/media error (${audio.error?.code}: ${audio.error?.message || "no detail"})`
        );
        ended();
      });
      // Lives in the DOM (in the same zero-size container as the fallback
      // iframe) rather than detached: a detached media element is eligible
      // for garbage collection while playing.
      this.querySelector("#audio-frame-container").appendChild(audio);
      session.audio = audio;

      const source =
        transport === "relay"
          ? this._openRelayStream(session, url)
          : this._openDirectStream(session, url);
      session.source = source;

      const mediaSource = new MediaSource();
      session.mediaSource = mediaSource;
      const objectUrl = URL.createObjectURL(mediaSource);
      audio.src = objectUrl;
      mediaSource.addEventListener(
        "sourceopen",
        () => {
          // Safe (and required, to not leak) once the MediaSource is
          // attached -- the element holds its own reference by then.
          URL.revokeObjectURL(objectUrl);
          let sourceBuffer;
          try {
            sourceBuffer = mediaSource.addSourceBuffer(AUDIO_MIME);
          } catch (err) {
            console.warn("sds200-card: addSourceBuffer failed", err);
            settle(false);
            return;
          }
          // `session.source !== source` means we closed this transport
          // ourselves (stopping, or moving on to the next one), so its
          // ending is expected and must not overwrite the status line with
          // a scarier explanation than the truth.
          this._pumpStream(session, source, sourceBuffer).then(
            () => {
              if (session.stopped || session.source !== source) return;
              console.warn("sds200-card: audio stream ended");
              this._setAudioStatus(session, "stream closed by the server");
              ended();
            },
            (err) => {
              if (session.stopped || session.source !== source) return;
              if (err?.name === "AbortError") return;
              console.warn("sds200-card: audio streaming failed", err);
              this._setAudioStatus(session, `stream failed (${transport}): ${err?.name}: ${err?.message}`);
              ended();
            }
          );
        },
        { once: true }
      );
    });
  }

  async _pumpStream(session, source, sourceBuffer) {
    const audio = session.audio;
    // The session can be torn down between any two awaits below -- by
    // _stopAudio (session.stopped) or, without ever being "stopped", by
    // _startAudio giving up on this path and switching to the iframe
    // fallback (which detaches session.audio out from under this loop).
    // Both mean: stop touching the SourceBuffer, whose MediaSource is now
    // closed and would throw on every call.
    const alive = () => !session.stopped && session.audio === audio;
    const idle = () =>
      new Promise((resolve) => {
        if (!sourceBuffer.updating) {
          resolve();
          return;
        }
        sourceBuffer.addEventListener("updateend", resolve, { once: true });
      });

    let playRequested = false;
    while (alive()) {
      const { done, value } = await source.next();
      if (done || !alive()) break;

      await idle();
      if (!alive()) break;
      this._trimAndReseek(audio, sourceBuffer);
      await idle();
      if (!alive()) break;

      try {
        sourceBuffer.appendBuffer(value);
      } catch (err) {
        if (err?.name !== "QuotaExceededError") throw err;
        // Drop everything already played and retry once -- see
        // AUDIO_BUFFER_KEEP_S; hitting this means the trim above wasn't
        // enough for this device's buffer budget.
        const end = Math.max(0, audio.currentTime - 1);
        if (sourceBuffer.buffered.length && end > sourceBuffer.buffered.start(0)) {
          sourceBuffer.remove(sourceBuffer.buffered.start(0), end);
          await idle();
        }
        if (!alive()) break;
        sourceBuffer.appendBuffer(value);
      }
      await idle();

      if (!playRequested) {
        playRequested = true;
        // The autoplay attribute alone isn't enough under Chrome/WebView's
        // autoplay policy; this explicit play() runs on the user activation
        // from the Play button click that started the session.
        audio.play().catch((err) => console.warn("sds200-card: audio.play() rejected", err));
      }
    }
  }

  _trimAndReseek(audio, sourceBuffer) {
    const buffered = sourceBuffer.buffered;
    if (!buffered.length) return;

    const start = buffered.start(0);
    const end = buffered.end(buffered.length - 1);
    // Skip forward when playback has drifted behind live (backgrounded
    // page, stalls). Landing slightly before the end, not exactly on it,
    // avoids immediately stalling again on the buffer's leading edge.
    if (end - audio.currentTime > AUDIO_MAX_LAG_S) {
      audio.currentTime = Math.max(start, end - 1);
    }
    const cutoff = audio.currentTime - AUDIO_BUFFER_KEEP_S;
    if (cutoff > start && !sourceBuffer.updating) {
      sourceBuffer.remove(start, cutoff);
    }
  }

  _teardownMediaSource(session) {
    session.source?.close();
    session.source = null;
    session.mediaSource = null;
    if (session.audio) {
      session.audio.pause();
      // Detach the MediaSource before dropping the element, so the browser
      // stops fetching/decoding immediately instead of whenever it gets
      // around to collecting it.
      session.audio.removeAttribute("src");
      session.audio.load();
      session.audio.remove();
      session.audio = null;
    }
  }

  /**
   * `clearStatus` is for the user pressing Stop -- every other caller stops
   * *because* something failed, and has just written why to the status line
   * (which no longer updates once _audioSession is cleared below, so the
   * reason stays on screen instead of being wiped by its own cleanup).
   */
  _stopAudio(clearStatus = false) {
    const session = this._audioSession;
    this._audioSession = null;
    if (clearStatus) this._setAudioStatus(null, "");
    // Reset the label here rather than in _toggleAudio: playback also stops
    // without a click (disconnectedCallback), and the button has to agree.
    this._setAudioButton(false);
    if (!session) return;
    session.stopped = true;
    // Before _audioSession is forgotten -- it's the only handle on the wake
    // lock, and a lock nobody can release keeps the screen on forever.
    this._releaseScreenLock(session);
    if (clearStatus) this._setScreenStatus(null, "");
    this._teardownMediaSource(session);
    if (session.frame) {
      session.frame.remove();
      session.frame = null;
    }
  }
}

customElements.define("sds200-card", SDS200Card);

/**
 * Self-heal already-broken card placeholders.
 *
 * Root cause (see docs/protocol-notes.md "Card doesn't render" for the full
 * debugging trail): HA's auto-registration (add_extra_js_url) injects a
 * bare, unawaited `import(...)`, which has none of a static module script's
 * ordering guarantees -- Lovelace can (and does) decide "sds200-card
 * doesn't exist" before this module's promise resolves, and doesn't
 * recheck once it does. Can't fix HA's own injection mechanism, so this
 * scans for already-rendered "unknown card type" placeholders meant to be
 * us once this module finishes loading, and swaps in a real instance --
 * plus a MutationObserver for placeholders rendered afterward.
 *
 * Two non-obvious things this depends on:
 * - Lovelace's card tree is built of nested shadow roots, so finding
 *   placeholders requires piercing `.shadowRoot` explicitly (plain
 *   `querySelectorAll`/`MutationObserver` only ever see light DOM) --
 *   `deepQuery`/`observeDeep` below, plus patching `attachShadow` globally
 *   so shadow roots created later (e.g. a new dashboard view) get observed
 *   too.
 * - `<hui-error-card>`'s own config is just `{ type: "error", message }` --
 *   HA never preserves the original failed config on the error element
 *   itself for cards (confirmed against create-element-base.ts; only
 *   badges get an `origConfig` field). The real config lives one level up,
 *   on the shadow-root host (e.g. a `hui-vertical-stack-card`'s own
 *   `_config.cards`), since a container card is just handed its children's
 *   raw configs. `findOrigConfig` climbs to that host and searches its
 *   config tree instead of the error element.
 */
function deepQuery(root, selector, out = []) {
  if (root.querySelectorAll) {
    out.push(...root.querySelectorAll(selector));
  }
  const walker = root.querySelectorAll ? root.querySelectorAll("*") : [];
  for (const el of walker) {
    if (el.shadowRoot) deepQuery(el.shadowRoot, selector, out);
  }
  return out;
}

function collectSds200Configs(node, out) {
  if (!node || typeof node !== "object") return;
  if (node.type === "custom:sds200-card") {
    out.push(node);
    return;
  }
  if (Array.isArray(node)) {
    node.forEach((item) => collectSds200Configs(item, out));
    return;
  }
  if (node.cards) collectSds200Configs(node.cards, out);
  if (node.card) collectSds200Configs(node.card, out);
}

function findOrigConfig(errorEl) {
  let host = errorEl.getRootNode()?.host;
  for (let depth = 0; host && depth < 8; depth++) {
    const matches = [];
    collectSds200Configs(host._config || host.config, matches);
    if (matches.length === 1) return matches[0];
    if (matches.length > 1) {
      // More than one sds200 card in the same container -- pair this
      // placeholder with its config by position among this host's broken
      // sds200 placeholders (assumes config array order matches render
      // order, true for HA's built-in stack/grid cards). Falls back to the
      // first match if position can't be determined.
      const siblings = deepQuery(host.shadowRoot || host, "hui-error-card").filter((el) =>
        el._config?.message?.includes("sds200-card")
      );
      const index = siblings.indexOf(errorEl);
      return matches[index >= 0 ? index : 0];
    }
    const next = host.getRootNode?.()?.host;
    if (!next || next === host) break;
    host = next;
  }
  return undefined;
}

// Keyed by the original config object (a stable reference pulled straight
// out of the parent's `_config` tree -- see findOrigConfig) so the same
// logical card slot maps to the same key across repeated heal attempts.
// HA has its own native repair path for this exact "custom element not
// defined yet" race, running independently of this workaround, and
// Lovelace's own reactivity can also produce more than one fresh
// `hui-error-card` for the same slot -- this map makes healing idempotent:
// once a slot has a live replacement, a later placeholder for the same
// config is dropped instead of spawning another card.
const healedReplacements = new WeakMap();

function healBrokenCard(errorEl) {
  const config = findOrigConfig(errorEl);
  if (!config) return;
  const existing = healedReplacements.get(config);
  if (existing?.isConnected) {
    errorEl.remove();
    return;
  }
  try {
    const replacement = document.createElement("sds200-card");
    replacement.setConfig(config);
    if (errorEl.hass) replacement.hass = errorEl.hass;
    errorEl.replaceWith(replacement);
    healedReplacements.set(config, replacement);
    console.log("[sds200-card] healed a broken card placeholder");
  } catch (err) {
    console.warn("[sds200-card] failed to heal a broken card placeholder", err);
  }
}

function handleAddedNode(node) {
  if (node.tagName === "HUI-ERROR-CARD") healBrokenCard(node);
  deepQuery(node, "hui-error-card").forEach(healBrokenCard);
  if (node.shadowRoot) observeDeep(node.shadowRoot);
}

const observedRoots = new WeakSet();
function observeDeep(root) {
  if (observedRoots.has(root)) return;
  observedRoots.add(root);
  deepQuery(root, "hui-error-card").forEach(healBrokenCard);
  new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType === Node.ELEMENT_NODE) handleAddedNode(node);
      }
    }
  }).observe(root, { childList: true, subtree: true });
}

// Catches shadow roots created after this script runs (e.g. switching to a
// dashboard view/card not yet rendered) -- observeDeep on an existing root
// can't see into a shadow root that doesn't exist yet.
const nativeAttachShadow = Element.prototype.attachShadow;
Element.prototype.attachShadow = function (init) {
  const root = nativeAttachShadow.call(this, init);
  observeDeep(root);
  return root;
};

observeDeep(document.body);

/**
 * Visual config editor: a single device picker (filtered to this
 * integration) using HA's built-in <ha-selector>, so the card is pickable
 * and configurable entirely from the Lovelace UI without hand-typing a
 * scanner_id. Uses <ha-selector>'s standard "value-changed" event contract,
 * which has been stable across recent HA frontend versions but -- like the
 * hass.devices/hass.entities lookups above -- hasn't been checked against
 * every version this project might run on.
 */
class SDS200CardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass) return;
    if (!this._selector) {
      this.innerHTML = `<div style="padding: 8px 0;"></div>`;
      this._selector = document.createElement("ha-selector");
      this._selector.selector = { device: { integration: DOMAIN } };
      this._selector.label = "SDS200 scanner";
      this._selector.required = true;
      this._selector.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        const deviceId = ev.detail.value;
        const scannerId = scannerIdForDeviceId(this._hass, deviceId);
        if (!scannerId) return;
        const newConfig = { ...this._config, scanner_id: scannerId };
        this.dispatchEvent(
          new CustomEvent("config-changed", { detail: { config: newConfig }, bubbles: true, composed: true })
        );
      });
      this.firstElementChild.appendChild(this._selector);
    }
    this._selector.hass = this._hass;
    this._selector.value = deviceIdForScannerId(this._hass, this._config.scanner_id);
  }
}

customElements.define("sds200-card-editor", SDS200CardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "sds200-card",
  name: "SDS200 Scanner",
  description: "Front-panel style control card for a Uniden SDS200 scanner.",
});
