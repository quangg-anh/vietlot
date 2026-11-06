import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app import crawler, ledger
from backend.app.crawl_http import CrawlError
from backend.app.db import connect, encode, initialize
from backend.app.games import game
from backend.app.inspect_draw import inspect_draw

FIXTURES = Path(__file__).with_name("fixtures")


class DrawIdentityTests(unittest.TestCase):
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

    def seed(self, code="mega645"):
        spec = game(code)
        raw = (FIXTURES / (code + "_minhchinh_source.html")).read_bytes()
        url = spec["crawler"]["latest_url"]
        rows, _ = crawler.parse_results(raw, spec | {"crawler": spec["crawler"] | {"source": "minhchinh"}}, url)
        snapshot = crawler.snapshot(code, url, raw, source="minhchinh")
        row = rows[0]
        crawler.persist_page(code, [row], snapshot, 1)
        return row, snapshot, raw

    def test_identical_slot_with_different_id_reuses_stored_identity_for_all_games(self):
        for code in ("mega645", "power655", "lotto535", "max3d", "max3dpro"):
            with self.subTest(code=code):
                row, _, _ = self.seed(code)
                incoming = deepcopy(row)
                incoming["draw_id"] += 1000
                snapshot = crawler.snapshot(code, incoming["source_url"], b"second source observation", source="test")
                self.assertEqual(crawler.persist_page(code, [incoming], snapshot, 2), (0, 1))
                self.assertEqual(crawler.persist_page(code, [incoming], snapshot, 2), (0, 1))
                with connect() as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM draws WHERE game_code=?", (code,)).fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT draw_id FROM crawl_observations WHERE snapshot_id=?", (snapshot,)).fetchone()[0], row["draw_id"])
                    state = connection.execute("SELECT * FROM crawl_state WHERE game_code=?", (code,)).fetchone()
                    self.assertEqual(state["oldest_draw_id"], row["draw_id"])
                    self.assertEqual(state["next_history_page"], 3)
                    detail = json.loads(connection.execute("SELECT detail FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0])
                    self.assertEqual(detail["draw_id_aliases"][0]["source_draw_id"], incoming["draw_id"])
                    self.assertEqual(detail["draw_id_aliases"][0]["stored_draw_id"], row["draw_id"])
                self.assertEqual(incoming["draw_id"], row["draw_id"] + 1000)

    def test_reconciliation_preserves_verified_metadata(self):
        row, snapshot, _ = self.seed()
        with connect() as connection:
            connection.execute("UPDATE draws SET verified=1,source_url='https://vietlott.vn' WHERE game_code='mega645'")
            before = dict(connection.execute("SELECT * FROM draws").fetchone())
        incoming = row | {"draw_id": row["draw_id"] + 1000}
        self.assertEqual(crawler.persist_page("mega645", [incoming], snapshot, None), (0, 1))
        with connect() as connection:
            self.assertEqual(dict(connection.execute("SELECT * FROM draws").fetchone()), before)

    def test_conflicting_numbers_roll_back_entire_history_page(self):
        row, snapshot, _ = self.seed()
        earlier = row | {"draw_id": row["draw_id"] - 1, "draw_date": "2026-10-16"}
        conflict = row | {"draw_id": row["draw_id"] + 1000, "main": [1, 11, 31, 38, 40, 44]}
        with self.assertRaisesRegex(CrawlError, "no records overwritten"):
            crawler.persist_page("mega645", [earlier, conflict], snapshot, 2)
        with connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM draws").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM crawl_observations").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT next_history_page FROM crawl_state").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM audit_log").fetchone()[0], 1)

    def test_special_and_digit_conflicts_are_not_merged(self):
        for code, field in (("lotto535", "special"), ("max3d", "digits")):
            with self.subTest(code=code):
                row, snapshot, _ = self.seed(code)
                incoming = deepcopy(row)
                incoming["draw_id"] += 1000
                if field == "special":
                    incoming[field] = row[field] % 12 + 1
                else:
                    group = next(iter(incoming[field]))
                    value = incoming[field][group][0]
                    incoming[field][group][0] = "001" if value != "001" else "002"
                with self.assertRaisesRegex(CrawlError, "no records overwritten"):
                    crawler.persist_page(code, [incoming], snapshot, 2)

    def test_source_id_already_used_by_another_slot_is_rejected(self):
        row, snapshot, _ = self.seed()
        later = row | {"draw_id": row["draw_id"] + 1, "draw_date": "2026-10-21"}
        crawler.persist_page("mega645", [later], snapshot, None)
        incoming = row | {"draw_id": later["draw_id"]}
        with self.assertRaisesRegex(CrawlError, "no records overwritten"):
            crawler.persist_page("mega645", [incoming], snapshot, 2)
        with connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM draws").fetchone()[0], 2)

    def test_same_id_cannot_move_to_another_date(self):
        row, snapshot, _ = self.seed()
        incoming = row | {"draw_date": "2026-10-16"}
        with self.assertRaisesRegex(CrawlError, "no records overwritten"):
            crawler.persist_page("mega645", [incoming], snapshot, 2)

    def test_conflict_reports_both_payloads_and_sources(self):
        row, snapshot, _ = self.seed()
        incoming = row | {"draw_id": row["draw_id"] + 1, "main": [1, 11, 31, 38, 40, 44]}
        with self.assertRaises(CrawlError) as caught:
            crawler.persist_page("mega645", [incoming], snapshot, 2)
        detail = json.loads(str(caught.exception).split("details=", 1)[1].split("; no records overwritten", 1)[0])
        self.assertEqual(detail["snapshot_id"], snapshot)
        self.assertEqual(detail["incoming"]["main"], incoming["main"])
        self.assertEqual(detail["stored"][0]["main"], row["main"])
        self.assertEqual(detail["stored"][0]["source_url"], row["source_url"])

    def test_read_only_inspection_includes_failed_snapshot_without_changing_data(self):
        row, _, raw = self.seed()
        incoming_raw = raw.replace(b"#1577", b"#1578").replace(b'>10</span>', b'>12</span>')
        snapshot = crawler.snapshot("mega645", row["source_url"], incoming_raw, source="minhchinh")
        with connect() as connection:
            before = list(connection.iterdump())
        report = inspect_draw(os.environ["DATABASE_PATH"], "mega645", row["draw_date"])
        self.assertTrue(report["read_only"])
        self.assertEqual(report["stored_nearby"][0]["draw_id"], row["draw_id"])
        self.assertEqual(report["unprocessed_snapshots"][0]["id"], snapshot)
        incoming = report["unprocessed_snapshots"][0]["matching_results"][0]
        self.assertEqual(incoming["draw_id"], 1578)
        self.assertIn(12, incoming["main"])
        with connect() as connection:
            self.assertEqual(list(connection.iterdump()), before)

    def test_inspection_does_not_create_missing_database(self):
        missing = Path(self.directory.name) / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            inspect_draw(missing, "mega645", "2019-11-06")
        self.assertFalse(missing.exists())

    def test_inspection_rejects_snapshot_paths_outside_raw_storage(self):
        row, _, _ = self.seed()
        snapshot = crawler.snapshot("mega645", row["source_url"], b"unprocessed", source="minhchinh")
        with connect() as connection:
            connection.execute("UPDATE raw_snapshots SET relative_path='../outside.html.gz' WHERE id=?", (snapshot,))
        report = inspect_draw(os.environ["DATABASE_PATH"], "mega645", row["draw_date"])
        self.assertIn("outside raw storage", report["unprocessed_snapshots"][0]["inspection_error"])

    def test_crawl_alias_keeps_prediction_hash_and_scores_canonical_draw(self):
        row, _, raw = self.seed()
        code = "mega645"
        locked = "2026-10-17T12:00:00+07:00"
        target = row["draw_date"] + "T" + row["draw_slot"] + ":00+07:00"
        payload = encode({"data_policy": "verified_or_crawled", "probabilities": [6 / 45] * 45,
                          "tickets": [{"main": row["main"]}]})
        with connect() as connection:
            version = connection.execute("INSERT INTO model_versions(game_code,method,parameters,created_at) VALUES(?,?,?,?)", (code, "uniform", "{}", locked)).lastrowid
            hashed = ledger.digest(code, row["draw_id"], version, payload, locked)
            identity = connection.execute("INSERT INTO predictions(game_code,target_draw_id,target_at,model_version_id,payload,payload_hash,locked_at) VALUES(?,?,?,?,?,?,?)", (code, row["draw_id"], target, version, payload, hashed, locked)).lastrowid
        source_id = row["draw_id"] + 1000
        fetcher = Mock()
        fetcher.fetch.return_value = raw.replace(("#" + str(row["draw_id"])).encode(), ("#" + str(source_id)).encode())
        job = crawler.queue_crawl(code)
        with patch.object(ledger, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 22, tzinfo=timezone.utc)
            result = crawler.run_crawl({"game": code, "mode": "latest", "pages": 1}, job["job_id"], fetcher=fetcher)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(result["scored"], 1)
        with connect() as connection:
            prediction = connection.execute("SELECT * FROM predictions WHERE id=?", (identity,)).fetchone()
            self.assertEqual(prediction["payload_hash"], hashed)
            self.assertEqual(prediction["target_draw_id"], row["draw_id"])
            self.assertEqual(json.loads(connection.execute("SELECT result FROM scores WHERE prediction_id=?", (identity,)).fetchone()[0])["hits"], [6])
            self.assertEqual(connection.execute("SELECT next_history_page FROM crawl_state WHERE game_code=?", (code,)).fetchone()[0], 2)
