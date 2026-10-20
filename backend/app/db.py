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
        if connection.execute("PRAGMA user_version").fetchone()[0] > 1:
            raise RuntimeError("Database schema newer than application")
        connection.executescript(Path(__file__).with_name("schema.sql").read_text())
        connection.execute("PRAGMA user_version=1")


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
