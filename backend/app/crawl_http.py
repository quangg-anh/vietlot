import random
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urlsplit


USER_AGENT = "VietlottResearchBot/2.1 (+https://github.com/quangg-anh/vietlot; public results research)"
ALLOWED_HOSTS = {"xoso.com.vn", "ketquaxoso.net"}
MAX_BYTES = 2_000_000
_request_lock = threading.Lock()
_last_request = {}


class CrawlError(ValueError):
    pass


def allowed_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or parts.port not in (None, 443) or parts.username or parts.password:
        raise CrawlError("Only configured HTTPS result hosts are allowed")
    return parts


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise CrawlError(f"Source redirected (HTTP {code}); review adapter URL, redirects are not followed")


class Fetcher:
    def __init__(self, stop=None):
        self.stop = stop or threading.Event()
        self.opener = urllib.request.build_opener(NoRedirect())
        self.robots = {}

    def wait(self, seconds):
        if self.stop.wait(seconds):
            raise InterruptedError("Crawler stopped; job can resume")

    def throttle(self, host, delay):
        with _request_lock:
            remaining = _last_request.get(host, 0) + max(1.05, delay) - time.monotonic()
            if remaining > 0:
                self.wait(remaining)
            if self.stop.is_set():
                raise InterruptedError("Crawler stopped")
            _last_request[host] = time.monotonic()

    def request(self, url, delay=1.05):
        host = allowed_url(url).hostname
        for attempt in range(3):
            self.throttle(host, delay)
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain", "Accept-Encoding": "identity"})
            try:
                with self.opener.open(request, timeout=15) as response:
                    if response.status != 200:
                        raise CrawlError(f"Unexpected HTTP {response.status}")
                    if response.headers.get_content_type() not in ("text/html", "text/plain", "application/xhtml+xml"):
                        raise CrawlError("Source returned an unexpected content type")
                    data = response.read(MAX_BYTES + 1)
                    if len(data) > MAX_BYTES:
                        raise CrawlError("Source response exceeds 2 MB")
                    return data
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise CrawlError(f"Source HTTP {exc.code}; no bypass attempted") from exc
                retry_after = exc.headers.get("Retry-After", "0")
                try:
                    wait = float(retry_after)
                except ValueError:
                    try:
                        wait = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        wait = 0
                if wait > 60:
                    raise CrawlError("Source requests a longer pause; retry this job later") from exc
                self.wait(max(wait, 2 ** attempt + random.random()))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == 2:
                    raise CrawlError("Source network error after 3 attempts") from exc
                self.wait(2 ** attempt + random.random())
        raise CrawlError("Source fetch failed")

    def fetch(self, url):
        parts = allowed_url(url)
        if parts.hostname not in self.robots:
            robots_url = f"https://{parts.hostname}/robots.txt"
            try:
                raw = self.request(robots_url)
            except CrawlError as exc:
                if isinstance(exc.__cause__, urllib.error.HTTPError) and exc.__cause__.code == 404:
                    raw = b"User-agent: *\nAllow: /\n"
                else:
                    raise CrawlError("Cannot check robots.txt; crawling stopped: " + str(exc)) from exc
            parser = urllib.robotparser.RobotFileParser(robots_url)
            parser.parse(raw.decode("utf-8", "replace").splitlines())
            self.robots[parts.hostname] = parser
        parser = self.robots[parts.hostname]
        if not parser.can_fetch(USER_AGENT, url):
            raise CrawlError("robots.txt disallows this URL")
        delay = parser.crawl_delay(USER_AGENT) or 1.05
        rate = parser.request_rate(USER_AGENT)
        if rate and rate.requests:
            delay = max(delay, rate.seconds / rate.requests)
        return self.request(url, delay)
