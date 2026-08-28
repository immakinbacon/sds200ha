"""Tests for AudioBridge._await_control(): the auto-reboot path that covers
a scanner whose *control* interface has gone dark.

Worth testing rather than leaving to a live trial, because every branch here
only ever runs when the hardware is already broken -- the condition it
recovers from (2026-07-25, see docs/protocol-notes.md) took a manual
power-cycle to clear and can't be reproduced on demand, so "we'll see it
work next time it breaks" would mean shipping untested code that cuts power
to a production switch port.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests

No external dependencies, per README -- `aiohttp` is stubbed below, since
audio_bridge imports mikrotik which imports it, but nothing in these tests
touches it.
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

# Stub aiohttp only if it genuinely isn't installed. An unconditional
# setdefault() here was enough to break *other* test modules in the same
# `unittest discover` run: the stub is an empty module, so a later module
# that does use aiohttp for real (tests/test_triggers.py) found no
# ClientError on it and failed -- but only when run as part of the suite,
# never on its own.
try:  # noqa: SIM105
    import aiohttp  # noqa: F401
except ImportError:
    sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

import audio_bridge  # noqa: E402
from audio_bridge import AudioBridge  # noqa: E402
from mikrotik import MikrotikApiError  # noqa: E402


class FakeControl:
    """The two ScannerConnection methods _await_control() actually uses."""

    def __init__(self, reachable: bool = False):
        self._event = asyncio.Event()
        if reachable:
            self._event.set()

    def is_reachable(self) -> bool:
        return self._event.is_set()

    async def wait_until_reachable(self) -> None:
        await self._event.wait()

    def come_back(self) -> None:
        self._event.set()


class FakePoeReset:
    def __init__(self, *, recovers: bool = True, control: FakeControl | None = None, exc=None):
        self.interface = "ether12"
        self.url = "http://router/rest/interface/ethernet/poe/power-cycle"
        self.calls = 0
        self._recovers = recovers
        self._control = control
        self._exc = exc

    async def power_cycle(self) -> None:
        self.calls += 1
        if self._exc:
            raise self._exc
        if self._recovers and self._control:
            self._control.come_back()


class AwaitControlTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Real values are minutes long; the logic under test is the branching,
        # not the durations, so shrink them for the duration of each test.
        self._originals = (
            audio_bridge.CONTROL_REBOOT_AFTER,
            audio_bridge.REBOOT_COOLDOWN,
        )
        audio_bridge.CONTROL_REBOOT_AFTER = 0.02
        audio_bridge.REBOOT_COOLDOWN = 0.01

    def tearDown(self):
        (audio_bridge.CONTROL_REBOOT_AFTER, audio_bridge.REBOOT_COOLDOWN) = self._originals

    def _bridge(self, control, poe_reset=None, *, enabled=True) -> AudioBridge:
        return AudioBridge(
            scanner_id="test",
            host="198.51.100.1",
            rtsp_port=554,
            rtp_client_port=5004,
            control=control,
            poe_reset=poe_reset,
            auto_reboot_on_control_failure=enabled,
        )

    async def test_returns_immediately_when_control_is_up(self):
        control = FakeControl(reachable=True)
        poe = FakePoeReset(control=control)
        await asyncio.wait_for(self._bridge(control, poe)._await_control(), 1)
        self.assertEqual(poe.calls, 0, "a reachable scanner must never be power-cycled")

    async def test_power_cycles_a_dark_control_interface(self):
        control = FakeControl()
        poe = FakePoeReset(control=control)  # recovers on the first power-cycle
        await asyncio.wait_for(self._bridge(control, poe)._await_control(), 1)
        self.assertEqual(poe.calls, 1)

    async def test_no_reboot_while_the_toggle_is_off(self):
        control = FakeControl()
        poe = FakePoeReset(control=control)
        bridge = self._bridge(control, poe, enabled=False)
        task = asyncio.create_task(bridge._await_control())
        with self.assertRaises(asyncio.TimeoutError):
            # Must keep waiting, not power-cycle: this is opt-in.
            await asyncio.wait_for(asyncio.shield(task), 0.1)
        self.assertEqual(poe.calls, 0)

        control.come_back()  # ...and still return once the scanner recovers
        await asyncio.wait_for(task, 1)

    async def test_no_reboot_without_a_poe_reset_configured(self):
        control = FakeControl()
        bridge = self._bridge(control, None)
        task = asyncio.create_task(bridge._await_control())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.1)
        control.come_back()
        await asyncio.wait_for(task, 1)

    async def test_gives_up_after_the_attempt_cap(self):
        control = FakeControl()
        poe = FakePoeReset(recovers=False, control=control)  # nothing brings it back
        task = asyncio.create_task(self._bridge(control, poe)._await_control())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.5)
        self.assertEqual(
            poe.calls,
            audio_bridge.CONTROL_REBOOT_MAX_ATTEMPTS,
            "must stop power-cycling a port that three cycles didn't fix, not loop forever",
        )

        control.come_back()
        await asyncio.wait_for(task, 1)
        self.assertEqual(poe.calls, audio_bridge.CONTROL_REBOOT_MAX_ATTEMPTS)

    async def test_a_failed_power_cycle_does_not_escape(self):
        # This runs on the long-lived supervisor loop: an exception here would
        # kill audio supervision for the scanner over a failed *recovery*.
        control = FakeControl()
        poe = FakePoeReset(control=control, exc=MikrotikApiError("router unreachable"))
        task = asyncio.create_task(self._bridge(control, poe)._await_control())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.3)
        self.assertGreaterEqual(poe.calls, 1)
        control.come_back()
        await asyncio.wait_for(task, 1)


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


class StartupRebootGraceTest(unittest.TestCase):
    """The window after a restart in which a refused RTSP port is expected.

    This is the guard that stops an add-on Rebuild from power-cycling the
    radio. The scanner holds the abandoned session from the previous
    container for up to 60s and refuses new ones until it expires; the
    reboot decision used to land at 60-90s, right on top of that, and losing
    the race power-cycled the scanner -- which reloads its saved hold state
    from flash. That is how one accidental hold came back after every
    rebuild for four days (docs/protocol-notes.md, 2026-08-15).
    """

    def _bridge(self, clock) -> AudioBridge:
        return AudioBridge(
            scanner_id="test",
            host="198.51.100.1",
            rtsp_port=554,
            rtp_client_port=5004,
            control=FakeControl(reachable=True),
            poe_reset=FakePoeReset(),
            auto_reboot=True,
            clock=clock,
        )

    def test_a_failure_just_after_startup_does_not_justify_a_power_cycle(self):
        clock = FakeClock()
        bridge = self._bridge(clock)
        bridge._supervising_since = clock()
        bridge._consecutive_failures = audio_bridge.AUTO_REBOOT_FAILURE_THRESHOLD
        clock.tick(90)  # where the old code power-cycled
        self.assertFalse(bridge._past_startup_grace())

    def test_the_same_failure_later_does(self):
        clock = FakeClock()
        bridge = self._bridge(clock)
        bridge._supervising_since = clock()
        bridge._consecutive_failures = audio_bridge.AUTO_REBOOT_FAILURE_THRESHOLD
        clock.tick(audio_bridge.STARTUP_REBOOT_GRACE)
        self.assertTrue(bridge._past_startup_grace())

    def test_the_grace_clears_the_scanners_own_session_timeout(self):
        # The whole point: it has to outlast the 60s the scanner holds the
        # abandoned session for, with room for a backoff cycle on top.
        self.assertGreater(audio_bridge.STARTUP_REBOOT_GRACE, 60.0 + audio_bridge.RECONNECT_BACKOFF)

    def test_it_delays_rather_than_disarms(self):
        # Failures keep accumulating while the grace holds, so a scanner that
        # is genuinely wedged is still power-cycled -- just later. A guard
        # that reset the counter would disable auto-reboot for good on any
        # scanner that fails fast enough at startup.
        clock = FakeClock()
        bridge = self._bridge(clock)
        bridge._supervising_since = clock()
        bridge._consecutive_failures = 5
        clock.tick(10)
        self.assertFalse(bridge._past_startup_grace())
        self.assertEqual(bridge._consecutive_failures, 5)
        clock.tick(audio_bridge.STARTUP_REBOOT_GRACE)
        self.assertTrue(bridge._past_startup_grace())

    def test_a_bridge_that_never_started_supervising_is_not_held_back(self):
        # Defensive: _supervising_since is set by the supervisor loop, so
        # anything calling this before then (or a future caller) gets the
        # old behaviour rather than a silent permanent block.
        bridge = self._bridge(FakeClock())
        self.assertIsNone(bridge._supervising_since)
        self.assertTrue(bridge._past_startup_grace())


if __name__ == "__main__":
    unittest.main()
