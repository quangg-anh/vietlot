import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .db import connect, encode
from .games import game


def now():
    return datetime.now(timezone.utc).isoformat()


def unpack(row):
    item = dict(row)
    for field in ("main", "digits"):
        item[field] = json.loads(item[field])
    item["verified"] = bool(item["verified"])
    return item


def history(code, connection=None, include_crawled=False):
    if connection is None:
        with connect() as current:
            return history(code, current, include_crawled)
    return [unpack(row) for row in connection.execute(
        "SELECT d.* FROM draws d WHERE game_code=? AND (verified=1 OR (? AND EXISTS("
        "SELECT 1 FROM crawl_observations c WHERE c.game_code=d.game_code AND c.draw_id=d.draw_id))) "
        "ORDER BY draw_date,draw_slot,draw_id", (code, include_crawled))]


def training_history(code, connection=None, include_crawled=False, before=None):
    spec = game(code)
    cutoff = before or datetime.now(timezone.utc)
    return [row for row in history(code, connection, include_crawled)
            if datetime.fromisoformat(f"{row['draw_date']}T{row['draw_slot']}")
            .replace(tzinfo=ZoneInfo(spec["timezone"])) < cutoff]

def training_fingerprint(rows, spec, revision, policy="verified_or_crawled"):
    return hashlib.sha256(encode([rows, spec, policy, revision]).encode()).hexdigest()

def validate(item):
    if any(value is None or not isinstance(value, str) for value in item.values()):
        raise ValueError("CSV row has missing fields or extra columns")
    code = item.get("game_code") or "power655"
    spec = game(code)
    if not spec["import_enabled"]:
        raise ValueError(f"{code}: import disabled pending official rules review")
    draw_id = int(item["draw_id"])
    if draw_id <= 0:
        raise ValueError("draw_id must be positive")
    draw_date = date.fromisoformat(item["draw_date"])
    if draw_date > datetime.now(ZoneInfo(spec["timezone"])).date():
        raise ValueError("Draw date cannot be in the future")
    slot = item.get("draw_slot", "").strip()
    if len(spec["slots"]) == 1 and not slot:
        slot = spec["slots"][0]
    if slot not in spec["slots"] or draw_date.weekday() not in spec["weekdays"]:
        raise ValueError("Draw date/slot does not match configured schedule")
    main, digits, special = [], {}, None
    if spec["kind"] == "pick_k":
        if item.get("main"):
            main = [int(value) for value in item["main"].split()]
        else:
            main = [int(item[f"n{index}"]) for index in range(1, spec["pick_count"] + 1)]
        if len(main) != spec["pick_count"] or len(set(main)) != len(main):
            raise ValueError("Incorrect count or duplicate main numbers")
        if any(not spec["number_min"] <= number <= spec["number_max"] for number in main):
            raise ValueError("Main number outside configured range")
        if str(item.get("special", "")).strip():
            special = int(item["special"])
            if not spec.get("special_max") or not 1 <= special <= spec["special_max"]:
                raise ValueError("Special number outside configured range")
            if spec["special_excludes_main"] and special in main:
                raise ValueError("Special must differ from main numbers")
        elif spec["special_required"]:
            raise ValueError("Special number required")
    else:
        digits = json.loads(item.get("digits", "{}"))
        if not spec["digit_groups"] or set(digits) != set(spec["digit_groups"]):
            raise ValueError("Digit groups not configured or mismatched")
        for group, count in spec["digit_groups"].items():
            if not isinstance(digits[group], list) or len(digits[group]) != count:
                raise ValueError("Incorrect digit group count")
            if any(not isinstance(value, str) or not re.fullmatch(r"[0-9]{3}", value) for value in digits[group]):
                raise ValueError("Digit values must be three-character strings")
    verified_text = item.get("verified", "").strip().lower()
    if verified_text not in ("", "true", "false"):
        raise ValueError("verified must be true or false")
    source = item.get("source_url", "").strip()
    verified = verified_text == "true"
    draw_at = datetime.fromisoformat(f"{draw_date.isoformat()}T{slot}:00").replace(tzinfo=ZoneInfo(spec["timezone"]))
    if verified and draw_at > datetime.now(timezone.utc):
        raise ValueError("Verified draw cannot precede its scheduled draw time")
    if verified and (urlparse(source).scheme not in ("https", "http") or not urlparse(source).netloc):
        raise ValueError("Verified import requires source_url and independent operator reconciliation")
    return dict(game_code=code, draw_id=draw_id, draw_date=draw_date.isoformat(), draw_slot=slot,
                main=sorted(main), special=special, digits=digits, source_url=source, verified=verified)


def import_csv(raw):
    if len(raw) > 10_000_000:
        raise ValueError("CSV exceeds 10 MB")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if not {"draw_id", "draw_date"}.issubset(reader.fieldnames or []):
        raise ValueError("Missing required CSV headers")
    rows, seen = [], set()
    for line, item in enumerate(reader, 2):
        if len(rows) >= 50000:
            raise ValueError("CSV exceeds 50000 rows")
        try:
            row = validate(item)
            identity = row["game_code"], row["draw_id"]
            if identity in seen:
                raise ValueError("Duplicate game/draw_id inside CSV")
            seen.add(identity)
            rows.append(row)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"CSV line {line}: {exc}") from exc
    if not rows:
        raise ValueError("CSV contains no draws")
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            existing = connection.execute("SELECT * FROM draws WHERE game_code=? AND draw_id=?",
                                          (row["game_code"], row["draw_id"])).fetchone()
            if existing:
                previous = unpack(existing)
                comparable = {field: previous[field] for field in row}
                if comparable == row:
                    continue
                if previous["verified"]:
                    raise ValueError("Verified draw is immutable; use a reviewed migration for corrections")
                connection.execute("INSERT INTO audit_log(action,detail,at) VALUES(?,?,?)",
                                   ("draw_update", encode({"before": comparable, "after": row}), now()))
            connection.execute(
                "INSERT INTO draws VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(game_code,draw_id) DO UPDATE SET "
                "draw_date=excluded.draw_date,draw_slot=excluded.draw_slot,main=excluded.main,"
                "special=excluded.special,digits=excluded.digits,source_url=excluded.source_url,"
                "verified=excluded.verified,imported_at=excluded.imported_at",
                (row["game_code"], row["draw_id"], row["draw_date"], row["draw_slot"], encode(row["main"]),
                 row["special"], encode(row["digits"]), row["source_url"], int(row["verified"]), now()))
        for code in {row["game_code"] for row in rows}:
            ordered = connection.execute("SELECT draw_id FROM draws WHERE game_code=? ORDER BY draw_date,draw_slot", (code,)).fetchall()
            ids = [row[0] for row in ordered]
            if ids != sorted(ids):
                raise ValueError("Draw IDs must increase chronologically")
        connection.execute("INSERT INTO audit_log(action,detail,at) VALUES(?,?,?)",
                           ("csv_import", encode({"sha256": hashlib.sha256(raw).hexdigest(), "rows": len(rows),
                            "verification": "operator attestation; not automatic two-source verification"}), now()))
    return {"processed": len(rows), "verification": "operator_attested", "missing": {code: gaps(code) for code in {row["game_code"] for row in rows}}}


def gaps(code):
    with connect() as connection:
        ids = [row[0] for row in connection.execute("SELECT draw_id FROM draws WHERE game_code=? ORDER BY draw_id", (code,))]
    return [{"from": previous + 1, "to": current - 1} for previous, current in zip(ids, ids[1:]) if current > previous + 1]


def freeze(code):
    payload = encode(history(code))
    if payload == "[]":
        raise ValueError("No verified data to freeze")
    digest = hashlib.sha256(payload.encode()).hexdigest()
    with connect() as connection:
        identity = connection.execute("INSERT INTO datasets(game_code,payload,payload_hash,created_at) VALUES(?,?,?,?)",
                                      (code, payload, digest, now())).lastrowid
    return {"id": identity, "sha256": digest, "draw_count": len(json.loads(payload))}
