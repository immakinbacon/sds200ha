"""Owns the running scanners and applies configuration changes to them.

Settings are edited in the add-on's own web UI now (see `settings_api.py`),
which means configuration can change while the add-on is running. Rather
than restart the container on every save, this applies the change in place
and only to the scanners it actually affects.

That distinction matters more here than the code cost suggests. This
scanner's RTSP server wedges when audio sessions are torn down and reopened
repeatedly (see `audio_bridge.py`'s module docstring), and recovering it
takes a physical power-cycle. Restarting the whole add-on to change one
scanner's name would drop and reopen the audio session of every *other*
scanner too -- turning a settings edit into a hardware risk for equipment
the edit had nothing to do with. So: a scanner whose settings did not
change is never touched.
"""

from __future__ import annotations

import asyncio
import logging

from audio_bridge import AudioBridge
from config_store import MAX_SCANNERS, POE_FIELDS, slugify
import stt
from mikrotik import MikrotikPoeReset
from protocol import ScannerConnection

logger = logging.getLogger(__name__)

RTP_BASE_PORT = 5004

# Scanner settings that only the add-on acts on -- nothing about the
# connection, the audio session or the scanner itself depends on them. They
# are applied to the running WeatherWatch/StuckWatch instead and excluded
# from the restart diff below, so changing "return to scanning after a
# weather alert" doesn't tear down and reopen an audio session (which this
# module exists to avoid doing needlessly -- see the module docstring).
WATCH_ONLY_FIELDS = (
    "wx_return_to_scan_s", "wx_return_to_scan_key", "stuck_hold_s", "stuck_silence_s",
    # Transcription reads the audio through AudioBridge's raw-payload
    # listener list, which is attached and detached independently of the RTSP
    # session. Listing these here is what keeps that true from the settings
    # side: leave them out and this module's exclusion-based diff treats them
    # as connection-affecting, so ticking "transcribe this scanner" would
    # tear down and reopen a session on hardware where that is the one thing
    # known to wedge it.
    "transcribe_enabled", "transcribe_modes", "require_audio",
)


def _connection_config(cfg: dict) -> dict:
    return {k: v for k, v in cfg.items() if k not in WATCH_ONLY_FIELDS}


def set_log_level(level: str) -> None:
    """Apply a log level by name. "trace" has no stdlib equivalent and
    falls back to INFO, matching what config.yaml's old schema allowed.

    aiohttp's access log is held at WARNING whatever the level, because it
    is not diagnostic here and it drowns everything that is: the settings
    page polls twice every five seconds per open browser tab, so a minute of
    log is fifty lines of `GET /api/settings 200` and whatever else happened
    to be in there. Turning debug logging on to investigate the audio and
    getting a wall of access lines back is how a real debugging session was
    lost. Warnings and errors from the web server still come through.
    """
    logging.getLogger().setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def _build_poe_reset(cfg: dict, scanner_id: str) -> MikrotikPoeReset | None:
    """poe_reset_* fields are flat (not a nested object) for the same reason
    they were flat in the old config.yaml schema: nothing needs the nesting,
    and the settings UI renders them as a labelled group anyway.

    All four of host/username/password/interface are required together --
    a partially-configured poe_reset is almost certainly a half-finished
    edit, not an intentionally-partial one, so warn rather than silently
    using an incomplete/broken reset mechanism. The settings UI surfaces
    the same thing as a warning at save time, so this log line is now the
    backstop rather than the only notice.
    """
    provided = {f.removeprefix("poe_reset_"): cfg.get(f) for f in POE_FIELDS}
    if not any(provided.values()):
        return None
    missing = [k for k, v in provided.items() if not v]
    if missing:
        logger.warning(
            "scanner %r has a partial poe_reset config (missing %s) -- ignoring it entirely",
            scanner_id, ", ".join(missing),
        )
        return None
    poe_reset = MikrotikPoeReset(
        host=provided["host"],
        username=provided["username"],
        password=provided["password"],
        interface=provided["interface"],
        verify_ssl=cfg["poe_reset_verify_ssl"],
        use_ssl=cfg["poe_reset_use_ssl"],
    )
    # Logged when the scanner starts, not just when a power-cycle is
    # attempted: whether poe_reset_use_ssl actually took effect is the thing
    # most worth being able to confirm *before* needing a reboot to work.
    logger.info(
        "scanner %r poe_reset configured: %s (interface %r, verify_ssl=%s)",
        scanner_id, poe_reset.url, provided["interface"], poe_reset.verify_ssl,
    )
    if poe_reset.use_ssl:
        # Nudged to warning: https is the default, so a user who set the
        # option and did not get it applied would otherwise have to know to
        # go looking for an info line to find that out.
        logger.warning(
            "scanner %r will use https:// for PoE reset. If you switched \"Use HTTPS\" off in "
            "the add-on's settings and are still seeing this, the save did not take -- reload "
            "the settings page and check the value it shows.",
            scanner_id,
        )
    return poe_reset


class ScannerManager:
    """Holds the live ScannerConnection/AudioBridge pairs and reconciles
    them against a configuration.

    `scanners` and `audio_bridges` are handed to the aiohttp app once at
    startup and mutated in place from then on, so the API routes always see
    the current set without needing to be rebuilt on every settings change.
    """

    def __init__(self, status_hub, history=None, trigger_engine=None, weather=None,
                 stuck=None, transcriber=None):
        self.status_hub = status_hub
        self.history = history
        self.trigger_engine = trigger_engine
        self.weather = weather
        self.stuck = stuck
        self.transcriber = transcriber
        self.scanners: dict[str, ScannerConnection] = {}
        self.audio_bridges: dict[str, AudioBridge] = {}
        # What each running scanner is running *with*, so a save that
        # didn't change a given scanner can be detected as a plain dict
        # comparison and skipped.
        self._running: dict[str, dict] = {}
        self._ports: dict[str, int] = {}
        # Two saves landing at once would otherwise interleave their
        # start/stop sequences and could hand the same RTP port to two
        # bridges.
        self._lock = asyncio.Lock()

    async def apply(self, config: dict) -> dict:
        """Reconcile the running scanners with `config`.

        Returns a summary of what changed, which the settings UI shows back
        so a save that deliberately did nothing (because nothing that
        matters to the running scanners changed) is distinguishable from
        one that silently failed.
        """
        async with self._lock:
            return await self._apply_locked(config)

    async def _apply_locked(self, config: dict) -> dict:
        # Where speech-to-text lives is global, so it is rebuilt here rather
        # than per scanner. Without this a changed address would be saved to
        # disk and shown in the UI while the running add-on kept talking to
        # the old one -- the settings equivalent of a lie.
        if self.transcriber is not None:
            transcribe_cfg = config.get("transcribe") or {}
            self.transcriber.set_client(stt.build(transcribe_cfg))
            if transcribe_cfg.get("max_segment_s"):
                self.transcriber.set_limits(
                    max_segment_s=transcribe_cfg["max_segment_s"],
                    silence_gap_s=transcribe_cfg.get("silence_gap_s"),
                )

        # Applied before the scanner reconcile below, so a save that both
        # switches history off and restarts a scanner doesn't record the
        # restart's first poll into a history the user just disabled.
        if self.history is not None:
            self.history.configure(
                enabled=config["history"]["enabled"],
                max_records=config["history"]["max_records"],
                retention_days=config["history"]["retention_days"],
            )
        if self.trigger_engine is not None:
            self.trigger_engine.set_rules(config["triggers"])

        wanted: dict[str, dict] = {}
        for cfg in config["scanners"]:
            scanner_id = slugify(cfg["name"])
            if not scanner_id or not cfg["host"]:
                # Blocked at save time by config_store.validate, so reaching
                # here means a hand-edited or migrated config. Skip the
                # entry rather than refuse to start at all -- one bad line
                # shouldn't take the working scanners down with it.
                logger.error(
                    "ignoring an unusable scanner entry (name=%r host=%r): both a name that "
                    "yields an id and a host are required",
                    cfg["name"], cfg["host"],
                )
                continue
            if scanner_id in wanted:
                logger.error(
                    "ignoring duplicate scanner %r: id %r is already taken", cfg["name"], scanner_id
                )
                continue
            if len(wanted) >= MAX_SCANNERS:
                logger.error(
                    "ignoring scanner %r: only %d scanners are supported (RTP port range)",
                    cfg["name"], MAX_SCANNERS,
                )
                continue
            wanted[scanner_id] = cfg

        connection_cfg = {i: _connection_config(cfg) for i, cfg in wanted.items()}
        removed = [i for i in self._running if i not in wanted]
        changed = [i for i, cfg in connection_cfg.items()
                   if i in self._running and self._running[i] != cfg]
        added = [i for i in wanted if i not in self._running]
        unchanged = [i for i in wanted if i in self._running and self._running[i] == connection_cfg[i]]

        # Stop first, so a changed scanner's RTP port is back in the pool
        # before anything asks for one -- otherwise a restart-in-place would
        # needlessly move to a different port.
        for scanner_id in removed + changed:
            await self._stop(scanner_id)
        for scanner_id in added + changed:
            await self._start(scanner_id, wanted[scanner_id])

        # Every scanner, not just the started ones: this is how a changed
        # WATCH_ONLY_FIELD reaches a scanner that was left running untouched.
        for scanner_id, cfg in wanted.items():
            if self.weather is not None:
                self.weather.configure(
                    scanner_id,
                    return_after_s=cfg["wx_return_to_scan_s"],
                    fallback_key=cfg["wx_return_to_scan_key"],
                )
            if self.stuck is not None:
                self.stuck.configure(
                    scanner_id,
                    hold_s=cfg["stuck_hold_s"],
                    silence_s=cfg["stuck_silence_s"],
                )
            if self.transcriber is not None:
                self.transcriber.configure(
                    scanner_id,
                    enabled=cfg["transcribe_enabled"],
                    modes=cfg["transcribe_modes"],
                    require_audio=cfg["require_audio"],
                )

        if removed or changed or added:
            logger.info(
                "applied settings: %d started, %d restarted, %d stopped, %d left running",
                len(added), len(changed), len(removed), len(unchanged),
            )
        return {
            "started": added,
            "restarted": changed,
            "stopped": removed,
            "unchanged": unchanged,
        }

    async def _stop(self, scanner_id: str) -> None:
        if self.history is not None:
            # Closes any call this scanner was mid-way through, so it lands
            # in the history with the duration it actually had rather than
            # hanging open until the scanner comes back and starts a new one.
            self.history.scanner_removed(scanner_id)
        if self.weather is not None:
            self.weather.detach(scanner_id)
        if self.stuck is not None:
            self.stuck.detach(scanner_id)
        if self.transcriber is not None:
            self.transcriber.detach(scanner_id)
        bridge = self.audio_bridges.pop(scanner_id, None)
        if bridge is not None:
            await bridge.stop()
        conn = self.scanners.pop(scanner_id, None)
        if conn is not None:
            await conn.stop()
        self._running.pop(scanner_id, None)
        self._ports.pop(scanner_id, None)
        logger.info("stopped scanner %r", scanner_id)

    async def _start(self, scanner_id: str, cfg: dict) -> None:
        port = self._claim_port(scanner_id)
        conn = ScannerConnection(
            scanner_id=scanner_id,
            name=cfg["name"],
            host=cfg["host"],
            control_port=cfg["control_port"],
            rtsp_port=cfg["rtsp_port"],
            gsi_poll_interval=cfg["gsi_poll_interval"],
        )
        conn.add_status_listener(self.status_hub.status_callback)
        if self.history is not None:
            conn.add_status_listener(self.history.status_callback)
        if self.weather is not None:
            # Attached before start(), so the very first poll of a scanner
            # that comes up already sitting in a weather alert is seen.
            self.weather.attach(conn)
            self.weather.configure(
                scanner_id,
                return_after_s=cfg["wx_return_to_scan_s"],
                fallback_key=cfg["wx_return_to_scan_key"],
            )
            conn.add_status_listener(self.weather.status_callback)
        if self.stuck is not None:
            # Attached before start() for the same reason as the weather
            # watch, and with rather more point: a scanner that comes up
            # already held is exactly the case this exists for, and its
            # silence clock has to start from when it started running
            # rather than from whenever the first call happens to land.
            self.stuck.attach(conn)
            self.stuck.configure(
                scanner_id,
                hold_s=cfg["stuck_hold_s"],
                silence_s=cfg["stuck_silence_s"],
            )
            conn.add_status_listener(self.stuck.status_callback)
        await conn.start()
        self.scanners[scanner_id] = conn

        bridge = AudioBridge(
            scanner_id=scanner_id,
            host=cfg["host"],
            rtsp_port=cfg["rtsp_port"],
            rtp_client_port=port,
            control=conn,
            poe_reset=_build_poe_reset(cfg, scanner_id),
            auto_reboot=cfg["auto_reboot_on_audio_failure"],
            auto_reboot_on_control_failure=cfg["auto_reboot_on_control_failure"],
        )
        # Started here unconditionally -- not on the first HTTP subscriber
        # -- and kept running until this scanner is stopped; see
        # audio_bridge.py's module docstring for why (repeated connect/
        # disconnect cycles wedge this scanner's RTSP server). The RTSP
        # session itself is still internally gated on `conn`'s control
        # interface being reachable -- see AudioBridge._supervise_forever.
        bridge.start()
        # After bridge.start(), and via the raw-payload listener list rather
        # than subscribe(): the tap is inert with respect to the RTSP session,
        # which is what lets transcription be switched on and off from the
        # settings UI without touching the radio. See transcribe.py.
        if self.transcriber is not None:
            self.transcriber.attach(scanner_id, bridge,
                                    poll_interval=cfg["gsi_poll_interval"])
            self.transcriber.configure(
                scanner_id,
                enabled=cfg["transcribe_enabled"],
                modes=cfg["transcribe_modes"],
                require_audio=cfg["require_audio"],
            )
        self.audio_bridges[scanner_id] = bridge
        self._running[scanner_id] = _connection_config(cfg)
        logger.info(
            "configured scanner %r as %r (%s, RTP port %d)",
            cfg["name"], scanner_id, cfg["host"], port,
        )

    def _claim_port(self, scanner_id: str) -> int:
        """Lowest free RTP port, rather than the scanner's index in the list.

        By index, deleting the first of three scanners would move the other
        two onto different RTP ports -- meaning their audio sessions would
        have to be torn down and reopened despite their settings not having
        changed, which is exactly what this module exists to avoid.
        """
        taken = set(self._ports.values())
        for port in range(RTP_BASE_PORT, RTP_BASE_PORT + MAX_SCANNERS):
            if port not in taken:
                self._ports[scanner_id] = port
                return port
        raise RuntimeError(f"no free RTP port for {scanner_id!r} (max {MAX_SCANNERS} scanners)")
