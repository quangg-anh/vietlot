import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import analytics, etl, jobs, ledger
from backend.app.db import connect, encode, initialize
from backend.app.games import game, games
from backend.app.main import app


def csv_bytes(rows):
    buffer = io.StringIO()
    fields = ["game_code", "draw_id", "draw_date", "draw_slot", "main", "special", "digits", "source_url", "verified"]
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode()


def draw(identity=1, day="2025-01-02", verified="true"):
    return dict(game_code="power655", draw_id=str(identity), draw_date=day, draw_slot="18:00",
                main="1 2 3 4 5 6", special="7", digits="", source_url="https://vietlott.vn", verified=verified)


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "DATABASE_PATH": str(Path(self.directory.name) / "test.sqlite3"),
            "API_KEY": "test-key-not-for-production", "DISABLE_RUNNER": "1",
        })
        self.environment.start()
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.headers = {"X-API-Key": "test-key-not-for-production"}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.environment.stop()
        self.directory.cleanup()

    def seed(self, count=24):
        rows = []
        current = date(2024, 1, 1)
        while len(rows) < count:
            if current.weekday() in game("power655")["weekdays"]:
                rows.append(draw(len(rows) + 1, current.isoformat()))
            current += timedelta(days=1)
        etl.import_csv(csv_bytes(rows))

    def test_configs_and_storage(self):
        self.assertEqual(len(games()), 5)
        initialize()
        with connect() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_only_verified_data_public_and_used(self):
        etl.import_csv(csv_bytes([draw(1), draw(2, "2025-01-04", "false")]))
        self.assertEqual(len(etl.history("power655")), 1)
        response = self.client.get("/api/games/power655/draws").json()
        self.assertEqual(len(response["items"]), 1)
        self.assertEqual(self.client.get("/api/games/power655/summary").json()["verified_ratio"], 0.5)
        self.assertEqual(self.client.get("/api/games/power655/stats/frequency").json()["items"][0]["count"], 1)

    def test_import_is_atomic(self):
        invalid = draw(2, "2025-01-04") | {"main": "1 1 2 3 4 5"}
        with self.assertRaises(ValueError):
            etl.import_csv(csv_bytes([draw(), invalid]))
        self.assertEqual(etl.history("power655"), [])

    def test_database_conflict_rolls_back_whole_batch(self):
        with self.assertRaises(sqlite3.IntegrityError):
            etl.import_csv(csv_bytes([draw(), draw(2)]))
        self.assertEqual(etl.history("power655"), [])

    def test_schedule_numbers_source_and_status_validation(self):
        invalids = [
            draw() | {"main": "0 2 3 4 5 6"},
            draw() | {"main": "1 2 3 4 5 56"},
            draw() | {"special": "1"}, draw() | {"special": ""},
            draw() | {"draw_date": "2025-01-03"}, draw() | {"draw_slot": "12:00"},
            draw() | {"source_url": "javascript:alert(1)"}, draw() | {"verified": "yes"},
            draw() | {"draw_id": "0"}, draw() | {"draw_date": "2099-01-01"},
        ]
        for item in invalids:
            with self.subTest(item=item), self.assertRaises(ValueError):
                etl.validate(item)

    def test_duplicate_ids_and_backwards_history(self):
        with self.assertRaises(ValueError):
            etl.import_csv(csv_bytes([draw(), draw()]))
        with self.assertRaises(ValueError):
            etl.import_csv(csv_bytes([draw(2), draw(1, "2025-01-04")]))
        self.assertEqual(etl.history("power655"), [])

    def test_draft_games_fail_closed(self):
        with self.assertRaises(ValueError):
            etl.validate(draw() | {"game_code": "lotto535"})
        response = self.client.post("/api/funds", headers=self.headers, json={"game": "power655"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/games/power655/ev").status_code, 409)

    def test_verified_draw_cannot_be_replaced_or_downgraded(self):
        etl.import_csv(csv_bytes([draw()]))
        etl.import_csv(csv_bytes([draw()]))
        for changed in [draw() | {"special": 8}, draw(verified="false")]:
            with self.assertRaises(ValueError):
                etl.import_csv(csv_bytes([changed]))

    def test_unverified_promotion_has_audit(self):
        etl.import_csv(csv_bytes([draw(verified="false")]))
        etl.import_csv(csv_bytes([draw()]))
        with connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM audit_log WHERE action='draw_update'").fetchone()[0], 1)

    def test_missing_ranges(self):
        etl.import_csv(csv_bytes([draw(), draw(5, "2025-01-04")]))
        self.assertEqual(etl.gaps("power655"), [{"from": 2, "to": 4}])

    def test_legacy_csv(self):
        raw = b"draw_id,draw_date,n1,n2,n3,n4,n5,n6,special,source_url,verified\n1,2025-01-02,1,2,3,4,5,6,7,https://vietlott.vn,true\n"
        etl.import_csv(raw)
        self.assertEqual(etl.history("power655")[0]["main"], [1, 2, 3, 4, 5, 6])

    def test_freeze_is_immutable(self):
        with self.assertRaises(ValueError):
            etl.freeze("power655")
        self.seed()
        frozen = etl.freeze("power655")
        with connect() as connection:
            row = connection.execute("SELECT * FROM datasets WHERE id=?", (frozen["id"],)).fetchone()
            self.assertEqual(hashlib.sha256(row["payload"].encode()).hexdigest(), frozen["sha256"])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE datasets SET payload='[]'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM datasets")

    def test_probabilities_are_inclusion_probabilities(self):
        self.seed()
        for spec in [game("power655"), game("mega645")]:
            for method in analytics.METHODS:
                probability = analytics.probabilities(etl.history("power655"), spec, method)
                self.assertAlmostEqual(sum(probability), spec["pick_count"])
                self.assertTrue(all(0 < value < 1 for value in probability))
                tickets = analytics.sample_tickets(probability, spec, 10, 42)
                for ticket in tickets:
                    self.assertEqual(len(set(ticket["main"])), spec["pick_count"])
                    self.assertNotIn(ticket["special"], ticket["main"])
        with self.assertRaises(ValueError):
            analytics.probabilities([], game("max3d"))
        with self.assertRaises(ValueError):
            analytics.probabilities([], game("power655"), "xgboost")

    def test_walk_forward_never_sees_future(self):
        self.seed()
        rows = etl.history("power655")
        original = analytics.probabilities
        observed = []
        def checked(history, spec, method):
            if history:
                observed.append(history[-1]["draw_id"])
            return original(history, spec, method)
        with patch.object(analytics, "probabilities", side_effect=checked):
            result = analytics.backtest(rows, game("power655"), 10, 5)
        self.assertEqual(observed, [19, 20, 21, 22, 23])
        self.assertEqual(result["evaluated_draws"], 5)
        self.assertFalse(result["holdout"])
        self.assertEqual(result, analytics.backtest(rows, game("power655"), 10, 5))
        with self.assertRaises(ValueError):
            analytics.backtest([], game("power655"))

    def test_queue_recovers_and_uses_frozen_input(self):
        self.seed()
        response = self.client.post("/api/backtest", headers=self.headers,
                                    json={"game": "power655", "min_train": 10, "test_draws": 5})
        self.assertEqual(response.status_code, 202)
        identity = response.json()["job_id"]
        with connect() as connection:
            connection.execute("UPDATE jobs SET status='running' WHERE id=?", (identity,))
        jobs.recover()
        self.assertEqual(self.client.get(f"/api/jobs/{identity}").json()["status"], "queued")
        self.assertTrue(jobs.run_one())
        self.assertFalse(jobs.run_one())
        status = self.client.get(f"/api/jobs/{identity}").json()
        self.assertEqual(status["status"], "done")
        self.assertIn("dataset_hash", status["result"])
        self.assertEqual(self.client.get(f"/api/backtest/{identity}").status_code, 200)

    def test_failed_job_is_persisted(self):
        identity = jobs.enqueue("backtest", {})
        jobs.run_one()
        result = self.client.get(f"/api/jobs/{identity}").json()
        self.assertEqual(result["status"], "failed")
        self.assertIsNotNone(result["finished_at"])

    def test_ledger_deadline_hash_and_immutability(self):
        self.seed()
        spec = game("power655") | {"rules_verified": True}
        target = datetime.now(timezone.utc) + timedelta(days=7)
        while target.weekday() not in spec["weekdays"]:
            target += timedelta(days=1)
        target = target.replace(hour=11, minute=0, second=0, microsecond=0)
        with patch.object(ledger, "game", return_value=spec):
            for invalid in ["2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00"]:
                with self.assertRaises(ValueError):
                    ledger.lock("power655", 25, invalid, "uniform", 1)
            with self.assertRaises(ValueError):
                ledger.lock("power655", 24, target.isoformat(), "uniform", 1)
            result = ledger.lock("power655", 25, target.isoformat(), "uniform", 2)
        self.assertEqual(len(result["payload"]["tickets"]), 2)
        with connect() as connection:
            row = connection.execute("SELECT * FROM predictions").fetchone()
            self.assertEqual(row["payload_hash"], ledger.digest("power655", 25, row["model_version_id"], row["payload"], row["locked_at"]))
            for statement in ["UPDATE predictions SET target_draw_id=999", "DELETE FROM predictions"]:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)
        self.assertEqual(len(self.client.get("/api/ledger").json()["items"]), 1)

    def test_scoring_idempotent_and_feedback_not_auto_promoted(self):
        self.seed()
        payload = encode({"probabilities": analytics.probabilities([], game("power655"), "uniform"),
                          "tickets": [{"main": [1, 2, 3, 4, 5, 6], "special": 7}]})
        locked = "2023-01-01T00:00:00+00:00"
        with connect() as connection:
            version = connection.execute("INSERT INTO model_versions(game_code,method,parameters,created_at) VALUES('power655','uniform','{}',?)", (locked,)).lastrowid
            connection.execute("INSERT INTO predictions(game_code,target_draw_id,target_at,model_version_id,payload,payload_hash,locked_at) VALUES(?,?,?,?,?,?,?)",
                               ("power655", 1, "2024-01-02T11:00:00+00:00", version, payload, ledger.digest("power655", 1, version, payload, locked), locked))
        self.assertEqual(ledger.score_pending(), {"scored": 1})
        self.assertEqual(ledger.score_pending(), {"scored": 0})
        feedback = self.client.get(f"/api/models/{version}/feedback").json()
        self.assertEqual(feedback[0]["action"], "skipped")
        with connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM scores")

    def test_api_security_errors_and_html(self):
        response = self.client.post("/api/datasets/freeze", json={"game": "power655"})
        self.assertEqual(response.status_code, 401)
        with patch.dict(os.environ, {"API_KEY": "REPLACE_ME"}):
            self.assertEqual(self.client.post("/api/datasets/freeze", headers=self.headers, json={"game": "power655"}).status_code, 503)
        self.assertEqual(self.client.get("/api/jobs/999").status_code, 404)
        self.assertEqual(self.client.get("/api/backtest/999").status_code, 404)
        self.assertEqual(self.client.post("/api/backtest", headers=self.headers, json={"min_train": -1}).status_code, 422)
        self.assertEqual(self.client.get("/api/games/unknown/summary").status_code, 400)
        for code in games():
            response = self.client.get(f"/?code={code}")
            self.assertEqual(response.status_code, 200)
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(self.client.get("/static/app.css").status_code, 200)
        self.assertEqual(self.client.get("/docs").status_code, 200)
        self.assertIn("/api/backtest", self.client.get("/openapi.json").json()["paths"])

    def test_upload_limit_and_extensions(self):
        self.assertEqual(self.client.post("/api/import/csv", headers=self.headers, files={"file": ("bad.txt", b"bad")}).status_code, 400)
        result = self.client.post("/api/import/csv", headers=self.headers, files={"file": ("draws.csv", csv_bytes([draw()]))})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["processed"], 1)
        self.assertEqual(self.client.post("/api/import/csv", headers=self.headers, files={"file": ("bad.csv", b"bad")}).status_code, 400)

    def test_lab_and_illusion(self):
        self.seed()
        from backend.app.lab import illusion, randomness
        result = randomness(etl.history("power655"), game("power655"), simulations=20)
        self.assertEqual(result["tested"], 2)
        self.assertTrue(all(0 <= item["p_adjusted"] <= 1 for item in result["tests"]))
        simulated = illusion(game("power655"), 20)
        self.assertEqual(sum(item["count"] for item in simulated["frequency"]), 20 * game("power655")["pick_count"])
        with self.assertRaises(ValueError):
            randomness([], game("power655"))

    def test_backup_restores_verified_rows(self):
        from backend.app.backup import backup
        self.seed()
        target = Path(self.directory.name) / "snapshot.sqlite3"
        self.assertEqual(backup(target), target)
        with closing(sqlite3.connect(target)) as restored:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(restored.execute("SELECT count(*) FROM draws WHERE verified=1").fetchone()[0], 24)
        with self.assertRaises(ValueError):
            backup(target)
        with self.assertRaises(ValueError):
            backup(os.environ["DATABASE_PATH"])

    def test_malformed_csv_and_empty_csv(self):
        for raw in [b"draw_id,draw_date\n", b"draw_id,draw_date\n1\n", b"draw_id,draw_date\n1,2025-01-02,extra\n", b"\xff\xfe"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                etl.import_csv(raw)

    def test_digit_validation_preserves_leading_zeroes(self):
        spec = game("max3d") | {"import_enabled": True, "digit_groups": {"first": 2}}
        item = draw(day="2025-01-03") | {"game_code": "max3d", "digits": '{"first":["001","099"]}'}
        with patch.object(etl, "game", return_value=spec):
            self.assertEqual(etl.validate(item)["digits"]["first"][0], "001")
            for invalid in ['{"first":[1,"099"]}', '{"first":["1","099"]}', '{"first":["001"]}']:
                with self.assertRaises(ValueError):
                    etl.validate(item | {"digits": invalid})

    def test_fund_controls_share_policy_and_balance(self):
        from backend.app import funds
        spec = game("power655") | {"rules_verified": True, "ticket_price": 10000, "prizes": {"3": 10000}}
        policy = {"tickets_per_draw": 1, "max_fraction": 0.01, "max_daily_cost": 100000}
        with patch.object(funds, "game", return_value=spec):
            created = funds.create_funds("power655", "bayesian", 1000000, policy)
        self.assertEqual(len(created["fund_ids"]), 3)
        with connect() as connection:
            rows = connection.execute("SELECT * FROM sim_funds ORDER BY id").fetchall()
        self.assertEqual([row["owner_name"] for row in rows], ["bayesian", "random", "no_bet"])
        self.assertEqual(len({row["group_id"] for row in rows}), 1)
        self.assertTrue(all(row["balance"] == 1000000 and json.loads(row["policy"]) == policy for row in rows))
        response = self.client.post("/api/funds", headers=self.headers,
                                    json={"game": "power655", "policy": {"max_fraction": 1}})
        self.assertEqual(response.status_code, 422)

    def test_runner_start_stop_and_score_job(self):
        runner = jobs.Runner()
        runner.start()
        runner.close()
        self.assertFalse(runner.thread.is_alive())
        identity = jobs.enqueue("score", {})
        while jobs.run_one():
            pass
        self.assertEqual(self.client.get(f"/api/jobs/{identity}").json()["status"], "done")


if __name__ == "__main__":
    unittest.main()
