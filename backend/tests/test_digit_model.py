import copy
import json
import math
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import autopilot, crawler, digit_model, etl, ledger
from backend.app.db import connect, initialize
from backend.app.games import game
from backend.app.main import app, visitors


def rows_for(code, count=24, last_day="2025-01-01"):
    spec = game(code)
    days = []
    day = datetime.fromisoformat(last_day).date()
    while len(days) < count:
        if day.weekday() in spec["weekdays"]:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    return [{"game_code": code, "draw_id": index + 1, "draw_date": day,
             "draw_slot": "18:00", "main": [], "special": None,
             "digits": {group: [f"{(index + offset * 17) % 1000:03d}" for offset in range(size)]
                        for group, size in spec["digit_groups"].items()},
             "verified": False, "source_url": spec["crawler"]["list_url"]}
            for index, day in enumerate(reversed(days))]


class DigitModelTests(unittest.TestCase):
    def test_all_groups_are_positive_normalized_distributions(self):
        for code in ("max3d", "max3dpro"):
            spec = game(code)
            for config in digit_model.CANDIDATES:
                with self.subTest(code=code, config=config["name"]):
                    distribution = digit_model.fit(rows_for(code), spec, config)
                    self.assertEqual(set(distribution), set(spec["digit_groups"]))
                    for values in distribution.values():
                        self.assertEqual(len(values), 1000)
                        self.assertAlmostEqual(sum(values), 1)
                        self.assertTrue(all(0 < value < 1 for value in values))

    def test_uniform_loss_has_known_categorical_value(self):
        spec = game("max3d")
        distribution = digit_model.fit([], spec, digit_model.CANDIDATES[0])
        result = digit_model.losses(distribution, rows_for("max3d", 1)[0]["digits"])
        self.assertAlmostEqual(result["logloss"], math.log(1000))
        self.assertAlmostEqual(result["brier"], 0.999)

    def test_each_prize_group_learns_independently(self):
        rows = rows_for("max3d")
        changed = copy.deepcopy(rows)
        for row in changed:
            row["digits"]["first"] = ["999", "999"]
        for config in digit_model.CANDIDATES[1:]:
            original = digit_model.fit(rows, game("max3d"), config)
            updated = digit_model.fit(changed, game("max3d"), config)
            self.assertNotEqual(original["first"], updated["first"])
            self.assertEqual(original["second"], updated["second"])

    def test_rolling_window_forgets_old_results(self):
        rows = rows_for("max3d", 100)
        changed = copy.deepcopy(rows)
        for row in changed[:40]:
            row["digits"]["first"] = ["999", "999"]
        config = digit_model.CANDIDATES[1]
        self.assertEqual(digit_model.fit(rows, game("max3d"), config),
                         digit_model.fit(changed, game("max3d"), config))

    def test_selection_never_receives_evaluation_draws(self):
        rows = rows_for("max3d", 24)
        with patch.object(digit_model, "select_config", wraps=digit_model.select_config) as choose:
            trained = digit_model.train(rows, game("max3d"), 42)
        selected_rows = choose.call_args.args[0]
        metrics = trained["metrics"]
        self.assertEqual(len(selected_rows), 18)
        self.assertEqual(metrics["evaluated_draws"], 6)
        self.assertEqual(metrics["selection_through"], selected_rows[-1]["draw_id"])
        self.assertLess(selected_rows[-1]["draw_id"], metrics["per_draw"][0]["draw_id"])
        for result in metrics["per_draw"]:
            self.assertLess(result["trained_through"], result["draw_id"])
        self.assertFalse(metrics["holdout"])

    def test_training_is_deterministic_and_preserves_zeroes(self):
        for code in ("max3d", "max3dpro"):
            rows = rows_for(code, 12)
            first = digit_model.train(rows, game(code), 7)
            self.assertEqual(first, digit_model.train(rows, game(code), 7))
            self.assertEqual(len(first["tickets"]), 5)
            expected_width = 2 if code == "max3dpro" else 1
            for ticket in first["tickets"]:
                self.assertEqual(len(ticket["digits"]), expected_width)
                self.assertTrue(all(len(value) == 3 and value.isascii() and value.isdigit() for value in ticket["digits"]))

    def test_later_outcomes_cannot_change_earlier_evaluation(self):
        rows = rows_for("max3d", 24)
        changed = copy.deepcopy(rows)
        changed[-1]["digits"]["first"] = ["999", "999"]
        original = digit_model.train(rows, game("max3d"), 42)
        modified = digit_model.train(changed, game("max3d"), 42)
        self.assertEqual(original["config"], modified["config"])
        self.assertEqual(original["metrics"]["per_draw"][:-1], modified["metrics"]["per_draw"][:-1])

    def test_predictable_history_can_select_nonuniform_candidate(self):
        rows = rows_for("max3d", 24)
        for row in rows:
            row["digits"] = {group: ["007"] * size for group, size in game("max3d")["digit_groups"].items()}
        model = digit_model.train(rows, game("max3d"), 4)
        self.assertNotEqual(model["config"]["kind"], "uniform")
        self.assertGreater(model["digit_probabilities"]["first"][7], 0.001)
        self.assertLess(model["metrics"]["logloss"], model["metrics"]["baseline_logloss"])

    def test_uniform_selected_when_unseen_values_defeat_frequency(self):
        rows = rows_for("max3d", 24)
        candidates = digit_model.select_config(rows, game("max3d"))[1]
        self.assertEqual(len(candidates), len(digit_model.CANDIDATES))
        self.assertEqual(digit_model.select_config(rows[:5], game("max3d"))[0]["kind"], "uniform")

    def test_rejects_partial_invalid_and_nonchronological_history(self):
        rows = rows_for("max3d", 3)
        for changed in (list(reversed(rows)), rows + [rows[-1]]):
            with self.assertRaises(ValueError):
                digit_model.train(changed, game("max3d"), 1)
        for bad in ("7", "0000", "abc", "\uff10\uff10\uff17", 7):
            changed = copy.deepcopy(rows)
            changed[0]["digits"]["first"][0] = bad
            with self.assertRaises(ValueError):
                digit_model.train(changed, game("max3d"), 1)
        with self.assertRaises(ValueError):
            digit_model.train([], game("max3d"), 1)

    def test_score_uses_only_selected_group_not_other_prizes(self):
        spec = game("max3d")
        row = rows_for("max3d", 1)[0]
        row["digits"]["first"] = ["001", "001"]
        row["digits"]["second"][0] = "999"
        tickets = [{"digits": ["001"], "group": "first"}, {"digits": ["999"], "group": "first"}]
        result = digit_model.score(digit_model.fit([], spec, digit_model.CANDIDATES[0]), tickets, row["digits"], spec)
        self.assertEqual(result["hits"], [1, 0])


class DigitAutopilotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"DATABASE_PATH": str(Path(self.directory.name) / "digit.sqlite3"),
                                                   "DISABLE_RUNNER": "1", "AUTO_SYNC": "0"})
        self.environment.start()
        initialize()
        visitors.clear()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def store(self, code, rows):
        source = game(code)["crawler"]["list_url"]
        snapshot = crawler.snapshot(code, source, b"digit test provenance")
        crawler.persist_page(code, rows, snapshot, None)

    def test_both_games_train_retrain_and_keep_version_lineage(self):
        for code in ("max3d", "max3dpro"):
            with self.subTest(code=code):
                rows = rows_for(code, 13, "2025-01-04")
                self.store(code, rows[:-1])
                first = autopilot.train(code)
                self.assertEqual(autopilot.train(code), first)
                self.store(code, [rows[-1]])
                second = autopilot.train(code)
                self.assertNotEqual(second, first)
                state = autopilot.workspace(code)
                self.assertTrue(state["supported"])
                self.assertEqual(state["kind"], "digits")
                self.assertEqual(state["model"]["parent_version_id"], first)
                self.assertEqual(state["model"]["training_count"], 13)
                self.assertEqual(state["training"]["status"], "ready")
                self.assertEqual(len(state["model_versions"]), 2)
                self.assertIsNone(state["baseline"]["jackpot_probability"])
                self.assertEqual(state["baseline"]["number_probability"], 0.001)

    def test_unverified_non_crawled_rows_are_not_training_data(self):
        row = rows_for("max3d", 1)[0]
        with connect() as connection:
            connection.execute("INSERT INTO draws VALUES(?,?,?,?,?,?,?,?,?,?)", ("max3d", row["draw_id"], row["draw_date"], row["draw_slot"], "[]", None, json.dumps(row["digits"]), row["source_url"], 0, etl.now()))
        self.assertIsNone(autopilot.train("max3d"))
        self.assertEqual(autopilot.workspace("max3d")["training"]["status"], "waiting_data")

    def test_future_draw_slots_are_excluded(self):
        rows = rows_for("max3d", 3)
        self.store("max3d", rows)
        cutoff = datetime.fromisoformat(rows[-1]["draw_date"] + "T10:00:00+00:00")
        eligible = etl.training_history("max3d", include_crawled=True, before=cutoff)
        self.assertEqual(len(eligible), 2)

    def test_failed_training_is_visible_and_recovers(self):
        self.store("max3d", rows_for("max3d", 3))
        with patch.object(digit_model, "train", side_effect=RuntimeError("training test failure")):
            with self.assertRaises(RuntimeError):
                autopilot.train("max3d")
        state = autopilot.workspace("max3d")
        self.assertEqual(state["training"]["status"], "failed")
        self.assertIn("training test failure", state["training"]["last_error"])
        autopilot.train("max3d")
        self.assertEqual(autopilot.workspace("max3d")["training"]["status"], "ready")

    def test_failed_retraining_keeps_previous_model(self):
        rows = rows_for("max3d", 4)
        self.store("max3d", rows[:-1])
        previous = autopilot.train("max3d")
        self.store("max3d", [rows[-1]])
        with patch.object(digit_model, "train", side_effect=RuntimeError("retraining failed")):
            with self.assertRaises(RuntimeError):
                autopilot.train("max3d")
        state = autopilot.workspace("max3d")
        self.assertEqual(state["model"]["id"], previous)
        self.assertEqual(state["training"]["status"], "failed")
        self.assertNotEqual(autopilot.train("max3d"), previous)

    def test_algorithm_revision_change_triggers_retraining(self):
        self.store("max3d", rows_for("max3d", 3))
        previous = autopilot.train("max3d")
        with patch.object(digit_model, "REVISION", "digit-test-next-revision"):
            self.assertNotEqual(autopilot.train("max3d"), previous)
            self.assertEqual(autopilot.workspace("max3d")["model"]["algorithm_revision"], "digit-test-next-revision")

    def test_known_model_can_be_reactivated_after_config_reverts(self):
        self.store("max3d", rows_for("max3d", 3))
        first = autopilot.train("max3d")
        with patch.object(digit_model, "REVISION", "digit-test-next-revision"):
            second = autopilot.train("max3d")
        self.assertNotEqual(first, second)
        self.assertEqual(autopilot.train("max3d"), first)
        self.assertEqual(autopilot.workspace("max3d")["model"]["id"], first)
        self.assertFalse(autopilot.workspace("max3d")["retraining_due"])

    def test_model_versions_cannot_be_rewritten(self):
        self.store("max3d", rows_for("max3d", 3))
        autopilot.train("max3d")
        for statement in ("UPDATE model_versions SET parameters='{}'", "DELETE FROM model_versions"):
            with self.assertRaises(sqlite3.IntegrityError), connect() as connection:
                connection.execute(statement)

    def test_lineage_follows_active_model_after_reactivation(self):
        rows = rows_for("max3d", 4, "2025-01-03")
        self.store("max3d", rows[:-1])
        first = autopilot.train("max3d")
        with patch.object(digit_model, "REVISION", "digit-test-next-revision"):
            autopilot.train("max3d")
        self.assertEqual(autopilot.train("max3d"), first)
        self.store("max3d", [rows[-1]])
        autopilot.train("max3d")
        self.assertEqual(autopilot.workspace("max3d")["model"]["parent_version_id"], first)

    def test_scoring_failure_does_not_stop_training(self):
        self.store("max3d", rows_for("max3d", 3))
        with patch.object(autopilot, "score_pending", side_effect=ValueError("bad score")):
            autopilot.tick()
        self.assertIsNotNone(autopilot.workspace("max3d")["model"])

    def test_automatic_lock_requires_current_digit_model(self):
        self.store("max3d", rows_for("max3d", 3))
        target = autopilot.next_slot(game("max3d"))
        with self.assertRaisesRegex(ValueError, "stale"):
            ledger.lock("max3d", 4, target, "bayesian", 5, include_crawled=True)
        with connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM predictions").fetchone()[0], 0)

    def test_changed_data_during_training_does_not_publish_stale_model(self):
        rows = rows_for("max3d", 4)
        self.store("max3d", rows[:-1])
        original = digit_model.train

        def concurrent_update(*args):
            result = original(*args)
            self.store("max3d", [rows[-1]])
            return result

        with patch.object(digit_model, "train", side_effect=concurrent_update):
            self.assertIsNone(autopilot.train("max3d"))
        self.assertIsNone(autopilot.workspace("max3d")["model"])
        self.assertEqual(autopilot.workspace("max3d")["training"]["status"], "pending")
        self.assertIsNotNone(autopilot.train("max3d"))

    def test_full_prediction_score_and_relearning_cycle(self):
        for code in ("max3d", "max3dpro"):
            with self.subTest(code=code):
                current = datetime(2025, 1, 2, 10, tzinfo=timezone.utc)

                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return current.astimezone(tz) if tz else current.replace(tzinfo=None)

                rows = rows_for(code, 12, "2025-01-01")
                self.store(code, rows)
                with patch.object(autopilot, "datetime", Clock), patch.object(etl, "datetime", Clock), patch.object(ledger, "datetime", Clock):
                    autopilot.tick()
                    state = autopilot.workspace(code)
                    first = state["model"]["id"]
                    self.assertEqual(len(state["predictions"]), 1)
                    prediction = state["predictions"][0]
                    original_hash = prediction["payload_hash"]
                    self.assertEqual(prediction["model_version_id"], first)
                    self.assertEqual(prediction["payload"]["kind"], "digits")
                    target = datetime.fromisoformat(prediction["target_at"])
                    current = target + timedelta(minutes=1)
                    local = target.astimezone(autopilot.ZoneInfo(game(code)["timezone"]))
                    actual = copy.deepcopy(rows[-1])
                    actual.update(draw_id=rows[-1]["draw_id"] + 1, draw_date=local.date().isoformat(), draw_slot=local.strftime("%H:%M"))
                    group = digit_model.target_group(game(code))
                    values = prediction["payload"]["tickets"][0]["digits"]
                    actual["digits"][group] = values + ["999"] * (game(code)["digit_groups"][group] - len(values))
                    self.store(code, [actual])
                    autopilot.tick()
                    state = autopilot.workspace(code)
                    self.assertNotEqual(state["model"]["id"], first)
                    self.assertEqual(state["model"]["parent_version_id"], first)
                    scored = next(item for item in state["predictions"] if item["id"] == prediction["id"])
                    self.assertEqual(scored["payload_hash"], original_hash)
                    self.assertEqual(scored["score"]["hits"][0], len(values))
                    self.assertFalse(scored["score"]["source_verified"])
                    self.assertEqual(ledger.score_pending()["scored"], 0)
                    with connect() as connection:
                        action = connection.execute("SELECT action FROM feedback_events WHERE prediction_id=?", (prediction["id"],)).fetchone()[0]
                        self.assertEqual(action, "eligible_for_retraining")

    def test_api_exposes_digit_models_and_learning_state(self):
        self.store("max3d", rows_for("max3d", 3))
        autopilot.train("max3d")
        with TestClient(app) as client:
            response = client.get("/api/games/max3d/workspace")
            self.assertEqual(response.status_code, 200)
            state = response.json()
            self.assertEqual(state["model"]["kind"], "digits")
            self.assertEqual(state["training"]["status"], "ready")
            self.assertEqual(len(state["model"]["digit_probabilities"]["first"]), 1000)
            page = client.get("/?code=max3d").text
            self.assertIn('id="digit-group"', page)
            self.assertIn('id="model-learning"', page)
