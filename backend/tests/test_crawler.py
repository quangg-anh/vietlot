import gzip
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from contextlib import closing
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend.app import crawl_http, crawler, jobs
from backend.app.db import connect, encode, initialize
from backend.app.etl import history
from backend.app.games import game
from backend.app.main import app, visitors


FIXTURES = Path(__file__).with_name("fixtures")
POWER = (FIXTURES / "power_results.html").read_bytes()
MEGA = (FIXTURES / "mega_results.html").read_bytes()
SOURCE = "https://xoso.com.vn/xo-so-power-655.html"


def older_power():
    return POWER.replace(b"01413", b"01411").replace(b"01412", b"01410").replace(b"20/10/2026", b"15/10/2026").replace(b"17/10/2026", b"13/10/2026").replace(b"20-10-2026", b"15-10-2026").replace(b"17-10-2026", b"13-10-2026")


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, raw, content_type="text/html"):
        super().__init__(raw)
        self.headers = Message()
        self.headers["Content-Type"] = content_type


class ParserTests(unittest.TestCase):
    def test_power_fixture_isolates_result_numbers(self):
        rows, incomplete = crawler.parse_results(POWER, game("power655"), SOURCE)
        self.assertEqual(incomplete, 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["draw_id"], 1413)
        self.assertEqual(rows[0]["main"], [13, 20, 23, 30, 50, 55])
        self.assertEqual(rows[0]["special"], 45)
        self.assertFalse(rows[0]["verified"])
        self.assertEqual(rows[0]["source_url"], "https://xoso.com.vn/xo-so-power-655-ngay-20-10-2026.html")

    def test_mega_fixture_sorts_without_special(self):
        rows, _ = crawler.parse_results(MEGA, game("mega645"), game("mega645")["crawler"]["list_url"])
        self.assertEqual(rows[0]["main"], [10, 11, 31, 38, 40, 44])
        self.assertIsNone(rows[0]["special"])
        alias = MEGA.decode().replace('>Xổ số Mega 6/45 Chủ Nhật<', ' title="Xổ số Mega 6/45 Chủ Nhật">KQXS 6/45 Chủ Nhật<')
        rows, _ = crawler.parse_results(alias, game("mega645"), game("mega645")["crawler"]["list_url"])
        self.assertEqual(len(rows), 1)

    def test_parser_fails_closed_on_changes_and_ambiguous_identity(self):
        invalid = [b"<html>Access denied</html>", POWER.replace(b"#01413", b"unknown"),
                   POWER.replace(b"bg_jackpot", b"unknown"),
                   POWER.replace(b">55</span>", b">99</span>"),
                   POWER.replace(b"20/10/2026", b"20/10/2026 21/10/2026"),
                   POWER.replace(b'/xo-so-power-655-ngay-20-10-2026.html', b'https://evil.example/results')]
        for raw in invalid:
            with self.subTest(raw=raw[:60]), self.assertRaises(ValueError):
                crawler.parse_results(raw, game("power655"), SOURCE)
        with self.assertRaises(ValueError):
            crawler.parse_results(MEGA, game("power655"), SOURCE)

    def test_live_incomplete_draws_are_skipped(self):
        raw = POWER.replace(b">13</span>", b">...</span>").replace(b">01</span>", b">...</span>")
        rows, incomplete = crawler.parse_results(raw, game("power655"), SOURCE)
        self.assertEqual(rows, [])
        self.assertEqual(incomplete, 2)

    def test_conflicting_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            crawler.parse_results(POWER + POWER.replace(b">13</span>", b">14</span>"), game("power655"), SOURCE)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = crawl_http.Fetcher()
        self.fetcher.throttle = Mock()
        self.fetcher.wait = Mock()

    def test_robots_read_before_results_and_cached_per_run(self):
        self.fetcher.opener.open = Mock(side_effect=[FakeResponse(b"User-agent: *\nDisallow: /private"), FakeResponse(POWER), FakeResponse(MEGA)])
        self.assertEqual(self.fetcher.fetch(SOURCE), POWER)
        self.fetcher.fetch(SOURCE)
        calls = self.fetcher.opener.open.call_args_list
        self.assertTrue(calls[0].args[0].full_url.endswith("/robots.txt"))
        self.assertEqual(len(calls), 3)

    def test_robots_forbidden_disallowed_and_missing(self):
        self.fetcher.opener.open = Mock(return_value=FakeResponse(b"User-agent: *\nDisallow: /"))
        with self.assertRaisesRegex(ValueError, "robots.txt disallows"):
            self.fetcher.fetch(SOURCE)
        self.assertEqual(self.fetcher.opener.open.call_count, 1)
        blocked = urllib.error.HTTPError(SOURCE, 403, "Forbidden", {}, None)
        self.fetcher.robots.clear()
        self.fetcher.opener.open = Mock(side_effect=blocked)
        with self.assertRaisesRegex(ValueError, "Cannot check robots"):
            self.fetcher.fetch(SOURCE)
        self.assertEqual(self.fetcher.opener.open.call_count, 1)
        missing = urllib.error.HTTPError(SOURCE, 404, "Not Found", {}, None)
        self.fetcher.opener.open = Mock(side_effect=[missing, FakeResponse(POWER)])
        self.assertEqual(self.fetcher.fetch(SOURCE), POWER)

    def test_host_allowlist_rejects_ssrf_and_credentials(self):
        for url in ["http://xoso.com.vn/", "https://127.0.0.1/", "https://xoso.com.vn.evil.test/", "https://xoso.com.vn:444/", "https://key@xoso.com.vn/"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                crawl_http.allowed_url(url)
        with self.assertRaises(ValueError):
            crawl_http.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.org")

    def test_transient_errors_retry_but_forbidden_does_not(self):
        transient = urllib.error.HTTPError(SOURCE, 503, "Unavailable", {}, None)
        self.fetcher.opener.open = Mock(side_effect=[transient, FakeResponse(POWER)])
        self.assertEqual(self.fetcher.request(SOURCE), POWER)
        self.assertEqual(self.fetcher.opener.open.call_count, 2)
        forbidden = urllib.error.HTTPError(SOURCE, 403, "Forbidden", {}, None)
        self.fetcher.opener.open = Mock(side_effect=forbidden)
        with self.assertRaises(ValueError):
            self.fetcher.request(SOURCE)
        self.assertEqual(self.fetcher.opener.open.call_count, 1)
        limited = urllib.error.HTTPError(SOURCE, 429, "Limited", {"Retry-After": "120"}, None)
        self.fetcher.opener.open = Mock(side_effect=limited)
        with self.assertRaisesRegex(ValueError, "longer pause"):
            self.fetcher.request(SOURCE)

    def test_payload_size_type_and_network_limit(self):
        for response in [FakeResponse(b"x" * (crawl_http.MAX_BYTES + 1)), FakeResponse(b"x", "application/octet-stream")]:
            self.fetcher.opener.open = Mock(return_value=response)
            with self.assertRaises(ValueError):
                self.fetcher.request(SOURCE)
        self.fetcher.opener.open = Mock(side_effect=urllib.error.URLError("unreachable"))
        with self.assertRaisesRegex(ValueError, "3 attempts"):
            self.fetcher.request(SOURCE)
        self.assertEqual(self.fetcher.opener.open.call_count, 3)

    def test_throttle_delay_and_shutdown(self):
        fetcher = crawl_http.Fetcher()
        fetcher.wait = Mock()
        with patch.dict(crawl_http._last_request, {"xoso.com.vn": 100}), patch.object(crawl_http.time, "monotonic", return_value=100):
            fetcher.throttle("xoso.com.vn", 5)
        self.assertEqual(fetcher.wait.call_args.args[0], 5)
        fetcher.stop.set()
        with self.assertRaises(InterruptedError):
            fetcher.throttle("xoso.com.vn", 1)
        with self.assertRaises(InterruptedError):
            crawl_http.Fetcher(fetcher.stop).wait(0)


class CrawlIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"DATABASE_PATH": str(Path(self.directory.name) / "research.sqlite3"),
                                                  "API_KEY": "crawler-test-only", "DISABLE_RUNNER": "1"})
        self.environment.start()
        visitors.clear()
        self.client = TestClient(app)
        self.client.__enter__()
        self.headers = {"X-API-Key": "crawler-test-only"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()
        self.directory.cleanup()

    def crawl(self, mode="latest", pages=1, responses=None):
        job = crawler.queue_crawl("power655", mode, pages)
        fake = Mock()
        fake.fetch.side_effect = responses or [POWER]
        with patch.object(crawler, "Fetcher", return_value=fake):
            for _ in range(5):
                jobs.run_one()
                current = self.client.get(f"/api/jobs/{job['job_id']}").json()
                if current["status"] in ("done", "failed"):
                    break
        return self.client.get(f"/api/jobs/{job['job_id']}").json()

    def test_crawl_snapshots_and_unverified_only(self):
        result = self.crawl()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["result"]["added"], 2)
        self.assertEqual(history("power655"), [])
        with connect() as connection:
            snapshot = connection.execute("SELECT * FROM raw_snapshots").fetchone()
            self.assertEqual(snapshot["content_hash"], hashlib.sha256(POWER).hexdigest())
        with gzip.open(Path(self.directory.name) / snapshot["relative_path"], "rb") as saved:
            self.assertEqual(saved.read(), POWER)
        repeated = self.crawl()
        self.assertEqual(repeated["result"]["added"], 0)
        self.assertEqual(repeated["result"]["unchanged"], 2)

    def test_history_cursor_resume_and_empty_end(self):
        result = self.crawl("history", 2, [POWER, older_power()])
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["result"]["added"], 4)
        self.assertEqual(crawler.status("power655")["state"]["next_history_page"], 2)
        self.crawl("latest", 2, [POWER, older_power()])
        self.assertEqual(crawler.status("power655")["state"]["next_history_page"], 2)
        result = self.crawl("history", 2, [POWER, b" "])
        self.assertTrue(result["result"]["history_complete"])

    def test_single_page_history_advances_cursor(self):
        result = self.crawl("history", 1, [older_power()])
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["result"]["pages_completed"], 1)
        self.assertEqual(crawler.status("power655")["state"]["next_history_page"], 2)
        result = self.crawl("history", 1, [b" "])
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["result"]["history_complete"])

    def test_incomplete_history_keeps_cursor_for_retry(self):
        incomplete = older_power().replace(b">13</span>", b">...</span>")
        result = self.crawl("history", 2, [POWER, incomplete])
        self.assertEqual(result["status"], "failed")
        self.assertIn("cursor retained", result["error"])
        self.assertEqual(crawler.status("power655")["state"]["next_history_page"], 1)
        result = self.crawl("history", 1, [older_power()])
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["result"]["added"], 2)

    def test_queue_deduplicates_and_recovers_restart(self):
        job = crawler.queue_crawl("power655")
        self.assertEqual(crawler.queue_crawl("power655")["job_id"], job["job_id"])
        with connect() as connection:
            connection.execute("UPDATE jobs SET status='running' WHERE id=?", (job["job_id"],))
        jobs.recover()
        self.assertEqual(self.client.get(f"/api/jobs/{job['job_id']}").json()["status"], "queued")

    def test_failure_and_snapshot_retention(self):
        result = self.crawl(responses=[b"<html>changed</html>"])
        self.assertEqual(result["status"], "failed")
        state = crawler.status("power655")["state"]
        self.assertEqual(state["failures"], 1)
        self.assertIn("No recognized results", state["last_error"])
        with connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM raw_snapshots").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM draws").fetchone()[0], 0)

    def test_changed_source_cannot_overwrite_results(self):
        self.crawl()
        result = self.crawl(responses=[POWER.replace(b">13</span>", b">14</span>")])
        self.assertEqual(result["status"], "failed")
        self.assertIn("no records overwritten", result["error"])
        self.assertEqual(crawler.pending("power655")[0]["main"], [13, 20, 23, 30, 50, 55])

    def test_duplicate_pagination_stops(self):
        result = self.crawl("history", 2, [POWER, POWER])
        self.assertEqual(result["status"], "failed")
        self.assertIn("Pagination made no", result["error"])
        self.assertEqual(crawler.status("power655")["state"]["next_history_page"], 1)

    def test_schedule_is_opt_in_and_persisted(self):
        crawler.queue_due()
        with connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        crawler.set_schedule("power655", True)
        crawler.queue_due()
        crawler.queue_due()
        with connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        crawler.set_schedule("power655", False)
        self.assertFalse(crawler.status("power655")["state"]["scheduled"])

    def test_api_review_requires_auth_acknowledgement_and_independent_source(self):
        self.assertEqual(self.client.post("/api/games/power655/crawl", json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/games/unknown/crawl", headers=self.headers, json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/games/power655/crawl", headers=self.headers, json={"pages": 100}).status_code, 422)
        self.crawl()
        response = self.client.get("/api/games/power655/draws/pending", headers=self.headers)
        item = response.json()["items"][0]
        review = {"draws": [{"draw_id": item["draw_id"], "fingerprint": item["fingerprint"]}],
                  "checked_source_url": "https://vietlott.vn", "note": "Test operator attestation", "acknowledged": True}
        self.assertEqual(self.client.post("/api/games/power655/draws/verify", headers=self.headers, json=review | {"acknowledged": False}).status_code, 422)
        self.assertEqual(self.client.post("/api/games/power655/draws/verify", headers=self.headers,
                                         json=review | {"checked_source_url": SOURCE}).status_code, 400)
        result = self.client.post("/api/games/power655/draws/verify", headers=self.headers, json=review)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["verified"], 1)
        self.assertEqual(len(history("power655")), 1)
        self.assertEqual(len(crawler.pending("power655")), 1)
        result = self.crawl()
        self.assertEqual(result["result"]["unchanged"], 2)
        self.assertEqual(len(history("power655")), 1)

    def test_stale_review_hash_is_rejected_atomically(self):
        self.crawl()
        rows = crawler.pending("power655")
        reviews = [{"draw_id": row["draw_id"], "fingerprint": row["fingerprint"]} for row in rows]
        reviews[-1]["fingerprint"] = "0" * 64
        with self.assertRaises(ValueError):
            crawler.verify("power655", reviews, "https://vietlott.vn", "Test verification")
        self.assertEqual(history("power655"), [])

    def test_shutdown_requeues_crawl(self):
        job = crawler.queue_crawl("power655")
        stopped = threading.Event()
        stopped.set()
        jobs.run_one(stopped)
        self.assertEqual(self.client.get(f"/api/jobs/{job['job_id']}").json()["status"], "queued")

    def test_schema_v1_migration_preserves_jobs_and_backtests(self):
        legacy = Path(self.directory.name) / "legacy.sqlite3"
        schema = Path("backend/app/schema.sql").read_text().split("CREATE TABLE IF NOT EXISTS raw_snapshots")[0]
        schema = schema.replace("('backtest','score','crawl')", "('backtest','score')")
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript(schema)
            connection.execute("INSERT INTO jobs(id,kind,payload,status,created_at) VALUES(1,'backtest','{}','done','2025-01-01')")
            connection.execute("INSERT INTO backtest_runs(job_id,game_code,config,metrics,created_at) VALUES(1,'power655','{}','{}','2025-01-01')")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        with patch.dict(os.environ, {"DATABASE_PATH": str(legacy)}):
            initialize()
            initialize()
            with connect() as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertIsNone(connection.execute("PRAGMA foreign_key_check").fetchone())
                self.assertEqual(connection.execute("SELECT job_id FROM backtest_runs").fetchone()[0], 1)
                connection.execute("INSERT INTO jobs(kind,payload,created_at) VALUES('crawl','{}','2025-01-01')")


if __name__ == "__main__":
    unittest.main()
