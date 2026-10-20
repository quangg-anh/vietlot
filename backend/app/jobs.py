import json
import logging
import threading
from datetime import timedelta, datetime, timezone

from .analytics import backtest
from .db import connect, encode
from .etl import now
from .ledger import score_pending


log = logging.getLogger(__name__)


def enqueue(kind, payload):
    with connect() as connection:
        return connection.execute("INSERT INTO jobs(kind,payload,created_at) VALUES(?,?,?)",
                                  (kind, encode(payload), now())).lastrowid


def recover():
    with connect() as connection:
        connection.execute("UPDATE jobs SET status='queued',started_at=NULL WHERE status='running'")


def run_one():
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return False
        connection.execute("UPDATE jobs SET status='running',started_at=?,error=NULL WHERE id=?", (now(), row["id"]))
    try:
        payload = json.loads(row["payload"])
        if row["kind"] == "backtest":
            result = backtest(payload["rows"], payload["spec"], payload["min_train"],
                              payload["test_draws"], payload["method"], payload["seed"])
            result["dataset_hash"] = payload["dataset_hash"]
            with connect() as connection:
                connection.execute("INSERT OR IGNORE INTO backtest_runs(job_id,game_code,config,metrics,created_at) VALUES(?,?,?,?,?)",
                                   (row["id"], payload["game"], encode({key: value for key, value in payload.items() if key != "rows"}), encode(result), now()))
        else:
            result = score_pending()
        with connect() as connection:
            connection.execute("UPDATE jobs SET status='done',result=?,finished_at=? WHERE id=?", (encode(result), now(), row["id"]))
    except Exception as exc:
        log.exception("Job %s failed", row["id"])
        with connect() as connection:
            connection.execute("UPDATE jobs SET status='failed',error=?,finished_at=? WHERE id=?", (str(exc), now(), row["id"]))
    return True


class Runner:
    def __init__(self):
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="sqlite-job-runner", daemon=True)

    def start(self):
        recover()
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join()

    def loop(self):
        next_score = datetime.now(timezone.utc)
        while not self.stop.is_set():
            try:
                if datetime.now(timezone.utc) >= next_score:
                    with connect() as connection:
                        pending = connection.execute("SELECT 1 FROM jobs WHERE kind='score' AND status IN ('queued','running')").fetchone()
                    if not pending:
                        enqueue("score", {})
                    next_score = datetime.now(timezone.utc) + timedelta(minutes=5)
                if not run_one():
                    self.stop.wait(1)
            except Exception:
                log.exception("Runner iteration failed")
                self.stop.wait(5)
