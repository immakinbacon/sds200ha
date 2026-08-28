"""Entrypoint: loads the add-on's own settings, brings up the configured
scanners, and serves the REST/WS API plus the settings web UI.

Settings live in /data/config.json and are edited in the add-on's own UI
(Home Assistant ingress -> the add-on page's "Open Web UI"), not in
Supervisor's Configuration tab -- see config_store.py for why, and for the
one-time migration off /data/options.json.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiohttp import web

import api
import settings_api
import stt
from avoids import AvoidLog
from config_store import ConfigStore
from history import ReceiveHistory
from manager import ScannerManager, set_log_level
from stuck import StuckWatch
from transcribe import Transcriber
from triggers import TriggerEngine
from weather import WeatherWatch
from ws import StatusHub

HTTP_PORT = 8000


async def main() -> None:
    logging.basicConfig(
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logger = logging.getLogger("main")

    store = ConfigStore()
    config = store.load()
    set_log_level(config["log_level"])

    status_hub = StatusHub()
    history = ReceiveHistory(
        max_records=config["history"]["max_records"],
        retention_days=config["history"]["retention_days"],
    )
    trigger_engine = TriggerEngine(config["triggers"])
    # Order matters: the engine is registered first so a rule fires from the
    # same callback that recorded the call, before the WS fan-out awaits
    # anything. Both are plain listeners -- neither knows about the other.
    history.add_listener(trigger_engine.on_call)
    history.add_listener(status_hub.reception_callback)
    # Weather alerts reach the same engine, but off the status stream rather
    # than the receive history -- an alert is a state the scanner is *in*, not
    # a call it heard.
    weather = WeatherWatch(trigger_engine)
    # Watches for the opposite failure to weather.py's: not a scanner parked
    # on a screen, but one still scanning and reporting itself as scanning
    # while covering a fraction of its lists. It reads both streams -- the
    # status one for the hold flags, the receive history for "when did this
    # last hear anything" -- and presses nothing, ever. See stuck.py.
    stuck = StuckWatch(trigger_engine)
    history.add_listener(stuck.on_call)

    # Speech-to-text. The client is None until an address is configured,
    # which is the default -- the tap still attaches to every scanner, but
    # nothing is queued and nothing is sent. See transcribe.py.
    transcriber = Transcriber(history=history)
    transcriber.set_client(stt.build(config["transcribe"]))
    # Clips whose call is no longer in the log, from a history cleared by a
    # version that left them behind or a database removed by hand. They have
    # to go before anything new is stored: they hold the ids the next calls
    # will be given. See Transcriber.reconcile_clips.
    transcriber.reconcile_clips()
    # Rows left mid-transcription by the last run. The queue they were
    # waiting on was in memory and went with it.
    transcriber.settle_pending()
    # Registered after the WS fan-out so a call that produced no audio is
    # marked as such before anything asks why its transcript is missing.
    history.add_listener(transcriber.on_call)
    transcriber.start()

    manager = ScannerManager(
        status_hub,
        history=history,
        trigger_engine=trigger_engine,
        weather=weather,
        stuck=stuck,
        transcriber=transcriber,
    )
    await manager.apply(config)

    # The manager's dicts are passed by reference and mutated in place as
    # settings change, so these routes keep seeing the current scanners
    # without the app being rebuilt on every save.
    app = api.create_app(manager.scanners, manager.audio_bridges, status_hub, avoids=AvoidLog())
    settings_api.add_routes(app, store, manager, history, trigger_engine)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    logger.info(
        "SDS200 bridge listening on :%d (%d scanner(s) configured)",
        HTTP_PORT, len(manager.scanners),
    )
    if not manager.scanners:
        logger.warning(
            "no scanners are configured -- open the add-on's web UI (Open Web UI on the add-on "
            "page) to add one. The Configuration tab is no longer read."
        )

    await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
