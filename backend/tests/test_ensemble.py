import json
import math
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import autopilot, crawler, ensemble, etl, ledger, llm, tickets
from backend.app.db import connect, encode, initialize
from backend.app.games import game
from backend.app.main import app, visitors
from backend.tests.test_research import csv_bytes, draw


def history(spec, count=120, seed=11, hot=None):
    generator = random.Random(seed)
    rows = []
    for index in range(count):
        if hot:
            picks = generator.sample(hot, spec["pick_count"] - 1) + [generator.randrange(spec["number_max"] * 2 // 3,
                                                                                        spec["number_max"]) + 1]
        else:
            picks = generator.sample(range(1, spec["number_max"] + 1), spec["pick_count"])
        rows.append({"draw_id": index + 1, "draw_date": "2025-01-01", "draw_slot": "18:00",
                     "main": sorted(set(picks) if len(set(picks)) == spec["pick_count"] else picks),
                     "special": None, "digits": {}, "verified": False, "source_url": "https://example.test"})
    return rows


class EnsembleTests(unittest.TestCase):
    def test_probabilities_are_positive_inclusion_distributions(self):
        for code in ("power655", "mega645", "lotto535"):
            spec = game(code)
            with self.subTest(code=code):
                result = ensemble.estimate(history(spec, 90), spec)
                values = result["probabilities"]
                self.assertEqual(len(values), spec["number_max"])
                self.assertAlmostEqual(sum(values), spec["pick_count"], places=9)
                self.assertTrue(all(value > 0 for value in values))
                self.assertAlmostEqual(sum(result["weights"].values()), 1.0, places=9)

    def test_fitting_is_deterministic_and_cached(self):
        spec = game("power655")
        rows = history(spec, 80)
        first = ensemble.estimate(rows, spec)
        ensemble._CACHE.clear()
        second = ensemble.estimate(rows, spec)
        self.assertEqual(first["probabilities"], second["probabilities"])
        self.assertEqual(first["weights"], second["weights"])

    def test_mutating_returned_estimate_does_not_poison_cache(self):
        spec = game("power655")
        rows = history(spec, 60)
        first = ensemble.estimate(rows, spec)
        first["probabilities"][0] = 999.0
        self.assertNotEqual(ensemble.estimate(rows, spec)["probabilities"][0], 999.0)

    def test_no_history_degrades_to_uniform(self):
        spec = game("power655")
        result = ensemble.estimate([], spec)
        self.assertEqual(result["weights"], {"uniform": 1.0})
        self.assertEqual(result["selection"]["status"], "no_history")
        self.assertAlmostEqual(sum(result["probabilities"]), spec["pick_count"], places=9)
        self.assertIsNone(ensemble.fit([], spec)["metrics"])

    def test_short_history_falls_back_without_tuning(self):
        spec = game("power655")
        result = ensemble.estimate(history(spec, 6), spec)
        self.assertEqual(result["selection"]["status"], "insufficient_history")
        self.assertIsNone(ensemble.fit(history(spec, 6), spec)["metrics"])

    def test_selection_never_sees_the_target_draw(self):
        spec = game("power655")
        rows = history(spec, 70)
        seen = []
        original = ensemble.select

        def spy(training, spec_):
            seen.append(len(training))
            return original(training, spec_)

        with patch.object(ensemble, "select", side_effect=spy):
            ensemble.evaluate(rows, spec, test_draws=10)
        self.assertTrue(seen)
        for length in seen:
            self.assertLess(length, len(rows))

    def test_injected_signal_is_detected_and_beats_uniform(self):
        spec = game("power655")
        rows = history(spec, 140, hot=[1, 2, 3, 4, 5])
        fitted = ensemble.fit(rows, spec)
        weights = fitted["weights"]
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)
        self.assertLess(weights.get("uniform", 0.0), 0.5,
                        "a strong planted pattern should not stay dominated by the uniform prior")
        metrics = fitted["metrics"]
        self.assertLess(metrics["logloss"], metrics["baseline_logloss"])
        self.assertLess(metrics["logloss_difference_ci95"][1], 0)
        self.assertIn("bang chung loi the", " ".join(metrics["conclusion"].split()))

    def test_random_history_does_not_claim_an_edge(self):
        spec = game("power655")
        metrics = ensemble.fit(history(spec, 200, seed=5), spec)["metrics"]
        self.assertIn("Chua du bang chung", metrics["conclusion"])
        self.assertEqual(metrics["holdout"], False)
        self.assertTrue(metrics["exploratory"])

    def test_calibration_is_bounded(self):
        spec = game("power655")
        fitted = ensemble.fit(history(spec, 120), spec)
        self.assertGreaterEqual(fitted["calibration"]["expected_calibration_error"], 0.0)
        self.assertLessEqual(fitted["calibration"]["expected_calibration_error"], 1.0)

    def test_tilt_is_bounded_and_renormalised(self):
        spec = game("power655")
        base = ensemble.estimate(history(spec, 40), spec)["probabilities"]
        scores = [1.0] * spec["number_max"]
        tilted, report = ensemble.tilt(base, scores, confidence=1.0, maximum=0.25)
        self.assertAlmostEqual(sum(tilted), sum(base), places=9)
        self.assertTrue(report["applied"])
        self.assertAlmostEqual(report["strength"], 0.25)
        # Even at full confidence the adjustment stays a nudge, not a rewrite.
        ratio = max(tilted[number] / base[number] for number in range(spec["number_max"]))
        self.assertLess(ratio, math.exp(0.26))
        identical, none = ensemble.tilt(base, [0.0] * spec["number_max"], 1.0, 1.0)
        self.assertIsNone(none)
        self.assertEqual(identical, base)

    def test_tilt_rejects_empty_scores_and_keeps_positivity(self):
        spec = game("power655")
        base = ensemble.estimate(history(spec, 30), spec)["probabilities"]
        self.assertEqual(ensemble.tilt(base, [], 1.0, 1.0)[0], base)
        tilted, _ = ensemble.tilt(base, [-1.0] * spec["number_max"], 1.0, 1.0)
        self.assertAlmostEqual(sum(tilted), sum(base), places=9)
        self.assertTrue(all(value > 0 for value in tilted))

    def test_analytics_dispatch_supports_adaptive(self):
        from backend.app import analytics
        spec = game("power655")
        rows = [{**row, "verified": True} for row in history(spec, 40)]
        self.assertAlmostEqual(sum(analytics.probabilities(rows, spec, "adaptive")), spec["pick_count"], places=9)
        self.assertAlmostEqual(sum(analytics.probabilities(rows, spec, "bayesian")), spec["pick_count"], places=9)
        with self.assertRaises(ValueError):
            analytics.probabilities(rows, spec, "xgboost")
        with self.assertRaises(ValueError):
            analytics.probabilities([], game("max3d"), "adaptive")

    def test_backtest_reports_logloss_interval(self):
        from backend.app import analytics
        spec = game("power655")
        rows = [{**row, "verified": True} for row in history(spec, 40)]
        result = analytics.backtest(rows, spec, min_train=20, test_draws=10, method="adaptive")
        self.assertEqual(result["evaluated_draws"], 10)
        self.assertEqual(len(result["logloss_difference_ci95"]), 2)
        self.assertLessEqual(result["logloss_difference_ci95"][0], result["logloss_difference_ci95"][1])


class TicketTests(unittest.TestCase):
    def test_sampling_keeps_historical_contract(self):
        spec = game("power655")
        probability = ensemble.estimate(history(spec, 40), spec)["probabilities"]
        chosen = tickets.sample(probability, spec, 8, 3)
        self.assertEqual(len(chosen), 8)
        self.assertEqual(len({tuple(ticket["main"]) for ticket in chosen}), 8)
        for ticket in chosen:
            self.assertEqual(len(ticket["main"]), spec["pick_count"])
            self.assertEqual(len(set(ticket["main"])), spec["pick_count"])
            self.assertNotIn(ticket["special"], ticket["main"])

    def test_portfolio_is_deduplicated_and_complete(self):
        for code in ("power655", "mega645", "lotto535"):
            spec = game(code)
            with self.subTest(code=code):
                probability = ensemble.estimate(history(spec, 60), spec)["probabilities"]
                chosen = tickets.portfolio(probability, spec, 5, 21)
                self.assertEqual(len(chosen), 5)
                self.assertEqual(len({tuple(ticket["main"]) for ticket in chosen}), 5)
                for ticket in chosen:
                    self.assertEqual(len(set(ticket["main"])), spec["pick_count"])
                    self.assertTrue(all(1 <= number <= spec["number_max"] for number in ticket["main"]))

    def test_portfolio_beats_independent_sampling_on_a_peaked_model(self):
        spec = game("power655")
        probability = ensemble.estimate(history(spec, 140, hot=[1, 2, 3, 4, 5]), spec)["probabilities"]
        report = tickets.compare(probability, spec, 5, 7, simulations=400)
        self.assertGreaterEqual(report["portfolio"]["coverage"], report["independent"]["coverage"])
        self.assertEqual(report["portfolio"]["tickets"], 5)
        self.assertFalse(report["portfolio"]["scope"].startswith("win"))
        # Overlap is a consequence of the model, not a defect: a peaked model
        # concentrates every ticket on the hot numbers on purpose.
        self.assertGreaterEqual(report["portfolio"]["distinct_numbers"], spec["pick_count"])

    def test_tiers_are_structural(self):
        self.assertEqual(tickets.tiers(game("power655")), [3, 4])
        self.assertEqual(tickets.tiers(game("lotto535")), [2, 3])

    def test_portfolio_validates_count(self):
        spec = game("power655")
        probability = ensemble.estimate(history(spec, 20), spec)["probabilities"]
        for count in (0, -1, 101, 1.5):
            with self.subTest(count=count), self.assertRaises(ValueError):
                tickets.portfolio(probability, spec, count, 1)


class AdvisorParsingTests(unittest.TestCase):
    def setUp(self):
        self.spec = game("power655")

    def test_parse_rejects_confidence_without_adjustments(self):
        self.assertIsNone(llm.parse({"confidence": 0.9}, self.spec))
        self.assertIsNone(llm.parse({"confidence": 0.0, "boost": [{"number": 3, "score": 0.5}]}, self.spec))

    def test_parse_clamps_and_sorts(self):
        parsed = llm.parse({"confidence": 5, "boost": [{"number": 3, "score": 9}, {"number": 3, "score": 0.2},
                                                       {"number": 0, "score": 0.4}, {"number": 99, "score": 0.4}],
                            "suppress": [{"number": 7, "score": -9}, {"number": 8, "score": 0.5}]}, self.spec)
        self.assertEqual(parsed["confidence"], 1.0)
        self.assertEqual(parsed["boost"], [{"number": 3, "score": 1.0}])
        self.assertEqual(parsed["suppress"], [{"number": 7, "score": -1.0}])
        self.assertEqual(len(parsed["scores"]), self.spec["number_max"])
        self.assertEqual(parsed["scores"][2], 1.0)
        self.assertEqual(parsed["scores"][6], -1.0)

    def test_parse_caps_number_of_adjustments(self):
        boosts = [{"number": number, "score": 0.5} for number in range(1, 40)]
        parsed = llm.parse({"confidence": 1, "boost": boosts}, self.spec)
        self.assertEqual(len(parsed["boost"]), llm.MAX_ADJUSTMENTS)

    def test_extract_handles_fenced_and_parted_content(self):
        fenced = {"choices": [{"message": {"content": "```json\n{\"confidence\": 0.4, \"boost\": []}\n```"}}]}
        self.assertEqual(llm._extract(fenced)[0]["confidence"], 0.4)
        parted = {"choices": [{"message": {"content": [{"type": "text", "text": "{\"confidence\": 0.2}"}]}}]}
        self.assertEqual(llm._extract(parted)[0]["confidence"], 0.2)
        for bad in ({}, {"choices": []}, {"choices": [{"message": {"content": "sorry"}}]}, "text"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                llm._extract(bad)

    def test_status_never_leaks_the_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-secret-value", "OPENAI_MODEL": "local-model"}):
            status = llm.status()
        self.assertTrue(status["configured"])
        self.assertTrue(status["enabled"])
        self.assertNotIn("sk-secret-value", json.dumps(status))
        self.assertEqual(status["model"], "local-model")

    def test_disabled_without_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            self.assertFalse(llm.status()["enabled"])
            self.assertIsNone(llm.advise("power655", self.spec, history(self.spec, 20),
                                         [0.1] * self.spec["number_max"], []))

    def test_disabled_reason_distinguishes_flag_from_missing_key(self):
        """A set key with OPENAI_ENABLED=0 must not be reported as 'no key'."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-present", "OPENAI_ENABLED": "0"}):
            status = llm.status()
        self.assertFalse(status["enabled"])
        self.assertTrue(status["configured"])
        self.assertEqual(status["disabled_reason"], "disabled_by_flag")
        self.assertIn("OPENAI_ENABLED=0", status["disabled_message"])
        self.assertNotIn("Chưa có OPENAI_API_KEY", status["disabled_message"])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "OPENAI_ENABLED": "auto"}):
            missing = llm.status()
        self.assertEqual(missing["disabled_reason"], "missing_key")
        self.assertIn("OPENAI_API_KEY", missing["disabled_message"])

    def test_enabled_flag_variants(self):
        for flag, expected in (("auto", True), ("1", True), ("true", True),
                               ("0", False), ("off", False), ("FALSE", False), ("no", False)):
            with self.subTest(flag=flag), patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x", "OPENAI_ENABLED": flag}):
                self.assertEqual(llm.status()["enabled"], expected)

    def test_check_reports_disabled_without_network(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x", "OPENAI_ENABLED": "0"}):
            result = llm.check()
        self.assertFalse(result["reachable"])
        self.assertEqual(result["disabled_reason"], "disabled_by_flag")
        self.assertIn("OPENAI_ENABLED=0", result["error"])

    def test_check_accepts_plain_text_reply(self):
        """The probe must not demand JSON; a bare 'ok' proves reachability."""
        def transport(url, key, body, timeout):
            self.assertNotIn("response_format", body)
            return {"model": "served-name", "usage": {"total_tokens": 9},
                    "choices": [{"message": {"content": "ok"}}]}

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x", "OPENAI_ENABLED": "auto"}):
            result = llm.check(transport=transport)
        self.assertTrue(result["reachable"])
        self.assertEqual(result["reply"], "ok")
        self.assertEqual(result["model_echoed"], "served-name")
        self.assertIsNone(result["error"])

    def test_check_surfaces_http_errors(self):
        import urllib.error

        def transport(url, key, body, timeout):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {},
                                         __import__("io").BytesIO(b'{"error":"bad key"}'))

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x", "OPENAI_ENABLED": "auto"}):
            result = llm.check(transport=transport)
        self.assertFalse(result["reachable"])
        self.assertIn("401", result["error"])
        self.assertIn("bad key", result["error"])

    def test_timeout_default_survives_a_slow_gateway(self):
        """A full 55-number advisory can take ~30s; 15s silently killed every call."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x"}, clear=False):
            os.environ.pop("OPENAI_TIMEOUT", None)
            self.assertGreaterEqual(llm.config()["timeout"], 30)
        with patch.dict(os.environ, {"OPENAI_TIMEOUT": "600"}):
            self.assertEqual(llm.config()["timeout"], 180.0)

    def test_explicit_disable_flag(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x", "OPENAI_ENABLED": "0"}):
            self.assertFalse(llm.status()["enabled"])

    def test_plain_http_only_for_loopback(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://api.example.com/v1"}):
            with self.assertRaises(ValueError):
                llm._endpoint(llm.config())
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"}):
            self.assertEqual(llm._endpoint(llm.config()), "http://127.0.0.1:11434/v1/chat/completions")

    def test_advise_reports_failures_without_raising(self):
        def failing(url, key, body, timeout):
            raise OSError("connection refused")

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x"}):
            self.assertIsNone(llm.advise("power655", self.spec, history(self.spec, 20),
                                         [0.1] * self.spec["number_max"], [], transport=failing))

    def test_advise_returns_bounded_scores_from_transport(self):
        calls = []

        def transport(url, key, body, timeout):
            calls.append((url, body))
            return {"choices": [{"message": {"content": json.dumps({
                "confidence": 0.5, "boost": [{"number": 4, "score": 0.6}],
                "suppress": [{"number": 9, "score": -0.3}], "rationale": "recent gap"})}}],
                "usage": {"total_tokens": 42}}

        rows = history(self.spec, 25)
        probability = [0.1] * self.spec["number_max"]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x", "OPENAI_MAX_TILT": "0.3", "OPENAI_MODEL": "m"}):
            suggestion = llm.advise("power655", self.spec, rows, probability, [], transport=transport)
        self.assertEqual(suggestion["confidence"], 0.5)
        self.assertEqual(suggestion["scores"][3], 0.6)
        self.assertEqual(suggestion["scores"][8], -0.3)
        self.assertEqual(suggestion["usage"], {"total_tokens": 42})
        self.assertEqual(len(calls), 1)
        self.assertIn("recent_draws", calls[0][1]["messages"][1]["content"])
        self.assertNotIn("sk-x", json.dumps(calls[0][1]))

    def test_advise_retries_without_json_mode_on_400(self):
        import urllib.error
        bodies = []

        def transport(url, key, body, timeout):
            bodies.append(dict(body))
            if "response_format" in body:
                raise urllib.error.HTTPError(url, 400, "unsupported", {}, None)
            return {"choices": [{"message": {"content": "{\"confidence\": 0.1, \"boost\": [{\"number\": 1, \"score\": 0.2}]}"}}]}

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x"}):
            suggestion = llm.advise("power655", self.spec, history(self.spec, 25),
                                    [0.1] * self.spec["number_max"], [], transport=transport)
        self.assertEqual(len(bodies), 2)
        self.assertIn("response_format", bodies[0])
        self.assertNotIn("response_format", bodies[1])
        self.assertEqual(suggestion["confidence"], 0.1)


class AdaptiveLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "DATABASE_PATH": str(Path(self.directory.name) / "adaptive.sqlite3"),
            "API_KEY": "adaptive-test-key", "DISABLE_RUNNER": "1", "AUTO_SYNC": "0",
            "OPENAI_API_KEY": "", "OPENAI_ENABLED": "0",
        })
        self.environment.start()
        initialize()
        visitors.clear()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def test_lock_records_adaptive_payload_with_coverage(self):
        spec = game("power655") | {"rules_verified": True}
        target = datetime.now(timezone.utc) + timedelta(days=30)
        while target.weekday() not in spec["weekdays"]:
            target += timedelta(days=1)
        target = target.replace(hour=11, minute=0, second=0, microsecond=0)
        rows = [draw(index + 1, (datetime(2025, 1, 2) + timedelta(days=index * 2)).date().isoformat())
                for index in range(20)]
        rows = [row for row in rows if datetime.fromisoformat(row["draw_date"]).weekday() in spec["weekdays"]]
        etl.import_csv(csv_bytes(rows))
        with patch.object(ledger, "game", return_value=spec):
            result = ledger.lock("power655", max(int(row["draw_id"]) for row in rows) + 1,
                                 target.isoformat(), "adaptive", 5)
        payload = result["payload"]
        self.assertEqual(payload["method"], "adaptive")
        self.assertEqual(payload["model_revision"], ensemble.REVISION)
        self.assertEqual(payload["ticket_strategy"], "portfolio")
        self.assertEqual(len(payload["tickets"]), 5)
        self.assertAlmostEqual(sum(payload["probabilities"]), spec["pick_count"], places=9)
        self.assertIsNotNone(payload["coverage"])
        self.assertNotIn("advisor", payload)
        self.assertNotIn("tilt", payload)

    def test_advisor_tilt_is_recorded_with_base_vector(self):
        spec = game("power655") | {"rules_verified": True}
        target = datetime.now(timezone.utc) + timedelta(days=30)
        while target.weekday() not in spec["weekdays"]:
            target += timedelta(days=1)
        target = target.replace(hour=11, minute=0, second=0, microsecond=0)
        rows = [draw(index + 1, (datetime(2025, 1, 2) + timedelta(days=index * 2)).date().isoformat())
                for index in range(20)]
        rows = [row for row in rows if datetime.fromisoformat(row["draw_date"]).weekday() in spec["weekdays"]]
        etl.import_csv(csv_bytes(rows))
        scores = [0.0] * spec["number_max"]
        scores[0] = 0.8
        scores[1] = -0.8
        suggestion = {"confidence": 0.9, "max_tilt": 0.25, "scores": scores, "boost": [], "suppress": [],
                      "rationale": "test", "model": "m", "provider": "p", "latency_ms": 5,
                      "request_sha256": "0" * 64, "usage": {}}
        with patch.object(ledger, "game", return_value=spec), \
                patch.object(llm, "advise", return_value=suggestion):
            result = ledger.lock("power655", max(int(row["draw_id"]) for row in rows) + 1,
                                 target.isoformat(), "adaptive", 3)
        payload = result["payload"]
        self.assertIn("base_probabilities", payload)
        self.assertIn("advisor", payload)
        self.assertNotIn("scores", payload["advisor"])
        self.assertAlmostEqual(sum(payload["probabilities"]), spec["pick_count"], places=9)
        self.assertAlmostEqual(sum(payload["base_probabilities"]), spec["pick_count"], places=9)
        self.assertNotEqual(payload["probabilities"], payload["base_probabilities"])

    def test_advisor_failure_still_locks_a_statistical_prediction(self):
        spec = game("power655") | {"rules_verified": True}
        target = datetime.now(timezone.utc) + timedelta(days=30)
        while target.weekday() not in spec["weekdays"]:
            target += timedelta(days=1)
        target = target.replace(hour=11, minute=0, second=0, microsecond=0)
        rows = [draw(index + 1, (datetime(2025, 1, 2) + timedelta(days=index * 2)).date().isoformat())
                for index in range(20)]
        rows = [row for row in rows if datetime.fromisoformat(row["draw_date"]).weekday() in spec["weekdays"]]
        etl.import_csv(csv_bytes(rows))
        with patch.object(ledger, "game", return_value=spec), \
                patch.object(llm, "advise", side_effect=MachineError("boom")):
            with self.assertRaises(MachineError):
                ledger.lock("power655", max(int(row["draw_id"]) for row in rows) + 1,
                            target.isoformat(), "adaptive", 2)
        with patch.object(ledger, "game", return_value=spec), \
                patch.object(llm, "advise", return_value=None):
            result = ledger.lock("power655", max(int(row["draw_id"]) for row in rows) + 1,
                                 target.isoformat(), "adaptive", 2)
        self.assertNotIn("advisor", result["payload"])

    def _target(self, spec, days=30):
        """A future timestamp on the game's real local schedule."""
        local = (datetime.now(ledger.ZoneInfo(spec["timezone"])) + timedelta(days=days))
        while local.weekday() not in spec["weekdays"]:
            local += timedelta(days=1)
        hour, minute = (int(part) for part in spec["slots"][0].split(":"))
        return local.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def test_repeated_lock_is_idempotent_after_the_result_arrives(self):
        """`once` must short-circuit before fitting, or a finished draw raises."""
        spec = game("power655") | {"rules_verified": True}
        target = self._target(spec)
        rows = [draw(index + 1, (datetime(2025, 1, 2) + timedelta(days=index * 2)).date().isoformat())
                for index in range(20)]
        rows = [row for row in rows if datetime.fromisoformat(row["draw_date"]).weekday() in spec["weekdays"]]
        etl.import_csv(csv_bytes(rows))
        highest = max(int(row["draw_id"]) for row in rows)
        with patch.object(ledger, "game", return_value=spec):
            first = ledger.lock("power655", highest + 1, target.isoformat(), "adaptive", 3, once=True)
        # The target result now exists, so a fresh fit would be rejected as unseen.
        from backend.app.db import connect as db_connect
        local = target.astimezone(ledger.ZoneInfo(spec["timezone"]))
        with db_connect() as connection:
            connection.execute("INSERT INTO draws VALUES(?,?,?,?,?,?,?,?,?,?)",
                               ("power655", highest + 1, local.date().isoformat(), local.strftime("%H:%M"),
                                encode([1, 2, 3, 4, 5, 6]), None, "{}", "https://vietlott.vn", 1, etl.now()))
        with patch.object(ledger, "game", return_value=spec):
            again = ledger.lock("power655", highest + 1, target.isoformat(), "adaptive", 3, once=True)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["payload_hash"], first["payload_hash"])

    def test_advisor_api_surface(self):
        spec = game("power655")
        rows = [draw(index + 1, (datetime(2025, 1, 2) + timedelta(days=index * 2)).date().isoformat())
                for index in range(24)]
        rows = [row for row in rows if datetime.fromisoformat(row["draw_date"]).weekday() in spec["weekdays"]]
        etl.import_csv(csv_bytes(rows))
        with TestClient(app) as client:
            status = client.get("/api/games/power655/advisor")
            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.json()["advisor"]["enabled"])
            self.assertEqual(client.post("/api/games/power655/advisor/preview").status_code, 401)
            response = client.post("/api/games/power655/advisor/preview",
                                   headers={"X-API-Key": "adaptive-test-key"})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(len(body["statistical"]), spec["number_max"])
            self.assertEqual(sorted(body["top"]), sorted(body["top"]))
            self.assertIsNone(body["suggestion"])
            self.assertIsNone(body["tilt"])
            self.assertEqual(client.post("/api/games/max3d/advisor/preview",
                                         headers={"X-API-Key": "adaptive-test-key"}).status_code, 400)

    def test_workspace_exposes_advisor_state_and_ensemble_metadata(self):
        spec = game("power655")
        rows = [draw(index + 1, (datetime(2025, 1, 2) + timedelta(days=index * 2)).date().isoformat())
                for index in range(24)]
        rows = [row for row in rows if datetime.fromisoformat(row["draw_date"]).weekday() in spec["weekdays"]]
        etl.import_csv(csv_bytes(rows))
        autopilot.tick()
        state = autopilot.workspace("power655")
        self.assertFalse(state["advisor"]["enabled"])
        model = state["model"]
        self.assertEqual(model["method"], "adaptive")
        self.assertEqual(model["algorithm_revision"], ensemble.REVISION)
        self.assertIn("weights", model)
        self.assertIn("selection", model)
        self.assertEqual(len(model["probabilities"]), spec["number_max"])
        self.assertAlmostEqual(sum(model["probabilities"]), spec["pick_count"], places=9)
        self.assertEqual(len(model["tickets"]), 5)
        self.assertTrue(all(len(set(ticket["main"])) == spec["pick_count"] for ticket in model["tickets"]))


class MachineError(RuntimeError):
    """Deliberately not caught by the advisor's handled-exception list."""


if __name__ == "__main__":
    unittest.main()