/* Actions tab: rules that fire something when the scanner hears something.
 *
 * Saves independently of the settings tab (POST api/triggers, which the
 * server merges over the stored config -- see settings_api._save), so a
 * half-finished scanner edit on another tab can't block saving an action and
 * vice versa.
 *
 * Every match field is optional and empty ones are ignored, which makes an
 * untouched rule match everything. That's a legitimate "send every call to
 * my webhook" configuration rather than a mistake, so the UI says so in the
 * summary line instead of trying to prevent it.
 */

"use strict";

(function (SDS) {
  const { el, fill, api, post } = SDS;

  const ACTION_LABELS = {
    ha_event: "Fire a Home Assistant event",
    ha_service: "Call a Home Assistant service",
    webhook: "POST to a webhook",
  };
  const EVENT_LABELS = {
    start: "when a call starts",
    end: "when a call ends",
    transcript: "when a call is transcribed",
    both: "when a call starts and ends",
    wx_alert: "when a weather alert starts",
    wx_clear: "when a weather alert clears",
    stuck: "when the scanner stops scanning its lists",
    stuck_clear: "when the scanner starts scanning its lists again",
  };
  // The criteria below are about a call, and neither of these families is
  // one: a weather alert reports whichever weather channel the scanner is
  // parked on, and a stuck report says what it is stuck *on*. Filling these
  // in on either is how you write a rule that never fires.
  const WX_EVENTS = ["wx_alert", "wx_clear"];
  const STUCK_EVENTS = ["stuck", "stuck_clear"];
  const STATE_EVENTS = [...WX_EVENTS, ...STUCK_EVENTS];
  const MODE_LABELS = {
    analog: "Analog (AM/FM/NFM)",
    p25: "P25",
    dmr: "DMR / MotoTRBO",
    nxdn: "NXDN / NEXEDGE",
    provoice: "ProVoice",
    edacs: "EDACS",
    ltr: "LTR",
    motorola: "Motorola (analog trunked)",
    unknown: "Unrecognized",
  };
  // Transcript first: it is the criterion someone comes to this page to
  // write, and the only one that needs its own event to be usable.
  const TEXT_CRITERIA = [
    ["transcript", "Something said (transcript)"],
    ["system", "System"],
    ["department", "Department"],
    ["channel", "Channel / talkgroup name"],
    ["tgid", "Talkgroup ID"],
    ["unit_id", "Unit ID"],
    ["sub_audio", "Tone / NAC"],
    ["site", "Site"],
    ["system_type", "System type (raw)"],
  ];

  class ActionsView {
    constructor() {
      this.root = el("div", "actions");
      this.rules = [];
      this.scanners = [];
      this.modes = Object.keys(MODE_LABELS);
      this.lastFired = {};
      this.maxTriggers = 50;
      this.dirty = false;
      this._build();
    }

    _build() {
      this.messages = el("div", "messages");
      this.root.appendChild(this.messages);

      const intro = el("p", "sub");
      intro.textContent =
        "An action fires when a call matches every criterion you fill in. Leave a field empty to " +
        "ignore it. Text fields match anywhere in the value and are not case-sensitive.";
      this.root.appendChild(intro);

      this.list = el("div", "action-list");
      this.root.appendChild(this.list);

      const addRow = el("p");
      this.addButton = el("button", "secondary", "Add action");
      this.addButton.type = "button";
      this.addButton.addEventListener("click", () => {
        this.rules.push(this._blankRule());
        this._markDirty();
        this.render();
      });
      addRow.appendChild(this.addButton);
      this.root.appendChild(addRow);

      const bar = el("div", "inline-savebar");
      this.saveButton = el("button", "primary", "Save actions");
      this.saveButton.type = "button";
      this.saveButton.disabled = true;
      this.saveButton.addEventListener("click", () => this.save());
      this.saveStatus = el("span", "status");
      bar.appendChild(this.saveButton);
      bar.appendChild(this.saveStatus);
      this.root.appendChild(bar);
    }

    _blankRule() {
      return {
        id: "",
        name: "",
        enabled: true,
        scanner_id: "",
        event: "start",
        cooldown_s: 30,
        match: {
          transcript: "", system: "", department: "", site: "", channel: "", tgid: "",
          unit_id: "",
          sub_audio: "", system_type: "", modes: [], frequency: null,
          frequency_tolerance_khz: 5, min_duration_s: 0,
        },
        action: {
          type: "ha_event", url: "", method: "POST", domain: "", service: "",
          entity_id: "", event_type: "sds200_reception", include_reception: false, data: {},
        },
      };
    }

    setScanners(scanners) {
      this.scanners = scanners;
    }

    /** Start a new action prefilled from a call in the history, and put it
     *  where it can be seen. Unsaved like any other new rule: the point is to
     *  skip retyping what the scanner already heard, not to commit anything.
     *
     *  Criteria come from the call's identity (system / department / channel
     *  / talkgroup) rather than from everything on the record -- a rule
     *  carrying that call's frequency *and* its talkgroup *and* its unit id
     *  would only ever fire on that one radio keying up again. */
    addFromRecord(record) {
      if (this.rules.length >= this.maxTriggers) {
        this._showMessages({
          errors: [`Already at the limit of ${this.maxTriggers} actions. Remove one first.`],
        });
        return null;
      }
      const rule = this._blankRule();
      rule.name = record.label ? `When ${record.label}` : "";
      rule.scanner_id = record.scanner_id || "";
      rule.match.system = record.system || "";
      rule.match.department = record.department || "";
      rule.match.channel = record.channel || "";
      rule.match.tgid = record.tgid || "";
      // Conventional: no talkgroup to key off, so the frequency is the
      // identity. Trunked: the talkgroup is, and the frequency is just
      // whichever site channel it happened to land on this time.
      if (!record.tgid && record.frequency !== null && record.frequency !== undefined) {
        rule.match.frequency = record.frequency;
      }
      // First, not last: this rule came from somewhere else on the page, so
      // it has to be visible without hunting for it.
      this.rules.unshift(rule);
      this._markDirty();
      this.render();
      const card = this.list.firstElementChild;
      if (card && card.scrollIntoView) card.scrollIntoView({ block: "nearest" });
      return rule;
    }

    _markDirty() {
      this.dirty = true;
      this.saveButton.disabled = false;
      this.saveStatus.textContent = "Unsaved changes.";
    }

    async refresh() {
      try {
        const data = await api("api/triggers");
        this.rules = data.triggers || [];
        this.modes = data.modes || this.modes;
        this.lastFired = data.last_fired || {};
        this.maxTriggers = data.max_triggers || this.maxTriggers;
        this.dirty = false;
        this.saveButton.disabled = true;
        this.saveStatus.textContent = "";
        this._showMessages({ errors: data.errors, warnings: data.warnings });
        this.render();
      } catch (err) {
        fill(this.list, el("p", "message error", `Could not load the actions: ${err.message}`));
      }
    }

    async save() {
      this.saveButton.disabled = true;
      this.saveStatus.textContent = "Saving…";
      let data;
      try {
        data = await post("api/triggers", { triggers: this.rules });
      } catch (err) {
        this._showMessages({ errors: [err.message] });
        this.saveStatus.textContent = "Not saved.";
        this.saveButton.disabled = false;
        return;
      }
      this.dirty = false;
      this.saveStatus.textContent = "Saved.";
      this._showMessages({ warnings: data.warnings, ok: "Actions saved." });
      // Re-render from what the server actually stored, not from the form:
      // this is what makes "the value I typed did not take" impossible to
      // miss, and it is also how a new rule picks up its generated id.
      this.rules = data.triggers || [];
      this.render();
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

    render() {
      this.addButton.disabled = this.rules.length >= this.maxTriggers;
      if (!this.rules.length) {
        fill(
          this.list,
          el(
            "p",
            "empty",
            "No actions yet. Add one to have the add-on call a service, fire an event or POST a " +
              "webhook when a matching call comes in."
          )
        );
        return;
      }
      fill(this.list, ...this.rules.map((rule, index) => this._ruleCard(rule, index)));
    }

    /* ---------------------------------------------------------------- */

    _ruleCard(rule, index) {
      const card = el("section", "card action");
      if (!rule.enabled) card.classList.add("disabled");

      const head = el("div", "scanner-head");
      const title = el("div", "scanner-title");

      const toggle = el("input");
      toggle.type = "checkbox";
      toggle.checked = rule.enabled;
      toggle.title = "Enabled";
      toggle.addEventListener("change", () => {
        rule.enabled = toggle.checked;
        card.classList.toggle("disabled", !rule.enabled);
        this._markDirty();
      });
      title.appendChild(toggle);

      const name = el("input", "action-name");
      name.type = "text";
      name.value = rule.name || "";
      name.placeholder = "Name this action";
      name.addEventListener("input", () => {
        rule.name = name.value;
        this._markDirty();
      });
      title.appendChild(name);
      head.appendChild(title);

      const remove = el("button", "link danger", "Remove");
      remove.type = "button";
      remove.addEventListener("click", () => {
        this.rules.splice(index, 1);
        this._markDirty();
        this.render();
      });
      head.appendChild(remove);
      card.appendChild(head);

      card.appendChild(this._whenBlock(rule));
      card.appendChild(this._matchBlock(rule));
      card.appendChild(this._actionBlock(rule));
      card.appendChild(this._footer(rule));
      return card;
    }

    _whenBlock(rule) {
      const grid = el("div", "grid");
      grid.appendChild(
        this._select("Fire", rule.event, Object.keys(EVENT_LABELS), (value) => {
          rule.event = value;
          // Redrawn because the match section says something different for a
          // weather rule than for a call one.
          this.render();
        }, (value) => EVENT_LABELS[value])
      );
      grid.appendChild(
        this._select(
          "Scanner",
          rule.scanner_id,
          ["", ...this.scanners.map((s) => s.id)],
          (value) => {
            rule.scanner_id = value;
          },
          (value) => {
            if (!value) return "Any scanner";
            const scanner = this.scanners.find((s) => s.id === value);
            return scanner ? scanner.name || scanner.id : value;
          }
        )
      );
      grid.appendChild(
        this._number(
          "Cooldown (seconds)",
          rule.cooldown_s,
          (value) => {
            rule.cooldown_s = value;
          },
          "How long to wait before this action can fire again for the same channel or talkgroup. " +
            "0 fires on every matching call."
        )
      );
      return grid;
    }

    _matchBlock(rule) {
      const details = el("details", "action-section");
      details.open = true;
      const summary = el("summary", null, "Match");
      summary.appendChild(el("span", "badge", this._summarize(rule)));
      details.appendChild(summary);

      if (STATE_EVENTS.includes(rule.event)) {
        details.appendChild(
          el(
            "p",
            "note",
            STUCK_EVENTS.includes(rule.event)
              ? "This action fires on the scanner having stopped covering its lists — held on a " +
                "system or department, or hearing nothing for as long as you configured on the " +
                "Scanners tab. The criteria below usually want to be left empty; use the " +
                "scanner picker above to limit which scanner it fires for. While it stays " +
                "stuck this repeats hourly, so set a cooldown if you want it quieter."
              : "This action fires on the alert itself, so the criteria below usually want to be " +
                "left empty — a weather alert reports whichever weather channel the scanner is " +
                "parked on, not a call. Use the scanner picker above to limit which scanner it " +
                "fires for."
          )
        );
      }

      const grid = el("div", "grid");
      TEXT_CRITERIA.forEach(([field, label]) => {
        grid.appendChild(
          this._text(label, rule.match[field], (value) => {
            rule.match[field] = value;
            summary.lastChild.textContent = this._summarize(rule);
          })
        );
      });
      grid.appendChild(
        this._number(
          "Frequency (MHz)",
          rule.match.frequency,
          (value) => {
            rule.match.frequency = value;
            summary.lastChild.textContent = this._summarize(rule);
          },
          "Matched within the tolerance below, so 462.612 and 462.6125 both work.",
          "0.0001"
        )
      );
      grid.appendChild(
        this._number("Frequency tolerance (kHz)", rule.match.frequency_tolerance_khz, (value) => {
          rule.match.frequency_tolerance_khz = value;
        })
      );
      grid.appendChild(
        this._number(
          "Minimum duration (seconds)",
          rule.match.min_duration_s,
          (value) => {
            rule.match.min_duration_s = value;
          },
          "Only applies to \"when a call ends\" — at the start nothing has a duration yet."
        )
      );
      details.appendChild(grid);

      const modes = el("div", "mode-picker");
      modes.appendChild(el("span", "field-label", "Modes"));
      const help = el(
        "small",
        null,
        "None selected means any mode. Only \"Conventional\" has been confirmed on this " +
          "hardware, so if your system reports a type this add-on doesn't recognize it lands " +
          "under \"Unrecognized\" — match it by its raw system type above instead."
      );
      const chips = el("div", "chips");
      this.modes.forEach((mode) => {
        const label = el("label", "chip-check");
        const input = el("input");
        input.type = "checkbox";
        input.checked = (rule.match.modes || []).includes(mode);
        input.addEventListener("change", () => {
          const set = new Set(rule.match.modes || []);
          if (input.checked) set.add(mode);
          else set.delete(mode);
          rule.match.modes = Array.from(set);
          this._markDirty();
          summary.lastChild.textContent = this._summarize(rule);
        });
        label.appendChild(input);
        label.appendChild(el("span", null, MODE_LABELS[mode] || mode));
        chips.appendChild(label);
      });
      modes.appendChild(chips);
      modes.appendChild(help);
      details.appendChild(modes);
      return details;
    }

    _actionBlock(rule) {
      const details = el("details", "action-section");
      details.open = true;
      const summary = el("summary", null, "Do this");
      summary.appendChild(el("span", "badge", ACTION_LABELS[rule.action.type] || rule.action.type));
      details.appendChild(summary);

      const body = el("div");
      const renderBody = () => {
        fill(body);
        body.appendChild(
          this._select(
            // "Type", not "Action": the whole rule is an action now, and a
            // field inside it with the same name would be asking which of
            // the two you meant. The section header ("Do this") already
            // says what this picks.
            "Type",
            rule.action.type,
            Object.keys(ACTION_LABELS),
            (value) => {
              rule.action.type = value;
              summary.lastChild.textContent = ACTION_LABELS[value] || value;
              renderBody();
            },
            (value) => ACTION_LABELS[value] || value
          )
        );

        const grid = el("div", "grid");
        if (rule.action.type === "webhook") {
          grid.appendChild(
            this._text("URL", rule.action.url, (value) => {
              rule.action.url = value;
            }, "http:// or https://. The call's details are sent as the JSON body.")
          );
          grid.appendChild(
            this._select("Method", rule.action.method, ["POST", "PUT", "GET"], (value) => {
              rule.action.method = value;
            })
          );
        } else if (rule.action.type === "ha_service") {
          grid.appendChild(
            this._text("Domain", rule.action.domain, (value) => {
              rule.action.domain = value;
            }, "e.g. notify, light, script")
          );
          grid.appendChild(
            this._text("Service", rule.action.service, (value) => {
              rule.action.service = value;
            }, "e.g. persistent_notification, turn_on. For a script, its own "
                + "name -- its fields go in Fields below.")
          );
          grid.appendChild(
            this._text("Entity (optional)", rule.action.entity_id, (value) => {
              rule.action.entity_id = value;
            })
          );
        } else {
          grid.appendChild(
            this._text("Event type", rule.action.event_type, (value) => {
              rule.action.event_type = value;
            }, "Use this as the event trigger in a Home Assistant automation.")
          );
        }
        body.appendChild(grid);

        if (rule.action.type === "ha_service") {
          const check = el("label", "check");
          const input = el("input");
          input.type = "checkbox";
          input.checked = !!rule.action.include_reception;
          input.addEventListener("change", () => {
            rule.action.include_reception = input.checked;
            this._markDirty();
          });
          const text = el("span");
          text.appendChild(document.createTextNode("Include the full call details"));
          text.appendChild(
            el(
              "small",
              null,
              "Passed as a \"reception\" field. Off by default: most service schemas reject " +
                "fields they don't know about."
            )
          );
          check.appendChild(input);
          check.appendChild(text);
          body.appendChild(check);
        }

        body.appendChild(this._dataEditor(rule));
      };
      renderBody();
      details.appendChild(body);
      return details;
    }

    /** The extra key/value pairs sent with the action. Values are template-
     *  rendered server-side, hence the placeholder hint, and keep their JSON
     *  type so a script field declared as a number gets one. */
    _dataEditor(rule) {
      const wrap = el("div", "data-editor");
      wrap.appendChild(
        el("span", "field-label", rule.action.type === "ha_service" ? "Fields" : "Extra fields")
      );
      wrap.appendChild(
        el(
          "small",
          null,
          (rule.action.type === "ha_service"
            ? "Sent as the service's data -- for a script, these are its fields. "
            : "Sent with the action. ") +
            "Values can use placeholders: {label}, {channel}, {system}, {department}, " +
            "{frequency}, {tgid}, {unit_ids}, {mode}, {duration}."
        )
      );
      wrap.appendChild(
        el(
          "small",
          null,
          "Numbers, true/false and [\"lists\"] keep their type; everything else is text. " +
            "Quote a value (\"911\") to force text where a number would be read."
        )
      );

      const rows = el("div", "data-rows");
      const redraw = () => {
        fill(rows);
        const entries = Object.entries(rule.action.data || {});
        entries.forEach(([key, value]) => {
          const row = el("div", "data-row");
          const keyInput = el("input");
          keyInput.type = "text";
          keyInput.value = key;
          keyInput.placeholder = "field";
          const valueInput = el("input");
          valueInput.type = "text";
          valueInput.value = value;
          valueInput.placeholder = "value";
          const commit = () => {
            const next = {};
            // Only the rows, by class. `rows` also holds the "Add a field"
            // button as its last child, and reading .children[0].value off
            // that threw on every keystroke -- which took the two lines
            // below down with it, so nothing typed into a field was ever
            // stored and every rule saved with the empty defaults the "Add"
            // button had written directly.
            rows.querySelectorAll(".data-row").forEach((child) => {
              const k = child.children[0].value.trim();
              if (k) next[k] = child.children[1].value;
            });
            rule.action.data = next;
            this._markDirty();
          };
          keyInput.addEventListener("change", commit);
          valueInput.addEventListener("input", commit);
          const remove = el("button", "link danger", "×");
          remove.type = "button";
          remove.title = "Remove this field";
          remove.addEventListener("click", () => {
            const next = { ...rule.action.data };
            // By the captured key, not by index: renaming a field above this
            // one reorders the object, and an index would then delete the
            // wrong row.
            delete next[keyInput.value.trim() || key];
            rule.action.data = next;
            this._markDirty();
            redraw();
          });
          row.appendChild(keyInput);
          row.appendChild(valueInput);
          row.appendChild(remove);
          rows.appendChild(row);
        });
        const add = el("button", "link", "Add a field");
        add.type = "button";
        add.addEventListener("click", () => {
          rule.action.data = { ...rule.action.data, [`field${entries.length + 1}`]: "" };
          this._markDirty();
          redraw();
        });
        rows.appendChild(add);
      };
      redraw();
      wrap.appendChild(rows);
      return wrap;
    }

    _footer(rule) {
      const footer = el("div", "action-footer");
      const test = el("button", "secondary", "Test");
      test.type = "button";
      test.title = "Fire this action now, ignoring its match conditions and cooldown.";
      const status = el("span", "status");
      test.addEventListener("click", async () => {
        test.disabled = true;
        status.textContent = "Testing…";
        status.classList.remove("error-text");
        try {
          const data = await post("api/triggers/test", { trigger: rule });
          status.textContent = data.detail || "Sent.";
        } catch (err) {
          status.textContent = err.message;
          status.classList.add("error-text");
        } finally {
          test.disabled = false;
        }
      });
      footer.appendChild(test);
      footer.appendChild(status);

      const fired = this.lastFired[rule.id];
      if (fired) {
        const when = SDS.formatTime(fired.at);
        const note = el(
          "span",
          `status ${fired.ok ? "" : "error-text"}`.trim(),
          fired.ok ? `Last fired ${when}: ${fired.detail}` : `Last failed ${when}: ${fired.detail}`
        );
        footer.appendChild(note);
      }
      return footer;
    }

    /* ---------------------------------------------------------------- */

    _summarize(rule) {
      const parts = [];
      TEXT_CRITERIA.forEach(([field, label]) => {
        if (rule.match[field]) parts.push(`${label.toLowerCase()} ~ "${rule.match[field]}"`);
      });
      if (rule.match.frequency) parts.push(`${rule.match.frequency} MHz`);
      if ((rule.match.modes || []).length) parts.push(rule.match.modes.join("/"));
      if (parts.length) return parts.join(", ");
      if (STUCK_EVENTS.includes(rule.event)) return "whenever it stops scanning";
      return WX_EVENTS.includes(rule.event) ? "every weather alert" : "every call";
    }

    _text(label, value, onChange, help) {
      const wrap = el("label");
      wrap.appendChild(el("span", null, label));
      const input = el("input");
      input.type = "text";
      input.value = value === null || value === undefined ? "" : value;
      input.addEventListener("input", () => {
        onChange(input.value);
        this._markDirty();
      });
      wrap.appendChild(input);
      if (help) wrap.appendChild(el("small", null, help));
      return wrap;
    }

    _number(label, value, onChange, help, step) {
      const wrap = el("label");
      wrap.appendChild(el("span", null, label));
      const input = el("input");
      input.type = "number";
      if (step) input.step = step;
      input.value = value === null || value === undefined ? "" : value;
      input.addEventListener("input", () => {
        // "" means "not set", which for frequency is a real state (no
        // frequency criterion) and for the rest normalizes to the default
        // server-side -- so pass the empty string through rather than
        // coercing it to 0 here.
        onChange(input.value === "" ? null : Number(input.value));
        this._markDirty();
      });
      wrap.appendChild(input);
      if (help) wrap.appendChild(el("small", null, help));
      return wrap;
    }

    _select(label, value, options, onChange, labeller) {
      const wrap = el("label");
      wrap.appendChild(el("span", null, label));
      const select = el("select");
      options.forEach((option) => {
        const node = el("option", null, labeller ? labeller(option) : option);
        node.value = option;
        select.appendChild(node);
      });
      select.value = value;
      select.addEventListener("change", () => {
        onChange(select.value);
        this._markDirty();
      });
      wrap.appendChild(select);
      return wrap;
    }
  }

  SDS.ActionsView = ActionsView;
})(window.SDS);
