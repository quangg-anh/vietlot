import uuid

from .db import connect, encode
from .etl import now
from .games import game


def create_funds(code, method, initial_balance, policy):
    spec = game(code)
    if not spec["rules_verified"] or not spec.get("ticket_price") or not spec.get("prizes"):
        raise ValueError("Fund simulation disabled until official ticket price and payout rules are verified")
    group = uuid.uuid4().hex
    identities = []
    with connect() as connection:
        for owner in (method, "random", "no_bet"):
            identities.append(connection.execute(
                "INSERT INTO sim_funds(group_id,game_code,owner_name,initial_balance,balance,policy,created_at) VALUES(?,?,?,?,?,?,?)",
                (group, code, owner, initial_balance, initial_balance, encode(policy), now())).lastrowid)
    return {"group_id": group, "fund_ids": identities, "money": "simulated",
            "status": "created; betting and settlement not enabled in this release"}
