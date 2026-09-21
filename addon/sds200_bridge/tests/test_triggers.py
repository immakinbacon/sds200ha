"""Tests for triggers.py -- which rules fire, and what they send.

Matching is the part worth testing: a rule that fires when it shouldn't is
an annoyance, but a rule that silently *never* fires looks identical to one
that is simply waiting for traffic, and there is nothing in the UI or the
log to distinguish them. Most of these are the "never fires" cases.

Delivery is tested through a stubbed session rather than over the network --
what matters here is that the right URL/body is built and that a failure
surfaces as a TriggerError with something readable in it, not that aiohttp
works.

Run with:

    cd addon/sds200_bridge && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config_store  # noqa: E402
import triggers  # noqa: E402
from triggers import TriggerEngine, TriggerError  # noqa: E402


def rule(**overrides):
    """A normalized rule, so these tests exercise the same shape the running
    engine sees rather than a hand-built dict that skips defaults."""
    match = overrides.pop("match", {})
    action = overrides.pop("action", {})
    return config_store.normalize_trigger(
        {"name": "test", "match": match, "action": {"type": "ha_event", **action}, **overrides}
    )


CALL = {
    "scanner_id": "home", "label": "Dispatch / Fire / Countywide", "system": "Countywide",
    "department": "Fire", "channel": "Dispatch", "site": None, "frequency": 851.0125,
    "tgid": "1001", "unit_ids": ["4021"], "mode": "p25", "system_type": "P25 Standard",
    "sub_audio": "NAC 261h", "duration": 12.5, "identity": "Countywide|Fire|Dispatch|851.0125|1001",
}


class TestMatching(unittest.TestCase):
    def test_an_empty_rule_matches_everything(self):
        # Legitimate ("log every call to my webhook"), not a mistake -- the
        # UI says "every call" rather than trying to prevent it.
        self.assertTrue(triggers.matches(rule(), CALL, "start"))

    def test_a_disabled_rule_never_matches(self):
        self.assertFalse(triggers.matches(rule(enabled=False), CALL, "start"))

    def test_the_event_edge_has_to_agree(self):
        self.assertFalse(triggers.matches(rule(event="end"), CALL, "start"))
        self.assertTrue(triggers.matches(rule(event="end"), CALL, "end"))
        self.assertTrue(triggers.matches(rule(event="both"), CALL, "start"))
        self.assertTrue(triggers.matches(rule(event="both"), CALL, "end"))

    def test_both_means_both_call_edges_not_every_event(self):
        # "both" predates the weather events. If it meant "everything", every
        # call rule already out there would have started firing on weather
        # alerts the moment those shipped.
        for event in triggers.WX_EVENTS:
            with self.subTest(event=event):
                self.assertFalse(triggers.matches(rule(event="both"), CALL, event))

    def test_a_weather_rule_fires_only_on_its_own_edge(self):
        alert = {**CALL, "wx_alert": True, "label": "Weather alert"}
        self.assertTrue(triggers.matches(rule(event="wx_alert"), alert, "wx_alert"))
        self.assertFalse(triggers.matches(rule(event="wx_alert"), alert, "wx_clear"))
        self.assertFalse(triggers.matches(rule(event="wx_alert"), CALL, "start"))
        self.assertFalse(triggers.matches(rule(event="start"), alert, "wx_alert"))

    def test_text_criteria_are_substring_and_case_insensitive(self):
        self.assertTrue(triggers.matches(rule(match={"department": "fire"}), CALL, "start"))
        self.assertTrue(triggers.matches(rule(match={"channel": "spat"}), CALL, "start"))
        self.assertFalse(triggers.matches(rule(match={"department": "police"}), CALL, "start"))

    def test_every_filled_criterion_has_to_match(self):
        self.assertTrue(
            triggers.matches(rule(match={"department": "fire", "system": "county"}), CALL, "start")
        )
        self.assertFalse(
            triggers.matches(rule(match={"department": "fire", "system": "state"}), CALL, "start")
        )

    def test_a_criterion_against_a_missing_field_does_not_match(self):
        self.assertFalse(triggers.matches(rule(match={"site": "north"}), CALL, "start"))

    def test_the_unit_id_is_matched_against_every_unit_heard(self):
        call = {**CALL, "unit_ids": ["4021", "4099"]}
        self.assertTrue(triggers.matches(rule(match={"unit_id": "4099"}), call, "start"))
        self.assertFalse(triggers.matches(rule(match={"unit_id": "5000"}), call, "start"))

    def test_a_live_snapshots_single_unit_id_is_also_matched(self):
        # A snapshot has "unit_id"; a history record has "unit_ids". Both
        # reach this code -- the test button fires against a snapshot.
        snapshot = {**CALL, "unit_ids": None, "unit_id": "4021"}
        self.assertTrue(triggers.matches(rule(match={"unit_id": "4021"}), snapshot, "start"))

    def test_frequency_matches_within_the_tolerance(self):
        # The scanner reports 851.012500 and a person types 851.012. An
        # exact float compare would essentially never fire.
        self.assertTrue(triggers.matches(rule(match={"frequency": 851.012}), CALL, "start"))
        self.assertTrue(triggers.matches(rule(match={"frequency": 851.0125}), CALL, "start"))
        self.assertFalse(triggers.matches(rule(match={"frequency": 851.1}), CALL, "start"))

    def test_a_zero_tolerance_demands_an_exact_frequency(self):
        exact = rule(match={"frequency": 851.012, "frequency_tolerance_khz": 0})
        self.assertFalse(triggers.matches(exact, CALL, "start"))

    def test_modes_are_an_any_of_list(self):
        self.assertTrue(triggers.matches(rule(match={"modes": ["p25", "dmr"]}), CALL, "start"))
        self.assertFalse(triggers.matches(rule(match={"modes": ["dmr"]}), CALL, "start"))
        self.assertTrue(triggers.matches(rule(match={"modes": []}), CALL, "start"))

    def test_a_rule_limited_to_another_scanner_does_not_match(self):
        self.assertFalse(triggers.matches(rule(scanner_id="shed"), CALL, "start"))
        self.assertTrue(triggers.matches(rule(scanner_id="home"), CALL, "start"))

    def test_the_minimum_duration_only_applies_on_the_end_edge(self):
        long_call = rule(event="both", match={"min_duration_s": 30})
        # At the start nothing has a duration yet, so the criterion is
        # ignored rather than failing every start event.
        self.assertTrue(triggers.matches(long_call, CALL, "start"))
        self.assertFalse(triggers.matches(long_call, CALL, "end"))
        self.assertTrue(triggers.matches(long_call, {**CALL, "duration": 45}, "end"))


class TestTemplates(unittest.TestCase):
    def test_placeholders_are_substituted(self):
        self.assertEqual(
            triggers.render("{channel} on {system}", CALL), "Dispatch on Countywide"
        )

    def test_unit_ids_render_as_a_list(self):
        self.assertEqual(triggers.render("{unit_ids}", {**CALL, "unit_ids": ["1", "2"]}), "1, 2")

    def test_an_unknown_placeholder_renders_empty_rather_than_failing(self):
        # A typo should produce a slightly wrong notification, not a rule
        # that is silently dead.
        self.assertEqual(triggers.render("[{nope}]", CALL), "[]")

    def test_a_none_value_renders_empty(self):
        self.assertEqual(triggers.render("[{site}]", CALL), "[]")

    def test_text_without_placeholders_is_untouched(self):
        self.assertEqual(triggers.render("plain text", CALL), "plain text")

    def test_typed_values_pass_through_render_unchanged(self):
        # They are service-call fields: a number has to arrive as a number.
        self.assertEqual(
            triggers.render_data({"priority": 3, "urgent": True, "note": None}, CALL),
            {"priority": 3, "urgent": True, "note": None},
        )

    def test_templates_render_inside_lists_and_objects(self):
        self.assertEqual(
            triggers.render_data(
                {"tags": ["{channel}", 2], "payload": {"where": "{system}", "n": 1}}, CALL
            ),
            {"tags": ["Dispatch", 2], "payload": {"where": "Countywide", "n": 1}},
        )


class FakeResponse:
    def __init__(self, status=200, body="ok"):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or FakeResponse()
        self.closed = False

    def request(self, method, url, json=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": headers})
        return self.response

    async def close(self):
        self.closed = True


class DeliveryTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = TriggerEngine()
        self.session = FakeSession()
        self.engine._session = self.session

    def deliver(self, r, snapshot=None, event="start"):
        return asyncio.run(self.engine.deliver(r, snapshot or CALL, event))


class TestWebhook(DeliveryTestCase):
    def test_it_posts_the_call_as_json(self):
        detail = self.deliver(rule(action={"type": "webhook", "url": "http://example/hook"}))
        call = self.session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "http://example/hook")
        self.assertEqual(call["json"]["channel"], "Dispatch")
        self.assertEqual(call["json"]["event"], "start")
        self.assertIn("example", detail)

    def test_extra_fields_are_merged_and_rendered(self):
        self.deliver(
            rule(action={"type": "webhook", "url": "http://example/hook",
                         "data": {"message": "{channel} is up"}})
        )
        self.assertEqual(self.session.calls[0]["json"]["message"], "Dispatch is up")

    def test_an_http_error_becomes_a_readable_failure(self):
        self.session.response = FakeResponse(status=500, body="boom")
        with self.assertRaises(TriggerError) as caught:
            self.deliver(rule(action={"type": "webhook", "url": "http://example/hook"}))
        self.assertIn("500", str(caught.exception))


class TestHomeAssistantActions(DeliveryTestCase):
    def setUp(self):
        super().setUp()
        self._token = triggers.os.environ.get("SUPERVISOR_TOKEN")
        triggers.os.environ["SUPERVISOR_TOKEN"] = "test-token"
        self.addCleanup(self._restore_token)

    def _restore_token(self):
        if self._token is None:
            triggers.os.environ.pop("SUPERVISOR_TOKEN", None)
        else:
            triggers.os.environ["SUPERVISOR_TOKEN"] = self._token

    def test_a_service_call_goes_to_the_supervisor_proxy(self):
        self.deliver(
            rule(action={"type": "ha_service", "domain": "notify",
                         "service": "persistent_notification",
                         "data": {"message": "{label}"}})
        )
        call = self.session.calls[0]
        self.assertEqual(call["url"], "http://supervisor/core/api/services/notify/persistent_notification")
        self.assertEqual(call["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(call["json"], {"message": "Dispatch / Fire / Countywide"})

    def test_a_script_gets_its_fields_with_their_types(self):
        # Calling script.<name> directly, which is the form where the
        # script's own fields are the service data.
        self.deliver(
            rule(action={"type": "ha_service", "domain": "script", "service": "send_notification",
                         "data": {"message": "{label}", "priority": 3, "urgent": True}})
        )
        call = self.session.calls[0]
        self.assertEqual(
            call["url"], "http://supervisor/core/api/services/script/send_notification"
        )
        self.assertEqual(
            call["json"],
            {"message": "Dispatch / Fire / Countywide", "priority": 3, "urgent": True},
        )

    def test_the_call_details_are_only_included_when_asked_for(self):
        # Most service schemas reject fields they don't know about, so
        # merging the whole reception in by default would break the common
        # case (notify) rather than help it.
        self.deliver(rule(action={"type": "ha_service", "domain": "light", "service": "turn_on",
                                  "entity_id": "light.hall"}))
        self.assertEqual(self.session.calls[0]["json"], {"entity_id": "light.hall"})

        self.deliver(rule(action={"type": "ha_service", "domain": "light", "service": "turn_on",
                                  "include_reception": True}))
        self.assertIn("reception", self.session.calls[1]["json"])

    def test_an_event_carries_the_whole_call(self):
        self.deliver(rule(action={"type": "ha_event", "event_type": "sds200_reception"}))
        call = self.session.calls[0]
        self.assertEqual(call["url"], "http://supervisor/core/api/events/sds200_reception")
        self.assertEqual(call["json"]["tgid"], "1001")

    def test_a_401_explains_the_missing_permission(self):
        # Without homeassistant_api in config.yaml the token is issued but
        # the proxy rejects it -- a bare "HTTP 401" would send someone
        # hunting for a credential that doesn't exist.
        self.session.response = FakeResponse(status=401, body="unauthorized")
        with self.assertRaises(TriggerError) as caught:
            self.deliver(rule(action={"type": "ha_event", "event_type": "x"}))
        self.assertIn("homeassistant_api", str(caught.exception))

    def test_no_token_at_all_says_so(self):
        triggers.os.environ.pop("SUPERVISOR_TOKEN", None)
        with self.assertRaises(TriggerError) as caught:
            self.deliver(rule(action={"type": "ha_event", "event_type": "x"}))
        self.assertIn("SUPERVISOR_TOKEN", str(caught.exception))


class TestCooldown(unittest.TestCase):
    def setUp(self):
        self.engine = TriggerEngine()

    def test_a_repeat_within_the_cooldown_is_suppressed(self):
        r = rule(cooldown_s=30)
        self.assertTrue(self.engine._claim_cooldown(r, CALL))
        self.assertFalse(self.engine._claim_cooldown(r, CALL))

    def test_a_different_talkgroup_is_not_suppressed(self):
        # Keyed by rule *and* what was heard: an action for engine 12 must not
        # swallow the one for engine 3 thirty seconds later.
        r = rule(cooldown_s=30)
        self.assertTrue(self.engine._claim_cooldown(r, CALL))
        self.assertTrue(self.engine._claim_cooldown(r, {**CALL, "identity": "other"}))

    def test_zero_means_every_call(self):
        r = rule(cooldown_s=0)
        self.assertTrue(self.engine._claim_cooldown(r, CALL))
        self.assertTrue(self.engine._claim_cooldown(r, CALL))

    def test_removing_a_rule_forgets_its_cooldowns(self):
        r = rule(cooldown_s=30)
        self.engine.set_rules([r])
        self.engine._claim_cooldown(r, CALL)
        self.engine.set_rules([])
        self.assertEqual(self.engine._cooldowns, {})


class TestTranscriptRules(unittest.TestCase):
    """Firing on what was said.

    The reason the transcript needs an event of its own: a call ends when the
    squelch has been shut for two polls, while its transcript lands after a
    model elsewhere has had its turn. Folding it into "end" would either hold
    up every rule that has nothing to do with speech, or match nothing
    because the text is not written yet.
    """

    def rule(self, **match):
        return {
            "id": "r1", "enabled": True, "event": "transcript",
            "scanner_id": "", "match": {"modes": [], **match},
        }

    def record(self, **fields):
        base = {"scanner_id": "home", "mode": "analog", "transcript": "",
                "transcript_status": None}
        base.update(fields)
        return base

    def test_it_fires_on_a_word_in_the_transcript(self):
        rule = self.rule(transcript="structure fire")
        record = self.record(transcript="engine twelve respond to a structure fire on elm")
        self.assertTrue(triggers.matches(rule, record, "transcript"))

    def test_matching_is_case_insensitive_like_every_other_criterion(self):
        rule = self.rule(transcript="STRUCTURE")
        self.assertTrue(triggers.matches(
            rule, self.record(transcript="a structure fire"), "transcript"))

    def test_it_does_not_fire_on_a_call_with_no_transcript(self):
        # The important one. A rule looking for a word must not fire on a
        # call whose transcript was rejected or never attempted -- those
        # carry a status and no text, and an empty needle would match
        # everything.
        rule = self.rule(transcript="fire")
        for status in (None, "no-speech", "no-audio", "doubtful"):
            with self.subTest(status=status):
                record = self.record(transcript="", transcript_status=status)
                self.assertFalse(triggers.matches(rule, record, "transcript"))

    def test_a_transcript_rule_does_not_fire_when_the_call_ends(self):
        # The text does not exist yet at that moment.
        rule = self.rule(transcript="fire")
        record = self.record(transcript="a structure fire")
        self.assertFalse(triggers.matches(rule, record, "end"))

    def test_both_still_means_the_two_call_edges_only(self):
        # Otherwise every existing "both" rule would start firing a third
        # time per call the moment transcription was switched on.
        rule = self.rule()
        rule["event"] = "both"
        self.assertFalse(triggers.fires_on("both", "transcript"))
        self.assertTrue(triggers.fires_on("both", "start"))
        self.assertTrue(triggers.fires_on("both", "end"))

    def test_other_criteria_still_apply_on_a_transcript(self):
        rule = self.rule(transcript="fire", department="Fire")
        heard = self.record(transcript="structure fire", department="Fire")
        elsewhere = self.record(transcript="structure fire", department="Police")
        self.assertTrue(triggers.matches(rule, heard, "transcript"))
        self.assertFalse(triggers.matches(rule, elsewhere, "transcript"))


class TestTriggerValidation(unittest.TestCase):
    def _check(self, r):
        errors, warnings = [], []
        config_store.validate_triggers({"triggers": [r], "scanners": []}, errors, warnings)
        return errors, warnings

    def test_a_webhook_without_a_url_is_an_error(self):
        errors, _ = self._check(rule(action={"type": "webhook"}))
        self.assertTrue(any("URL" in e for e in errors))

    def test_a_webhook_url_has_to_be_http(self):
        errors, _ = self._check(rule(action={"type": "webhook", "url": "example.com/hook"}))
        self.assertTrue(any("http://" in e for e in errors))

    def test_a_service_needs_a_domain_and_a_service(self):
        errors, _ = self._check(rule(action={"type": "ha_service", "domain": "notify"}))
        self.assertTrue(errors)

    def test_a_minimum_duration_on_the_start_edge_warns(self):
        # The one combination that looks reasonable and silently never fires.
        _, warnings = self._check(rule(event="start", match={"min_duration_s": 30}))
        self.assertTrue(any("never fire" in w for w in warnings))

    def test_a_rule_pinned_to_a_missing_scanner_warns(self):
        _, warnings = self._check(rule(scanner_id="gone"))
        self.assertTrue(any("gone" in w for w in warnings))

    def test_a_valid_rule_has_no_errors(self):
        errors, _ = self._check(rule(action={"type": "ha_event", "event_type": "sds200_reception"}))
        self.assertEqual(errors, [])


class TestNormalization(unittest.TestCase):
    def test_an_id_is_generated_when_missing(self):
        self.assertTrue(config_store.normalize_trigger({})["id"])

    def test_an_existing_id_is_kept(self):
        # The cooldown bookkeeping and the "last fired" display are keyed by
        # it, so regenerating on every save would reset both.
        self.assertEqual(config_store.normalize_trigger({"id": "keep-me"})["id"], "keep-me")

    def test_unknown_modes_are_dropped(self):
        normalized = config_store.normalize_trigger({"match": {"modes": ["p25", "made-up"]}})
        self.assertEqual(normalized["match"]["modes"], ["p25"])

    def test_an_unknown_action_type_falls_back_rather_than_saving_garbage(self):
        self.assertEqual(config_store.normalize_trigger({"action": {"type": "nope"}})["action"]["type"], "ha_event")

    def test_a_blank_frequency_is_no_criterion_rather_than_zero(self):
        # 0.0 would mean "match DC", i.e. never -- a blank field has to stay
        # blank all the way through.
        self.assertIsNone(config_store.normalize_trigger({"match": {"frequency": ""}})["match"]["frequency"])


if __name__ == "__main__":
    unittest.main()
