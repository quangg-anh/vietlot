import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from bs4 import BeautifulSoup

from backend.app import crawler
from backend.app.crawl_http import CrawlError
from backend.app.db import connect, initialize
from backend.app.games import game

FIXTURES = Path(__file__).with_name("fixtures")


class DateReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(self.directory.name) / "test.sqlite3")
        initialize()
        self.spec = game("mega645")
        self.history_raw = (FIXTURES / "mega645_xoso_wrong_date_20191106.html").read_bytes()
        self.confirmed_raw = (FIXTURES / "mega645_minhchinh_20191106.html").read_bytes()
        self.check_url = self.spec["crawler"]["reconcile_url"].format(date="06-11-2019")
        self.check_spec = self.spec | {"crawler": self.spec["crawler"] | {"source": "minhchinh", "historical": True}}
        rows, _ = crawler.parse_results(self.confirmed_raw, self.check_spec, self.check_url)
        self.correct = rows
        stored = rows[0] | {"source_url": "https://xoso.com.vn/xo-so-mega-ngay-06-11-2019.html"}
        snapshot = crawler.snapshot("mega645", stored["source_url"], b"stored history observation")
        crawler.persist_page("mega645", [stored], snapshot, 100)
        self.job = crawler.queue_crawl("mega645", "history", 1)
        self.payload = {"game": "mega645", "mode": "history", "pages": 1}

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self.previous
        self.directory.cleanup()

    def run_history(self, confirmation=None):
        fetcher = Mock()
        fetcher.fetch.side_effect = [self.history_raw, self.confirmed_raw if confirmation is None else confirmation]
        return crawler.run_crawl(self.payload, self.job["job_id"], fetcher=fetcher), fetcher

    def assert_original_unchanged(self):
        with connect() as connection:
            rows = connection.execute("SELECT * FROM draws ORDER BY draw_id").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["draw_id"], 515)
            self.assertEqual(json.loads(rows[0]["main"]), [3, 15, 22, 23, 38, 43])
            self.assertEqual(connection.execute("SELECT next_history_page FROM crawl_state").fetchone()[0], 101)
            self.assertEqual(connection.execute("SELECT count(*) FROM audit_log").fetchone()[0], 1)

    def test_wrong_date_is_corrected_from_matching_result_identity_and_stored_slot(self):
        with connect() as connection:
            before = dict(connection.execute("SELECT * FROM draws WHERE draw_id=515").fetchone())
        result, fetcher = self.run_history()
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(fetcher.fetch.call_args_list[1].args[0], self.check_url)
        correction = result["source_corrections"][0]
        self.assertEqual(correction["original"]["draw_date"], "2019-11-06")
        self.assertEqual(correction["corrected"]["draw_date"], "2019-11-03")
        self.assertEqual(correction["corrected"]["draw_id"], 514)
        with connect() as connection:
            self.assertEqual(dict(connection.execute("SELECT * FROM draws WHERE draw_id=515").fetchone()), before)
            corrected = connection.execute("SELECT * FROM draws WHERE draw_id=514").fetchone()
            self.assertEqual(corrected["draw_date"], "2019-11-03")
            self.assertIn("minhchinh.com", corrected["source_url"])
            self.assertEqual(corrected["verified"], 0)
            observation = connection.execute("SELECT snapshot_id FROM crawl_observations WHERE draw_id=514").fetchone()[0]
            self.assertEqual(observation, correction["evidence_snapshot_id"])
            self.assertNotEqual(observation, correction["original_snapshot_id"])
            audit = json.loads(connection.execute("SELECT detail FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0])
            self.assertEqual(audit["source_date_corrections"], result["source_corrections"])
            self.assertEqual(connection.execute("SELECT next_history_page FROM crawl_state").fetchone()[0], 102)

    def invalid_weekday_page(self):
        self.history_raw = self.history_raw.replace(b"06/11/2019", b"04/11/2019").replace(b"06-11-2019", b"04-11-2019")
        soup = BeautifulSoup(self.confirmed_raw, "html.parser")
        self.confirmed_raw = str(soup.select(".boxketqua_vl")[1]).encode()

    def test_invalid_weekday_is_reconciled_before_validation_and_persistence(self):
        self.invalid_weekday_page()
        result, fetcher = self.run_history()
        self.assertEqual(result["added"], 1)
        self.assertTrue(fetcher.fetch.call_args_list[1].args[0].endswith("04-11-2019.html"))
        correction = result["source_corrections"][0]
        self.assertEqual(correction["reason"], "source_schedule_mismatch")
        self.assertEqual(correction["original"]["draw_date"], "2019-11-04")
        self.assertEqual(correction["corrected"]["draw_date"], "2019-11-03")
        with connect() as connection:
            row = connection.execute("SELECT * FROM draws WHERE draw_id=514").fetchone()
            self.assertEqual(row["draw_date"], "2019-11-03")
            self.assertEqual(row["verified"], 0)
            self.assertEqual(connection.execute("SELECT snapshot_id FROM crawl_observations WHERE draw_id=514").fetchone()[0], correction["evidence_snapshot_id"])
            audit = json.loads(connection.execute("SELECT detail FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0])
            self.assertEqual(audit["source_date_corrections"], result["source_corrections"])
            self.assertEqual(connection.execute("SELECT next_history_page FROM crawl_state").fetchone()[0], 102)

    def test_repeated_draw_with_different_date_or_link_uses_same_evidence(self):
        self.invalid_weekday_page()
        for valid_duplicate in (False, True):
            with self.subTest(valid_duplicate=valid_duplicate):
                soup = BeautifulSoup(self.history_raw, "html.parser")
                duplicate = BeautifulSoup(self.history_raw, "html.parser").select_one("section")
                link = duplicate.select_one("header h2 a[href]")
                if valid_duplicate:
                    link.string = "03/11/2019"
                    link["href"] = "/xo-so-mega-ngay-03-11-2019.html"
                else:
                    link["href"] += "?duplicate=1"
                soup.insert(0, duplicate)
                fetcher = Mock()
                fetcher.fetch.side_effect = [str(soup).encode(), self.confirmed_raw]
                _, _, rows, incomplete = crawler.read_result_page(
                    "mega645", self.spec, self.spec["crawler"]["list_url"], fetcher, "xoso")
                self.assertEqual(incomplete, 0)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["draw_date"], "2019-11-03")
                self.assertIn("minhchinh.com", rows[0]["source_url"])
                self.assertIn("_evidence_snapshot_id", rows[0])
                self.assertEqual(fetcher.fetch.call_count, 2)
                self.assert_original_unchanged()

    def test_repeated_draw_with_different_numbers_is_rejected(self):
        self.invalid_weekday_page()
        soup = BeautifulSoup(self.history_raw, "html.parser")
        duplicate = BeautifulSoup(self.history_raw, "html.parser").select_one("section")
        duplicate.select_one(".btn-results").string = "27"
        soup.append(duplicate)
        self.history_raw = str(soup).encode()
        with self.assertRaisesRegex(CrawlError, "Source row does not match corroborating result"):
            self.run_history()
        self.assert_original_unchanged()

    def test_invalid_weekday_cannot_be_fixed_with_different_numbers(self):
        self.invalid_weekday_page()
        with self.assertRaisesRegex(CrawlError, "Corroborating draw ID or numbers do not match source"):
            self.run_history(self.confirmed_raw.replace(b">28</span>", b">27</span>"))
        self.assert_original_unchanged()

    def test_invalid_schedule_in_both_sources_is_rejected_without_recursive_fetch(self):
        self.invalid_weekday_page()
        invalid_confirmation = self.confirmed_raw.replace(b"03/11/2019", b"04/11/2019").replace(b"03-11-2019", b"04-11-2019")
        fetcher = Mock()
        fetcher.fetch.side_effect = [self.history_raw, invalid_confirmation]
        with self.assertRaisesRegex(CrawlError, "separate source is required"):
            crawler.run_crawl(self.payload, self.job["job_id"], fetcher=fetcher)
        self.assertEqual(fetcher.fetch.call_count, 2)
        self.assert_original_unchanged()

    def test_schedule_error_without_config_contains_game_draw_date_slot_and_snapshot(self):
        self.invalid_weekday_page()
        fetcher = Mock()
        fetcher.fetch.return_value = self.history_raw
        spec = self.spec | {"crawler": {key: value for key, value in self.spec["crawler"].items() if key != "reconcile_url"}}
        url = self.spec["crawler"]["list_url"]
        with self.assertRaises(CrawlError) as caught:
            crawler.read_result_page("mega645", spec, url, fetcher, "xoso")
        message = str(caught.exception)
        for text in ("game=mega645", "draw_id=00514", "date=2019-11-04", "weekday=Monday", "18:00", "expected_weekdays=", "expected_slots=", "snapshot_id=", url):
            self.assertIn(text, message)
        fetcher.fetch.assert_called_once_with(url)
        self.assert_original_unchanged()

    def test_repeated_reconciliation_is_idempotent(self):
        self.run_history()
        result, _ = self.run_history()
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["unchanged"], 2)
        with connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM draws").fetchone()[0], 2)

    def test_missing_confirmation_keeps_history_cursor_and_existing_data(self):
        with self.assertRaisesRegex(CrawlError, "Date reconciliation unavailable"):
            self.run_history(CrawlError("Source network failure"))
        self.assert_original_unchanged()

    def test_confirmation_must_corroborate_stored_slot(self):
        changed = self.confirmed_raw.replace(b">43</span>", b">42</span>")
        with self.assertRaises(crawler.DrawConflict):
            self.run_history(changed)
        self.assert_original_unchanged()

    def test_corrected_draw_must_have_same_numbers_as_incoming(self):
        changed = self.confirmed_raw.replace(b">28</span>", b">27</span>")
        with self.assertRaises(crawler.DrawConflict):
            self.run_history(changed)
        self.assert_original_unchanged()

    def test_incomplete_confirmation_is_not_used(self):
        changed = self.confirmed_raw.replace(b">28</span>", b">...</span>")
        with self.assertRaises(crawler.DrawConflict):
            self.run_history(changed)
        self.assert_original_unchanged()

    def test_historical_no_id_layout_is_opt_in(self):
        with self.assertRaisesRegex(CrawlError, "ball layout"):
            crawler.parse_results(self.confirmed_raw, self.check_spec | {"crawler": self.check_spec["crawler"] | {"historical": False}}, self.check_url)
        rows, incomplete = crawler.parse_results(self.confirmed_raw, self.check_spec, self.check_url)
        self.assertEqual([row["draw_id"] for row in rows], [515, 514])
        self.assertEqual(incomplete, 0)

    def test_mixed_ball_ids_are_rejected_even_in_history_mode(self):
        soup = BeautifulSoup(self.confirmed_raw, "html.parser")
        soup.select_one(".box_ketqua > span")["id"] = "mega645_G1"
        with self.assertRaises(CrawlError):
            crawler.parse_results(str(soup), self.check_spec, self.check_url)

    def test_verified_stored_result_is_never_changed_or_downgraded(self):
        with connect() as connection:
            connection.execute("UPDATE draws SET verified=1 WHERE draw_id=515")
            before = dict(connection.execute("SELECT * FROM draws WHERE draw_id=515").fetchone())
        self.run_history()
        with connect() as connection:
            self.assertEqual(dict(connection.execute("SELECT * FROM draws WHERE draw_id=515").fetchone()), before)

    def test_a_later_unresolved_conflict_rolls_back_earlier_correction(self):
        correct_513 = self.correct[1] | {"draw_id": 513, "draw_date": "2019-11-01", "main": [6, 11, 20, 30, 37, 40]}
        snapshot = crawler.snapshot("mega645", correct_513["source_url"], b"stored 513")
        crawler.persist_page("mega645", [correct_513], snapshot, None)
        soup = BeautifulSoup(self.history_raw, "html.parser")
        extra = BeautifulSoup(self.history_raw, "html.parser").select_one("section")
        extra.select_one("strong").string = "#00513"
        extra.select_one("header h2 a[href]")["href"] = "/xo-so-mega-ngay-01-11-2019.html"
        extra.select_one("header h2 a[href]").string = "01/11/2019"
        for ball, value in zip(extra.select(".btn-results"), [1, 11, 20, 30, 37, 40]):
            ball.string = str(value)
        soup.append(extra)
        fetcher = Mock()
        fetcher.fetch.side_effect = [str(soup).encode(), self.confirmed_raw, self.confirmed_raw]
        with self.assertRaises(crawler.DrawConflict):
            crawler.run_crawl(self.payload, self.job["job_id"], fetcher=fetcher)
        with connect() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM draws WHERE draw_id=514").fetchone())
            self.assertEqual(connection.execute("SELECT count(*) FROM draws").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT next_history_page FROM crawl_state").fetchone()[0], 101)
            self.assertEqual(connection.execute("SELECT count(*) FROM audit_log").fetchone()[0], 2)
