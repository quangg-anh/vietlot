import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from backend.app import crawler
from backend.app.db import connect, initialize
from backend.app.games import game


FIXTURES = Path(__file__).with_name("fixtures")


def fixture(code):
    return (FIXTURES / f"{code}_source.html").read_bytes()


def parse(code, raw=None):
    spec = game(code)
    return crawler.parse_results(raw if raw is not None else fixture(code), spec, spec["crawler"]["list_url"])


class ExtendedParserTests(unittest.TestCase):
    def test_lotto_slots_and_special(self):
        rows, incomplete = parse("lotto535")
        self.assertEqual(incomplete, 0)
        self.assertEqual(len(rows), 14)
        self.assertEqual([row["draw_slot"] for row in rows[:2]], ["21:00", "13:00"])
        self.assertEqual(rows[0]["main"], [2, 9, 11, 25, 30])
        self.assertEqual(rows[0]["special"], 4)
        self.assertFalse(rows[0]["verified"])

    def test_digit_groups_preserve_leading_zeroes(self):
        for code in ("max3d", "max3dpro"):
            with self.subTest(code=code):
                rows, incomplete = parse(code)
                self.assertEqual(incomplete, 0)
                self.assertEqual(len(rows), 10)
                for group, count in game(code)["digit_groups"].items():
                    self.assertEqual(len(rows[0]["digits"][group]), count)
                self.assertIn("094" if code == "max3d" else "089", rows[0]["digits"]["first"])

    def test_incomplete_results_skipped(self):
        for code, selector in (("lotto535", ".lh-sp"), ("max3d", ".lh-n3")):
            soup = BeautifulSoup(fixture(code), "html.parser")
            soup.select_one(selector).decompose()
            rows, incomplete = parse(code, str(soup))
            self.assertEqual(incomplete, 1)
            self.assertTrue(rows)

    def test_rejects_wrong_game_and_missing_slot(self):
        with self.assertRaises(ValueError):
            parse("max3d", fixture("max3dpro"))
        with self.assertRaises(ValueError):
            parse("lotto535", fixture("lotto535").replace(b"21:00", b""))

    def test_unknown_prize_label_rejected(self):
        soup = BeautifulSoup(fixture("max3d"), "html.parser")
        soup.select_one(".lh-lb").string = "Unexpected prize"
        with self.assertRaises(ValueError):
            parse("max3d", str(soup))


class ExtendedCrawlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"DATABASE_PATH": str(Path(self.directory.name) / "test.sqlite3")})
        self.environment.start()
        initialize()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def test_all_games_persist_and_repeated_sync_is_idempotent(self):
        for code in ("lotto535", "max3d", "max3dpro"):
            with self.subTest(code=code):
                job = crawler.queue_crawl(code)
                fetcher = Mock()
                fetcher.fetch.return_value = fixture(code)
                payload = {"game": code, "mode": "latest", "pages": 1}
                first = crawler.run_crawl(payload, job["job_id"], fetcher=fetcher)
                second = crawler.run_crawl(payload, job["job_id"], fetcher=fetcher)
                self.assertGreater(first["added"], 0)
                self.assertEqual(second["added"], 0)
                self.assertEqual(second["unchanged"], first["added"])
                with connect() as connection:
                    self.assertEqual(connection.execute("SELECT source FROM raw_snapshots WHERE game_code=? LIMIT 1", (code,)).fetchone()[0], "ketquaxoso")

    def test_history_uses_source_page_number_and_stops_at_last_page(self):
        code = "lotto535"
        job = crawler.queue_crawl(code, "history", 1)
        soup = BeautifulSoup(fixture(code), "html.parser")
        for link in soup.select("a[href]"):
            if "-p" in link["href"]:
                link.decompose()
        fetcher = Mock()
        fetcher.fetch.return_value = str(soup).encode()
        result = crawler.run_crawl({"game": code, "mode": "history", "pages": 1}, job["job_id"], fetcher=fetcher)
        fetcher.fetch.assert_called_once_with("https://ketquaxoso.net/lotto-5-35-p2.html")
        self.assertTrue(result["history_complete"])

    def test_same_source_cannot_verify_draws(self):
        with self.assertRaisesRegex(ValueError, "independent"):
            crawler.verify("max3d", [], "https://ketquaxoso.net/max-3d.html", "review")
