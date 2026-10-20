import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import autopilot, crawler, etl, jobs, ledger
from backend.app.db import connect, encode, initialize
from backend.app.games import game
from backend.app.main import app, visitors
from backend.tests.test_research import csv_bytes, draw
from backend.tests.test_crawler import POWER, SOURCE, older_power


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "DATABASE_PATH": str(Path(self.directory.name) / "test.sqlite3"),
            "API_KEY": "autopilot-test-key", "DISABLE_RUNNER": "1", "AUTO_SYNC": "1",
        })
        self.environment.start()
        initialize()
        visitors.clear()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def seed(self, count=24):
        current = datetime(2024, 1, 1).date()
        rows = []
        while len(rows) < count:
            if current.weekday() in game("power655")["weekdays"]:
                rows.append(draw(len(rows) + 1, current.isoformat()))
            current += timedelta(days=1)
        etl.import_csv(csv_bytes(rows))

    def test_default_sync_enabled_without_resetting_failure_backoff(self):
        autopilot.enable_sync()
        with connect() as connection:
            states = connection.execute("SELECT * FROM crawl_state").fetchall()
            self.assertEqual({row["game_code"] for row in states}, {"power655", "mega645", "lotto535", "max3d", "max3dpro"})
            self.assertTrue(all(row["scheduled"] for row in states))
            connection.execute("UPDATE crawl_state SET next_due='2099-01-01',failures=3 WHERE game_code='power655'")
        autopilot.enable_sync()
        self.assertEqual(crawler.status("power655")["state"]["next_due"], "2099-01-01")
        self.assertEqual(crawler.status("power655")["state"]["failures"], 3)

    def test_auto_sync_can_be_disabled_in_environment(self):
        with patch.dict(os.environ, {"AUTO_SYNC": "0"}):
            autopilot.enable_sync()
        self.assertIsNone(crawler.status("power655")["state"])

    def test_disabled_startup_stops_existing_schedule(self):
        crawler.set_schedule("power655", True)
        with patch.dict(os.environ, {"AUTO_SYNC": "0"}):
            autopilot.enable_sync()
        self.assertFalse(crawler.status("power655")["state"]["scheduled"])

    def test_runner_calls_automatic_model_cycle(self):
        runner = jobs.Runner()
        with patch.object(jobs, "queue_due"), patch.object(jobs, "run_one", return_value=False), patch.object(runner.stop, "wait", return_value=False), patch.object(autopilot, "tick", side_effect=runner.stop.set) as update:
            runner.model_loop()
        update.assert_called_once_with()

    def test_scheduler_automatically_backfills_history(self):
        crawler.set_schedule("power655", True)
        crawler.queue_due()
        with connect() as connection:
            job = connection.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(json.loads(job["payload"]), {"game": "power655", "mode": "history", "pages": 20})
        with connect() as connection:
            connection.execute("UPDATE jobs SET status='done'")
            connection.execute("UPDATE crawl_state SET history_complete=1")
        crawler.queue_due()
        with connect() as connection:
            latest = connection.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(json.loads(latest["payload"])["mode"], "latest")

    def test_no_training_from_unverified_draws(self):
        etl.import_csv(csv_bytes([draw(verified="false")]))
        self.assertIsNone(autopilot.train("power655"))
        state = autopilot.workspace("power655")
        self.assertIsNone(state["model"])
        self.assertEqual(state["counts"], {"total": 1, "verified": 0, "pending": 1})
        self.assertEqual(len(state["history"]), 1)
        self.assertFalse(state["history"][0]["verified"])

    def add_crawled(self, raw=None):
        raw = raw or older_power()
        rows, _ = crawler.parse_results(raw, game("power655"), SOURCE)
        snapshot = crawler.snapshot("power655", SOURCE, raw)
        crawler.persist_page("power655", rows, snapshot, None)
        return rows

    def test_crawled_results_train_without_manual_verification(self):
        self.add_crawled()
        autopilot.tick()
        state = autopilot.workspace("power655")
        self.assertEqual(state["usable_count"], 2)
        self.assertEqual(state["counts"]["verified"], 0)
        self.assertEqual(state["model"]["training_count"], 2)
        self.assertEqual(state["model"]["unverified_training_count"], 2)
        self.assertEqual(state["model"]["data_policy"], "verified_or_crawled")
        self.assertTrue(all(row["crawled"] for row in state["history"]))
        self.assertEqual(len(etl.history("power655")), 0)
        self.assertEqual(len(etl.history("power655", include_crawled=True)), 2)
        first = state["model"]["id"]
        autopilot.tick()
        self.assertEqual(autopilot.workspace("power655")["model"]["id"], first)

    def test_fresh_crawled_history_predicts_without_rules_review(self):
        spec = game("power655")
        self.assertFalse(spec["rules_verified"])
        target = datetime.fromisoformat(autopilot.next_slot(spec))
        previous = target.date() - timedelta(days=1)
        while previous.weekday() not in spec["weekdays"]:
            previous -= timedelta(days=1)
        older = previous - timedelta(days=1)
        while older.weekday() not in spec["weekdays"]:
            older -= timedelta(days=1)
        raw = POWER.replace(b"20/10/2026", b"LATEST_DATE").replace(b"17/10/2026", b"OLDER_DATE")
        raw = raw.replace(b"LATEST_DATE", previous.strftime("%d/%m/%Y").encode()).replace(b"OLDER_DATE", older.strftime("%d/%m/%Y").encode())
        self.add_crawled(raw)
        autopilot.tick()
        autopilot.tick()
        with connect() as connection:
            predictions = connection.execute("SELECT * FROM predictions").fetchall()
        self.assertEqual(len(predictions), 1)
        self.assertEqual(predictions[0]["target_draw_id"], 1414)
        payload = json.loads(predictions[0]["payload"])
        self.assertFalse(payload["rules_verified"])
        self.assertEqual(payload["data_policy"], "verified_or_crawled")
        self.assertEqual(payload["unverified_training_count"], 2)

    def test_source_results_score_automatic_but_not_verified_only_predictions(self):
        self.add_crawled()
        locked = "2026-10-14T00:00:00+00:00"
        with connect() as connection:
            version = connection.execute("INSERT INTO model_versions(game_code,method,parameters,created_at) VALUES('power655','bayesian','{}',?)", (locked,)).lastrowid
            for policy in ("verified_or_crawled", "verified_only"):
                payload = encode({"data_policy": policy, "probabilities": [6 / 55] * 55, "tickets": [{"main": [13, 20, 23, 30, 50, 55]}]})
                connection.execute("INSERT INTO predictions(game_code,target_draw_id,target_at,model_version_id,payload,payload_hash,locked_at) VALUES(?,?,?,?,?,?,?)",
                                   ("power655", 1411, "2026-10-15T11:00:00+00:00", version, payload, ledger.digest("power655", 1411, version, payload, locked), locked))
        self.assertEqual(ledger.score_pending()["scored"], 1)
        self.assertEqual(ledger.score_pending()["scored"], 0)
        with connect() as connection:
            scored = connection.execute("SELECT result FROM scores").fetchall()
        result = json.loads(scored[0]["result"])
        self.assertFalse(result["source_verified"])
        self.assertEqual(result["data_policy"], "verified_or_crawled")
        self.assertEqual(result["hits"], [6])

    def test_training_is_automatic_idempotent_and_evaluated(self):
        self.seed()
        autopilot.tick()
        autopilot.tick()
        state = autopilot.workspace("power655")
        model = state["model"]
        self.assertEqual(model["training_count"], 24)
        self.assertEqual(len(model["tickets"]), 5)
        self.assertEqual(len(model["probabilities"]), 55)
        self.assertEqual(model["metrics"]["evaluated_draws"], 14)
        self.assertTrue(model["preview_only"])
        with connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM model_versions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM predictions").fetchone()[0], 0)

    def test_training_does_not_wait_for_blocked_crawler(self):
        crawling = threading.Event()
        release = threading.Event()
        trained = threading.Event()

        def blocked_crawl(stop):
            crawling.set()
            release.wait(5)
            return True

        runner = jobs.Runner()
        with patch.object(jobs, "recover"), patch.object(autopilot, "enable_sync"), \
                patch.object(jobs, "queue_due"), patch.object(jobs, "enqueue"), \
                patch.object(jobs, "run_one", side_effect=blocked_crawl), \
                patch.object(autopilot, "tick", side_effect=trained.set):
            runner.start()
            try:
                self.assertTrue(crawling.wait(2))
                self.assertTrue(trained.wait(2))
                self.assertFalse(release.is_set())
            finally:
                release.set()
                runner.close()
        self.assertFalse(runner.model_thread.is_alive())

    def test_training_refreshes_after_verified_data_change(self):
        etl.import_csv(csv_bytes([draw()]))
        first = autopilot.train("power655")
        etl.import_csv(csv_bytes([draw(2, "2025-01-04", "false")]))
        self.assertEqual(autopilot.train("power655"), first)
        etl.import_csv(csv_bytes([draw(2, "2025-01-04")]))
        self.assertNotEqual(autopilot.train("power655"), first)
        self.assertEqual(autopilot.workspace("power655")["model"]["training_count"], 2)

    def test_old_history_cannot_create_future_prediction(self):
        self.seed()
        with patch.object(autopilot, "game", return_value=game("power655") | {"rules_verified": True}):
            autopilot.predict_next("power655")
        with connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM predictions").fetchone()[0], 0)

    def test_fresh_history_automatically_locks_once(self):
        spec = game("power655") | {"rules_verified": True}
        target = datetime.fromisoformat(autopilot.next_slot(spec))
        previous = target.date() - timedelta(days=1)
        while previous.weekday() not in spec["weekdays"]:
            previous -= timedelta(days=1)
        etl.import_csv(csv_bytes([draw(100, previous.isoformat())]))
        with patch.object(autopilot, "game", return_value=spec), patch("backend.app.ledger.game", return_value=spec):
            autopilot.predict_next("power655")
            autopilot.predict_next("power655")
            repeated = ledger.lock("power655", 101, target.isoformat(), "bayesian", 5, once=True)
        with connect() as connection:
            stored = connection.execute("SELECT * FROM predictions").fetchall()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["target_draw_id"], 101)
        self.assertEqual(stored[0]["id"], repeated["id"])
        self.assertLess(stored[0]["locked_at"], stored[0]["target_at"])

    def test_workspace_api_pagination_unsupported_game_and_sync_auth(self):
        self.seed()
        with TestClient(app) as client:
            state = client.get("/api/games/power655/workspace").json()
            self.assertEqual(len(state["history"]), 20)
            self.assertEqual(state["baseline"]["combinations"], 28989675)
            second = client.get(f"/api/games/power655/workspace?before={state['next_before']}").json()
            self.assertEqual(len(second["history"]), 4)
            self.assertIsNone(second["next_before"])
            self.assertEqual(client.get("/api/games/unknown/workspace").status_code, 400)
            self.assertTrue(client.get("/api/games/max3d/workspace").json()["supported"])
            self.assertEqual(client.post("/api/games/power655/sync").status_code, 401)
            headers = {"X-API-Key": "autopilot-test-key"}
            response = client.post("/api/games/power655/sync", headers=headers)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(client.post("/api/games/power655/sync", headers=headers).json()["job_id"], response.json()["job_id"])
            self.assertTrue(crawler.status("power655")["state"]["scheduled"])
            self.assertEqual(client.post("/api/games/max3d/sync", headers=headers).status_code, 202)

    def test_dashboard_has_no_manual_training_controls(self):
        with TestClient(app) as client:
            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn('id="sync-button"', page.text)
            self.assertIn('id="probability-grid"', page.text)
            self.assertNotIn('id="backtest-form"', page.text)
            for asset in ("dashboard.css", "dashboard.js", "theme.js", "theme-boot.js"):
                self.assertEqual(client.get(f"/static/{asset}").status_code, 200)

    def test_history_scores_include_predictions_older_than_latest_twenty(self):
        self.seed()
        with connect() as connection:
            version = connection.execute("INSERT INTO model_versions(game_code,method,parameters,created_at) VALUES('power655','bayesian','{}','2023-01-01')").lastrowid
            for identity in range(1, 25):
                prediction = connection.execute("INSERT INTO predictions(game_code,target_draw_id,target_at,model_version_id,payload,payload_hash,locked_at) VALUES(?,?,?,?,?,?,?)",
                                                ("power655", identity, "2024-01-01T11:00:00+00:00", version, encode({"tickets": []}), f"test-{identity}", "2023-12-01T00:00:00+00:00")).lastrowid
                connection.execute("INSERT INTO scores VALUES(?,?,?)", (prediction, encode({"hits": [3]}), "2024-03-01"))
        state = autopilot.workspace("power655", before=5)
        self.assertEqual(len(state["predictions"]), 20)
        self.assertEqual(len(state["history"]), 4)
        for record in state["history"]:
            self.assertEqual(record["predictions"][0]["score"]["hits"], [3])


if __name__ == "__main__":
    unittest.main()
