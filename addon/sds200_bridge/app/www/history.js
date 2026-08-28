/* History tab: every call the add-on has reconstructed from the GSI poll
 * stream (see app/history.py).
 *
 * The list is fetched once and then kept current from the WebSocket's
 * "reception" messages, so a call appears the moment it starts rather than
 * on the next poll of this page. Filtering re-queries the server instead of
 * filtering client-side, and that is not an optimisation: the log runs to
 * months and hundreds of thousands of calls, so the browser only ever holds
 * the page being looked at, and every filter -- text, scanner, mode, date
 * range -- resolves in SQL against the whole log.
 */

"use strict";

(function (SDS) {
  const { el, fill, api, formatTime, formatDuration, formatFrequency } = SDS;

  // Why a call has no transcript. Shown instead of an empty space, because
  // "nobody spoke", "the model was not confident enough to record what it
  // thought it heard" and "the speech-to-text server could not be reached"
  // are three different facts and only the last one is a problem to fix.
  const TRANSCRIPT_REASONS = {
    // Not a reason there is no transcript -- a statement that one is coming.
    // The gap between a call ending and its transcript arriving is a model's
    // runtime plus a queue, and for all of it the row used to be blank, which
    // is what a call nobody tried to transcribe also looks like.
    pending: "transcribing\u2026",
    "no-speech": "no speech",
    "too-short": "too short to transcribe",
    doubtful: "discarded — too uncertain to trust",
    artifact: "discarded — the model returned boilerplate",
    repeated: "discarded — the model repeated itself",
    error: "the speech-to-text server could not be reached",
    "no-call": "no matching call",
    "no-audio": "no audio — squelch opened but nothing was said",
    // Same status, different truth depending on the mode. See _noAudioReason.
    // Only reachable where the squelch is still deciding what a call is. Once
    // the scanner's mute flag is answering, a row exists because it unmuted,
    // so "a talkgroup it is not monitoring" cannot be the explanation.
    "no-audio-digital": "signal, but no audio reached the add-on — check this scanner's live audio",
    "no-feed": "no audio reaching the add-on — check this scanner's live audio",
    // Not a fact about the transmission: we heard it and gave up on it.
    dropped: "audio discarded before it could be transcribed",
  };

  const PAGE_SIZE = 200;

  class HistoryView {
    constructor({ onCreateAction } = {}) {
      this.root = el("div", "history");
      // Handed in rather than reached for through SDS.app: this view knows
      // which call was clicked, the shell knows how to get to the Actions
      // tab, and neither needs to know the other's internals.
      this.onCreateAction = onCreateAction || null;
      this.records = [];
      this.filters = { scanner: "", q: "", mode: "", since: "", until: "" };
      this.total = 0;
      this.oldest = null;
      this.scanners = [];
      this._build();
    }

    _build() {
      const bar = el("div", "card filter-bar");
      const fields = el("div", "filter-fields");

      this.search = el("input");
      this.search.type = "search";
      this.search.placeholder = "Search channel, talkgroup, system, unit, date…";
      this.search.addEventListener(
        "input",
        SDS.debounce(() => {
          this.filters.q = this.search.value;
          this.refresh();
        }, 250)
      );
      fields.appendChild(this._field("Search", this.search));

      this.scannerSelect = el("select");
      this.scannerSelect.addEventListener("change", () => {
        this.filters.scanner = this.scannerSelect.value;
        this.refresh();
      });
      fields.appendChild(this._field("Scanner", this.scannerSelect));

      this.modeSelect = el("select");
      this.modeSelect.addEventListener("change", () => {
        this.filters.mode = this.modeSelect.value;
        this.refresh();
      });
      fields.appendChild(this._field("Mode", this.modeSelect));

      // datetime-local rather than date: with months in the log, "that
      // afternoon" is as common a question as "that day". The picker always
      // sends a time with the date; /api/history also accepts a bare date
      // (and stretches it to the end of the day as an upper bound) for
      // anything querying it directly.
      this.since = el("input");
      this.since.type = "datetime-local";
      this.since.addEventListener("change", () => {
        this.filters.since = this.since.value;
        this.refresh();
      });
      fields.appendChild(this._field("From", this.since));

      this.until = el("input");
      this.until.type = "datetime-local";
      this.until.addEventListener("change", () => {
        this.filters.until = this.until.value;
        this.refresh();
      });
      fields.appendChild(this._field("To", this.until));

      bar.appendChild(fields);

      // Both buttons say "clear" and only one of them destroys anything, so
      // they are not the same button twice: the reset is a plain secondary,
      // the delete carries the danger outline.
      const actions = el("div", "filter-actions");
      this.resetButton = el("button", "secondary", "Clear filters");
      this.resetButton.type = "button";
      this.resetButton.addEventListener("click", () => this._resetFilters());
      actions.appendChild(this.resetButton);

      const clear = el("button", "danger", "Clear history");
      clear.type = "button";
      clear.addEventListener("click", () => this._clear());
      actions.appendChild(clear);
      bar.appendChild(actions);
      this._syncReset();

      this.root.appendChild(bar);

      this.count = el("p", "sub", "");
      this.root.appendChild(this.count);

      this.list = el("div", "history-list");
      this.root.appendChild(this.list);
    }

    _field(label, control) {
      const wrap = el("label", "filter-field");
      wrap.appendChild(el("span", null, label));
      wrap.appendChild(control);
      return wrap;
    }

    setScanners(scanners) {
      this.scanners = scanners;
      const current = this.scannerSelect.value;
      fill(this.scannerSelect, this._option("", "All scanners"));
      scanners.forEach((s) => this.scannerSelect.appendChild(this._option(s.id, s.name || s.id)));
      this.scannerSelect.value = current;
      // Rows carry the scanner's name, so a rename (or the runtime list
      // simply arriving after the first render) has to redraw them.
      if (this.records.length) this.render();
    }

    _option(value, label) {
      const option = el("option", null, label);
      option.value = value;
      return option;
    }

    _setModes(modes) {
      if (this.modeSelect.options.length) return;
      fill(this.modeSelect, this._option("", "Any mode"));
      modes.forEach((mode) => this.modeSelect.appendChild(this._option(mode, mode)));
    }

    _anyFilter() {
      return Boolean(
        this.filters.q || this.filters.mode || this.filters.scanner ||
          this.filters.since || this.filters.until
      );
    }

    _resetFilters() {
      this.filters = { scanner: "", q: "", mode: "", since: "", until: "" };
      this.search.value = "";
      this.scannerSelect.value = "";
      this.modeSelect.value = "";
      this.since.value = "";
      this.until.value = "";
      this.refresh();
    }

    /** Visible but dead when there is nothing set: hiding it would make the
     *  bar reflow every time a filter comes and goes. */
    _syncReset() {
      this.resetButton.disabled = !this._anyFilter();
    }

    async refresh() {
      this._syncReset();
      const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
      if (this.filters.scanner) params.set("scanner", this.filters.scanner);
      // Finished calls only. A call in progress is a row whose duration
      // keeps changing and whose transcript cannot exist yet -- that belongs
      // in the live view, which is where it already is.
      params.set("finished", "1");
      if (this.filters.q) params.set("q", this.filters.q);
      if (this.filters.mode) params.set("mode", this.filters.mode);
      if (this.filters.since) params.set("since", this.filters.since);
      if (this.filters.until) params.set("until", this.filters.until);
      try {
        const data = await api(`api/history?${params}`);
        this._setModes(data.modes || []);
        this.records = data.records || [];
        this.total = data.total || this.records.length;
        this.oldest = data.oldest || null;
        this.render();
      } catch (err) {
        fill(this.list, el("p", "message error", `Could not load the history: ${err.message}`));
      }
    }

    /** A "reception" push: a call ending, or a transcript arriving for one.
     *
     *  A call *starting* is deliberately ignored. The row exists from the
     *  first poll and is rewritten as the call runs, which is what the live
     *  view is for; here it would be a row whose duration keeps changing,
     *  whose transcript cannot exist yet, and which may still turn out to be
     *  two calls or none. The log shows what happened, so a call joins it
     *  when it is over.
     *
     *  Only prepends when nothing is being filtered on -- a new call that
     *  doesn't match the active filter has no business appearing in a
     *  filtered list, and re-querying on every call would fight with typing
     *  in the search box.
     *
     *  A "transcript" needs no case of its own: it lands seconds to minutes
     *  after the call ended, by which point the row is on screen, and
     *  replacing the record in place is exactly the right thing. Without the
     *  push the row would show a blank transcript until something else forced
     *  a refresh. */
    onReception(message) {
      const record = message.record;
      if (!record) return;
      if (message.event === "start") {
        return;
      } else if (message.event === "dropped") {
        // The scanner never played this one and is set to log only what it
        // did. Taking it off the page rather than leaving it until a refresh
        // keeps the list matching what a reload would show.
        const index = this.records.findIndex((r) => r.id === record.id);
        if (index === -1) return;
        this.records.splice(index, 1);
        this.total = Math.max(0, this.total - 1);
      } else {
        const index = this.records.findIndex((r) => r.id === record.id);
        if (index === -1) {
          // The call ending is what puts it on the page, so this is the
          // ordinary path rather than the odd one.
          if (message.event !== "end") return;
          if (this.filters.q || this.filters.mode || this.filters.since || this.filters.until) {
            return;
          }
          if (this.filters.scanner && record.scanner_id !== this.filters.scanner) return;
          this.records.unshift(record);
          this.records = this.records.slice(0, PAGE_SIZE);
          this.total += 1;
          this.render();
          return;
        }
        // Keep any transcript already on screen unless this push carries one.
        // An "end" push is built from the poll snapshots the call was
        // reconstructed from and has never held a transcript field, so
        // replacing wholesale blanks a transcript that had already arrived --
        // the call ends *after* the audio does, so that is the common order.
        const previous = this.records[index];
        const merged = { ...record };
        if (merged.transcript_status === undefined || merged.transcript_status === null) {
          merged.transcript = previous.transcript;
          merged.transcript_status = previous.transcript_status;
          merged.clip = previous.clip;
        }
        this.records[index] = merged;
        // Just that row, not the whole page. A transcript lands seconds
        // after the call it belongs to, so every call redraws the list a
        // second time -- two hundred rows torn down and rebuilt for one
        // changed line, which is visible as a flash and loses the selection
        // and the scroll position with it.
        this._redrawRow(index, merged);
        return;
      }
      this.render();
    }

    render() {
      if (!this.records.length) {
        fill(
          this.list,
          el(
            "p",
            "empty",
            this._anyFilter()
              ? "No calls match that filter."
              : "Nothing heard yet. Calls appear here as the scanner receives them."
          )
        );
        this.count.textContent = "";
        return;
      }
      // The page is a window onto a log that now runs to months, so say how
      // much is behind it -- "200 calls" on its own reads as "that's all
      // there is" when it means "that's all we sent".
      const total = Math.max(this.total, this.records.length);
      const shown =
        total > this.records.length
          ? `${this.records.length} of ${total.toLocaleString()} calls`
          : `${total} call${total === 1 ? "" : "s"}`;
      const back =
        this.oldest && !this._anyFilter() ? ` · history goes back to ${formatTime(this.oldest)}` : "";
      this.count.textContent = shown + back;
      fill(this.list, ...this.records.map((record) => this._row(record)));
    }

    /** Swap one rendered row in place, or fall back to a full redraw.
     *
     *  The fallback matters: the list is only row-per-record while nothing
     *  else is on screen -- the empty-state paragraph is a single child, and
     *  a redraw that assumed otherwise would write a row into the middle of
     *  a message. */
    _redrawRow(index, record) {
      const existing = this.list.children[index];
      if (!existing || this.list.children.length !== this.records.length) {
        this.render();
        return;
      }
      this.list.replaceChild(this._row(record), existing);
    }

    _row(record) {
      const row = el("article", "history-row");

      const when = el("div", "hr-when");
      when.appendChild(el("time", null, formatTime(record.started)));
      when.appendChild(
        el(
          "small",
          null,
          formatDuration(record.duration) + (record.interrupted ? " (cut off)" : "")
        )
      );
      row.appendChild(when);

      const main = el("div", "hr-main");
      main.appendChild(el("div", "hr-label", record.label || "unknown"));

      const meta = el("div", "hr-meta");
      const chip = (text, className) => {
        if (!text) return;
        meta.appendChild(el("span", `chip ${className || ""}`.trim(), text));
      };
      // First, and always -- including with a single scanner configured. The
      // history outlives the scanner list it was recorded against (records
      // for a since-removed scanner stay in the log), so hiding it whenever
      // there happens to be one scanner now would leave those rows unable to
      // say where they came from.
      chip(this._scannerName(record.scanner_id), "scanner");
      chip(record.mode, `mode-${record.mode}`);
      chip(record.frequency !== null ? formatFrequency(record.frequency) : "");
      chip(record.mod);
      chip(record.tgid ? `TG ${record.tgid}` : "");
      chip(record.sub_audio);
      chip((record.unit_ids || []).length ? `Unit ${record.unit_ids.join(", ")}` : "");
      main.appendChild(meta);

      const speech = this._transcript(record);
      if (speech) main.appendChild(speech);
      row.appendChild(main);

      if (record.rssi_peak !== null && record.rssi_peak !== undefined) {
        row.appendChild(el("div", "hr-rssi", `${record.rssi_peak} dBm`));
      }

      if (this.onCreateAction) {
        const create = el("button", "secondary", "Create action");
        create.type = "button";
        create.title =
          "Start an action prefilled with this call's system, department, channel and talkgroup.";
        create.addEventListener("click", () => this.onCreateAction(record));
        const cell = el("div", "hr-actions");
        cell.appendChild(create);
        row.appendChild(cell);
      }
      return row;
    }

    /** The transcript line, or the reason there isn't one.
     *
     *  A rejected transcript is shown as *why* rather than as nothing: a call
     *  nobody spoke in, one the model declined as too doubtful, and one where
     *  the server was unreachable are three different facts, and all three
     *  look identical as a blank row. The play button is offered whenever a
     *  clip was kept -- including for rejections, which is exactly when
     *  someone wants to hear what the model was given and judge whether it
     *  was right to refuse. */
    _transcript(record) {
      const status = record.transcript_status;
      if (!status) return null;

      const wrap = el("div", "hr-transcript");
      if (record.clip) {
        const play = el("button", "linkish", "▶");
        play.type = "button";
        play.title = "Play what the scanner actually heard.";
        play.addEventListener("click", () => this._play(record, play));
        wrap.appendChild(play);
      }
      if (record.transcript) {
        wrap.appendChild(el("q", "hr-speech", record.transcript));
      } else {
        wrap.appendChild(el("span", "hr-nospeech", this._reason(record, status)));
      }
      return wrap;
    }

    /** Why there is no transcript, in the words that are true for this call.
     *
     *  "Squelch opened but nothing was said" is right on a conventional
     *  channel, where RF and speech amount to the same thing. On a trunked
     *  digital system they do not: the receiver has signal whenever the site
     *  is active, so the squelch-open test (RSSI, see reception.py) reports a
     *  call, while the scanner only unmutes for a talkgroup that is actually
     *  being monitored. Those rows are not a fault and nobody failed to
     *  speak -- somebody spoke on a talkgroup this scanner was not listening
     *  to. */
    _reason(record, status) {
      if (status === "no-audio" && record.mode && record.mode !== "analog") {
        return TRANSCRIPT_REASONS["no-audio-digital"];
      }
      return TRANSCRIPT_REASONS[status] || status;
    }

    /** One <audio> at a time, built on demand: a page of two hundred rows
     *  should not hold two hundred media elements open. */
    _play(record, button) {
      if (this._audio) {
        this._audio.pause();
        if (this._playing === record.id) {
          this._audio = null;
          this._playing = null;
          button.textContent = "▶";
          return;
        }
      }
      this.root.querySelectorAll(".hr-transcript button").forEach((b) => {
        b.textContent = "▶";
      });
      this._audio = new Audio(`api/history/${record.id}/audio`);
      this._playing = record.id;
      button.textContent = "◼";
      this._audio.addEventListener("ended", () => {
        button.textContent = "▶";
        this._playing = null;
      });
      this._audio.play().catch(() => {
        button.textContent = "▶";
        button.title = "That clip is no longer kept.";
      });
    }

    /** The configured name for a scanner, falling back to its id -- which is
     *  all there is for a record whose scanner has since been removed. */
    _scannerName(scannerId) {
      if (!scannerId) return "";
      const scanner = this.scanners.find((s) => s.id === scannerId);
      return (scanner && (scanner.name || scanner.id)) || scannerId;
    }

    async _clear() {
      const scope = this.filters.scanner
        ? `the history for ${this.filters.scanner}`
        : "the whole receive history";
      if (!window.confirm(`Delete ${scope}? This cannot be undone.`)) return;
      const params = this.filters.scanner ? `?scanner=${encodeURIComponent(this.filters.scanner)}` : "";
      try {
        await api(`api/history${params}`, { method: "DELETE" });
        await this.refresh();
      } catch (err) {
        fill(this.list, el("p", "message error", `Could not clear the history: ${err.message}`));
      }
    }
  }

  SDS.HistoryView = HistoryView;
})(window.SDS);
