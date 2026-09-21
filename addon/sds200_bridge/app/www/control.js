/* Control tab: the scanner's front panel, its live audio, and what it is
 * hearing right now.
 *
 * Display state arrives over the same WebSocket the Home Assistant
 * integration uses (`type: "status"`, demuxed by scanner_id) rather than by
 * polling: the add-on is already polling the scanner and fanning the result
 * out at most once a second, and a
 * second polling loop on top of that would only add latency and traffic.
 * Commands go out over the REST API, one per press.
 */

"use strict";

(function (SDS) {
  const { el, fill, post, api, formatTime, formatDuration } = SDS;

  // The panel's own tail of the receive history. Long enough to see what the
  // scanner has been doing, short enough that it doesn't turn into the
  // History tab, which is where the whole log lives.
  const RECENT_LIMIT = 10;

  // Matches media_player.py / number.py, which match the scanner's own
  // documented ranges.
  const MAX_VOLUME = 29;
  const MAX_SQUELCH = 19;

  const KEYPAD = [
    ["1", "2", "3"],
    ["4", "5", "6"],
    ["7", "8", "9"],
    ["dot", "0", "enter"],
  ];
  const KEY_LABELS = { dot: ".", enter: "E" };

  // The soft keys under the screen, then the dedicated function keys. Same
  // split the real unit has, and the same one the Lovelace card settled on.
  const SOFT_KEYS = [
    { label: "System", code: "soft1" },
    { label: "Dept", code: "soft2" },
    { label: "Channel", code: "soft3" },
  ];
  const FUNCTION_KEYS = [
    { label: "Menu", code: "menu" },
    { label: "Func", code: "func" },
    { label: "Avoid", code: "avoid" },
    { label: "Zip", code: "zip" },
    { label: "Range", code: "range" },
    { label: "Replay", code: "replay" },
    { label: "Srv Type", code: "service_type" },
  ];

  /** An avoid target as something to put in a sentence. The name is
   *  Uniden's own for the list entry; the index is what AVD actually takes,
   *  and stands in for entries the database gives no name. */
  function avoidName(target) {
    return target.name || `index ${target.index}`;
  }

  class ControlPanel {
    constructor(scanner) {
      this.scanner = scanner;
      this.root = el("section", "card scanner-panel");
      this.player = new SDS.AudioPlayer(scanner.id, {
        onStatus: (text) => this._setAudioStatus(text),
        // Playback can end without a click on Stop (the add-on's audio
        // session restarted, the socket dropped) -- put the button back
        // rather than leave it offering to stop nothing.
        onStopped: () => {
          this.playButton.textContent = "Listen";
        },
      });
      this.status = {};
      // What a permanent avoid would be pointed at, refreshed from every
      // status push (see _renderAvoid). Null until GSI says otherwise, which
      // is also the state that keeps the button disabled.
      this.avoidTarget = null;
      this.recent = [];
      this.recentOpen = new Set();
      // Set while a slider is being dragged, so an incoming status push
      // doesn't yank the thumb back to the scanner's last reported value
      // mid-gesture.
      this.holdingSlider = null;
      this._build();
    }

    destroy() {
      this.player.stop();
    }

    /* ---------------------------------------------------------------- */

    _build() {
      const head = el("div", "scanner-head");
      const title = el("div", "scanner-title");
      this.dot = el("span", "dot");
      this.dot.dataset.state = "unknown";
      title.appendChild(this.dot);
      title.appendChild(el("h2", "scanner-name", this.scanner.name || this.scanner.id));
      head.appendChild(title);

      this.rebootButton = el("button", "danger", "Power-cycle");
      this.rebootButton.type = "button";
      this.rebootButton.title =
        "Cuts and restores PoE power at the switch port configured for this scanner.";
      this.rebootButton.hidden = !this.scanner.can_reboot;
      this.rebootButton.addEventListener("click", () => this._reboot());
      head.appendChild(this.rebootButton);
      this.root.appendChild(head);

      // What it's hearing is all a closed panel shows: it is the one thing
      // worth reading at a glance. The screen, the audio and the keys are all
      // behind the disclosure.
      this.root.appendChild(this._buildNowPlaying());
      this.root.appendChild(this._buildControls());
    }

    /** What the scanner is showing, read out of the data behind it rather
     *  than drawn as a copy of the screen.
     *
     *  This used to be a character-for-character mirror: a <pre> of the STS
     *  display lines in green monospace, complete with reverse-video runs.
     *  It was faithful and it was the wrong thing -- 30 columns of fixed
     *  text can't reflow, can't be styled, and reduced everything the add-on
     *  already knows structurally (this is a talkgroup, this is a frequency,
     *  this is a signal level) back down to characters at fixed offsets.
     *
     *  Every value here comes from the same GSI payload reception.py reads,
     *  laid out roughly the way the unit lays it out -- system and
     *  department over the channel, details under it, soft-key labels along
     *  the bottom -- so it still reads like the scanner without pretending
     *  to be its LCD.
     *
     *  The raw screen text is still one click away (see _renderScreenText):
     *  menus, the weather-alert screen and anything else GSI has no fields
     *  for exist only as display text, and dropping it would have made the
     *  keypad unusable for anything but scanning. */
    _buildScreen() {
      const stack = el("div", "display-stack");
      const display = el("div", "display");

      const head = el("div", "display-head");
      this.displayState = el("span", "state-chip");
      this.displayState.appendChild(el("span", "state-dot"));
      this.displayStateText = el("span", null, "Connecting");
      this.displayState.appendChild(this.displayStateText);
      head.appendChild(this.displayState);
      this.displayBadge = el("span", "badge mode");
      this.displayBadge.hidden = true;
      head.appendChild(this.displayBadge);
      display.appendChild(head);

      this.displayTitle = el("div", "display-title", "connecting…");
      this.displaySub = el("div", "display-sub");
      display.appendChild(this.displayTitle);
      display.appendChild(this.displaySub);

      // The one thing the character screen could never show well: signal as
      // a level rather than as a number in a corner.
      this.displayMeter = el("div", "display-meter");
      this.displayMeter.hidden = true;
      const track = el("div", "meter-track");
      this.displayMeterFill = el("span", "meter-fill");
      track.appendChild(this.displayMeterFill);
      this.displayMeterValue = el("span", "meter-value");
      this.displayMeter.appendChild(track);
      this.displayMeter.appendChild(this.displayMeterValue);
      display.appendChild(this.displayMeter);

      this.displayGrid = el("div", "display-grid");
      display.appendChild(this.displayGrid);

      this.screenDetails = el("details", "screen-text");
      this.screenDetails.appendChild(el("summary", null, "Screen text"));
      this.screen = el("div", "screen-lines");
      this.screenDetails.appendChild(this.screen);
      display.appendChild(this.screenDetails);

      stack.appendChild(display);

      // Labelled from the scanner's own soft-key row when it sends one (see
      // _renderSoftKeys); these are the defaults until then.
      const soft = el("div", "key-grid soft-keys");
      this.softButtons = SOFT_KEYS.map((key) => {
        const button = this._keyButton(key.label, key.code);
        soft.appendChild(button);
        return button;
      });
      stack.appendChild(soft);
      return stack;
    }

    _buildNowPlaying() {
      const box = el("div", "now-playing");
      this.nowPlaying = el("div", "np-fields");
      this.nowLabel = el("div", "np-label", "—");
      this.nowBadge = el("span", "badge mode");
      const head = el("div", "np-head");
      head.appendChild(this.nowLabel);
      head.appendChild(this.nowBadge);
      box.appendChild(head);
      box.appendChild(this.nowPlaying);
      return box;
    }

    /** The screen, the audio and every key live behind one disclosure,
     *  closed by default: a panel at rest is a line about what the scanner is
     *  hearing, and opening it is how you go and administer the thing.
     *  Audio, once started, keeps playing whether the drawer is open or not. */
    _buildControls() {
      const wrap = el("div", "controls");

      const drawer = el("details", "scanner-drawer");
      drawer.appendChild(el("summary", null, "Display and controls"));
      drawer.appendChild(this._buildScreen());
      // The display inside the drawer is a superset of the closed panel's
      // summary -- same readout, more of it -- so showing both at once would
      // be the same scanner twice, one above the other.
      drawer.addEventListener("toggle", () =>
        this.root.classList.toggle("drawer-open", drawer.open)
      );

      // Under the screen: the last few calls on the left, everything that
      // drives the scanner in a column on the right.
      const body = el("div", "drawer-body");
      body.appendChild(this._buildRecent());

      const column = el("div", "drawer-column");
      this.volume = this._slider("Volume", MAX_VOLUME, (level) =>
        post(`scanners/${this.scanner.id}/volume`, { level })
      );
      this.squelch = this._slider("Squelch", MAX_SQUELCH, (level) =>
        post(`scanners/${this.scanner.id}/squelch`, { level })
      );
      const levels = el("div", "level-row");
      levels.appendChild(this.volume.node);
      levels.appendChild(this.squelch.node);
      column.appendChild(levels);

      column.appendChild(this._buildKeys());

      // Bottom right, after every key: the one control here that isn't a
      // press on the scanner's front panel.
      const audio = el("div", "audio-row");
      this.audioStatus = el("span", "status");
      this.playButton = el("button", "primary", "Listen");
      this.playButton.type = "button";
      this.playButton.addEventListener("click", () => this._toggleAudio());
      audio.appendChild(this.audioStatus);
      audio.appendChild(this.playButton);
      column.appendChild(audio);

      body.appendChild(column);
      drawer.appendChild(body);
      wrap.appendChild(drawer);

      this.commandStatus = el("p", "status command-status");
      wrap.appendChild(this.commandStatus);
      return wrap;
    }

    /** The last few calls this scanner heard. Fetched once and then kept
     *  current from the same "reception" pushes the History tab listens to,
     *  so a call lands here as it starts rather than on the next poll. */
    _buildRecent() {
      const box = el("div", "recent");
      box.appendChild(el("div", "recent-head", "Recent calls"));
      this.recentList = el("div", "recent-list");
      box.appendChild(this.recentList);
      this._renderRecent();
      return box;
    }

    _renderRecent() {
      if (!this.recent.length) {
        fill(this.recentList, el("p", "recent-empty", "Nothing heard yet."));
        return;
      }
      fill(
        this.recentList,
        ...this.recent.map((record) => {
          const live = this.recentOpen.has(record.id);
          const row = el("div", `recent-row${live ? " live" : ""}`);
          row.appendChild(el("time", null, formatTime(record.started)));
          row.appendChild(el("span", "recent-label", record.label || "unknown"));
          row.appendChild(
            el(
              "small",
              null,
              live
                ? "receiving…"
                : formatDuration(record.duration) + (record.interrupted ? " (cut off)" : "")
            )
          );
          return row;
        })
      );
    }

    /** A "reception" push for this scanner, handed down by app.js. */
    onReception(message) {
      const record = message.record;
      if (!record) return;
      if (message.event === "start") {
        this.recent.unshift(record);
        this.recentOpen.add(record.id);
        this.recent = this.recent.slice(0, RECENT_LIMIT);
      } else if (message.event === "dropped") {
        // The row has been deleted from the log. Without this it stayed on
        // this list and only this list, so the control tab showed calls the
        // history tab knew nothing about -- which is exactly how it was
        // reported.
        this.recentOpen.delete(record.id);
        const index = this.recent.findIndex((r) => r.id === record.id);
        if (index === -1) return;
        this.recent.splice(index, 1);
      } else {
        this.recentOpen.delete(record.id);
        const index = this.recent.findIndex((r) => r.id === record.id);
        if (index === -1) return;
        this.recent[index] = record;
      }
      this._renderRecent();
    }

    /** Every key on one grid of the same width, in the order the unit has
     *  them down its face: rotary, keypad -- then the function keys, which
     *  used to run down the side of the screen. The soft keys are not here:
     *  they belong to the screen and are drawn under it (_buildScreen), same
     *  as on the unit. */
    _buildKeys() {
      const keys = el("div", "keys");

      const nav = el("div", "key-grid");
      nav.appendChild(this._keyButton("◀", "rotary_left", "Rotary left"));
      nav.appendChild(this._keyButton("Select", "rotary_push", "Rotary push"));
      nav.appendChild(this._keyButton("▶", "rotary_right", "Rotary right"));
      keys.appendChild(nav);

      const pad = el("div", "key-grid keypad");
      KEYPAD.forEach((row) =>
        row.forEach((code) => pad.appendChild(this._keyButton(KEY_LABELS[code] || code, code)))
      );
      keys.appendChild(pad);

      const fn = el("div", "key-grid fn-grid");
      FUNCTION_KEYS.forEach((key) => fn.appendChild(this._keyButton(key.label, key.code)));
      // Eighth cell of a grid the seven function keys leave a gap in, and
      // the only thing here that isn't a key press -- next to Srv Type, two
      // along from the Avoid key it is the lasting version of.
      fn.appendChild(this._buildAvoid());
      keys.appendChild(fn);

      return keys;
    }

    /** The one control on this panel that outlives a power cycle.
     *
     *  The Avoid key two cells back is the front panel's, and one press of
     *  it is a *temporary* avoid: it lasts until the scanner restarts. This
     *  sends AVD for the channel on screen instead, which edits the
     *  favourites list on the unit -- nothing here can put it back, and the
     *  scanner's own menus are where you'd go to clear it. Hence the danger
     *  colouring among otherwise identical keys, the confirm before it
     *  fires, and the disabled state whenever there is nothing indexed on
     *  screen to point AVD at. What it would hit is in its tooltip and named
     *  again in the confirm, since a key has room for a label and not for a
     *  channel name that changes every few seconds. */
    _buildAvoid() {
      this.avoidButton = el("button", "key warn", "Perm Avoid");
      this.avoidButton.type = "button";
      this.avoidButton.disabled = true;
      this.avoidButton.addEventListener("click", () => this._avoidPermanently());
      this._renderAvoid(null);
      return this.avoidButton;
    }

    _keyButton(label, code, title) {
      const button = el("button", "key", label);
      button.type = "button";
      if (title) button.title = title;
      // Read the label at press time, not at build time: the soft keys are
      // relabelled from the scanner's own screen, and the status line should
      // say which key it just pressed by its current name.
      button.addEventListener("click", () => this._sendKey(code, button.textContent || label));
      return button;
    }

    /** A slider plus a live readout, laid out on one line. Sends on release
     *  and on the debounced tail of a drag, not on every input event -- one
     *  UDP command per pixel dragged would swamp the scanner's control
     *  interface, which serializes one outstanding command at a time. */
    _slider(name, max, send) {
      const node = el("label", "slider");
      node.appendChild(el("span", "slider-name", name));

      const input = el("input");
      input.type = "range";
      input.min = "0";
      input.max = String(max);
      input.value = "0";
      node.appendChild(input);

      const readout = el("b", "readout", "—");
      node.appendChild(readout);

      const commit = SDS.debounce(async () => {
        try {
          await send(Number(input.value));
          this._setCommandStatus(`${name} set to ${input.value}`);
        } catch (err) {
          this._setCommandStatus(`${name} failed: ${err.message}`, true);
        }
      }, 250);

      input.addEventListener("input", () => {
        readout.textContent = input.value;
        this.holdingSlider = name;
        commit();
      });
      input.addEventListener("change", () => {
        this.holdingSlider = null;
      });
      return { node, input, readout, name };
    }

    /* ---------------------------------------------------------------- */

    async _sendKey(code, label) {
      try {
        const result = await post(`scanners/${this.scanner.id}/key`, { code, mode: "P" });
        // The scanner answers "KEY,OK" or "KEY,NG"; the API turns that into
        // a bool, and a refused key is worth saying out loud -- several
        // codes in the table are documented for the SDS100 and unverified
        // on this model (see key_codes.py).
        this._setCommandStatus(
          result.ok ? `${label} sent` : `${label}: the scanner refused that key`,
          !result.ok
        );
      } catch (err) {
        this._setCommandStatus(`${label} failed: ${err.message}`, true);
      }
    }

    /** Avoid the current channel for good, and say what actually happened.
     *
     *  This presses the unit's own AVOID key twice rather than sending the
     *  AVD command, and the reason is the whole story of this feature (see
     *  docs/protocol-notes.md, "How avoids actually persist"): AVD sets a
     *  genuine permanent avoid but writes only the scanner's working copy,
     *  so a power cycle discards it -- which made it, in practice,
     *  indistinguishable from the temporary avoid the Avoid key already
     *  does. Only a keypress that sets a permanent avoid gets written to
     *  flash.
     *
     *  The cost is aim: a key press lands on whatever the scanner is on at
     *  that instant, and it is scanning. So the add-on reads the scanner's
     *  own channel list back afterwards and reports what it finds -- avoided
     *  for good, only temporarily avoided (the presses didn't land close
     *  enough together), or missed entirely because the scanner had moved
     *  on. A miss is worth showing plainly: it means some *other* channel
     *  may have been avoided instead. */
    async _avoidPermanently() {
      const target = this.avoidTarget;
      if (!target) return;
      const name = avoidName(target);
      if (!window.confirm(
        `Permanently avoid ${name}? This presses the scanner's own Avoid key twice, so it ` +
        `acts on whatever the scanner is on right now — you'll be told which channel it ` +
        `actually caught. Clearing it afterwards is done on the scanner itself.`
      )) {
        return;
      }
      this.avoidButton.disabled = true;
      this._setCommandStatus(`avoiding ${name}…`);
      try {
        const result = await post(`scanners/${this.scanner.id}/avoid_current`, {});
        const avoided = avoidName(result.target || target);
        if (result.ok) {
          const also = result.committed
            ? `, and saved ${result.committed} pending un-avoid${result.committed > 1 ? "s" : ""}`
            : "";
          this._setCommandStatus(`${avoided} is avoided for good${also}`);
        } else if (result.state === "T-Avoid") {
          this._setCommandStatus(
            `${avoided} is only avoided until the next restart — the two presses didn't ` +
            `land close enough together. Try again.`, true
          );
        } else {
          this._setCommandStatus(
            `${avoided} was not avoided (the scanner's list still reads ` +
            `${result.state || "unchanged"}) — it had moved on, and another channel may ` +
            `have been caught instead.`, true
          );
        }
      } catch (err) {
        this._setCommandStatus(`avoid failed: ${err.message}`, true);
      } finally {
        this.avoidButton.disabled = !this.avoidTarget;
      }
    }

    async _reboot() {
      if (!window.confirm(
        `Cut power to "${this.scanner.name || this.scanner.id}" at its switch port and restore ` +
        `it? Audio and control will drop for about a minute.`
      )) {
        return;
      }
      this.rebootButton.disabled = true;
      this._setCommandStatus("power-cycling…");
      try {
        await post(`scanners/${this.scanner.id}/reboot`, {});
        this._setCommandStatus("power-cycled — the scanner will take about a minute to come back");
      } catch (err) {
        this._setCommandStatus(`power-cycle failed: ${err.message}`, true);
      } finally {
        this.rebootButton.disabled = false;
      }
    }

    async _toggleAudio() {
      if (this.player.playing) {
        this.player.stop();
        this.playButton.textContent = "Listen";
        return;
      }
      this.playButton.textContent = "Stop";
      await this.player.start();
      // start() resolves once a transport has taken (or every one has
      // failed), so this reflects what actually happened rather than what
      // was asked for.
      if (!this.player.playing) this.playButton.textContent = "Listen";
    }

    _setAudioStatus(text) {
      this.audioStatus.textContent = text;
    }

    _setCommandStatus(text, isError) {
      this.commandStatus.textContent = text;
      this.commandStatus.classList.toggle("error-text", !!isError);
      clearTimeout(this._statusTimer);
      this._statusTimer = setTimeout(() => {
        this.commandStatus.textContent = "";
        this.commandStatus.classList.remove("error-text");
      }, 6000);
    }

    /* ---------------------------------------------------------------- */

    /** Fetch the current state once, so a freshly-opened tab isn't blank
     *  until the next push. */
    async refresh() {
      try {
        this.update(await api(`scanners/${this.scanner.id}/status`));
      } catch (err) {
        // Where the reading would have gone, rather than in a status line
        // under it: a stale channel name sitting above an error is worse
        // than no channel name.
        this.displayTitle.textContent = `could not read the scanner: ${err.message}`;
        this.displaySub.textContent = "";
      }
      for (const control of [this.volume, this.squelch]) {
        try {
          const path = control === this.volume ? "volume" : "squelch";
          const data = await api(`scanners/${this.scanner.id}/${path}`);
          if (data.level !== null && data.level !== undefined) {
            control.input.value = String(data.level);
            control.readout.textContent = String(data.level);
          }
        } catch (err) {
          /* The scanner drops the occasional command (see protocol.py);
             the value stays "—" until the next reading. */
        }
      }
      await this._loadRecent();
    }

    async _loadRecent() {
      const params = new URLSearchParams({
        scanner: this.scanner.id,
        limit: String(RECENT_LIMIT),
      });
      try {
        const data = await api(`api/history?${params}`);
        this.recent = data.records || [];
        this.recentOpen = new Set(data.open_ids || []);
      } catch (err) {
        /* Leaves the list as it was -- the reception pushes will fill it in
           as calls come through. */
      }
      this._renderRecent();
    }

    setReachable(reachable) {
      this.dot.dataset.state = reachable ? "up" : "down";
      this.dot.title = reachable
        ? "Responding on the control interface."
        : "Not responding on the control interface.";
    }

    update(status) {
      this.status = status || {};
      const readout = this._readout();
      this._renderDisplay(readout, this.status.lines || []);
      this._renderNowPlaying(readout);
      this._renderScreenText(this.status.lines || []);
      this._renderSoftKeys(this.status.soft_keys || []);
      this._renderAvoid(readout);
      this._syncSliders();
    }

    /** One reading of the GSI payload, for both readouts.
     *
     *  Deliberately re-derived here from the same GSI payload the add-on's
     *  own reception.py reads, rather than shipped down pre-flattened: the
     *  status push is the HA integration's message format and adding fields
     *  to it for this page's benefit would put a second consumer's needs
     *  into a contract that already has one.
     *
     *  Both the closed panel's summary and the open panel's display read
     *  this same object, so the two can't drift into disagreeing about the
     *  same scanner. */
    _readout() {
      const gsi = this.status.gsi;
      if (!gsi) return null;

      const conv = gsi.ConvFrequency || {};
      const site = gsi.Site || {};
      const siteFreq = gsi.SiteFrequency || {};
      const tgid = gsi.TGID || {};
      const property = gsi.Property || {};

      const clean = (value) => {
        const text = (value === null || value === undefined ? "" : String(value)).trim();
        return ["", "None", "Off", "---", "TGID None", "UID None"].includes(text) ? "" : text;
      };
      const first = (...values) => values.map(clean).find(Boolean) || "";

      const rssi = Number(property.Rssi);
      const receiving = Number.isFinite(rssi) && rssi > -999;

      // What AVD could be pointed at, by the same rule the add-on applies
      // server-side (reception.channel_target): conventional scanning is on a
      // frequency entry, trunked is on a talkgroup, and the list index GSI
      // puts on that element is the only handle AVD takes. Read here so the
      // button can name its target and disable itself on a screen that
      // hasn't got one; the add-on re-reads it before actually sending.
      const avoidTarget =
        [conv, tgid]
          .map((entry) => ({
            index: clean(entry.Index),
            name: clean(entry.Name),
            avoided: String(entry.Avoid || "").toLowerCase() === "on",
          }))
          .find((entry) => entry.index) || null;

      return {
        avoidTarget,
        channel: first(conv.Name, tgid.Name),
        department: clean((gsi.Department || {}).Name),
        system: clean((gsi.System || {}).Name),
        systemType: clean((gsi.System || {}).SystemType),
        receiving,
        rssi: receiving ? rssi : null,
        rows: [
          ["Frequency", first(conv.Freq, siteFreq.Freq)],
          ["Modulation", first(conv.Mod, site.Mod)],
          ["Talkgroup", first(tgid.TGID, conv.TGID)],
          ["Unit", first(tgid.U_Id, conv.U_Id)],
          ["Tone / NAC", first(conv.SAD, siteFreq.SAD, conv.SAS, siteFreq.SAS)],
          ["Site", clean(site.Name)],
          ["System type", clean((gsi.System || {}).SystemType)],
          ["RSSI", receiving ? `${rssi} dBm` : ""],
        ].filter(([, value]) => value),
      };
    }

    /** The open panel's display: the same fields the unit puts on its screen,
     *  as text that can reflow and a signal level that can be a bar. */
    _renderDisplay(readout, lines) {
      // With no GSI the scanner is showing something GSI has no fields for
      // -- a menu, the boot screen -- and its own first line is the closest
      // thing to a heading that exists ("Menu", "Weather Alert"). Falling
      // back to it beats a headline that says "connecting…" at a scanner
      // that is plainly connected and sitting in a menu.
      const heading = (lines.find((line) => (line.text || "").trim()) || {}).text || "";
      const parts = readout
        ? [readout.channel, readout.department, readout.system].filter(Boolean)
        : [];
      this.displayTitle.textContent =
        parts.shift() || (readout ? "Scanning…" : heading.trim() || "connecting…");
      this.displaySub.textContent = parts.join(" · ");

      const receiving = Boolean(readout && readout.receiving);
      this.displayState.classList.toggle("live", receiving);
      // Nothing to claim about a screen we can only read as text: the state
      // chip is about scanning, and this isn't it.
      this.displayState.hidden = !readout && Boolean(heading);
      this.displayStateText.textContent = !readout
        ? "No data"
        : receiving
          ? "Receiving"
          : "Scanning";

      this.displayBadge.hidden = !(readout && readout.systemType);
      this.displayBadge.textContent = readout ? readout.systemType : "";

      // dBm to a proportion of the bar. -120 to -40 is the usable span on
      // this hardware; the numeric value is next to it for anyone who wants
      // the actual reading, so the bar only has to be indicative.
      this.displayMeter.hidden = !receiving;
      if (receiving) {
        const level = Math.max(0, Math.min(100, ((readout.rssi + 120) / 80) * 100));
        this.displayMeterFill.style.width = `${level.toFixed(0)}%`;
        this.displayMeterValue.textContent = `${readout.rssi} dBm`;
      }

      // RSSI is already the meter, and the system type is already the badge.
      const tiles = (readout ? readout.rows : []).filter(
        ([name]) => name !== "RSSI" && name !== "System type"
      );
      fill(
        this.displayGrid,
        ...tiles.map(([name, value]) => {
          const tile = el("div", "display-tile");
          tile.appendChild(el("div", "tile-name", name));
          tile.appendChild(el("div", "tile-value", value));
          return tile;
        })
      );
    }

    /** Whatever the scanner is showing, as text.
     *
     *  Needed because GSI only describes scanning: menus, the weather-alert
     *  screen, the boot screen and every settings page exist only as display
     *  lines. Rendered as rows of text rather than as a 30-column grid --
     *  the scanner right-aligns its second column inside each line with
     *  padding spaces, so a line is split on runs of two or more spaces and
     *  the pieces are pushed to opposite ends, which survives any font.
     *  Reverse-video runs (the scanner's own way of marking a selection)
     *  become highlighted spans.
     *
     *  Collapsed by default, and it stays however you left it: a disclosure
     *  that opens itself moves the rest of the panel out from under your
     *  finger, and no reading of "the readout has nothing to say" was ever
     *  reliable enough to be worth that. */
    _renderScreenText(lines) {
      const rows = lines
        .map((line) => this._screenRow(line))
        .filter(Boolean);
      fill(this.screen, ...rows);
      if (!rows.length) {
        this.screen.appendChild(el("div", "screen-empty", "waiting for the scanner…"));
      }
    }

    /** One display line: its runs, split into a left and a right group on
     *  the scanner's own column padding. */
    _screenRow(line) {
      const text = line.text || "";
      if (!text.trim()) return null;
      const modes = line.mode || "";

      const row = el("div", "screen-row");
      // Two or more spaces is the scanner's own column gap; a single space
      // is a space inside a label ("Weather Alert").
      const columns = text.split(/ {2,}/).filter((part) => part.trim());
      let offset = 0;
      columns.forEach((column) => {
        const start = text.indexOf(column, offset);
        offset = start + column.length;
        const group = el("span", "screen-col");
        // Runs of the same per-character mode, so a highlighted selection
        // stays one span instead of one per character.
        let runStart = 0;
        for (let i = 1; i <= column.length; i++) {
          const here = modes[start + i] || " ";
          if (i < column.length && here === (modes[start + runStart] || " ")) continue;
          const mode = modes[start + runStart] || " ";
          const run = column.slice(runStart, i);
          group.appendChild(mode === " " ? document.createTextNode(run) : el("span", mode === "*" ? "rev" : "und", run));
          runStart = i;
        }
        row.appendChild(group);
      });
      return row;
    }

    /** The three soft keys, labelled the way the scanner is currently
     *  labelling them (protocol.py's _parse_soft_keys reads the row off the
     *  display). On the weather-alert screen one of them says "TO SCAN";
     *  hardcoded "System / Dept / Channel" labels would be a lie there. */
    _renderSoftKeys(labels) {
      this.softButtons.forEach((button, index) => {
        const label = (labels[index] || "").trim();
        button.textContent = label || SOFT_KEYS[index].label;
      });
    }

    /** What the permanent-avoid key is currently aimed at.
     *
     *  The label says what it does; the tooltip says what it would do it to,
     *  which is the part that changes every time the scanner moves on. A
     *  screen with no indexed entry on it (a menu, a search) leaves nothing
     *  for AVD to name, and the key goes disabled with a tooltip saying so
     *  rather than sitting there looking pressable. */
    _renderAvoid(readout) {
      const target = (readout && readout.avoidTarget) || null;
      // Only while something is being received. This presses the unit's own
      // key, and a free-scanning scanner steps through channels many times a
      // second -- the channel named on screen is gone before the press
      // lands, so it catches something else. Stopped on a transmission it is
      // exact, and that is also when you would want it: you avoid what you
      // are hearing.
      const live = Boolean(readout && readout.receiving);
      this.avoidTarget = target && live ? target : null;
      this.avoidButton.disabled = !this.avoidTarget;
      this.avoidButton.title = !target
        ? "Nothing on screen to avoid — no channel with a list index."
        : !live
          ? "Only while the scanner is stopped on a transmission — mid-scan the key press " +
            "would land on whatever channel it had moved to."
          : target.avoided
            ? `${avoidName(target)} is already avoided.`
            : `Avoid ${avoidName(target)} for good, by pressing the scanner's own Avoid key ` +
              "twice. The Avoid key alone is the temporary one.";
    }

    /** The closed panel's one-line summary of the same readout. */
    _renderNowPlaying(readout) {
      if (!readout) {
        this.nowLabel.textContent = "—";
        this.nowBadge.hidden = true;
        this.root.classList.remove("receiving");
        fill(this.nowPlaying);
        return;
      }

      this.nowLabel.textContent =
        [readout.channel, readout.department, readout.system].filter(Boolean).join(" / ") ||
        "scanning…";
      this.root.classList.toggle("receiving", readout.receiving);

      fill(
        this.nowPlaying,
        ...readout.rows.map(([name, value]) => {
          const field = el("div", "np-field");
          field.appendChild(el("span", "np-name", name));
          field.appendChild(el("span", "np-value", value));
          return field;
        })
      );

      this.nowBadge.hidden = !readout.systemType;
      this.nowBadge.textContent = readout.systemType;
    }

    _syncSliders() {
      const property = (this.status.gsi || {}).Property || {};
      const apply = (control, value) => {
        if (this.holdingSlider === control.name) return;
        const level = Number(value);
        if (!Number.isFinite(level)) return;
        control.input.value = String(level);
        control.readout.textContent = String(level);
      };
      apply(this.volume, property.VOL);
      apply(this.squelch, property.SQL);
    }
  }

  SDS.ControlPanel = ControlPanel;
})(window.SDS);
