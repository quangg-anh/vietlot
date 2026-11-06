import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .analytics import DISCLAIMER, losses, probabilities, sample_tickets
from .db import connect, encode
from .etl import now, training_history, training_fingerprint
from .games import game
from . import digit_model


def digest(code, target, version, payload, locked):
    return hashlib.sha256(encode([code, target, version, payload, locked]).encode()).hexdigest()


def lock(code, target_draw_id, target_at, method, count, once=False, include_crawled=False):
    spec = game(code)
    if include_crawled and (not spec.get("crawl_enabled") or spec["kind"] not in ("pick_k", "digits")):
        raise ValueError("Automatic source-data predictions support configured crawler games only")
    if not spec["rules_verified"] and not include_crawled:
        raise ValueError("Official rules/schedule not verified; ledger locking disabled")
    target = datetime.fromisoformat(target_at)
    if target.tzinfo is None or target <= datetime.now(timezone.utc):
        raise ValueError("target_at must be a future timezone-aware timestamp")
    local = target.astimezone(ZoneInfo(spec["timezone"]))
    if local.weekday() not in spec["weekdays"] or local.strftime("%H:%M") not in spec["slots"] or local.second or local.microsecond:
        raise ValueError("target_at does not match configured draw schedule")
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if once:
            existing = connection.execute("SELECT * FROM predictions WHERE game_code=? AND target_draw_id=? ORDER BY id DESC LIMIT 1", (code, target_draw_id)).fetchone()
            if existing:
                return {"id": existing["id"], "payload_hash": existing["payload_hash"],
                        "payload": json.loads(existing["payload"]), "locked_at": existing["locked_at"], "disclaimer": DISCLAIMER}
        rows = training_history(code, connection, include_crawled=include_crawled,
                                before=min(target, datetime.now(timezone.utc)))
        latest = connection.execute("SELECT max(draw_id) FROM draws WHERE game_code=?", (code,)).fetchone()[0]
        if not rows or target_draw_id <= (latest or 0):
            raise ValueError("Need eligible history and an unseen future draw ID")
        if rows[-1]["draw_date"] > local.date().isoformat():
            raise ValueError("Training data must precede target")
        seed = secrets.randbits(53)
        version = None
        if spec["kind"] == "digits":
            if method not in ("bayesian", "uniform"):
                raise ValueError("method must be uniform or bayesian")
            policy = "verified_or_crawled" if include_crawled else "verified_only"
            hashed_data = training_fingerprint(rows, spec, digit_model.REVISION, policy)
            saved = connection.execute("SELECT * FROM model_versions WHERE game_code=? AND json_extract(parameters,'$.automatic')=1 AND json_extract(parameters,'$.training_hash')=? ORDER BY id DESC LIMIT 1", (code, hashed_data)).fetchone()
            if method == "uniform":
                parameters = {"digit_probabilities": digit_model.fit([], spec, digit_model.CANDIDATES[0])}
            elif saved:
                parameters = json.loads(saved["parameters"])
                version = saved["id"]
            elif include_crawled:
                raise ValueError("Automatic digit model is stale; retraining required")
            else:
                parameters = digit_model.train(rows, spec, seed)
            parameters = parameters | {"data_policy": policy, "training_hash": hashed_data,
                                       "kind": "digits", "algorithm_revision": digit_model.REVISION}
            probability = parameters["digit_probabilities"]
            predicted = {"kind": "digits", "tickets": digit_model.sample_tickets(probability, spec, count, seed),
                         "digit_probabilities": probability, "model_revision": digit_model.REVISION,
                         "probability_scope": "single_prize_value_not_ticket_win_probability"}
        else:
            probability = probabilities(rows, spec, method)
            predicted = {"kind": "pick_k", "tickets": sample_tickets(probability, spec, count, seed),
                         "probabilities": probability}
            parameters = {"window": 300, "strength": 100, "data_policy": "verified_or_crawled" if include_crawled else "verified_only"}
        payload = encode({**predicted, "seed": seed,
                          "training_hash": hashlib.sha256(encode(rows).encode()).hexdigest(),
                          "data_policy": "verified_or_crawled" if include_crawled else "verified_only",
                          "rules_verified": spec["rules_verified"],
                          "unverified_training_count": sum(not row["verified"] for row in rows),
                          "disclaimer": DISCLAIMER})
        locked = now()
        if datetime.fromisoformat(locked) >= target:
            raise ValueError("Draw deadline passed")
        if version is None:
            version = connection.execute(
                "INSERT INTO model_versions(game_code,method,trained_through_draw_id,parameters,created_at) VALUES(?,?,?,?,?)",
                (code, "digits_" + method if spec["kind"] == "digits" else method, rows[-1]["draw_id"], encode(parameters), locked)).lastrowid
        hashed = digest(code, target_draw_id, version, payload, locked)
        identity = connection.execute(
            "INSERT INTO predictions(game_code,target_draw_id,target_at,model_version_id,payload,payload_hash,locked_at) VALUES(?,?,?,?,?,?,?)",
            (code, target_draw_id, target.astimezone(timezone.utc).isoformat(), version, payload, hashed, locked)).lastrowid
    return {"id": identity, "payload_hash": hashed, "payload": json.loads(payload), "locked_at": locked, "disclaimer": DISCLAIMER}


SCORING_COLUMNS = (
    "d.main AS actual,d.special AS actual_special,d.digits AS actual_digits,"
    "d.draw_date,d.draw_slot,d.verified AS actual_verified,d.source_url AS actual_source_url,"
    "EXISTS(SELECT 1 FROM crawl_observations c WHERE c.game_code=p.game_code "
    "AND c.draw_id=p.target_draw_id) AS actual_crawled"
)


def scoring_blocker(row, current=None):
    target = datetime.fromisoformat(row["target_at"])
    locked = datetime.fromisoformat(row["locked_at"])
    if target.tzinfo is None or locked.tzinfo is None or locked >= target:
        return "invalid_lock_time"
    if digest(row["game_code"], row["target_draw_id"], row["model_version_id"], row["payload"], row["locked_at"]) != row["payload_hash"]:
        return "ledger_hash_mismatch"
    if target > (current or datetime.now(timezone.utc)):
        return "waiting_draw"
    if row["actual"] is None:
        return "waiting_result"
    payload = json.loads(row["payload"])
    if not row["actual_verified"] and (payload.get("data_policy") != "verified_or_crawled" or not row["actual_crawled"]):
        return "source_not_eligible"
    scheduled = target.astimezone(ZoneInfo(game(row["game_code"])["timezone"]))
    if row["draw_date"] != scheduled.date().isoformat() or row["draw_slot"] != scheduled.strftime("%H:%M"):
        return "result_slot_mismatch"
    return None


def score_pending(code=None):
    scored, errors = 0, []
    log = logging.getLogger(__name__)
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        pending = connection.execute(
            "SELECT p.*," + SCORING_COLUMNS + " FROM predictions p "
            "JOIN draws d ON d.game_code=p.game_code AND d.draw_id=p.target_draw_id "
            "LEFT JOIN scores s ON s.prediction_id=p.id WHERE s.prediction_id IS NULL "
            "AND (? IS NULL OR p.game_code=?) ORDER BY p.id", (code, code)).fetchall()
        for row in pending:
            connection.execute("SAVEPOINT score_prediction")
            try:
                blocker = scoring_blocker(row)
                if blocker:
                    if blocker not in ("waiting_draw", "waiting_result", "source_not_eligible"):
                        errors.append({"prediction_id": row["id"], "reason": blocker})
                        log.warning("Scoring blocked: prediction=%s game=%s reason=%s", row["id"], row["game_code"], blocker)
                    continue
                payload, actual = json.loads(row["payload"]), json.loads(row["actual"])
                spec = game(row["game_code"])
                if spec["kind"] == "digits":
                    actual_digits = json.loads(row["actual_digits"])
                    result = digit_model.score(payload["digit_probabilities"], payload["tickets"], actual_digits, spec)
                    result["matched_digits"] = [[value for value in ticket["digits"] if value in actual_digits[ticket["group"]]] for ticket in payload["tickets"]]
                else:
                    if len(payload["probabilities"]) != spec["number_max"]:
                        raise ValueError("Prediction probability vector has incorrect size")
                    result = losses(payload["probabilities"], actual)
                    result["hits"] = [len(set(ticket["main"]) & set(actual)) for ticket in payload["tickets"]]
                    result["matched_numbers"] = [sorted(set(ticket["main"]) & set(actual)) for ticket in payload["tickets"]]
                    result["ticket_width"] = spec["pick_count"]
                    result["special_hits"] = [ticket.get("special") == row["actual_special"] if row["actual_special"] is not None and ticket.get("special") is not None else None for ticket in payload["tickets"]]
                    result["special_scope"] = "separate_number" if row["game_code"] == "lotto535" else "reference_only"
                    result["metric_scope"] = "main_number_matches; not_payouts"
                    result["baseline"] = losses(probabilities([], spec, "uniform"), actual)
                result["source_verified"] = bool(row["actual_verified"])
                result["source_url"] = row["actual_source_url"]
                result["data_policy"] = payload.get("data_policy", "verified_only")
                connection.execute("INSERT INTO scores VALUES(?,?,?)", (row["id"], encode(result), now()))
                connection.execute(
                    "INSERT INTO feedback_events(prediction_id,model_version_id,draw_id,action,detail,at) VALUES(?,?,?,?,?,?)",
                    (row["id"], row["model_version_id"], row["target_draw_id"], "eligible_for_retraining" if spec["kind"] == "digits" else "skipped",
                     encode({"reason": "Actual draw enters the next automatic refit; no promotion or profit claim" if spec["kind"] == "digits" else "Automatic model promotion disabled until independent validation is implemented", "metrics": result}), now()))
                scored += 1
                log.info("Prediction scored: game=%s draw=%s prediction=%s hits=%s", row["game_code"], row["target_draw_id"], row["id"], result["hits"])
            except (ValueError, KeyError, TypeError, IndexError, ZeroDivisionError, OverflowError) as exc:
                connection.execute("ROLLBACK TO score_prediction")
                errors.append({"prediction_id": row["id"], "reason": str(exc)})
                log.exception("Cannot score prediction %s; other predictions will continue", row["id"])
            finally:
                connection.execute("RELEASE score_prediction")
    return {"scored": scored} | ({"errors": errors} if errors else {})
