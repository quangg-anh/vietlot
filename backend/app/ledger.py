import hashlib
import json
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .analytics import DISCLAIMER, losses, probabilities, sample_tickets
from .db import connect, encode
from .etl import history, now
from .games import game


def digest(code, target, version, payload, locked):
    return hashlib.sha256(encode([code, target, version, payload, locked]).encode()).hexdigest()


def lock(code, target_draw_id, target_at, method, count):
    spec = game(code)
    if not spec["rules_verified"]:
        raise ValueError("Official rules/schedule not verified; ledger locking disabled")
    target = datetime.fromisoformat(target_at)
    if target.tzinfo is None or target <= datetime.now(timezone.utc):
        raise ValueError("target_at must be a future timezone-aware timestamp")
    local = target.astimezone(ZoneInfo(spec["timezone"]))
    if local.weekday() not in spec["weekdays"] or local.strftime("%H:%M") not in spec["slots"] or local.second or local.microsecond:
        raise ValueError("target_at does not match configured draw schedule")
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = history(code, connection)
        latest = connection.execute("SELECT max(draw_id) FROM draws WHERE game_code=?", (code,)).fetchone()[0]
        if not rows or target_draw_id <= (latest or 0):
            raise ValueError("Need verified history and an unseen future draw ID")
        if rows[-1]["draw_date"] > local.date().isoformat():
            raise ValueError("Training data must precede target")
        probability = probabilities(rows, spec, method)
        seed = secrets.randbits(53)
        payload = encode({"tickets": sample_tickets(probability, spec, count, seed),
                          "probabilities": probability, "seed": seed,
                          "training_hash": hashlib.sha256(encode(rows).encode()).hexdigest(),
                          "disclaimer": DISCLAIMER})
        locked = now()
        if datetime.fromisoformat(locked) >= target:
            raise ValueError("Draw deadline passed")
        version = connection.execute(
            "INSERT INTO model_versions(game_code,method,trained_through_draw_id,parameters,created_at) VALUES(?,?,?,?,?)",
            (code, method, rows[-1]["draw_id"], encode({"window": 300, "strength": 100}), locked)).lastrowid
        hashed = digest(code, target_draw_id, version, payload, locked)
        identity = connection.execute(
            "INSERT INTO predictions(game_code,target_draw_id,target_at,model_version_id,payload,payload_hash,locked_at) VALUES(?,?,?,?,?,?,?)",
            (code, target_draw_id, target.astimezone(timezone.utc).isoformat(), version, payload, hashed, locked)).lastrowid
    return {"id": identity, "payload_hash": hashed, "payload": json.loads(payload), "locked_at": locked, "disclaimer": DISCLAIMER}


def score_pending():
    scored = 0
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        pending = connection.execute(
            "SELECT p.*,d.main AS actual,d.draw_date,d.draw_slot FROM predictions p "
            "JOIN draws d ON d.game_code=p.game_code AND d.draw_id=p.target_draw_id AND d.verified=1 "
            "LEFT JOIN scores s ON s.prediction_id=p.id WHERE s.prediction_id IS NULL").fetchall()
        for row in pending:
            if datetime.fromisoformat(row["target_at"]) > datetime.now(timezone.utc):
                continue
            spec = game(row["game_code"])
            scheduled = datetime.fromisoformat(row["target_at"]).astimezone(ZoneInfo(spec["timezone"]))
            if row["draw_date"] != scheduled.date().isoformat() or row["draw_slot"] != scheduled.strftime("%H:%M"):
                continue
            if digest(row["game_code"], row["target_draw_id"], row["model_version_id"], row["payload"], row["locked_at"]) != row["payload_hash"]:
                raise ValueError("Ledger hash mismatch")
            payload, actual = json.loads(row["payload"]), json.loads(row["actual"])
            result = losses(payload["probabilities"], actual)
            result["hits"] = [len(set(ticket["main"]) & set(actual)) for ticket in payload["tickets"]]
            result["baseline"] = losses(probabilities([], spec, "uniform"), actual)
            connection.execute("INSERT INTO scores VALUES(?,?,?)", (row["id"], encode(result), now()))
            connection.execute(
                "INSERT INTO feedback_events(prediction_id,model_version_id,draw_id,action,detail,at) VALUES(?,?,?,?,?,?)",
                (row["id"], row["model_version_id"], row["target_draw_id"], "skipped",
                 encode({"reason": "Automatic model promotion disabled until independent validation is implemented", "metrics": result}), now()))
            scored += 1
    return {"scored": scored}
