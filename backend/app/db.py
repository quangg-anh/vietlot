import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def database_path():
    return Path(os.getenv("DATABASE_PATH", "data/research.sqlite3"))


@contextmanager
def connect():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize():
    with connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise RuntimeError("Database schema newer than application")
        connection.executescript(Path(__file__).with_name("schema.sql").read_text())
        if version == 1:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE jobs_new (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('backtest','score','crawl')),
                    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN ('queued','running','done','failed')),
                    result TEXT, error TEXT, created_at TEXT NOT NULL,
                    started_at TEXT, finished_at TEXT
                );
                INSERT INTO jobs_new SELECT * FROM jobs;
                DROP TABLE jobs;
                ALTER TABLE jobs_new RENAME TO jobs;
                CREATE INDEX jobs_status ON jobs(status,id);
            """)
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise sqlite3.IntegrityError("Migration foreign key validation failed")
        connection.execute("PRAGMA user_version=2")


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
