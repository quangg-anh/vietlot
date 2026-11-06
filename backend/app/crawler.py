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
from .etl import DrawScheduleError, now, unpack, validate, validate_source
from .games import game, games
from .ledger import score_pending
from . import crawl_ketquaxoso, crawl_minhchinh


def crawl_spec(code):
    spec = game(code)
    if not spec.get("crawl_enabled") or not spec.get("crawler"):
        raise CrawlError(f"Crawler is not configured for {code}")
    for field in ("list_url", "history_url"):
        allowed_url(spec["crawler"][field])
    if spec["crawler"].get("latest_url"):
        allowed_url(spec["crawler"]["latest_url"])
    if spec["crawler"].get("reconcile_url"):
        allowed_url(spec["crawler"]["reconcile_url"].format(date="01-01-2000"))
    return spec


def parse_results(raw, spec, url):
    if spec["crawler"].get("source") == "minhchinh":
        return crawl_minhchinh.parse_results(raw, spec, url)
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
        row = validate_source({"game_code": spec["code"], "draw_id": identity_text[1:],
                        "draw_date": datetime.strptime(dates[0], "%d/%m/%Y").date().isoformat(),
                        "draw_slot": spec["slots"][0], "main": " ".join(map(str, main)),
                        "special": str(specials[0]) if specials else "", "digits": "",
                        "source_url": source, "verified": "false"}, spec)
        previous = identities.get(row["draw_id"])
        fields = ("game_code", "draw_id", "draw_date", "draw_slot", "main", "special", "digits")
        if previous and any(previous[field] != row[field] for field in fields):
            raise CrawlError("Conflicting draw IDs within source page")
        if previous and "_source_date_correction" in row:
            previous.update(row)
        if not previous:
            rows.append(row)
            identities[row["draw_id"]] = row
    if not rows and not incomplete:
        raise CrawlError("No recognized results; parser/source may have changed")
    return rows, incomplete


def snapshot(code, url, raw, source=None):
    digest = hashlib.sha256(raw).hexdigest()
    relative = f"raw/{code}/{digest}.html.gz"
    path = database_path().parent / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with gzip.open(path, "wb") as output:
            output.write(raw)
    with connect() as connection:
        return connection.execute("INSERT INTO raw_snapshots(game_code,source,url,content_hash,relative_path,fetched_at) VALUES(?,?,?,?,?,?)",
                                  (code, source or game(code)["crawler"]["source"], url, digest, relative, now())).lastrowid


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

class DrawConflict(CrawlError):
    def __init__(self, message, incoming, stored):
        super().__init__(message)
        self.incoming = incoming
        self.stored = stored

def draw_conflict(incoming, stored, snapshot_id):
    fields = ("draw_id", "draw_date", "draw_slot", "main", "special", "digits", "source_url", "verified")
    detail = {"snapshot_id": snapshot_id, "incoming": {field: incoming[field] for field in fields},
              "stored": [{field: row[field] for field in fields} for row in stored]}
    return DrawConflict(f"Source draw #{incoming['draw_id']} conflicts with stored draw #{stored[-1]['draw_id']} "
                        f"at {incoming['draw_date']} {incoming['draw_slot']}; details={encode(detail)}; no records overwritten",
                        incoming, stored)

def persist_page(code, rows, snapshot_id, page, evidence=None, corrections=None):
    added, unchanged = 0, 0
    accepted_ids, aliases = [], []
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            if row["game_code"] != code:
                raise CrawlError("Result game does not match crawl job")
            existing = connection.execute("SELECT * FROM draws WHERE game_code=? AND draw_id=?", (code, row["draw_id"])).fetchone()
            same_slot = connection.execute("SELECT * FROM draws WHERE game_code=? AND draw_date=? AND draw_slot=?",
                                           (code, row["draw_date"], row["draw_slot"])).fetchone()
            if existing and same_slot and existing["draw_id"] != same_slot["draw_id"]:
                raise draw_conflict(row, [unpack(existing), unpack(same_slot)], snapshot_id)
            existing = existing or same_slot
            canonical_id = existing["draw_id"] if existing else row["draw_id"]
            if existing:
                previous = unpack(existing)
                if any(previous[field] != row[field] for field in ("draw_date", "draw_slot", "main", "special", "digits")):
                    raise draw_conflict(row, [previous], snapshot_id)
                if canonical_id != row["draw_id"]:
                    aliases.append({"source_draw_id": row["draw_id"], "stored_draw_id": canonical_id,
                                    "draw_date": row["draw_date"], "draw_slot": row["draw_slot"]})
                unchanged += 1
            else:
                connection.execute("INSERT INTO draws VALUES(?,?,?,?,?,?,?,?,?,?)",
                                   (code, row["draw_id"], row["draw_date"], row["draw_slot"], encode(row["main"]),
                                    row["special"], encode(row["digits"]), row["source_url"], 0, now()))
                added += 1
            observation = (evidence or {}).get(row["draw_id"], snapshot_id)
            connection.execute("INSERT OR IGNORE INTO crawl_observations VALUES(?,?,?)", (observation, code, canonical_id))
            accepted_ids.append(canonical_id)
        ordered = connection.execute("SELECT draw_id FROM draws WHERE game_code=? ORDER BY draw_date,draw_slot", (code,)).fetchall()
        ids = [row[0] for row in ordered]
        if ids != sorted(ids):
            raise CrawlError("Crawled draw IDs do not increase chronologically")
        connection.execute("INSERT OR IGNORE INTO crawl_state(game_code) VALUES(?)", (code,))
        if accepted_ids:
            oldest = min(accepted_ids)
            connection.execute("UPDATE crawl_state SET oldest_draw_id=min(coalesce(oldest_draw_id,?),?) WHERE game_code=?", (oldest, oldest, code))
        if page is not None and page > 0:
            connection.execute("UPDATE crawl_state SET next_history_page=? WHERE game_code=?", (page + 1, code))
        connection.execute("INSERT INTO audit_log(action,detail,at) VALUES(?,?,?)",
                           ("crawl_import", encode({"game": code, "snapshot_id": snapshot_id, "added": added,
                            "unchanged": unchanged, "draw_id_aliases": aliases,
                            "source_date_corrections": corrections or [], "verified": False}), now()))
    for alias in aliases:
        logging.getLogger(__name__).warning("%s: source draw #%s matches stored draw #%s at %s %s; identical results, stored ID retained",
                                            code, alias["source_draw_id"], alias["stored_draw_id"], alias["draw_date"], alias["draw_slot"])
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

def expected_result_slot(spec, current=None):
    current = (current or datetime.now(timezone.utc)).astimezone(ZoneInfo(spec["timezone"]))
    candidates = []
    for offset in range(8):
        day = current.date() - timedelta(days=offset)
        if day.weekday() not in spec["weekdays"]:
            continue
        for slot in spec["slots"]:
            target = datetime.fromisoformat(f"{day.isoformat()}T{slot}").replace(tzinfo=current.tzinfo)
            if target + timedelta(minutes=45) <= current:
                candidates.append(target)
    return max(candidates)

def read_result_page(code, spec, url, fetcher, source, allow_empty=False):
    raw = fetcher.fetch(url)
    snapshot_id = snapshot(code, url, raw, source=source)
    approvals = {}
    while True:
        page_spec = spec | {"_source_date_corrections": approvals}
        try:
            rows, incomplete = ([], 0) if allow_empty and not raw.strip() else parse_results(raw, page_spec, url)
            return raw, snapshot_id, rows, incomplete
        except DrawScheduleError as exc:
            identity = int(exc.item["draw_id"])
            context = f"game={code} source={source} url={url} snapshot_id={snapshot_id}"
            if identity in approvals or len(approvals) >= 3:
                raise CrawlError(f"{exc}; {context}; schedule reconciliation limit reached") from exc
            try:
                approvals[identity] = corroborate_schedule(code, spec, exc.item, snapshot_id, fetcher)
            except ValueError as cause:
                raise CrawlError(f"{exc}; {context}; schedule reconciliation failed: {cause}") from cause
        except ValueError as exc:
            raise CrawlError(f"{exc}; game={code} source={source} url={url} snapshot_id={snapshot_id}") from exc


def corroborate_schedule(code, spec, incoming, snapshot_id, fetcher):
    template = spec["crawler"].get("reconcile_url")
    if not template:
        raise CrawlError("No historical reconciliation source configured")
    date_text = datetime.fromisoformat(incoming["draw_date"]).strftime("%d-%m-%Y")
    check_url = template.format(date=date_text)
    allowed_url(check_url)
    if urlsplit(check_url).hostname == urlsplit(incoming["source_url"]).hostname:
        raise CrawlError("A separate source is required for schedule reconciliation")
    check_spec = spec | {"crawler": spec["crawler"] | {"source": "minhchinh", "historical": True}}
    _, check_snapshot, confirmed, incomplete = read_result_page(code, check_spec, check_url, fetcher, "minhchinh")
    candidates = [row for row in confirmed if row["draw_id"] == int(incoming["draw_id"])]
    if incomplete or len(candidates) != 1:
        raise CrawlError("Missing or incomplete corroborating draw")
    corrected = candidates[0]
    normalized = validate(incoming | {"draw_date": corrected["draw_date"], "draw_slot": corrected["draw_slot"]})
    fields = ("game_code", "draw_id", "main", "special", "digits")
    if any(normalized[field] != corrected[field] for field in fields):
        raise CrawlError("Corroborating draw ID or numbers do not match source")
    return {"reason": "source_schedule_mismatch", "original": incoming, "corrected": corrected,
            "original_snapshot_id": snapshot_id, "evidence_snapshot_id": check_snapshot}

def persist_with_reconciliation(code, spec, rows, snapshot_id, page, fetcher):
    working = [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]
    evidence = {row["draw_id"]: row["_evidence_snapshot_id"] for row in rows if "_evidence_snapshot_id" in row}
    corrections = [row["_source_date_correction"] for row in rows if "_source_date_correction" in row]
    attempted = set(evidence)
    identity_fields = ("game_code", "draw_id", "draw_date", "draw_slot", "main", "special", "digits")
    result_fields = ("main", "special", "digits")
    while True:
        try:
            added, unchanged = persist_page(code, working, snapshot_id, page, evidence, corrections)
            return added, unchanged, corrections
        except DrawConflict as conflict:
            incoming = conflict.incoming
            template = spec["crawler"].get("reconcile_url")
            if not template or incoming["draw_id"] in attempted or len(attempted) >= 3:
                raise
            date_text = datetime.fromisoformat(incoming["draw_date"]).strftime("%d-%m-%Y")
            check_url = template.format(date=date_text)
            allowed_url(check_url)
            if urlsplit(check_url).hostname == urlsplit(incoming["source_url"]).hostname:
                raise
            attempted.add(incoming["draw_id"])
            check_spec = spec | {"crawler": spec["crawler"] | {"source": "minhchinh", "historical": True}}
            try:
                _, check_snapshot, confirmed, incomplete = read_result_page(code, check_spec, check_url, fetcher, "minhchinh")
            except ValueError as exc:
                raise CrawlError(f"Date reconciliation unavailable: {exc}; original conflict: {conflict}") from exc
            candidates = [row for row in confirmed if row["draw_id"] == incoming["draw_id"]]
            slot_results = [row for row in confirmed if (row["draw_date"], row["draw_slot"]) == (incoming["draw_date"], incoming["draw_slot"])]
            if incomplete or len(candidates) != 1 or len(slot_results) != 1:
                raise conflict
            corrected, slot_result = candidates[0], slot_results[0]
            if slot_result["draw_id"] == incoming["draw_id"] or any(corrected[field] != incoming[field] for field in result_fields):
                raise conflict
            if any(not any(all(row[field] == stored[field] for field in identity_fields) for row in confirmed) for stored in conflict.stored):
                raise conflict
            correction = {"original": incoming, "corrected": corrected, "corroborated_slot": slot_result,
                          "original_snapshot_id": snapshot_id, "evidence_snapshot_id": check_snapshot}
            corrections.append(correction)
            working = [corrected if row["draw_id"] == incoming["draw_id"] else row for row in working]
            evidence[corrected["draw_id"]] = check_snapshot
            if not any(row["draw_id"] == slot_result["draw_id"] for row in working):
                working.append(slot_result)
                evidence[slot_result["draw_id"]] = check_snapshot

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
              "unchanged": 0, "incomplete": 0, "scored": 0, "history_complete": complete, "verified": False,
              "message": "Validated crawler results are automatically eligible for model training; source provenance retained"}
    previous_min = None
    checked_latest = False
    source_latest_id = None
    for page in pages:
        if stop and stop.is_set():
            raise InterruptedError("Crawler stopped")
        config = spec["crawler"]
        alternate = config["source"] == "ketquaxoso"
        latest_source = config.get("latest_source", config["source"])
        page_source = latest_source if page == 0 else config["source"]
        page_spec = spec | {"crawler": config | {"source": page_source}}
        url = config.get("latest_url", config["list_url"]) if page == 0 else (
            config["history_url"].format(page=page + 1) if alternate else f"{config['history_url']}?pageIndex={page}")
        try:
            raw, snapshot_id, rows, incomplete = read_result_page(code, page_spec, url, fetcher, page_source, allow_empty=page > 0)
            if page == 0 and page_source != config["source"] and not rows and incomplete:
                raise CrawlError("Latest source results are incomplete; trying backup source")
        except ValueError as exc:
            if page != 0 or page_source == config["source"]:
                raise
            result["source_warning"] = f"{page_source}: {exc}"
            logging.getLogger(__name__).warning("%s latest source unavailable: %s; trying %s", code, exc, config["source"])
            page_source = config["source"]
            url = config["list_url"]
            raw, snapshot_id, rows, incomplete = read_result_page(code, spec, url, fetcher, page_source)
        if page > 0 and not raw.strip():
            with connect() as connection:
                connection.execute("UPDATE crawl_state SET history_complete=1 WHERE game_code=?", (code,))
            result["history_complete"] = True
            break
        if page == 0:
            checked_latest = True
            result["source"] = page_source
            result["history_source"] = config["source"]
            source_latest_id = max((row["draw_id"] for row in rows), default=None)
        if mode == "history" and page > 0 and incomplete:
            raise CrawlError("History page contains incomplete results; cursor retained for retry")
        if rows:
            oldest = min(row["draw_id"] for row in rows)
            if previous_min is not None and oldest >= previous_min:
                raise CrawlError("Pagination made no historical progress; stopping to avoid a loop")
            previous_min = oldest
        added, unchanged, corrections = persist_with_reconciliation(code, spec, rows, snapshot_id, page if mode == "history" else None, fetcher)
        if corrections:
            result.setdefault("source_corrections", []).extend(corrections)
            logging.getLogger(__name__).warning("%s: reconciled %s source date errors using Minh Chinh; stored results unchanged", code, len(corrections))
        try:
            scoring = score_pending(code)
            result["scored"] += scoring["scored"]
            if scoring.get("errors"):
                result["scoring_errors"] = scoring["errors"]
        except Exception as exc:
            logging.getLogger(__name__).exception("%s results saved but immediate scoring failed", code)
            result["scoring_errors"] = [{"reason": str(exc)}]
        result["added"] += added
        result["unchanged"] += unchanged
        result["incomplete"] += incomplete
        result["pages_completed"] += 1
        with connect() as connection:
            connection.execute("UPDATE jobs SET result=? WHERE id=?", (encode(result), job_id))
        if alternate and not incomplete and (page > 0 or page_source == config["source"]) and not crawl_ketquaxoso.has_next_page(raw, config, max(1, page + 1)):
            with connect() as connection:
                connection.execute("UPDATE crawl_state SET history_complete=1 WHERE game_code=?", (code,))
            result["history_complete"] = True
            break
    with connect() as connection:
        state = connection.execute("SELECT history_complete FROM crawl_state WHERE game_code=?", (code,)).fetchone()
        latest = connection.execute("SELECT draw_id,draw_date,draw_slot FROM draws WHERE game_code=? ORDER BY draw_date DESC,draw_slot DESC LIMIT 1", (code,)).fetchone()
        expected = expected_result_slot(spec)
        stale = checked_latest and (latest is None or (latest["draw_date"], latest["draw_slot"]) < (expected.date().isoformat(), expected.strftime("%H:%M")))
        due = next_result_sync(spec) if state and state["history_complete"] else (
            datetime.now(timezone.utc) + timedelta(minutes=spec["crawler"]["interval_minutes"])).isoformat()
        warning = None
        if stale:
            due = min(due, (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat())
            warning = f"Source stale: latest source draw #{source_latest_id or '?'}; waiting for {expected.isoformat()}; retry in 15 minutes"
            logging.getLogger(__name__).warning("%s: %s", code, warning)
        result.update(source_stale=stale, source_latest_draw_id=source_latest_id,
                      latest_draw_id=latest["draw_id"] if latest else None,
                      expected_result_at=expected.isoformat(), next_due=due)
        connection.execute("UPDATE crawl_state SET last_success_at=?,last_error=?,failures=0,next_due=? WHERE game_code=?", (now(), warning, due, code))
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
        due = connection.execute("SELECT game_code,history_complete,failures,next_due,last_success_at,last_error FROM crawl_state WHERE scheduled=1 AND (next_due IS NULL OR next_due<=?)", (current.isoformat(),)).fetchall()
    for row in due:
        code = row[0]
        try:
            spec = game(code)
            previous_due = datetime.fromisoformat(row["next_due"]) if row["next_due"] else None
            if previous_due and previous_due.tzinfo is None:
                previous_due = previous_due.replace(tzinfo=timezone.utc)
            missed_result_sync = previous_due is not None and result_sync_window(spec, previous_due)
            stale_retry = (row["last_error"] or "").startswith("Source stale:")
            if row["history_complete"] and row["last_success_at"] and not row["failures"] and not stale_retry and not missed_result_sync and not result_sync_window(spec, current):
                with connect() as connection:
                    connection.execute("UPDATE crawl_state SET next_due=? WHERE game_code=?", (next_result_sync(spec, current), code))
                continue
            queued = queue_crawl(code, "latest" if row["history_complete"] else "history", 1 if row["history_complete"] else 20)
            if queued["status"] == "queued":
                logging.getLogger(__name__).info("Automatic sync queued: game=%s job=%s mode=%s", code, queued["job_id"], "latest" if row["history_complete"] else "history")
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
    return {"game": code, "source": spec["crawler"].get("latest_source", spec["crawler"]["source"]), "state": dict(state) if state else None,
            "source_url": spec["crawler"].get("latest_url", spec["crawler"]["list_url"]),
            "history_source": spec["crawler"]["source"],
            "latest_job": job, "pending_count": count, "interval_minutes": spec["crawler"]["interval_minutes"],
            "verification": "source_only; no manual review required for automatic model use", "data_policy": "verified_or_crawled"}


def verify(code, reviews, source_url, note):
    crawl_spec(code)
    checked = urlsplit(source_url)
    if checked.scheme != "https" or not checked.hostname or checked.username or checked.password:
        raise CrawlError("Verification requires an independent HTTPS result source")
    if any(checked.hostname == host or checked.hostname.endswith("." + host)
           for host in ("xoso.com.vn", "ketquaxoso.net", "minhchinh.com")):
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
