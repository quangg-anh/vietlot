import argparse
import gzip
import json
import sqlite3
from datetime import date
from pathlib import Path

from .crawler import parse_results
from .etl import unpack
from .games import game


def inspect_draw(database, code, draw_date, snapshot_limit=5):
    game(code)
    date.fromisoformat(draw_date)
    database = Path(database).resolve(strict=True)
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        stored = [unpack(row) for row in connection.execute(
            "SELECT * FROM draws WHERE game_code=? AND draw_date BETWEEN date(?,'-7 days') AND date(?,'+7 days') ORDER BY draw_date,draw_slot",
            (code, draw_date, draw_date))]
        snapshots = connection.execute(
            "SELECT s.* FROM raw_snapshots s WHERE s.game_code=? AND NOT EXISTS(SELECT 1 FROM crawl_observations c WHERE c.snapshot_id=s.id) ORDER BY s.id DESC LIMIT ?",
            (code, snapshot_limit)).fetchall()
        reports = []
        raw_root = (database.parent / "raw").resolve()
        spec = game(code)
        for snapshot in snapshots:
            report = dict(snapshot)
            raw_path = (database.parent / snapshot["relative_path"]).resolve()
            try:
                if not raw_path.is_relative_to(raw_root):
                    raise ValueError("Snapshot path is outside raw storage")
                with gzip.open(raw_path, "rb") as saved:
                    raw = saved.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("Snapshot exceeds size limit")
                reconcile_prefix = spec["crawler"].get("reconcile_url", "").split("{date}")[0]
                historical = snapshot["source"] == "minhchinh" and bool(reconcile_prefix) and snapshot["url"].startswith(reconcile_prefix)
                source_spec = spec | {"crawler": spec["crawler"] | {"source": snapshot["source"], "historical": historical}}
                rows, incomplete = parse_results(raw, source_spec, snapshot["url"])
                report["matching_results"] = [row for row in rows if row["draw_date"] == draw_date]
                report["incomplete"] = incomplete
            except (OSError, ValueError, KeyError, TypeError, EOFError) as exc:
                report["inspection_error"] = str(exc)
            reports.append(report)
        return {"database": str(database), "game": code, "date": draw_date, "read_only": True,
                "stored_nearby": stored, "unprocessed_snapshots": reports}
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspect stored results and unprocessed crawl snapshots without modifying the database")
    parser.add_argument("--database", required=True)
    parser.add_argument("--game", required=True)
    parser.add_argument("--date", required=True)
    arguments = parser.parse_args()
    print(json.dumps(inspect_draw(arguments.database, arguments.game, arguments.date), ensure_ascii=True, indent=2))
