# Uniden SDS200 for Home Assistant

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-support-FFDD00?style=flat-square&logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/immakinbacon)

Control one or more Uniden SDS200 scanners from Home Assistant, with a
Lovelace card showing what each one is hearing.

This repo contains three pieces:

- **`addon/sds200_bridge`** — a Home Assistant Supervisor add-on that owns
  the actual connection(s) to your scanner(s): the UDP remote-command
  protocol (port 50536) and the RTSP/RTP audio stream (port 554), re-exposed
  as a small local REST/WebSocket API plus an HTTP audio stream. Its own web
  UI (ingress, "Open Web UI") is also a full control surface — see
  [The add-on's web UI](#the-add-ons-web-ui).
- **`custom_components/sds200`** — a custom integration that talks to the
  add-on and creates one Home Assistant device (with sensors, a media player,
  binary sensors, and services) per configured scanner.
- **`custom_components/sds200/www`** — a custom Lovelace card, auto-registered
  by the integration, showing what the scanner is hearing right now
  (channel/department/system, frequency, modulation, tone, signal) with
  **Listen** for its live audio and **Hold** / **Avoid** for whatever it is
  on at that moment. Deliberately a readout plus those two keys, not a front
  panel: driving the scanner is what the add-on's own web UI is for. Lives
  inside the integration's own directory (not a separate top-level `www/`)
  so it ships along with it when installed via HACS.

See [`docs/protocol-notes.md`](docs/protocol-notes.md) for the distilled
protocol reference this project is built against, including real-hardware
findings from development against an actual SDS200.

## Status

- Add-on: control protocol (`protocol.py`), XML list/status reassembly
  (`xml_lists.py`), the REST/WS API (`api.py`/`main.py`), and the RTSP/audio
  path (`audio_bridge.py`) are all verified against a real SDS200 — see
  `docs/protocol-notes.md`. The scanner's embedded RTSP server is fragile
  (wedges on repeated connect/disconnect cycles, recovers via power-cycle
  or the optional `poe_reset_*` config); the audio
  session is kept persistent for the add-on's whole lifetime specifically
  to avoid that. Restarting the add-on abandons that session without a
  TEARDOWN, and the scanner then refuses new ones until its own 60s
  inactivity timeout expires — so the auto-reboot path deliberately holds
  off for the first two and a half minutes after startup rather than
  power-cycling a scanner that is only declining to open a second session.
- Integration and card: installed and working on a real HA OS instance.
  Several real bugs found and fixed along the way — see `docs/protocol-notes.md`
  for the full list (frequency sensor unavailable, glyph bytes in the
  display, add-on hostname/port gotchas, an extended card-loading saga
  around HA's `add_extra_js_url`/Lovelace's shadow-DOM card tree, etc.).

This repo isn't published publicly (no HACS listing). The **add-on** can be
installed either as a local add-on copied into `/addons/`, or by adding
this repo's URL as a Supervisor add-on repository (`repository.yaml` is
that descriptor) — in which case Supervisor serves it from its own cached
clone and needs an explicit "Check for updates" to see new commits. The
**integration** under `custom_components/` is not covered by either of
those: a Supervisor add-on repository only ever serves add-ons, and HACS
talks to the GitHub API so it can't consume a self-hosted GitLab. It has
to be copied onto the host. See Installation below.

## Installation

1. **Get the repo onto your HA host.** SSH in (or use the Terminal & SSH
   add-on) and clone it, e.g.:
   ```
   git clone <this-repo's-url> /tmp/sds200ha
   ```
2. **Add-on, as a local add-on** (no add-on repository URL needed):
   ```
   cp -r /tmp/sds200ha/addon/sds200_bridge /addons/sds200_bridge
   ```
   Then in the UI: Settings → Add-ons → Add-on Store → refresh → find
   "SDS200 Bridge" under Local add-ons → Install → Start → **Open Web UI**,
   and add your scanner(s) there: a name and a host (its IP) are all that's
   required, the ports are prefilled with the right defaults (50536/554).
   Saving takes effect immediately — no restart, and scanners you didn't
   change keep their audio session running.

   Settings live in the add-on's own UI, not in Supervisor's Configuration
   tab. If you're upgrading from 0.1.x, whatever was in that tab is copied
   across the first time 0.2.0 starts; the tab itself sticks around for one
   release (labelled as legacy) and is no longer read.
3. **Integration**:
   ```
   mkdir -p /config/custom_components
   cp -r /tmp/sds200ha/custom_components/sds200 /config/custom_components/sds200
   ```
   Restart Home Assistant (required the first time so the new
   `custom_components` entry is discovered), then Settings → Devices &
   Services → Add Integration → "Uniden SDS200". You'll need the add-on's
   **internal** port (8000 — this is *not* the same as any host-external
   port you might have remapped in the add-on's Network settings, that
   remap doesn't apply to the internal Supervisor network the integration
   actually uses) and its Supervisor hostname, which for a locally-installed
   add-on is **not predictable** (not the slug, not `local_<slug>` either —
   on one real install it was a Supervisor-generated hash like
   `aa65dcfd-sds200-bridge`). If unsure, check from another add-on's
   terminal on the same internal network: `curl http://<guess>:8000/scanners`.
4. **Card**: auto-registers itself once the integration loads — it should
   just show up in Add Card search as "SDS200 Scanner". HA's own
   auto-registration (`add_extra_js_url`) races with Lovelace's card-type
   resolution and can leave the card showing as broken on first load;
   `sds200-card.js` self-heals that case once it actually finishes loading
   (see `docs/protocol-notes.md`'s "Card doesn't render" section for the
   full debugging trail). If it's ever still stuck, a hard refresh should
   clear it.
5. **Redeploying after a code change** (this repo iterates — expect to
   repeat this): `git pull` in your clone, then re-copy — but **not** with
   the bare `cp -r` commands from steps 2/3. Once the destination exists,
   `cp -r src/sds200 dst/sds200` copies *into* it rather than over it,
   leaving `/config/custom_components/sds200/sds200/` while HA keeps
   loading the stale outer copy — a code change that silently never takes
   effect. Delete first (or copy the directory's *contents*):
   ```
   rm -rf /config/custom_components/sds200
   cp -r /tmp/sds200ha/custom_components/sds200 /config/custom_components/sds200

   rm -rf /addons/sds200_bridge
   cp -r /tmp/sds200ha/addon/sds200_bridge /addons/sds200_bridge
   ```
   (`cp -r /tmp/sds200ha/custom_components/sds200/. /config/custom_components/sds200/`
   also works and preserves the directory, but won't remove files deleted
   upstream.) If the add-on is installed from this repo's **add-on
   repository URL** rather than as a local add-on, skip its copy entirely
   and use Settings → Add-ons → Add-on Store → ⋮ → **Check for updates**
   instead — Supervisor caches its clone and won't see new commits until
   you do. Either way, confirm the version actually moved
   (`config.yaml`'s `version` for the add-on, `manifest.json`'s for the
   integration) rather than assuming the copy took. Card-only changes (`custom_components/sds200/www/`) just need the
   file re-copied and a browser reload — no restart, it's served straight
   from disk. Integration (Python) changes need Home Assistant restarted;
   add-on changes need **Rebuild** (Settings → Add-ons → SDS200 Bridge →
   Rebuild — a plain restart reuses the old built image and won't pick up
   code changes) or uninstall/reinstall if there's no Rebuild button.

## The add-on's web UI

Reached from the add-on page's **Open Web UI** (or the sidebar's SDS200
panel). Served over Home Assistant ingress, so it is authenticated by HA
itself and needs no port of its own. Four tabs:

**Control** — one panel per scanner. Closed, it is a line about what the
scanner is hearing; open, it reads the display out live — channel over
department and system, signal as a meter, the rest as fields, with the three
soft keys under it carrying the scanner's own current labels — and drives
the keypad, rotary, volume and squelch. Anything the status feed has no
fields for (menus, the weather-alert screen) is under **Screen text**, as
the display's own lines, which opens by itself whenever that is all there
is. On the key grid, **Perm Avoid** avoids the channel on screen for good —
the Avoid *key* two along is the temporary one that a power cycle clears. It
works by pressing the scanner's own Avoid key twice, which is the only thing
this hardware saves to flash — so it is only offered **while the scanner is
stopped on a transmission**, since mid-scan the key press lands on whichever
channel it has moved to by then. The add-on reads the scanner's channel list
back afterwards and tells you whether the avoid actually took, rather than
trusting the scanner's acknowledgement. **Listen** plays the
scanner's audio in the
page (over a WebSocket — behind ingress that is the only transport Home
Assistant's frontend service worker doesn't cut off after ~30 seconds; see
`docs/protocol-notes.md`). If the scanner has `poe_reset_*` configured there's a Power-cycle
button too.

**History** — every call the add-on has heard, newest first, searchable and
filterable by scanner and by mode. Calls appear as they start and update as
they end.

How it works, and the limits that come with it: the scanner pushes nothing,
it only answers polls, so a "call" is reconstructed from the detail (`GSI`)
poll stream — a contiguous run of polls with the squelch open on the same
system/department/channel/talkgroup. Two consequences are properties of the
hardware rather than things a setting can remove:

- **A transmission shorter than the poll interval can be missed entirely.**
  At the 3s default a two-second reply will often fall between two polls.
  Each scanner's *Detail poll interval* setting trades that off: lower
  catches more, but this firmware answers detail polls slowly and drops
  some, so below about a second you lose more than you gain.
- **Timestamps and durations are poll-quantized**, and duration is a lower
  bound. A call's end also needs two consecutive idle polls to confirm,
  because one dropped poll is indistinguishable from the squelch closing —
  without that, one transmission would routinely split into two rows.

**Actions** — rules that fire something when a matching call comes in, when
a weather alert starts or clears, or when the scanner stops (and resumes)
scanning its lists. Each rule matches on any combination of system, department, channel/talkgroup
name, talkgroup ID, unit ID, tone/NAC, site, raw system type, frequency
(within a tolerance, so `462.612` and `462.6125` both work) and mode. Empty
fields are ignored, so an untouched rule matches every call — which is a
legitimate "log everything to my webhook" setup, not a mistake.

Types:

| Type | What it does |
|------|--------------|
| Fire a Home Assistant event | Puts an event on HA's bus (default type `sds200_reception`). Use it as an Event trigger in an automation — nothing to set up on the other end. |
| Call a Home Assistant service | e.g. `notify.persistent_notification`, `light.turn_on`. |
| POST to a webhook | Sends the call's details as a JSON body to any URL. |

Extra fields can be attached to any action, and their values accept
placeholders — `{label}`, `{channel}`, `{system}`, `{department}`,
`{frequency}`, `{tgid}`, `{unit_ids}`, `{mode}`, `{duration}`. Each rule has
a cooldown (keyed by rule *and* by what was heard, so an action for one
talkgroup doesn't swallow the next one's) and a **Test** button that fires
it immediately, ignoring both the match conditions and the cooldown — and
works before the rule is saved, which is when you actually want to find out
whether the URL or service name is right.

Every history row has a **Create action** button, which opens this tab with
a rule already filled in from that call's system, department, channel and
talkgroup — the usual way you find out you want a rule is hearing the call
that should have fired one.

The two Home Assistant action types go through the Supervisor proxy, which
is why the add-on declares `homeassistant_api: true`. If you are upgrading
and see a 401 from those actions, restart the add-on so Supervisor reissues
its token with the new permission.

### Digital modes

Each call is tagged with a normalized mode — `analog`, `p25`, `dmr`,
`nxdn`, `provoice`, `edacs`, `ltr`, `motorola` or `unknown` — so "do
something on any digital traffic" is expressible without knowing Uniden's system-type
naming. It's derived, in order, from the scanner's `P25Status` (the only
field that reports an actually-observed decode), then the programmed system
type, then the modulation.

**Caveat worth knowing before you rely on it:** only `Conventional` has
been seen in a capture from real hardware here. The rest of the mapping
comes from Uniden's own naming and is unverified. Anything unrecognized is
tagged `unknown` rather than guessed at — and every rule can also match the
raw `system_type` string directly, which is the reliable escape hatch if
your system reports something this doesn't classify. If you find one,
`_SYSTEM_TYPE_MODES` in `addon/sds200_bridge/app/reception.py` is the one
place to add it.

### Weather alerts

A weather alert parks the scanner on its alert screen, and it stays there
until somebody presses a key — so an unattended scanner silently stops
scanning until you notice. Two things address that:

- Actions can fire on **when a weather alert starts** / **clears**, so the
  alert can reach a notification or an automation like any other event.
- Per scanner, **Return to scanning after N seconds** (Settings, under
  Weather alerts; off by default). The add-on presses whichever soft key the
  scanner is showing a "to scan" label above, read off the display lines,
  so it keeps working if a firmware update moves it. A fallback key is there
  for a screen that offers no such label; it defaults to pressing nothing,
  since an arbitrary key on an unknown screen can hold or avoid a channel.

The `sds200.hold` service holds the scanner on the channel it is on (or
releases it — call it again, or pass `hold: true`/`false` to set the state
outright). It needs no target: the add-on looks up the current channel's
list index, which the scanner's hold command requires and which earlier
versions never supplied.

### When the scanner stops scanning without saying so

A scanner held on a **system or department** still reports itself as
scanning: the mode reads `Scan Mode`, the display reads `Scanning...`, the
control link answers every poll, and it really is scanning — one department
out of everything in the list. Nothing else in this project can tell that
apart from normal operation, because the scanner doesn't. It has happened
here three times in four days, twice for eleven hours and once for a day.

Per scanner, under **Stopped scanning** in Settings:

- **Report a held system/department after N seconds** (default 600). Channel
  holds are deliberately ignored — that is what the card's Hold button and
  `sds200.hold` set, so watching them would fire on ordinary use.
- **Report hearing nothing for N seconds** (default off). The cause-agnostic
  backstop: a channel hold left on for a day, squelch wound shut, an antenna
  knocked off. Off by default because how long is normal depends entirely on
  how busy your lists are.

Either fires the action event **when the scanner stops scanning its lists**
(and **…starts again** on recovery), so it can reach a notification like any
other event. While it stays stuck the event repeats hourly, so a
notification missed at 02:00 comes back — set a rule cooldown if you want it
quieter.

**It never presses a key to fix it.** Every episode of this was caused by
something pressing a key at a screen it had misread, so the watchdog reports
and stops there. To release one, use the soft keys on the **Control** tab,
or send a targeted hold release — `HLD,SYS,<index>,` or `HLD,DEPT,<index>,`
with the index from the status view, which unlike a soft key cannot land on
the wrong thing if the scanner moves while you are typing.

**Settings** — the scanners themselves, their weather-alert handling, the
stopped-scanning watchdog, the log level, and how many calls the history
keeps. Saving takes effect immediately; a scanner whose settings didn't
change keeps its audio session running untouched, and the weather and
watchdog settings apply without even a reconnect.

Each scanner also has an **Avoided channels** section, listing what
**Perm Avoid** has avoided on it, with **Un-avoid** to reverse one. That
list is the only way back: the scanner never stops on an avoided channel
again, so the list index it was avoided by can't be read off the scanner a
second time, and the add-on keeps it in `/data/avoids.json` precisely so it
survives a restart.

Un-avoiding takes effect immediately but **is not saved** — this hardware
only writes to flash when a permanent avoid is made, so an un-avoided
channel comes back if the scanner restarts first. The row stays on the list
saying so, and the next permanent avoid you make writes out everything
pending at once. **Forget** drops a stale row without sending anything.

Because a record is what was *sent*, the same section can ask the scanner
what is actually true. **Check** reads each listed channel back out of it
and marks any row it disagrees with — a stale record the scanner is no
longer avoiding, or one it holds only temporarily because the two key
presses landed too far apart. **Full scan** walks the entire database to
find avoids nothing here recorded, which is how a keypress that landed on
the wrong channel gets caught; it reads several thousand entries and takes a
few minutes, so it only ever runs when you ask for it. Both read the
scanner's working copy, so a confirmed avoid is one that is live now — a
power cycle is still the only thing that proves it was saved.

## Development

The add-on's container has no Supervisor-specific dependencies, so it also
runs as a plain `docker run` for Home Assistant Core-only setups — useful for
local development against a real scanner without a full Supervisor install.
It's also just a normal `aiohttp` app (`addon/sds200_bridge/app/main.py`),
so it runs directly with `python3` too, no Docker required, given `aiohttp`
is installed and `/data` is writable (point `config_store.CONFIG_PATH`
somewhere else if it isn't). With no settings file it starts up empty and
serves the settings UI at `http://localhost:8000/` — outside Supervisor
there's no ingress in front of it, so it's reachable directly.

### Tests

```
cd addon/sds200_bridge
python3 -m unittest discover -s tests -v
```

Mostly no external dependencies — the parsing/reassembly logic under test
(`protocol.py`, `xml_lists.py`, `key_codes.py`, `reception.py`,
`history.py`) is pure stdlib, and the tests that only *reach* code importing
`aiohttp` (`config_store.py`, `manager.py`, `audio_bridge.py`) stub it out
when it isn't installed. Two want the real `aiohttp`: `test_triggers.py`
exercises action delivery (through a fake session — nothing touches a
network), and `test_api_audio_ws.py` runs the audio WebSocket route against
a real server on loopback, skipping itself if `aiohttp` is missing.
Fixtures in
`tests/fixtures.py` are raw payloads captured from a real SDS200; see that
file's docstring for provenance/trimming notes.

The Lovelace card's screen-wake logic has its own test, in JavaScript, run
separately (it needs a JS runtime, which nothing else here does):

```
node tests/test_card_screen_lock.js
```

### Transcription

Turns what a scanner hears into searchable text on its receive-history row.
Off by default and enabled per scanner, under that scanner's **Transcription**
section in the add-on's settings; where speech-to-text happens is the
**Transcription** card under Add-on, which takes either the Home Assistant
Whisper add-on (Wyoming, port 10300) or any OpenAI-compatible server. There
is a **Test connection** button, because every way this fails otherwise looks
identical from a settings form.

`small.en` (or `small-int8`) is a sensible floor. Smaller models do not just
err more, they hallucinate more — weaker acoustic grounding lets the language
prior take over sooner, which on this audio is the wrong failure.

**Expect the gist, not a record.** The stream is 8 kHz telephone bandwidth,
and on a digital system it has already been through a vocoder that
resynthesizes speech rather than carrying it. Both cost exactly the detail a
speech model leans on. Good enough to search; not good enough to quote.

A call with no audio behind it is often not a fault. Squelch-open detection is
RSSI (`reception._is_receiving`), which asks whether there is RF in front of
the receiver — on a conventional channel that is much the same as whether
somebody is talking, and on a trunked digital system it is not. The site has
RF whenever it is active, while the scanner only unmutes for a talkgroup it is
monitoring, so those calls are logged and correctly produce no audio. The
history says so in the words that are true for the mode rather than claiming
nobody spoke.

Because a fabricated transcript is worse than none — it fires action rules
and answers searches with events that never happened — the pipeline is biased
hard toward writing nothing, and records *why* when it does: `no speech`,
`too short`, `discarded — too uncertain`, `discarded — boilerplate`,
`discarded — repeated itself`, or a server that could not be reached. All six
would otherwise render as an identical blank.

Audio is segmented by listening to the samples, not by the history's own call
boundaries. Measured on real hardware (2026-08-19) those disagree badly: a
call is noticed up to a GSI poll late and held open two polls after it ends,
and in three of six minutes sampled *every* frame carrying audio fell outside
the window history called a call. Squelched output was measured as *exactly* the mu-law silence code rather
than merely quiet — and stopped being so within a day, which is why the
detector now learns the noise floor from the audio instead of assuming
silence. It still collapses to the exact test when the floor really is zero.
See `audio_tap.DEFAULT_THRESHOLD` and `NOISE_MARGIN`.

Recent calls keep their audio (`transcribe.ClipStore`, a few hundred clips),
playable from the ▶ on the history row. That is the only way to tell a good
transcript from a plausible invention, and it is offered for rejected ones
too — those are exactly when you want to hear what the model was given.

Actions can fire on what was said — a `Something said (transcript)` criterion
under the Actions tab, with its own **when a call is transcribed** event. It
needs its own event because a call ends when the squelch has been shut for two
polls while its transcript lands after the model has had its turn; folding the
two together would either delay every rule that has nothing to do with speech,
or match nothing. A rule looking for a word never fires on a call whose
transcript was rejected or never attempted.

The integration exposes `sensor.sds200_<id>_transcript` — the last thing that
scanner was heard to say, with the untruncated text and the call's details in
its attributes (an HA state caps at 255 characters and a long transmission can
exceed it). It updates only when a transcript actually has text, so a
rejected or silent call never blanks it.

Transcription never touches the RTSP session: it reads the raw RTP payloads
through `AudioBridge`'s listener list, which attaches and detaches
independently of the session, and its settings are in
`manager.WATCH_ONLY_FIELDS` so toggling one never restarts a scanner.

