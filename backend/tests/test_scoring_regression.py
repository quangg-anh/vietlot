import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app import autopilot, crawler, ledger
from backend.app.db import connect, encode, initialize
from backend.app.games import game

FIXTURES = Path(__file__).with_name("fixtures")


class ScoringRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "DATABASE_PATH": str(Path(self.directory.name) / "test.sqlite3"),
            "DISABLE_RUNNER": "1",
        })
        self.environment.start()
        initialize()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def seed(self, target_at="2026-10-21T13:00:00+07:00", special=True, locked="2026-10-20T22:14:00+07:00"):
        spec = game("lotto535")
        url = spec["crawler"]["latest_url"]
        raw = (FIXTURES / "lotto535_minhchinh_source.html").read_bytes()
        rows, _ = crawler.parse_results(raw, spec | {"crawler": spec["crawler"] | {"source": "minhchinh"}}, url)
        snapshot = crawler.snapshot("lotto535", url, raw, source="minhchinh")
        crawler.persist_page("lotto535", rows, snapshot, None)
        mains = [[2, 12, 14, 18, 25], [8, 14, 26, 30, 34], [9, 14, 22, 26, 34], [7, 9, 17, 28, 35], [9, 11, 13, 20, 35]]
        tickets = [{"main": main, **({"special": number} if special else {})} for main, number in zip(mains, [11, 9, 7, 9, 1])]
        payload = encode({"data_policy": "verified_or_crawled", "probabilities": [5 / 35] * 35, "tickets": tickets})
        with connect() as connection:
            version = connection.execute("INSERT INTO model_versions(game_code,method,parameters,created_at) VALUES('lotto535','uniform','{}',?)", (locked,)).lastrowid
            identity = connection.execute("INSERT INTO predictions(game_code,target_draw_id,target_at,model_version_id,payload,payload_hash,locked_at) VALUES(?,?,?,?,?,?,?)", ("lotto535", 959, target_at, version, payload, ledger.digest("lotto535", 959, version, payload, locked), locked)).lastrowid
        return identity

    def score(self):
        with patch.object(ledger, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 22, tzinfo=timezone.utc)
            return ledger.score_pending("lotto535")

    def test_draw_959_main_and_special_are_independent(self):
        self.seed()
        self.assertEqual(self.score(), {"scored": 1})
        self.assertEqual(self.score(), {"scored": 0})
        with connect() as connection:
            result = json.loads(connection.execute("SELECT result FROM scores").fetchone()[0])
            self.assertEqual(connection.execute("SELECT count(*) FROM feedback_events").fetchone()[0], 1)
        self.assertEqual(result["hits"], [1, 3, 2, 0, 1])
        self.assertEqual(result["special_hits"], [True, False, False, False, False])
        self.assertEqual(result["matched_numbers"], [[14], [14, 30, 34], [14, 34], [], [20]])
        self.assertEqual(autopilot.workspace("lotto535")["predictions"][0]["scoring_status"], "scored")

    def test_missing_special_is_unknown_not_a_miss(self):
        self.seed(special=False)
        self.assertEqual(self.score(), {"scored": 1})
        with connect() as connection:
            result = json.loads(connection.execute("SELECT result FROM scores").fetchone()[0])
        self.assertEqual(result["special_hits"], [None] * 5)

    def test_crawl_immediately_scores_pending_prediction(self):
        self.seed()
        fetcher = Mock()
        fetcher.fetch.return_value = (FIXTURES / "lotto535_minhchinh_source.html").read_bytes()
        job = crawler.queue_crawl("lotto535")
        with patch.object(ledger, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 22, tzinfo=timezone.utc)
            result = crawler.run_crawl({"game": "lotto535", "mode": "latest", "pages": 1}, job["job_id"], fetcher=fetcher)
        self.assertEqual(result["scored"], 1)
        self.assertNotIn("scoring_errors", result)

    def test_all_games_fall_back_when_minhchinh_is_blocked(self):
        for code in ("lotto535", "mega645", "power655", "max3d", "max3dpro"):
            with self.subTest(code=code):
                spec = game(code)
                legacy = {"power655": "power_results.html", "mega645": "mega_results.html"}.get(code, code + "_source.html")
                fetcher = Mock()
                fetcher.fetch.side_effect = [b"<html>Checking access</html>", (FIXTURES / legacy).read_bytes()]
                job = crawler.queue_crawl(code)
                result = crawler.run_crawl({"game": code, "mode": "latest", "pages": 1}, job["job_id"], fetcher=fetcher)
                self.assertEqual(result["source"], spec["crawler"]["source"])
                self.assertIn("minhchinh", result["source_warning"])
                self.assertGreater(result["added"], 0)
                self.assertEqual(fetcher.fetch.call_args_list[1].args[0], spec["crawler"]["list_url"])

    def test_wrong_slot_is_blocked_and_exposed(self):
        self.seed(target_at="2026-10-20T21:00:00+07:00", locked="2026-10-19T22:14:00+07:00")
        result = self.score()
        self.assertEqual(result["scored"], 0)
        self.assertEqual(result["errors"][0]["reason"], "result_slot_mismatch")
        state = autopilot.workspace("lotto535")
        self.assertEqual(state["predictions"][0]["scoring_status"], "result_slot_mismatch")

    def test_all_minhchinh_game_layouts(self):
        for code in ("lotto535", "mega645", "power655", "max3d", "max3dpro"):
            with self.subTest(code=code):
                spec = game(code)
                raw = (FIXTURES / (code + "_minhchinh_source.html")).read_bytes()
                rows, incomplete = crawler.parse_results(raw, spec | {"crawler": spec["crawler"] | {"source": "minhchinh"}}, spec["crawler"]["latest_url"])
                self.assertGreater(len(rows), 0)
                self.assertEqual(incomplete, 0)
                self.assertTrue(all(not row["verified"] for row in rows))
