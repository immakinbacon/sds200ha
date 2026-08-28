# Uniden SDS200 protocol notes

Condensed reference distilled from Uniden's official specs (not redistributed
here verbatim — see source URLs below). Keep this in sync with `protocol.py`
as commands get implemented; this file is the map, the code is the territory.

Sources:
- `SDS_Series_RemoteCommand_Specification_V2_00.pdf` (2025/07/07) —
  https://info.uniden.com/twiki/pub/UnidenMan4/SDS100FirmwareUpdate/SDS_Series_RemoteCommand_Specification_V2_00.pdf
- `SDS200_Virtual_Serial_on_Network_Specification_V1_00.pdf` —
  https://info.uniden.com/twiki/pub/UnidenMan4/SDS200FirmwareUpdate/SDS200_Virtual_Serial_on_Network_Specification_V1_00.pdf
- `RTSP.pdf` (audio streaming) —
  https://info.uniden.com/twiki/pub/UnidenMan4/SDS200FirmwareUpdate/RTSP.pdf

## Transport

- **Control**: plain ASCII commands terminated with `\r`, sent as **UDP
  datagrams to port 50536**. Stateless request/response, no handshake, no
  auth. One command per packet; response comes back as one or more UDP
  packets (XML responses like `GLT`/`GSI`/`MSI` are paginated — see below).
- **Audio**: RTSP on **TCP port 554**, media path `au:scanner.au`, standard
  `OPTIONS → DESCRIBE → SETUP → PLAY → GET_PARAMETER (keepalive) → TEARDOWN`.
  Payload is **G.711 µ-law**, mono, delivered over RTP/UDP on a client_port
  negotiated in `SETUP`. RTSP `Session` has a `timeout=60`; must send
  `GET_PARAMETER` periodically (well under 60s) to keep it alive.

## Paginated XML responses (GLT, GSI, MSI)

Response is split across multiple UDP packets, each shaped like:

```
GLT,<XML>,
<?xml version="1.0" encoding="utf-8"?>
<GLT>
  <FL .../>
  ...
  <Footer No="N" EOT="0|1"/>
```

`Footer/@No` is a sequence number (detect gaps → drop and re-request).
`Footer/@EOT` = `1` marks the final packet. Reassemble by concatenating XML
body text across packets in `No` order until `EOT=1`, then parse as one XML
document.

## Command list (from Remote Command Specification v2.00)

| Cmd | Purpose |
|-----|---------|
| MDL | Get model info (`MDL,SDS200`) |
| VER | Get firmware version |
| KEY | Push a key (`KEY,<code>,<mode>` → `KEY,OK`) — see Key codes below |
| QSH | Go to quick search hold mode at a frequency |
| STS | Get current status — fixed-format display mirror (line text + mode bits) |
| JNT | Jump to a numbered tag (favorites list / system / channel) |
| NXT / PRV | Next/previous, target-word driven (see "tkd and 1st,2nd opt" table) |
| FQK / SQK / DQK | Get/set Favorites-list / System / Department quick-key status |
| PSI / GSI | Push/get scanner information — periodic XML status dump (richer than STS) |
| GLT | Get a named list (favorites, system, department, site, freq, TGID, ...) as paginated XML |
| HLD | Hold on system/department/channel |
| AVD | Avoid / temp-avoid / un-avoid a system/department/channel/frequency |
| SVC | Get/set service-type filter bitmap (see Service Type table) |
| JPM | Jump mode (switch scan/search/close-call/weather/etc. mode) |
| DTM | Get/set date & time |
| LCR | Get/set location + range |
| AST / APR | Analyze start / pause-resume (current activity, LCN monitor, activity log, LCN finder — trunking diagnostics) |
| URC | User record control (start/stop recording) |
| MNU / MSI / MSV / MSB | Menu navigation (enter, read state as XML, set a value, go back) |
| GST | Scanner status incl. waterfall/display metadata (superset of STS) |
| PWF / GWF / GW2 | Waterfall FFT data (text CSV / binary) |
| KAL | Keepalive — no response, send periodically to hold session-ish state |
| POF | Power off |
| GCS | Get charge status (battery voltage/%/current/temp) |
| VOL | Get/set volume (0-29 on SDS200, 0-15 on SDS100/150) |
| SQL | Get/set squelch (0-19 on SDS200, 0-15 on SDS100/150) |

### STS / GST display mirroring

Response: `STS,[DSP_FORM],[L1_CHAR],[L1_MODE],[L2_CHAR],[L2_MODE],...,[L20_CHAR],[L20_MODE],[RSV]x9`

- `DSP_FORM` is a string of 5-20 `0`/`1` digits — each digit is a per-line
  font-size flag (`0` small, `1` large) and its length determines how many
  `Lx_CHAR`/`Lx_MODE` pairs follow.
- Each `Lx_CHAR` is a fixed-width string (24 or 30 chars depending on
  `DSP_FORM` length/mode) — one visible line of the scanner's screen.
- Each `Lx_MODE` is the same width, with per-character formatting: `' '`
  normal, `'*'` reverse video, `'_'` underline.
- If a whole `Lx_CHAR`/`Lx_MODE` field is blank it's sent as `""`; literal
  commas inside a field are escaped as `\t`.
- `GST` extends `STS` with `MUTE`, `LED1`/`LED2` (alert/charge LED color),
  `WF_MODE` (0 normal / 1 waterfall / 2 menu), `FREQ`, `MOD`, marker/center/
  lower/upper frequency, `COLOR_MODE`, `FFT_SIZE`.

### PSI/GSI (richer XML status, pushed periodically)

Root `<ScannerInfo Mode="..." V_Screen="...">` with children whose presence
depends on `Mode` (see "Depend on mode elements" matrix in the spec) —
`MonitorList`, `System`, `Department`, `Site`, `ConvFrequency`, `TGID`,
`SiteFrequency`, plus `Property` (VOL/SQL/Sig/Battery/Mute/backlight/etc.),
`AGC`, `DualWatch`, and `ViewDescription` (`InfoArea1/2`, `OverWrite` error
banner, `PopupScreen` modal with its own buttons+key codes, `PlainText`
lines). This is the best source for structured "what is the scanner doing
right now" data — `STS`/`GST` are really for pixel-accurate screen mirroring.

### Key codes (`KEY` command) — from "key code for KEY Command" table

The table in the spec is headed `BCD536HP` / `SDS100`; no SDS200-specific
column is given (SDS100/SDS200/SDS150 share the command set). SDS200 is a
base/mobile unit with physical Volume and Squelch **knobs**, unlike the
handheld SDS100 — verify on real hardware whether `Q` (squelch knob push)
responds on the SDS200 even though the table marks it `(none)` for SDS100.

| Code | Key |
|------|-----|
| M | Menu |
| F | Func |
| L | Avoid |
| 0-9 | Number keys |
| . | Dot |
| E | Enter ("E yes") |
| > | Rotary right |
| < | Rotary left |
| ^ | Rotary knob push |
| V | Volume knob push |
| Q | Squelch knob push (verify on SDS200) |
| Y | Replay |
| A | Soft 1 (System) |
| B | Soft 2 (Dept) |
| C | Soft 3 (Channel) |
| Z | Zip |
| T | Service type (verify on SDS200) |
| R | Range |

`KEY,<code>,<mode>` → `KEY,OK`. `[KEY_MODE]` presumably short/long press —
confirm exact accepted values against a live scanner before wiring up the
card's press-and-hold behavior (e.g. for power off via long-press).

## Audio (RTSP/RTP)

- `OPTIONS rtsp://<ip>/au:scanner.au` → capability check.
- `DESCRIBE` → SDP body: `m=audio 0 RTP/AVP 0` (payload type 0 = PCMU/µ-law),
  `a=control:trackID=1`.
- `SETUP .../trackID=1` with `Transport: RTP/AVP;unicast;client_port=<port>`
  → scanner replies with its own `server_port` + `ssrc` and a `Session` id.
- `PLAY` starts RTP flowing to `client_port`. `GET_PARAMETER` on the session
  periodically to keep `Session` alive (timeout=60s from `SETUP`/`DESCRIBE`
  response — keep the interval comfortably under that, e.g. 20-30s).
- `TEARDOWN` to end.
- RTP packets: standard 12-byte header + raw µ-law payload (payload type 0).
  The payload goes straight into `ffmpeg -f mulaw -ar 8000 -ac 1 -i pipe:0
  ...` — no need to hand-decode µ-law→PCM ourselves.
- **Do not throw the header away.** It was stripped and discarded until
  0.7.44, which left arrival time as the only clock the transcription tap
  had. An arrival-driven timeline advances only by audio that *arrives*, so
  every lost packet slid it permanently earlier, cumulatively, for the life
  of the session — invisibly, and in exactly the way that attaches a
  transcript to the wrong transmission. `seq` (bytes 2-4) finds loss;
  `timestamp` (bytes 4-8) counts samples at 8kHz and is the media clock. The
  payload does not always start at byte 12 either: a CSRC list or a header
  extension pushes it further in and padding shortens it at the far end
  (`audio_bridge.parse_rtp`).
- Alignment between the audio and the receive log is **measured, not
  assumed** (`app/align.py`, from 0.7.52). Two timelines are kept per
  scanner — when recordings ran, and when transmissions ran — and
  cross-correlated to find the offset between the clocks. Positive means the
  audio led and the 3s GSI poll caught up; near zero or negative means audio
  arriving later than the poll notices the call, which is the regime where
  the row a clip lands on starts being the wrong one.
- **The voids carry the signal, not the durations.** A call seen at one poll
  records a duration of zero and one seen at two polls records 3s whether it
  ran 3s or 5s, so durations are quantized into uselessness — while a
  two-minute silence is two minutes on both sides. Matching the *pattern* of
  traffic and gaps also survives what matching events one at a time cannot:
  roughly a quarter of logged calls have no audio behind them, and
  transmissions shorter than a poll interval never get a row at all.
- Two properties of that correlation are worth keeping in mind before
  changing it. A channel that is almost never quiet has no pattern in it and
  must report *no answer* rather than the largest of a set of equal numbers
  (measured: ~2.3x peak-to-median on structured traffic, ~1.06x on
  back-to-back). And the peak is a **plateau**, not a spike — a row's
  on-interval sits inside its transmission, so a range of offsets contain it
  equally; the answer is the middle of that range, and taking the first
  offset to reach it biases the result by up to a poll interval.

## Confirmed against real hardware (SDS200, firmware 1.23.15, 2026-07-21)

- **`STS` is fast and reliable**: ~20-70ms round trip, occasional drop.
  `GST`/`GSI` are noticeably slower and flakier (0-4s, sometimes no response
  at all) — not a code bug, just how this firmware behaves. The add-on runs
  two independent poll loops per scanner rather than one: `STS` at 1/sec for
  the display mirror (`protocol.STATUS_POLL_INTERVAL`), `GSI` at 1/3sec,
  best-effort, for structured system/department/site/TGID/frequency data
  (`protocol.GSI_POLL_INTERVAL`). Both merge into the same `last_status`
  dict (`GSI`'s result nested under `"gsi"`) rather than being separate
  message types.
- **The scanner interleaves unsolicited/stale packets with real replies.**
  Observed a `GST` request's queue slot get filled by a stray `GSI` push,
  and the next command's slot get the `GST` reply arriving late. Naively
  treating "next datagram" as "my response" silently desyncs everything
  downstream. Fixed by matching each response against the sent command's
  prefix (`protocol.send_command`/`send_xml_command`) and discarding
  anything that doesn't match while still waiting on the deadline.
- **`GSI`/`PSI` responses are a different shape than `GLT`**: a single
  packet containing one complete, already well-formed XML document (root
  tag *with* attributes, e.g. `<ScannerInfo Mode="..." V_Screen="...">`),
  no `Footer`/pagination at all — unlike `GLT`'s multi-packet
  re-header'd-per-packet scheme. `xml_lists.reassemble` handles both shapes
  now; earlier versions failed to extract the root tag at all because (a)
  the root-tag regex didn't allow for attributes, and (b) a stray `\r`
  between the `<XML>,` marker and `<?xml ...?>` survived stripping and threw
  off the anchored match. Also: **line separators inside the XML body are
  bare `\r`**, not `\n`/`\r\n`.
- **Some display line text embeds control characters** (e.g. `\x1a\x1b`,
  `\x06\x07`) — looked like scroll/blink codes for a marquee-style
  long-name field mid-animation. `protocol._strip_control_chars` removes
  them before the text reaches HA/the card.
- Confirmed the full `ScannerConnection` production loop (not just ad hoc
  commands) against the real unit for 10-15s stretches: correctly tracked
  the scanner scanning across conventional systems and into a live P25
  trunked call (talkgroup, unit ID, NAC, RSSI, signal level all populated
  correctly via the `GSI` merge).
- **Ran the real add-on app** (not just the bare protocol client) against
  the live scanner -- REST API, WebSocket `/ws` status push, and a genuine
  40+ packet / 359-entry `GLT,SYS` multi-packet response all confirmed
  working end-to-end. This turned up two more real bugs, both fixed:
  - `element_to_dicts`/the reassembled list included a fake `Footer` entry
    for the *single-packet* GLT case (e.g. `GLT,FL`). Root cause: a
    single-packet GLT response is **both** a complete document (has its
    own closing `</GLT>`) **and** has `<Footer .../>` embedded before that
    closing tag -- a third shape distinct from the originally-assumed
    "multi-packet, footer only at the very end" case. The real multi-packet
    `GLT,SYS` capture confirmed *every* packet, continuation or not, has
    this same self-closing-document-with-embedded-footer shape; there's no
    "headerless continuation" packet at all. Fixed by removing the footer
    wherever it appears in the body (not anchored to end-of-string) and
    independently checking for a trailing closing tag to strip, since the
    two conditions aren't mutually exclusive.
  - `gsi_to_dict`'s `mode`/`v_screen` came back `null` even on a capture
    with `Mode="Scan Mode"` right there in the XML. Root cause:
    `reassemble()` only ever kept the root tag's *name*, not its
    attributes, when synthesizing the wrapper (`f"<{root_tag}>..."`
    silently dropped `Mode=`/`V_Screen=`). Fixed by capturing the full
    opening tag (name + attributes) from the first packet and reusing it
    verbatim in the synthesized wrapper.

  Regression tests for both, built from real captured raw UDP payloads,
  live in `addon/sds200_bridge/tests/`.

## Still open

- Confirm `KEY_MODE` accepted values (short/long press) on a live SDS200 —
  not yet exercised, only read/status and `VOL` set/get (confirmed working:
  live-decremented volume 4→3 via `VOL,3` and read it back) have been
  tested so far.
- Confirm SDS200-specific behavior for `Q`/`T` key codes (marked `(none)`
  for SDS100 in the shared table).
- **RTSP audio path (port 554) needs re-verification and hardening.**
  During testing, the first `OPTIONS` request got a `RTSP/1.0 400 Bad
  Request`, and on retrying, the RTSP TCP listener stopped accepting new
  connections entirely (`ConnectionRefusedError`) -- didn't recover after
  15+ seconds. The UDP control interface (port 50536) was completely
  unaffected throughout (confirmed `MDL` still responds normally), so this
  is isolated to the embedded RTSP/audio server, not the scanner as a
  whole. Before trying again: (a) get the exact `400`-triggering request
  right on paper first rather than iterating live against the device --
  candidates worth checking against the spec/a packet capture from a known
  working client (e.g. ProScan) are exact header casing/order, whether
  `CSeq` needs to start at a specific value, and whether a `User-Agent`
  header is required or disallowed; (b) once fixed, `audio_bridge.py`'s
  RTSP retry/backoff needs to be conservative (the embedded server seems to
  tolerate a single well-formed session but not rapid reconnect attempts)
  -- don't hammer it with retries the way ad hoc testing did here; (c) if
  the port is ever wedged like this again, a scanner power-cycle is the
  known recovery path (untested whether it also self-recovers given more
  time).

  **Implemented**: since the user confirmed power-cycling is the only fix
  they've found, `AudioBridge` now supports an optional
  `reboot_webhook_url` (+ `auto_reboot_on_audio_failure`) per scanner in the
  add-on config -- see `audio_bridge.py`. It's a plain webhook POST (not
  tied to any specific smart-plug brand), so it's opt-in and does nothing
  unless configured. Manual trigger via `POST /scanners/{id}/reboot` /
  `sds200.reboot` works regardless of the auto setting. There's no
  documented SDS200 network reboot command (an unofficial `MSM,5` UDP
  command surfaced on RadioReference forums via ProScan's author, but it's
  absent from the official spec and unverified here -- deliberately not
  used).

## HA integration review (no live HA available -- see "Testing without Docker/HA" below)

With Docker unavailable in the dev sandbox (no `docker` group membership,
no passwordless sudo) and no `homeassistant` package available via apt,
a full live-HA test pass wasn't possible. Did a careful re-read of every
`custom_components/sds200` file instead, cross-checked against what the
add-on actually returns (confirmed live). Found and fixed:

- **Real bug**: `sensor.py`'s frequency/mode sensors and `binary_sensor.py`'s
  muted sensor read `status["freq"]`/`status["mod"]`/`status["mute"]` --
  fields that only ever existed when the add-on polled `GST`. After
  switching to the two-tier `STS`+`GSI` polling (see above), those are gone
  from the top level; the real data lives nested under `status["gsi"]`, and
  is itself mode-dependent (`ConvFrequency` for conventional scanning vs.
  `Site`/`SiteFrequency` for trunked). These sensors would have silently
  shown `None`/unavailable forever. Fixed with `_extract_frequency`/
  `_extract_mod` helpers that check both shapes; verified by hand against
  both a real conventional-scan capture and a real trunked-call capture,
  and confirmed live against the running add-on.
- **Correctness gap**: `coordinator.async_setup()` failing (add-on
  unreachable) wasn't wrapped in `ConfigEntryNotReady`, so a transient
  failure (e.g. add-on still starting when HA boots) would hard-fail the
  config entry instead of HA retrying automatically. Fixed.
- **Correctness gap**: services (`sds200.key`/`hold`/`avoid`/`set_volume`/
  `set_squelch`/`reboot`) were registered without voluptuous schemas, so
  the "required"/type constraints declared in `services.yaml` only applied
  to the UI form, not actual service calls (a bad programmatic call would
  raise a raw `KeyError` instead of a clean validation error). Added
  schemas; validated standalone against the real `voluptuous` package.
- **Cleanup**: removed a redundant explicit `device_registry.async_get_or_create`
  loop in `__init__.py` -- every scanner unconditionally gets entities in
  all three platforms, and each entity's `_attr_device_info` already causes
  HA to create the device; the explicit loop just duplicated that.
- **Cleanup**: `coordinator.async_shutdown()` cancelled the websocket task
  without awaiting it, which can produce "task was destroyed but it is
  pending" warnings on unload/reload. Fixed to await the cancellation.
- **Unverified, flagged rather than guessed around**: `StaticPathConfig`'s
  exact constructor signature and `homeassistant.data_entry_flow.FlowResult`'s
  import path have moved across HA versions; `__init__.py`'s frontend
  registration already has a fallback + comment for the former. Both need
  a real HA instance to confirm against the target version.

### Testing without Docker/HA in this sandbox

Neither Docker nor `pip`/`sudo` were available here (see earlier notes), but
`apt-get download <pkg>` + `dpkg-deb -x` (no root needed) got `aiohttp` and
`voluptuous` running locally, which unlocked two useful things beyond the
pure-stdlib `addon/sds200_bridge/tests/` suite:

- Running the add-on's real `main.py`/`api.py` directly against the live
  scanner (not just the bare protocol client) -- see the bugs found above
  the "Confirmed against real hardware" section.
- `tests/test_integration_api_client.py` (repo root): spins up the add-on's
  real `api.py` app in-process via `aiohttp.test_utils.TestServer` and
  exercises the HA integration's real `api_client.py` against it end to
  end, with a stubbed `ScannerConnection`/`AudioBridge` standing in for
  actual UDP/RTSP I/O. This is what caught the AppKey deprecation warning
  and is the shape of test that would have caught the `sensor.py` bug above
  if it had existed before that bug was introduced -- worth extending as
  the integration grows, since it doesn't need `homeassistant` at all
  (`api_client.py` has no such dependency).

Still can't exercise `custom_components/sds200`'s actual HA-dependent code
(`__init__.py`, `coordinator.py`, entity platforms, `config_flow.py`) this
way -- that needs either a real HA instance or `pytest-homeassistant-custom-component`,
neither available here.

## First real HA OS install (2026-07-21)

- **Add-on hostnames for locally-installed add-ons aren't predictable.**
  Assumed `local_<slug>` (a common convention for repository add-ons
  installed as "local"); actual result on a real HA OS install was a
  Supervisor-generated hash prefix: `aa65dcfd-sds200-bridge`. Removed the
  wrong-guess `DEFAULT_HOST` in `const.py` (was `"sds200_bridge"`) rather
  than replace it with another guess -- the config flow's host field just
  starts blank now, with strings.json's description explaining how to find
  the real one (`curl` from another add-on on the same internal network, or
  `ha addons info sds200_bridge`).
- **Host-external port remaps don't change the container's internal port.**
  The add-on's Network tab in the Supervisor UI lets you remap the
  add-on's *host*-exposed port (useful for resolving a conflict with
  another add-on already using 8000) -- but Supervisor's internal Docker
  network (which is what the integration talks over) always uses the
  container's own internal listening port, which is whatever `main.py`'s
  `HTTP_PORT` actually binds to (8000, hardcoded, regardless of any host
  remap). Confirmed via the add-on's own log line still showing
  "listening on :8000" after a host-side remap to 8001. The config flow's
  description now calls this out explicitly since it's a non-obvious trap.
- **Still open, surfaced by the above but not yet fixed**: the card's audio
  `stream_url` is built from the same host/port the integration uses for
  its internal REST/WS calls -- but that's the Supervisor-internal hostname,
  which the *browser* (running the card) can't resolve at all, independent
  of the port-remap issue. Needs a separate "browser-reachable" address
  (the HA host's real LAN IP/hostname + whatever external port the add-on
  is actually mapped to) distinct from the internal one. Not yet
  implemented.

## Uniden logo asset

`custom_components/sds200/logo.svg` and `custom_components/sds200/www/uniden-logo.svg`
are the same file: Uniden's own logo, fetched directly from their official
site (`uniden.com/cdn/shop/files/Uniden_weblogo_black.svg`), used here only
to identify that this project interfaces with a Uniden product (shown on
the Lovelace card, filtered white via CSS to read on the dark panel). It's
Uniden's trademark, not something this project has rights to beyond that
identifying/nominative use -- this is a private repo, not a public
redistribution, and the intent is purely "this integrates with a Uniden
SDS200," not implying endorsement.

Initially placed as SVG only (no PNG rasterizer available in the dev
sandbox). Turned out `librsvg2-2` and its full dependency chain (cairo,
pango, glib, libxml2) were already installed as base system libraries --
only the `rsvg-convert` CLI binary itself was missing, and that's a single
small package (`apt-get download librsvg2-bin`, same no-root trick as
aiohttp/voluptuous). Rendered `addon/sds200_bridge/icon.png` (128x128) and
`logo.png` (256x256): `rsvg-convert -w <n> -h <n> -a uniden-logo.svg` (the
source is a wide wordmark, not square, so `-a`/keep-aspect-ratio produces a
non-square PNG on its own), then composited onto a white square canvas with
Pillow (`python3-pil`, also already installed) since the raw logo is
black-on-transparent and would be invisible against Supervisor's UI in some
themes. These two files are Supervisor's own local add-on icon convention
(picked up directly from files in the add-on's folder, no central registry
needed) -- confirmed this works differently from the *integration*-side
icon question below.

**Integration icon (Settings > Devices & Services) is a separate, harder
problem**: HA only pulls integration icons from the centralized
`brands.home-assistant.io` repo by domain, with no supported local-file
fallback for a private/unlisted custom integration. `custom_components/sds200/logo.svg`
is kept as a starting point for that submission if ever wanted (needs the
project to be public + going through home-assistant/brands' process +
PNG renders, same tooling as above), but it will not make an icon appear
in the integrations list on its own.

## Frequency sensor showed "unavailable" (real HA OS install)

Symptom: `sensor.sds200_<id>_frequency` showed HA's "unavailable" state
while `sensor.sds200_<id>_display` worked fine on the same scanner at the
same time -- pointed at something frequency-specific, not a
coordinator/connectivity problem (display and frequency share the exact
same `available` check).

Root cause: the scanner's raw `Freq` value has the unit baked directly into
the string (e.g. `" 462.612500MHz"`), and `_extract_frequency` was just
`.strip()`-ing it, returning that whole string as `native_value` --
`SDS200FrequencySensor` also sets `native_unit_of_measurement = "MHz"`
separately, and HA requires `native_value` to be numeric when a unit is
set. Fixed by stripping the "MHz" suffix and converting to `float`.

Not caught by the earlier "sensor.py bug" fix (which addressed reading the
right *nested key* for frequency) because that fix never exercised what
type the extracted value actually was -- verified manually this time
(`float(" 462.612500MHz".strip().removesuffix("MHz"))` etc.) rather than
adding a `homeassistant`-stubbed automated test, since stubbing enough of
`homeassistant` to import `sensor.py` for a 5-line pure function isn't
proportionate to the fix.

## New entities and card redesign (2026-07-21, user request)

- Added `number.sds200_<id>_squelch` (optimistic, same pattern as
  media_player's volume) so the card has a real state to step from, and so
  squelch is controllable outside the card too.
- Added `sensor` entities for `ctcss_dcs` (GSI `SAS`/`SAD`, prefers the
  actually-decoded value over the configured search setting), `rssi` (GSI
  `Property.Rssi`; `-999` is the scanner's own "not receiving" sentinel,
  mapped to `None`/unknown rather than shown as a real dBm value), `system`/
  `department` (GSI `System.Name`/`Department.Name`, same key names in both
  conventional and trunked captures, no mode branching needed), and
  `channel` (mode-dependent like frequency: `ConvFrequency.Name`
  conventional, `TGID.Name` -- the talkgroup -- trunked). `noise` has no
  structured GSI field at all; it only ever appears as literal text
  (`"NOISE:38500"`) embedded in an STS display line, so it's regex-extracted
  from the raw display text instead. Units/scale for `noise` aren't
  documented anywhere -- only ever observed on the scanner's own screen.
  All extraction helpers verified by hand against real captured
  conventional and trunked GSI data (see sensor.py).
- Card redesign: replaced the volume/squelch sliders with +/- stepper
  button pairs (the protocol's VOL/SQL are absolute-set commands, so
  stepping is computed client-side from the current state and clamped to
  the real 0-29 / 0-19 hardware ranges); added a soft-key row (System/Dept/
  Channel, KEY codes A/B/C) matching the real front panel's row of buttons
  below the screen; added a menu-navigation row (Menu plus rotary left/
  select/right, KEY codes M/</^/>); added a Restart button calling
  `sds200.reboot` behind a confirm() dialog (only functions if that
  scanner has a `reboot_webhook_url` configured in the add-on).

## Card doesn't render: unresolved (2026-07-22)

Extensive live debugging session on a real HA OS install, still
unresolved. Documenting the full trail so this doesn't get re-litigated
from scratch next time.

**Symptom**: dashboard shows "Configuration error: Custom element doesn't
exist: sds200-card" for the card, whether pre-existing or freshly added
(including via raw YAML, bypassing the Add Card search picker entirely).

**Ruled out, with evidence**:
- Not a syntax error: `node --check` passes clean.
- Not a logic bug reachable from normal use: executing the file in a
  Node harness with mocked DOM globals runs cleanly through to both
  `customElements.define()` calls with no exception.
- Not a missing/mismatched element id: every `querySelector`/
  `querySelectorAll` target in the JS was cross-checked against actual
  `id`/`class` attributes in the HTML template -- all present.
- Not a stale/wrong file being served: content fetched directly from the
  URL matches expectations; a byte-for-byte identical copy, pasted
  directly into the browser console, DOES successfully define the element
  and render the card correctly, every time.
- Not a MIME-type issue: response `Content-Type` is `text/javascript`,
  which is valid for `<script type="module">`.
- Not the file simply never being fetched: confirmed present in Network
  tab, filtered to "sds200", specifically during the *dashboard's own*
  page load (not just a manual URL visit).
- Not HTTP/service-worker caching: a brand new incognito window with a
  fresh login, and separately a fully closed-and-reopened browser tab,
  both still fail the same way.
- Not a different JS realm/frame: `window === top` is `true` at the point
  of failure.
- Not YAML-mode dashboard (Resources UI only applying to Storage-mode
  dashboards, a real and common gotcha): confirmed Storage/UI mode.
- Not `add_extra_js_url` being masked by the manual resource: retested
  with the manual resource entry fully removed, in incognito with a fresh
  login -- still nothing, and page source contained no injected `<script>`
  reference to the file at all. `add_extra_js_url` appears to do nothing
  at all in this environment (not investigated further yet -- see the
  new diagnostic logging added in `__init__.py`, elevated to `warning`
  level specifically so its actual effect, or lack of one, is visible
  without needing debug logging enabled).

**The genuinely confusing part**: with the manual Lovelace resource in
place, added `console.log` markers at the top of the file, right after
each `customElements.define()` call, and at the very end. On a real
dashboard reload, *all three* fired, ending with
`"reached end of file, registered? true"` -- i.e. the script's own,
in-the-moment check of `customElements.get("sds200-card")` confirms
success. Despite that exact same page load, in the exact same window,
Lovelace still showed the card as broken, and stayed broken after
removing/re-adding the card widget without reloading (so *after* that
confirmed-successful registration, with no new page load in between).

This doesn't fit a simple load-order race (Lovelace checks before the
resource finishes loading, doesn't recheck) -- that would predict the
post-registration re-add attempt succeeding, and it didn't. It also
doesn't fit a frame/realm separation issue (`window === top` is true) or
a duplicate-definition collision (only one resource entry). Genuinely
unclear what's actually happening. Original working theory (race made
worse by the file growing larger across several feature-addition commits)
is plausible but unconfirmed and doesn't explain the post-registration
re-add failure.

**Next things to try** (not yet done):
- Check HA core logs for the new `warning`-level `add_extra_js_url(...)`
  log line added in `__init__.py` -- either it'll show the registered
  URL set (proving the call succeeds server-side but something else is
  wrong) or an exception (finally explaining *why* it doesn't work).
- Try a *completely different* browser (not just a new tab/incognito in
  the same browser) to rule out a browser-specific quirk.
- Try the card on a different, brand-new, minimal dashboard/view rather
  than the existing "control-panel/wireless" one, in case something
  dashboard-specific (a stale saved error state tied to that specific
  card's config entry, persisted server-side) is involved.

## Card doesn't render: root cause found (2026-07-22)

Resolved the mystery from the section above. Checked the actual served
page source (`View Page Source` on HA's root page) and found HA's
`add_extra_js_url` injects:

```html
<script>import("/sds200_static/sds200-card.js");</script>
```

A **bare, unawaited dynamic `import()`** -- not a static
`<script type="module" src="...">` tag. This is the actual root cause of
everything in the section above. Static module scripts get browser-level
ordering guarantees (they behave like `defer`, finishing before
DOMContentLoaded-driven app code runs); a fire-and-forget `import()` call
has none of that -- it just kicks off the fetch and returns immediately.
Lovelace's own bundle evidently finishes checking "does `sds200-card`
exist?" before this import's promise resolves, and doesn't recheck once it
does -- consistent with every symptom observed: script always eventually
loads and registers successfully (confirmed via the console.log markers),
Lovelace still shows "doesn't exist" on the same page load, and it doesn't
self-correct for a freshly-added card either (the "doesn't retry" part
isn't just about the original placeholder -- Lovelace's card-type
resolution for that type seems to be decided once per page session).

The manual "Resources" mechanism isn't meaningfully different -- it's
async by essentially the same shape, just with an extra layer (client-side
resource-list fetch, then load), so it's just as racy, not a more reliable
alternative to add_extra_js_url as originally assumed.

**Can't fix the injection mechanism itself** (that's HA core's own
implementation, not something this integration controls). Implemented a
workaround instead: `sds200-card.js` now self-heals. Once the module
actually finishes loading (whenever that happens to be), it scans the DOM
for `<hui-error-card>` placeholders whose config says they were meant to
be `custom:sds200-card`, and replaces them with a real, working instance;
a `MutationObserver` also catches placeholders Lovelace renders *after*
this script has already run. This is best-effort / unverified against
`hui-error-card`'s actual internal shape (assumes a `.config` property
holding the original card config, based on general lit-element-based HA
card conventions, not confirmed against source) -- if that assumption is
wrong, it simply finds nothing and does nothing, same as before.

Diagnostic console.log markers (added earlier while tracking this down)
are still in place for this next round of testing; remove once the
self-heal approach is confirmed working (or found not to help, in which
case the next lever to pull is probably filing/checking for an upstream
HA frontend issue about add_extra_js_url using unawaited import()).

## Self-heal didn't fire on first real-install test: shadow DOM (2026-07-22)

Tested the self-heal workaround above on a real install. Saw only 3 of the
4 diagnostic markers -- `"script execution started"`, `"defined ...
registered? true"`, `"reached end of file, registered? true"` -- but never
`"healed a broken card placeholder"` *or* the `"failed to heal"` warning,
while the card was confirmed still visibly broken. That means the scan
found **zero** `<hui-error-card>` elements at all, on either the initial
`document.querySelectorAll` pass or via the `MutationObserver`.

Root cause: Lovelace's card tree is built almost entirely out of nested
shadow roots (`hui-view` -> `hui-card` -> ... each attaching its own
`shadowRoot`). Plain `querySelectorAll` and a `MutationObserver` observing
`document.body` both only ever traverse **light DOM** -- neither can see
past a shadow boundary. The broken `<hui-error-card>` placeholder was
there; the original code had no way to reach it, regardless of timing.

Fix: rewrote the scan as a `deepQuery` helper that recurses into
`el.shadowRoot` wherever one exists, and `observeDeep`, which sets up a
`MutationObserver` per shadow root (not just on `document.body`) and
recurses into any shadow roots it finds along the way. To catch shadow
roots created *after* this script runs (e.g. navigating to a
not-yet-rendered dashboard view), also patched
`Element.prototype.attachShadow` globally to call `observeDeep` on every
new shadow root as it's created -- a per-root observer has no way to learn
about a sibling shadow root that doesn't exist yet. Not yet re-tested
against a real install; that's the next thing to confirm.

Worth noting: patching `attachShadow` on the global `Element.prototype` is
a page-wide side effect for the lifetime of the tab (every custom element's
shadow root creation now also triggers our observer setup) -- broader than
this card's own footprint, but the intercept itself is a thin pass-through
(calls the native method, observes the result, returns it unchanged) so it
shouldn't alter any other element's behavior.

## Third real-install test: unbounded card duplication (2026-07-22)

Deployed the ancestor-config-search fix live via SSH access to the HA host
(see below) without a restart -- confirmed via `curl` that the static file
is served straight from disk on every request, so no HA restart is needed
for card-only changes, just a browser reload. The card rendered this time,
but an unbounded, continuously-growing number of duplicate cards appeared
-- not a fixed handful, kept climbing the longer the dashboard stayed open.

Checked HA frontend source (`create-element-base.ts`) for anything that
might independently retrigger card creation, and found HA has its **own**
native repair path for exactly this "custom element not defined yet at
creation time" race: `customElements.whenDefined(tag).then(() =>
fireEvent(element, "ll-rebuild"))`, which asks the parent card to recreate
the child once the type becomes defined -- running completely independently
of this workaround.

The actual bug: `healBrokenCard` had no memory of having healed a slot
before. Whatever the exact interaction between our MutationObserver-based
healing and HA's own `ll-rebuild` path (not fully root-caused -- plausibly
each fires its own attempt for the same slot, or something server keeps
re-emitting placeholders while stale internal state lingers on Lovelace's
side), *every* fresh `hui-error-card` seen for the same original config
produced a brand new `sds200-card`, with nothing removed in between.

Fix: made healing idempotent per slot, keyed by the original config object
reference (stable across repeated heals, since `findOrigConfig` always
pulls it from the same underlying `_config` tree) via a `WeakMap` from
config -> the live replacement element already created for it. A later
placeholder for an already-healed config is just dropped (`errorEl.remove()`)
instead of spawning another card. This sidesteps needing to fully
root-cause *why* more placeholders keep appearing -- regardless of cause,
the dashboard converges to exactly one card per slot. Not yet re-tested;
next step is confirming this actually stops the growth on a real install.

## Fourth real-install test: healed card renders once, then freezes (2026-07-22/23)

Idempotency fix stopped the card duplication -- exactly one card rendered,
stable. But its display never updated after that, and interactions were
half-broken: buttons technically worked (clicking a keypad key did reach
the scanner), but volume +/- only nudged once and then stopped responding
to further clicks.

Root cause: `errorEl.replaceWith(replacement)` swaps the DOM node, but the
*parent* (e.g. `hui-vertical-stack-card`) caches its child elements once at
`setConfig()` time (`hui-stack-card.ts`'s `_cards`) and never re-derives
that array -- every subsequent hass update is just `this._cards.forEach(
(card) => { card.hass = this.hass; })` against those original cached
references. Our replacement was never one of them, so it got `hass`
exactly once (manually, in `healBrokenCard`, at heal time) and never again.
Explains every symptom: display frozen at the heal-time snapshot; buttons
worked because `hass.callService`/device-registry lookups don't need
freshness; volume stepper "sort of" worked because `_stepVolume` computes
its target from `this._volumeRaw`, which is set in `_render()` and thus
also frozen -- first click nudges the real entity correctly, but every
click after computes the same stale `current + 1` target since the card
never learns the entity actually changed, so the UI appears to stop
responding after one step.

First fix attempt: `repointHostReference()` -- reach into the parent and
replace its cached reference (checked the known `_cards` array shape, plus
a generic scan of the parent's own properties for anything `=== errorEl`).
Tested on the real install via a targeted console diagnostic
(`host._cards.includes(replacementElement)`) -- came back `false`. The
cached reference genuinely wasn't where expected/patchable this way;
didn't chase the exact internal shape further (create-card-element.ts's
factory wraps element creation with its own `ll-rebuild` event plumbing
that we didn't fully trace -- see the "native repair path" note above).

Actual fix: gave up on patching the parent's internal caching at all.
Instead, `sds200-card` now polls the frontend's root `<home-assistant>`
element directly (a single, stable, always-present light-DOM element
holding the master, always-current `hass` object) every second and pushes
it in via the card's own `set hass()` whenever the reference changes --
independent of whatever Lovelace's own (for us, broken) propagation does.
Removed `repointHostReference` entirely since it was confirmed non-
functional and added real risk (mutating another component's internal
state) for no benefit. Not yet re-tested on the real install.

## SSH access to the real HA host (2026-07-22)

Got SSH access (HA OS's Terminal & SSH add-on, Alpine-based, `/config` ->
`/homeassistant`) to speed up iteration -- no more relying on the user to
manually redeploy every change. Key findings from this session, worth
keeping in mind for future debugging:
- Password auth works; a manually-added `~/.ssh/authorized_keys` entry
  does **not** grant passwordless login -- the add-on apparently manages
  its own auth separately from the container's normal `~/.ssh`. Password
  (via `SSH_ASKPASS`/`SSH_ASKPASS_REQUIRE=force`, no `sshpass` needed) is
  the reliable path.
- `docker`/`ha` CLI access is blocked by the add-on's "Protection mode" --
  fine for this bug (frontend-only), but means no easy access to HA core's
  live logs from this shell without the user disabling it (a destructive-
  capable setting, not asked for here).
- Static files under `/sds200_static/` are served straight from disk on
  every request (`cache_headers=False`, confirmed via `curl` before/after
  overwriting the file) -- redeploying `sds200-card.js` only needs the file
  copied to `/config/custom_components/sds200/www/` and a browser reload,
  **no HA restart**, unlike integration/Python changes.
- `/tmp/sds200ha` (the on-host git clone used for redeploys) is owned by
  root from an earlier root session; the `homeassistant` SSH user can't
  `git pull` it directly (permission denied on `.git/FETCH_HEAD`), and that
  clone's `origin` is an unauthenticated HTTP remote anyway. Simplest path
  for pushing a single-file fix during active debugging: base64-pipe the
  file over SSH and `sudo tee` it directly to the deployed path, bypassing
  the git clone entirely.

## Screen's right-hand columns not lining up (2026-07-23)

Reported from a real-install screenshot: labels like `TGID:`, `Site ID:`,
`NOISE:`, `RSSI:` in the multi-field status block didn't visually line up
in a column, despite looking like they should.

Checked the actual raw display text character-by-character (not guessed):
in every affected line, the second column starts at the exact same
character offset (column 16, e.g. `"Sys ID: ---     TGID: ---"` vs.
`"WACN: ---       NOISE:34744"` -- 9 chars + 7 spaces = 16 either way). The
device's own padding is correct; this was never a data/parsing bug.

Root cause: `.panel`'s CSS was `font-family: "Courier New", monospace` --
naming a specific font first. If that exact font isn't installed (e.g. a
Linux desktop with no "Courier New"), the browser/OS can substitute a
similarly-named or metrically-close font that *isn't* perfectly fixed-width
for every glyph, silently breaking column alignment that's only correct
under a truly monospace assumption. Fixed by dropping the named font
entirely and using bare `font-family: monospace` -- the CSS generic
`monospace` keyword is the one case browsers actually guarantee uniform
glyph width for, regardless of what's installed on the host. Not yet
re-confirmed against the real install after this change.

Turned out to be necessary but not sufficient: after the font fix, still
reported as "sits in the middle of the window, not right side aligned" --
a second, separate bug. `.screen` had no explicit width, so it just
stretches to fill the whole card; the fixed-width scanner text (24 or 30
chars, per "STS / GST display mirroring" above) only needs a fraction of
that, leaving it floating with empty space on the right instead of filling
the box edge-to-edge the way the real LCD's bezel does. Fixed by sizing
`.screen` to `width: 30ch` (the wider of the two possible line lengths,
`max-width: 100%` as a floor for narrow cards) so the box hugs the actual
content instead of stretching past it.

## Weather alert sensor (2026-07-23)

Wanted a binary_sensor for "did the scanner receive a weather alert."
Checked live data first rather than guessing: queried the add-on's
`/scanners/{id}/status` endpoint over SSH (see "SSH access" above) while
the scanner was in normal trunk-scan mode. Two candidates showed up in the
`gsi` payload:
- `DualWatch.WX` = `"Priority"`/`"Off"` -- this is just whether weather
  dual-watch monitoring is *enabled*, confirmed against the spec (`WX
  Off/Priority` under the `DualWatch` menu-input section) -- not an alert
  state.
- The display's own last line, e.g. `"N                        WX "` --
  just the scanner's own on-screen indicator text, not structured data.

Neither is the right field. Found the actual one in the Remote Command
spec PDF: a `WxMode` element (`Mode="Monitor Weather"` or `"Weather
Alert"`, plus a `SAME` attribute -- `"Alert Only"` or the actual SAME group
name) that, per the spec's "Depend on mode elements" matrix, is **only
present in GSI/PSI while the scanner is actually in weather monitor/alert
mode** -- consistent with it being absent entirely from the live payload
we pulled (scanner was trunk-scanning, not in weather mode). `gsi_to_dict`
already carries over arbitrary GSI children generically, so no
protocol/xml_lists.py changes were needed -- just a new
`SDS200WeatherAlertSensor` in `binary_sensor.py` reading
`gsi.get("WxMode", {}).get("Mode") == "Weather Alert"`, exposing `SAME` as
an attribute. Unlike the existing muted sensor, a missing `WxMode` key is
treated as a real `False` (not monitoring/alerted), not `None` -- its
absence is meaningful by design per the mode matrix, not missing data.

~~Not yet confirmed against an actual live weather alert~~ -- **confirmed
2026-08-11 against a real alert on the hardware**, exactly as written above:
`<WxMode Mode="Weather Alert" SAME="Alert Only" />`. See "A real weather
alert, caught live" below, which also found what the spec-only reading
missed.

## Audio playback resolved: proxy through HA core (2026-07-23)

Confirmed live via a real browser console what the "Still open" note above
predicted: `media_player`'s `stream_url` pointed at
`http://aa65dcfd-sds200-bridge:8000/scanners/.../audio/stream.mp3` -- the
add-on's Supervisor-internal hostname, which the browser can't resolve at
all -- and separately hit `Mixed Content: Upgrading insecure display
request ... to use 'https'` (HA's frontend is HTTPS, the add-on is plain
HTTP), so even a browser-reachable address alone wouldn't have fully fixed
it.

Rather than the "still open" note's originally-suggested fix (a second,
LAN-facing address distinct from the internal one, needing the user to
figure out/configure the add-on's externally-mapped port), added
`SDS200AudioProxyView` (`__init__.py`) -- an HA `HomeAssistantView` at
`/api/sds200/{scanner_id}/audio/stream.mp3` that fetches from the add-on's
existing internal URL (same client HA core already uses successfully for
every other REST/WS call) and streams the response straight through.
`media_player.py`'s `stream_url`/`media_content_id` now point at that
proxy's path -- deliberately relative (no scheme/host), so the browser
resolves it against whatever origin it's already using to reach HA. Fixes
both problems at once with no new configuration: same-origin (no unreachable
hostname) and same-scheme (no mixed-content block).

Separately, while investigating this, noticed a second, not-yet-explored
possible bug in `audio_bridge.py`'s RTSP client: `path = "/trackID=1" if
method == "SETUP" else "/"` gives OPTIONS/DESCRIBE/PLAY/GET_PARAMETER/
TEARDOWN a URL of `rtsp://<host>/au:scanner.au/` (trailing slash), but the
documented working format (this file's "Audio (RTSP/RTP)" section) is
`rtsp://<ip>/au:scanner.au` with **no** trailing slash -- a real candidate
for the earlier-documented `400 Bad Request`/wedged-port incident. Not
touched yet -- per the existing "Still open" caution, want to nail the
exact request format against the spec/a packet capture before trying
anything live against the RTSP port again, given it wedged for 15+ seconds
last time with no recovery but a power-cycle.

**Follow-up, same day**: the relative-path proxy fix above got past the
unreachable-host and mixed-content problems (confirmed live: the browser
correctly requested the same-origin HTTPS proxy URL this time) but hit a
new `401` on the proxy URL itself. Root cause: a plain `<audio src=...>`
load is a normal browser resource fetch, not a request this integration
controls -- there's no way to attach the `Authorization: Bearer ...` header
HA's frontend normally uses, and `media_content_id`/`stream_url` are read
as a plain entity property (whenever HA serializes state), not during an
active HTTP request, so there's no "current session" to derive auth from
either. Fixed by signing the path (`async_sign_path(..., use_content_user=
True)`, `homeassistant.components.http.auth`) -- the same mechanism HA's
own TTS/media-source URLs use for browser-loadable media independent of
the current user session. Not yet re-confirmed against the real install.

**Second follow-up, same day**: after the signing fix, no more console
errors, but still no actual sound. Isolated it from the browser/proxy layer
entirely: `curl`'d the add-on's own stream.mp3 endpoint directly (over SSH)
and got instant `200 OK` with correct headers (`Content-Type: audio/mpeg`,
chunked), but **zero bytes** of body in 8 seconds -- the add-on's HTTP
layer is fine, but nothing is actually feeding ffmpeg, meaning the RTSP
session to the scanner itself isn't producing audio.

Re-checked the RTSP spec's actual packet trace (not just the prose
summary) for the exact URL format per method, and found `audio_bridge.py`
had it backwards for exactly two of the five methods: `OPTIONS` and
`DESCRIBE` should address the bare path with **no** trailing slash
(`rtsp://<host>/au:scanner.au`), while `SETUP`/`PLAY`/`GET_PARAMETER`/
`TEARDOWN` correctly use one (matching the `Content-Base` `DESCRIBE`'s
response returns). The code gave every non-SETUP method the trailing-slash
form -- so `PLAY`/`GET_PARAMETER`/`TEARDOWN` were already right, but
`OPTIONS`/`DESCRIBE` (the very first two requests of every session) were
wrong, a strong candidate for the earlier-seen `400 Bad Request`. Fixed the
per-method path logic. Not yet re-tested live -- this needs an add-on
**rebuild** (not just a file copy -- Supervisor bakes add-on code into the
image at build time) to take effect, and given this exact port wedged for
15+ minutes with no recovery but a power-cycle last time a malformed
request was sent, test carefully: a single attempt, not repeated rapid
retries.

**Third follow-up, same day**: confirmed the RTSP fix wasn't the (whole)
story. Tested raw TCP connectivity to the scanner's port 554 directly from
a shell on the HA host, bypassing the add-on entirely -- `Connection
refused`. Control (UDP 50536) still works fine at the same time (confirmed
via the add-on's own `/status` endpoint returning live data). So the RTSP
server is *currently* in exactly the wedged state this file already
documented -- the OPTIONS/DESCRIBE fix above is very likely still a real
correctness fix (worth keeping), but it can't matter until the port
actually recovers, which per the existing notes needs a power-cycle.

## MikroTik PoE power-cycle (2026-07-23)

The scanner is PoE-powered from a MikroTik switch, and RouterOS exposes a
one-shot `/interface ethernet poe power-cycle <interface>` CLI action
specifically for this -- a real network-triggerable equivalent of
unplugging the scanner, and a much better fit than the existing generic
`reboot_webhook_url` (which needs a separate smart plug/automation).

Confirmed against MikroTik's own REST API docs (not guessed, since this
runs against real production network hardware): CLI actions map to `POST
https://<router>/rest/<menu path>/<action>` with the action's named
arguments as a JSON body -- so `POST
.../rest/interface/ethernet/poe/power-cycle` with `{"interface":
"<name>"}`. Auth is HTTP Basic (console user credentials); RouterOS's
default REST cert is self-signed, so `verify_ssl` defaults to a config
option rather than being hardcoded either way.

Added `mikrotik.py` (`MikrotikPoeReset`) in the add-on, wired into
`audio_bridge.py`'s existing `trigger_reboot()` as a preferred alternative
to `reboot_webhook_url` (a scanner can have either, or neither -- poe_reset
wins if both are configured). New add-on config fields, flat rather than a
nested object (`poe_reset_host`/`_username`/`_password`/`_interface`/
`_verify_ssl`) to match this add-on's existing config.yaml style and avoid
depending on Supervisor's schema validator supporting nested optional
objects, which wasn't checked. All four of host/username/password/
interface are required together -- a partial config is treated as a typo
and ignored with a warning, not used half-broken.

Not yet tested against the real switch (needs the user's MikroTik
host/credentials/interface name entered into the add-on's Configuration
tab) -- given this is live, existing production network hardware (a switch
port might carry other traffic beyond just the scanner if misconfigured),
worth confirming the interface name against a read-only GET first, before
ever calling power-cycle for real.

**Config save initially failed** with Supervisor logging `Can't fetch HIBP
data: Timeout` -- the `password?` schema type triggers a Have I Been Pwned
breach-check lookup on save, which has no route out to HIBP's API on this
network and fails the whole save as an opaque "unknown error". Switched
`poe_reset_password` to plain `str?` (loses the masked/starred input
styling, but actually saves). Also: local add-on config.yaml schema
changes need the Add-on Store's own "Reload" action, not just
Rebuild/Restart on the add-on itself, before new fields show up in the
Configuration tab UI at all.

**Fields configured, power-cycle call made, scanner did not reboot.** Not
yet root-caused -- paused here at the user's request (manually
power-cycling for now instead). Worth checking next time: whether the
configured interface name actually matches the port the scanner is
physically plugged into (confirm via a read-only `GET
.../rest/interface/ethernet` first), whether the REST API call is even
reaching the switch/authenticating successfully (no server-side logging
added yet to distinguish "request succeeded but port didn't reset" from
"request never succeeded"), and whether RouterOS's `power-cycle` action
needs a different parameter name/shape than `{"interface": "<name>"}`
(guessed from the CLI syntax, not confirmed against a real REST response
from this switch).

## RTSP wedges on essentially any connection attempt (2026-07-23)

Direct evidence this session, bypassing the add-on entirely (raw Python
socket from the HA host): after a scanner power-cycle, port 554 accepted a
TCP connection but gave **zero response** to a single, correctly-formatted
`OPTIONS` request even after 15s -- then the very next connection attempt
got `Connection refused` again. One connect + one well-formed request was
enough to wedge it again. This is consistent with (and sharpens) the
existing note that the server "tolerates a single well-formed session but
not rapid reconnect attempts" -- it may not even take rapid/repeated
attempts; a single request during whatever this hardware's fragile state
is can be enough.

Root design problem this exposed: the add-on's old `AudioBridge` opened a
*fresh* RTSP session every time an HTTP client subscribed, and tore it
down every time the last one disconnected (see `audio_bridge.py`'s old
docstring). Since the card's own audio toggle button starts/stops
playback, and multiple debugging attempts (`curl` against the add-on's own
endpoint, not just direct scanner probes) each independently trigger this,
the add-on itself was very likely a significant source of the reconnect
churn that keeps wedging this scanner -- not just external testing.

**Fix**: `AudioBridge` now opens the RTSP session exactly once, at add-on
startup (`start()`, called unconditionally from `main.py`, not on first
subscriber), and keeps it running for the add-on's entire lifetime
regardless of subscriber count -- `subscribe()`/`unsubscribe()` now only
manage the HTTP fan-out set, they no longer start or stop the underlying
session. If the session does fail, `_supervise_forever()` reconnects after
a `RECONNECT_BACKOFF` (30s) delay rather than immediately, same reasoning
as the existing `REBOOT_COOLDOWN` after an auto-reboot. This means ffmpeg
and the RTSP/RTP session run continuously for the whole add-on lifetime
even with zero listeners (a deliberate tradeoff -- constant low CPU use in
exchange for not reconnecting on every play/stop click), and any reconnect
that does happen is at least spaced out rather than immediate.

Not yet re-tested against the real scanner (needs another reboot to clear
the current wedge, and given how easily this hardware wedges, testing this
change means confirming a **single** long-lived session actually stays up
over time -- not just that it connects once).

## Audio pipeline confirmed working end to end, then a browser-side blocker (2026-07-23)

After the persistent-session fix and a scanner reboot, the RTSP session
connected successfully (logged) and the add-on's own stream endpoint
finally produced real data -- confirmed two ways: (1) `curl`'d it directly
over SSH and got actual growing byte counts (previously always 200 OK with
zero body forever), and (2) sent the captured bytes to the user as a file,
who confirmed it's genuinely audible scanner audio, not silence/garbage.
So the whole RTSP -> RTP -> ffmpeg -> HTTP pipeline is confirmed correct.

But the card still produced no sound, or sound with heavy growing lag.
Root cause, found via direct browser console investigation: HA's own
frontend service worker (`sw-legacy.js`) intercepts the `<audio src=...>`
element's native resource load of this long-lived stream and either errors
outright (`A ServiceWorker intercepted the request and encountered an
unexpected error`, seen on Firefox, alongside `NS_BINDING_ABORTED` in the
Network tab) or introduces heavy buffering lag. This is not specific to
this integration -- it's a known, already-tracked class of bug in HA's own
frontend for long-lived streaming responses generally (HA's own camera
MJPEG streams hit the same thing in Firefox, per an open frontend issue
found via search). Confirmed via a live console test that a plain JS
`fetch()` to the *identical* signed URL streams cleanly with no such
problem (steadily growing byte count, no error) -- so the bug is
specifically in how the `<audio>` element's native loading behaves, not
the URL, auth, or server.

Also discovered live: Firefox's MediaSource Extensions implementation does
not support `audio/mpeg` (MP3) as a SourceBuffer type at all
(`MediaSource.isTypeSupported("audio/mpeg")` → `false`), only `audio/mp4`
with an AAC codec string (`audio/mp4; codecs="mp4a.40.2"` → `true`).

Fix, two parts:
- `audio_bridge.py`'s ffmpeg invocation now outputs fragmented MP4/AAC
  (`-c:a aac -f mp4 -movflags empty_moov+default_base_moof -frag_duration
  500000`) instead of plain MP3 -- `empty_moov` since there's no upfront
  duration/seek table for a live stream, `frag_duration` (not
  `frag_keyframe`) for fragment boundaries since audio has no video-style
  keyframes. `api.py`/`__init__.py`'s Content-Type headers updated to
  match (`audio/mp4`; the HA-side proxy now forwards the add-on's own
  Content-Type rather than hardcoding it a second time, so they can't drift
  out of sync).
- The card's `_toggleAudio()` now drives playback via `fetch()` + a
  `MediaSource`/`SourceBuffer`, appending chunks as they arrive from our
  own read loop, instead of ever setting `audio.src` to the stream URL
  directly -- sidesteps the service worker's native-load interception
  entirely, since the audio element is only ever given an in-memory
  `MediaSource` object URL, never the real network URL. Falls back to the
  old plain-`audio.src` behavior if `MediaSource`/this codec isn't
  available at all (a real fallback for other browsers/situations, not
  known-broken there).
- The stream URL path still literally ends in `.mp3` (`/audio/stream.mp3`)
  even though the content is now MP4/AAC -- harmless (the browser only
  cares about the `Content-Type` header, not the URL's file extension) but
  a known cosmetic mismatch, not yet renamed to avoid touching more files
  than necessary for what was already a multi-layer fix.

Not yet re-tested against the real install end-to-end with this exact
combination (add-on rebuild + integration + card all need redeploying
together for this one).

**Confirmed working end to end on the real install** after the rebuild +
restart above. Closing out this saga -- summary of everything that had to
be true simultaneously for audio to work at all: RTSP session persistent
rather than per-subscriber (stopped wedging the scanner on every play/stop
click), OPTIONS/DESCRIBE using the correct no-trailing-slash URL, the
stream proxied through HA core with a signed path (browser-reachable,
correct scheme, authenticated without a Bearer header), and fragmented
MP4/AAC driven via fetch()+MediaSource instead of a plain `<audio src>`
(Firefox's service worker + MP3-in-MSE support were both blockers). Any
one of these alone left it broken.

## Late subscribers never got the MP4 init segment (2026-07-23)

The "confirmed working end to end" above was real but incomplete: it was
tested by the first client to connect after an add-on rebuild/restart,
which happens to be the one case that actually works with the code as it
was then.

Root cause: `empty_moov` fragmented MP4 puts the `ftyp`+`moov` init segment
at the very start of ffmpeg's output, once, for the life of the process --
and MSE requires that init segment to be the *first* thing ever appended to
a `SourceBuffer`. But `_pump_stdout()` just fanned out whatever bytes ffmpeg
produced to whichever queues were in `_subscribers` *at that instant*, with
no memory of what it had already sent. Since the RTSP/ffmpeg session is
intentionally persistent from add-on startup (previous section) rather than
started per-subscriber, that one-time init segment goes out once, to
whoever's subscribed at that moment -- and every subsequent subscriber
(reload the page, open a second tab, hit `stream.mp3` directly, or just
reconnect after the first test session ended) gets a stream of bare
`moof`/`mdat` fragments with no init segment ever, so
`SourceBuffer.appendBuffer()` can't decode anything and playback silently
never starts. Matches the reported symptom exactly: works (if at all) only
by the accident of being the first-ever listener; everyone else gets a long
hang and then nothing, both via the card and via opening the stream URL
directly (the direct-URL path hits the same underlying add-on stream, it's
not a separate code path).

Fix, in `audio_bridge.py`: `_pump_stdout()` now buffers ffmpeg's output at
MP4-box granularity until it can identify the boundary between the
`ftyp`/`moov` init segment and the first `moof` box (`_split_init_segment()`
scans top-level box headers by size/type), caches that init segment on the
bridge, and `subscribe()` now seeds every new subscriber's queue with the
cached init segment before adding it to the live fan-out set -- so it's
always the first thing that queue ever yields, regardless of how long the
underlying ffmpeg process has already been running. The cache is cleared
whenever a new ffmpeg process starts (a new process means a new, different
moov) so it can never replay a stale/mismatched init segment after an RTSP
reconnect.

Not yet re-tested against the real install.

## Gate RTSP on the UDP control interface's reachability (2026-07-23)

Requested: don't open the RTSP audio session at all unless the scanner's
UDP control interface (port 50536, `protocol.py`'s `ScannerConnection`) is
known to be responding, and if control reachability is lost while an RTSP
session is running, stop that session immediately rather than leave it
running (or let it fail on its own, possibly much later) against a scanner
that's gone dark. The two interfaces fail independently on this hardware
(see the "Third follow-up" entry above -- RTSP wedged while control kept
working fine at the same time), so this doesn't prevent every possible
RTSP failure, but treats control going dark as a strong signal the whole
scanner is down/rebooting, not just that this one interface hiccuped, and
avoids repeatedly trying to open new RTSP sessions against a scanner that
can't even answer control commands.

Implementation:
- `ScannerConnection` (`protocol.py`) now tracks control reachability from
  its fast/reliable STS poll loop (not the flakier GSI one): three
  consecutive failed polls (`CONTROL_UNREACHABLE_THRESHOLD`) -- not a
  single one, since "an occasional drop" is normal per the existing STS
  calibration note -- flips it to unreachable; any successful poll flips it
  back. Exposed via `wait_until_reachable()`/`wait_until_unreachable()`
  (paired `asyncio.Event`s) and `is_reachable()`. Starts unreachable, since
  nothing has responded yet at add-on startup.
- `AudioBridge` (`audio_bridge.py`) now takes the scanner's `ScannerConnection`
  as `control` (wired up in `main.py`, same scanner). `_supervise_forever()`
  waits on `control.wait_until_reachable()` before opening each new RTSP
  session, and races the running session against
  `control.wait_until_unreachable()` -- if control reachability is lost
  first, the session is cancelled immediately and the loop waits for
  control to come back before trying again, without counting it toward
  `_consecutive_failures`/the auto-reboot threshold (that's for RTSP-side
  failures specifically, not "the scanner went away").

**Real-install test (2026-07-23), first thing found**: control came up fine
(`control interface responding again` logged right after startup, matching
the new gating's intent), but no `audio session started` log ever
followed, with total silence -- not even an error. Root cause: neither the
initial `asyncio.open_connection` nor any individual RTSP request/response
round trip (`OPTIONS`/`DESCRIBE`/`SETUP`/`PLAY`/`GET_PARAMETER`) had a
timeout. On this hardware's documented wedged state (see the "RTSP wedges"
entries above -- TCP connects fine, then zero response to anything), that
left `_run_session()` hanging forever on a bare `await reader.readline()`
inside `_read_rtsp_response()`, with no exception raised and therefore
none of the existing retry/backoff/auto-reboot machinery ever engaging --
completely invisible, and indistinguishable from "audio just hasn't
started yet."

Fixed: `RTSP_CONNECT_TIMEOUT` (5s) around the initial connect and
`RTSP_REQUEST_TIMEOUT` (5s) around each RTSP request/response now raise a
descriptive `ConnectionError` (naming the method that timed out) instead of
hanging silently -- verified with a standalone test server that accepts
the TCP connection and then never replies to anything, confirming
`_run_session()` now raises within the timeout instead of hanging past a
5s outer guard. This routes a wedged RTSP server through the normal
failure path: logged by `_supervise_forever`'s existing
`logger.exception(...)`, counted toward `_consecutive_failures`, and
(if configured) eventually triggers `auto_reboot`.

Also tightened `ScannerConnection._mark_poll_failure()`: the original
version only logged when reachability *dropped* from a previously-working
state, so a scanner that's never been reachable since add-on startup (a
real possibility, not just a hypothetical) logged nothing at all once past
the failure threshold -- now it logs once for that case too, so "audio is
permanently waiting on control" is never silent either.

Not yet re-tested against the real install with this exact combination.

## fetch()+MediaSource hits the same service-worker bug it was meant to dodge (2026-07-23)

After the RTSP timeout fix above, the real install confirmed audio now
plays when browsing directly to the signed `stream.mp3` URL -- the
add-on/proxy pipeline itself is solid. But the card's Play/Stop button
still produced nothing. Browser console:

```
Failed to load '.../api/sds200/scanner0/audio/stream.mp3?authSig=...'.
A ServiceWorker intercepted the request and encountered an unexpected error.
sds200-card: audio streaming failed TypeError: Error in input stream
```

This is the *exact* service-worker bug documented above ("Audio pipeline
confirmed working end to end, then a browser-side blocker") that the
switch to `fetch()`+`MediaSource` was supposed to avoid -- except this
time it's `fetch()` itself hitting it, not `<audio src>`. The original
"confirmed live" test of a plain `fetch()` streaming cleanly almost
certainly wasn't a real fix, just a false negative: DevTools' "bypass for
network" / "update on reload" service-worker options are commonly left on
during a debugging session, which would silently exempt every fetch() run
from the console during that session from the exact bug being tested for.
In normal day-to-day browsing (service worker actually active), `fetch()`
turns out to be just as broken as `<audio src>` always was -- both are
subresource fetches from a page the service worker controls.

The one thing confirmed working, twice now (the original direct-URL test
and this one): a genuine top-level browser navigation to the identical
signed URL. That's a different request type (`mode: "navigate"`) than
either `fetch()` or `<audio src>`'s subresource loads, and it's what the
service worker apparently doesn't mangle.

**Fix**: `sds200-card.js`'s `_toggleAudio()` now embeds the stream via
`<iframe src=streamUrl>` instead of fetch()+MediaSource -- an iframe's own
document load is that same "navigate" request type, so it should sidestep
the bug the same way a full tab navigation does. Trade-off: the card now
shows the browser's own native audio player controls inside the iframe
(play/pause, a seek bar that doesn't mean much for a live stream) instead
of the card's custom Play/Stop button styling. The `<audio>` element,
`MediaSource`, and fetch()-based reader loop are gone entirely from the
card; `_toggleAudio()` just creates/removes the iframe.

Real-install follow-up: the iframe approach worked, and separately the
native controls it showed were hidden via CSS (zero-size box, not
`display:none`/`visibility:hidden`, to avoid the browser treating the
iframe as non-visible and risking the audio inside it being paused) so
the card's own Play/Stop button is the only visible control again.

## Audio stopping after ~30s: keepalive timeout too strict (2026-07-23)

Real-install finding: audio would play, then stop after almost exactly 30
seconds. Root cause: the RTSP timeout fix earlier in this file
(`RTSP_REQUEST_TIMEOUT`, 5s) applies to every `rtsp()` call uniformly,
including the periodic `GET_PARAMETER` keepalive `keepalive()` sends every
`KEEPALIVE_INTERVAL` (20s) purely to stay under the RTSP Session's 60s
inactivity timeout. On the real scanner, `GET_PARAMETER` can apparently
take longer to answer while already mid-stream than the initial
OPTIONS/DESCRIBE/SETUP/PLAY handshake does -- so the first keepalive (at
~20s) would hit the 5s timeout, raise, and tear down an otherwise-healthy
session; `_supervise_forever()` then waits out the full `RECONNECT_BACKOFF`
(30s) before trying again. 20s (first keepalive) + up to 5s (timeout) lines
up with the reported ~30s cutoff almost exactly.

Fix: `rtsp()` now takes a `timeout` parameter (default
`RTSP_REQUEST_TIMEOUT`), and the keepalive call passes a separate, much
more generous `RTSP_KEEPALIVE_TIMEOUT` (30s) instead -- there's ~40s of
slack before the 60s Session timeout actually matters, so there's no need
for the keepalive to be anywhere near as strict as the initial handshake
(where a quick failure to detect a genuinely wedged scanner is worth
keeping).

Not yet re-tested against the real install.

## Removed the reboot_webhook_url option (2026-07-24)

Requested: drop the webhook-based reboot mechanism, keeping only
`poe_reset_*`. Removed `reboot_webhook_url` from `AudioBridge` (the
`aiohttp.ClientSession().post(...)` fallback branch in `trigger_reboot()`,
and the `aiohttp` import that only that branch needed), `config.yaml`'s
schema, and `main.py`'s wiring. `has_reboot_mechanism()`/`trigger_reboot()`
now only know about `poe_reset`. Updated comments/error text/service
descriptions across `api.py`, `custom_components/sds200/__init__.py`,
`api_client.py`, `services.yaml`, `sds200-card.js`, and `README.md` that
referenced the webhook option, plus `tests/test_integration_api_client.py`'s
`FakeAudioBridge` (renamed its stand-in field from `reboot_webhook_url` to
`poe_reset` and added `has_reboot_mechanism()`, matching the real class's
interface rather than a stale one).

## Reboot cooldown shortened to 60s (2026-07-24)

Requested: confirm the RTSP-failure -> MikroTik PoE power-cycle path gives
the router/scanner ample time to come back before retrying, specifically
60 seconds. This mechanism already existed (`poe_reset_*` +
`auto_reboot_on_audio_failure`, `AUTO_REBOOT_FAILURE_THRESHOLD = 3`
consecutive RTSP failures before triggering) -- kept the 3-failure
threshold as-is (confirmed: avoids power-cycling on a single transient
hiccup), just shortened `REBOOT_COOLDOWN` from 90s to 60s.

## PoE reset needed an http/https option (2026-07-24)

Real-install finding, resolving the "Fields configured, power-cycle call
made, scanner did not reboot" mystery from the MikroTik PoE section above:
`mikrotik.py` hardcoded `https://` for the REST API call, but RouterOS's
REST API is served by the "www" (plain HTTP) and "www-ssl" (HTTPS) services
independently -- a router with only "www" enabled has no "www-ssl" to
answer an https:// request at all, so the call was just silently failing
to connect, indistinguishable from a wrong password/interface with the
logging that existed at the time.

Added `poe_reset_use_ssl` (`config.yaml`, defaults `true`/https, matching
prior behavior) alongside the existing `poe_reset_verify_ssl` --
`MikrotikPoeReset.power_cycle()` now builds the URL with `http://` when
`use_ssl=False`, and `verify_ssl` is simply ignored (harmless) in that case
since there's no TLS involved either way. Not yet re-tested against the
real router.

## Reboot still not power-cycling: timeout mismatch + concurrent calls (2026-07-24)

Real-install log from a manual "Restart Scanner" click, with the RTSP
session simultaneously failing on its own (`ConnectionRefusedError` on
554), showed two real bugs:

1. `custom_components/sds200/api_client.py`'s `reboot()` shared `_post()`'s
   hardcoded 5s client timeout with every other control call (key/volume/
   squelch/etc). But `mikrotik.py`'s own REST call to the router allows up
   to 15s. So the reboot request from HA always had a real chance of timing
   out client-side before the add-on's own attempt could possibly finish
   either way, regardless of whether the power-cycle would have succeeded.
   Fixed: `_post()` now takes an optional per-call `timeout` (default
   unchanged at 5s for the fast control commands), and `reboot()` passes
   20s -- comfortably past the add-on's 15s allowance.

2. Because the RTSP session was *also* failing at the same moment, the
   auto-reboot path (`_supervise_forever()`, 3 consecutive failures) fired
   its own `trigger_reboot()` about 2 seconds after the manual one -- two
   overlapping power-cycle POSTs hit the same router port concurrently,
   with nothing to prevent that race (a real, not hypothetical, sequence:
   RTSP failing is often *why* someone reaches for the manual reboot
   button). Fixed: `AudioBridge` now serializes `trigger_reboot()` calls
   through an `asyncio.Lock` (`_reboot_lock`) -- verified with a standalone
   test that two concurrent `trigger_reboot()` calls never overlap at the
   `power_cycle()` call itself.

Also flagged for the user to double check: the logged interface value was
`'ether12 - scan0'` -- worth confirming that's genuinely the exact RouterOS
interface identifier (via a read-only `GET /rest/interface/ethernet`) and
not a descriptive label accidentally used as the config value, which would
make every call fail regardless of the two fixes above.

Not yet re-tested against the real install.

## Root cause found: wrong REST body parameter name (2026-07-24)

Direct `curl` testing against the real router (bypassing the add-on
entirely) nailed it: RouterOS's REST API rejects `{"interface": "ether12"}`
outright --

```
{"detail":"unknown parameter interface","error":400,"message":"Bad Request"}
```

-- and `{"numbers": "ether12"}` works. `numbers` (not `interface`) is the
correct body key for identifying the target port on this action, and it
accepts the interface name directly (no need to resolve it to a numeric
index first). This was never going to work regardless of the http/https
fix, the timeout fix, or the concurrent-call fix from the sections above --
every single request was outright rejected by RouterOS from the start.

Also incidentally confirmed along the way: the *earlier* attempts (before
this was isolated with direct `curl`) were additionally masked by
`fw1.example` intermittently round-tripping through a
*different* nginx reverse-proxy/gateway in front of the router (a plain
"504 Gateway Time-out" nginx error page came back at one point, nothing to
do with this add-on's own code or the HA SSL proxy add-on) -- worth keeping
in mind if connectivity to the router flakes again, as a separate class of
failure from the REST API parameter bug.

Fixed: `mikrotik.py`'s `power_cycle()` now sends `{"numbers": self.interface}`
instead of `{"interface": self.interface}`. `REBOOT_COOLDOWN` reverted to
90s (from the 60s change earlier in this file) per follow-up request.
Confirmed working against the real router via direct `curl` with the
`numbers` parameter; not yet re-tested end-to-end through the add-on itself
with this fix.

## Still 504ing after the parameter fix: add-on container's source IP (2026-07-24)

With the `numbers` parameter fix in place, reboot attempts through the
add-on still came back with the exact same nginx 504 page (`<center>
nginx</center>`, ~169 bytes) -- and switching `poe_reset_host` from the
hostname to the router's direct IP made no difference either, ruling out
DNS. But the identical `curl` request run **from the HA host itself**
(not through the add-on) worked fine and reached the real router. Since
this same add-on container already reaches the scanner (a completely
different host, different ports) without issue, "the add-on can't reach
the LAN at all" doesn't fit either -- the discriminating factor is
specifically the *source* the request originates from: the HA host's own
IP works, Supervisor's isolated Docker bridge network's NAT'd source IP
(what the add-on was using) doesn't. Consistent with a firewall/gateway
ACL on the network (host is literally named `fw1.example`) that only allows
certain source subnets through to the router's REST API, silently
redirecting anything else through a proxy/gateway page instead.

Fix: added `host_network: true` to `config.yaml`. This puts the add-on's
container directly on the HA host's own network stack, so its outbound
connections (this MikroTik call, and the scanner RTSP/control connections)
originate from the host's own IP -- the same path already confirmed
working. Since every port in the existing `ports:` mapping was already
published 1:1 to the same host port, this doesn't change inbound
reachability at all (Supervisor ignores `ports` entirely once
`host_network` is set; left in place for documentation). Version bumped
to 0.1.9. Not yet re-tested against the real install.

## Reverted host_network: broke the add-on's own connectivity (2026-07-24)

Real-install finding: `host_network: true` broke HA core's connection to
the add-on entirely (not just the reboot feature) --

```
Cannot connect to host aa65dcfd-sds200-bridge:8000 ssl:default
[Connect call failed ('172.30.33.5', 8000)]
```

-- because the add-on no longer has its own address on Supervisor's
internal `172.30.33.x` bridge network once it's on the host's network
stack directly; the Supervisor-assigned hostname HA core was using to
reach it stopped resolving to anything reachable. Compounding this: since
`ports:` is ignored under `host_network`, changing it to `8001` (an
attempt to work around what was presumably a port conflict on the shared
host network stack) had no effect either -- the code still hardcodes port
8000 (`main.py`'s `HTTP_PORT`).

Given `host_network` introduced two new problems (hostname resolution,
port conflicts) without a clean way to reconfigure around them (the
integration's `config_flow.py` has no reconfigure step -- changing
`CONF_HOST`/`CONF_PORT` means deleting and re-adding the whole integration
entry), reverted it. The correct fix for the original 504-through-a-
different-proxy problem is on the network side: the firewall/gateway
(`fw1.example`) needs an ACL change to allow Supervisor's Docker bridge
subnet through to the router's REST API the same way the HA host's own IP
is already allowed -- not something fixable from this repo. Version
bumped to 0.1.10.

## Uncaught asyncio.TimeoutError crashing the reboot handler (2026-07-24)

Real-install log nailed a genuine, independent bug on top of the
firewall/504 saga above. Timing: `power-cycling PoE port...` logged, then
~15 seconds later (matching `mikrotik.py`'s own `ClientTimeout(total=15)`
exactly) an `[aiohttp.server] Error handling request from 172.30.32.1` --
aiohttp's own **server-level** unhandled-exception log, not this project's
`audio_bridge` logger. That means an exception was escaping our own code
entirely, not being raised/caught as `MikrotikApiError` like every other
failure path here.

Root cause: aiohttp's `ClientTimeout(total=...)` expiring raises a plain
`asyncio.TimeoutError` -- **not** an `aiohttp.ClientError` subclass. This is
a well-known aiohttp gotcha (`except aiohttp.ClientError` alone does not
catch it). `power_cycle()`'s `except aiohttp.ClientError as exc:` never
caught it, so a genuine 15s timeout crashed straight through
`trigger_reboot()` into the request handler instead of being converted to
`MikrotikApiError` and returning `{"ok": false}` cleanly -- very likely
what was surfacing as the garbled "504, message='Gateway Timeout'" seen on
the HA/card side.

Fixed: `except (aiohttp.ClientError, asyncio.TimeoutError) as exc:`.
Verified with a standalone test that simulates aiohttp's real timeout
behavior (a bare `asyncio.TimeoutError` from the `session.post()` context
manager) and confirms it's now converted to `MikrotikApiError` instead of
escaping uncaught. Independent of the firewall ACL issue -- this fix means
a genuine timeout (whether from the firewall problem or any other cause)
now fails gracefully instead of crashing the handler, but the underlying
firewall ACL still needs fixing for the power-cycle to actually succeed.
Version bumped to 0.1.11.

## host_network reverted again; real clue found (2026-07-25)

Tried `host_network: true` again (previous section) with a configurable
`http_port` to dodge the port-8000 conflict it exposed. Still didn't fix
the actual power-cycle call -- even with a raw IP (no DNS involved) and a
confirmed full rebuild, the MikroTik REST call still hung on the bare TCP
`sock_connect()` for the full 15s and never got further (full traceback
confirmed this -- `aiohappyeyeballs`/`asyncio` never got a SYN-ACK back at
all, not an HTTP-level error).

The real clue, from the user: **the add-on can already reach the scanner
fine, and the scanner is on the same subnet as the router.** That rules out
a general egress/routing/Docker-networking problem entirely -- traffic to
that subnet clearly works. The failure is specific to the router *itself*,
not the path to its subnet. Most likely explanation: RouterOS routers
commonly apply much stricter rules to traffic destined for the router's
*own* management services (`/ip service`'s "address" restriction, or an
`input`-chain firewall rule) than to traffic simply being *routed through*
it to other LAN devices (the scanner) -- a `forward`-chain vs `input`-chain
distinction. That would explain every symptom seen: the scanner (forwarded
traffic) always worked, the router's own REST API (input-chain-guarded)
never did, from any source that isn't already allowed.

This means `host_network` was solving a problem that didn't exist (a
Docker-networking/NAT issue), while the actual blocker (the router's own
service/firewall configuration) was never something fixable from this
add-on's side at all. Reverted `host_network`, `http_port`, and the
`ports:`/`ports_description:` removal entirely -- back to normal bridge
networking (confirmed: `config.yaml`/`main.py` match their pre-host_network
state). Version bumped to 0.1.14.

Next step, on the user's own router: check `/ip service print` (the
`www`/`www-ssl` entry's "address" field) and `/ip firewall filter print`
(`input` chain, not `forward`) for a rule that only allows a narrower
source range than wherever the add-on's traffic actually originates from.

Also flagged for the user to check, independent of all of the above: since
every reboot attempt has *timed out* rather than gotten a clean
success/failure response, it's possible some of them still reached the
router and actually triggered a real power-cycle on `ether12` before the
response was lost -- worth confirming the scanner and that port are
actually in a sane state after this whole debugging session, not assuming
no side effects occurred just because the add-on reported failures.

## host_network reverted for good (2026-07-25)

`host_network` was briefly re-requested and re-applied (0.1.15/0.1.16)
after the revert above, then reverted again -- for good this time,
confirmed by the user: it never actually fixed the reboot/power-cycle
issue at any point it was tested (raw IP, full rebuild, everything),
which lines up exactly with the router-input-chain theory from the
previous section -- host_network addresses a Docker-networking/NAT
concern that was never the actual problem, so it wasn't providing any
real benefit against the risk/complexity it added (integration
reconfiguration, port conflicts, Supervisor UI quirks). Back to normal
bridge networking with `ports:`, matching 0.1.14's state exactly. Version
bumped to 0.1.17. The router's own `/ip service`/`/ip firewall filter`
`input` chain is still the one open, unexplored lead for the actual fix.

## ROOT CAUSE: the add-on was dialing https:// at a router with no www-ssl (2026-07-25)

Found by probing the router directly from a LAN host (192.0.2.248) instead
of theorizing about the add-on's network position. The router
(`fw1.example` = 192.0.2.252):

| target | result |
| --- | --- |
| `http://192.0.2.252/rest/system/resource` (port 80) | RouterOS's own `{"error":401,"message":"Unauthorized"}`, **connects in 6ms** |
| `https://192.0.2.252/...` (port 443) | SYN **silently dropped** -- connect hangs for the full timeout, no SYN-ACK |

That is exactly, character-for-character, the symptom recorded in the
host_network section above ("hung on the bare TCP `sock_connect()` for the
full 15s and never got a SYN-ACK back at all"). `www` is enabled on the
router, `www-ssl` is not -- and `poe_reset_use_ssl` **defaults to `true`**,
so unless it was explicitly set to `false` in the add-on's Configuration
tab, every power-cycle call was aimed at a black-holed port 443.

This retires two theories at once, both wrong:

- **The router input-chain/`/ip service` ACL theory** (previous section):
  no ACL is blocking anything. A plain LAN host gets RouterOS's own 401
  immediately, with no proxy and no source filtering.
- **The Docker-NAT/source-IP theory** (the whole `host_network` saga):
  also never the problem -- which is precisely why four rounds of
  `host_network` changes never moved the needle. Standard Docker bridge
  networking masquerades outbound traffic to the host's own IP anyway, so
  the add-on's requests already originated from the same source address as
  the HA host's working `curl`. The one thing that differed between the
  working host `curl` and the failing add-on call was the *scheme*: the
  manual `curl` tests used `http://`, the add-on used `https://`.

The nginx 504 pages seen earlier were a genuinely separate, intermittent
issue (already noted above as a different class of failure) and sent the
investigation toward network topology, away from the config.

**The fix is a config change, not a code change**: set
`poe_reset_use_ssl: false` for the scanner. Note the older gotcha from the
MikroTik section above -- a new `config.yaml` field needs the Add-on
Store's own "Reload" action (not just Rebuild/Restart) before it shows up
in the Configuration tab at all, so it's worth confirming the option is
actually present and set rather than assuming it applied.

Code changes made so this can never again be a featureless hang (0.1.18):

- `MikrotikPoeReset.url` property; `audio_bridge.trigger_reboot()` and
  `manager._build_poe_reset()` both log it. The scheme in use is now visible
  in the add-on log at startup, without needing to trigger a reboot --
  previously only the interface name was logged, leaving http-vs-https
  (the single most likely misconfiguration) completely invisible.
- `ClientTimeout(total=15, sock_connect=5)` -- a black-holed SYN now fails
  in 5s instead of burning the full 15s budget.
- The `MikrotikApiError` message now includes the full URL, plus an
  explicit hint when `use_ssl` is true telling the user to check
  `www-ssl`/set `poe_reset_use_ssl: false`.

Verified end-to-end against the real router with `MikrotikPoeReset`
itself (deliberately wrong credentials, so no live port was cycled):
https fails in 5.0s carrying the hint, http reaches RouterOS in 0.1s and
returns a clean `401` -- i.e. the request lands correctly and only the
credentials stood in the way. Existing test suite: 9/9 pass.

## A failed power-cycle was reported to the user as success (2026-07-25)

Found while chasing a follow-up `[Errno 104] Connection reset by peer`
from `sds200.reboot`. Reproducing the add-on's reboot endpoint locally
(real `api.create_app` + real `AudioBridge` + real `MikrotikPoeReset`
pointed at the live router) showed the handler does **not** crash on a
router-side failure -- it returned a clean `HTTP 200 {"ok": false}`.

Which exposed the actual bug: `post_reboot()` returned 200 regardless of
outcome, `trigger_reboot()` swallowed `MikrotikApiError` into a `False`,
and `__init__.py`'s `handle_reboot` only raises `ServiceValidationError`
when the *request* fails. So a power-cycle that never happened looked to
HA like one that succeeded -- the button silently did nothing -- and
`mikrotik.py`'s diagnostic message (the one naming the URL and the
www-ssl misconfiguration) was discarded rather than shown. This had been
masking every poe_reset failure since the feature was added.

Fixed, end to end:

- `AudioBridge.trigger_reboot()` now propagates `MikrotikApiError` instead
  of returning `False`. The auto-reboot path in `_supervise_forever()`
  catches it explicitly -- that one *must* stay non-fatal, since letting it
  escape would kill audio supervision for the scanner over a failed
  recovery attempt.
- `api.post_reboot()` converts it to a **502 carrying the message**.
- `api_client._post()` reads the response body on `status >= 400` instead
  of `raise_for_status()`, which discarded it and left the user with a bare
  `502, message='Bad Gateway'`.

Verified against the live router through the full chain (add-on app +
integration client, wrong credentials so no port was cycled): HA now shows
`reboot failed for scanner0: could not reach MikroTik REST API at
https://... -- ... set poe_reset_use_ssl: false`. Regression test added
(`test_failed_power_cycle_surfaces_the_routers_error_message`); suite
10/10. Version 0.1.19.

**Still open: the `[Errno 104] Connection reset by peer` itself.** That
error comes from `api_client.py`'s `_post` -- it is HA core <-> add-on, not
add-on <-> router (a router failure now yields a 502 with a message, and
before this fix yielded a 200). Errno 104 means the TCP connection was
established and then reset, i.e. the add-on's HTTP server accepted the
request and then died or dropped it -- a crashed/restarting add-on
container, not a networking or MikroTik problem. Needs the add-on's own
log from the moment the button was pressed to go further.

## The Errno 104: a stale config entry pointed at a port the add-on left (2026-07-25)

The add-on was never the problem. Probing from a LAN host:

- `http://192.0.2.235:8001/scanners` -> **`200 OK`** in milliseconds,
  `[{"id": "scanner0", "name": "scanner0.example", "host": "192.0.2.232"}]`.
  The add-on is healthy.
- `http://192.0.2.235:8000/...` -> TCP **accepts**, then sends **zero bytes**
  and never responds -- to an HTTP request or a TLS handshake alike. Some
  unrelated service had already taken 8000 on the host, which is why the
  add-on's published port had been moved to 8001 in the first place.

A connection that is accepted and then reset mid-request is exactly
`[Errno 104] Connection reset by peer`, and it is raised in
`api_client.py`'s `_post` -- the HA-core-to-add-on hop. So HA had not been
reaching the add-on at all, which is why none of the MikroTik-side fixes
(0.1.18, 0.1.19) changed the error message by a single character.

`__init__.py:33` does build the client from `entry.data[CONF_PORT]`, so
the integration honours whatever port the config entry holds -- the stale
value was in the entry itself. And `config_flow.py` had **only**
`async_step_user`: no reconfigure step, so there was no way to change
host/port in the UI at all. The only route was deleting and re-adding the
integration, discarding every entity id and its history. That is what
turned an ordinary event (a port conflict moving the add-on's published
port) into a permanently misconfigured entry whose symptom looked like an
add-on crash or a network fault.

Note the two ports are genuinely different things, which is what makes
this easy to get wrong:

- The add-on's **internal** port is hardcoded (`main.py:22`,
  `HTTP_PORT = 8000`). Supervisor's Network tab does not change it; it
  only remaps the host side.
- So: connecting by the add-on's **Supervisor hostname** (the intended
  path, over the internal `hassio` network) -> use **8000**. Connecting by
  the **HA host's LAN IP** -> use the **remapped external** port (here
  **8001**).

Added `async_step_reconfigure` to `config_flow.py` (sharing the existing
reachability check and schema with `async_step_user`, so a wrong value is
still rejected with `cannot_connect` rather than silently saved). It moves
the unique id with the host/port, since that id is derived from them and
would otherwise go stale and wrongly reject a later re-add of the same
address. Strings added to `strings.json` and `translations/en.json`.

Also fixed while there: the `reboot` service description still advertised
"via its configured reboot webhook", a mechanism removed back on
2026-07-24.

Untested here: `config_flow.py` has no test coverage and the `homeassistant`
package isn't available in this environment, so the reconfigure step is
verified only by compile-check and by review against the current config-flow
API. `_get_reconfigure_entry()`/`_abort_if_unique_id_mismatch()`/
`async_update_reload_and_abort(data_updates=...)` all require HA 2024.11 or
newer. Suite still 10/10.

## Audio silent in the HA Android app; iframe playback was desktop-only (2026-07-25)

Real-install report: audio plays fine in desktop Firefox but never in the
Home Assistant **Android app** -- Play does nothing, no sound, no error
visible in the UI. Everything server-side is shared between the two (same
add-on, same `SDS200AudioProxyView`, same signed URL), so the difference is
entirely in how the card gets the bytes to a decoder.

Root cause: the `<iframe src=streamUrl>` mechanism from the "fetch()+
MediaSource hits the same service-worker bug" entry above depends on the
browser rendering a **media document** for a navigation to an `audio/mp4`
URL -- the built-in player desktop Firefox/Chrome show when you browse
straight to an audio file. That's what was actually playing the stream; the
card never had a media element of its own. The HA Android app renders the
frontend in an **Android WebView**, which doesn't do that: a WebView
navigation to a non-HTML content type goes to the app's download handler or
nowhere at all. A zero-size iframe pointed at the stream is therefore
silently a complete no-op there -- exactly the reported symptom. (Autoplay
policy in a subframe would be a second obstacle even if it did render.)

Note also that the service-worker breakage the iframe was working around is
**Firefox-specific**: both observed error strings ("A ServiceWorker
intercepted the request and encountered an unexpected error", "TypeError:
Error in input stream") are Firefox's, from its handling of a never-ending
response body passing through a service worker. There was never evidence
that `fetch()` is broken in Chromium here.

**Fix** (`sds200-card.js`): `_toggleAudio()` now tries two paths in order,
so each covers the other's known breakage:

1. `_playViaMediaSource()` -- an in-page `<audio>` element fed by
   `fetch()` + `MediaSource`/`SourceBuffer`, needing neither a media
   document nor a navigate-type request. This is the path Android uses.
2. `_startAudio()`'s `<iframe>` fallback -- unchanged from before, used
   only if the MediaSource path doesn't reach a playing state. Firefox's
   `fetch()` rejects almost immediately under the service-worker bug, so
   Firefox lands here in a moment rather than waiting out
   `AUDIO_START_TIMEOUT_MS` (6s), and behaves exactly as it did before.

Details that matter for the MediaSource path specifically, none of which
the iframe needed:

- **Explicit `audio.play()`** after the first appended fragment, not just
  the `autoplay` attribute: Chrome/WebView autoplay policy needs the user
  activation from the Play click, which `_toggleAudio()` runs on.
- **Buffer eviction** (`AUDIO_BUFFER_KEEP_S`, 30s): this stream runs for
  hours, and an append-only `SourceBuffer` eventually hits
  `QuotaExceededError` -- far sooner on mobile, whose buffer budget is much
  smaller than desktop's. `_trimAndReseek()` removes played-out audio each
  append, with a one-shot aggressive remove+retry if a quota error happens
  anyway.
- **Skip-forward when behind live** (`AUDIO_MAX_LAG_S`, 10s): a mobile
  browser suspends the page when backgrounded/screen-off while bytes keep
  arriving, so playback resumes minutes behind. `_trimAndReseek()` jumps
  back to the buffer's leading edge instead of playing a growing backlog.
- **Stop on `disconnectedCallback`**: playback used to stop by itself when
  the card was removed, purely because it lived inside the iframe that went
  with it. An `<audio>` element plus a read loop still referenced by
  `this._audioSession` does not -- without this, switching dashboard views
  leaves the scanner playing with no button left to stop it.
- The `<audio>` element is appended to the same zero-size container as the
  fallback iframe (not left detached, which makes a playing media element
  GC-eligible).

Not yet re-tested against the real install -- specifically unconfirmed:
that the MediaSource path is what actually plays on Android, and that
Firefox still falls back cleanly rather than sitting through the 6s
timeout.

## The Android silence was a stale cached card; the ~30s stop is back (2026-07-25)

Follow-up to the entry above. The MediaSource path does work in the Home
Assistant Android app -- but only after force-closing and reopening the
app. Before that, the app kept playing the *previous* `sds200-card.js`, so
the fix appeared to change nothing. `add_extra_js_url()` was pointed at a
bare, unversioned `/sds200_static/sds200-card.js`, and registering the
static path with `cache_headers=False` only suppresses explicit
far-future caching -- with no validator either way, a client may
heuristically cache, and that WebView does. `__init__.py` now appends
`?v=<manifest version>`, so bumping `manifest.json` (which is required
on every `custom_components/` change anyway) changes the URL and forces a
refetch. It falls back to a timestamp rather than a constant if the version
can't be read: degrade to "refetch every restart", never to "never
refetch".

Then, with both clients finally running the same code, **audio stops after
~30 seconds in Firefox and Android alike** -- the same symptom as the
keepalive entry above, which has now been chased twice. Two candidate
causes, in different halves of the system, that the evidence to date can't
distinguish:

1. Server-side: the RTSP session (or the RTP flow) dies ~30s in and the
   stream simply stops producing bytes.
2. Client-side: something in the new MediaSource path breaks at that point
   -- and Firefox is no longer necessarily on the iframe path, since the
   service-worker bug that forced it there may not reproduce, so a
   MediaSource-side bug could now hit both clients.

Rather than guess, this pass makes the next test conclusive:

- **The card reports what happened, on the card.** A status line under the
  Play button (`#audio-status`, `_setAudioStatus()`) shows the elapsed time
  and the reason playback ended. The distinction that matters: `waiting`
  ("buffer empty, waiting for data") or "stream closed by the server" means
  the *bytes stopped arriving*, while "decode/media error" means bytes
  arrived and the decoder rejected them. `console.warn()` was useless for
  this -- the Android app has no reachable JS console, and that's the
  platform whose behaviour needed explaining.
- **`AUDIO_BUFFER_KEEP_S` moved from 30s to 120s.** The first
  `SourceBuffer.remove()` firing at exactly the reported failure time made
  the eviction logic impossible to rule out. At 120s it's nowhere near the
  symptom.
- **The add-on no longer drops bytes silently.** `_pump_stdout()`'s fan-out
  was `if not queue.full(): queue.put_nowait(chunk)` -- a subscriber that
  fell behind quietly lost bytes out of the middle of its stream. For
  fragmented MP4 that's fatal, not lossy: a hole desynchronizes every
  following box header, so the client stops decoding for good, and nothing
  was logged. `_fan_out()` now closes that subscriber's response instead
  (`STREAM_CLOSED` sentinel, honoured by `api.py`'s `stream_audio`) and
  logs it as a warning. Worth noting for cause (1): the queue is 64 chunks
  of 4 KiB, which at this stream's bitrate is roughly half a minute of
  audio -- so "consumer stalls, queue fills, bytes get dropped" was itself
  a mechanism that would have produced a ~30s cutoff, silently.
- **Session end closes subscribers.** They used to stay connected across an
  RTSP reconnect and be handed the next ffmpeg process's fragments on the
  same response -- a different `moov` than the init segment they started
  with, i.e. unplayable, after sitting silent for the reconnect backoff.
- Subscriber add/remove is logged with a count, so the add-on log shows
  whether a client was still attached when the audio stopped.

What the next test needs to record: what the card's status line says (and
at what elapsed time), and whether the add-on log shows `audio session
ended` / `an audio subscriber fell behind` / `closing N audio subscriber(s)`
at that same moment. Those two together identify which half is at fault.

### First hard evidence on the ~30s stop (2026-07-25)

Add-on log for one playback attempt:

```
13:48:15,846 INFO [audio_bridge] scanner0: audio subscriber added (1 now)
13:48:45,956 INFO [audio_bridge] scanner0: audio subscriber removed (0 left)
```

30.1 seconds, and -- importantly -- **nothing else**. No `audio session
ended`, no `an audio subscriber fell behind`, no `closing N audio
subscriber(s)`. That rules out most of the server side:

- The RTSP session did **not** die at 30s, so this is *not* another
  instance of the keepalive bug (the previous ~30s symptom, above).
- The bridge did not close the subscriber: neither the fell-behind path
  nor session-end. So the fan-out was keeping up and the stream was still
  producing bytes.
- Therefore the HTTP request itself went away from the *client* end of the
  add-on's socket -- i.e. HA core's proxy request to the add-on was
  dropped, propagated back from the browser.

What that still can't distinguish: whether the *browser* hung up on its
own (something in the browser/service-worker/proxy path killing a
long-lived `fetch()` at 30s), or whether the card's own error handling hit
a media error and called `_stopAudio()`, which aborts the fetch -- both
end with the add-on logging exactly the line above. The card's status line
answers that, but the first test ran against a card that hadn't reloaded,
so there was nothing to read.

Correlation worth keeping in view: the iframe/native-player path (a
top-level navigation) played for long periods in Firefox, and the ~30s
stop appeared when both clients moved to `fetch()`+MediaSource. A
service-worker-mediated `fetch()` is exactly the thing that differs.

`SDS200AudioProxyView` was the one hop in the chain that logged nothing at
all, which is why the above is ambiguous. It now logs, on every stream
teardown: duration, bytes forwarded, and which side ended it (upstream
EOF / client disconnect / proxy exception). Between that and the card's
status line, the next run should place the failure exactly.

If it does turn out to be the service worker killing long `fetch()`es,
the option not yet tried is an `<iframe sandbox="allow-scripts">` player
document: a sandboxed iframe gets an opaque origin, and service workers do
not control opaque-origin documents, so a plain `<audio src>` inside it
reaches the network directly. That needs `Access-Control-Allow-Origin` on
`SDS200AudioProxyView` (the opaque origin makes the request cross-origin),
which is ours to add -- and it needs no MediaSource at all.

### Root cause of the ~30s stop: the service worker, again (2026-07-25)

The card's status line, on the run after it finally reloaded:

```
stream failed TypeError: Error in input stream at 30.0s
```

That is the *same* Firefox service-worker error documented twice above --
but the important new fact is **when**: not on connect, at 30.0 seconds. So
the earlier conclusion that "Firefox's fetch() rejects fast, so it falls
back to the iframe in a moment" was wrong. The fetch succeeds, streams
perfectly for half a minute, and *then* the service worker's handling of a
never-ending response body gives out. The card's fallback only triggers when
playback fails to *start*, so it never fired -- it had no idea anything was
wrong until well after it had declared success.

Reading HA's own service worker (`frontend/src/entrypoints/service-worker.ts`)
settles what can be done about it:

- `/(api|auth)/.*` → `NetworkOnly` (no `networkTimeoutSeconds`, so the 30s
  is Gecko's, not Workbox's).
- Behind it, a **catch-all route matching every path** →
  `StaleWhileRevalidate`.

So every same-origin request from the page is intercepted, and relocating
this endpoint can't help -- moving it off `/api/` would be actively worse,
landing it on the catch-all, which would try to *cache* an endless stream.

**Fix**: run the fetch somewhere the service worker doesn't control. An
`<iframe sandbox="allow-scripts">` (no `allow-same-origin`) has an opaque
origin, and an opaque origin has no service worker registration, so its
requests go straight to the network. `sds200-card.js` now has three
transports, tried in order, each falling back to the next if playback
doesn't start:

1. **relay** -- a sandboxed `srcdoc` iframe (`RELAY_HTML`,
   `_openRelayStream()`) whose only job is to fetch the stream and
   `postMessage` the bytes to the card. It plays nothing: decoding stays on
   the card's own `<audio>` element, which already holds the user
   activation from the Play click, so this depends on no autoplay
   permission inside a sandboxed frame.
2. **direct** -- the plain in-page fetch this replaces, kept because it is
   the one transport confirmed to decode on Android (for 30s).
3. **iframe** -- the old navigate-an-iframe-at-the-stream trick.

The status line names the winner ("playing (relay)"), so the next run says
which of these still earn their place.

Two consequences of the opaque origin, both handled: the relay's requests
are *cross-origin*, so `SDS200AudioProxyView` now sends
`Access-Control-Allow-Origin: *` (not as broad as it looks -- the URL
carries a signed, short-lived `authSig`, so it only widens access to
someone who already holds a valid signed URL), and they carry no cookies,
which is fine for exactly the same reason.

Watch out for, when editing the big JSDoc block above `_startAudio()`: it
describes these service-worker routes, and writing a regex like the
catch-all's literally inside a `/** ... */` block ends the comment early at
the `*/` and breaks the file. Describe it in words instead.

**Confirmed on the real install (2026-07-25)**: both clients report
`playing (relay)` and play past 30 seconds -- desktop Firefox and the Home
Assistant Android app, on the same transport for the first time. That
closes out both symptoms in this section: the Android silence (a stale
cached card, now fixed by the versioned card URL) and the ~30s cutoff (the
frontend service worker, now bypassed by the sandboxed relay iframe).

The `direct` and `iframe` transports are now dead weight in every
environment tested so far -- but they cost nothing while unused, and the
history in this file is a long list of things that worked in one browser
and not another, so they stay until there's a reason beyond tidiness. The
status line naming the winning transport is what makes that decidable
later.

## Card showed "unavailable": the scanner's network stack wedged (2026-07-25)

Symptom: every entity on the card read `unavailable`. Reloading the
integration and restarting the add-on changed nothing -- correctly, since
neither is where the fault was.

Diagnosis, from a LAN host on the same segment (192.0.2.248), bypassing HA
and the add-on entirely:

- ARP/ICMP: **alive**. `ping` 0% loss, and the ARP entry was
  `00:e0:11:12:23:6f` -- OUI `00:E0:11` is Uniden, so the IP had not been
  reassigned to some other device by DHCP (worth ruling out first; it looks
  identical from HA's side).
- UDP 50536 (control): **dead**. Six `MDL\r` datagrams over 25s, no reply.
- TCP 554 (RTSP) and TCP 80: **dead**. `Connection timed out`, not
  refused.

So the scanner was answering at the IP layer while every application
listener was gone. **ICMP replies prove nothing about this device's
services** -- the ping is answered by a part of the stack that survives
whatever kills the rest. Probe 50536 directly before suspecting the
add-on or the integration:

```python
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2)
s.connect(("192.0.2.232", 50536)); s.send(b"MDL\r")
print(s.recv(4096))   # healthy: b'MDL,SDS200\r'
```

A power-cycle fixed it, as this file's earlier RTSP-wedge notes predict:
~18s after restart `MDL` answered, `VER` reported `Version 1.23.15`, `STS`
returned live screen content, and 554 was open again. Unlike the previous
wedges, this one took control (50536) down with it, not just RTSP -- the
earlier notes' "control is unaffected" does not generalize.

Why `unavailable` specifically, rather than a stale-looking card: the
add-on's `_poll_loop` keeps `last_status` on a failed poll (it only counts
failures toward `CONTROL_UNREACHABLE_THRESHOLD` and logs), so a scanner
that wedges under a running add-on leaves the card showing **stale but
plausible** values indefinitely. Restarting the add-on is what reset
`last_status` to `{}`, and `SDS200Entity.available` is
`super().available and bool(self._status)` -- hence unavailable. Note the
consequence: `unavailable` here was a side effect of the restart, and had
the add-on not been restarted the card would have kept displaying a frozen
screen with no indication anything was wrong. Wiring the add-on's existing
unreachable detection through to an explicit push (so the entities go
unavailable on their own) is the obvious follow-up; not done yet.

### Auto-reboot on control failure (the follow-up above, done)

`auto_reboot_on_control_failure` (add-on Configuration tab, per scanner,
off by default, needs `poe_reset_*` to do anything): if the UDP control
interface stays dark for `CONTROL_REBOOT_AFTER` (120s), the add-on
power-cycles the scanner's PoE port, waits `REBOOT_COOLDOWN`, and
re-checks -- up to `CONTROL_REBOOT_MAX_ATTEMPTS` (3) times, then gives up
and just waits, logging why.

It is a **separate toggle from `auto_reboot_on_audio_failure`, and had to
be**: the existing auto-reboot counts consecutive *RTSP session* failures,
and a scanner with a dead control interface produces none of those.
`_supervise_forever()` gates every RTSP attempt on
`control.wait_until_reachable()`, so a wedged scanner isn't retried, never
fails, and never reaches the audio-failure threshold -- the audio
auto-reboot cannot fire in exactly the situation that most needs a reboot.
That's why the incident above sat there until it was noticed by hand.

Implemented as `AudioBridge._await_control()`, replacing the bare
`await self.control.wait_until_reachable()` at the top of the supervisor
loop. It lives in `AudioBridge` (not `ScannerConnection`) because
`trigger_reboot()`, `poe_reset` and `_reboot_lock` do, and running it on
the same loop as the audio-failure reboot means the two can only fire in
sequence, never concurrently at the same switch port.

The attempt cap is the part worth keeping: without it, a scanner that's
been unplugged, renumbered or has died would have its switch port
power-cycled every ~3.5 minutes indefinitely.

Covered by `tests/test_audio_bridge_control_reboot.py` (stubs `aiohttp`,
shrinks the timing constants) -- every branch here only runs when the
hardware is already broken, which is not a state that can be reproduced on
demand, so shipping it untested would have meant shipping untried code
that cuts power to a production switch port.

Still not done, from the section above: the add-on never *tells HA* the
scanner is unreachable (it keeps `last_status` on a failed poll), so the
card still shows stale-but-plausible values rather than going unavailable
on its own.

## Keeping the phone's screen awake while audio plays (2026-07-25)

Listening to a scanner is precisely the case where nobody touches the
screen, so the display sleeps within a minute and takes the card with it.
Three mechanisms exist; the card now tries the first two itself and points
at the third when neither takes.

1. **Screen Wake Lock API** (`navigator.wakeLock.request("screen")`).
   Correct, standard, and *expected to be missing on the one platform this
   is for*: Chrome for Android has had it since 84, but per MDN's compat
   data (and caniwebview, which derives from it) **Android WebView does not
   support it** -- a WebView holds no window it could set Android's
   keep-screen-on flag on. The HA Android app renders the frontend in a
   WebView, so on the phone this tier is expected to no-op.

   It also can't be requested once and forgotten: the spec releases the
   lock whenever the document becomes hidden and does *not* restore it, so
   the card re-requests on `visibilitychange`. Without that, a lock only
   ever covers the first visible stretch of a session -- which would look
   like it works, because the first stretch is when you're watching.

2. **A playing `<video>`.** Chromium keeps the screen on while a visible
   video element plays; that's a side effect of its media stack rather than
   an API, which is exactly why it works in a WebView where tier 1 doesn't
   (this is the old NoSleep.js trick). Frames come from a 12x12
   `canvas.captureStream(2)` repainted on a 500ms timer, so no encoded
   video asset ships with the card, and the canvas has to keep *changing*:
   a capture stream emits a frame only when its canvas does, and a video
   whose frames stopped arriving is not reliably still "playing" to the
   thing we're trying to influence.

   The `.keep-awake` CSS is load-bearing and looks like dead styling: 12px
   at 2% opacity, deliberately not `display:none`/zero-size/`opacity:0`,
   because Chromium decides "is this video visible" geometrically and a
   video it thinks is invisible is one it won't hold the screen on for.
   This is the opposite of `.audio-frame-container` right below it (which
   *is* zero-size), so the two blocks look inconsistent on purpose.

3. **The companion app's own setting** -- Settings > Companion app > Other
   settings > **Keep screen on** (it only applies while a dashboard is
   showing). A page can't reach an app setting, so the card can only name
   it. Worth knowing: it can also be toggled remotely by notifying the
   device with `message: command_screen_on` and `data: {command:
   keep_screen_on}`, which is what an automation would use.

Which tier won is written to its own status line under the audio one, in
the same spirit as the transport names -- the Android app has no reachable
JS console, so a status line is the only channel that survives it. Expect
"screen held awake (video)" on the phone and "(wake lock)" on desktop; if
the phone says video and the screen still sleeps, tier 2 doesn't work in
this WebView and tier 3 is the answer.

Branch coverage in `tests/test_card_screen_lock.js` (Node, stubbed DOM --
run separately from the Python suite). Stubs are the only way to reach the
missing-wakeLock path from a machine whose browser has the API; what they
cannot answer is whether tier 2 really holds the screen on in the app,
which only the phone can.

## Settings moved out of Supervisor and into the add-on's own UI (2026-08-03)

Scanners used to be configured in Supervisor's Configuration tab, i.e. as a
YAML blob validated against `config.yaml`'s `schema:` and delivered to the
add-on as `/data/options.json`. They're now configured in the add-on's own
web page, served over Home Assistant ingress by the same `aiohttp` app as
the REST API (`settings_api.py` + `app/www/`), and stored in
`/data/config.json` (`config_store.py`).

The motivating problems were all recorded above, in this file:

- **A new schema field doesn't reach the add-on until the Add-on Store's
  "Reload"** -- not a rebuild, *Reload* -- and until it does, Supervisor
  passes the saved value straight through as a raw string instead of
  validating and coercing it. That is the whole reason `_as_bool` exists
  (see the `poe_reset_use_ssl: false` saga): the add-on read the *string*
  `"false"`, `bool("false")` is `True`, and it kept dialling `https://` at
  a router with no `www-ssl` listener while the UI plainly read "false".
  Nothing about that failure is discoverable from the UI.
- **`password?` in a schema triggers a Have I Been Pwned lookup on save**,
  which times out and fails the entire save as an "unknown error" on a
  network with no route to HIBP. The workaround was to declare the router
  password as a plain `str`, losing the masked input. The add-on's own
  form has neither problem: the field is `type="password"`, and a *stored*
  password is never sent to the browser at all (see below).
- **No feedback.** A partial `poe_reset_*` config was ignored with a
  `logger.warning` nobody reads, so the symptom was a reboot button that
  silently did nothing.

The new form covers those directly: per-field help text, live warnings for
an incomplete PoE config or auto-recovery switched on with nothing to use,
a live reachability dot per scanner (the control interface actually
answering, which is the single most common thing to get wrong), and a
re-render from what the server *stored* after each save, so "the value I
typed didn't take" is impossible to miss.

### Applying a change without restarting the add-on

`manager.ScannerManager.apply()` diffs the new config against what's
running and touches only what changed: started, restarted, stopped, and --
the point of the exercise -- **left running untouched**.

That last category is why the obvious implementation (write the file, ask
Supervisor to restart the add-on) was rejected. This scanner's RTSP server
wedges on repeated session teardown/reopen and takes a physical
power-cycle to clear; a restart would tear down and reopen the audio
session of *every* scanner, so renaming one would put unrelated hardware
at risk. RTP ports are allocated from a free pool rather than by list
index for the same reason: by index, deleting the first of three scanners
moved the other two onto new ports, which would have forced precisely the
restart this avoids.

`api.create_app()` is given the manager's `scanners`/`audio_bridges` dicts
once at startup, and the manager mutates them in place, so the REST routes
keep seeing the current set without the app being rebuilt.

### Stored passwords are never sent to the browser

`GET /api/settings` returns `"__stored__"` in place of a saved router
password; posting that value back means "keep the one you have".
Restoration matches **by scanner id, not list position** -- reordering or
deleting a scanner in the form shifts every later index, and matching by
position would hand one scanner another's router credentials. The accepted
trade-off is that *renaming* a scanner changes its id and so loses the
stored password (it has to be retyped), which is much better than a
reorder silently leaking one. Both directions are covered in
`tests/test_config_store.py`.

### Migration, and why `config.yaml` still has the old schema

`/data/options.json` is read exactly once, on the first start after the
upgrade, to seed `config.json`; after that it's ignored no matter what
anyone types into the (now legacy-labelled, via `translations/en.yaml`)
Configuration tab.

The old `scanners:`/`log_level` schema is deliberately still in
`config.yaml` for this one release. Removing it looks like the obvious
cleanup and would **destroy the thing being migrated**: Supervisor strips
keys its schema no longer declares out of `options.json` on its next
validation pass, which happens on update -- before the add-on ever runs.
Both blocks can go once everyone has started 0.2.x at least once.

Covered by `tests/test_config_store.py` (migration runs once, a corrupt
config fails loudly rather than starting empty and looking like a fresh
install, atomic save) and `tests/test_manager_reconfigure.py` (the
untouched-scanner and RTP-port properties above, which are invisible when
they regress -- everything still works, the audio just gets torn down more
often than it should).

## Receive history and alerts (2026-08-05)

The add-on now reconstructs *calls* from its own poll stream, keeps them as
a searchable history, and can fire a webhook / HA service / HA event when
one matches a rule. All of it is in the add-on (`reception.py`,
`history.py`, `triggers.py`) and surfaced in its web UI, not in the
integration -- the add-on is the only thing with a continuous view of the
scanner, and an HA sensor's state history can't answer "what did I hear at
14:20" the way a purpose-built log can.

### A "call" is inferred, and what that costs

The scanner pushes nothing. Everything here is reconstructed from the `GSI`
poll loop, so:

- **Squelch-open detection** is `Property/@Rssi` != `-999` (its documented-
  by-observation "not receiving" sentinel -- confirmed earlier alongside a
  literal `RSSI: ---` in the same capture's `STS` text), falling back to
  `Property/@Sig` > 0 where the mode matrix omits `Rssi`.
- **A call is a contiguous run of receiving polls with the same identity**,
  where identity is system + department + channel + frequency + talkgroup.
  Unit ID is deliberately *not* part of it: on a trunked system the
  talkgroup stays put while individual radios key up in turn, and including
  the unit would shred one conversation into a row per transmission. Units
  heard during a call accumulate on the record instead.
- **Ending a call needs two consecutive idle polls.** One failed `GSI` read
  is indistinguishable from the squelch closing, and this firmware drops
  them routinely (see "Confirmed against real hardware" above), so a
  one-poll rule would split most calls in two. An identity *change*, by
  contrast, closes the call immediately -- that's positive evidence of a
  different transmission, not an absence of evidence.
- **Transmissions shorter than the poll interval can be missed entirely**,
  and every timestamp/duration is poll-quantized (duration is a lower
  bound). `GSI_POLL_INTERVAL` is now a per-scanner setting for exactly this
  reason, floored at 1s -- below that this firmware's `GSI` responses start
  timing out more often than they succeed, which loses more calls than the
  faster polling catches.

Records are appended to the history when a call *starts* and mutated in
place as it runs, so an add-on restart mid-transmission leaves a record with
`ended: null`. `ReceiveHistory.load` closes those on read and marks them
`interrupted`; without that they render as a transmission that has been
running for days.

### Digital mode classification is best-effort, on purpose

`reception.classify_mode` returns one of `analog`/`p25`/`dmr`/`nxdn`/
`provoice`/`edacs`/`ltr`/`motorola`/`unknown`, in this precedence order:

1. `Property/@P25Status` -- the only field that reports an *observed*
   decode rather than how the system was programmed, so it outranks
   everything else.
2. `System/@SystemType`, substring-matched. Order within the table matters:
   `MotoTRBO Capacity Plus` has to resolve to `dmr` before a bare `motorola`
   entry can claim it.
3. `ConvFrequency/@Mod`, and only far enough to say `analog` when the
   modulation is one of AM/FM/NFM/WFM/FMB.

**Only `Conventional` has ever been seen in a capture from this project's
hardware.** Every other `SystemType` string in `_SYSTEM_TYPE_MODES` comes
from Uniden's own naming and is unverified here. Anything unmatched stays
`unknown` rather than being guessed at, and every alert rule can match the
raw `system_type` string directly -- which is the escape hatch, and the
thing to reach for before adding a guess to the table. Note also that a
*conventional* system with a non-analog modulation (e.g. `AUTO`) classifies
as `unknown`, not `analog`: conventional systems can hold digital channels,
and claiming otherwise would be worse than admitting ignorance.

### Alert delivery

Three action types, all in `triggers.py`. The HA ones go through the
Supervisor proxy (`http://supervisor/core/api/...`) with `SUPERVISOR_TOKEN`,
which requires **`homeassistant_api: true` in `config.yaml`** -- without it
the token is still issued and the proxy answers 401, so `_ha_request`
special-cases that status and names the missing permission rather than
surfacing a bare auth error.

Cooldown is keyed by `(rule id, identity)`, not by rule alone: a rule
watching a whole department shouldn't have an alert for one unit swallow
another's thirty seconds later. The map is bounded and swept of expired
entries, since a rule matching everything on a busy system would otherwise
grow it without limit.

Rule ids are generated server-side in `config_store.normalize_trigger`, not
in the UI: the UI can reorder and delete freely, a client-assigned id would
be lost by anything editing `config.json` directly, and both the cooldown
bookkeeping and the "last fired" display are keyed by it.

### Partial saves

The web UI's tabs save independently (`POST /api/settings` vs
`POST /api/triggers`). Both go through `settings_api._save`, which merges
the posted payload **over the stored config** before normalizing. Without
that merge, `normalize()` would default every key the payload didn't carry
back to empty -- i.e. saving a scanner would silently delete every alert.

### Audio in the add-on's own UI

> **Superseded** by "The add-on UI's Listen button: the relay can't work
> behind ingress (2026-08-05)" below. The relay described here never worked
> on this page; it is a WebSocket now. Kept as written for the trail.

The add-on's page is served through ingress, i.e. from Home Assistant's own
origin, so it is subject to the same service-worker interception that broke
audio in the Lovelace card (see "Root cause of the ~30s stop", above). The
add-on's `www/audio.js` therefore carries the same sandboxed-relay-iframe +
MediaSource approach the card landed on, reimplemented rather than shared:
the card ships inside `custom_components/` and this ships inside the add-on
image, and neither can import a file from the other.

One difference worth recording: the card's *third* fallback -- navigate an
`<iframe>` at the stream URL and let the browser render a media document --
is deliberately absent here. That trick is desktop-only (the HA Android
app's WebView doesn't render a media document for `audio/mp4`), and unlike
the card's original situation this page always has a real in-page `<audio>`
element to decode into, so there is nothing for it to add.

## The add-on UI's Listen button: the relay can't work behind ingress (2026-08-05)

Reported symptom: **Listen in the add-on's own web UI doesn't work.** (The
Lovelace card's Play, on the same audio pipeline, is fine.)

The section above -- written when this page was built -- says the page
"carries the same sandboxed-relay-iframe + MediaSource approach the card
landed on". That was the mistake. The relay transport is inseparable from
*how the card's stream URL is authenticated*, and behind ingress that
authentication does not exist:

| | Lovelace card | add-on page |
| --- | --- | --- |
| URL | `/api/sds200/<id>/audio/stream.mp3?authSig=…`, HA core's proxy view | `scanners/<id>/audio/stream.mp3`, through ingress |
| Auth | a **signed**, short-lived query parameter | the `ingress_session` **cookie** |
| CORS | view sends `Access-Control-Allow-Origin: *` | nothing sends any CORS header |

A sandboxed `<iframe>` without `allow-same-origin` has an **opaque origin**.
That is what puts it outside the service worker -- and it is also what makes
its requests cross-origin and cookie-less. The card pays neither price
(signed URL, explicit `ACAO: *`). The add-on page pays both: the relay's
`fetch(url, {credentials: "omit"})` is blocked by CORS before it is sent,
and would be answered 401 by the Supervisor if it were sent, because ingress
authenticates by a cookie an opaque origin cannot carry. There is no signed
equivalent to reach for -- ingress has no signed-URL scheme.

So the relay could never start, every session fell through to the `direct`
in-page fetch, and that is exactly the transport HA's service worker kills
at ~30s (`/(api|auth)/.*` → `NetworkOnly` still runs the request *inside*
the worker; ingress paths live under `/api/hassio_ingress/…`, so they match).
Two independent bugs cancelling into one symptom: the fallback was the only
transport that ever ran, and the fallback is the broken one.

**Fix: a WebSocket transport** -- `GET /scanners/{id}/audio/ws` in `api.py`,
`_wsSource()` in `www/audio.js`, tried first, with `direct` kept behind it.
Same bytes (the cached init segment, then ffmpeg's 4 KiB fragments), same
MediaSource decode; only the pipe changes. It clears all three obstacles at
once:

- **Service worker**: a WebSocket handshake raises no `fetch` event, so
  there is nothing to intercept and no never-ending response body to
  mishandle. This is the part that actually fixes the ~30s cutoff, rather
  than dodging it from an opaque origin.
- **Ingress auth**: it is an ordinary same-origin request, so the
  `ingress_session` cookie goes with it.
- **CORS**: not cross-origin, so there is nothing to allow.

None of that is speculative for this deployment -- the page has kept a
status WebSocket open through ingress since the settings UI landed, so
ingress WebSocket forwarding, the cookie and the service worker's
indifference to the handshake are all already proven here in production.

The HTTP `stream.mp3` route stays exactly as it was: it is what the
integration's `SDS200AudioProxyView` fetches, and none of the above applies
on that path.

Two smaller things found while in here, both able to produce "the button
doesn't work" on their own once audio *had* started:

- The `<audio>` element was created detached and never inserted, which makes
  it eligible for garbage collection mid-playback. The card had already
  learned this (`#audio-frame-container`); this page had not. It now decodes
  into an element in `#audio-sink`.
- A stream that ended cleanly *after* playback started left the button
  saying "Stop" over a dead player, since `start()` had long since resolved
  and nothing told the panel otherwise. `AudioPlayer` now takes an
  `onStopped` callback, and says which of the two happened ("the add-on
  ended the stream" vs "the audio stream failed").

Tests: `tests/test_api_audio_ws.py` runs the route against a real aiohttp
server with a fake bridge -- init segment first, binary frames, the socket
closing on `STREAM_CLOSED`, and unsubscribing when the client goes away.

### Renamed: Alerts -> Actions (2026-08-05)

The tab, the file (`www/alerts.js` -> `www/actions.js`), the class
(`AlertsView` -> `ActionsView`) and every user-facing string. Nothing in
storage or on the wire changed: the config key and the API were already
`triggers`, never `alerts`, so this is a naming change with no migration
behind it.

One knock-on worth knowing about. A rule has always contained an *action*
(`rule["action"]`, `ACTION_TYPES` in `triggers.py`) -- what to do when it
matches. Now that the rule itself is called an action, the field inside it
that used to be labelled "Action" is labelled **"Type"**, under the section
header "Do this". The Python side keeps `action` for the sub-object, since
renaming that *would* be a stored-config migration.

### Volume and squelch follow the scanner (2026-08-05)

`media_player`'s volume and `number.<id>_squelch` were optimistic: HA showed
whatever it had last set, so a level turned on the unit's own knob, changed
from the add-on's web UI, or simply left over from before an HA restart was
invisible -- most visibly in the card, whose +/- steppers read those entity
states and so stepped from a stale number.

They are not actually unobservable. GSI's `Property` element carries `VOL`
and `SQL` next to `Rssi`, and the add-on already polls GSI every 3s and
pushes it in the status feed -- the add-on's own control panel has been
syncing its sliders off `Property.VOL`/`.SQL` all along
(`www/control.js:_syncSliders`). The integration just never read them.

`custom_components/sds200/levels.py` now reconciles the two sources for both
entities. A level HA sets outranks the reading until the reading agrees with
it or `SETTLE_SECONDS` (8s) passes -- a GSI poll already in flight when a set
lands still carries the pre-set value, and honouring it would snap the card
back to the old level for a poll or two. After the window the scanner wins:
if it still disagrees by then, the set didn't take (dropped UDP command, or
a value the scanner clamped) and the scanner is the one telling the truth.

Knock-on: mute. There's no mute command in the protocol, so muting sets the
scanner to 0 and unmuting restores. That restore level used to be whatever
was in `_volume`; now that `_volume` tracks the scanner it becomes 0 while
muted, so the level to restore is held separately (`_muted_restore`). Any
non-zero `volume_set` also clears the muted flag, which the old code didn't
do -- it could otherwise report muted while audibly playing.

Tested by `tests/test_levels.py` (no `homeassistant` import needed: levels.py
is deliberately dependency-free, same trick as the api_client test).

Two UI changes alongside it: the card's audio button says **Listen** /
**Stop**, matching the add-on's control panel and the README, instead of
"Play audio" / "Stop audio". And the History tab names the scanner on every
row, not only when more than one is configured -- records outlive the
scanner list they were recorded against, so a row for a since-removed
scanner could not say where it came from.

### Weather alerts: actions, and getting back to scanning (2026-08-05)

Two halves, both hanging off state the add-on was already polling.

**Firing actions on an alert.** `WxMode/@Mode` (`"Monitor Weather"` /
`"Weather Alert"`, plus `SAME`) is only present in GSI while the scanner is
in weather mode at all — see "Weather alert sensor" above, where the
integration's binary_sensor already reads it. `reception.extract` now lifts
it onto every snapshot (`wx_mode`, `wx_same`, `wx_alert`), and
`app/weather.py` watches the status stream for the rising edge and hands it
to `TriggerEngine.on_call` as its own event. So an alert is an action rule
like any other — same matching, cooldown, delivery and "last fired" display
— rather than a second delivery path.

The alert is deliberately an *edge*: the same alert state repeats on every
poll, and firing per poll would send a notification a second for as long as
the alert ran. `wx_clear` is the falling edge, for the "turn it back off"
half of an automation.

One trap this exposed in the pre-existing code. `matches()` tested
`rule["event"] not in (event, "both")`, i.e. a rule set to "both" fired on
*any* event name it didn't recognize. Adding `wx_alert` would therefore have
made every existing "when a call starts and ends" rule start firing on
weather alerts. `triggers.fires_on` now says it explicitly: "both" is the two
*call* edges (`CALL_EVENTS`), never everything.

**Getting back to scanning.** The scanner parks on the alert screen until a
key is pressed, so unattended it just stops scanning. The key it wants isn't
in the KEY table under a fixed name, and there's no documented "resume
scanning" command (`JPM` might do it — untried), but the alert screen labels
one of its soft keys "TO SCAN" — and those labels come through in every STS
poll, so the add-on can read which of soft1/soft2/soft3 it currently is and
press that. Per-scanner setting, off by default; a fallback key exists for a
screen with no such label but defaults to unset, because pressing an
arbitrary key on an unknown screen can hold or avoid a channel and leave the
scanner stranger than the alert did. Bounded at three attempts per alert.

Reading those labels needed a parser fix. `DSP_FORM` does *not* reliably
count the soft-key row: one real capture has 17 digits and 18 line pairs
(the label row falling outside the count, into the leftovers before the nine
reserved fields), another has 17 and 17 (inside it). So position can't find
the row — the label row's own per-character "mode" field can, since it is
three runs of asterisks marking each key's column
(`"********* ********** *********"`). `protocol._parse_soft_keys` scans for
that mask wherever it landed, and returns `[]` rather than guessing when
there isn't one.

**Not restarting the scanner to change it.** These two settings are the
first that nothing about the connection or the audio session depends on.
`manager.WATCH_ONLY_FIELDS` excludes them from the restart diff and applies
them to the running `WeatherWatch` instead, so changing "return to scanning
after 60s" doesn't tear down and reopen an RTSP session on hardware where
that is a real risk (see manager.py's docstring).

### Create an action from a history row (2026-08-05)

The History tab's rows now carry a "Create action" button that opens the
Actions tab with a new rule prefilled from that call. It fills in the call's
*identity* (system / department / channel / talkgroup, or the frequency when
there is no talkgroup) rather than everything on the record — a rule
carrying the frequency *and* the talkgroup *and* the unit id would only ever
fire on that one radio keying up again.

Ordering matters in the shell: `_show("actions")` kicks off a refresh, and
that refresh replaces the whole rule list. Switching tabs first would land
the reload *after* the prefilled rule was added and silently discard it — so
`App._createAction` awaits the refresh, then shows the tab with the reload
suppressed.

### The display box hugs its own 30 columns (2026-08-05)

The add-on's Control tab rendered the mirrored screen in a `<pre>` that
stretched to the panel's width. The text inside it is a fixed 30-character
grid, so everything the scanner right-aligns *within* that grid — the clock,
`VOL: 4 SQL: 4`, `TGID: ---`, `Site ID: ---` — ended up floating in the
middle of a mostly-empty green box instead of against its right edge, which
is where it is on the unit. Fixed with `width: max-content` on a wrapper
(`.screen-stack`), not by touching the text: the alignment was always
correct, the box was just too wide for it. The card already had the same fix
for the same reason (`width: 30ch`), from earlier.

The three soft keys moved out of the key column and under the screen, as
wide as it — where they are on the unit's face, and the only place their
labels (System / Dept / Channel, and on the weather-alert screen "TO SCAN")
mean anything.

### The Lovelace card is a readout, not a front panel (2026-08-05)

The card carried the whole panel: mirrored display, keypad, soft/nav keys,
volume and squelch steppers, and a "Restart Scanner" button. It now shows
what the add-on's own web UI shows for a *collapsed* scanner panel — the
label line (channel / department / system), frequency, modulation, tone/NAC
and RSSI, with the same green "receiving" border — plus **Listen**.

Reasoning: the add-on's web UI is the control surface and is better at it
(it drives the scanner directly over its own API, with live status). A
dashboard card duplicating it added a second thing to keep in step, and one
of its buttons power-cycled hardware on a mis-tap. Listening is the one
thing the card does that the web UI can't do as conveniently from a
dashboard, so that stays.

The card reads entities, not the add-on's status feed, so the field list is
what the integration actually exposes: talkgroup, unit, site and system type
have no sensor behind them and are simply absent rather than faked. The RSSI
sensor doubles as "is it receiving" — it is `None` whenever the squelch is
closed (sensor.py maps the -999 sentinel out), which is what drives the
green border.

Removed with it: `_pressKey` / `_stepVolume` / `_stepSquelch` / `_restart` /
`_callService` / `_resolveDeviceId` and the `KEY_LAYOUT`, `FUNC_KEYS`,
`MAX_VOLUME`, `MAX_SQUELCH` constants. The card no longer calls a service at
all. `sds200.key` / `sds200.reboot` are untouched and still available to
automations and to anything else that wants them.

### Permanent avoid: AVD with a target read out of GSI (2026-08-06)

The Avoid button on both surfaces presses the front panel's AVOID key
(`KEY,L,P`), which is a **temporary** avoid — cleared by the next power
cycle. Making it permanent means the `AVD` command, and the reason nothing
used `AVD` before is that it names what to avoid by **list index**: the one
`AVD` this project ever sent had an empty target (`AVD,,,,2`) and the
scanner did not act on it (see the card's Avoid button).

The indices were already arriving. `GSI` puts an `Index` on the very
elements `reception.py` reads the channel out of — the same mode-dependent
lookup as everywhere else, `ConvFrequency` conventional and `TGID` trunked:

```xml
<ConvFrequency Name="Channel 3" Index="20251" Avoid="Off" Freq=" 462.612500MHz" ... />
```

So `POST /scanners/{id}/avoid_current` (`api.post_avoid_current`) resolves
the target with `reception.avoid_target()` and sends
`AVD,<tkw>,<index>,,1`. Status 1 is the permanent one; the empty field is
AVD's second index, which a channel target doesn't appear to need.

**It re-polls GSI first** (`ScannerConnection.refresh_gsi`, factored out of
the GSI poll loop) rather than reading `last_status`. GSI is polled every 3s
by default and it is the flakier of the two polls, so `last_status["gsi"]`
can be seconds stale — and a permanent avoid aimed at the channel the
scanner has just moved off is not undoable from this add-on. If that poll
fails the endpoint answers 409 rather than falling back to stale data:
pressing the button again is the correct recovery.

**`AVD,CFREQ,<index>,,1` is confirmed working on real hardware** (2026-08-06,
conventional scanning, first try) — so `CFREQ` is the right target keyword
for a conventional channel, and GSI's `Index` is in the index space AVD
expects. That is the first `AVD` this project has landed, and it settles the
one thing the empty-target attempt couldn't distinguish: the command was
never the problem, the missing target was.

**Still unverified: `TGID`, for a trunked talkgroup.** It comes from the same
second-hand reading of the spec's "tkd and 1st,2nd opt" table (never
transcribed into this repo) as `CFREQ` did, and there is no trunked GSI
capture here carrying an `Index` to check it against. So the endpoint keeps
returning the command it sent and the scanner's raw reply verbatim, and the
web UI prints both on a refusal — `the scanner refused AVD,TGID,31007,,1
(AVD,NG)` names which half to change. `tkw` and `status` in the request body
override the defaults, so trying candidate keywords against live hardware
stays a curl loop rather than a code change.

**Not verifiable from GSI afterwards, either.** The obvious check — re-read
`Avoid` on the same element — doesn't work: a successful avoid makes the
scanner resume scanning, so the next GSI describes a *different* channel and
the avoided one is simply gone. `AVD,OK` is the only acknowledgement there
is.

The button lives in the add-on's web UI only, as **Perm Avoid** in the
eighth cell of the function-key grid — next to Srv Type, filling the gap
seven keys leave in four columns, two along from the Avoid key it is the
lasting version of. It carries the danger colour rather than a fill, is
disabled whenever the screen has no indexed entry to point AVD at, and
confirms before it fires: it is the one control on that panel whose effect
survives a power cycle, and clearing it means going to the scanner's own
menus.

What it would hit is in its tooltip, not standing beside it. That started as
a visible label naming the target, which earned its place only while nobody
knew whether the command worked; once it did, a channel name re-rendering
every few seconds next to a key was noise. The confirm dialog still names
the target — that is the check that matters, since it is the last thing
between a tap and something irreversible — and so does the result line
afterwards, which says so explicitly when the scanner had moved on to a
different channel between the two.

### Tracking avoids, and undoing them (2026-08-06)

The undo problem is the index, again, from the other direction. Reversing a
permanent avoid is `AVD,<tkw>,<index>,,3` — the same command, status
changed — but an avoided channel is *precisely* the one the scanner never
stops on again, so it never reappears in the GSI poll stream and its index
is unrecoverable a second after it was used. `GLT` can still be walked to
find it by name, but nothing about that is convenient from a dashboard.

So the add-on writes each one down. `avoids.AvoidLog` persists to
`/data/avoids.json` every avoid it sends that the scanner acknowledged, with
the tkw and index **as sent** — not re-derived later, so an undo replays the
command it is undoing — plus the system/department/frequency that
`reception.extract` read off the same GSI, so a row reads like a channel
rather than a number months later.

Written through on every change rather than debounced like `history.py`:
these arrive a handful of times a day, not a handful of times a minute, and
losing one to an unclean shutdown loses the only copy of a number that
cannot be looked up again. Only status 1 is recorded, and only on `AVD,OK` —
a temporary avoid clears itself at the next power cycle, so a record of it
would age into a lie, and a refused one never happened.

`GET /scanners/{id}/avoids` lists them, newest first.
`DELETE /scanners/{id}/avoids/{n}` sends the status-3 command and drops the
record **only if the scanner acknowledged it**; a refusal keeps the row,
since dropping it would discard the only copy of the index and strand a
channel avoided on hardware with nothing left able to reach it.
`?forget=true` drops the record without sending anything.

Two limits, both structural rather than fixable:

- **It only knows about avoids this add-on made.** Front-panel avoids were
  never seen by this process. The list is "what I did", not "what the
  scanner is avoiding".
- **A record is what was sent, not what is still true.** Clearing an avoid
  on the unit leaves a stale entry. Hence Forget — an operator assertion,
  not an inference.

> **Corrected 2026-08-13.** This entry originally said both limits were
> undetectable, "there is no command that enumerates what the scanner is
> currently avoiding". That was wrong. `GLT` reports the avoid state of
> every entry in the tree, so both are answerable by reading the scanner —
> see "Reading the avoid state back" below. The limits above are still true
> of the *file*; they were never true of the *scanner*. Forget stays an
> operator assertion, but now because the sweep that would justify inferring
> it costs minutes, not because nothing could know.

The UI is per scanner in **Settings**, not on the Control tab: making an
avoid is an operating action, reviewing what you have accumulated is
administration. It's fetched per section rather than carried in the settings
payload — live state the Control tab changes, not configuration, so it
neither dirties the form nor gets POSTed back on save. One consequence worth
knowing: the log is keyed by scanner id, and the id is derived from the
scanner's name, so **renaming a scanner orphans its avoids** — the same
thing that happens to its history and its HA entities.

### The scanner does not take our avoid as permanent (2026-08-06)

Permanent avoids kept coming back after a power-cycle, and the first theory
was that the add-on's own Power-cycle was to blame: cut the PoE port with no
warning and anything the scanner hadn't written to flash goes with it. `POF`
was wired in ahead of the cut on that basis. It was wrong, and the test that
settled it needs recording, because it rules out a whole class of theory:

> **A permanent avoid made from the front panel (press AVOID twice) survives
> an abrupt PoE power cut.** Confirmed on this hardware.

So the scanner persists permanent avoids without any graceful shutdown, the
power cut was never the problem, and `POF` was removed again along with its
five-second delay. (For the record: the SDS200 never answered `POF` --
`no response to 'POF'` on every attempt -- which proves nothing on its own,
since a command that powers the unit off has an obvious reason not to reply,
but there is now no reason to keep sending it.)

What remains is that `AVD,CFREQ,<index>,,1` is *accepted* (`AVD,OK`),
*applied* (the channel stops being scanned), and *not persisted* -- which is
the behaviour of a temporary avoid. The scanner is not treating our status 1
as permanent. Candidates, in order:

1. **The empty second index.** `AVD,[TKW],[XXX1],[XXX2],[STATUS]` is a
   second-hand reading, and "1st and 2nd opt" suggests a target named by a
   *pair* of indices. GSI hands us all of them --
   `System/@Index`, `Department/@Index`, `ConvFrequency/@Index` -- and we
   have only ever sent one, leaving the other field blank. A firmware that
   can't resolve the target precisely might fall back to avoiding "what I am
   on", temporarily.
2. **The status is positional and we are off by one.** If a channel target
   takes a single index, `AVD,CFREQ,<index>,1` is the real form and our `1`
   has been landing in the second index field all along, with the status
   field left empty and defaulting to temporary.
3. **The status values differ.** 1/2/3 for permanent/temporary/clear is from
   the same second-hand source as everything else.

All three are distinguishable on live hardware and none of them can be
distinguished from the reply, since `AVD,OK` comes back either way -- the
test is always "power-cycle and see". `POST /scanners/{id}/command` exists
for exactly this: it sends a raw command and hands back the raw reply, so
working through candidate forms is a curl loop rather than a release each
time.

### The screen text stopped opening itself mid-scan (2026-08-06)

The raw-display disclosure is supposed to open on its own when GSI has no
fields for what's on screen — a menu, the boot screen, a weather alert —
because there the display lines are the only thing there is. It was doing it
while the scanner was scanning normally, most visibly on digital systems.

The test was `!readout || !readout.channel`: no *channel name* right now. But
a trunked system has no talkgroup name between calls, so every gap in the
traffic read as a blank screen and popped the panel open. Two changes:

- The condition is now "nothing identifying at all" — no channel **and** no
  department **and** no system. A scanning scanner still names its system
  and department in the gaps; a menu names nothing.
- It has to stay that way for `BLANK_UPDATES_TO_OPEN` (3) consecutive
  pushes. GSI drops reads regularly, and one poll with a gap in it is a gap
  in the poll rather than a change of screen.

Counting rather than remembering the previous state also made it a genuine
one-shot: it opens on the way in and then leaves the disclosure alone, so
closing it again isn't overridden a second later.

### How avoids actually persist: two copies, and only one way to save (2026-08-06)

Established against real hardware over a run of experiments. This supersedes
the guesses in the two entries above.

**The scanner keeps two copies of the avoid state**: a working copy in RAM
and a saved copy in flash. `GLT` reads the working copy and reports three
values per entry — `Off`, `T-Avoid` (temporary) and `Avoid` (permanent) —
which makes it an instant oracle. No power cycle is needed to see what the
scanner *thinks*; a power cycle is only needed to see what it *saved*.

**`AVD,CFREQ,<index>,,1` is the right command and it does set a permanent
avoid** — `GLT` reads back `Avoid`, not `T-Avoid`. It writes to RAM only.
Nothing about the reply distinguishes this: `AVD,OK` comes back from
commands that do nothing at all (see the field-form table below).

**Only a keypad press that *sets* a permanent avoid saves**, and it flushes
the entire working copy, committing every pending `AVD` change along with
it. Demonstrated directly: a channel flagged by `AVD` alone, with no key
press anywhere near it, survived a power cycle because a double-press
elsewhere flushed it.

Everything else tried does **not** save: a temporary avoid, a clearing
press, a volume write, a menu round-trip.

**The escalation is timing-sensitive.** Two `KEY,L,P` presses 0.25s apart
produce `Avoid`. The same two presses ~1.2s apart leave it at `T-Avoid`, and
further slow presses don't escalate it. A third fast press returns the
channel to `Off` (confirmed by the owner, and by test) — but that clearing
write does not save, so it cannot be used as a harmless flush.

Field forms, all against a channel target, all confirmed:

| Command | Reply | GLT after |
|---|---|---|
| `AVD,CFREQ,<chan>,,1` | `AVD,OK` | `Avoid` |
| `AVD,CFREQ,<chan>,,2` | `AVD,OK` | `T-Avoid` |
| `AVD,CFREQ,<chan>,,3` | `AVD,OK` | `Off` |
| `AVD,CFREQ,<dept>,<chan>,1` | `AVD,OK` | unchanged — silently ignored |
| `AVD,CFREQ,<chan>,1` | `AVD,ERR` | unchanged |
| `AVD,,,,1` | — | unchanged (the original empty-target no-op) |

So `[XXX2]` is genuinely unused for a channel target, and `AVD,OK` is worth
nothing as a success signal. **`GLT` is the only way to know what happened.**

**`GLT` list types**, confirmed: `FL` (favorites lists), `SYS` (systems),
`FTO` (tone-outs), `DEPT` and `CFREQ` — the last two require the parent
index (`GLT,DEPT,<system>`, `GLT,CFREQ,<department>`). An unrecognized list
type silently returns the `FL` list rather than erroring, so a typo looks
like data.

**Two unrelated bugs surfaced on the way:**

- **`HLD,,,` answers `HLD,ERR`.** That is exactly what `sds200.hold` and the
  card's Hold button send, so that feature has never worked — as the comment
  in `sds200-card.js` guessed it might not.
  `HLD,CFREQ,<index>,` answers `HLD,OK` but the scanner keeps scanning and
  GSI still reports `Hold="Off"`, so a working hold is still unknown.
- **`MSB` answers `MSB,ERR`** at every menu depth. `MNU,MENU` enters the menu
  fine; `KEY,M,P` is what gets back out, and it doesn't always work on the
  first try. A scanner left in `Menu tree` has stopped scanning, so nothing
  should enter the menu without a verified way out.

**What this means for the feature.** The precise, index-targeted command
can't be made to stick on its own, and the only thing that saves is a key
press that avoids whatever the scanner is on at that instant. So a genuinely
permanent avoid has to be a keypress on the intended channel, which means
the targeting problem and the persistence problem cannot both be solved at
once — and any implementation has to verify with `GLT` afterwards rather
than trust `AVD,OK`.

One consolation: a mis-aimed press is self-correcting. Clearing the wrong
channel with `AVD,...,3` and then making a correct permanent avoid saves
both changes in the same flush.

### The permanent avoid, as built (2026-08-06)

Given the findings above, `POST /scanners/{id}/avoid_current` presses the
unit's AVOID key twice rather than sending `AVD`. That reverses the earlier
decision, and the reasoning is worth stating because `AVD` is not *wrong* --
it sets a genuine permanent avoid, precisely, by index. It just writes the
working copy only, so a power cycle discards it, which makes it
indistinguishable in practice from the temporary avoid the Avoid key already
did. A "permanent" avoid that behaves exactly like the temporary one is a
rename, not a feature.

So: two `KEY,L,P` presses `AVOID_PRESS_GAP_S` (0.25s) apart, nothing read
between them, because the escalation is timing-sensitive.

**The press is unaimable, so the result is checked instead.** A key press
lands on whatever the scanner is on at that instant and it is scanning;
there is no working hold to park it first (`HLD,,,` answers `HLD,ERR`).
`_avoid_state` reads `GLT,CFREQ,<department>` a second later and reports what
the scanner's own list says about the intended index:

| GLT reads | meaning | reported as |
|---|---|---|
| `Avoid` | landed, and saved | success |
| `T-Avoid` | landed, but the presses were too far apart | failure, "try again" |
| `Off` / missing | the scanner had moved on | failure, and *another channel may have been caught* |

`KEY,OK` is not treated as evidence of anything, because it isn't. The
department index comes from GSI alongside the channel index
(`reception.avoid_target`), and the route refuses outright if there isn't
one -- an unverifiable permanent avoid is the thing this is trying to stop
being.

**Un-avoiding is the mirror problem and is now honest about it.** `AVD,...,3`
takes effect immediately but is never saved, so the channel is scanning
again *now* and avoided again after the next restart. `AvoidLog.mark_cleared`
keeps the record with a `cleared_at` stamp instead of deleting it, the
Settings list shows it as "un-avoided, but not saved", and its Un-avoid
button is disabled since there is nothing left to send. The next successful
permanent avoid flushes the working copy and commits every one of them, at
which point `commit_pending` drops those records and the UI says how many
were saved.

### Perm Avoid only works while the scanner is stopped (2026-08-06)

First live test of the double-press route failed in exactly the way the
design anticipated: `ok: false`, `state: "Off"`, intended channel untouched.
The verification earned its keep on its first outing — the previous
implementation would have called that a success.

The cause is the one thing not accounted for. A free-scanning SDS200 steps
through channels many times a second, so the channel GSI named is long gone
by the time two key presses have gone out over UDP. They landed on something
else, and a sweep of the departments involved found no trace of what — there
are thousands of channels and no command that lists what is avoided.

Called again while the scanner was **stopped on a transmission**
(`Property/@Rssi` above the -999 sentinel) it was exact: `ok: true`,
`state: "Avoid"`, the right channel, recorded, and still avoided after a
power cycle. That same flush also committed two channels left stranded by
earlier experiments, confirming the commit-everything-pending model end to
end.

So `post_avoid_current` refuses unless `reception.extract()` reports
`receiving`, and the button is disabled with a tooltip saying so. This isn't
a limitation to work around: it's the condition a person already operates
under at the front panel. You hear something, you press Avoid. Aiming at a
channel the scanner is merely passing through was never meaningful — the
display is a blur at that point, and it only looked meaningful because GSI
polls slowly enough to freeze one frame of it.

Every refusal leaves the avoid log untouched, as does every post-press
failure. A record is written only once GLT confirms `Avoid`, because a
record is a claim that a channel is avoided *and* that the index in it is
the way back.

### Fixing Hold: it needs a target too (2026-08-06)

`HLD,,,` -- "hold here", no target -- answers `HLD,ERR`. That is what
`api.post_hold` sent from the day it was written, so `sds200.hold` and the
card's Hold button have never worked. The comment in `sds200-card.js`
guessed it might not (*"If it turns out to need a target too, it will now
say so rather than quietly doing nothing"*) and it was right.

HLD names its target by list index exactly as AVD does, and
`HLD,CFREQ,<index>,` holds. Confirmed against the hardware: GSI's `mode`
goes from `Scan Mode` to `Scan Hold`, and the element's own `Hold` attribute
from `Off` to `On`.

**It toggles.** The same command sent again releases. So the route reads the
current state before deciding whether to send anything: no `hold` in the
body means toggle (what a button labelled Hold should do), while an explicit
`hold: true`/`false` is a target state, and asking for a hold already in
place is a no-op rather than an accidental release.

`HLD,OK` is not evidence, as usual on this hardware -- it came back from
`HLD,CFREQ,<index>,` in an early probe where the scanner plainly kept
scanning, because the read-back happened before the state had settled. The
state in the response is re-read from the scanner afterwards
(`reception.is_holding`, which accepts either the mode or any element's
`Hold` flag).

`reception.avoid_target` is now `channel_target`: two commands name their
target the same way, and the name should say what it resolves rather than
what the first caller wanted it for.

One caution for anything that holds automatically: a held scanner has
stopped scanning, and nothing in the add-on notices. A probe left this
scanner parked in `Scan Hold` on one channel until it was released by hand.

### A real weather alert, caught live (2026-08-11)

The scanner was found sitting in an actual weather alert -- the state the
whole of `app/weather.py` was written against the spec for, and had never
been seen. Captured read-only over UDP (`STS`, `GSI`) before touching
anything; both captures are in `tests/fixtures.py` as
`STS_RESPONSE_WEATHER_ALERT` / `GSI_RESPONSE_WEATHER_ALERT`.

What the alert actually looks like:

```
<ScannerInfo Mode="WX Hold" V_Screen="wx_alert">
  <WxMode Mode="Weather Alert" SAME="Alert Only" />
  <WxChannel Index="0" CH_No="1" Freq=" 162.550000MHz" Mod="FM" Hold="On" ... />
```

**The spec-derived reading was right.** `WxMode/@Mode` really is
`"Weather Alert"` with a `SAME` attribute, so the binary_sensor, the
`wx_alert`/`wx_clear` events and `reception.extract`'s `wx_alert` were all
correct as written. The 2026-07-23 caveat above is now retired.

**`find_scan_key` works on the real screen.** The soft-key labels came
through as expected and the way out was `soft1`, labelled `to Scan` (note
the casing -- the earlier guess was "TO SCAN"; the match was already
case-insensitive). Pressing it (`KEY,A,P`) put the scanner straight back to
`Mode="Scan Mode" V_Screen="conventional_scan"` with `WxMode` gone from GSI
entirely, screen reading "Scanning...". So reading the label instead of
hardcoding a key is confirmed end-to-end, not just in principle.

**The bug the screen exposed.** Its soft-key label row is the only one seen
so far with custom glyph bytes *between* the labels:

```
raw   ' to Scan  \x01\x01\x01\x01\x01\x01\x01\x01\x01\x01  RESUME  '   (30 cols)
mask  '********* ********** *********'                                 (0-9 / 10-20 / 21-30)
```

`_parse_display_lines` stripped control/glyph bytes *before*
`_parse_soft_keys` cut the labels out by column, so the row arrived 20
characters wide and every label right of a glyph run slid one key left:
`['to Scan', 'RESUME', '']` -- the live add-on reproduced exactly that.
Benign only by luck here, because `to Scan` happens to sit left of the
glyphs; a screen with the scan label in the third column would have had the
add-on press the middle key, which on this hardware is how you hold or avoid
a channel by accident. `_parse_soft_keys` now takes the unstripped row and
strips each label after cutting it out, so a label that is nothing but
glyphs comes back empty (`['to Scan', '', 'RESUME']`) rather than shifting
its neighbours. Every other row in the fixtures keeps its glyphs outside the
label columns, which is why 247 tests passed over the bug.

**A third DSP_FORM data point.** 17 digits against 21 line pairs here (the
others are 17/18 and 17/17), so the label row landed well outside the count
again -- the mask-scanning approach is the only thing that finds it.

**The design hole it exposed.** `V_Screen` distinguishes two things the code
had been treating as one. The alert is a *state* that comes and goes on its
own; `wx_alert` is the *screen*, and the screen is what stops the scanner
scanning and stays up until a key is pressed. `weather.py` drove its
return-to-scan press off the alert, so an alert clearing before the
configured wait cancelled the press and left the scanner parked -- the exact
"quietly stopped scanning hours ago" failure the module exists to prevent.
The events still fire on `WxMode` (that is what an automation means by "a
weather alert came in"); the press now follows `reception.extract`'s new
`wx_parked` (`V_Screen == "wx_alert"`, falling back to `wx_alert` when GSI
sends no `V_Screen`). Leaving the screen is also the only real confirmation
that a press worked -- `KEY,OK` says the key was accepted, not that it was
the right key, so the two are logged separately.

**Why it was still sitting there.** `wx_return_to_scan_s: 0` and no trigger
rules configured -- the feature is off by default and nothing was set up, so
nothing fired and nothing pressed. Working as configured.

**Still open: the other weather state.** Everything above is one screen,
`V_Screen="wx_alert"` with `Mode="WX Hold"`. The scanner has been seen in at
least one *other* weather state that hasn't been captured yet -- most likely
`WxMode Mode="Monitor Weather"` (deliberately sitting on a weather channel,
which `wx_parked` treats as not-parked on purpose), or a WX scan/search mode
with its own `V_Screen`. Worth capturing `STS`+`GSI` the same read-only way
next time it turns up, because two questions depend on which it is: whether
that state also stops the scanner scanning (and so should count as parked),
and whether its soft-key row carries glyphs in different columns. Until then
`wx_parked` is deliberately narrow -- it only rescues the one screen we have
actually seen park a scanner.

> **Answered the same day** -- see "The other weather screens" below. Both
> guesses were wrong: it is a modal popup on the *same* `V_Screen`, and
> `Monitor Weather` turns out to be what the scanner reads while parked on
> the screen the popup leaves behind. The screen count is three, not two.

Two handling notes for future live probes. The add-on's API answered on
`http://192.0.2.235:8000` this time, not the `:8001` recorded in the older
"probing the router directly" entry above -- check both before concluding
the add-on is down. And ad-hoc UDP queries from a third
host worked alongside the add-on's own session, went silent for ~15s across
the mode change after the key press, and came back on their own. The add-on's
persistent session saw the transition throughout, and `reachable` never
dropped -- so that silence is the scanner being busy, not the wedge that the
RTSP server is known for.

### The other weather screens (2026-08-11, later)

The scanner turned up in the state the entry above left open, and it is not
either of the things that entry guessed. Captured `STS`+`GSI` read-only
before touching anything (twice, plus eight polls over 40s to confirm it was
stable, not a flicker); everything below is in `tests/fixtures.py`.

**A weather alert opens as a modal popup, not the soft-key screen.** Same
`Mode="WX Hold" V_Screen="wx_alert"` as the capture above -- so `wx_parked`
was already true and the add-on was already trying to rescue it -- but the
screen is entirely different:

```
STS,1111111111,  ...  Warning WX ... WX Alert ...  ,1,1,0,0,,,5,RED,3
<ScannerInfo Mode="WX Hold" V_Screen="wx_alert">
  <WxMode Mode="Weather Alert" SAME="Alert Only" />
  <ViewDescription>
    <PopupScreen Text="Warning WX&#xD;WX Alert        &#xD;&#xD;">
      <Button Text="&quot;E&quot; (OK)" KeyCode="E" />
```

**A popup takes the soft-key row with it.** STS carries 10 line pairs here
against the other screen's 21, and *no mask row at all* -- so `parse_sts`
returns `soft_keys == []`, `find_scan_key` returns None, and with the
fallback key unset (the default, and deliberately so) the add-on logged "no
soft key offering a way back to scanning" and left the scanner parked. The
screen the whole module exists to rescue was the one screen it could not
touch. `DSP_FORM` is 10 digits here, a fourth data point for the count
meaning nothing.

**The way out is in GSI, and `gsi_to_dict` was dropping it.** `PopupScreen`
is the first GSI element seen with a child of its own, and the flattener kept
each `ViewDescription` child's attributes and discarded its children -- so
`Button/@KeyCode`, the only machine-readable way off the screen, never
reached the code. It is carried through under `buttons` now, and surfaces as
`reception.extract`'s `popup_keys`.

**One press is not a rescue.** Pressing the popup's own key (`KEY,E,P`) does
*not* resume scanning. It dismisses the popup and reveals the WX Hold screen
underneath -- still `V_Screen="wx_alert"`, still `WxChannel Hold="On"` on
162.550 -- which is the screen from the earlier capture, soft-key row and
all, where `to Scan` is what actually resumes. Getting out is a chain of two
presses on two screens. `MAX_ATTEMPTS` is counted per *screen* now (a screen
that changes under a press is progress, not a failed attempt), with a new
`MAX_PRESSES` backstop across the whole park so two screens that lead to each
other can't key the panel forever.

Note also what `WxMode` did across that press: `"Weather Alert"` with `SAME`
became `"Monitor Weather"` with none. The alert state ended when the popup
was dismissed while the scanner stayed parked -- so `Monitor Weather` is not
only the "sitting on a weather channel on purpose" state the earlier entry
took it for, and treating it as not-parked would have abandoned the scanner
at exactly this step. `wx_parked` following `V_Screen` is what saves it.

**A third screen.** From the WX Hold screen, `RESUME` (soft3) gives
`Mode="WX Scan"` -- walking the WX channels, `WxChannel Index="3" CH_No="4"
Hold="Off"` -- still on `V_Screen="wx_alert"`. Still not scanning the
systems the user cares about, so still correctly parked, and `to Scan` stays
in the first column while soft3 relabels itself `HOLD`. That the third
column's label changes under the same screen name is the argument for
reading labels rather than remembering key positions, again.

**A key aimed at one screen can land on another.** During the capture a
`KEY,A,P` meant for the WX screen's `to Scan` arrived after the scanner had
already gone back to `Mode="Trunk Scan" V_Screen="trunk_scan"` on its own
(`DualWatch WX="Priority"` dips into weather and returns without anyone
pressing anything). On that screen soft1 is `SYSTEM`, and the press put the
system into hold -- `System ... Hold="Off"` to `"On"` -- which stops the
scanning the press was trying to restore. This is the accidental-hold hazard
the earlier entry flagged in theory, reached in practice by a stale screen
rather than a mis-parsed column. `weather.py` now re-reads GSI immediately
before sending (`refresh_gsi`, the same escape hatch `post_avoid_current`
uses for the same reason) and sends nothing if the scanner has left the
screen the key was chosen for.

**`KEY,E,P` on the popup returned an empty response**, where every other
key press on this hardware answers `KEY,OK`. The key was plainly taken --
the screen changed within 2s -- so this is "no reply", not a refusal, and it
is logged as such rather than as the scanner rejecting the key. Caveat: this
was an ad-hoc UDP socket alongside the add-on's own session, which the entry
above already records going quiet around mode changes, so it may be the
probe rather than the scanner. Worth re-checking from the add-on's session.

Unverified, and the one thing here not confirmed against hardware: whether
`soft1` dismisses the popup. The popup was cleared with the `E` it names,
and by the time the design settled on pressing `soft1` there (a soft key
position, so the same key works whichever weather screen is up) the alert was
over. `find_way_out` presses `soft1` first and falls back to the popup's own
declared key codes on the next attempt, so it clears either way -- but which
of the two does the work on a real popup is still open.

### Reading the avoid state back, and what the real scanner turned out to hold (2026-08-13)

The avoid log answers "what did I do". This answers "what is true", which
had been written off as unanswerable twice in this file and isn't.

**`GLT` reports `Avoid` on every element it returns, at every level.** Not
just channels: systems, sites, departments and talkgroups all carry the same
`Off` / `T-Avoid` / `Avoid` attribute. So the scanner's entire avoid state is
enumerable by walking the tree, and an avoid nothing recorded can be found
by walking it.

**Two list types this file never had.** The confirmed set was `FL`, `SYS`,
`FTO`, `DEPT`, `CFREQ`. Add:

| Command | Returns |
|---|---|
| `GLT,TGID,<department>` | the talkgroups of a *trunked* department, each with `Index`, `Name`, `TGID`, `AudioType`, `SvcType` and `Avoid` |
| `GLT,SITE,<system>` | the sites of a trunked system, with `Index`, `SiteId`, `Name` and `Avoid` |

`GLT,TGID` is the one that matters, and its failure mode is nasty:
**`GLT,CFREQ` on a trunked department returns zero entries, not an error.**
An audit that only ever asks for `CFREQ` therefore skips every trunked
system in silence and looks like it covered the database. On this scanner
that is 88 of 359 systems. Pick the child list from the system's `Type`
field — anything other than `Conventional` is trunked. (`TG` and `TFREQ`
fall into the already-documented trap of silently answering with the `FL`
list.)

**The full walk, measured.** 359 systems → 1938 departments → 6614 channels,
plus the sites and talkgroups of the 88 trunked systems. Paced at 30ms
between reads it takes about 3.5 minutes and never failed a read. Ad-hoc
queries ran alongside the add-on's own session and its live audio without
disturbing either.

**What the hardware actually held**, run against the eight records in
`/data/avoids.json`:

- All eight read back `Avoid`, and the `Name` in the list matched the name
  recorded at the time — so the indices are aimed where the log claims, not
  merely at something avoided.
- **A ninth permanent avoid nobody recorded**: index `19604`,
  "Aircraft Emergency and Distress (VHF Guard)", 121.500 MHz, department
  `19600` "Aviation" of system "Common Aviation - USA". Two of the recorded
  eight are also 121.5 guard channels, so the likely story is the mis-aim
  this design already anticipates: a double-press that verification scored
  `ok: false` because the *intended* channel read `Off`, while the press had
  landed on a different 121.5 entry. Nothing is recorded on failure, so the
  collateral went unlogged — which is precisely the hole the sweep exists to
  find, and it found one on its first real run.
- 26 systems and 111 channels at `T-Avoid`, across 34 systems, none of them
  from this add-on. Temporary, so they clear at the next power cycle.
- Sites, departments and talkgroups: all `Off`.

**Built as `avoid_audit.py`**, behind `GET /scanners/{id}/avoids/verify`.
The default reads back only the departments the log mentions — one read
each, fast enough to run from the Settings section — and reports each
record's state against what it should be (a cleared record expects `Off`,
not `Avoid`). `?sweep=true` does the full walk and additionally returns what
no record accounts for, split into permanent and temporary because they call
for different reactions. The sweep is never implicit: it is thousands of
reads on a radio someone is listening to.

One reporting decision worth keeping: the three states are passed through
rather than collapsed into a boolean. `T-Avoid` on a record that claims a
permanent avoid means the two presses were too far apart and it wants doing
again; `Off` means the record is stale and wants Forget. Those are different
repairs, and a bare "disagrees" would hide which.

**A fourth outcome, learned the hard way: no answer at all.** The first
version scored a record `agrees: state == expected`, and a read that failed
left `state` at `None` — so a single timed-out `GLT` was reported as every
record in that department disagreeing with the scanner, in red, while the
same check a minute earlier said all eight agreed. Two things came out of
that. A read the scanner never answered is now counted and shown apart from
one it contradicted (`unknown` per record, and in the route's totals), and
the same rule applies to the sweep: a walk whose `GLT,SYS` failed reads zero
systems, finds zero avoids, and must not render that as "nothing avoided
that isn't recorded here" — an all-clear issued without a single read behind
it. Second, `avoid_audit` retries each read once (`ATTEMPTS`). This is UDP
and `send_xml_command` has no retry of its own, unlike `send_command`; one
dropped datagram out of a few thousand is likely enough that the audit has
to survive it. Worth knowing when reading a failure detail: the timeout
raised on a read is `asyncio.TimeoutError`, whose `str()` is empty, so the
message falls back to the exception's class name rather than printing an
error inside empty parentheses.

**Still the working copy, not flash.** Everything above is what the scanner
is avoiding *right now*. A `T-Avoid`, and any `Avoid` that no keypad press
has flushed, is gone after the next power cycle — so a clean verify says the
avoid is live, not that it is saved. Nothing readable distinguishes the two;
only a power cycle does.

### The rescue held the scanner it was rescuing (2026-08-14)

Found the scanner reading `Mode="Scan Mode" V_Screen="conventional_scan"`,
display saying `Scanning...`, and:

```
System       "Citizens Band (CB) - USA"   Hold="On"
Department   "Citizens Band (CB)"         Hold="On"
ConvFrequency "Channel 40" -> "Channel 10" Hold="Off"
DualWatch WX="Priority"
```

So it was scanning — channels 1 through 40 of CB, and nothing else. The
newest receive-history record was 11.3 hours old. That is exactly the
"quietly stopped scanning hours ago" failure `weather.py` exists to prevent,
produced by `weather.py`. `wx_return_to_scan_s` was 180 (0 when the entries
above were written), `wx_return_to_scan_key` unset, no trigger rules, and
`app/weather.py:349` is the only place in the add-on that presses a key
unattended — every other `KEY,` send is UI-initiated. The keys it presses
are soft1/soft2/soft3, which on the scan screen are System/Dept/Channel hold
(the key table near the top of this file).

Released it read-only-first, one press at a time: `soft2` cleared the
department hold and `soft1` the system hold, each taking a GSI poll to show
up — worth knowing, because a check 2s after the press says nothing changed.

**`wx_parked` failed open.** It was
`V_Screen == "wx_alert" or wx_alert`, where the `or` was meant as a fallback
for GSI omitting `V_Screen` — but it applied unconditionally. `DualWatch
WX="Priority"` dips into the weather channel every few seconds and returns
on its own, and an alert still current in the region keeps `WxMode
Mode="Weather Alert"` in GSI across the dip. So a scanner that was scanning
perfectly well read as parked for as long as the alert ran, and a module
whose whole job is pressing keys at parked scanners had every reason to
press one at it. Now `V_Screen` decides whenever GSI sends one, and the
alert stands in only when it doesn't.

**And the confirm-before-press was only confirming half the screen.**
`screen_signature` identifies a screen by what it offers: soft-key labels
and popup key codes. Those come from *different commands* — labels from STS,
popup keys from GSI — and `_still_showing` called only `refresh_gsi`, which
merges `last_status["gsi"]` and touches nothing else. So the half that tells
the scan screen from a weather one was never re-read; it stayed whatever the
1/sec STS loop last left there. Added `ScannerConnection.refresh_sts` as the
counterpart (the STS poll loop now goes through it too) and `_still_showing`
calls both. Two round trips per press, and presses are rare.

The test fake had been hiding this: it swapped the whole `last_status` on
`refresh_gsi`, so a test saying "the scanner moved" moved both halves at
once and could not tell "re-read the screen" from "re-read half of it". It
now updates only its own half on each call, the way `protocol.py` does.

**Popup keys are tried before the guessed position now.** `find_way_out`
pressed `POPUP_DISMISS_KEY` (soft1) first and fell back to the popup's own
`Button/@KeyCode` after. That is backwards on both counts: soft1-on-a-popup
is the one thing in this design never confirmed against hardware, while `E`
is what was actually seen to clear one — and if the scanner has moved since
the key was chosen, `E` on the scan screen does nothing much where soft1
holds the system. Order reversed; soft1 is now the last resort for a popup
whose declared buttons all fail.

Worth stating plainly, because this is the third entry in a row about it:
every automated press this project makes is aimed at a screen read a moment
earlier, on hardware that changes screen without being touched. The guard is
not "did the key work" — it is "is the thing I aimed at still there", and it
has to re-read *everything* the aim depended on.

### Held on CB a third time, with the fix deployed (2026-08-15)

Found the scanner narrowed to Citizens Band again, the same shape as the
entry above: `Mode="Scan Mode"`, `V_Screen="conventional_scan"`, display
reading `Scanning...`, `DualWatch WX="Priority"`, and

```
System       "Citizens Band (CB) - USA"   Hold="On"   Index=36312
Department   "Citizens Band (CB)"         Hold="On"   Index=36315
ConvFrequency "Channel 22"                Hold="Off"
```

**The important difference: 0.7.9 was running.** The hold began at
`2026-08-14 02:22:39Z` — eighteen minutes *after* the accidental-hold fix
was committed and pushed. So the two bugs that entry fixed are not the
whole story, and the reflex of blaming `weather.py` is now the thing to be
careful about.

Reconstructed from the receive history, which is the only durable record of
when the scanner could hear anything:

| Window (UTC) | Gap |
| --- | --- |
| 08-12 14:15 → 08-13 14:04 | 23.8 h |
| 08-13 14:34 → 08-14 01:54 | 11.3 h (the entry above) |
| 08-14 02:22 → 08-15 03:1x | 24.8 h (this one) |

The lone CB record at 08-14 13:37 is CB traffic heard *while* held, not a
release — worth saying because it makes the gap look like two shorter ones
in a naive reading.

**What 0.7.9 leaves as the way in.** For a weather-screen key to land on the
scan screen now, the scanner has to still read `V_Screen="wx_alert"` with
matching soft-key labels through *both* re-reads and then move before the
KEY datagram lands — a window of one UDP send, and it has to be won twice
(System and Department are two presses) in two separate parks. Not
impossible; much narrower than what produced the first two.

**The other way in, which nothing rules out.** `app/www/control.js` maps
**System → soft1, Dept → soft2, Channel → soft3** as buttons on the Control
tab. Two clicks there produce exactly this state, including `Channel`
staying `Off`. 02:22Z is 22:22 local, i.e. during the session that shipped
0.7.9 — which is also when somebody would be poking those very buttons to
see what they do on the scan screen.

**The log is the discriminator, and only the log.** `weather.py` logs every
alert onset and every press it makes; `api.post_key` — the path the web UI
and the card use — logs nothing at all. So the add-on log across
`02:04–02:23Z` separates the two cleanly: weather lines present means a
residual hole, absent means a hand did it. Worth fixing regardless: a key
press that reaches the scanner is worth a log line whoever asked for it,
and its absence is why this entry cannot close.

**`HLD` names systems and departments too.** The release was done read-only
first, then one command at a time, and it settles a keyword this file has
been carrying as unverified since 2026-08-06 (only `CFREQ` and `TGID` were
ever confirmed; `reception.CHANNEL_TARGETS` still says as much):

```
HLD,DEPT,36315,   -> HLD,OK   Department Hold On -> Off
HLD,SYS,36312,    -> HLD,OK   System     Hold On -> Off
```

Both took a GSI poll to show up, same as `CFREQ` does. This matters beyond
tidiness: `HLD,<scope>,<index>,` is a *targeted* release, so unlike pressing
soft1/soft2 it cannot land on something else if the scanner moves between
the read and the send — which is the failure mode this whole run of entries
is about. It is the right way to release a stuck scanner, by hand or from a
script, and `POST /scanners/{id}/hold` already passes an explicit `tkw`
through verbatim.

**The actual gap, which is none of the above.** Three episodes, four days,
59 hours of a scanner that was not covering its lists, and nothing anywhere
noticed — not the add-on, not the integration, not the card, all of which
render `Scanning...` because that is what the scanner says. Every fix so far
has been aimed at one cause; the thing that was missing every time is
anything that notices the *effect*. This file already said so, in the
2026-08-06 Hold entry, before any of the three: *"a held scanner has stopped
scanning, and nothing in the add-on notices."*

Now `app/stuck.py` does. It watches two things and presses nothing, ever:

- **A scope hold** — `System`/`Department`/`Site` held for longer than
  `stuck_hold_s` (default 600s). Deliberately *not* the channel hold: that
  is what the card's Hold button and `sds200.hold` set, so watching it would
  fire on ordinary use until the alert got ignored. A scope hold has no
  deliberate path anywhere in this project, so it means the front panel or a
  stray soft key — which is exactly the accident being watched for. The
  split lives in `reception.SCOPE_HOLD_ELEMENTS`.
- **Silence** — nothing recorded for `stuck_silence_s` (default 0, off).
  Cause-agnostic backstop: catches a channel hold left on for a day, squelch
  wound shut, an antenna knocked off, a receiver that stopped while the
  control link kept answering. Off by default because how long is normal
  depends entirely on how busy the lists are.

It reports as a trigger event (`stuck`/`stuck_clear`) on the same `on_call`
path as the weather events, and repeats hourly while the condition holds —
an edge-only event sends its one notification at 02:00, which is precisely
how eleven hours happened twice. It never sends a key: every episode here
was caused by something pressing a key at a screen it had misread, and a
watchdog that responded by pressing more would be the same bug with a longer
fuse.

### It was never a new press: hold state lives in flash (2026-08-15)

Settles the entry above, and corrects it. The recurring CB hold was **not**
`weather.py`, not the Control tab, and not a fourth press from anywhere. It
was **one** hold, saved in the scanner's flash, restored on every power-up.

**The add-on log, from the window the entry above could not resolve** (log
is local, EDT = UTC-4):

```
23:50:16  last receive -- broad scanning
          ConnectionRefusedError: Connect call failed ('192.0.2.232', 554)
23:50:48  audio_bridge: power-cycling PoE port 'ether12'
23:51:00  control interface stopped responding
23:51:40  control interface responding again
23:52:19  audio session started
23:53     ...held on CB
```

Two absences in that log are what make it conclusive: **no `[weather]` lines
at all**, and **no `POST /scanners/{id}/key` in the aiohttp access log**,
which records every request the web UI, the card and the `sds200.key`
service make. Nothing pressed anything. The scanner was power-cycled and
came back held.

**Confirmed by controlled experiment, twice.** Released to a genuinely clean
state (`System`/`Department`/`Site`/`ConvFrequency` all `Hold="Off"`,
scanning federal systems), then `POST /reboot`:

| | before cut | after cut |
| --- | --- | --- |
| 04:06:05 | all `Off`, U.S. Federal Government | 04:07:03 `System`+`Department` **`On`**, CB |
| 04:12:08 | all `Off`, Indianapolis Regional Airport | 04:13:05 `System`+`Department` **`On`**, CB |

**Three release paths, only one of which saves:**

| Release | Effect | Survives a PoE cut |
| --- | --- | --- |
| `HLD,SYS,<index>,` / `HLD,DEPT,<index>,` | clears the hold immediately | **no** |
| `KEY,soft1/soft2,P` at the scan screen | clears the hold (one GSI poll later) | **no** |
| Graceful power-down at the unit | — | **yes** |

The second row is the one worth flagging, because it looked like the answer
and wasn't. The 2026-08-06 entry says only *a keypad press that sets a
permanent avoid* saves, and it means that literally: a hold press is not
that press, and a `KEY` sent over the wire does not flush anything. Reading
"keypad press" as the general case cost an experiment.

**This qualifies the 2026-08-06 conclusion.** That entry established that a
permanent avoid *"survives an abrupt PoE power cut"* and concluded *"the
power cut was never the problem"*. True for avoids, and the exact opposite
for holds: hold state is only written on a graceful shutdown, so an abrupt
cut doesn't lose the hold -- it **reveals** the saved one and discards
whatever the working copy had. `POF` was removed on the strength of that
entry; nothing here argues for bringing it back (the SDS200 never answered
it), but the asymmetry between the two kinds of state is now on record.

**Verified fixed.** Owner power-cycled the unit gracefully at ~04:18 with the
working copy clean, then `POST /reboot` at 04:23:54 -- an abrupt PoE cut, the
exact thing that had been resurrecting it:

```
04:24:47  first receive back (63s gap -- a real power cycle)
04:24:51  Indianapolis DPS                     SYS=Off DEPT=Off
04:25:16  Civil Air Patrol - USA               SYS=Off DEPT=Off
04:25:26  Customs and Border Protection - USA
04:25:31  Natl Incident Radio Support Cache - USA
04:25:38  Indiana Project Hoosier SAFE-T / Boone County / Hortonville
```

**How the hold got into flash is still unproven.** Two candidates, both
consistent with everything above and neither tested:

1. A **Perm Avoid double-press** made while the scanner sat on the CB hold.
   The 2026-08-06 entry proves that press flushes the *entire* working copy
   -- *"a channel flagged by `AVD` alone, with no key press anywhere near
   it, survived a power cycle because a double-press elsewhere flushed
   it"* -- and the avoid audit found exactly the fingerprint of one in that
   window ("a ninth permanent avoid nobody recorded", a mis-aimed 121.5).
2. A **graceful power-down** while held, which is now known to write hold
   state.

Distinguishing them costs a permanent avoid, and nothing depends on the
answer, so it is left open.

**Why it repeated daily: the add-on was power-cycling the radio on every
rebuild.** Restarting the add-on abandons the previous container's RTSP
session without a TEARDOWN. The scanner holds it until its own 60s
inactivity timeout expires and refuses new sessions the whole time --
`ConnectionRefusedError` on 554, which is correct behaviour, not a wedge.
With `AUTO_REBOOT_FAILURE_THRESHOLD = 3` and `RECONNECT_BACKOFF = 30.0` the
reboot decision landed at 60-90s, on top of that timeout, and lost the race.
Every lost race cut power; every power cut reloaded the CB hold.

`STARTUP_REBOOT_GRACE = 150.0` now holds the power-cycle back for the first
two and a half minutes of a bridge's life, logging why. It only delays:
failures keep counting, so a scanner still failing when it expires is
power-cycled on the next pass.

**And `api.post_key` now logs**, with the soft-key labels the key was
pressed against. Four investigations in a row turned on "did something press
a key", and the one route that could have answered said nothing while
`weather.py` logged every press -- so an empty log read as evidence when it
was only silence. `pressed soft1 against SYSTEM|DEPT|CHANNEL` is the whole
diagnosis in a line.

One last note for anyone reading the earlier entries in order: the
`stuck.py` watchdog shipped in 0.7.10 would have caught every one of these
within ten minutes, and it is cause-agnostic by design -- which is the only
reason it is still the right thing to have built, given the cause turned out
to be none of the ones anybody was looking for.
