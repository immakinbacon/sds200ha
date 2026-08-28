/* Settings tab: the scanners themselves, plus the add-on's own options.
 *
 * Scanners configured here are picked up straight away -- no add-on restart
 * -- and a scanner whose settings didn't change keeps its audio session
 * running untouched (see app/manager.py for why that distinction is worth
 * the code).
 */

"use strict";

(function (SDS) {
  const { el, fill, api, post, formatTime } = SDS;

  const BOOL_DEFAULTS = {
    poe_reset_use_ssl: true,
    poe_reset_verify_ssl: true,
    auto_reboot_on_audio_failure: false,
    auto_reboot_on_control_failure: false,
    transcribe_enabled: false,
    require_audio: true,
  };

  const POE_FIELDS = [
    "poe_reset_host",
    "poe_reset_username",
    "poe_reset_password",
    "poe_reset_interface",
  ];

  class SettingsView {
    constructor({ onSaved } = {}) {
      this.root = el("div", "settings");
      this.onSaved = onSaved || function () {};
      this.template = document.getElementById("scanner-template");
      // The value the API sends instead of a stored router password, and
      // accepts back to mean "unchanged" -- the real one is never sent to
      // the browser. Read from the API response rather than hardcoded, so
      // the two can't drift.
      this.passwordSentinel = "__stored__";
      this.logLevels = ["trace", "debug", "info", "warning", "error"];
      this.maxScanners = 8;
      this.historyDaysBounds = [1, 3650];
      this.historyRecordsBounds = [100, 10000000];
      this.gsiBounds = [1, 30];
      this.runtime = new Map();
      this.dirty = false;
      this._built = false;
    }

    async refresh() {
      let data;
      try {
        data = await api("api/settings");
      } catch (err) {
        fill(this.root, el("p", "message error", `Could not load settings: ${err.message}`));
        return;
      }
      this.passwordSentinel = data.password_sentinel || this.passwordSentinel;
      this.logLevels = data.log_levels || this.logLevels;
      this.maxScanners = data.max_scanners || this.maxScanners;
      this.historyDaysBounds = data.history_days_bounds || this.historyDaysBounds;
      this.historyRecordsBounds = data.history_records_bounds || this.historyRecordsBounds;
      this.gsiBounds = data.gsi_interval_bounds || this.gsiBounds;
      this.runtime = new Map((data.runtime || []).map((s) => [s.id, s]));

      if (!this._built) this._build(data.config);
      this._applyGeneral(data.config);
      this._renderScanners(data.config.scanners);
      this._showMessages({ errors: data.errors || [], warnings: data.warnings || [] });
      this._setStatus("");
      this.dirty = false;
      this.save.disabled = true;
    }

    /** Called on the page's runtime poll so the reachability dots stay
     *  current without re-rendering the form under the cursor. */
    setRuntime(runtime) {
      this.runtime = new Map((runtime || []).map((s) => [s.id, s]));
      this.root.querySelectorAll(".scanner").forEach((section) => this._refreshHeader(section));
    }

    _build(config) {
      this._built = true;
      this.messages = el("div", "messages");
      this.root.appendChild(this.messages);

      const general = el("section", "card");
      general.appendChild(el("h2", null, "Add-on"));

      const grid = el("div", "grid");

      const logLabel = el("label");
      logLabel.appendChild(el("span", null, "Log level"));
      this.logLevel = el("select");
      this.logLevels.forEach((level) => {
        const option = el("option", null, level);
        option.value = level;
        this.logLevel.appendChild(option);
      });
      this.logLevel.addEventListener("change", () => this._markDirty());
      logLabel.appendChild(this.logLevel);
      logLabel.appendChild(el("small", null, "Applies immediately on save; no restart needed."));
      grid.appendChild(logLabel);

      const daysLabel = el("label");
      daysLabel.appendChild(el("span", null, "Keep history for"));
      this.historyDays = el("input");
      this.historyDays.type = "number";
      this.historyDays.min = String(this.historyDaysBounds[0]);
      this.historyDays.max = String(this.historyDaysBounds[1]);
      this.historyDays.addEventListener("input", () => this._markDirty());
      daysLabel.appendChild(this.historyDays);
      daysLabel.appendChild(
        el(
          "small",
          null,
          `Days. Calls older than this are deleted. Up to ${this.historyDaysBounds[1]} (ten years).`
        )
      );
      grid.appendChild(daysLabel);

      const capLabel = el("label");
      capLabel.appendChild(el("span", null, "Never keep more than"));
      this.historyMaxRecords = el("input");
      this.historyMaxRecords.type = "number";
      this.historyMaxRecords.min = String(this.historyRecordsBounds[0]);
      this.historyMaxRecords.max = String(this.historyRecordsBounds[1]);
      this.historyMaxRecords.addEventListener("input", () => this._markDirty());
      capLabel.appendChild(this.historyMaxRecords);
      capLabel.appendChild(
        el(
          "small",
          null,
          "Calls. A disk safety net behind the window above — it only applies if the " +
            "scanner is busy enough to record this many within it. Oldest go first."
        )
      );
      grid.appendChild(capLabel);
      general.appendChild(grid);

      const historyCheck = el("label", "check");
      this.historyEnabled = el("input");
      this.historyEnabled.type = "checkbox";
      this.historyEnabled.addEventListener("change", () => this._markDirty());
      const historyText = el("span");
      historyText.appendChild(document.createTextNode("Record a receive history"));
      historyText.appendChild(
        el(
          "small",
          null,
          "Off stops both the history and the actions — actions fire off the same call detection."
        )
      );
      historyCheck.appendChild(this.historyEnabled);
      historyCheck.appendChild(historyText);
      const checks = el("div", "checks");
      checks.appendChild(historyCheck);
      general.appendChild(checks);
      this.root.appendChild(general);

      this.root.appendChild(this._buildTranscribe());

      this.list = el("div");
      this.list.id = "scanners";
      this.root.appendChild(this.list);

      const addRow = el("p");
      this.addButton = el("button", "secondary", "Add scanner");
      this.addButton.type = "button";
      this.addButton.addEventListener("click", () => {
        this.list.appendChild(this._buildScanner({}));
        this._markDirty();
        this._renderEmptyState();
      });
      addRow.appendChild(this.addButton);
      this.root.appendChild(addRow);

      const bar = el("div", "inline-savebar");
      this.save = el("button", "primary", "Save settings");
      this.save.type = "button";
      this.save.disabled = true;
      this.save.addEventListener("click", () => this._save());
      this.status = el("span", "status");
      bar.appendChild(this.save);
      bar.appendChild(this.status);
      this.root.appendChild(bar);
    }

    /** The speech-to-text server. Global rather than per scanner: the model
     *  is a service on the network, and pointing two scanners at two of them
     *  is a configuration nobody has asked for. Which scanners get sent to it
     *  is a per-scanner checkbox. */
    _buildTranscribe() {
      const card = el("section", "card");
      card.appendChild(el("h2", null, "Transcription"));
      card.appendChild(
        el(
          "p",
          "note",
          "Where speech-to-text happens. Nothing is transcribed until at least one " +
            "scanner is switched on for it, further down."
        )
      );

      const grid = el("div", "grid");

      const backendLabel = el("label");
      backendLabel.appendChild(el("span", null, "Speech-to-text server"));
      this.sttBackend = el("select");
      [
        ["wyoming", "Home Assistant Whisper add-on"],
        ["openai", "OpenAI-compatible server"],
      ].forEach(([value, text]) => {
        const option = el("option", null, text);
        option.value = value;
        this.sttBackend.appendChild(option);
      });
      this.sttBackend.addEventListener("change", () => {
        this._refreshSttBackend();
        this._markDirty();
      });
      backendLabel.appendChild(this.sttBackend);
      this.sttBackendHelp = el("small");
      backendLabel.appendChild(this.sttBackendHelp);
      grid.appendChild(backendLabel);

      const urlLabel = el("label");
      urlLabel.appendChild(el("span", null, "Address"));
      this.sttUrl = el("input");
      this.sttUrl.type = "text";
      this.sttUrl.addEventListener("input", () => this._markDirty());
      urlLabel.appendChild(this.sttUrl);
      this.sttUrlHelp = el("small");
      urlLabel.appendChild(this.sttUrlHelp);
      grid.appendChild(urlLabel);

      this.sttModelLabel = el("label");
      this.sttModelLabel.appendChild(el("span", null, "Model"));
      this.sttModel = el("input");
      this.sttModel.type = "text";
      this.sttModel.placeholder = "small.en";
      this.sttModel.addEventListener("input", () => this._markDirty());
      this.sttModelLabel.appendChild(this.sttModel);
      this.sttModelLabel.appendChild(
        el("small", null, "Larger models are more accurate and slower. small.en is a good start.")
      );
      grid.appendChild(this.sttModelLabel);

      const langLabel = el("label");
      langLabel.appendChild(el("span", null, "Language"));
      this.sttLanguage = el("input");
      this.sttLanguage.type = "text";
      this.sttLanguage.placeholder = "en";
      this.sttLanguage.addEventListener("input", () => this._markDirty());
      langLabel.appendChild(this.sttLanguage);
      grid.appendChild(langLabel);

      const gapLabel = el("label");
      gapLabel.appendChild(el("span", null, "End a transmission after (seconds of quiet)"));
      this.sttGap = el("input");
      this.sttGap.type = "number";
      this.sttGap.step = "0.1";
      this.sttGap.min = "0.2";
      this.sttGap.max = "5";
      this.sttGap.addEventListener("input", () => this._markDirty());
      gapLabel.appendChild(this.sttGap);
      gapLabel.appendChild(
        el(
          "small",
          null,
          "Too short cuts people off at an ordinary pause mid-sentence; too long merges " +
            "two separate transmissions into one clip, which is the lesser harm. Costs " +
            "only delay — the quiet itself is trimmed off before transcribing."
        )
      );
      grid.appendChild(gapLabel);

      const capLabel2 = el("label");
      capLabel2.appendChild(el("span", null, "Longest clip (seconds)"));
      this.sttMaxSegment = el("input");
      this.sttMaxSegment.type = "number";
      this.sttMaxSegment.min = "5";
      this.sttMaxSegment.max = "120";
      this.sttMaxSegment.addEventListener("input", () => this._markDirty());
      capLabel2.appendChild(this.sttMaxSegment);
      capLabel2.appendChild(
        el(
          "small",
          null,
          "A backstop for the voice detector failing to close, not a limit on how long " +
            "anyone may speak — a clip that hits it is still transcribed, so a long " +
            "message ends up split across two rows rather than lost. Lower costs less " +
            "when the detector misbehaves; higher avoids splitting long traffic."
        )
      );
      grid.appendChild(capLabel2);

      const promptLabel = el("label");
      promptLabel.appendChild(el("span", null, "Vocabulary hints"));
      this.sttPrompt = el("input");
      this.sttPrompt.type = "text";
      this.sttPrompt.placeholder = "e.g. Ravenna, Portage County, Engine 12, Medic 4";
      this.sttPrompt.addEventListener("input", () => this._markDirty());
      promptLabel.appendChild(this.sttPrompt);
      promptLabel.appendChild(
        el(
          "small",
          null,
          "Local agency names, street names and unit designators — the words a general " +
            "speech model has never heard and will otherwise guess at. Keep it to a handful: " +
            "this nudges the model rather than constraining it, and a long list can push it " +
            "into inventing the words you listed."
        )
      );
      grid.appendChild(promptLabel);
      card.appendChild(grid);

      const upCheck = el("label", "check");
      this.sttUpsample = el("input");
      this.sttUpsample.type = "checkbox";
      this.sttUpsample.addEventListener("change", () => this._markDirty());
      const upText = el("span");
      upText.appendChild(document.createTextNode("Convert to 16 kHz before sending"));
      upText.appendChild(
        el(
          "small",
          null,
          "Scanner audio is 8 kHz; speech models want 16 kHz. On means the add-on " +
            "converts it — certain, but a crude conversion. Off sends 8 kHz and lets " +
            "the server do it, which is better if it does it at all and produces " +
            "nothing if it ignores the rate. Try the other setting if transcripts come " +
            "back empty."
        )
      );
      upCheck.appendChild(this.sttUpsample);
      upCheck.appendChild(upText);
      const upRow = el("div", "checks");
      upRow.appendChild(upCheck);
      card.appendChild(upRow);

      const testRow = el("p");
      this.sttTest = el("button", "secondary", "Test connection");
      this.sttTest.type = "button";
      this.sttTest.addEventListener("click", () => this._testStt());
      this.sttTestStatus = el("span", "status");
      testRow.appendChild(this.sttTest);
      testRow.appendChild(this.sttTestStatus);
      card.appendChild(testRow);

      return card;
    }

    /** Wyoming picks its model in the Whisper add-on's own configuration, so
     *  showing a model field here would offer a setting that silently does
     *  nothing. */
    _refreshSttBackend() {
      const wyoming = this.sttBackend.value === "wyoming";
      this.sttModelLabel.hidden = wyoming;
      this.sttBackendHelp.textContent = wyoming
        ? "Install \u201cWhisper\u201d from the add-on store. Returns text only \u2014 no " +
          "per-result confidence, so a weak transcript cannot be filtered out automatically."
        : "Any server exposing /v1/audio/transcriptions. Returns confidence scores, which " +
          "lets doubtful transcripts be discarded rather than recorded.";
      this.sttUrlHelp.textContent = wyoming
        ? "host:port of the Whisper add-on, e.g. 198.51.100.5:10300. Its Network settings have " +
          "to expose that port."
        : "Base URL, e.g. http://nuc:8000";
      this.sttUrl.placeholder = wyoming ? "198.51.100.5:10300" : "http://nuc:8000";
    }

    async _testStt() {
      this.sttTest.disabled = true;
      this.sttTestStatus.textContent = "Testing\u2026";
      try {
        const result = await post("api/transcribe/test", {
          backend: this.sttBackend.value,
          url: this.sttUrl.value.trim(),
          model: this.sttModel.value.trim(),
          language: this.sttLanguage.value.trim(),
        });
        this.sttTestStatus.textContent = result.ok
          ? `Reached it in ${Math.round(result.elapsed * 1000)}ms.`
          : `No: ${result.error}`;
      } catch (err) {
        this.sttTestStatus.textContent = `No: ${err.message}`;
      } finally {
        this.sttTest.disabled = false;
      }
    }

    _applyGeneral(config) {
      this.logLevel.value = config.log_level;
      this.historyEnabled.checked = config.history.enabled;
      this.historyDays.value = config.history.retention_days;
      this.historyMaxRecords.value = config.history.max_records;

      const transcribe = config.transcribe || {};
      this.sttBackend.value = transcribe.backend || "wyoming";
      this.sttUrl.value = transcribe.url || "";
      this.sttModel.value = transcribe.model || "";
      this.sttLanguage.value = transcribe.language || "en";
      this.sttPrompt.value = transcribe.prompt || "";
      this.sttMaxSegment.value = transcribe.max_segment_s || 30;
      this.sttGap.value = transcribe.silence_gap_s || 1.2;
      this.sttUpsample.checked = transcribe.upsample !== false;
      this._refreshSttBackend();
    }

    _markDirty() {
      this.dirty = true;
      this.save.disabled = false;
      this._setStatus("Unsaved changes.");
    }

    _setStatus(text) {
      this.status.textContent = text;
    }

    _renderScanners(scanners) {
      fill(this.list);
      scanners.forEach((scanner) => this.list.appendChild(this._buildScanner(scanner)));
      this._renderEmptyState();
    }

    _renderEmptyState() {
      const existing = this.list.querySelector(".empty");
      const hasScanners = this.list.querySelector(".scanner") !== null;
      if (hasScanners && existing) existing.remove();
      if (!hasScanners && !existing) {
        this.list.appendChild(
          el("p", "empty", "No scanners configured yet. Add one to get started.")
        );
      }
      this.addButton.disabled = this.list.querySelectorAll(".scanner").length >= this.maxScanners;
    }

    _buildScanner(scanner) {
      const section = this.template.content.firstElementChild.cloneNode(true);

      const interval = section.querySelector('[data-field="gsi_poll_interval"]');
      interval.min = String(this.gsiBounds[0]);
      interval.max = String(this.gsiBounds[1]);

      section.querySelectorAll("[data-field]").forEach((input) => {
        const field = input.dataset.field;
        if (input.type === "checkbox") {
          input.checked = scanner[field] !== undefined ? !!scanner[field] : BOOL_DEFAULTS[field];
        } else {
          input.value =
            scanner[field] !== undefined && scanner[field] !== null ? scanner[field] : "";
        }
        input.addEventListener(input.type === "checkbox" ? "change" : "input", () => {
          this._markDirty();
          this._refreshHeader(section);
        });
      });

      const password = section.querySelector('[data-field="poe_reset_password"]');
      const hint = section.querySelector("[data-password-hint]");
      if (password.value === this.passwordSentinel) {
        // Never render the sentinel into the field: it would be saved back
        // as a literal password the moment anything else on the row is
        // edited. Empty field plus an explicit note instead.
        password.value = "";
        password.placeholder = "•••••••• (saved)";
        hint.textContent = "A password is saved. Leave this blank to keep it, or type a new one.";
        password.dataset.stored = "true";
      } else {
        hint.textContent = "Stored in the add-on's own data, not in Home Assistant's config.";
      }

      section.querySelector('[data-action="remove"]').addEventListener("click", () => {
        section.remove();
        this._markDirty();
        this._renderEmptyState();
      });

      // Bound here rather than in _loadAvoids, which re-runs after every
      // un-avoid and would stack a fresh listener each time.
      section
        .querySelector('[data-action="verify-avoids"]')
        .addEventListener("click", () => this._verifyAvoids(section, false));
      section
        .querySelector('[data-action="sweep-avoids"]')
        .addEventListener("click", () => this._verifyAvoids(section, true));

      this._refreshHeader(section);
      this._loadAvoids(section);
      return section;
    }

    /** The avoided-channel list for one scanner.
     *
     *  Fetched per section rather than carried in the settings payload: it
     *  is live state that the Control tab changes, not configuration, so it
     *  has no business making the form dirty or being POSTed back on save.
     *  Everything here is read and written straight against the running
     *  add-on. */
    async _loadAvoids(section) {
      const box = section.querySelector("[data-avoid-list]");
      const badge = section.querySelector("[data-avoid-badge]");
      const id = SDS.slugify(section.querySelector('[data-field="name"]').value.trim());

      // No id yet (a scanner being added), or one that isn't running (a
      // rename that hasn't been saved): there is no API to ask. Say which,
      // rather than render an empty list that reads as "nothing avoided".
      if (!id || !this.runtime.has(id)) {
        badge.textContent = "—";
        fill(box, el("p", "note", "Save this scanner first — its avoided channels are read from the running add-on."));
        return;
      }

      try {
        const data = await api(`scanners/${id}/avoids`);
        // A fresh list of records makes any previous verification stale --
        // it was read against the rows as they were before the un-avoid
        // that triggered this reload. The swept list goes with it: it was
        // "what no record accounts for", and the records just changed.
        section._avoidStates = null;
        fill(section.querySelector("[data-avoid-unrecorded]"));
        section.querySelector("[data-avoid-status]").textContent = "";
        this._renderAvoids(section, id, data.avoids || []);
      } catch (err) {
        badge.textContent = "?";
        fill(box, el("p", "message error", `Could not read the avoided channels: ${err.message}`));
      }
    }

    /** Ask the scanner what it is really avoiding, and say where that
     *  differs from the log.
     *
     *  Two costs behind one button each. The plain check reads back only
     *  the departments the log mentions, which is quick. The sweep walks
     *  the entire database -- the only way to find an avoid nothing here
     *  recorded, and minutes of reads -- so it says so before it starts and
     *  is never run on its own initiative. */
    async _verifyAvoids(section, sweep) {
      const id = SDS.slugify(section.querySelector('[data-field="name"]').value.trim());
      const status = section.querySelector("[data-avoid-status]");
      const unrecordedBox = section.querySelector("[data-avoid-unrecorded]");
      const buttons = section.querySelectorAll(
        '[data-action="verify-avoids"], [data-action="sweep-avoids"]'
      );
      const setStatus = (text, isError) => {
        status.textContent = text;
        status.classList.toggle("error-text", !!isError);
      };

      if (!id || !this.runtime.has(id)) {
        setStatus("Save this scanner first — the check is read from the running add-on.", true);
        return;
      }

      buttons.forEach((b) => (b.disabled = true));
      setStatus(
        sweep
          ? "Walking the whole database — several thousand reads, a few minutes. The scanner " +
            "keeps scanning throughout."
          : "Reading the scanner…"
      );

      try {
        const data = await api(`scanners/${id}/avoids/verify${sweep ? "?sweep=true" : ""}`);
        section._avoidStates = new Map((data.checked || []).map((c) => [c.id, c]));
        this._renderAvoids(section, id, data.checked || []);
        this._renderUnrecorded(unrecordedBox, data);

        const total = (data.checked || []).length;
        // A record the scanner never answered for is neither agreement nor
        // disagreement, and saying "8 of 8 disagree" when the radio simply
        // missed a read points at the wrong thing entirely.
        const unknown = data.unknown || 0;
        const answered = total - unknown;
        const parts = [];
        if (data.disagree) {
          parts.push(
            `${data.disagree} of ${total} record${total === 1 ? "" : "s"} ` +
              `disagree${data.disagree === 1 ? "s" : ""} with the scanner`
          );
        } else if (answered) {
          parts.push(`all ${answered} record${answered === 1 ? "" : "s"} agree with the scanner`);
        }
        if (unknown) {
          parts.push(
            unknown === total
              ? `the scanner did not answer for any of the ${total} record${total === 1 ? "" : "s"}`
              : `${unknown} could not be read back`
          );
        }

        let sweptNothing = false;
        if (data.swept) {
          const c = data.counts || {};
          const failed = (data.failures || []).length;
          // No systems read means the walk never started, so it has
          // nothing to say about unrecorded avoids -- least of all that
          // there aren't any.
          sweptNothing = !c.systems;
          if (sweptNothing) {
            parts.push("the sweep read no systems back, so it rules nothing out");
          } else {
            parts.push(
              `swept ${c.systems} systems, ${c.channels || 0} channels and ` +
                `${c.talkgroups || 0} talkgroups`
            );
            const extra = (data.unrecorded_permanent || []).length;
            parts.push(
              extra
                ? `${extra} permanent avoid${extra === 1 ? "" : "s"} not recorded here`
                : "nothing avoided that isn't recorded here"
            );
          }
          if (failed) {
            parts.push(
              `${failed} read${failed === 1 ? "" : "s"} failed, so this may be incomplete`
            );
          }
        }
        setStatus(`${parts.join("; ")}.`, Boolean(data.disagree || unknown || sweptNothing));
      } catch (err) {
        setStatus(`Could not check against the scanner: ${err.message}`, true);
      }
      buttons.forEach((b) => (b.disabled = false));
    }

    /** Avoids the scanner is holding that no record accounts for.
     *
     *  The permanent ones are the point: a keypress that landed on the
     *  wrong channel avoids it for good and leaves nothing behind, so
     *  until this existed the only trace was a channel that had gone
     *  quiet. The index printed here is what un-avoiding one needs. */
    _renderUnrecorded(box, data) {
      if (!data.swept) {
        return;
      }
      // A walk that read no systems found nothing because it looked at
      // nothing. Saying "the scanner is avoiding nothing that isn't
      // listed above" here would be a clean bill of health issued
      // without a single read behind it.
      if (!(data.counts || {}).systems) {
        fill(box, el("p", "note error-text",
          "The sweep read no systems back — it says nothing about what the scanner is " +
          "avoiding. Run it again."));
        return;
      }

      const permanent = data.unrecorded_permanent || [];
      const temporary = (data.unrecorded || []).filter((a) => a.avoid !== "Avoid");

      if (!permanent.length && !temporary.length) {
        fill(box, el("p", "note", "The scanner is avoiding nothing that isn't listed above."));
        return;
      }

      const kids = [];
      if (permanent.length) {
        kids.push(el("p", "note error-text",
          `Avoided for good on the scanner, with no record here — un-avoiding one needs its ` +
          `index and the Raw command box on the Control tab (AVD,CFREQ,<index>,,3), followed ` +
          `by a permanent avoid to save it.`));
        permanent.forEach((a) => kids.push(this._unrecordedRow(a)));
      }
      if (temporary.length) {
        // Worth showing but not worth alarm: these clear themselves at the
        // next power cycle, which is what makes them temporary.
        kids.push(el("p", "note",
          `${temporary.length} entr${temporary.length === 1 ? "y is" : "ies are"} temporarily ` +
          `avoided (a single AVOID press, or the unit's own menus). These clear themselves the ` +
          `next time the scanner restarts.`));
        temporary.slice(0, 10).forEach((a) => kids.push(this._unrecordedRow(a)));
        if (temporary.length > 10) {
          kids.push(el("p", "note", `…and ${temporary.length - 10} more.`));
        }
      }
      fill(box, ...kids);
    }

    _unrecordedRow(entry) {
      const row = el("div", "avoid-entry");
      const text = el("div", "avoid-entry-text");
      text.appendChild(el("strong", null, entry.name || `index ${entry.index}`));
      const where = [entry.system, entry.department, entry.frequency].filter(Boolean).join(" · ");
      text.appendChild(
        el("span", "note", `${entry.level} · index ${entry.index}${where ? ` — ${where}` : ""}`)
      );
      row.appendChild(text);
      row.appendChild(el("span", "badge", entry.avoid));
      return row;
    }

    _renderAvoids(section, id, records) {
      const box = section.querySelector("[data-avoid-list]");
      const badge = section.querySelector("[data-avoid-badge]");
      const active = records.filter((r) => !r.cleared_at).length;
      badge.textContent = records.length ? String(active) : "none";

      if (!records.length) {
        fill(box, el("p", "note", "Nothing avoided from here yet."));
        return;
      }

      const status = el("p", "status");
      const setStatus = (text, isError) => {
        status.textContent = text;
        status.classList.toggle("error-text", !!isError);
      };
      const states = section._avoidStates;
      fill(
        box,
        ...records.map((r) =>
          this._avoidRow(section, id, r, setStatus, states ? states.get(r.id) : null)
        ),
        status
      );
    }

    /** One line saying what the scanner said about this row.
     *
     *  The three states are kept distinct rather than collapsed into
     *  agrees/disagrees, because they call for different things: T-Avoid
     *  means the two presses were too far apart and it needs doing again,
     *  Off means the record is stale and wants Forget, and a missing index
     *  means the record points at nothing. */
    _avoidStateNote(checked) {
      const state = checked.scanner_state;
      let text;
      if (checked.agrees && state === "Avoid") {
        text = `the scanner agrees: avoided${
          checked.detail && checked.detail !== checked.name ? ` (listed as “${checked.detail}”)` : ""
        }`;
      } else if (checked.agrees) {
        text = "the scanner agrees: not avoided at the moment, and comes back on restart";
      } else if (state === "T-Avoid") {
        text = "the scanner has this as a temporary avoid — it will clear at the next restart, " +
          "so avoid it again";
      } else if (state === "Off") {
        text = "the scanner is not avoiding this — the record is stale, and Forget drops it";
      } else if (state === "Avoid") {
        text = "the scanner is still avoiding this, so the un-avoid did not take";
      } else {
        text = `the scanner could not confirm this — ${checked.detail || "no state came back"}`;
      }
      return el("span", checked.agrees ? "note" : "note error-text", text);
    }

    _avoidRow(section, id, record, setStatus, checked) {
      const row = el("div", "avoid-entry");
      const label = record.name || `index ${record.index}`;

      const text = el("div", "avoid-entry-text");
      text.appendChild(el("strong", null, label));
      const where = [record.system, record.department].filter(Boolean).join(" · ");
      text.appendChild(
        el("span", "note", [where, formatTime(record.at)].filter(Boolean).join(" — "))
      );
      // Un-avoiding writes the scanner's working copy only, so a cleared
      // entry is scanning again *now* but comes back avoided at the next
      // restart. It stays on the list saying so until a permanent avoid
      // flushes it -- see the note in the template.
      if (record.cleared_at) {
        text.appendChild(
          el("span", "note error-text",
             "un-avoided, but not saved — comes back if the scanner restarts first")
        );
      }
      if (checked) {
        text.appendChild(this._avoidStateNote(checked));
      }
      row.appendChild(text);

      // Both in one group, pinned to the right-hand edge: loose in the row
      // they get spaced apart by the row's own justification and Un-avoid
      // ends up floating in the middle of it.
      const actions = el("div", "avoid-actions");
      const undo = el("button", "secondary", "Un-avoid");
      const forget = el("button", "danger", "Forget");
      [undo, forget].forEach((button) => {
        button.type = "button";
        actions.appendChild(button);
      });
      undo.disabled = Boolean(record.cleared_at);
      row.appendChild(actions);

      const call = async (query, describe) => {
        undo.disabled = forget.disabled = true;
        try {
          const result = await api(`scanners/${id}/avoids/${record.id}${query}`, {
            method: "DELETE",
          });
          if (result.ok) {
            setStatus(describe(result));
            // Re-read rather than splice the row out: the add-on is the one
            // that decides whether the record is gone, and a refusal it
            // reported as ok:false has to leave the row where it was.
            this._loadAvoids(section);
            return;
          }
          setStatus(`the scanner refused ${result.command} (${result.response})`, true);
        } catch (err) {
          setStatus(`could not un-avoid ${label}: ${err.message}`, true);
        }
        undo.disabled = forget.disabled = false;
      };

      undo.title = record.cleared_at
        ? `${label} is already un-avoided in the scanner's working copy; it needs a ` +
          "permanent avoid somewhere to be written out."
        : `Un-avoids ${label} right away. Not saved until the next permanent avoid.`;
      undo.addEventListener("click", () => call("", () => `${label} is no longer avoided.`));

      forget.title =
        "Drops this record without touching the scanner. For an avoid you have already " +
        "cleared on the unit itself.";
      forget.addEventListener("click", () => {
        // Worth confirming: the index in this record is the only copy there
        // is, and the scanner cannot be asked for it again. Forgetting a
        // channel that is still avoided strands it.
        if (!window.confirm(
          `Forget ${label} without un-avoiding it? This drops the only record of the list ` +
          `index it was avoided by, so it can't be un-avoided from here afterwards. Do this ` +
          `only if you have already cleared it on the scanner.`
        )) {
          return;
        }
        call("?forget=true", () => `Forgot ${label}. The scanner was not touched.`);
      });

      return row;
    }

    _refreshHeader(section) {
      const name = section.querySelector('[data-field="name"]').value.trim();
      const id = SDS.slugify(name);
      section.querySelector(".scanner-name").textContent = name || "Unnamed scanner";

      const dot = section.querySelector(".dot");
      const state = this.runtime.get(id);
      if (state === undefined) {
        dot.dataset.state = "unknown";
        dot.title = name ? `Not running yet — save to start "${id}".` : "Not running yet.";
      } else if (state.reachable) {
        dot.dataset.state = "up";
        dot.title = "Responding on the control interface.";
      } else {
        dot.dataset.state = "down";
        dot.title = "Running, but the scanner is not responding on the control interface.";
      }

      const filled = POE_FIELDS.filter((field) => {
        const input = section.querySelector(`[data-field="${field}"]`);
        // A saved password renders as an empty field on purpose (see
        // _buildScanner), so an empty-but-stored one still counts as filled
        // -- otherwise a fully configured scanner reads "incomplete" forever.
        if (input.dataset.stored === "true") return true;
        return input.value.trim() !== "";
      }).length;
      const badge = section.querySelector("[data-poe-badge]");
      badge.textContent = filled === 0 ? "off" : filled === POE_FIELDS.length ? "on" : "incomplete";

      const seconds = Number(section.querySelector('[data-field="wx_return_to_scan_s"]').value);
      section.querySelector("[data-wx-badge]").textContent = seconds > 0 ? `${seconds}s` : "off";

      // Two independent checks behind one badge, so it has to say which are
      // on rather than just "on" -- "hold" and "silence" fail differently
      // and a user who switched one off should see that from the summary.
      const hold = Number(section.querySelector('[data-field="stuck_hold_s"]').value);
      const silence = Number(section.querySelector('[data-field="stuck_silence_s"]').value);
      const watching = [hold > 0 ? "hold" : null, silence > 0 ? "silence" : null].filter(Boolean);
      section.querySelector("[data-stuck-badge]").textContent =
        watching.length ? watching.join(" + ") : "off";

      const transcribing = section.querySelector('[data-field="transcribe_enabled"]').checked;
      const modes = section.querySelector('[data-field="transcribe_modes"]').value.trim();
      section.querySelector("[data-transcribe-badge]").textContent = transcribing
        ? modes || "on"
        : "off";
    }

    _collect() {
      const sections = Array.from(this.root.querySelectorAll(".scanner"));
      const scanners = sections.map((section) => {
        const scanner = {};
        section.querySelectorAll("[data-field]").forEach((input) => {
          scanner[input.dataset.field] =
            input.type === "checkbox" ? input.checked : input.value.trim();
        });
        // Blank password plus a stored one means "keep it", which the API
        // expresses as the sentinel. Done here rather than by putting the
        // sentinel in the input, so the field genuinely is empty in the DOM.
        const password = section.querySelector('[data-field="poe_reset_password"]');
        if (password.dataset.stored === "true" && scanner.poe_reset_password === "") {
          scanner.poe_reset_password = this.passwordSentinel;
        }
        return scanner;
      });
      return {
        log_level: this.logLevel.value,
        scanners,
        history: {
          enabled: this.historyEnabled.checked,
          retention_days: Number(this.historyDays.value) || undefined,
          max_records: Number(this.historyMaxRecords.value) || undefined,
        },
        transcribe: {
          backend: this.sttBackend.value,
          url: this.sttUrl.value.trim(),
          model: this.sttModel.value.trim(),
          language: this.sttLanguage.value.trim(),
          prompt: this.sttPrompt.value.trim(),
          max_segment_s: Number(this.sttMaxSegment.value) || undefined,
          silence_gap_s: Number(this.sttGap.value) || undefined,
          upsample: this.sttUpsample.checked,
        },
      };
    }

    async _save() {
      this.save.disabled = true;
      this._setStatus("Saving…");
      let data;
      try {
        data = await post("api/settings", this._collect());
      } catch (err) {
        this._showMessages({ errors: [err.message] });
        this._setStatus("Not saved.");
        this.save.disabled = false;
        return;
      }

      this.dirty = false;
      this.runtime = new Map((data.runtime || []).map((s) => [s.id, s]));
      this._showMessages({
        warnings: data.warnings || [],
        ok: this._describeChanges(data.changes),
      });
      // Re-render from what the server actually stored, not from the form:
      // this is what makes "the value I typed did not take" impossible to
      // miss.
      this._applyGeneral(data.config);
      this._renderScanners(data.config.scanners);
      this._setStatus("Saved.");
      this.onSaved(data);
    }

    _describeChanges(changes) {
      if (!changes) return "Settings saved.";
      const parts = [];
      if (changes.started.length) parts.push(`started ${changes.started.join(", ")}`);
      if (changes.restarted.length) parts.push(`reconnected ${changes.restarted.join(", ")}`);
      if (changes.stopped.length) parts.push(`stopped ${changes.stopped.join(", ")}`);
      if (changes.unchanged.length) {
        parts.push(`left ${changes.unchanged.join(", ")} running untouched`);
      }
      return parts.length ? `Settings saved — ${parts.join("; ")}.` : "Settings saved.";
    }

    _showMessages({ errors = [], warnings = [], ok = null }) {
      fill(this.messages);
      const add = (kind, title, items) => {
        const node = el("div", `message ${kind}`);
        node.appendChild(el("strong", null, title));
        if (items && items.length) {
          const ul = el("ul");
          items.forEach((item) => ul.appendChild(el("li", null, item)));
          node.appendChild(ul);
        }
        this.messages.appendChild(node);
      };
      if (errors.length) add("error", "Not saved:", errors);
      if (warnings.length) add("warning", "Saved, but worth checking:", warnings);
      if (ok) add("ok", ok, null);
    }
  }

  SDS.SettingsView = SettingsView;
})(window.SDS);
