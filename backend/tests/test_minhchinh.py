import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from bs4 import BeautifulSoup

from backend.app import crawler
from backend.app.crawl_http import allowed_url
from backend.app.db import connect, initialize
from backend.app.games import game


FIXTURES = Path(__file__).with_name("fixtures")
URL = "https://www.minhchinh.com/truc-tiep-xo-so-tu-chon-lotto-535.html"


def fixture():
    return (FIXTURES / "lotto535_minhchinh_source.html").read_bytes()


def parse(raw=None):
    spec = game("lotto535")
    spec = spec | {"crawler": spec["crawler"] | {"source": "minhchinh"}}
    return crawler.parse_results(raw if raw is not None else fixture(), spec, URL)


class MinhChinhParserTests(unittest.TestCase):
    def test_real_draw_959_excludes_prize_table_balls(self):
        rows, incomplete = parse()
        self.assertEqual(incomplete, 0)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["draw_id"], 959)
        self.assertEqual(row["draw_date"], "2026-10-21")
        self.assertEqual(row["draw_slot"], "13:00")
        self.assertEqual(row["main"], [14, 20, 29, 30, 34])
        self.assertEqual(row["special"], 11)
        self.assertFalse(row["verified"])
        self.assertIn("www.minhchinh.com/xs-lotto-535", row["source_url"])

    def test_missing_or_ambiguous_heading_is_rejected(self):
        for old, new in [(b"#959", b""), (b"#959", b"#959 #960"), (b"13:00", b""), (b"13:00", b"14:00")]:
            with self.subTest(old=old, new=new), self.assertRaises(ValueError):
                parse(fixture().replace(old, new))

    def test_incomplete_live_results_are_not_imported(self):
        soup = BeautifulSoup(fixture(), "html.parser")
        soup.select_one("#lotto535_G6").string = "..."
        self.assertEqual(parse(str(soup)), ([], 1))
        soup.select_one("#lotto535_G6").decompose()
        self.assertEqual(parse(str(soup)), ([], 1))

    def test_invalid_main_and_special_values_are_rejected(self):
        for selector, value in [("#lotto535_G1", "36"), ("#lotto535_G1", "20"), ("#lotto535_G6", "13")]:
            soup = BeautifulSoup(fixture(), "html.parser")
            soup.select_one(selector).string = value
            with self.subTest(selector=selector, value=value), self.assertRaises(ValueError):
                parse(str(soup))

    def test_special_cannot_be_silently_swapped_with_main(self):
        soup = BeautifulSoup(fixture(), "html.parser")
        soup.select_one("#lotto535_G6")["class"] = ["ball", "ball_lotto"]
        with self.assertRaises(ValueError):
            parse(str(soup))

    def test_conflicting_duplicate_draws_are_rejected(self):
        soup = BeautifulSoup(fixture(), "html.parser")
        block = soup.select_one(".boxketqua_vl")
        duplicate = BeautifulSoup(str(block), "html.parser")
        duplicate.select_one("#lotto535_G1").string = "15"
        block.insert_after(duplicate)
        with self.assertRaises(ValueError):
            parse(str(soup))

    def test_challenge_page_is_not_treated_as_empty_history(self):
        with self.assertRaisesRegex(ValueError, "No recognized Minh Chinh"):
            parse(b"<html><title>Checking access</title></html>")

    def test_only_explicit_minhchinh_host_is_allowed(self):
        allowed_url(URL)
        with self.assertRaises(ValueError):
            allowed_url("https://www.minhchinh.com.example.org/results")


class MinhChinhSyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(self.directory.name) / "test.sqlite3")
        initialize()

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self.previous
        self.directory.cleanup()

    def test_sync_adds_959_over_958_without_losing_history_cursor(self):
        spec = game("lotto535")
        old_raw = (FIXTURES / "lotto535_source.html").read_bytes()
        old_rows, _ = crawler.parse_results(old_raw, spec, spec["crawler"]["list_url"])
        snapshot = crawler.snapshot("lotto535", spec["crawler"]["list_url"], old_raw)
        crawler.persist_page("lotto535", old_rows, snapshot, 40)
        job = crawler.queue_crawl("lotto535")
        fetcher = Mock()
        fetcher.fetch.return_value = fixture()
        payload = {"game": "lotto535", "mode": "latest", "pages": 1}
        result = crawler.run_crawl(payload, job["job_id"], fetcher=fetcher)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["source"], "minhchinh")
        self.assertEqual(result["latest_draw_id"], 959)
        fetcher.fetch.assert_called_once_with(URL)
        state = crawler.status("lotto535")["state"]
        self.assertEqual(state["next_history_page"], 41)
        self.assertFalse(state["history_complete"])
        with connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM draws WHERE game_code='lotto535'").fetchone()[0], len(old_rows) + 1)
            self.assertEqual(connection.execute("SELECT source FROM raw_snapshots ORDER BY id DESC LIMIT 1").fetchone()[0], "minhchinh")
        repeated = crawler.run_crawl(payload, job["job_id"], fetcher=fetcher)
        self.assertEqual(repeated["added"], 0)
        self.assertEqual(repeated["unchanged"], 1)

    def test_history_batch_reads_latest_from_minhchinh_then_old_source(self):
        code = "lotto535"
        old_raw = (FIXTURES / "lotto535_source.html").read_bytes()
        soup = BeautifulSoup(old_raw, "html.parser")
        for link in soup.select("a[href]"):
            if "-p" in link["href"]:
                link.decompose()
        fetcher = Mock()
        fetcher.fetch.side_effect = [fixture(), str(soup).encode()]
        job = crawler.queue_crawl(code, "history", 2)
        result = crawler.run_crawl({"game": code, "mode": "history", "pages": 2}, job["job_id"], fetcher=fetcher)
        self.assertEqual(fetcher.fetch.call_args_list[0].args[0], URL)
        self.assertEqual(fetcher.fetch.call_args_list[1].args[0], "https://ketquaxoso.net/lotto-5-35-p2.html")
        self.assertTrue(result["history_complete"])
        self.assertEqual(result["pages_completed"], 2)

    def test_blocked_minhchinh_falls_back_with_explicit_provenance(self):
        code = "lotto535"
        old_raw = (FIXTURES / "lotto535_source.html").read_bytes()
        fetcher = Mock()
        fetcher.fetch.side_effect = [b"<html><title>Checking access</title></html>", old_raw]
        job = crawler.queue_crawl(code)
        result = crawler.run_crawl({"game": code, "mode": "latest", "pages": 1}, job["job_id"], fetcher=fetcher)
        self.assertEqual(result["source"], "ketquaxoso")
        self.assertIn("minhchinh", result["source_warning"])
        self.assertEqual(result["latest_draw_id"], 958)
        with connect() as connection:
            sources = [row[0] for row in connection.execute("SELECT source FROM raw_snapshots ORDER BY id")]
        self.assertEqual(sources, ["minhchinh", "ketquaxoso"])

    def test_minhchinh_is_not_an_independent_verification_source(self):
        with self.assertRaisesRegex(ValueError, "independent"):
            crawler.verify("lotto535", [], URL, "test")

    def test_incomplete_latest_source_tries_backup_without_importing_partial_draw(self):
        spec = game("lotto535")
        soup = BeautifulSoup(fixture(), "html.parser")
        soup.select_one("#lotto535_G6").string = "..."
        fetcher = Mock()
        fetcher.fetch.side_effect = [str(soup).encode(), (FIXTURES / "lotto535_source.html").read_bytes()]
        job = crawler.queue_crawl("lotto535")
        result = crawler.run_crawl({"game": "lotto535", "mode": "latest", "pages": 1}, job["job_id"], fetcher=fetcher)
        self.assertEqual(result["source"], "ketquaxoso")
        self.assertIn("incomplete", result["source_warning"])
        self.assertEqual(fetcher.fetch.call_args_list[1].args[0], spec["crawler"]["list_url"])
        with connect() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM draws WHERE game_code='lotto535' AND draw_id=959").fetchone())


if __name__ == "__main__":
    unittest.main()
