"""Tests for weather.py -- acting on the scanner sitting in a weather alert.

Two things here are worth pinning down, and both fail silently in the field:

* the alert is an *edge*, not a level. The status stream repeats the same
  alert state once a second, and a rule that fired on every poll would send
  a hundred notifications for one alert.
* the key pressed to leave the alert is read off the scanner's own soft-key
  labels. Get that wrong and the add-on presses some other key on the alert
  screen -- which on this hardware can hold or avoid a channel, leaving the
  scanner in a stranger state than the alert did.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import reception  # noqa: E402
import weather  # noqa: E402
from weather import WeatherWatch  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds=1.0):
        self.now += seconds
        return self.now


class FakeConnection:
    def __init__(self, scanner_id="home", response="KEY,OK"):
        self.id = scanner_id
        self.response = response
        self.commands: list[str] = []
        # What a re-read finds. The real refresh_gsi merges a fresh GSI into
        # last_status and hands it to the listeners; here the poll helper
        # keeps it in step, and `moves_to` is how a test says "the scanner
        # went somewhere else between choosing the key and sending it".
        self.last_status: dict = {}
        self.moves_to: dict | None = None
        self.refreshes = 0

    async def send_command(self, command, **kwargs):
        self.commands.append(command)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def refresh_display(self):
        # Each refresh updates only its own half of last_status, exactly as
        # protocol.py does -- STS merges the display keys over the top and
        # leaves "gsi" alone, GSI replaces "gsi" and touches nothing else.
        # Keeping them apart here is the point: a fake that swapped the whole
        # status on either call could not tell "re-read the screen" from
        # "re-read half the screen", which is the bug these tests are for.
        self.refreshes += 1
        if self.moves_to is not None:
            self.last_status = {
                **self.last_status,
                **{k: v for k, v in self.moves_to.items() if k != "gsi"},
            }
        return {k: v for k, v in self.last_status.items() if k != "gsi"}

    async def refresh_gsi(self):
        self.refreshes += 1
        if self.moves_to is not None:
            self.last_status = {**self.last_status, "gsi": self.moves_to.get("gsi", {})}
        return self.last_status.get("gsi", {})


class RecordingEngine:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def on_call(self, event, record):
        self.calls.append((event, record))


def status(
    *, alert=False, monitoring=False, soft_keys=None, same="Alert Only", screen=None, popup=None
):
    """A merged status dict shaped like the real one: GSI carries WxMode only
    while the scanner is in weather mode at all, STS carries the soft keys.

    `screen` is ScannerInfo/@V_Screen -- "wx_alert" while the scanner is
    parked on the alert screen, whatever it is showing otherwise. It defaults
    to matching the alert, which is the usual case; passing it explicitly is
    how the "alert gone, screen still up" state is written.

    `popup` is the key codes a modal popup offers. A popup takes the soft-key
    row with it -- the real STS sends no label row at all while one is up --
    so passing it also empties `soft_keys` unless a test says otherwise.
    """
    gsi: dict = {"mode": "WX Hold" if (alert or monitoring) else "Scan Mode"}
    if alert or monitoring:
        gsi["WxMode"] = {"Mode": "Weather Alert" if alert else "Monitor Weather", "SAME": same}
    gsi["v_screen"] = screen if screen is not None else ("wx_alert" if alert else "conventional_scan")
    if popup is not None:
        gsi["view_description"] = {
            "PopupScreen": {
                "Text": "Warning WX\rWX Alert        \r\r",
                "buttons": [{"Text": f'"{code}" (OK)', "KeyCode": code} for code in popup],
            }
        }
        if soft_keys is None:
            soft_keys = []
    return {"gsi": gsi, "soft_keys": soft_keys if soft_keys is not None else ["SYSTEM", "DEPT", "CHANNEL"]}


ALERT_SCREEN = ["HOLD", "WX ALERT", "TO SCAN"]

# The ordinary scan screen, and the reason a stray press is not a no-op:
# these three labels are System / Dept / Channel *hold*.
SCAN_SCREEN = ["SYSTEM", "DEPT", "CHANNEL"]

# What the live capture shows once the popup is dismissed: "to Scan" in the
# first column, the middle key nothing but glyphs, "RESUME" in the third.
WX_HOLD_SCREEN = ["to Scan", "", "RESUME"]


class TestFindScanKey(unittest.TestCase):
    def test_the_labelled_soft_key_is_found_by_position(self):
        self.assertEqual(weather.find_scan_key(status(soft_keys=ALERT_SCREEN)), "soft3")
        self.assertEqual(
            weather.find_scan_key(status(soft_keys=["To Scan", "HOLD", "WX"])), "soft1"
        )

    def test_a_screen_without_one_offers_nothing(self):
        self.assertIsNone(weather.find_scan_key(status()))
        self.assertIsNone(weather.find_scan_key({}))


class TestAlertEvents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.engine = RecordingEngine()
        self.conn = FakeConnection()
        self.watch = WeatherWatch(self.engine, clock=self.clock)
        self.watch.attach(self.conn)

    async def test_an_alert_fires_once_however_many_polls_it_spans(self):
        for _ in range(5):
            self.watch.status_callback("home", status(alert=True, soft_keys=ALERT_SCREEN))
            self.clock.tick()
        self.assertEqual([event for event, _ in self.engine.calls], ["wx_alert"])

    async def test_the_record_carries_the_same_group_and_a_label(self):
        self.watch.status_callback("home", status(alert=True, same="EAS: TOR"))
        _event, record = self.engine.calls[0]
        self.assertEqual(record["scanner_id"], "home")
        self.assertEqual(record["wx_same"], "EAS: TOR")
        self.assertTrue(record["wx_alert"])
        self.assertEqual(record["label"], "Weather alert")

    async def test_clearing_fires_the_other_edge_and_rearms(self):
        self.watch.status_callback("home", status(alert=True))
        self.clock.tick()
        self.watch.status_callback("home", status(monitoring=True))
        self.clock.tick()
        self.watch.status_callback("home", status(alert=True))
        self.assertEqual(
            [event for event, _ in self.engine.calls], ["wx_alert", "wx_clear", "wx_alert"]
        )

    async def test_monitoring_the_weather_is_not_an_alert(self):
        self.watch.status_callback("home", status(monitoring=True))
        self.assertEqual(self.engine.calls, [])

    async def test_a_detached_scanner_is_ignored(self):
        self.watch.detach("home")
        self.watch.status_callback("home", status(alert=True))
        self.assertEqual(self.engine.calls, [])


class TestReturnToScan(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.conn = FakeConnection()
        self.watch = WeatherWatch(None, clock=self.clock)
        self.watch.attach(self.conn)

    async def _poll(self, **kwargs):
        st = status(**kwargs)
        # The press re-reads the scanner before it sends, so what a re-read
        # would find has to track what was polled.
        self.conn.last_status = st
        self.watch.status_callback("home", st)
        # The press is spawned as a task so a slow scanner can't stall the
        # poll callback; let it run before asserting on what was sent.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_nothing_is_pressed_before_the_configured_wait(self):
        self.watch.configure("home", return_after_s=30, fallback_key="")
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(29)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, [])

    async def test_the_labelled_soft_key_is_pressed_once_the_wait_is_up(self):
        self.watch.configure("home", return_after_s=30, fallback_key="")
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(30)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, ["KEY,C,P"])

    async def test_it_is_not_pressed_again_on_the_very_next_poll(self):
        self.watch.configure("home", return_after_s=30, fallback_key="")
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(30)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(1)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, ["KEY,C,P"])

    async def test_a_press_that_did_not_take_is_retried_a_bounded_number_of_times(self):
        self.watch.configure("home", return_after_s=10, fallback_key="")
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        for _ in range(20):
            self.clock.tick(10)
            await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(len(self.conn.commands), weather.MAX_ATTEMPTS)

    async def test_off_by_default(self):
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(3600)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, [])

    async def test_an_unlabelled_screen_presses_the_fallback_key_if_there_is_one(self):
        self.watch.configure("home", return_after_s=10, fallback_key="soft1")
        await self._poll(alert=True, soft_keys=["", "", ""])
        self.clock.tick(10)
        await self._poll(alert=True, soft_keys=["", "", ""])
        self.assertEqual(self.conn.commands, ["KEY,A,P"])

    async def test_an_unlabelled_screen_with_no_fallback_presses_nothing(self):
        self.watch.configure("home", return_after_s=10, fallback_key="")
        await self._poll(alert=True, soft_keys=["", "", ""])
        self.clock.tick(10)
        await self._poll(alert=True, soft_keys=["", "", ""])
        self.clock.tick(10)
        await self._poll(alert=True, soft_keys=["", "", ""])
        self.assertEqual(self.conn.commands, [])

    async def test_a_second_alert_gets_a_fresh_set_of_attempts(self):
        self.watch.configure("home", return_after_s=10, fallback_key="")
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(10)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        await self._poll()  # cleared
        self.clock.tick(1)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(10)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, ["KEY,C,P", "KEY,C,P"])

    async def test_a_scanner_that_will_not_answer_does_not_stall_the_poll(self):
        self.conn.response = RuntimeError("no response to 'KEY,C,P'")
        self.watch.configure("home", return_after_s=10, fallback_key="")
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(10)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, ["KEY,C,P"])


class TestStillParkedAfterTheAlertClears(unittest.IsolatedAsyncioTestCase):
    """The alert screen outlives the alert, and it is the screen that stops
    the scanner scanning.

    A short alert -- one that clears before the configured wait is up -- used
    to call the press off with the scanner still sitting on the alert screen,
    which is the exact failure ("a scanner that quietly stopped scanning hours
    ago") this module exists to prevent. The press follows V_Screen now, and
    only the *events* follow the alert.
    """

    def setUp(self):
        self.clock = FakeClock()
        self.engine = RecordingEngine()
        self.conn = FakeConnection()
        self.watch = WeatherWatch(self.engine, clock=self.clock)
        self.watch.attach(self.conn)
        self.watch.configure("home", return_after_s=30, fallback_key="")

    async def _poll(self, **kwargs):
        st = status(**kwargs)
        self.conn.last_status = st
        self.watch.status_callback("home", st)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_the_press_still_happens_once_the_wait_is_up(self):
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(10)
        # The alert is over, but the scanner is still on its screen.
        await self._poll(screen="wx_alert", soft_keys=ALERT_SCREEN)
        self.clock.tick(20)
        await self._poll(screen="wx_alert", soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, ["KEY,C,P"])

    async def test_the_wait_is_measured_from_parking_not_from_the_alert_clearing(self):
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(29)
        await self._poll(screen="wx_alert", soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, [])

    async def test_the_clearing_event_still_fires_on_the_alert_not_the_screen(self):
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(1)
        await self._poll(screen="wx_alert", soft_keys=ALERT_SCREEN)
        self.assertEqual([event for event, _ in self.engine.calls], ["wx_alert", "wx_clear"])

    async def test_leaving_the_screen_rearms_the_attempts(self):
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(30)
        await self._poll(screen="wx_alert", soft_keys=ALERT_SCREEN)
        await self._poll()  # scanning again
        self.clock.tick(1)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(30)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, ["KEY,C,P", "KEY,C,P"])

    async def test_monitoring_the_weather_is_not_being_parked(self):
        # Sitting on a weather channel on purpose is not a scanner to rescue.
        for _ in range(5):
            await self._poll(monitoring=True, soft_keys=ALERT_SCREEN)
            self.clock.tick(30)
        self.assertEqual(self.conn.commands, [])


class TestTheDualWatchDip(unittest.IsolatedAsyncioTestCase):
    """A scanner that is scanning is not a scanner to rescue, alert or not.

    `DualWatch WX="Priority"` dips into the weather channel every few seconds
    and comes back on its own, and an alert that is still current leaves
    `WxMode` in GSI across the dip. `wx_parked` used to be
    `V_Screen == "wx_alert" or wx_alert`, so it stayed true on
    `V_Screen="conventional_scan"` for as long as the alert ran -- and a
    module whose whole job is pressing keys at parked scanners then had every
    reason to press one at a scanner that was scanning perfectly well. On
    that screen soft1/soft2/soft3 are System/Dept/Channel hold. Found in the
    field with the scanner scanning one CB department for eleven hours, the
    display reading "Scanning...", and no receptions recorded since.
    """

    def setUp(self):
        self.clock = FakeClock()
        self.conn = FakeConnection()
        self.watch = WeatherWatch(None, clock=self.clock)
        self.watch.attach(self.conn)
        self.watch.configure("home", return_after_s=30, fallback_key="")

    async def _poll(self, **kwargs):
        st = status(**kwargs)
        self.conn.last_status = st
        self.watch.status_callback("home", st)
        for _ in range(3):
            await asyncio.sleep(0)

    def test_an_alert_on_the_scan_screen_is_an_alert_but_not_a_park(self):
        snapshot = reception.extract(status(alert=True, screen="conventional_scan"))
        self.assertTrue(snapshot["wx_alert"])
        self.assertFalse(snapshot["wx_parked"])

    def test_the_alert_only_stands_in_when_there_is_no_v_screen_at_all(self):
        st = status(alert=True)
        del st["gsi"]["v_screen"]
        self.assertTrue(reception.extract(st)["wx_parked"])

    async def test_nothing_is_pressed_at_a_scanner_that_is_still_scanning(self):
        for _ in range(5):
            await self._poll(alert=True, screen="conventional_scan", soft_keys=SCAN_SCREEN)
            self.clock.tick(30)
        self.assertEqual(self.conn.commands, [])

    async def test_the_alert_itself_still_fires_while_it_is_scanning(self):
        # The dip is not a reason to stop reporting the alert -- an
        # automation means "an alert came in" by this, wherever the scanner
        # happens to be pointed.
        engine = RecordingEngine()
        watch = WeatherWatch(engine, clock=self.clock)
        watch.attach(self.conn)
        watch.configure("home", return_after_s=30, fallback_key="")
        watch.status_callback("home", status(alert=True, screen="conventional_scan"))
        self.assertEqual([event for event, _ in engine.calls], ["wx_alert"])


class TestFindWayOut(unittest.TestCase):
    """One step towards scanning, which is not always the same step."""

    def test_a_labelled_soft_key_wins_whenever_there_is_one(self):
        self.assertEqual(weather.find_way_out(status(soft_keys=ALERT_SCREEN), {}, 0), "soft3")

    def test_a_popup_that_hid_the_labels_gets_the_key_the_popup_named(self):
        st = status(alert=True, popup=["E"])
        snapshot = reception.extract(st)
        self.assertEqual(st["soft_keys"], [])
        self.assertEqual(weather.find_way_out(st, snapshot, 0), "E")

    def test_the_popup_gets_its_own_keys_before_the_guessed_position(self):
        # Order matters for damage, not just for odds. A key the popup named
        # is a specific one and does nothing much if it lands on the scan
        # screen; POPUP_DISMISS_KEY is a soft-key *position*, and that
        # position on the scan screen holds the system.
        st = status(alert=True, popup=["E", "M"])
        snapshot = reception.extract(st)
        self.assertEqual(weather.find_way_out(st, snapshot, 0), "E")
        self.assertEqual(weather.find_way_out(st, snapshot, 1), "M")

    def test_a_popup_that_ignored_them_all_falls_back_to_the_dismiss_key(self):
        st = status(alert=True, popup=["E"])
        snapshot = reception.extract(st)
        self.assertEqual(weather.find_way_out(st, snapshot, 1), weather.POPUP_DISMISS_KEY)
        # And it stays there rather than running off the end of the list.
        self.assertEqual(weather.find_way_out(st, snapshot, 2), weather.POPUP_DISMISS_KEY)

    def test_no_labels_and_no_popup_is_still_nothing_to_press(self):
        self.assertIsNone(weather.find_way_out(status(soft_keys=["", "", ""]), {}, 0))


class TestThePopupScreen(unittest.IsolatedAsyncioTestCase):
    """The screen an alert actually opens with, and the one nothing rescued.

    A real alert puts a modal popup over the WX Hold screen. The popup takes
    the soft-key row with it, so there were no labels to read, find_scan_key
    returned None and the add-on gave up -- leaving a parked scanner, which
    is the failure this module exists to prevent. Dismissing the popup is
    also not the end of it: underneath is the WX Hold screen, still held on
    the weather channel, where a second and different press is what resumes.
    """

    def setUp(self):
        self.clock = FakeClock()
        self.conn = FakeConnection()
        self.watch = WeatherWatch(None, clock=self.clock)
        self.watch.attach(self.conn)
        self.watch.configure("home", return_after_s=30, fallback_key="")

    async def _poll(self, **kwargs):
        st = status(**kwargs)
        self.conn.last_status = st
        self.watch.status_callback("home", st)
        for _ in range(3):
            await asyncio.sleep(0)

    async def test_the_popup_is_dismissed_even_with_no_labels_to_read(self):
        await self._poll(alert=True, popup=["E"])
        self.clock.tick(30)
        await self._poll(alert=True, popup=["E"])
        self.assertEqual(self.conn.commands, ["KEY,E,P"])  # the key the popup named

    async def test_dismissing_it_is_followed_by_the_press_that_actually_resumes(self):
        await self._poll(alert=True, popup=["E"])
        self.clock.tick(30)
        await self._poll(alert=True, popup=["E"])
        # The popup is gone and the screen underneath has the labels. The
        # scanner is still parked: WX Hold on the weather channel, and the
        # alert itself has already cleared, exactly as captured live.
        await self._poll(monitoring=True, screen="wx_alert", soft_keys=WX_HOLD_SCREEN)
        self.assertEqual(self.conn.commands, ["KEY,E,P", "KEY,A,P"])

    async def test_the_second_press_does_not_wait_for_the_whole_countdown_again(self):
        await self._poll(alert=True, popup=["E"])
        self.clock.tick(30)
        await self._poll(alert=True, popup=["E"])
        # No tick at all: the wait is there to let a person read the alert,
        # and reaching the next screen means that grace is already spent.
        await self._poll(monitoring=True, screen="wx_alert", soft_keys=WX_HOLD_SCREEN)
        self.assertEqual(len(self.conn.commands), 2)

    async def test_a_screen_change_is_progress_not_a_failed_attempt(self):
        # Three attempts is the cap *per screen*. A chain of screens that
        # each take a press must not run into it.
        await self._poll(alert=True, popup=["E"])
        for _ in range(weather.MAX_ATTEMPTS):
            self.clock.tick(30)
            await self._poll(alert=True, popup=["E"])
        self.assertEqual(len(self.conn.commands), weather.MAX_ATTEMPTS)
        await self._poll(monitoring=True, screen="wx_alert", soft_keys=WX_HOLD_SCREEN)
        self.assertEqual(self.conn.commands[-1], "KEY,A,P")
        self.assertEqual(len(self.conn.commands), weather.MAX_ATTEMPTS + 1)

    async def test_two_screens_that_lead_to_each_other_stop_keying_the_panel(self):
        await self._poll(alert=True, popup=["E"])
        self.clock.tick(30)
        for _ in range(20):
            await self._poll(alert=True, popup=["E"])
            self.clock.tick(30)
            await self._poll(monitoring=True, screen="wx_alert", soft_keys=WX_HOLD_SCREEN)
            self.clock.tick(30)
        self.assertEqual(len(self.conn.commands), weather.MAX_PRESSES)

    async def test_getting_back_to_scanning_clears_the_press_budget(self):
        await self._poll(alert=True, popup=["E"])
        self.clock.tick(30)
        for _ in range(20):
            await self._poll(alert=True, popup=["E"])
            self.clock.tick(30)
            await self._poll(monitoring=True, screen="wx_alert", soft_keys=WX_HOLD_SCREEN)
            self.clock.tick(30)
        await self._poll()  # scanning again
        self.clock.tick(1)
        await self._poll(alert=True, popup=["E"])
        self.clock.tick(30)
        await self._poll(alert=True, popup=["E"])
        self.assertEqual(len(self.conn.commands), weather.MAX_PRESSES + 1)


class TestThePressIsAimedAtAScreen(unittest.IsolatedAsyncioTestCase):
    """A key chosen for one screen must not land on another.

    This scanner moves on its own -- WX Priority dual-watch takes it off the
    weather screen after a few seconds with nobody pressing anything. A
    return-to-scan press that arrives late lands on the scan screen, where
    soft1 is SYSTEM and it holds the system instead: it stops the very
    scanning it was sent to restore. Observed for real while capturing the
    fixtures, which is why the press re-reads before it sends.
    """

    def setUp(self):
        self.clock = FakeClock()
        self.conn = FakeConnection()
        self.watch = WeatherWatch(None, clock=self.clock)
        self.watch.attach(self.conn)
        self.watch.configure("home", return_after_s=30, fallback_key="")

    async def _poll(self, **kwargs):
        st = status(**kwargs)
        self.conn.last_status = st
        self.watch.status_callback("home", st)
        for _ in range(3):
            await asyncio.sleep(0)

    async def test_nothing_is_sent_if_the_scanner_left_the_screen_first(self):
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(30)
        # It goes back to scanning between the poll that chose the key and
        # the press being delivered.
        self.conn.moves_to = status()
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, [])
        # STS and GSI both, because the screen is identified by what it
        # offers and the two halves of that come from the two commands.
        self.assertEqual(self.conn.refreshes, 2)

    async def test_nothing_is_sent_if_the_weather_screen_changed_underneath(self):
        await self._poll(alert=True, popup=["E"])
        self.clock.tick(30)
        # Still parked, but the popup went by itself -- the key chosen for
        # the popup is not the key for what is there now.
        self.conn.moves_to = status(monitoring=True, screen="wx_alert", soft_keys=WX_HOLD_SCREEN)
        await self._poll(alert=True, popup=["E"])
        self.assertEqual(self.conn.commands, [])

    async def test_nothing_is_sent_when_only_the_labels_say_it_moved(self):
        # The two halves of a screen come from two commands, and only one of
        # them can tell the scan screen from a weather one. Here GSI is
        # unchanged -- same alert, same popup -- and STS is what shows the
        # scan screen's own soft keys arriving underneath. Re-reading GSI
        # alone saw nothing wrong and sent soft1, which on that screen is
        # SYSTEM.
        await self._poll(alert=True, popup=["E"])
        self.clock.tick(30)
        self.conn.moves_to = status(alert=True, popup=["E"], soft_keys=SCAN_SCREEN)
        await self._poll(alert=True, popup=["E"])
        self.assertEqual(self.conn.commands, [])

    async def test_a_scanner_that_cannot_be_re_read_is_not_pressed_blind(self):
        async def refuse():
            raise RuntimeError("GSI timed out")

        self.conn.refresh_gsi = refuse
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(30)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, [])

    async def test_a_flaky_re_read_does_not_use_up_the_attempts(self):
        # GSI is much flakier than STS on this hardware, and a press that was
        # never sent must not spend one of the three. Otherwise a run of
        # timeouts exhausts the budget without a key ever going out, and the
        # scanner stays parked -- the failure the whole module is here for.
        failures = []

        async def flaky():
            if len(failures) < weather.MAX_ATTEMPTS * 2:
                failures.append(1)
                raise RuntimeError("GSI timed out")
            self.conn.refreshes += 1

        self.conn.refresh_gsi = flaky
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        for _ in range(weather.MAX_ATTEMPTS * 2):
            self.clock.tick(30)
            await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, [])
        # GSI comes back; the press it was holding still happens.
        self.clock.tick(30)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, ["KEY,C,P"])

    async def test_a_re_read_that_keeps_failing_still_paces_itself(self):
        async def refuse():
            raise RuntimeError("GSI timed out")

        self.conn.refresh_gsi = refuse
        tries = 0

        async def counting():
            nonlocal tries
            tries += 1
            await refuse()

        self.conn.refresh_gsi = counting
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        # Twenty polls inside one wait must not be twenty re-reads.
        for _ in range(20):
            self.clock.tick(1)
            await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(tries, 0)
        self.clock.tick(30)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(tries, 1)

    async def test_the_press_still_goes_when_the_screen_is_where_it_was(self):
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.clock.tick(30)
        await self._poll(alert=True, soft_keys=ALERT_SCREEN)
        self.assertEqual(self.conn.commands, ["KEY,C,P"])


class TestAgainstTheLiveAlertCapture(unittest.TestCase):
    """The real thing, off a real SDS200 mid-alert (fixtures.py)."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import fixtures
        import protocol
        import xml_lists

        self.parsed = protocol.parse_sts(
            fixtures.STS_RESPONSE_WEATHER_ALERT.decode("ascii", errors="replace")
        )
        xml = fixtures.GSI_RESPONSE_WEATHER_ALERT.decode("ascii").split("<XML>,", 1)[1]
        self.gsi = xml_lists.gsi_to_dict(ET.fromstring(xml.replace("\r", "")))

    def test_the_way_back_to_scanning_is_the_first_soft_key(self):
        self.assertEqual(self.parsed["soft_keys"], ["to Scan", "", "RESUME"])
        self.assertEqual(weather.find_scan_key(self.parsed), "soft1")

    def test_the_alert_reads_as_an_alert_and_as_parked(self):
        snapshot = reception.extract({**self.parsed, "gsi": self.gsi})
        self.assertTrue(snapshot["wx_alert"])
        self.assertTrue(snapshot["wx_parked"])
        self.assertEqual(snapshot["wx_mode"], "Weather Alert")
        self.assertEqual(snapshot["wx_same"], "Alert Only")
        self.assertEqual(snapshot["v_screen"], "wx_alert")


class TestAgainstTheLiveWeatherScreens(unittest.TestCase):
    """All three weather screens, off a real SDS200 (fixtures.py).

    They share Mode="WX Hold"/V_Screen="wx_alert" (the third is "WX Scan")
    and need three different things done to them, which is the whole reason
    the way out is read off the screen instead of configured.
    """

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import fixtures
        import protocol
        import xml_lists

        def load(sts, gsi):
            parsed = protocol.parse_sts(sts.decode("ascii", errors="replace"))
            xml = gsi.decode("ascii").split("<XML>,", 1)[1]
            return {**parsed, "gsi": xml_lists.gsi_to_dict(ET.fromstring(xml.replace("\r", "")))}

        self.popup = load(
            fixtures.STS_RESPONSE_WEATHER_ALERT_POPUP,
            fixtures.GSI_RESPONSE_WEATHER_ALERT_POPUP,
        )
        self.wx_hold = load(
            fixtures.STS_RESPONSE_WX_HOLD_AFTER_POPUP,
            fixtures.GSI_RESPONSE_WX_HOLD_AFTER_POPUP,
        )
        self.wx_scan = load(fixtures.STS_RESPONSE_WX_SCAN, fixtures.GSI_RESPONSE_WX_SCAN)

    def test_the_popup_sends_no_soft_key_row_at_all(self):
        # Not "empty labels" -- no mask row in the whole of STS, which is why
        # there was nothing for find_scan_key to work with.
        self.assertEqual(self.popup["soft_keys"], [])
        self.assertIsNone(weather.find_scan_key(self.popup))

    def test_the_popup_names_its_own_way_out(self):
        snapshot = reception.extract(self.popup)
        self.assertEqual(snapshot["popup_keys"], ["E"])
        self.assertEqual(weather.find_way_out(self.popup, snapshot, 0), "E")

    def test_the_popup_is_an_alert_and_is_parked(self):
        snapshot = reception.extract(self.popup)
        self.assertTrue(snapshot["wx_alert"])
        self.assertTrue(snapshot["wx_parked"])
        self.assertEqual(snapshot["wx_same"], "Alert Only")

    def test_dismissing_it_leaves_a_scanner_that_is_still_parked(self):
        # The alert has gone; the scanner has not gone back to scanning.
        snapshot = reception.extract(self.wx_hold)
        self.assertFalse(snapshot["wx_alert"])
        self.assertTrue(snapshot["wx_parked"])
        self.assertEqual(snapshot["wx_mode"], "Monitor Weather")
        self.assertEqual(self.wx_hold["gsi"]["WxChannel"]["Hold"], "On")

    def test_and_the_screen_underneath_has_the_way_out(self):
        self.assertEqual(self.wx_hold["soft_keys"], ["to Scan", "", "RESUME"])
        self.assertEqual(weather.find_scan_key(self.wx_hold), "soft1")

    def test_wx_scan_is_a_third_screen_and_also_counts_as_parked(self):
        snapshot = reception.extract(self.wx_scan)
        self.assertTrue(snapshot["wx_parked"])
        self.assertEqual(snapshot["scan_mode"], "WX Scan")
        # Scanning weather channels is still not scanning the systems the
        # user asked for, and "to Scan" is still where it was.
        self.assertEqual(self.wx_scan["soft_keys"], ["to Scan", "", "HOLD"])
        self.assertEqual(weather.find_scan_key(self.wx_scan), "soft1")

    def test_the_three_screens_are_told_apart(self):
        signature = weather.screen_signature
        screens = [
            signature(s, reception.extract(s)) for s in (self.popup, self.wx_hold, self.wx_scan)
        ]
        self.assertEqual(len(set(screens)), 3)


class TestTheWholeRescueOnRealBytes(unittest.IsolatedAsyncioTestCase):
    """The chain end to end, driven by the captured screens themselves.

    Everything else here builds its status dicts by hand. This one replays
    the three real screens in the order the hardware produced them --
    popup, then the WX Hold screen it uncovers, then scanning -- through the
    same parsers the add-on runs, and asserts the scanner gets out.
    """

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import fixtures
        import protocol
        import xml_lists

        def load(sts, gsi):
            parsed = protocol.parse_sts(sts.decode("ascii", errors="replace"))
            xml = gsi.decode("ascii").split("<XML>,", 1)[1]
            return {**parsed, "gsi": xml_lists.gsi_to_dict(ET.fromstring(xml.replace("\r", "")))}

        self.popup = load(
            fixtures.STS_RESPONSE_WEATHER_ALERT_POPUP,
            fixtures.GSI_RESPONSE_WEATHER_ALERT_POPUP,
        )
        self.wx_hold = load(
            fixtures.STS_RESPONSE_WX_HOLD_AFTER_POPUP,
            fixtures.GSI_RESPONSE_WX_HOLD_AFTER_POPUP,
        )
        self.clock = FakeClock()
        self.engine = RecordingEngine()
        self.conn = FakeConnection()
        self.watch = WeatherWatch(self.engine, clock=self.clock)
        self.watch.attach(self.conn)
        self.watch.configure("home", return_after_s=30, fallback_key="")

    async def _poll(self, st):
        self.conn.last_status = st
        self.watch.status_callback("home", st)
        for _ in range(3):
            await asyncio.sleep(0)

    async def test_two_presses_get_it_off_both_screens(self):
        await self._poll(self.popup)
        self.clock.tick(30)
        await self._poll(self.popup)
        await self._poll(self.wx_hold)
        # The "E" the popup named, then the "to Scan" it uncovers, which on
        # this capture is soft1.
        self.assertEqual(self.conn.commands, ["KEY,E,P", "KEY,A,P"])

    async def test_the_alert_fires_once_and_clears_when_the_popup_goes(self):
        await self._poll(self.popup)
        self.clock.tick(30)
        await self._poll(self.popup)
        await self._poll(self.wx_hold)
        self.assertEqual([event for event, _ in self.engine.calls], ["wx_alert", "wx_clear"])

    async def test_nothing_is_pressed_while_the_wait_is_still_running(self):
        await self._poll(self.popup)
        self.clock.tick(29)
        await self._poll(self.popup)
        self.assertEqual(self.conn.commands, [])


if __name__ == "__main__":
    unittest.main()
