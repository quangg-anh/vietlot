import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .analytics import backtest, probabilities, sample_tickets
from .db import connect, encode
from .etl import now, unpack, training_history, training_fingerprint
from . import digit_model
from .games import game, games
from .ledger import lock, score_pending

log = logging.getLogger(__name__)

def enable_sync():
    if os.getenv("AUTO_SYNC", "1") != "1":
        with connect() as connection:
            connection.execute("UPDATE crawl_state SET scheduled=0")
        return
    with connect() as connection:
        for code, spec in games().items():
            if spec.get("crawl_enabled"):
                connection.execute("INSERT OR IGNORE INTO crawl_state(game_code) VALUES(?)", (code,))
                connection.execute("UPDATE crawl_state SET scheduled=1,next_due=coalesce(next_due,?) WHERE game_code=?", (now(), code))

def next_slot(spec, current=None):
    current = (current or datetime.now(timezone.utc)).astimezone(ZoneInfo(spec["timezone"]))
    candidates = []
    for offset in range(8):
        day = current.date() + timedelta(days=offset)
        if day.weekday() not in spec["weekdays"]:
            continue
        for slot in spec["slots"]:
            target = datetime.fromisoformat(f"{day.isoformat()}T{slot}").replace(tzinfo=current.tzinfo)
            if target > current:
                candidates.append(target)
    return min(candidates).isoformat() if candidates else None

def training_status(code, status, version=None, error=None):
    with connect() as connection:
        connection.execute(
            "INSERT INTO model_training_state(game_code,status,started_at,finished_at,last_error,model_version_id) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(game_code) DO UPDATE SET status=excluded.status,"
            "started_at=CASE WHEN excluded.status='training' THEN excluded.started_at ELSE model_training_state.started_at END,"
            "finished_at=excluded.finished_at,last_error=excluded.last_error,model_version_id=coalesce(excluded.model_version_id,model_training_state.model_version_id)",
            (code, status, now() if status == "training" else None,
             None if status == "training" else now(), error, version))

def active_model(connection, code):
    return connection.execute(
        "SELECT * FROM model_versions WHERE game_code=? AND json_extract(parameters,'$.automatic')=1 "
        "ORDER BY (id=coalesce((SELECT model_version_id FROM model_training_state WHERE game_code=?),0)) DESC,id DESC LIMIT 1",
        (code, code)).fetchone()


def training_hash(rows, spec):
    revision = digit_model.REVISION if spec["kind"] == "digits" else "pick-k-bayesian-v1"
    return training_fingerprint(rows, spec, revision)


def train(code):
    try:
        return train_version(code)
    except Exception as exc:
        training_status(code, "failed", error=str(exc))
        raise


def train_version(code):
    spec = game(code)
    if not spec.get("import_enabled") or spec["kind"] not in ("pick_k", "digits"):
        return None
    rows = training_history(code, include_crawled=True)
    if not rows:
        training_status(code, "waiting_data")
        return None
    hashed = training_hash(rows, spec)
    with connect() as connection:
        latest = active_model(connection, code)
    if latest and json.loads(latest["parameters"]).get("training_hash") == hashed:
        training_status(code, "ready", latest["id"])
        return latest["id"]
    training_status(code, "training")
    seed = int(hashed[:12], 16)
    if spec["kind"] == "digits":
        parameters = digit_model.train(rows, spec, seed)
        method = "digits_adaptive_bayesian"
    else:
        probability = probabilities(rows, spec, "bayesian")
        parameters = {"kind": "pick_k", "probabilities": probability,
                      "metrics": backtest(rows, spec, min_train=10, test_draws=100) if len(rows) > 10 else None,
                      "tickets": sample_tickets(probability, spec, 5, seed), "window": 300, "strength": 100,
                      "algorithm_revision": "pick-k-bayesian-v1", "learning_policy": "refit_on_each_eligible_history_change"}
        method = "bayesian"
    parameters.update(automatic=True, data_policy="verified_or_crawled", training_hash=hashed,
                      training_count=len(rows), unverified_training_count=sum(not row["verified"] for row in rows),
                      preview_only=True)
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = training_history(code, connection, include_crawled=True)
        if training_hash(current, spec) != hashed:
            connection.execute("UPDATE model_training_state SET status='pending',finished_at=? WHERE game_code=?", (now(), code))
            return None
        existing = connection.execute("SELECT id FROM model_versions WHERE game_code=? AND json_extract(parameters,'$.automatic')=1 AND json_extract(parameters,'$.training_hash')=?", (code, hashed)).fetchone()
        if existing:
            identity = existing[0]
        else:
            parent = active_model(connection, code)
            identity = connection.execute("INSERT INTO model_versions(game_code,method,parent_version_id,trained_through_draw_id,parameters,created_at) VALUES(?,?,?,?,?,?)",
                                          (code, method, parent[0] if parent else None, rows[-1]["draw_id"], encode(parameters), now())).lastrowid
            connection.execute("INSERT INTO audit_log(action,detail,at) VALUES(?,?,?)", ("automatic_training", encode({"game": code, "version": identity, "training_hash": hashed}), now()))
        connection.execute("UPDATE model_training_state SET status='ready',finished_at=?,last_error=NULL,model_version_id=? WHERE game_code=?", (now(), identity, code))
    return identity

def previous_slot(spec, target_at):
    target = datetime.fromisoformat(target_at)
    candidates = []
    for offset in range(8):
        day = target.date() - timedelta(days=offset)
        if day.weekday() in spec["weekdays"]:
            for slot in spec["slots"]:
                candidate = datetime.fromisoformat(f"{day.isoformat()}T{slot}").replace(tzinfo=target.tzinfo)
                if candidate < target:
                    candidates.append(candidate)
    return max(candidates) if candidates else None

def predict_next(code):
    spec = game(code)
    if not spec.get("crawl_enabled") or spec["kind"] not in ("pick_k", "digits"):
        return
    rows = training_history(code, include_crawled=True)
    if not rows:
        return
    target_at = next_slot(spec)
    if not target_at:
        return
    previous = previous_slot(spec, target_at)
    if previous is None or (rows[-1]["draw_date"], rows[-1]["draw_slot"]) != (previous.date().isoformat(), previous.strftime("%H:%M")):
        return
    target_id = rows[-1]["draw_id"] + 1
    with connect() as connection:
        exists = connection.execute("SELECT 1 FROM predictions WHERE game_code=? AND target_draw_id=?", (code, target_id)).fetchone()
        latest = connection.execute("SELECT max(draw_id) FROM draws WHERE game_code=?", (code,)).fetchone()[0]
    if not exists and latest == rows[-1]["draw_id"]:
        lock(code, target_id, target_at, "bayesian", 5, once=True, include_crawled=True)

def tick():
    try:
        score_pending()
    except Exception:
        log.exception("Automatic scoring failed; training will continue independently")
    for code, spec in games().items():
        if spec.get("crawl_enabled"):
            try:
                if train(code):
                    predict_next(code)
            except Exception:
                log.exception("Automatic model update failed for %s", code)

def workspace(code, before=None, limit=20):
    spec = game(code)
    with connect() as connection:
        totals = connection.execute("SELECT count(*),coalesce(sum(verified),0) FROM draws WHERE game_code=?", (code,)).fetchone()
        usable = connection.execute("SELECT count(*) FROM draws d WHERE game_code=? AND (verified=1 OR EXISTS(SELECT 1 FROM crawl_observations c WHERE c.game_code=d.game_code AND c.draw_id=d.draw_id))", (code,)).fetchone()[0]
        raw = connection.execute("SELECT d.*,EXISTS(SELECT 1 FROM crawl_observations c WHERE c.game_code=d.game_code AND c.draw_id=d.draw_id) AS crawled FROM draws d WHERE game_code=? AND (? IS NULL OR draw_id<?) ORDER BY draw_id DESC LIMIT ?", (code, before, before, limit)).fetchall()
        model = active_model(connection, code)
        predictions = connection.execute("SELECT p.*,s.result AS score FROM predictions p LEFT JOIN scores s ON s.prediction_id=p.id WHERE p.game_code=? ORDER BY p.id DESC LIMIT 20", (code,)).fetchall()
        page_predictions = connection.execute("SELECT p.target_draw_id,s.result AS score FROM predictions p LEFT JOIN scores s ON s.prediction_id=p.id WHERE p.game_code=? AND p.target_draw_id IN (SELECT draw_id FROM draws WHERE game_code=? AND (? IS NULL OR draw_id<?) ORDER BY draw_id DESC LIMIT ?)", (code, code, before, before, limit)).fetchall()
        state = connection.execute("SELECT * FROM crawl_state WHERE game_code=?", (code,)).fetchone()
        active = connection.execute("SELECT id,status,error,result FROM jobs WHERE kind='crawl' AND json_extract(payload,'$.game')=? ORDER BY id DESC LIMIT 1", (code,)).fetchone()
        training = connection.execute("SELECT * FROM model_training_state WHERE game_code=?", (code,)).fetchone()
        versions = connection.execute("SELECT id,parent_version_id,method,trained_through_draw_id,created_at,json_extract(parameters,'$.training_count') AS training_count FROM model_versions WHERE game_code=? AND json_extract(parameters,'$.automatic')=1 ORDER BY id DESC LIMIT 10", (code,)).fetchall()
        eligible = training_history(code, connection, include_crawled=True)
        usable = len(eligible)
    records = [unpack(row) for row in raw]
    prediction_items = [dict(row) | {"payload": json.loads(row["payload"]), "score": json.loads(row["score"]) if row["score"] else None} for row in predictions]
    by_draw = {}
    for prediction in page_predictions:
        by_draw.setdefault(prediction["target_draw_id"], []).append({"score": json.loads(prediction["score"]) if prediction["score"] else None})
    for record in records:
        record["predictions"] = by_draw.get(record["draw_id"], [])
    parameters = json.loads(model["parameters"]) if model else None
    supported = spec.get("import_enabled", False) and (spec["kind"] == "pick_k" or bool(spec.get("digit_groups")))
    size = spec.get("number_max", 0)
    combinations = math.comb(size, spec["pick_count"]) if supported and spec["kind"] == "pick_k" else None
    if combinations and spec["special_required"] and not spec["special_excludes_main"]:
        combinations *= spec["special_max"]
    chance = 1 / combinations if combinations else None
    baseline = {"kind": "pick_k", "number_count": size, "pick_count": spec["pick_count"], "number_probability": spec["pick_count"] / size, "jackpot_probability": chance, "combinations": combinations} if combinations else None
    if supported and spec["kind"] == "digits":
        baseline = {"kind": "digits", "number_count": 1000, "number_probability": 0.001,
                    "pick_count": 2 if digit_model.target_group(spec) == "special" else 1,
                    "jackpot_probability": None, "combinations": None, "digit_groups": spec["digit_groups"],
                    "target_group": digit_model.target_group(spec), "probability_scope": "single_prize_value_not_ticket_win_probability"}
    target_at = next_slot(spec) if spec.get("crawl_enabled") else None
    previous = previous_slot(spec, target_at) if target_at else None
    forecast = "waiting_data" if not eligible else "waiting_cycle"
    if eligible and previous and (eligible[-1]["draw_date"], eligible[-1]["draw_slot"]) != (previous.date().isoformat(), previous.strftime("%H:%M")):
        forecast = "waiting_previous_result"
    if os.getenv("DISABLE_RUNNER") == "1":
        forecast = "worker_disabled"
    if any(datetime.fromisoformat(item["target_at"]) > datetime.now(timezone.utc) for item in prediction_items):
        forecast = "locked"
    return {"game": code, "supported": supported, "crawl_enabled": spec.get("crawl_enabled", False),
            "kind": spec["kind"], "rules_verified": spec["rules_verified"], "counts": {"total": totals[0], "verified": totals[1], "pending": totals[0] - totals[1]},
            "usable_count": usable, "data_policy": "verified_or_crawled",
            "sync": dict(state) if state else None, "job": dict(active) if active else None,
            "next_at": target_at,
            "model": {"id": model["id"], "parent_version_id": model["parent_version_id"], "method": model["method"], "created_at": model["created_at"], "trained_through": model["trained_through_draw_id"], **parameters} if model else None,
            "training": dict(training) if training else None, "model_versions": [dict(version) for version in versions],
            "retraining_due": bool(eligible and (not parameters or parameters.get("training_hash") != training_hash(eligible, spec))),
            "forecast": {"status": forecast, "required_previous_at": previous.isoformat() if previous else None},
            "baseline": baseline,
            "history": records, "next_before": records[-1]["draw_id"] if len(records) == limit else None,
            "predictions": prediction_items, "runner_enabled": os.getenv("DISABLE_RUNNER") != "1"}
