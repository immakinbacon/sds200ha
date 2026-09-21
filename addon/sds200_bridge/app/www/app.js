/* The shell: tabs, the shared WebSocket, and the runtime poll.
 *
 * One WebSocket for the whole page rather than one per view. The add-on
 * already fans out both status pushes (at most once a second per scanner) and
 * reception events on a single socket; opening a second connection per tab
 * would double that traffic for no benefit, and the Control tab needs to
 * keep receiving while the History tab is the one on screen.
 */

"use strict";

(function (SDS) {
  const { el, fill, api } = SDS;

  const RECONNECT_DELAY_MS = 3000;
  // Reachability isn't pushed -- the status socket only carries scanners
  // that are answering, and a configured-but-dark scanner is exactly what
  // the dots need to show. So it stays a poll, at the same interval the
  // settings page used before this rewrite.
  const RUNTIME_POLL_MS = 5000;

  const TABS = [
    { id: "control", label: "Control" },
    { id: "history", label: "History" },
    { id: "actions", label: "Actions" },
    { id: "settings", label: "Settings" },
  ];

  class App {
    constructor() {
      this.main = document.getElementById("app");
      this.scanners = [];
      this.panels = new Map();
      this.active = "control";
      this.socket = null;
      this.reconnectTimer = null;
    }

    async start() {
      this._buildTabs();

      this.control = el("div", "control-tab");
      this.history = new SDS.HistoryView({
        onCreateAction: (record) => this._createAction(record),
      });
      this.actions = new SDS.ActionsView();
      this.settings = new SDS.SettingsView({
        // A save can add, rename or remove a scanner, which changes every
        // other tab's idea of what exists -- so re-read the roster rather
        // than waiting for the next runtime poll to half-update it.
        onSaved: () => this._loadScanners(),
      });

      this.views = {
        control: this.control,
        history: this.history.root,
        actions: this.actions.root,
        settings: this.settings.root,
      };

      this.main.setAttribute("aria-busy", "false");
      fill(this.main, ...Object.values(this.views));

      await this._loadScanners();
      this._show("control");
      this._connect();
      setInterval(() => this._pollRuntime(), RUNTIME_POLL_MS);
    }

    _buildTabs() {
      const nav = document.getElementById("tabs");
      this.tabButtons = new Map();
      TABS.forEach((tab) => {
        const button = el("button", "tab", tab.label);
        button.type = "button";
        button.addEventListener("click", () => this._show(tab.id));
        nav.appendChild(button);
        this.tabButtons.set(tab.id, button);
      });
    }

    _show(id, { reload = true } = {}) {
      this.active = id;
      this.tabButtons.forEach((button, tabId) =>
        button.classList.toggle("active", tabId === id)
      );
      Object.entries(this.views).forEach(([viewId, node]) => {
        node.hidden = viewId !== id;
      });
      // Refreshed on open rather than kept live in the background: the
      // history and actions lists are whole-list replacements, and rebuilding
      // them behind a tab nobody is looking at is pure churn. The Control
      // tab is the exception -- it stays subscribed either way, so audio
      // doesn't stop when you go and look at something else.
      // ...and never over unsaved edits: switching to History to check a
      // frequency and coming back must not silently discard the action you
      // were half way through writing.
      if (!reload) return;
      if (id === "history") this.history.refresh();
      if (id === "actions" && !this.actions.dirty) this.actions.refresh();
      if (id === "settings" && !this.settings.dirty) this.settings.refresh();
    }

    /** "Create action" on a history row: switch to the Actions tab with a new
     *  rule already filled in from that call. */
    async _createAction(record) {
      // Loaded *before* the tab is shown and with the reload suppressed
      // after: switching first would kick off a refresh that lands after the
      // prefilled rule is added and replaces the whole list, silently
      // throwing it away.
      if (!this.actions.dirty) await this.actions.refresh();
      this._show("actions", { reload: false });
      this.actions.addFromRecord(record);
    }

    async _loadScanners() {
      let runtime = [];
      try {
        const data = await api("api/settings");
        runtime = data.runtime || [];
      } catch (err) {
        fill(this.control, el("p", "message error", `Could not reach the add-on: ${err.message}`));
        return;
      }
      this.scanners = runtime;
      this.history.setScanners(runtime);
      this.actions.setScanners(runtime);
      this._renderControl();
    }

    _renderControl() {
      // Panels are kept across reloads of the roster, keyed by scanner id,
      // so a scanner that's still there doesn't lose its playing audio when
      // some *other* scanner is added or renamed.
      const wanted = new Set(this.scanners.map((s) => s.id));
      for (const [id, panel] of this.panels) {
        if (!wanted.has(id)) {
          panel.destroy();
          this.panels.delete(id);
        }
      }

      if (!this.scanners.length) {
        fill(
          this.control,
          el(
            "p",
            "empty",
            "No scanners are running. Add one on the Settings tab to control and listen to it."
          )
        );
        return;
      }

      const nodes = this.scanners.map((scanner) => {
        let panel = this.panels.get(scanner.id);
        if (!panel) {
          panel = new SDS.ControlPanel(scanner);
          this.panels.set(scanner.id, panel);
          panel.refresh();
        }
        panel.setReachable(scanner.reachable);
        return panel.root;
      });
      fill(this.control, ...nodes);
    }

    async _pollRuntime() {
      let data;
      try {
        data = await api("api/settings");
      } catch (err) {
        return; /* Transient -- the add-on restarts on rebuild. Next tick. */
      }
      const runtime = data.runtime || [];
      this.settings.setRuntime(runtime);
      const changed =
        runtime.length !== this.scanners.length ||
        runtime.some((s, i) => this.scanners[i] && this.scanners[i].id !== s.id);
      this.scanners = runtime;
      if (changed) {
        this.history.setScanners(runtime);
        this.actions.setScanners(runtime);
        this._renderControl();
      } else {
        runtime.forEach((scanner) => {
          const panel = this.panels.get(scanner.id);
          if (panel) panel.setReachable(scanner.reachable);
        });
      }
    }

    _connect() {
      clearTimeout(this.reconnectTimer);
      let socket;
      try {
        socket = new WebSocket(SDS.wsUrl("ws"));
      } catch (err) {
        this.reconnectTimer = setTimeout(() => this._connect(), RECONNECT_DELAY_MS);
        return;
      }
      this.socket = socket;
      socket.addEventListener("message", (event) => this._onMessage(event.data));
      const retry = () => {
        if (this.socket !== socket) return;
        this.socket = null;
        this.reconnectTimer = setTimeout(() => this._connect(), RECONNECT_DELAY_MS);
      };
      socket.addEventListener("close", retry);
      socket.addEventListener("error", retry);
    }

    _onMessage(raw) {
      let message;
      try {
        message = JSON.parse(raw);
      } catch (err) {
        return;
      }
      if (message.type === "status") {
        const panel = this.panels.get(message.scanner_id);
        if (panel) panel.update(message.status);
      } else if (message.type === "reception") {
        this.history.onReception(message);
        // The control panel keeps its own short tail of the same log.
        const panel = this.panels.get((message.record || {}).scanner_id);
        if (panel) panel.onReception(message);
      }
    }
  }

  window.addEventListener("beforeunload", (event) => {
    // start() is async, so both views may not exist yet on a very early
    // unload -- nothing can be unsaved in that case either way.
    const app = SDS.app;
    if (!app || !app.settings || !app.actions) return;
    if (!app.settings.dirty && !app.actions.dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

  SDS.app = new App();
  SDS.app.start();
})(window.SDS);
