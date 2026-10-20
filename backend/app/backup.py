import argparse
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .db import connect, database_path


def backup(destination):
    target = Path(destination).resolve()
    if target == database_path().resolve() or target.exists():
        raise ValueError("Backup destination must be a new file, not the live database")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not database_path().exists():
        raise ValueError("Source database does not exist")
    with connect() as source:
        with closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Online SQLite backup with integrity verification")
    parser.add_argument("destination", nargs="?", default=f"data/backups/research-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.sqlite3")
    arguments = parser.parse_args()
    print(backup(arguments.destination))
