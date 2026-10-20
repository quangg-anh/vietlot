import argparse
import gzip
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .crawl_http import CrawlError, Fetcher, allowed_url
from .db import connect, database_path, encode
from .etl import now, unpack, validate
from .games import game, games
from . import crawl_ketquaxoso


def crawl_spec(code):
    spec = game(code)
    if not spec.get("crawl_enabled") or not spec.get("crawler"):
        raise CrawlError(f"Crawler is not configured for {code}")
    for field in ("list_url", "history_url"):
        allowed_url(spec["crawler"][field])
    return spec


def parse_results(raw, spec, url):
    if spec["crawler"].get("source") == "ketquaxoso":
        return crawl_ketquaxoso.parse_results(raw, spec, url)
    soup = BeautifulSoup(raw, "html.parser")
    rows, incomplete, identities = [], 0, {}
    for results in soup.select(".mega-results"):
        section = results.find_parent("section")
        heading = section.select_one("header h2") if section else None
        title = heading.get_text(" ", strip=True) if heading else ""
        identity_title = title + " " + " ".join(link.get("title", "") for link in heading.select("a") if heading) if heading else title
        expected = spec["name"].replace(" ", "").lower()
        if expected not in identity_title.replace(" ", "").lower():
            continue
        dates = re.findall(r"\b\d{2}/\d{2}/\d{4}\b", title)
        identity = results.select_one(".jackpot-item strong")
        identity_text = identity.get_text(strip=True) if identity else ""
        if len(dates) != 1 or not re.fullmatch(r"#\d{1,8}", identity_text):
            raise CrawlError("Result block lacks an unambiguous date/draw ID")
        balls = results.select(".btn-results")
        values = [ball.get_text(strip=True) for ball in balls]
        if len(values) < spec["pick_count"] + int(spec["special_required"]) or any(not re.fullmatch(r"\d{1,2}", value) for value in values):
            incomplete += 1
            continue
        specials = [int(ball.get_text(strip=True)) for ball in balls if "bg_jackpot" in ball.get("class", [])]
        main = [int(ball.get_text(strip=True)) for ball in balls if "bg_jackpot" not in ball.get("class", [])]
        if len(main) != spec["pick_count"] or len(specials) != int(spec["special_required"]):
            raise CrawlError("Result ball layout changed; refusing ambiguous import")
        source = url
        for link in heading.select("a[href]"):
            if dates[0] in link.get_text(" ", strip=True):
                source = urljoin(url, link["href"])
                allowed_url(source)
                break
        row = validate({"game_code": spec["code"], "draw_id": identity_text[1:],
                        "draw_date": datetime.strptime(dates[0], "%d/%m/%Y").date().isoformat(),
                        "draw_slot": spec["slots"][0], "main": " ".join(map(str, main)),
                        "special": str(specials[0]) if specials else "", "digits": "",
                        "source_url": source, "verified": "false"})
        previous = identities.get(row["draw_id"])
        if previous and previous != row:
            raise CrawlError("Conflicting draw IDs within source page")
        if not previous:
            rows.append(row)
            identities[row["draw_id"]] = row
    if not rows and not incomplete:
        raise CrawlError("No recognized results; parser/source may have changed")
    return rows, incomplete


def snapshot(code, url, raw):
    digest = hashlib.sha256(raw).hexdigest()
    relative = f"raw/{code}/{digest}.html.gz"
    path = database_path().parent / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with gzip.open(path, "wb") as output:
            output.write(raw)
    with connect() as connection:
        return connection.execute("INSERT INTO raw_snapshots(game_code,source,url,content_hash,relative_path,fetched_at) VALUES(?,?,?,?,?,?)",
                                  (code, game(code)["crawler"]["source"], url, digest, relative, now())).lastrowid


def fingerprint(row):
    return hashlib.sha256(encode({field: row[field] for field in (
        "game_code", "draw_id", "draw_date", "draw_slot", "main", "special", "digits", "source_url", "imported_at")}).encode()).hexdigest()


def cleanup_raw_snapshots():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    root = (database_path().parent / "raw").resolve()
    removed = 0
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        candidates = connection.execute("""
            SELECT relative_path FROM raw_snapshots
            GROUP BY relative_path
            HAVING max(fetched_at) < ? AND min(EXISTS(
                SELECT 1 FROM crawl_observations
                WHERE snapshot_id=raw_snapshots.id
            ))=1
        """, (cutoff,)).fetchall()
        for candidate in candidates:
            path = (database_path().parent / candidate["relative_path"]).resolve()
            if not path.is_relative_to(root) or path.suffixes[-2:] != [".html", ".gz"]:
                continue
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                continue
            except OSError:
                logging.getLogger(__name__).warning("Cannot remove raw snapshot %s", path, exc_info=True)
    return removed

def persist_page(code, rows, snapshot_id, page):
    added, unchanged = 0, 0
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            existing = connection.execute("SELECT * FROM draws WHERE game_code=? AND draw_id=?", (code, row["draw_id"])).fetchone()
            if existing:
                previous = unpack(existing)
                if any(previous[field] != row[field] for field in ("draw_date", "draw_slot", "main", "special", "digits")):
                    raise CrawlError(f"Source conflicts with stored draw #{row['draw_id']}; no records overwritten")
                unchanged += 1
            else:
                connection.execute("INSERT INTO draws VALUES(?,?,?,?,?,?,?,?,?,?)",
                                   (code, row["draw_id"], row["draw_date"], row["draw_slot"], encode(row["main"]),
                                    row["special"], encode(row["digits"]), row["source_url"], 0, now()))
                added += 1
            connection.execute("INSERT OR IGNORE INTO crawl_observations VALUES(?,?,?)", (snapshot_id, code, row["draw_id"]))
        ordered = connection.execute("SELECT draw_id FROM draws WHERE game_code=? ORDER BY draw_date,draw_slot", (code,)).fetchall()
        ids = [row[0] for row in ordered]
        if ids != sorted(ids):
            raise CrawlError("Crawled draw IDs do not increase chronologically")
        connection.execute("INSERT OR IGNORE INTO crawl_state(game_code) VALUES(?)", (code,))
        if rows:
            oldest = min(row["draw_id"] for row in rows)
            connection.execute("UPDATE crawl_state SET oldest_draw_id=min(coalesce(oldest_draw_id,?),?) WHERE game_code=?", (oldest, oldest, code))
        if page is not None and page > 0:
            connection.execute("UPDATE crawl_state SET next_history_page=? WHERE game_code=?", (page + 1, code))
        connection.execute("INSERT INTO audit_log(action,detail,at) VALUES(?,?,?)",
                           ("crawl_import", encode({"game": code, "snapshot_id": snapshot_id, "added": added,
                            "unchanged": unchanged, "verified": False}), now()))
    return added, unchanged


def queue_crawl(code, mode="latest", pages=1):
    crawl_spec(code)
    if mode not in ("latest", "history") or not 1 <= pages <= 50:
        raise CrawlError("mode must be latest/history and pages must be 1..50")
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute("SELECT id FROM jobs WHERE kind='crawl' AND status IN ('queued','running') AND json_extract(payload,'$.game')=?", (code,)).fetchone()
        if active:
            return {"job_id": active[0], "status": "already_queued"}
        if connection.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 20:
            raise CrawlError("Job queue full; try later")
        identity = connection.execute("INSERT INTO jobs(kind,payload,created_at) VALUES('crawl',?,?)", (encode({"game": code, "mode": mode, "pages": pages}), now())).lastrowid
        connection.execute("INSERT OR IGNORE INTO crawl_state(game_code) VALUES(?)", (code,))
        connection.execute("UPDATE crawl_state SET last_job_id=? WHERE game_code=?", (identity, code))
    return {"job_id": identity, "status": "queued"}


def next_result_sync(spec, current=None):
    current = (current or datetime.now(timezone.utc)).astimezone(ZoneInfo(spec["timezone"]))
    candidates = []
    for offset in range(-1, 8):
        day = current.date() + timedelta(days=offset)
        if day.weekday() not in spec["weekdays"]:
            continue
        for slot in spec["slots"]:
            draw_at = datetime.fromisoformat(f"{day.isoformat()}T{slot}").replace(tzinfo=current.tzinfo)
            for delay in (45, 60):
                target = draw_at + timedelta(minutes=delay)
                if target > current:
                    candidates.append(target)
    return min(candidates).astimezone(timezone.utc).isoformat()

def result_sync_window(spec, current):
    local = current.astimezone(ZoneInfo(spec["timezone"]))
    for offset in (-1, 0):
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in spec["weekdays"]:
            continue
        for slot in spec["slots"]:
            draw_at = datetime.fromisoformat(f"{day.isoformat()}T{slot}").replace(tzinfo=local.tzinfo)
            if timedelta(minutes=45) <= local - draw_at <= timedelta(minutes=60):
                return True
    return False

def run_crawl(payload, job_id, stop=None, fetcher=None):
    code, mode, limit = payload["game"], payload["mode"], payload["pages"]
    spec = crawl_spec(code)
    fetcher = fetcher or Fetcher(stop)
    with connect() as connection:
        state = connection.execute("SELECT * FROM crawl_state WHERE game_code=?", (code,)).fetchone()
    cursor = state["next_history_page"] if state else 1
    complete = bool(state["history_complete"]) if state else False
    pages = [0] if mode == "history" and complete else [0] + list(range(cursor if mode == "history" else 1, (cursor if mode == "history" else 1) + limit - 1))
    if mode == "history" and not complete and limit == 1:
        pages = [cursor]
    result = {"game": code, "source": spec["crawler"]["source"], "mode": mode, "pages_completed": 0, "added": 0,
              "unchanged": 0, "incomplete": 0, "history_complete": complete, "verified": False,
              "message": "Validated crawler results are automatically eligible for model training; source provenance retained"}
    previous_min = None
    for page in pages:
        if stop and stop.is_set():
            raise InterruptedError("Crawler stopped")
        config = spec["crawler"]
        alternate = config["source"] == "ketquaxoso"
        url = config["list_url"] if page == 0 else (
            config["history_url"].format(page=page + 1) if alternate else f"{config['history_url']}?pageIndex={page}")
        raw = fetcher.fetch(url)
        snapshot_id = snapshot(code, url, raw)
        if page > 0 and not raw.strip():
            with connect() as connection:
                connection.execute("UPDATE crawl_state SET history_complete=1 WHERE game_code=?", (code,))
            result["history_complete"] = True
            break
        rows, incomplete = parse_results(raw, spec, url)
        if mode == "history" and page > 0 and incomplete:
            raise CrawlError("History page contains incomplete results; cursor retained for retry")
        if rows:
            oldest = min(row["draw_id"] for row in rows)
            if previous_min is not None and oldest >= previous_min:
                raise CrawlError("Pagination made no historical progress; stopping to avoid a loop")
            previous_min = oldest
        added, unchanged = persist_page(code, rows, snapshot_id, page if mode == "history" else None)
        result["added"] += added
        result["unchanged"] += unchanged
        result["incomplete"] += incomplete
        result["pages_completed"] += 1
        with connect() as connection:
            connection.execute("UPDATE jobs SET result=? WHERE id=?", (encode(result), job_id))
        if alternate and not incomplete and not crawl_ketquaxoso.has_next_page(raw, config, max(1, page + 1)):
            with connect() as connection:
                connection.execute("UPDATE crawl_state SET history_complete=1 WHERE game_code=?", (code,))
            result["history_complete"] = True
            break
    with connect() as connection:
        state = connection.execute("SELECT history_complete FROM crawl_state WHERE game_code=?", (code,)).fetchone()
        due = next_result_sync(spec) if state and state["history_complete"] else (
            datetime.now(timezone.utc) + timedelta(minutes=spec["crawler"]["interval_minutes"])).isoformat()
        connection.execute("UPDATE crawl_state SET last_success_at=?,last_error=NULL,failures=0,next_due=? WHERE game_code=?", (now(), due, code))
    return result


def set_schedule(code, enabled):
    crawl_spec(code)
    with connect() as connection:
        connection.execute("INSERT OR IGNORE INTO crawl_state(game_code) VALUES(?)", (code,))
        connection.execute("UPDATE crawl_state SET scheduled=?,next_due=? WHERE game_code=?", (int(enabled), now() if enabled else None, code))
        connection.execute("INSERT INTO audit_log(action,detail,at) VALUES(?,?,?)", ("crawl_schedule", encode({"game": code, "enabled": enabled}), now()))
    return {"game": code, "scheduled": enabled}


def queue_due():
    current = datetime.now(timezone.utc)
    with connect() as connection:
        due = connection.execute("SELECT game_code,history_complete,failures FROM crawl_state WHERE scheduled=1 AND (next_due IS NULL OR next_due<=?)", (current.isoformat(),)).fetchall()
    for row in due:
        code = row[0]
        try:
            if row["history_complete"] and not row["failures"] and not result_sync_window(game(code), current):
                with connect() as connection:
                    connection.execute("UPDATE crawl_state SET next_due=? WHERE game_code=?", (next_result_sync(game(code), current), code))
                continue
            queue_crawl(code, "latest" if row["history_complete"] else "history", 1 if row["history_complete"] else 20)
        except ValueError as exc:
            record_failure(code, str(exc))


def record_failure(code, error):
    with connect() as connection:
        state = connection.execute("SELECT failures FROM crawl_state WHERE game_code=?", (code,)).fetchone()
        failures = (state[0] if state else 0) + 1
        due = (datetime.now(timezone.utc) + timedelta(minutes=min(1440, 30 * 2 ** min(failures - 1, 6)))).isoformat()
        connection.execute("UPDATE crawl_state SET failures=?,last_error=?,next_due=? WHERE game_code=?", (failures, error, due, code))


def pending(code, limit=100):
    crawl_spec(code)
    with connect() as connection:
        rows = connection.execute("SELECT d.* FROM draws d WHERE d.game_code=? AND d.verified=0 "
                                  "AND EXISTS(SELECT 1 FROM crawl_observations c WHERE c.game_code=d.game_code AND c.draw_id=d.draw_id) ORDER BY d.draw_id DESC LIMIT ?", (code, limit)).fetchall()
    items = [unpack(row) for row in rows]
    return [row | {"fingerprint": fingerprint(row)} for row in items]


def status(code):
    spec = crawl_spec(code)
    with connect() as connection:
        state = connection.execute("SELECT * FROM crawl_state WHERE game_code=?", (code,)).fetchone()
        latest = connection.execute("SELECT id,status,result,error,created_at,finished_at FROM jobs "
                                    "WHERE kind='crawl' AND json_extract(payload,'$.game')=? ORDER BY id DESC LIMIT 1", (code,)).fetchone()
        count = connection.execute("SELECT count(*) FROM draws d WHERE game_code=? AND verified=0 "
                                   "AND EXISTS(SELECT 1 FROM crawl_observations c WHERE c.game_code=d.game_code AND c.draw_id=d.draw_id)", (code,)).fetchone()[0]
    job = dict(latest) if latest else None
    if job:
        job["result"] = json.loads(job["result"]) if job["result"] else None
    return {"game": code, "source": spec["crawler"]["source"], "state": dict(state) if state else None,
            "latest_job": job, "pending_count": count, "interval_minutes": spec["crawler"]["interval_minutes"],
            "verification": "source_only; no manual review required for automatic model use", "data_policy": "verified_or_crawled"}


def verify(code, reviews, source_url, note):
    crawl_spec(code)
    checked = urlsplit(source_url)
    if checked.scheme != "https" or not checked.hostname or checked.username or checked.password:
        raise CrawlError("Verification requires an independent HTTPS result source")
    if any(checked.hostname == host or checked.hostname.endswith("." + host)
           for host in ("xoso.com.vn", "ketquaxoso.net")):
        raise CrawlError("Verification source must be independent of the crawled source")
    if not reviews or len({entry["draw_id"] for entry in reviews}) != len(reviews):
        raise CrawlError("Select unique draws to verify")
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for entry in reviews:
            row = connection.execute("SELECT * FROM draws WHERE game_code=? AND draw_id=?", (code, entry["draw_id"])).fetchone()
            if not row or row["verified"]:
                raise CrawlError("Draw missing or already verified; reload pending list")
            if not connection.execute("SELECT 1 FROM crawl_observations WHERE game_code=? AND draw_id=?", (code, entry["draw_id"])).fetchone():
                raise CrawlError("Only crawled draws can be reviewed through this endpoint")
            item = unpack(row)
            if fingerprint(item) != entry["fingerprint"]:
                raise CrawlError("Draw changed since review; reload and check again")
            serialized = {key: str(value) for key, value in item.items() if key not in ("main", "digits", "special", "verified")}
            serialized.update(main=" ".join(map(str, item["main"])), digits=encode(item["digits"]),
                              special="" if item["special"] is None else str(item["special"]), verified="true")
            validate(serialized)
            connection.execute("UPDATE draws SET verified=1 WHERE game_code=? AND draw_id=?", (code, entry["draw_id"]))
        connection.execute("INSERT INTO audit_log(action,detail,at) VALUES(?,?,?)",
                           ("crawl_operator_verification", encode({"game": code, "reviews": reviews,
                            "checked_source_url": source_url, "note": note, "verification": "operator_attested"}), now()))
    return {"verified": len(reviews), "verification": "operator_attested"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Queue result crawling in the running application's durable SQLite job queue")
    parser.add_argument("game", choices=tuple(code for code, spec in games().items() if spec.get("crawl_enabled")))
    parser.add_argument("--mode", choices=("latest", "history"), default="latest")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--schedule", choices=("on", "off"))
    arguments = parser.parse_args()
    result = set_schedule(arguments.game, arguments.schedule == "on") if arguments.schedule else queue_crawl(arguments.game, arguments.mode, arguments.pages)
    print(json.dumps(result))
