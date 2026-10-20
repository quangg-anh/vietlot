import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .analytics import DISCLAIMER, frequency
from .db import connect, encode, initialize
from .etl import freeze, gaps, history, import_csv, unpack
from .funds import create_funds
from .games import game, games
from .jobs import Runner, enqueue
from .lab import illusion, randomness
from .ledger import lock


logging.basicConfig(level=logging.INFO)
ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))


@asynccontextmanager
async def lifespan(app):
    initialize()
    runner = Runner()
    if os.getenv("DISABLE_RUNNER") != "1":
        runner.start()
    yield
    if runner.thread.is_alive():
        runner.close()


app = FastAPI(title="Vietlott Research Lab", version="2.0.0", lifespan=lifespan, docs_url=None, redoc_url=None,
              description="Verified-data research. No guaranteed winning numbers or profit.")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
visitors = OrderedDict()
visitors_lock = threading.Lock()


@app.middleware("http")
async def controls(request, call_next):
    if request.url.path.startswith("/api/"):
        address = request.client.host if request.client else "unknown"
        with visitors_lock:
            current = time.monotonic()
            count, started = visitors.pop(address, (0, current))
            if current - started >= 60:
                count, started = 0, current
            visitors[address] = count + 1, started
            while len(visitors) > 4096:
                visitors.popitem(last=False)
        if count >= 120:
            return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ValueError)
async def invalid_input(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(sqlite3.IntegrityError)
async def conflict(request, exc):
    return JSONResponse({"detail": "Data conflict; no changes saved"}, status_code=409)


def require_key(x_api_key: str | None = Header(default=None)):
    expected = os.getenv("API_KEY", "")
    if not expected or expected.startswith("REPLACE_"):
        raise HTTPException(503, "Set a strong API_KEY to enable write endpoints")
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, "Missing or invalid API key")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BacktestRequest(StrictModel):
    game: str = "power655"
    method: Literal["uniform", "bayesian"] = "bayesian"
    min_train: int = Field(default=100, ge=10, le=10000)
    test_draws: int = Field(default=100, ge=1, le=1000)
    seed: int = Field(default=42, ge=0, le=2**32 - 1)


class PredictRequest(StrictModel):
    target_draw_id: int = Field(gt=0)
    target_at: str
    method: Literal["uniform", "bayesian"] = "bayesian"
    count: int = Field(default=5, ge=1, le=100)


class GameRequest(StrictModel):
    game: str


class FundPolicy(StrictModel):
    tickets_per_draw: int = Field(default=1, ge=1, le=10)
    max_fraction: float = Field(default=0.01, gt=0, le=0.05)
    max_daily_cost: int = Field(default=100000, gt=0, le=1000000)


class FundRequest(GameRequest):
    method: Literal["uniform", "bayesian"] = "bayesian"
    initial_balance: int = Field(default=1000000, ge=10000, le=1000000000)
    policy: FundPolicy = Field(default_factory=FundPolicy)


def summary(code):
    game(code)
    with connect() as connection:
        counts = connection.execute("SELECT count(*),coalesce(sum(verified),0) FROM draws WHERE game_code=?", (code,)).fetchone()
        latest = connection.execute("SELECT * FROM draws WHERE game_code=? AND verified=1 ORDER BY draw_date DESC,draw_slot DESC LIMIT 1", (code,)).fetchone()
    return {"game": code, "draw_count": counts[0], "verified_count": counts[1],
            "verified_ratio": counts[1] / counts[0] if counts[0] else 0,
            "latest": unpack(latest) if latest else None, "missing_ranges": gaps(code), "disclaimer": DISCLAIMER}


@app.get("/api/health")
def health():
    with connect() as connection:
        connection.execute("SELECT 1").fetchone()
    return {"status": "ok", "version": app.version, "storage": "sqlite",
            "crawl": "disabled: official sources/rules require review", "disclaimer": DISCLAIMER}


@app.get("/api/games")
def list_games():
    return list(games().values())


@app.get("/api/games/{code}/summary")
def game_summary(code: str):
    return summary(code)


@app.get("/api/games/{code}/draws")
def draws(code: str, limit: int = Query(100, ge=1, le=1000), before: int | None = Query(None, gt=0)):
    game(code)
    with connect() as connection:
        rows = connection.execute("SELECT * FROM draws WHERE game_code=? AND verified=1 AND (? IS NULL OR draw_id<?) ORDER BY draw_id DESC LIMIT ?",
                                  (code, before, before, limit)).fetchall()
    return {"items": [unpack(row) for row in rows], "next_before": rows[-1]["draw_id"] if len(rows) == limit else None, "disclaimer": DISCLAIMER}


@app.get("/api/games/{code}/stats/frequency")
def stats(code: str):
    return {"items": frequency(history(code), game(code)), "disclaimer": DISCLAIMER}


@app.post("/api/games/{code}/randomness", dependencies=[Depends(require_key)])
def lab(code: str):
    return randomness(history(code), game(code))


@app.get("/api/games/{code}/ev")
def ev(code: str):
    game(code)
    raise HTTPException(409, "EV unavailable until official payout rules, jackpot sharing and ticket price are verified")


@app.post("/api/games/{code}/predict", dependencies=[Depends(require_key)])
def predict(code: str, body: PredictRequest):
    return lock(code, body.target_draw_id, body.target_at, body.method, body.count)


@app.get("/api/ledger")
def ledger(game: str | None = None, limit: int = Query(100, ge=1, le=500), before: int | None = Query(None, gt=0)):
    with connect() as connection:
        rows = connection.execute("SELECT p.*,s.result AS score FROM predictions p LEFT JOIN scores s ON s.prediction_id=p.id "
                                  "WHERE (? IS NULL OR p.game_code=?) AND (? IS NULL OR p.id<?) ORDER BY p.id DESC LIMIT ?",
                                  (game, game, before, before, limit)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["score"] = json.loads(item["score"]) if item["score"] else None
        items.append(item)
    return {"items": items, "disclaimer": DISCLAIMER}


@app.post("/api/import/csv", dependencies=[Depends(require_key)])
async def upload(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, "Upload a .csv file")
    raw = await file.read(10_000_001)
    if len(raw) > 10_000_000:
        raise HTTPException(413, "CSV exceeds 10 MB")
    result = await run_in_threadpool(import_csv, raw)
    enqueue("score", {})
    return result


@app.post("/api/datasets/freeze", dependencies=[Depends(require_key)])
def freeze_dataset(body: GameRequest):
    game(body.game)
    return freeze(body.game)


@app.post("/api/backtest", dependencies=[Depends(require_key)], status_code=202)
def queue_backtest(body: BacktestRequest):
    spec = game(body.game)
    rows = history(body.game)
    if spec["kind"] != "pick_k" or len(rows) <= body.min_train:
        raise ValueError("Need pick_k game and more verified draws than min_train")
    payload = body.model_dump() | {"rows": rows, "spec": spec, "dataset_hash": hashlib.sha256(encode(rows).encode()).hexdigest()}
    with connect() as connection:
        if connection.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 20:
            raise HTTPException(429, "Job queue full")
    return {"job_id": enqueue("backtest", payload), "status": "queued"}


@app.get("/api/jobs/{identity}")
def job_status(identity: int):
    with connect() as connection:
        row = connection.execute("SELECT id,kind,status,result,error,created_at,started_at,finished_at FROM jobs WHERE id=?", (identity,)).fetchone()
    if not row:
        raise HTTPException(404, "Job not found")
    item = dict(row)
    item["result"] = json.loads(item["result"]) if item["result"] else None
    return item


@app.get("/api/backtest/{identity}")
def backtest_result(identity: int):
    with connect() as connection:
        row = connection.execute("SELECT metrics FROM backtest_runs WHERE job_id=?", (identity,)).fetchone()
    if not row:
        raise HTTPException(404, "Backtest result not found; use job ID")
    return json.loads(row[0])


@app.get("/api/models/{identity}/feedback")
def feedback(identity: int):
    with connect() as connection:
        rows = connection.execute("SELECT * FROM feedback_events WHERE model_version_id=? ORDER BY id DESC LIMIT 500", (identity,)).fetchall()
    return [dict(row) | {"detail": json.loads(row["detail"])} for row in rows]


@app.post("/api/funds", dependencies=[Depends(require_key)])
def new_fund(body: FundRequest):
    return create_funds(body.game, body.method, body.initial_balance, body.policy.model_dump())


@app.get("/api/funds")
def funds(game: str | None = None):
    with connect() as connection:
        rows = connection.execute("SELECT * FROM sim_funds WHERE ? IS NULL OR game_code=? ORDER BY id DESC LIMIT 500", (game, game)).fetchall()
    return {"items": [dict(row) | {"policy": json.loads(row["policy"])} for row in rows], "money": "simulated"}


@app.post("/api/simulator/illusion", dependencies=[Depends(require_key)])
def simulate(body: GameRequest):
    return illusion(game(body.game))


@app.get("/")
def dashboard(request: Request, code: str = "power655"):
    spec = game(code)
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "games": games(), "selected": spec, "summary": summary(code),
        "draws": draws(code, 20, None)["items"], "frequency": frequency(history(code), spec),
    })


@app.get("/docs", include_in_schema=False)
def documentation(request: Request):
    operations = [(path, method.upper(), operation) for path, methods in app.openapi()["paths"].items()
                  for method, operation in methods.items() if method in {"get", "post"}]
    return templates.TemplateResponse(request=request, name="docs.html", context={"operations": operations})
